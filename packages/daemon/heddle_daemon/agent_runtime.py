# SPDX-License-Identifier: Apache-2.0
"""Self-written agent runtime — feat-019 (D-053).

A hand-rolled LLM ↔ tools loop. NOT a LangChain ``AgentExecutor``;
this module is the single source of truth for the heddle agent's
reasoning cycle, so future LLM-provider changes, tool-protocol
upgrades, and stop-condition tweaks only need to touch one file.

Loop
----

For a single user message:

    1. Load thread state from the LangGraph checkpointer (feat-018).
       If the thread is new, build a fresh state with just the system
       prompt + the user's new message.
    2. Call the LLM with the current messages list.
    3. Append the LLM response to the messages.
    4. If the response carries tool calls: dispatch each one through
       the sandbox middleware (feat-021), append the tool result as a
       ``tool`` message, persist, and go back to step 2.
    5. If the response carries no tool calls (or
       ``stop_reason == "end_turn"``), persist and return the final
       assistant message.
    6. If ``max_steps`` is reached before step 5: raise
       :class:`RecursionLimitError` with ``cause="recursion_limit"``
       per D-052. Callers (CLI / supervisor) translate this into the
       "red in_progress sub-state" the user sees in the kanban.

The state shape persisted to LangGraph is just ``{"messages": [...]}``
— a list of dicts in the standard OpenAI / LangChain message format.
LangGraph keys it by ``thread_id`` (= ``feature_id``) via the
checkpointer; on resume, we read it back as the starting messages
list for the next call.

LLM abstraction
---------------

The runtime takes any callable matching
``Callable[[list[dict]], Awaitable[LLMResponse] | LLMResponse]``. In
production that's a LangChain ``BaseChatModel.invoke`` wrapped to
match the signature; in tests it's ``FakeLLM`` (heddle_common.fake_llm)
or a hand-written stub. The upside: agent_runtime.py never imports
langchain_core chat-model types directly, so the runtime can be unit
tested without bringing in the LLM SDK.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence, Union

from langgraph.checkpoint.base import BaseCheckpointSaver

from heddle_common import logging as _logging
from heddle_daemon.checkpointing import ProjectCheckpointStore
from heddle_daemon.sandbox import ToolDispatchMiddleware


# ---------- constants ----------


# Maximum number of LLM turns (tool_use OR end_turn) per single
# run_agent_step invocation. Acts as the recursion guardrail (D-052)
# at the Python level; LangGraph's own ``recursion_limit`` is set on
# the StateGraph (feat-022) and surfaces separately.
DEFAULT_MAX_STEPS: int = 50


SYSTEM_PROMPT: str = (
    "You are heddle, a coding agent working on the user's project. "
    "Use the provided tools to read, edit, and run code. Think step "
    "by step. When you're done with the user's request, write a "
    "concise final message that summarizes what you did. Never claim "
    "a file was written or a command was run unless the corresponding "
    "tool result confirms it."
)


# ---------- types ----------


@dataclass(frozen=True)
class ToolCall:
    """A single tool call request from the LLM.

    Mirrors the LangChain tool_call dict shape so the same dict can
    be passed to ``FakeLLM`` (heddle_common.fake_llm) and to a real
    LangChain chat model without translation.
    """

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    """One LLM turn's reply, in a provider-neutral shape."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"  # "end_turn" | "tool_use" | "max_tokens"

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


LLMCallable = Callable[[Sequence[dict[str, Any]]], Union[LLMResponse, Awaitable[LLMResponse]]]


# ---------- errors ----------


class AgentRuntimeError(Exception):
    """Base class for runtime errors."""


class RecursionLimitError(AgentRuntimeError):
    """Raised when ``max_steps`` is reached without an end_turn.

    D-052 mandates a structured ``cause="recursion_limit"`` so the
    daemon can surface the failure with a stable classification that
    the UI / WS layer can render without parsing prose.
    """

    def __init__(self, steps_taken: int, max_steps: int) -> None:
        super().__init__(
            f"agent exceeded max_steps={max_steps} after {steps_taken} LLM turns"
        )
        self.steps_taken = steps_taken
        self.max_steps = max_steps
        self.cause = "recursion_limit"


# ---------- the runtime ----------


@dataclass
class AgentRuntime:
    """The single-source-of-truth heddle agent loop.

    Construct with a checkpointer (from feat-018) and a sandbox
    middleware (from feat-021); pass an LLM callable at run time so
    the same instance can be used for many threads + many models.

    Example wiring (post-feat-023):

        runtime = AgentRuntime(
            checkpoint_store=daemon.checkpoint_store,
            sandbox=sandbox_middleware,
        )
        result = await runtime.run_agent_step(
            thread_id="feat-019",
            user_message="Add a README.",
            llm=build_chat_model(config),
            max_steps=50,
        )

    For tests:

        from heddle_common.fake_llm import FakeLLM
        result = await runtime.run_agent_step(
            thread_id="feat-019",
            user_message="Read README.md",
            llm=FakeLLM(load_fixture("read_then_done.json")),
        )
    """

    checkpoint_store: ProjectCheckpointStore
    sandbox: ToolDispatchMiddleware
    system_prompt: str = SYSTEM_PROMPT

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoint_store, ProjectCheckpointStore):
            raise TypeError(
                "checkpoint_store must be a ProjectCheckpointStore; "
                f"got {type(self.checkpoint_store).__name__}"
            )
        if not isinstance(self.sandbox, ToolDispatchMiddleware):
            raise TypeError(
                "sandbox must be a ToolDispatchMiddleware; "
                f"got {type(self.sandbox).__name__}"
            )

    # ---- public API ----

    async def run_agent_step(
        self,
        thread_id: str,
        user_message: str,
        llm: LLMCallable,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> LLMResponse:
        """Run one user-message through the LLM ↔ tools loop.

        Loads thread state from the checkpointer, appends the user
        message, calls the LLM, dispatches any tool calls through the
        sandbox, and persists the updated messages back. Returns the
        final assistant message (LLMResponse with ``stop_reason ==
        "end_turn"``) once the LLM stops requesting tools.

        Raises:
            RecursionLimitError: ``max_steps`` LLM turns reached
                without an end_turn. ``.cause == "recursion_limit"``
                per D-052.
            ValueError: thread_id malformed, user_message empty.
        """
        if not isinstance(user_message, str) or not user_message:
            raise ValueError(f"user_message must be a non-empty string; got {user_message!r}")
        if max_steps <= 0:
            raise ValueError(f"max_steps must be > 0; got {max_steps}")

        # ``thread_id_for_feature`` validates the id format (rejects
        # path-traversal-style inputs). Centralized chokepoint.
        canonical_thread = ProjectCheckpointStore.thread_id_for_feature(thread_id)

        # 1. Load previous state.
        state = await self._load_state(canonical_thread)
        messages: list[dict[str, Any]] = list(state.get("messages", []))

        # First turn of this thread: inject the system prompt.
        if not messages:
            messages.append({"role": "system", "content": self.system_prompt})

        messages.append({"role": "user", "content": user_message})

        # 2. Loop: call LLM, dispatch tool calls, repeat.
        steps_taken = 0
        final: LLMResponse | None = None
        while steps_taken < max_steps:
            steps_taken += 1
            response = await self._call_llm(llm, messages)
            messages.append(self._response_to_message(response))

            if not response.has_tool_calls:
                # LLM is done. Persist and return.
                await self._save_state(canonical_thread, messages)
                _logging.info(
                    component="agent_runtime",
                    event="agent_step_completed",
                    msg=f"agent step finished after {steps_taken} turn(s)",
                    thread_id=canonical_thread,
                    steps_taken=steps_taken,
                )
                return response

            # Dispatch every tool call sequentially. (Parallel is
            # possible later if we need throughput, but a single
            # ``write``+``edit`` race would corrupt state — keep it
            # simple for v0.1.)
            for tc in response.tool_calls:
                # Persist BEFORE dispatch so a crash mid-dispatch
                # leaves the assistant message + tool-call entry on
                # record; a future resume can re-dispatch the tool
                # call instead of losing the step entirely.
                await self._save_state(canonical_thread, messages)
                tool_result_text = await self.sandbox.dispatch_async(
                    tc.name, tc.args
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result_text,
                })

        # Loop exhausted without end_turn. Persist so the user can
        # inspect the partial conversation, then raise.
        await self._save_state(canonical_thread, messages)
        _logging.warn(
            component="agent_runtime",
            event="agent_recursion_limit",
            msg=(
                f"agent exceeded max_steps={max_steps} after "
                f"{steps_taken} LLM turn(s); raising RecursionLimitError"
            ),
            thread_id=canonical_thread,
            max_steps=max_steps,
            steps_taken=steps_taken,
        )
        raise RecursionLimitError(steps_taken=steps_taken, max_steps=max_steps)

    async def resume(self, thread_id: str, llm: LLMCallable) -> LLMResponse:
        """Resume a thread from its last persisted state with no new message.

        Used by the daemon restart / supervisor after a crash: the
        previous step's tool result (or partial assistant message) is
        already persisted; ``resume`` re-enters the loop and continues
        from where it stopped. Raises ``RecursionLimitError`` if the
        thread is already at max_steps of tool calls without an
        end_turn (D-051, D-052 budget exhaustion).
        """
        canonical_thread = ProjectCheckpointStore.thread_id_for_feature(thread_id)
        state = await self._load_state(canonical_thread)
        messages: list[dict[str, Any]] = list(state.get("messages", []))
        if not messages:
            raise ValueError(
                f"cannot resume thread {thread_id!r}: no persisted state"
            )
        # Find the last message. If it was an assistant tool_use, the
        # next iteration dispatches those tool calls. If it was a tool
        # result, the next iteration calls the LLM again to decide
        # what to do with the result. Either way, jump back to the
        # top of the loop.
        steps_taken = 0
        while steps_taken < DEFAULT_MAX_STEPS:
            steps_taken += 1
            response = await self._call_llm(llm, messages)
            messages.append(self._response_to_message(response))
            if not response.has_tool_calls:
                await self._save_state(canonical_thread, messages)
                return response
            for tc in response.tool_calls:
                await self._save_state(canonical_thread, messages)
                tool_result_text = await self.sandbox.dispatch_async(tc.name, tc.args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result_text,
                })
        await self._save_state(canonical_thread, messages)
        raise RecursionLimitError(steps_taken=steps_taken, max_steps=DEFAULT_MAX_STEPS)

    # ---- checkpoint I/O ----

    async def _load_state(self, thread_id: str) -> dict[str, Any]:
        """Read ``{"messages": [...]}`` from the checkpointer.

        Returns ``{"messages": []}`` for a never-seen thread (which
        ``run_agent_step`` will then seed with the system prompt).
        """
        saver = self.checkpoint_store.get_checkpointer()
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }
        try:
            tup = await saver.aget_tuple(config)
        except Exception as exc:
            _logging.warn(
                component="agent_runtime",
                event="checkpoint_load_failed",
                msg=f"failed to load state for thread {thread_id!r}: {exc}",
                thread_id=thread_id,
            )
            return {"messages": []}
        if tup is None:
            return {"messages": []}
        # LangGraph checkpoints store the channel values inside
        # ``tup.checkpoint["channel_values"]``. We only ever persist
        # a single channel called "messages".
        channel_values = getattr(tup.checkpoint, "channel_values", None)
        if channel_values is None:
            # Older LangGraph versions use dict access.
            try:
                channel_values = tup.checkpoint["channel_values"]
            except (KeyError, TypeError):
                channel_values = {}
        messages = channel_values.get("messages", []) if isinstance(channel_values, dict) else []
        return {"messages": list(messages)}

    async def _save_state(self, thread_id: str, messages: list[dict[str, Any]]) -> None:
        """Persist ``{"messages": [...]}`` to the checkpointer.

        Uses LangGraph's low-level ``aput`` interface because we're
        driving the loop by hand (not via a compiled graph). The
        ``checkpoint_ns=""`` and ``checkpoint_id`` keys match what
        LangGraph's StateGraph would produce for a single-channel
        state, so a future feat that DOES compile a graph against
        the same checkpointer can resume the thread without
        translation.
        """
        saver = self.checkpoint_store.get_checkpointer()
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }
        checkpoint = {
            "v": 1,
            "id": f"{thread_id}-{id(messages)}",  # monotonic per save
            "ts": _now_iso(),
            "channel_values": {"messages": list(messages)},
            "channel_versions": {"messages": len(messages)},
            "versions_seen": {},
            "pending_sends": [],
        }
        metadata: dict[str, Any] = {"source": "agent_runtime", "step": len(messages)}
        try:
            await saver.aput(config, checkpoint, metadata, list(messages))
        except Exception as exc:
            _logging.error(
                component="agent_runtime",
                event="checkpoint_save_failed",
                msg=f"failed to save state for thread {thread_id!r}: {exc}",
                thread_id=thread_id,
            )
            raise

    # ---- helpers ----

    @staticmethod
    async def _call_llm(
        llm: LLMCallable,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        """Call the LLM, normalising sync + async callables.

        Three callable shapes are supported:
          * async function returning LLMResponse
          * sync function returning LLMResponse
          * LangChain BaseChatModel (or anything with .ainvoke)
        """
        # Detect LangChain-style "ainvoke" first because that's the
        # production case.
        ainvoke = getattr(llm, "ainvoke", None)
        if callable(ainvoke):
            result = await ainvoke(messages)
            return _coerce_llm_response(result)
        # FakeLLM exposes a sync .invoke().
        invoke = getattr(llm, "invoke", None)
        if callable(invoke):
            result = invoke(messages)
            if asyncio.iscoroutine(result):
                result = await result
            return _coerce_llm_response(result)
        # Plain callable.
        result = llm(messages)
        if asyncio.iscoroutine(result):
            result = await result
        return _coerce_llm_response(result)

    @staticmethod
    def _response_to_message(response: LLMResponse) -> dict[str, Any]:
        """Convert an LLMResponse into a message dict for the history.

        Tool calls become the ``tool_calls`` key (LangChain /
        OpenAI format); the assistant content is preserved as
        ``content``. Messages without tool calls store just
        ``content``.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": response.content}
        if response.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "args": tc.args,
                }
                for tc in response.tool_calls
            ]
        return msg


# ---------- helpers (module-level so they can be tested in isolation) ----------


def _coerce_llm_response(result: Any) -> LLMResponse:
    """Coerce whatever the LLM callable returned into an LLMResponse.

    Accepts:
      * LLMResponse (no-op)
      * LangChain AIMessage (or anything with .content / .tool_calls / .response_metadata)
      * FakeLLM's _FakeAIMessage (same shape as AIMessage)
      * dict (already in the OpenAI message format)
    """
    if isinstance(result, LLMResponse):
        return result
    if hasattr(result, "content") and hasattr(result, "tool_calls"):
        # LangChain / FakeLLM AI message shape.
        tool_calls: list[ToolCall] = []
        for tc in result.tool_calls or []:
            if isinstance(tc, dict):
                tool_calls.append(
                    ToolCall(
                        id=str(tc.get("id", f"call_{len(tool_calls)}")),
                        name=str(tc.get("name", "")),
                        args=dict(tc.get("args", {})),
                    )
                )
            else:
                # LangChain ToolCall objects have .id / .name / .args
                tool_calls.append(
                    ToolCall(
                        id=str(getattr(tc, "id", f"call_{len(tool_calls)}")),
                        name=str(getattr(tc, "name", "")),
                        args=dict(getattr(tc, "args", {}) or {}),
                    )
                )
        stop_reason = "end_turn"
        metadata = getattr(result, "response_metadata", None) or {}
        if isinstance(metadata, dict):
            stop_reason = metadata.get("stop_reason", stop_reason)
        return LLMResponse(
            content=str(getattr(result, "content", "") or ""),
            tool_calls=tuple(tool_calls),
            stop_reason=stop_reason,
        )
    if isinstance(result, dict):
        # OpenAI / Anthropic message format.
        tool_calls = []
        for tc in result.get("tool_calls", []) or []:
            tool_calls.append(
                ToolCall(
                    id=str(tc.get("id", f"call_{len(tool_calls)}")),
                    name=str(tc.get("name", "")),
                    args=dict(tc.get("args", {})),
                )
            )
        return LLMResponse(
            content=str(result.get("content", "") or ""),
            tool_calls=tuple(tool_calls),
            stop_reason=result.get("stop_reason", "end_turn"),
        )
    raise TypeError(
        f"LLM callable returned an unsupported type: {type(result).__name__}; "
        f"expected LLMResponse, LangChain AIMessage, or dict"
    )


def _now_iso() -> str:
    """ISO 8601 UTC timestamp for the checkpoint ``ts`` field."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "DEFAULT_MAX_STEPS",
    "SYSTEM_PROMPT",
    "AgentRuntime",
    "AgentRuntimeError",
    "LLMCallable",
    "LLMResponse",
    "RecursionLimitError",
    "SYSTEM_PROMPT",
    "ToolCall",
]
