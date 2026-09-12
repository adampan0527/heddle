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
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence, Union

from langgraph.checkpoint.base import BaseCheckpointSaver

from heddle_common import logging as _logging
from heddle_daemon.checkpointing import ProjectCheckpointStore
from heddle_daemon.sandbox import ToolDispatchMiddleware


# ---------- constants ----------


# Default upper bound on LLM turns (tool_use OR end_turn) per single
# run_agent_step invocation. Acts as the recursion guardrail (D-052)
# at the Python level. Override via the HEDDLE_RECURSION_LIMIT env
# var (per feat-022). The default of 200 matches the LangGraph
# ``recursion_limit`` the spec calls for, so an agent moved between
# the hand-rolled runtime and a compiled StateGraph hits the same
# boundary.
DEFAULT_MAX_STEPS: int = 200

# Env-var name for the runtime's max_steps ceiling (D-052 / feat-022).
# Single source of truth: the daemon reads this when constructing the
# runtime, and tests can monkeypatch ``os.environ`` to assert the
# value flow end-to-end.
RECURSION_LIMIT_ENV_VAR: str = "HEDDLE_RECURSION_LIMIT"

# Default LLM-call retry budget and backoff (D-041 / feat-022). Three
# attempts total; backoff is exponential 1s -> 2s -> 4s (the third
# attempt fires immediately after the second 2s sleep fails). Tests
# can shrink these via the ``max_retries`` / ``backoff_base_seconds``
# fields on AgentRuntime.
DEFAULT_MAX_LLM_RETRIES: int = 3
DEFAULT_LLM_BACKOFF_BASE_SECONDS: float = 1.0


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


class LLMRetryExhaustedError(AgentRuntimeError):
    """Raised when the LLM retry budget (D-041 / feat-022) is exhausted.

    The daemon surfaces this with ``cause="llm_retry_exhausted"`` so
    the WS layer / UI can render a structured failure ("the LLM
    service returned errors 3 times in a row") rather than parsing
    the underlying exception's message. The original last exception
    is preserved as ``__cause__`` for debugging.
    """

    def __init__(self, attempts: int, last_exception: BaseException) -> None:
        super().__init__(
            f"LLM call failed after {attempts} attempts; "
            f"last error: {type(last_exception).__name__}: {last_exception}"
        )
        self.attempts = attempts
        self.cause = "llm_retry_exhausted"
        self.last_exception = last_exception


# Exceptions we deliberately do NOT retry. (KeyboardInterrupt,
# SystemExit, asyncio.CancelledError, plus our own runtime errors
# — a RecursionLimitError on the LLM call would loop the retry
# forever.) Everything else (``Exception``) is treated as transient
# and retried per D-041. Defined AFTER the error classes so the
# forward references resolve.
_NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    SystemExit,
    asyncio.CancelledError,
    RecursionLimitError,
    LLMRetryExhaustedError,
)


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
            max_steps=get_max_steps_from_env(),
        )
        result = await runtime.run_agent_step(
            thread_id="feat-019",
            user_message="Add a README.",
            llm=build_chat_model(config),
        )

    For tests:

        from heddle_common.fake_llm import FakeLLM
        result = await runtime.run_agent_step(
            thread_id="feat-019",
            user_message="Read README.md",
            llm=FakeLLM(load_fixture("read_then_done.json")),
            max_steps=50,
        )
    """

    checkpoint_store: ProjectCheckpointStore
    sandbox: ToolDispatchMiddleware
    system_prompt: str = SYSTEM_PROMPT
    # Per-invocation ceiling on LLM turns (D-052). Set at construction
    # so the daemon reads ``HEDDLE_RECURSION_LIMIT`` once at startup
    # rather than per call. Per-call ``max_steps`` overrides still
    # work for tests that want a smaller budget.
    max_steps: int = DEFAULT_MAX_STEPS
    # Per-LLM-call retry budget (D-041). 3 attempts with exponential
    # backoff (1s -> 2s -> 4s by default). Set to 1 to disable
    # retries entirely (e.g. in latency-sensitive tests).
    max_retries: int = DEFAULT_MAX_LLM_RETRIES
    backoff_base_seconds: float = DEFAULT_LLM_BACKOFF_BASE_SECONDS

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
        if self.max_steps <= 0:
            raise ValueError(f"max_steps must be > 0; got {self.max_steps}")
        if self.max_retries < 1:
            raise ValueError(f"max_retries must be >= 1; got {self.max_retries}")
        if self.backoff_base_seconds < 0:
            raise ValueError(
                f"backoff_base_seconds must be >= 0; got {self.backoff_base_seconds}"
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
        while steps_taken < self.max_steps:
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
        raise RecursionLimitError(steps_taken=steps_taken, max_steps=self.max_steps)

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
        # Monotonic checkpoint id per thread. The id is
        # ``f"{thread_id}-{len(messages):020d}"`` so:
        #   * lexicographic sort matches numeric sort (20-digit
        #     zero-pad, well past any realistic message count);
        #   * the "latest" lookup (LangGraph's aget_tuple picks
        #     max-by-id) returns the save with the most messages,
        #     which is the most recent because we always save AFTER
        #     appending;
        #   * the id is stable across processes / runtime instances:
        #     a resumed daemon hits the same id sequence as the
        #     daemon that wrote the thread.
        # An earlier scheme used ``id(messages)`` (a random memory
        # address), which made ids non-monotonic across reallocated
        # lists and produced intermittent "stale save" reads. Don't
        # regress to that.
        checkpoint_id = _format_checkpoint_id(thread_id, len(messages))
        checkpoint = {
            "v": 1,
            "id": checkpoint_id,
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

    async def _call_llm(
        self,
        llm: LLMCallable,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        """Call the LLM with retry + exponential backoff (D-041 / feat-022).

        Wraps :meth:`_call_llm_once` (the raw single-attempt call) in
        a retry loop. On any exception (network error, transient
        LLM provider failure, etc.), the call is retried after a
        sleep of ``backoff_base_seconds * 2**(attempt-1)`` — so the
        default settings produce 1s, 2s, 4s gaps. After
        ``max_retries`` failed attempts, the original exception is
        wrapped in :class:`LLMRetryExhaustedError` with
        ``cause="llm_retry_exhausted"`` and raised.

        The retry budget is per-LLM-call, NOT per-loop-iteration: a
        successful LLM call followed by a tool dispatch and another
        LLM call starts a fresh retry budget. This matches D-041
        ("daemon-internal LLM/tool retries").
        """
        last_exc: BaseException | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._call_llm_once(llm, messages)
                if attempt > 1:
                    _logging.info(
                        component="agent_runtime",
                        event="llm_retry_succeeded",
                        msg=(
                            f"LLM call succeeded on attempt {attempt} "
                            f"after {attempt - 1} prior failure(s)"
                        ),
                        attempt=attempt,
                    )
                return response
            except _NON_RETRYABLE_EXCEPTIONS:
                # Programming errors should not retry; let them
                # propagate to the caller / supervisor so the bug
                # surfaces immediately rather than masking itself as
                # a transient LLM failure.
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = self.backoff_base_seconds * (2 ** (attempt - 1))
                    _logging.warn(
                        component="agent_runtime",
                        event="llm_retry",
                        msg=(
                            f"LLM call attempt {attempt}/{self.max_retries} "
                            f"failed: {type(exc).__name__}: {exc}; "
                            f"sleeping {delay:.3f}s before retry"
                        ),
                        attempt=attempt,
                        max_retries=self.max_retries,
                        delay_seconds=delay,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                # attempt == max_retries: fall through to exhaustion.
                break
        # All attempts failed.
        assert last_exc is not None  # loop body always sets it on failure
        _logging.error(
            component="agent_runtime",
            event="llm_retry_exhausted",
            msg=(
                f"LLM call failed after {self.max_retries} attempts; "
                f"raising LLMRetryExhaustedError"
            ),
            max_retries=self.max_retries,
            error_type=type(last_exc).__name__,
            error_message=str(last_exc),
        )
        raise LLMRetryExhaustedError(self.max_retries, last_exc) from last_exc

    @staticmethod
    async def _call_llm_once(
        llm: LLMCallable,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        """Call the LLM once, normalising sync + async callables.

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


def _format_checkpoint_id(thread_id: str, message_count: int) -> str:
    """Format a monotonic checkpoint id for a thread + message count.

    Format: ``f"{thread_id}-{message_count:020d}"``. The 20-digit
    zero-pad means lexicographic comparison matches numeric
    comparison up to 10**20 saves per thread — well past any
    realistic message count.

    Why message count and not a per-instance counter? Because
    LangGraph's table key is ``(thread_id, checkpoint_ns,
    checkpoint_id)`` and ``aget_tuple`` returns the
    lexicographically-largest id for the thread. Two separate
    runtime instances (the one that wrote the thread + the one
    that resumed it) MUST produce a non-colliding, monotonic id
    sequence. Using ``len(messages)`` at save time satisfies both:
    every save appends at least one message, so within one thread
    the message count is strictly increasing across all saves by
    any process.
    """
    return f"{thread_id}-{message_count:020d}"


def get_max_steps_from_env(env: Mapping[str, str] | None = None) -> int:
    """Read the recursion-limit ceiling from the environment.

    Reads ``HEDDLE_RECURSION_LIMIT``; on missing / non-integer /
    non-positive value, falls back to :data:`DEFAULT_MAX_STEPS` and
    logs a warning. The fallback is intentionally lenient (we never
    crash the daemon over a typo in an env var) but loud (the warning
    appears in every structured-log stream).

    feat-022 single-source-of-truth helper: the daemon, the CLI, and
    tests all read through this function rather than parsing the env
    var inline.
    """
    src = os.environ if env is None else env
    raw = src.get(RECURSION_LIMIT_ENV_VAR)
    if raw is None:
        return DEFAULT_MAX_STEPS
    try:
        value = int(raw)
    except ValueError:
        _logging.warn(
            component="agent_runtime",
            event="recursion_limit_env_invalid",
            msg=(
                f"{RECURSION_LIMIT_ENV_VAR}={raw!r} is not an integer; "
                f"using DEFAULT_MAX_STEPS={DEFAULT_MAX_STEPS}"
            ),
            env_var=RECURSION_LIMIT_ENV_VAR,
            raw_value=raw,
        )
        return DEFAULT_MAX_STEPS
    if value <= 0:
        _logging.warn(
            component="agent_runtime",
            event="recursion_limit_env_nonpositive",
            msg=(
                f"{RECURSION_LIMIT_ENV_VAR}={value} must be > 0; "
                f"using DEFAULT_MAX_STEPS={DEFAULT_MAX_STEPS}"
            ),
            env_var=RECURSION_LIMIT_ENV_VAR,
            raw_value=value,
        )
        return DEFAULT_MAX_STEPS
    return value


__all__ = [
    "DEFAULT_MAX_LLM_RETRIES",
    "DEFAULT_LLM_BACKOFF_BASE_SECONDS",
    "DEFAULT_MAX_STEPS",
    "RECURSION_LIMIT_ENV_VAR",
    "SYSTEM_PROMPT",
    "AgentRuntime",
    "AgentRuntimeError",
    "LLMCallable",
    "LLMRetryExhaustedError",
    "LLMResponse",
    "RecursionLimitError",
    "SYSTEM_PROMPT",
    "ToolCall",
    "get_max_steps_from_env",
    "_format_checkpoint_id",
]
