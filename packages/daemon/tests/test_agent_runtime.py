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
    DEFAULT_MAX_STEPS,
    SYSTEM_PROMPT,
    AgentRuntime,
    AgentRuntimeError,
    LLMResponse,
    RecursionLimitError,
    ToolCall,
    _coerce_llm_response,
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


if __name__ == "__main__":
    unittest.main()
