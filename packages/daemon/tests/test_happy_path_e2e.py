# SPDX-License-Identifier: Apache-2.0
"""End-to-end happy-path test — feat-049.

Walks the full user journey through a real ``Daemon`` + ``websockets``
client without touching a real LLM:

  1. ``dialog_turn`` returns a single draft card (FakeLLM fixture mode).
  2. ``drafts_confirm`` persists the draft to ``feature_list.json``
     with ``status="pending"``.
  3. ``start_feature`` transitions the feature to ``in_progress``,
     resolves the per-feature LLM via the FakeLLM fixture, and emits
     ``feature_attempt_started`` + ``llm_resolved`` events.
  4. The test drives ``AgentRuntime.run_agent_step`` with the
     scripted FakeLLM (one ``end_turn`` response — completes
     immediately). The LLM audit log captures the call.
  5. ``feature_transition`` with ``action="mark-done"`` advances
     the feature to ``passing`` (mirrors what the supervisor does
     once the agent reports success in production).

The test exercises every wiring point from feat-017 through feat-049
in one flow so a regression in any of them is caught immediately.
Marked ``@pytest.mark.slow`` so ``heddle test -m "not slow"`` can
skip it; the full happy-path round-trip spins up an asyncio daemon +
websocket client + an AgentRuntime loop, so a typical run takes
~3-5s on Windows.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
import websockets

from heddle_common import add_project, feature_list_io
from heddle_common.event_log import DEFAULT_EVENT_LOG_FILENAME
from heddle_common.projects_io import (
    DEFAULT_PROJECTS_PATH,
    Project,
    save_projects,
)
from heddle_daemon.agent_runtime import AgentRuntime
from heddle_daemon.decompose import DECOMPOSE_FIXTURE_NAME
from heddle_daemon.llm_audit import LLM_AUDIT_FILENAME
from heddle_daemon.sandbox import SandboxConfig, ToolDispatchMiddleware
from heddle_daemon.server import (
    Daemon,
    DaemonConfig,
    JsonEnvelope,
    build_envelope,
    parse_envelope,
)


# ---------- helpers ----------


@contextmanager
def _projects_registry(registry_path: Path, projects: list[Project]) -> Iterator[None]:
    """Point ``heddle_common.projects_io`` at an isolated projects.json file."""
    save_projects(registry_path, {p.id: p for p in projects})
    saved_default = DEFAULT_PROJECTS_PATH
    import heddle_common.projects_io as _pio

    object.__setattr__(_pio, "DEFAULT_PROJECTS_PATH", str(registry_path))
    try:
        yield
    finally:
        object.__setattr__(_pio, "DEFAULT_PROJECTS_PATH", saved_default)


@contextmanager
def _isolated_logs_dir(logs_dir: Path) -> Iterator[None]:
    """Point ``server.DEFAULT_LOGS_DIR`` at an isolated directory."""
    import heddle_daemon.server as _server

    saved = _server.DEFAULT_LOGS_DIR
    object.__setattr__(_server, "DEFAULT_LOGS_DIR", str(logs_dir))
    try:
        yield
    finally:
        object.__setattr__(_server, "DEFAULT_LOGS_DIR", saved)


async def _round_trip(
    daemon: Daemon,
    env: JsonEnvelope,
    *,
    timeout: float = 5.0,
) -> JsonEnvelope:
    """Send one envelope; wait for the matching response; parse it.

    Drains unsolicited event envelopes silently so callers see only
    the terminal response (matches test_intent_wiring's helper).
    """
    url = f"ws://127.0.0.1:{daemon.bound_port}/ws"
    async with websockets.connect(url) as conn:
        await conn.send(env.to_json())
        deadline = timeout
        while True:
            raw = await asyncio.wait_for(conn.recv(), timeout=deadline)
            received = parse_envelope(raw)
            if received.type == "event":
                continue
            return received


async def _round_trip_collect_events(
    daemon: Daemon,
    env: JsonEnvelope,
    *,
    timeout: float = 5.0,
) -> tuple[JsonEnvelope, list[dict[str, Any]]]:
    """Like ``_round_trip`` but also returns the events pushed before
    the terminal response. Used to assert on the ``feature_attempt_started``
    + ``llm_resolved`` pair.
    """
    url = f"ws://127.0.0.1:{daemon.bound_port}/ws"
    events: list[dict[str, Any]] = []
    async with websockets.connect(url) as conn:
        await conn.send(env.to_json())
        deadline = timeout
        while True:
            raw = await asyncio.wait_for(conn.recv(), timeout=deadline)
            received = parse_envelope(raw)
            if received.type == "event":
                events.append(received.extra)
                continue
            return received, events


def _empty_feature_list(project_dir: Path) -> Path:
    """Drop an empty-but-valid feature_list.json (no rows)."""
    fl = {
        "schema_version": 1,
        "features": [],
        "metadata": {
            "total_features": 0,
            "passing": 0,
            "failing": 0,
            "in_progress": 0,
            "blocked": 0,
            "deferred": 0,
            "last_updated": "2026-09-13",
        },
    }
    p = project_dir / "feature_list.json"
    p.write_text(json.dumps(fl, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _write_decompose_fixture(fixture_root: Path) -> Path:
    """Single-draft decompose.json — the fake-LLM returns one card."""
    drafts = [
        {
            "id": "temp-001",
            "title": "Add hello-world feature",
            "description": "Minimal hello-world feature for the e2e happy path.",
            "steps": [
                "Read the user's request.",
                "Write the implementation.",
                "Run the relevant tests.",
            ],
            "depends_on": [],
            "kind": "feature",
        }
    ]
    p = fixture_root / DECOMPOSE_FIXTURE_NAME
    p.write_text(json.dumps(drafts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def _write_agent_fixture(fixture_root: Path, feature_id: str) -> Path:
    """A fixture that completes in 1 LLM turn (``end_turn``).

    The daemon's ``build_llm_for_feature`` loads this under fake mode.
    """
    payload = {
        "schema_version": 1,
        "responses": [
            {
                "content": "hello-world feature shipped.",
                "tool_calls": [],
                "stop_reason": "end_turn",
            }
        ],
    }
    p = fixture_root / f"{feature_id}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


# ---------- the test ----------


def _make_project_stub(project_dir: Path, project_id: str = "proj-happy-stub") -> Project:
    """Build a registered Project stub pointing at ``project_dir``.

    The daemon's ``Daemon.start()`` matches ``config.project_path``
    against the registered projects to decide whether to attach the
    per-project log sinks; without a matching stub the audit / event
    log fall through to ``event_log_skipped``.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Project(
        id=project_id,
        name="proj-happy",
        path=str(project_dir.resolve()),
        added_at=now,
        last_accessed_at=now,
    )


@pytest.mark.slow
class TestHappyPathEndToEnd(unittest.IsolatedAsyncioTestCase):
    """Full user flow: dialog → draft → confirm → start → run → passing."""

    async def asyncSetUp(self) -> None:
        self._setup_tmp_dirs()
        self._setup_env()
        self._setup_project_registry()
        await self._start_daemon()

    async def asyncTearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("HEDDLE_FAKE_LLM", None)
        else:
            os.environ["HEDDLE_FAKE_LLM"] = self._saved_env

    # ----- setup helpers (split out to keep asyncSetUp ≤ 40 lines) -----

    def _setup_tmp_dirs(self) -> None:
        """Allocate tmp project + fixture roots and seed both files."""
        self._proj_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._proj_tmp.cleanup)
        self.proj_tmp = Path(self._proj_tmp.name)
        self.projects_path = self.proj_tmp / "projects.json"
        self.project_dir = self.proj_tmp / "proj-happy"
        self.project_dir.mkdir()
        _empty_feature_list(self.project_dir)

        self._fix_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._fix_tmp.cleanup)
        self.fixture_root = Path(self._fix_tmp.name)
        _write_decompose_fixture(self.fixture_root)

        self.logs_dir = self.proj_tmp / "logs"

    def _setup_env(self) -> None:
        """Save + restore the HEDDLE_FAKE_LLM env var around the test."""
        self._saved_env = os.environ.get("HEDDLE_FAKE_LLM")
        os.environ["HEDDLE_FAKE_LLM"] = "1"

    def _setup_project_registry(self) -> None:
        """Register the project in projects.json + patch the default.

        Must run BEFORE daemon.start() so the log sinks can find a
        matching project and don't fall through to the
        ``no_matching_project`` graceful fallback.
        """
        self._stub = _make_project_stub(self.project_dir)
        save_projects(self.projects_path, {self._stub.id: self._stub})
        self._registry_cm = _projects_registry(self.projects_path, [self._stub])
        self._registry_cm.__enter__()
        self.addCleanup(self._registry_cm.__exit__, None, None, None)
        self.project_id = self._stub.id

        self._logs_cm = _isolated_logs_dir(self.logs_dir)
        self._logs_cm.__enter__()
        self.addCleanup(self._logs_cm.__exit__, None, None, None)

    async def _start_daemon(self) -> None:
        """Construct + bind the daemon and register the cleanup."""
        self.daemon = Daemon(
            DaemonConfig(
                port=0,
                project_path=self.project_dir.resolve(),
                fixture_root=self.fixture_root,
                logs_dir=self.logs_dir,
            )
        )
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        self.daemon._routes.fixture_root = self.fixture_root
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

    # ----- WS envelope helpers -----

    async def _dialog(self, message: str, *, req_id: str) -> dict[str, Any]:
        env = build_envelope(
            "dialog_turn", req_id=req_id, project_id=self.project_id, message=message
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        return resp.extra["data"]

    async def _drafts_confirm(self, drafts: list[dict[str, Any]]) -> dict[str, Any]:
        env = build_envelope(
            "drafts_confirm",
            req_id="confirm-1",
            project_id=self.project_id,
            drafts=drafts,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        return resp.extra["data"]

    async def _start_feature(
        self, feature_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        env = build_envelope(
            "start_feature",
            req_id="start-1",
            project_id=self.project_id,
            feature_id=feature_id,
        )
        resp, events = await _round_trip_collect_events(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        return resp.extra["data"], events

    async def _feature_transition(
        self, feature_id: str, action: str
    ) -> dict[str, Any]:
        env = build_envelope(
            "feature_transition",
            req_id=f"tr-{action}",
            project_id=self.project_id,
            feature_id=feature_id,
            action=action,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        return resp.extra["data"]

    def _feature_status(self, feature_id: str) -> str:
        data = feature_list_io.load(self.project_dir / "feature_list.json")
        for f in data["features"]:
            if f.get("id") == feature_id:
                return f.get("status", "?")
        raise AssertionError(f"feature {feature_id} not found")

    # ----- the actual test (split into numbered step methods) -----

    async def test_happy_path(self) -> None:
        feature_id = await self._step_1_dialog_returns_draft()
        self._step_2_drop_agent_fixture(feature_id)
        await self._step_3_drafts_confirm(feature_id)
        await self._step_4_start_feature(feature_id)
        await self._step_5_run_agent(feature_id)
        await self._step_6_mark_done(feature_id)
        self._step_7_assert_audit_and_events(feature_id)

    async def _step_1_dialog_returns_draft(self) -> str:
        """User types 'add a hello-world feature' → FakeLLM returns 1 draft."""
        data = await self._dialog("add a hello-world feature", req_id="d1")
        self.assertEqual(data["kind"], "work")
        drafts = data["drafts"]
        self.assertIsInstance(drafts, list)
        self.assertGreaterEqual(len(drafts), 1)
        draft = drafts[0]
        self.assertTrue(draft["id"].startswith("temp-"))
        # Stash the drafts list on self so step 3 can reuse the same
        # payload without re-issuing a dialog round-trip.
        self._drafts = drafts
        return draft["id"]

    def _step_2_drop_agent_fixture(self, feature_id: str) -> None:
        """Drop the per-feature FakeLLM fixture (only now that we know id)."""
        _write_agent_fixture(self.fixture_root, feature_id)

    async def _step_3_drafts_confirm(self, feature_id: str) -> None:
        """User confirms the draft → feature_list.json gains a pending row."""
        added = await self._drafts_confirm(self._drafts)
        self.assertIn("added", added)
        self.assertGreaterEqual(len(added["added"]), 1)
        self.assertEqual(added["added"][0]["id"], feature_id)
        self.assertEqual(self._feature_status(feature_id), "pending")

    async def _step_4_start_feature(self, feature_id: str) -> None:
        """start_feature → in_progress; emits feature_attempt_started + llm_resolved."""
        start_data, events = await self._start_feature(feature_id)
        self.assertEqual(start_data["status"], "in_progress")
        self.assertEqual(self._feature_status(feature_id), "in_progress")
        names = [e.get("event") for e in events]
        self.assertIn("feature_attempt_started", names)
        self.assertIn("llm_resolved", names)

    async def _step_5_run_agent(self, feature_id: str) -> None:
        """Drive AgentRuntime with the FakeLLM built by start_feature.

        The daemon's start_feature handler doesn't invoke run_agent_step
        itself yet (feat-019 wiring is a v0.2 follow-up); the supervisor
        (Node.js) owns this step in production. We drive it directly
        here, threading the daemon's llm_audit logger into the runtime
        so the LLM call writes to the per-project audit sidecar.
        """
        sandbox = ToolDispatchMiddleware(
            project_root=self.project_dir, config=SandboxConfig()
        )
        sandbox.setup()
        runtime = AgentRuntime(
            checkpoint_store=self.daemon.checkpoint_store,
            sandbox=sandbox,
            llm_audit=self.daemon._llm_audit,
            audit_model="anthropic-claude-sonnet",
        )
        attempt_llms = self.daemon._routes._attempt_llms
        self.assertGreaterEqual(len(attempt_llms), 1)
        _, llm = next(iter(attempt_llms.values()))
        result = await runtime.run_agent_step(
            thread_id=feature_id,
            user_message="Implement the hello-world feature.",
            llm=llm,
            max_steps=10,
        )
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertFalse(result.has_tool_calls)

    async def _step_6_mark_done(self, feature_id: str) -> None:
        """feature_transition mark-done → status=passing."""
        await self._feature_transition(feature_id, "mark-done")
        self.assertEqual(self._feature_status(feature_id), "passing")

    def _step_7_assert_audit_and_events(self, feature_id: str) -> None:
        """LLM audit log + structured event log both captured the run."""
        audit_path = self.logs_dir / self.project_id / LLM_AUDIT_FILENAME
        records = _read_jsonl(audit_path)
        self.assertGreaterEqual(
            len(records), 1,
            f"expected at least one audit record at {audit_path}; "
            f"got {len(records)}",
        )
        rec = records[0]
        self.assertEqual(rec.get("feature_id"), feature_id)
        self.assertEqual(rec.get("outcome"), "ok")
        self.assertIn("latency_ms", rec)
        self.assertIn("model", rec)

        events_path = self.logs_dir / self.project_id / DEFAULT_EVENT_LOG_FILENAME
        events_log = _read_jsonl(events_path)
        names = [e.get("event") for e in events_log]
        self.assertIn("feature_attempt_started", names)
        self.assertIn("dialog_done", names)


if __name__ == "__main__":
    unittest.main()
