# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_common.fake_llm (feat-007 / T-018 / T-031).

Covers the three pieces of feat-007:

  1. Fixture loader (`load_fixture`) — schema, validation, error paths.
  2. FakeLLM class — sequential consumption, looping, exhaustion.
  3. `fake_llm_or_real` chokepoint — env-var-driven switching.

Plus an integration test that exercises a 3-step agent run end-to-end
via `FakeLLM.invoke()` (the same surface feat-019's `agent_runtime.py`
will call).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from heddle_common import fake_llm as fl

# REPO_ROOT kept for parity with the sibling test file even though this
# suite uses a tmp dir for every fixture — leaves room for fixtures
# shipped under packages/daemon/tests/fixtures/ later.
REPO_ROOT = Path(__file__).resolve().parents[4]


def _write_fixture(tmpdir: Path, name: str, payload: dict) -> Path:
    p = tmpdir / name
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def _minimal_fixture(responses: list[dict] | None = None) -> dict:
    """A 3-step agent run used by the integration tests.

    Mirrors the canonical "load → read → write" scenario a coding-agent
    fixture would exercise (D-053's Read/Write tools).
    """
    if responses is None:
        responses = [
            {
                "content": "I'll start by reading the README.",
                "tool_calls": [
                    {"name": "Read", "args": {"path": "README.md"}},
                ],
                "stop_reason": "tool_use",
            },
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "Write",
                        "args": {"path": "out.txt", "content": "hello"},
                    },
                ],
                "stop_reason": "tool_use",
            },
            {
                "content": "Done.",
                "tool_calls": [],
                "stop_reason": "end_turn",
            },
        ]
    return {"schema_version": 1, "responses": responses}


class TestIsFakeLLMEnabled(unittest.TestCase):
    """`is_fake_llm_enabled(env)` — env var truthiness contract."""

    def test_missing_env_var_disables(self):
        self.assertFalse(fl.is_fake_llm_enabled({}))

    def test_truthy_values_enable(self):
        for v in ("1", "true", "yes", "on", "TRUE", "Yes", " 1 "):
            with self.subTest(value=v):
                self.assertTrue(fl.is_fake_llm_enabled({fl.FAKE_LLM_ENV_VAR: v}))

    def test_falsy_values_disable(self):
        for v in ("0", "false", "no", "off", "", "anything-else"):
            with self.subTest(value=v):
                self.assertFalse(fl.is_fake_llm_enabled({fl.FAKE_LLM_ENV_VAR: v}))


class TestLoadFixture(unittest.TestCase):
    """`load_fixture(path)` — schema, validation, error paths."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="fake_llm_", dir=Path(sys.argv[0]).parent))

    def tearDown(self):
        for p in self.tmpdir.glob("*"):
            p.unlink()
        self.tmpdir.rmdir()

    def test_minimal_fixture_loads(self):
        p = _write_fixture(self.tmpdir, "ok.json", _minimal_fixture())
        responses = fl.load_fixture(p)
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0].content, "I'll start by reading the README.")
        self.assertEqual(responses[0].stop_reason, "tool_use")
        self.assertEqual(len(responses[0].tool_calls), 1)
        self.assertEqual(responses[0].tool_calls[0].name, "Read")
        self.assertEqual(responses[0].tool_calls[0].args, {"path": "README.md"})
        # Last response: end_turn, no tool calls.
        self.assertEqual(responses[-1].content, "Done.")
        self.assertEqual(responses[-1].stop_reason, "end_turn")
        self.assertEqual(responses[-1].tool_calls, ())

    def test_empty_responses_array_fails_loud(self):
        p = _write_fixture(self.tmpdir, "empty.json", {"schema_version": 1, "responses": []})
        with self.assertRaises(SystemExit):
            fl.load_fixture(p)

    def test_unknown_top_level_key_fails_loud(self):
        p = _write_fixture(
            self.tmpdir,
            "extra_key.json",
            {"schema_version": 1, "responses": [], "bogus_key": 42},
        )
        with self.assertRaises(SystemExit):
            fl.load_fixture(p)

    def test_unknown_response_key_fails_loud(self):
        p = _write_fixture(
            self.tmpdir,
            "bad_resp.json",
            {
                "schema_version": 1,
                "responses": [{"content": "x", "extra": 1}],
            },
        )
        with self.assertRaises(SystemExit):
            fl.load_fixture(p)

    def test_bad_stop_reason_fails_loud(self):
        p = _write_fixture(
            self.tmpdir,
            "bad_stop.json",
            {
                "schema_version": 1,
                "responses": [{"content": "x", "stop_reason": "wat"}],
            },
        )
        with self.assertRaises(SystemExit):
            fl.load_fixture(p)

    def test_tool_call_with_empty_name_fails_loud(self):
        p = _write_fixture(
            self.tmpdir,
            "bad_tc.json",
            {
                "schema_version": 1,
                "responses": [{"tool_calls": [{"name": "", "args": {}}]}],
            },
        )
        with self.assertRaises(SystemExit):
            fl.load_fixture(p)

    def test_tool_call_with_non_dict_args_fails_loud(self):
        p = _write_fixture(
            self.tmpdir,
            "bad_args.json",
            {
                "schema_version": 1,
                "responses": [{"tool_calls": [{"name": "Read", "args": "nope"}]}],
            },
        )
        with self.assertRaises(SystemExit):
            fl.load_fixture(p)

    def test_non_object_root_fails_loud(self):
        p = self.tmpdir / "list_root.json"
        p.write_text(json.dumps([{"responses": []}]), encoding="utf-8")
        with self.assertRaises(SystemExit):
            fl.load_fixture(p)

    def test_missing_responses_key_fails_loud(self):
        p = self.tmpdir / "no_resp.json"
        p.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            fl.load_fixture(p)

    def test_missing_file_fails_loud(self):
        with self.assertRaises(SystemExit):
            fl.load_fixture(self.tmpdir / "does_not_exist.json")

    def test_oversize_file_fails_loud(self):
        big = self.tmpdir / "huge.json"
        # Write slightly over MAX_FIXTURE_BYTES without filling RAM.
        with big.open("w", encoding="utf-8") as f:
            f.write('{"schema_version": 1, "responses": ["')
            chunk = "x" * 1024
            target = fl.MAX_FIXTURE_BYTES + 1024
            written = 0
            while written < target:
                f.write(chunk)
                written += len(chunk)
            f.write('"]}')
        with self.assertRaises(SystemExit):
            fl.load_fixture(big)

    def test_too_many_responses_fails_loud(self):
        p = _write_fixture(
            self.tmpdir,
            "many.json",
            {"schema_version": 1, "responses": [{"content": "x"}] * (fl.MAX_FIXTURE_RESPONSES + 1)},
        )
        with self.assertRaises(SystemExit):
            fl.load_fixture(p)


class TestFakeLLM(unittest.TestCase):
    """`FakeLLM` — sequential consumption, looping, exhaustion."""

    def _make(self, responses: list[dict] | None = None, **kw) -> fl.FakeLLM:
        return fl.FakeLLM(fl.load_fixture(_write_fixture(self.tmpdir, "f.json", _minimal_fixture(responses))), **kw)

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="fake_llm_", dir=Path(sys.argv[0]).parent))

    def tearDown(self):
        for p in self.tmpdir.glob("*"):
            p.unlink()
        self.tmpdir.rmdir()

    def test_sequential_consumption_returns_each_response_once(self):
        llm = self._make()
        seen_contents = []
        for _ in range(3):
            msg = llm.invoke([])
            seen_contents.append(msg.content)
        self.assertEqual(
            seen_contents,
            [
                "I'll start by reading the README.",
                "",
                "Done.",
            ],
        )

    def test_invoke_wraps_tool_calls_with_ids(self):
        llm = self._make()
        msg = llm.invoke([])
        self.assertEqual(msg.content, "I'll start by reading the README.")
        self.assertEqual(len(msg.tool_calls), 1)
        tc = msg.tool_calls[0]
        self.assertEqual(tc["name"], "Read")
        self.assertEqual(tc["args"], {"path": "README.md"})
        self.assertTrue(tc["id"].startswith("call_"))

    def test_invoke_propagates_stop_reason(self):
        llm = self._make()
        m1 = llm.invoke([])
        m2 = llm.invoke([])
        m3 = llm.invoke([])
        self.assertEqual(m1.response_metadata["stop_reason"], "tool_use")
        self.assertEqual(m2.response_metadata["stop_reason"], "tool_use")
        self.assertEqual(m3.response_metadata["stop_reason"], "end_turn")

    def test_exhaustion_raises_loud(self):
        llm = self._make()
        for _ in range(3):
            llm.invoke([])
        with self.assertRaises(fl.FakeLLMExhausted):
            llm.invoke([])

    def test_loop_recycles_from_start(self):
        llm = self._make(loop=True)
        first = llm.invoke([])
        for _ in range(5):
            llm.invoke([])
        # After 6 invocations (1 + 5), looping restarts.
        sixth = llm.invoke([])
        self.assertEqual(sixth.content, first.content)

    def test_remaining_decrements(self):
        llm = self._make()
        self.assertEqual(llm.remaining, 3)
        llm.invoke([])
        self.assertEqual(llm.remaining, 2)
        llm.invoke([])
        self.assertEqual(llm.remaining, 1)
        llm.invoke([])
        self.assertEqual(llm.remaining, 0)

    def test_remaining_is_minus_one_when_looping(self):
        llm = self._make(loop=True)
        self.assertEqual(llm.remaining, -1)
        for _ in range(50):
            llm.invoke([])
        self.assertEqual(llm.remaining, -1)

    def test_empty_responses_fails_at_construction(self):
        with self.assertRaises(SystemExit):
            fl.FakeLLM([])

    def test_iter_yields_scripted_responses(self):
        llm = self._make()
        self.assertEqual(
            [r.stop_reason for r in iter(llm)],
            ["tool_use", "tool_use", "end_turn"],
        )


class TestFakeLLMOrReal(unittest.TestCase):
    """`fake_llm_or_real` — env-var-driven chokepoint."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="fake_llm_", dir=Path(sys.argv[0]).parent))

    def tearDown(self):
        for p in self.tmpdir.glob("*"):
            p.unlink()
        self.tmpdir.rmdir()

    def test_returns_fake_when_env_set_and_fixture_provided(self):
        fixture = _write_fixture(self.tmpdir, "f.json", _minimal_fixture())
        sentinel = object()
        result = fl.fake_llm_or_real(
            real_factory=lambda: sentinel,
            fixture_path=fixture,
            env={fl.FAKE_LLM_ENV_VAR: "1"},
        )
        self.assertIsInstance(result, fl.FakeLLM)
        self.assertIsNot(result, sentinel)

    def test_fake_mode_without_fixture_fails_loud(self):
        with self.assertRaises(SystemExit):
            fl.fake_llm_or_real(
                real_factory=lambda: None,
                fixture_path=None,
                env={fl.FAKE_LLM_ENV_VAR: "1"},
            )

    def test_returns_real_when_env_unset(self):
        sentinel = object()
        result = fl.fake_llm_or_real(real_factory=lambda: sentinel, fixture_path=None, env={})
        self.assertIs(result, sentinel)

    def test_returns_real_when_env_falsy(self):
        sentinel = object()
        result = fl.fake_llm_or_real(
            real_factory=lambda: sentinel,
            fixture_path=None,
            env={fl.FAKE_LLM_ENV_VAR: "0"},
        )
        self.assertIs(result, sentinel)

    def test_real_factory_not_called_in_fake_mode(self):
        # Sanity: passing a real_factory that would crash is fine because
        # fake mode must never invoke it. We use a callable that records
        # whether it was called (instead of raising), since fake mode
        # short-circuits before real_factory ever runs.
        fixture = _write_fixture(self.tmpdir, "f.json", _minimal_fixture())

        called = {"n": 0}

        def _factory() -> object:
            called["n"] += 1
            return None

        llm = fl.fake_llm_or_real(
            real_factory=_factory,
            fixture_path=fixture,
            env={fl.FAKE_LLM_ENV_VAR: "1"},
        )
        self.assertIsInstance(llm, fl.FakeLLM)
        self.assertEqual(called["n"], 0, "real_factory must not run in fake mode")
        # Sanity: invoking the returned FakeLLM also must not touch the factory.
        llm.invoke([])
        llm.invoke([])
        self.assertEqual(called["n"], 0)


class TestEndToEndThreeStepAgentRun(unittest.TestCase):
    """Integration test — feat-007 step 5.

    Drives `FakeLLM.invoke()` for the canonical 3-step scenario
    (Read → Write → done) and asserts the scripted conversation yields
    the right tool calls and final stop_reason. This is the same shape
    feat-019's `agent_runtime.py` loop will execute under
    `HEDDLE_FAKE_LLM=1`.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="fake_llm_", dir=Path(sys.argv[0]).parent))

    def tearDown(self):
        for p in self.tmpdir.glob("*"):
            p.unlink()
        self.tmpdir.rmdir()

    def test_three_step_run_via_chokepoint(self):
        fixture = _write_fixture(self.tmpdir, "3step.json", _minimal_fixture())

        # The chokepoint returns a FakeLLM under fake mode.
        llm = fl.fake_llm_or_real(
            real_factory=lambda: None,  # would be `build_chat_model(cfg)` in feat-019
            fixture_path=fixture,
            env={fl.FAKE_LLM_ENV_VAR: "1"},
        )
        self.assertIsInstance(llm, fl.FakeLLM)

        # Step 1: Read.
        m1 = llm.invoke([])
        self.assertEqual(len(m1.tool_calls), 1)
        self.assertEqual(m1.tool_calls[0]["name"], "Read")
        self.assertEqual(m1.tool_calls[0]["args"], {"path": "README.md"})
        self.assertEqual(m1.response_metadata["stop_reason"], "tool_use")

        # Step 2: Write.
        m2 = llm.invoke([])
        self.assertEqual(len(m2.tool_calls), 1)
        self.assertEqual(m2.tool_calls[0]["name"], "Write")
        self.assertEqual(
            m2.tool_calls[0]["args"],
            {"path": "out.txt", "content": "hello"},
        )
        self.assertEqual(m2.response_metadata["stop_reason"], "tool_use")

        # Step 3: terminal end_turn.
        m3 = llm.invoke([])
        self.assertEqual(m3.content, "Done.")
        self.assertEqual(m3.tool_calls, [])
        self.assertEqual(m3.response_metadata["stop_reason"], "end_turn")

        # Step 4: exhausted.
        with self.assertRaises(fl.FakeLLMExhausted):
            llm.invoke([])


if __name__ == "__main__":
    unittest.main()
