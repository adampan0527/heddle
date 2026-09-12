# SPDX-License-Identifier: Apache-2.0
"""Tests for ``AgentRuntime``'s external-abort signal (feat-014).

The runtime accepts an optional ``stop_event: asyncio.Event`` argument.
When the event is set during a run, the runtime must raise
``FeatureAbortedError`` (cause="feature_aborted") promptly, without
calling the LLM again and without retrying. The previous-invocation
behavior (no stop_event) must be byte-identical — the optional kwarg
defaults to ``None`` and the existing feat-019 / feat-022 tests cover
that path.

These tests are deliberately scoped to the abort branch; the
non-abort happy path lives in test_agent_runtime.py.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from heddle_daemon.agent_runtime import (
    AgentRuntime,
    FeatureAbortedError,
    LLMResponse,
    ToolCall,
)
from heddle_daemon.checkpointing import ProjectCheckpointStore
from heddle_daemon.sandbox import SandboxConfig, ToolDispatchMiddleware


# ---------- helpers ----------


class _TempProject:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="heddle_abort_test_"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


async def _setup(proj: Path) -> tuple[AgentRuntime, ProjectCheckpointStore]:
    store = ProjectCheckpointStore(project_path=proj)
    await store.setup()
    sandbox = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
    sandbox.setup()
    return AgentRuntime(checkpoint_store=store, sandbox=sandbox), store


class _CountingLLM:
    """Bare-bones LLM that records invocations and returns scripted turns.

    Distinct from FakeLLM in that it exposes ``call_count`` directly
    and returns ``LLMResponse`` instances (no LangChain / OpenAI
    message coercion) so we can pin down the exact turn at which the
    abort fires.
    """

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def __call__(self, messages: Sequence[dict]) -> LLMResponse:
        idx = self.call_count
        self.call_count += 1
        if idx >= len(self._responses):
            return LLMResponse(content="exhausted", stop_reason="end_turn")
        return self._responses[idx](messages)


def _tool_use(name: str, args: dict | None = None) -> LLMResponse:
    """Build a tool-use LLMResponse."""
    return LLMResponse(
        content="",
        tool_calls=(ToolCall(id=f"call_{name}", name=name, args=args or {}),),
        stop_reason="tool_use",
    )


def _end_turn(content: str = "done") -> LLMResponse:
    return LLMResponse(content=content, stop_reason="end_turn")


# ---------- tests ----------


class TestAbortBeforeFirstLLMCall(unittest.IsolatedAsyncioTestCase):
    """A pre-set stop_event must abort the runtime at the first check point.

    The runtime's stop_event check fires AFTER ``_call_llm`` returns
    (so a half-finished LLM call is never wasted) but BEFORE state is
    mutated. A pre-set event therefore triggers after the first LLM
    call and aborts — the LLM is invoked exactly once, the loop
    exits with ``FeatureAbortedError``.
    """

    async def test_pre_set_stop_event_aborts_after_first_llm_call(self):
        with _TempProject() as proj:
            runtime, store = await _setup(proj)
            try:
                (proj / "README.md").write_text("# hi\n")
                llm = _CountingLLM([
                    lambda _msgs: _tool_use("read", {"path": "README.md"}),
                ])
                stop = asyncio.Event()
                stop.set()  # set BEFORE the call

                with self.assertRaises(FeatureAbortedError) as ctx:
                    await runtime.run_agent_step(
                        thread_id="feat-014",
                        user_message="please read",
                        llm=llm,
                        stop_event=stop,
                    )
                self.assertEqual(ctx.exception.thread_id, "feat-014")
                self.assertEqual(ctx.exception.cause, "feature_aborted")
                # The LLM is invoked exactly once (then abort fires at
                # the post-LLM check). The runtime never reaches the
                # tool-dispatch path.
                self.assertEqual(llm.call_count, 1)
            finally:
                await store.close()


class TestAbortAfterToolDispatch(unittest.IsolatedAsyncioTestCase):
    """A stop_event set during the run must abort at the next check point."""

    async def test_stop_event_set_after_tool_dispatch_aborts_with_cause(self):
        with _TempProject() as proj:
            runtime, store = await _setup(proj)
            try:
                (proj / "a.txt").write_text("a")
                stop = asyncio.Event()

                turn_0 = _tool_use("read", {"path": "a.txt"})

                def turn_1(_msgs) -> LLMResponse:
                    return _tool_use("read", {"path": "a.txt"})

                def turn_2(_msgs) -> LLMResponse:
                    # Should never be reached.
                    return _end_turn("should not happen")

                llm = _CountingLLM([lambda _m: turn_0, turn_1, turn_2])

                # Schedule the abort to fire after the first LLM turn
                # completes and the runtime has reached its post-LLM
                # stop_event check. Two yields is enough for a fresh
                # event loop to enter the agent loop, call the LLM,
                # and reach the check.
                async def trigger_abort() -> None:
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    stop.set()

                with self.assertRaises(FeatureAbortedError) as ctx:
                    await asyncio.gather(
                        runtime.run_agent_step(
                            thread_id="feat-014",
                            user_message="read",
                            llm=llm,
                            stop_event=stop,
                        ),
                        trigger_abort(),
                    )
                self.assertEqual(ctx.exception.cause, "feature_aborted")
                # The LLM was called once (turn 0) before the abort.
                self.assertEqual(llm.call_count, 1)
            finally:
                await store.close()

    async def test_no_stop_event_behaves_as_before(self):
        """Backstop: omitting stop_event preserves feat-019 / feat-022 behavior."""
        with _TempProject() as proj:
            runtime, store = await _setup(proj)
            try:
                (proj / "a.txt").write_text("a")
                # Builder functions returning tool-use / end-turn in order.
                responses = [
                    lambda _m: _tool_use("read", {"path": "a.txt"}),
                    lambda _m: _tool_use("read", {"path": "a.txt"}),
                    lambda _m: _end_turn("done"),
                ]
                llm = _CountingLLM(responses)
                result = await runtime.run_agent_step(
                    thread_id="feat-014",
                    user_message="read",
                    llm=llm,
                    # No stop_event kwarg → default None.
                )
                self.assertEqual(result.stop_reason, "end_turn")
                self.assertEqual(result.content, "done")
                self.assertEqual(llm.call_count, 3)
            finally:
                await store.close()


class TestAbortCauseClassification(unittest.TestCase):
    """The cause classification distinguishes abort from other runtime errors."""

    def test_feature_aborted_error_has_distinct_cause(self):
        e = FeatureAbortedError("feat-014")
        self.assertEqual(e.cause, "feature_aborted")
        self.assertEqual(e.thread_id, "feat-014")
        self.assertIn("feat-014", str(e))


if __name__ == "__main__":
    unittest.main()