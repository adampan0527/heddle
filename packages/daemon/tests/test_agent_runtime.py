# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_daemon.agent_runtime — feat-019 (self-written LLM loop)."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from heddle_common.fake_llm import (
    FAKE_LLM_ENV_VAR,
    FakeLLM,
    ScriptedResponse,
    ScriptedToolCall,
    _FakeAIMessage,
    load_fixture,
)

from heddle_daemon.agent_runtime import (
    DEFAULT_MAX_LLM_RETRIES,
    DEFAULT_MAX_STEPS,
    RECURSION_LIMIT_ENV_VAR,
    SYSTEM_PROMPT,
    AgentRuntime,
    AgentRuntimeError,
    LLMRetryExhaustedError,
    LLMResponse,
    RecursionLimitError,
    ToolCall,
    _coerce_llm_response,
    _format_checkpoint_id,
    get_max_steps_from_env,
)
from heddle_daemon.checkpointing import ProjectCheckpointStore
from heddle_daemon.sandbox import SandboxConfig, ToolDispatchMiddleware


# ---------- helpers ----------


class _TempProject:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="heddle_runtime_test_"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _make_fixture_file(proj: Path, responses: list[ScriptedResponse]) -> Path:
    """Write a fake-llm fixture JSON under proj/tests/fixtures and return it."""
    import json
    fixtures = proj / "tests" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    path = fixtures / "scenario.json"
    payload = {
        "schema_version": 1,
        "responses": [
            {
                "content": r.content,
                "tool_calls": [
                    {"name": tc.name, "args": tc.args} for tc in r.tool_calls
                ],
                "stop_reason": r.stop_reason,
            }
            for r in responses
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _jsonify(d: dict) -> str:
    import json
    return json.dumps(d, ensure_ascii=False)


async def _setup_runtime(proj: Path) -> tuple[AgentRuntime, ProjectCheckpointStore, ToolDispatchMiddleware]:
    """Spin up a checkpoint store + sandbox + runtime, all wired together.

    The caller is responsible for closing the store; the runtime and
    sandbox don't own the connection.
    """
    store = ProjectCheckpointStore(project_path=proj)
    await store.setup()
    sandbox = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
    sandbox.setup()
    runtime = AgentRuntime(checkpoint_store=store, sandbox=sandbox)
    return runtime, store, sandbox


# ---------- construction ----------


class TestRuntimeConstruction(unittest.TestCase):
    def test_requires_checkpoint_store(self):
        with _TempProject() as proj:
            sandbox = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
            sandbox.setup()
            with self.assertRaises(TypeError):
                AgentRuntime(checkpoint_store="not a store", sandbox=sandbox)  # type: ignore[arg-type]

    def test_requires_sandbox(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            with self.assertRaises(TypeError):
                AgentRuntime(checkpoint_store=store, sandbox="not a sandbox")  # type: ignore[arg-type]


# ---------- the spec'd happy path (step 5) ----------


class TestSpecHappyPath(unittest.IsolatedAsyncioTestCase):
    """feat-019 step 5: with a fixture that has 2 tool calls then done,
    the runtime completes after exactly 3 LLM invocations."""

    async def test_two_tool_calls_then_done_completes_in_three_llm_turns(self):
        with _TempProject() as proj:
            # Seed the project with files the LLM will discover.
            (proj / "README.md").write_text("# My Project\n", encoding="utf-8")
            (proj / "a.txt").write_text("x", encoding="utf-8")
            (proj / "b.txt").write_text("y", encoding="utf-8")

            runtime, store, sandbox = await _setup_runtime(proj)
            try:
                llm = FakeLLM([
                    ScriptedResponse(
                        content="",
                        tool_calls=(ScriptedToolCall(name="read", args={"path": "README.md"}),),
                        stop_reason="tool_use",
                    ),
                    ScriptedResponse(
                        content="",
                        tool_calls=(ScriptedToolCall(name="glob", args={"pattern": "*.txt"}),),
                        stop_reason="tool_use",
                    ),
                    ScriptedResponse(
                        content="Done — I read the README and listed the txt files.",
                        stop_reason="end_turn",
                    ),
                ])

                result = await runtime.run_agent_step(
                    thread_id="feat-019",
                    user_message="Read README and list txt files",
                    llm=llm,
                )

                self.assertEqual(result.stop_reason, "end_turn")
                self.assertIn("Done", result.content)
                self.assertFalse(result.has_tool_calls)
                # The LLM was invoked exactly 3 times.
                self.assertEqual(llm.remaining, 0)
                # The sandbox allowed exactly 2 tool calls (the read + glob).
                self.assertEqual(sandbox.allowed_count, 2)
            finally:
                await store.close()

    async def test_messages_persisted_with_full_history(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("x", encoding="utf-8")
            runtime, store, _ = await _setup_runtime(proj)
            try:
                llm = FakeLLM([
                    ScriptedResponse(
                        tool_calls=(ScriptedToolCall(name="read", args={"path": "f.txt"}),),
                        stop_reason="tool_use",
                    ),
                    ScriptedResponse(content="Done.", stop_reason="end_turn"),
                ])
                await runtime.run_agent_step("feat-019", "Read f.txt", llm)

                # Load state and inspect.
                config = {
                    "configurable": {"thread_id": "feat-019", "checkpoint_ns": ""}
                }
                tup = await store.get_checkpointer().aget_tuple(config)
                messages = tup.checkpoint["channel_values"]["messages"]
                # system + user + assistant(tool_call) + tool + assistant(end)
                self.assertEqual(len(messages), 5)
                self.assertEqual(messages[0]["role"], "system")
                self.assertEqual(messages[1]["role"], "user")
                self.assertEqual(messages[2]["role"], "assistant")
                self.assertEqual(messages[3]["role"], "tool")
                self.assertEqual(messages[4]["role"], "assistant")
                self.assertEqual(messages[1]["content"], "Read f.txt")
                self.assertEqual(messages[3]["content"], "x")
                self.assertEqual(messages[4]["content"], "Done.")
            finally:
                await store.close()


# ---------- RecursionLimitError (D-052) ----------


class TestRecursionLimit(unittest.IsolatedAsyncioTestCase):
    async def test_recursion_limit_raises_with_structured_cause(self):
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                # LLM always wants another tool call.
                llm = FakeLLM(
                    [
                        ScriptedResponse(
                            tool_calls=(ScriptedToolCall(name="read", args={"path": "f.txt"}),),
                            stop_reason="tool_use",
                        )
                    ] * 100,
                    loop=True,
                )

                with self.assertRaises(RecursionLimitError) as ctx:
                    await runtime.run_agent_step(
                        "feat-019", "loop forever", llm, max_steps=5
                    )
                err = ctx.exception
                self.assertEqual(err.cause, "recursion_limit")
                self.assertEqual(err.max_steps, 5)
                self.assertGreaterEqual(err.steps_taken, 5)
                self.assertLessEqual(err.steps_taken, 5)
                # Partial conversation must be persisted even when the
                # loop aborts so the user can inspect what happened.
                config = {
                    "configurable": {"thread_id": "feat-019", "checkpoint_ns": ""}
                }
                tup = await store.get_checkpointer().aget_tuple(config)
                self.assertIsNotNone(tup)
            finally:
                await store.close()

    def test_recursion_limit_error_is_agent_runtime_error(self):
        err = RecursionLimitError(steps_taken=3, max_steps=3)
        self.assertIsInstance(err, AgentRuntimeError)
        self.assertEqual(err.cause, "recursion_limit")
        self.assertIn("max_steps=3", str(err))
        self.assertIn("3 LLM turn", str(err))


# ---------- input validation ----------


class TestInputValidation(unittest.IsolatedAsyncioTestCase):
    async def test_empty_user_message_raises(self):
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                with self.assertRaises(ValueError):
                    await runtime.run_agent_step("feat-019", "", lambda m: LLMResponse())
                with self.assertRaises(ValueError):
                    await runtime.run_agent_step("feat-019", None, lambda m: LLMResponse())  # type: ignore[arg-type]
            finally:
                await store.close()

    async def test_non_positive_max_steps_raises(self):
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                with self.assertRaises(ValueError):
                    await runtime.run_agent_step("feat-019", "hi", lambda m: LLMResponse(), max_steps=0)
                with self.assertRaises(ValueError):
                    await runtime.run_agent_step("feat-019", "hi", lambda m: LLMResponse(), max_steps=-1)
            finally:
                await store.close()

    async def test_invalid_thread_id_raises(self):
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                with self.assertRaises(ValueError):
                    await runtime.run_agent_step("feat with space", "hi", lambda m: LLMResponse())
                with self.assertRaises(ValueError):
                    await runtime.run_agent_step("../escape", "hi", lambda m: LLMResponse())
            finally:
                await store.close()


# ---------- resume ----------


class TestResume(unittest.IsolatedAsyncioTestCase):
    async def test_resume_with_no_state_raises(self):
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                with self.assertRaises(ValueError):
                    await runtime.resume("feat-019", lambda m: LLMResponse())
            finally:
                await store.close()

    async def test_resume_continues_from_persisted_state(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("hello", encoding="utf-8")

            # Run 1: persists a thread with tool call + end_turn.
            store1 = ProjectCheckpointStore(project_path=proj)
            await store1.setup()
            sandbox1 = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
            sandbox1.setup()
            runtime1 = AgentRuntime(checkpoint_store=store1, sandbox=sandbox1)
            llm1 = FakeLLM([
                ScriptedResponse(
                    tool_calls=(ScriptedToolCall(name="read", args={"path": "f.txt"}),),
                    stop_reason="tool_use",
                ),
                ScriptedResponse(content="first run done.", stop_reason="end_turn"),
            ])
            try:
                result1 = await runtime1.run_agent_step("feat-019", "read f.txt", llm1)
                self.assertEqual(result1.content, "first run done.")
                # Snapshot the message count after the first run.
                config = {
                    "configurable": {"thread_id": "feat-019", "checkpoint_ns": ""}
                }
                tup = await store1.get_checkpointer().aget_tuple(config)
                messages_before_resume = len(
                    tup.checkpoint["channel_values"]["messages"]
                )
            finally:
                await store1.close()

            # Run 2: open a brand-new store against the same project,
            # resume the thread, and assert the LLM call + the new
            # assistant end_turn message were appended.
            store2 = ProjectCheckpointStore(project_path=proj)
            await store2.setup()
            sandbox2 = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
            sandbox2.setup()
            runtime2 = AgentRuntime(checkpoint_store=store2, sandbox=sandbox2)
            try:
                llm2 = FakeLLM([
                    ScriptedResponse(content="resumed!", stop_reason="end_turn"),
                ])
                result2 = await runtime2.resume("feat-019", llm2)
                self.assertEqual(result2.content, "resumed!")
                tup = await store2.get_checkpointer().aget_tuple(config)
                msgs = tup.checkpoint["channel_values"]["messages"]
                # Exactly one new assistant message was appended during resume.
                self.assertEqual(len(msgs), messages_before_resume + 1)
            finally:
                await store2.close()


# ---------- integration with FakeLLM and a fixture file (HEDDLE_FAKE_LLM=1) ----------


class TestFakeLLMFixture(unittest.IsolatedAsyncioTestCase):
    """End-to-end using a fixture file under the project, the way CI
    will exercise the runtime."""

    async def test_runtime_against_fixture_file(self):
        with _TempProject() as proj:
            (proj / "x.txt").write_bytes(b"sample content")

            fixture_path = _make_fixture_file(proj, [
                ScriptedResponse(
                    tool_calls=(ScriptedToolCall(name="read", args={"path": "x.txt"}),),
                    stop_reason="tool_use",
                ),
                ScriptedResponse(content="Finished.", stop_reason="end_turn"),
            ])
            self.assertTrue(fixture_path.exists())

            llm = FakeLLM(load_fixture(fixture_path))
            runtime, store, sandbox = await _setup_runtime(proj)
            try:
                result = await runtime.run_agent_step(
                    "feat-019", "Read x.txt", llm
                )
                self.assertEqual(result.content, "Finished.")
                self.assertEqual(sandbox.allowed_count, 1)
                self.assertEqual(llm.remaining, 0)
            finally:
                await store.close()


# ---------- LLM callable shape support ----------


class TestLLMCallableShapes(unittest.IsolatedAsyncioTestCase):
    """The runtime should accept (a) sync LLMResponse callables,
    (b) async LLMResponse callables, (c) LangChain-style objects
    with .ainvoke / .invoke."""

    async def test_sync_callable(self):
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                calls = []
                def llm(messages):
                    calls.append(len(messages))
                    return LLMResponse(content="sync done.", stop_reason="end_turn")
                result = await runtime.run_agent_step("feat-019", "hi", llm)
                self.assertEqual(result.content, "sync done.")
                self.assertEqual(len(calls), 1)
            finally:
                await store.close()

    async def test_async_callable(self):
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                calls = []
                async def llm(messages):
                    calls.append(len(messages))
                    return LLMResponse(content="async done.", stop_reason="end_turn")
                result = await runtime.run_agent_step("feat-019", "hi", llm)
                self.assertEqual(result.content, "async done.")
                self.assertEqual(len(calls), 1)
            finally:
                await store.close()

    async def test_langchain_style_ainvoke(self):
        """An object with .ainvoke(messages) should work too."""
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                class _LangChainStyle:
                    def __init__(self) -> None:
                        self.calls = 0
                    async def ainvoke(self, messages):
                        self.calls += 1
                        return _FakeAIMessage(
                            content="lc done.",
                            tool_calls=[],
                            response_metadata={"stop_reason": "end_turn"},
                        )
                llm = _LangChainStyle()
                result = await runtime.run_agent_step("feat-019", "hi", llm)
                self.assertEqual(result.content, "lc done.")
                self.assertEqual(llm.calls, 1)
            finally:
                await store.close()


# ---------- _coerce_llm_response ----------


class TestCoerceLLMResponse(unittest.TestCase):
    def test_passthrough_llm_response(self):
        resp = LLMResponse(content="x", stop_reason="end_turn")
        self.assertIs(_coerce_llm_response(resp), resp)

    def test_from_fake_ai_message(self):
        from heddle_common.fake_llm import _FakeAIMessage
        m = _FakeAIMessage(
            content="hi",
            tool_calls=[{"name": "read", "args": {"path": "x"}, "id": "c1"}],
            response_metadata={"stop_reason": "tool_use"},
        )
        out = _coerce_llm_response(m)
        self.assertEqual(out.content, "hi")
        self.assertEqual(out.stop_reason, "tool_use")
        self.assertEqual(len(out.tool_calls), 1)
        self.assertEqual(out.tool_calls[0].name, "read")
        self.assertEqual(out.tool_calls[0].id, "c1")

    def test_from_dict(self):
        d = {
            "content": "dict",
            "tool_calls": [{"id": "c1", "name": "write", "args": {"path": "a"}}],
            "stop_reason": "tool_use",
        }
        out = _coerce_llm_response(d)
        self.assertEqual(out.content, "dict")
        self.assertEqual(out.tool_calls[0].name, "write")

    def test_unsupported_type_raises(self):
        with self.assertRaises(TypeError):
            _coerce_llm_response(42)
        with self.assertRaises(TypeError):
            _coerce_llm_response(["list", "not", "supported"])


# ---------- sandbox rejection as a result, not an exception ----------


class TestSandboxRejection(unittest.IsolatedAsyncioTestCase):
    async def test_sandbox_rejection_becomes_tool_message(self):
        """If the sandbox blocks a tool call, the rejection becomes a
        normal tool message — the LLM sees it and can react. The
        runtime must NOT crash the loop."""
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            # Re-create with a read-only sandbox so writes are refused.
            try:
                from heddle_daemon.sandbox import SandboxLevel
                sandbox_ro = ToolDispatchMiddleware(
                    project_root=proj,
                    config=SandboxConfig(level=SandboxLevel.READ_ONLY),
                )
                sandbox_ro.setup()
                runtime_ro = AgentRuntime(
                    checkpoint_store=store,
                    sandbox=sandbox_ro,
                )
                llm = FakeLLM([
                    ScriptedResponse(
                        tool_calls=(ScriptedToolCall(name="write", args={"path": "a.txt", "content": "x"}),),
                        stop_reason="tool_use",
                    ),
                    ScriptedResponse(content="I see — read-only.", stop_reason="end_turn"),
                ])
                result = await runtime_ro.run_agent_step("feat-019", "write a.txt", llm)
                self.assertEqual(result.content, "I see — read-only.")
                self.assertEqual(sandbox_ro.rejected_readonly_count, 1)
            finally:
                await store.close()


# =========================================================================
# feat-022: recursion_limit guardrail (D-052) + daemon-internal LLM retry
# (D-041). HEDDLE_RECURSION_LIMIT → max_steps; exponential backoff on
# LLM failures; LLMRetryExhaustedError after max_retries.
# =========================================================================


class TestRecursionLimitAt200Steps(unittest.IsolatedAsyncioTestCase):
    """feat-022 step 5: a fixture that loops forever must trigger
    RecursionLimitError at max_steps=200 (the spec value, which is
    also the LangGraph recursion_limit the design references)."""

    async def test_infinite_loop_hits_recursion_limit_at_200_steps(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("x", encoding="utf-8")
            runtime, store, _ = await _setup_runtime(proj)
            try:
                llm = FakeLLM(
                    [ScriptedResponse(
                        tool_calls=(ScriptedToolCall(name="read", args={"path": "f.txt"}),),
                        stop_reason="tool_use",
                    )] * 1000,
                    loop=True,
                )
                with self.assertRaises(RecursionLimitError) as ctx:
                    await runtime.run_agent_step(
                        "feat-019", "loop forever", llm, max_steps=200
                    )
                err = ctx.exception
                self.assertEqual(err.cause, "recursion_limit")
                self.assertEqual(err.max_steps, 200)
                self.assertEqual(err.steps_taken, 200)
            finally:
                await store.close()


class TestRecursionLimitEnvVar(unittest.TestCase):
    """feat-022 step 1: HEDDLE_RECURSION_LIMIT env var is the single
    chokepoint for the max_steps ceiling."""

    def test_default_when_env_missing(self):
        self.assertEqual(get_max_steps_from_env({}), DEFAULT_MAX_STEPS)

    def test_explicit_value(self):
        self.assertEqual(get_max_steps_from_env({RECURSION_LIMIT_ENV_VAR: "50"}), 50)

    def test_invalid_value_falls_back_to_default(self):
        self.assertEqual(get_max_steps_from_env({RECURSION_LIMIT_ENV_VAR: "abc"}), DEFAULT_MAX_STEPS)

    def test_zero_value_falls_back_to_default(self):
        self.assertEqual(get_max_steps_from_env({RECURSION_LIMIT_ENV_VAR: "0"}), DEFAULT_MAX_STEPS)

    def test_negative_value_falls_back_to_default(self):
        self.assertEqual(get_max_steps_from_env({RECURSION_LIMIT_ENV_VAR: "-5"}), DEFAULT_MAX_STEPS)


class TestFormatCheckpointId(unittest.TestCase):
    """The id must be lexicographically monotonic across saves."""

    def test_basic_format(self):
        self.assertEqual(
            _format_checkpoint_id("feat-019", 5),
            "feat-019-00000000000000000005",
        )

    def test_lex_order_matches_numeric_order(self):
        ids = [_format_checkpoint_id("feat-019", n) for n in (1, 10, 100, 1000)]
        self.assertEqual(ids, sorted(ids))

    def test_zero_pads_large_counts(self):
        self.assertEqual(
            _format_checkpoint_id("t", 1_000_000_000_000),
            "t-00000001000000000000",
        )


class TestLLMRetry(unittest.IsolatedAsyncioTestCase):
    """feat-022 step 3+4: exponential backoff 1s->2s->4s, max 3 attempts;
    on exhaustion, structured LLMRetryExhaustedError per D-041."""

    async def test_retry_succeeds_on_third_attempt_after_two_transient_failures(self):
        """feat-022 step 6: 'a mock LLM that fails twice then succeeds;
        assert 3 calls happened with correct delays'."""
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                call_times: list[float] = []

                async def flaky(messages):
                    call_times.append(asyncio.get_running_loop().time())
                    if len(call_times) < 3:
                        raise RuntimeError(f"transient failure #{len(call_times)}")
                    return LLMResponse(content="finally", stop_reason="end_turn")

                # 50ms backoff base so the test runs in <1s. The
                # exponential 0.05 -> 0.10 pattern is what the
                # production 1s -> 2s pattern scales to.
                runtime.backoff_base_seconds = 0.05

                response = await runtime._call_llm(flaky, [])
                self.assertEqual(response.content, "finally")
                self.assertEqual(len(call_times), 3)
                # Delay between attempt 1 and 2: ~0.05s; 2 and 3: ~0.10s.
                # We assert each delay was at least 80% of its
                # nominal value (Windows asyncio.sleep can wake up a
                # few ms early). Exponential backoff is verified by
                # d2 > d1 — no need for a strict 2x ratio that's
                # brittle under scheduler jitter.
                d1 = call_times[1] - call_times[0]
                d2 = call_times[2] - call_times[1]
                self.assertGreaterEqual(d1, 0.04)
                self.assertGreaterEqual(d2, 0.08)
                self.assertGreater(d2, d1)
            finally:
                await store.close()

    async def test_all_failures_raises_structured_error(self):
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                runtime.backoff_base_seconds = 0.01  # fast
                call_count = 0

                async def always_fails(messages):
                    nonlocal call_count
                    call_count += 1
                    raise ConnectionError("LLM provider down")

                with self.assertRaises(LLMRetryExhaustedError) as ctx:
                    await runtime._call_llm(always_fails, [])
                err = ctx.exception
                self.assertEqual(err.cause, "llm_retry_exhausted")
                self.assertEqual(err.attempts, 3)
                self.assertEqual(call_count, 3)
                self.assertIsInstance(err.last_exception, ConnectionError)
            finally:
                await store.close()

    async def test_default_max_retries_is_three(self):
        """The spec calls for 3 attempts; verify the default matches."""
        self.assertEqual(DEFAULT_MAX_LLM_RETRIES, 3)

    async def test_max_retries_one_disables_retry(self):
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                runtime.max_retries = 1
                runtime.backoff_base_seconds = 0.01
                call_count = 0

                async def always_fails(messages):
                    nonlocal call_count
                    call_count += 1
                    raise RuntimeError("nope")

                with self.assertRaises(LLMRetryExhaustedError) as ctx:
                    await runtime._call_llm(always_fails, [])
                self.assertEqual(call_count, 1)
                self.assertEqual(ctx.exception.attempts, 1)
            finally:
                await store.close()

    async def test_non_retryable_exceptions_propagate_immediately(self):
        """Programming errors (e.g. RecursionLimitError) must not be
        retried \u2014 retrying them would loop forever or mask the bug."""
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                call_count = 0

                async def raises_rl(messages):
                    nonlocal call_count
                    call_count += 1
                    raise RecursionLimitError(steps_taken=1, max_steps=1)

                with self.assertRaises(RecursionLimitError):
                    await runtime._call_llm(raises_rl, [])
                # Retried: only 1 call (the exception short-circuits).
                self.assertEqual(call_count, 1)
            finally:
                await store.close()

    async def test_cancellation_not_retried(self):
        """asyncio.CancelledError must propagate so the runtime
        shutdown path works cleanly."""
        with _TempProject() as proj:
            runtime, store, _ = await _setup_runtime(proj)
            try:
                async def raise_cancel(messages):
                    raise asyncio.CancelledError()

                with self.assertRaises(asyncio.CancelledError):
                    await runtime._call_llm(raise_cancel, [])
            finally:
                await store.close()


class TestLLMRetryExhaustedError(unittest.TestCase):
    def test_is_agent_runtime_error(self):
        err = LLMRetryExhaustedError(attempts=3, last_exception=RuntimeError("x"))
        self.assertIsInstance(err, AgentRuntimeError)
        self.assertEqual(err.cause, "llm_retry_exhausted")
        self.assertEqual(err.attempts, 3)
        self.assertIsInstance(err.last_exception, RuntimeError)
        self.assertIn("3 attempts", str(err))
        self.assertIn("RuntimeError", str(err))

    def test_cause_is_d_041_compliant(self):
        """D-041 mandates cause='llm_retry_exhausted' so the UI / WS
        layer can render a structured failure without parsing prose."""
        err = LLMRetryExhaustedError(3, RuntimeError("boom"))
        self.assertEqual(err.cause, "llm_retry_exhausted")


class TestRetryIntegrationWithRun(unittest.IsolatedAsyncioTestCase):
    """End-to-end: a flaky LLM heals mid-loop; the loop still completes."""

    async def test_flaky_llm_with_recovery_completes_run(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("hello", encoding="utf-8")
            runtime, store, _ = await _setup_runtime(proj)
            try:
                runtime.backoff_base_seconds = 0.01
                # 2 failures, then 1 tool_use, then 1 end_turn = 4 calls.
                # The retry budget is per-LLM-call, so the tool_use
                # call after recovery starts a fresh 3-attempt budget.
                call_count = 0

                async def flaky_then_ok(messages):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        raise RuntimeError("transient 1")
                    if call_count == 2:
                        raise RuntimeError("transient 2")
                    if call_count == 3:
                        return LLMResponse(
                            tool_calls=(ToolCall(
                                id="c1", name="read", args={"path": "f.txt"},
                            ),),
                            stop_reason="tool_use",
                        )
                    return LLMResponse(content="done.", stop_reason="end_turn")

                result = await runtime.run_agent_step(
                    "feat-019", "Read f.txt", flaky_then_ok
                )
                self.assertEqual(result.content, "done.")
                self.assertEqual(call_count, 4)
            finally:
                await store.close()


if __name__ == "__main__":
    unittest.main()
