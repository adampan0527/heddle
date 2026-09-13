# SPDX-License-Identifier: Apache-2.0
"""Tests for feat-030's per-feature command handlers + event emission.

The four new commands (start_feature / stop_feature / retry_feature /
dialog_turn) push one or more ``type: "event"`` envelopes onto the
WS BEFORE returning their terminal response envelope. These tests
exercise the full daemon <-> WS round-trip and assert:

  1. The terminal response envelope is well-formed (ok=True, data
     matches the command's contract).
  2. Every event the handler should have emitted was actually sent,
     in the documented order, with the documented fields.
  3. Malformed input produces an error envelope and ZERO events.

Why a separate file from ``test_routes_smoke.py``:
    The smoke tests were written for feat-028's request/response
    shape and use ``_round_trip`` which (since this PR) drains
    event frames silently. To assert on the actual emitted events
    we need a tighter helper that collects EVERY frame the daemon
    sends. Keeping that helper local to this file avoids
    cross-coupling.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

import websockets

from heddle_common import add_project
from heddle_daemon.server import (
    Daemon,
    DaemonConfig,
    JsonEnvelope,
    build_envelope,
    parse_envelope,
)


# ---------- helpers ----------


async def _collect_frames(
    daemon: Daemon,
    env: JsonEnvelope,
    *,
    timeout: float = 2.0,
) -> list[JsonEnvelope]:
    """Send one envelope; collect EVERY frame the daemon sends back.

    Unlike ``_round_trip`` (which drains events silently), this
    helper returns the full ordered list of envelopes the daemon
    emits. Tests assert on the order to prove "event before
    response".
    """
    url = f"ws://127.0.0.1:{daemon.bound_port}/ws"
    frames: list[JsonEnvelope] = []
    async with websockets.connect(url) as conn:
        await conn.send(env.to_json())
        # The first frame may be either an event or the response.
        # We loop until we see a frame whose ``req_id`` echoes the
        # request — that's the terminal response; everything before
        # it is events.
        while True:
            raw = await asyncio.wait_for(conn.recv(), timeout=timeout)
            received = parse_envelope(raw)
            frames.append(received)
            if received.extra.get("req_id") == env.extra.get("req_id"):
                return frames
            if len(frames) > 20:
                # Safety bound — if we ever see >20 frames per
                # command something is very wrong.
                raise AssertionError(
                    f"too many frames for {env.type!r}: {len(frames)}"
                )


# ---------- shared fixtures ----------


class _CommandTestBase(unittest.IsolatedAsyncioTestCase):
    """Common setup: a daemon bound to loopback, routes enabled, one
    project + one feature_list with a pending feature registered.

    feat-031: the daemon's per-feature handlers now resolve the
    feature's ``implementation_model`` against the LLM-config registry
    and build the chat model. To keep these tests hermetic (no
    network, no API keys), we enable ``HEDDLE_FAKE_LLM=1`` for the
    duration of the test and drop a single ``fixture_root/<fid>.json``
    next to each project. The daemon's fake-mode chokepoint returns a
    FakeLLM without ever touching ``build_chat_model``.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.projects_path = self.tmpdir / "projects.json"
        self.project_dir = self.tmpdir / "proj-cmd"
        self.project_dir.mkdir()

        # feat-031: enable fake-LLM mode for the duration of the test
        # by setting the env var on ``os.environ`` (the daemon reads it
        # via ``fake_llm_or_real``). Cleaned up in asyncTearDown.
        self._saved_env = os.environ.get("HEDDLE_FAKE_LLM")
        os.environ["HEDDLE_FAKE_LLM"] = "1"

        # Drop a minimal fake-LLM fixture so the daemon's
        # ``build_llm_for_feature`` finds a fixture to load. The
        # fixture content is irrelevant — the existing tests only
        # assert on event/response shape, not on LLM output.
        self.fixture_root = self.tmpdir / "fixtures"
        self.fixture_root.mkdir()
        self.feature_id = "feat-cmd-test"
        self._drop_fixture(self.feature_id)

        self.daemon = Daemon(
            DaemonConfig(port=0, fixture_root=self.fixture_root)
        )
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        # feat-031: also point the route handler's fixture_root at our
        # tmp dir so _resolve_fixture_path looks there.
        self.daemon._routes.fixture_root = self.fixture_root
        # Empty configs registry is OK — None implementation_model will
        # fall back to the first entry (which ``ensure_configs``
        # populates with the four default templates).
        self.daemon._routes.configs_path = None
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-cmd",
        )
        self.project_id = self.project.id

        # Drop a feature_list.json with one pending feature so the
        # start_feature / retry_feature / stop_feature handlers
        # have something to mutate.
        self._drop_feature_list(
            [
                {
                    "id": self.feature_id,
                    "category": "functional",
                    "description": "test feature for command handlers",
                    "steps": ["step one", "step two"],
                    "status": "pending",
                    "priority": "medium",
                    "depends_on": [],
                    "attempts": [],
                    "kind": "feature",
                    "fixes": None,
                    "enhances": None,
                    "superseded_by": None,
                    "implementation_model": None,
                }
            ]
        )

    async def asyncTearDown(self) -> None:
        # Restore the original HEDDLE_FAKE_LLM value (or delete the
        # key if it was unset). Avoids leakage to subsequent tests in
        # the same process.
        if self._saved_env is None:
            os.environ.pop("HEDDLE_FAKE_LLM", None)
        else:
            os.environ["HEDDLE_FAKE_LLM"] = self._saved_env

    def _drop_fixture(self, feature_id: str) -> None:
        """Write a minimal valid fake-LLM fixture for ``feature_id``.

        The fixture has a single scripted response so the
        ``FakeLLM`` constructor accepts it; the existing tests do
        not consume the response — they only assert on event/response
        wire shape.
        """
        fx = self.fixture_root / f"{feature_id}.json"
        fx.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "responses": [
                        {
                            "content": "stub",
                            "tool_calls": [],
                            "stop_reason": "end_turn",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _drop_feature_list(self, features: list[dict[str, Any]]) -> None:
        fl = {
            "schema_version": 1,
            "features": features,
            "metadata": {
                "total_features": len(features),
                "passing": 0,
                "failing": len(features),
                "in_progress": 0,
                "blocked": 0,
                "deferred": 0,
                "last_updated": "2026-09-12",
            },
        }
        p = self.project_dir / "feature_list.json"
        p.write_text(
            json.dumps(fl, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _find_event(
        self, frames: list[JsonEnvelope], event_name: str
    ) -> dict[str, Any] | None:
        for f in frames:
            if f.type == "event" and f.extra.get("event") == event_name:
                return {
                    "project_id": f.extra.get("project_id"),
                    "feature_id": f.extra.get("feature_id"),
                    "payload": f.extra.get("payload", {}),
                }
        return None


# ---------- start_feature ----------


class TestStartFeature(_CommandTestBase):
    async def test_start_feature_emits_event_then_response(self) -> None:
        env = build_envelope(
            "start_feature",
            req_id="s1",
            project_id=self.project_id,
            feature_id=self.feature_id,
        )
        frames = await _collect_frames(self.daemon, env)

        # Last frame is the response; the one before it (if any) is
        # the event.
        self.assertGreaterEqual(len(frames), 2, frames)
        resp = frames[-1]
        self.assertEqual(resp.type, "start_feature_response")
        self.assertEqual(resp.extra.get("req_id"), "s1")
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        data = resp.extra["data"]
        self.assertEqual(data["project_id"], self.project_id)
        self.assertEqual(data["feature_id"], self.feature_id)
        self.assertEqual(data["status"], "in_progress")
        self.assertTrue(data["attempt_id"])

        # At least one feature_attempt_started event, emitted BEFORE
        # the response. feat-031 adds a follow-up ``llm_resolved``
        # event right after, so we assert on the *first* event being
        # ``feature_attempt_started`` rather than on the event count.
        events = [
            f for f in frames[:-1] if f.type == "event"
        ]
        self.assertGreaterEqual(len(events), 1, frames)
        self.assertEqual(events[0].extra.get("event"), "feature_attempt_started")
        self.assertEqual(events[0].extra.get("project_id"), self.project_id)
        self.assertEqual(events[0].extra.get("feature_id"), self.feature_id)
        self.assertEqual(
            events[0].extra["payload"]["attempt_id"], data["attempt_id"]
        )

    async def test_start_feature_unknown_project_returns_not_found_no_event(
        self,
    ) -> None:
        env = build_envelope(
            "start_feature",
            req_id="s2",
            project_id="nope",
            feature_id=self.feature_id,
        )
        frames = await _collect_frames(self.daemon, env)
        # Only the response, no event.
        self.assertEqual(len(frames), 1)
        resp = frames[0]
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "not_found")

    async def test_start_feature_missing_project_id_rejected(self) -> None:
        env = build_envelope(
            "start_feature", req_id="s3", feature_id=self.feature_id
        )
        frames = await _collect_frames(self.daemon, env)
        self.assertEqual(len(frames), 1)
        resp = frames[0]
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")


# ---------- start_feature — feat-031 LLM resolution ----------


class TestStartFeatureEmitsLlmResolved(_CommandTestBase):
    """feat-031: ``start_feature`` emits ``llm_resolved`` after the
    ``feature_attempt_started`` event when the LLM-config registry
    resolves successfully.

    The base fixture (``_CommandTestBase``) leaves the feature's
    ``implementation_model`` as ``None``, which triggers the
    default-fallback path in ``llm_config.resolve_feature_llm_config``.
    The ``llm_resolved`` event therefore carries ``source="default"``
    and ``config_name`` equal to the first registry entry (the
    ``anthropic-claude-sonnet`` template seeded by
    ``ensure_configs``).
    """

    async def test_start_feature_emits_llm_resolved_after_attempt_started(
        self,
    ) -> None:
        env = build_envelope(
            "start_feature",
            req_id="lr1",
            project_id=self.project_id,
            feature_id=self.feature_id,
        )
        frames = await _collect_frames(self.daemon, env)
        resp = frames[-1]
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        data = resp.extra["data"]
        attempt_id = data["attempt_id"]
        # The response echoes the resolved config (feat-031 addition).
        self.assertTrue(data["config_name"])
        self.assertEqual(data["config_source"], "default")

        events = [f for f in frames[:-1] if f.type == "event"]
        # Order: feature_attempt_started, llm_resolved.
        event_names = [f.extra.get("event") for f in events]
        self.assertEqual(
            event_names,
            ["feature_attempt_started", "llm_resolved"],
            events,
        )
        llm_event = events[1]
        self.assertEqual(llm_event.extra.get("project_id"), self.project_id)
        self.assertEqual(llm_event.extra.get("feature_id"), self.feature_id)
        payload = llm_event.extra["payload"]
        self.assertEqual(payload["attempt_id"], attempt_id)
        self.assertEqual(payload["config_name"], data["config_name"])
        self.assertEqual(payload["source"], "default")

    async def test_start_feature_with_explicit_implementation_model(self) -> None:
        """When the feature lists an explicit ``implementation_model``,
        ``source`` is ``"explicit"`` and the config_name matches."""
        fl_path = self.project_dir / "feature_list.json"
        data = json.loads(fl_path.read_text(encoding="utf-8"))
        for f in data["features"]:
            if f["id"] == self.feature_id:
                f["implementation_model"] = "anthropic-claude-sonnet"
        fl_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        env = build_envelope(
            "start_feature",
            req_id="lr2",
            project_id=self.project_id,
            feature_id=self.feature_id,
        )
        frames = await _collect_frames(self.daemon, env)
        resp = frames[-1]
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        data = resp.extra["data"]
        self.assertEqual(data["config_name"], "anthropic-claude-sonnet")
        self.assertEqual(data["config_source"], "explicit")

    async def test_start_feature_with_unknown_implementation_model_returns_error(
        self,
    ) -> None:
        """Unknown implementation_model → ``ok: false`` envelope with
        code ``llm_config_error`` and NO ``llm_resolved`` event."""
        fl_path = self.project_dir / "feature_list.json"
        data = json.loads(fl_path.read_text(encoding="utf-8"))
        for f in data["features"]:
            if f["id"] == self.feature_id:
                f["implementation_model"] = "does-not-exist"
        fl_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        env = build_envelope(
            "start_feature",
            req_id="lr3",
            project_id=self.project_id,
            feature_id=self.feature_id,
        )
        frames = await _collect_frames(self.daemon, env)
        resp = frames[-1]
        self.assertEqual(resp.type, "start_feature_response")
        self.assertFalse(resp.extra.get("ok"), resp.extra)
        self.assertEqual(
            resp.extra["error"]["code"], "llm_config_error"
        )

        events = [f for f in frames[:-1] if f.type == "event"]
        # feature_attempt_started IS emitted (the user saw the attempt
        # begin); llm_resolved is NOT (the config check refused before
        # resolution completed).
        event_names = [f.extra.get("event") for f in events]
        self.assertEqual(event_names, ["feature_attempt_started"], events)


# ---------- stop_feature ----------


class TestStopFeature(_CommandTestBase):
    async def test_stop_feature_emits_event_then_response(self) -> None:
        env = build_envelope(
            "stop_feature",
            req_id="x1",
            project_id=self.project_id,
            feature_id=self.feature_id,
            attempt_id="a-stop-1",
        )
        frames = await _collect_frames(self.daemon, env)

        self.assertGreaterEqual(len(frames), 2)
        resp = frames[-1]
        self.assertEqual(resp.type, "stop_feature_response")
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        self.assertEqual(
            resp.extra["data"]["attempt_id"], "a-stop-1"
        )

        events = [f for f in frames[:-1] if f.type == "event"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].extra.get("event"), "feature_stopped")
        self.assertEqual(events[0].extra["payload"]["attempt_id"], "a-stop-1")

    async def test_stop_feature_min_attempt_id_when_not_provided(self) -> None:
        env = build_envelope(
            "stop_feature",
            req_id="x2",
            project_id=self.project_id,
            feature_id=self.feature_id,
        )
        frames = await _collect_frames(self.daemon, env)
        resp = frames[-1]
        self.assertTrue(resp.extra.get("ok"))
        # Mints an attempt_id so the browser can correlate the
        # response with the emitted event.
        self.assertTrue(resp.extra["data"]["attempt_id"])


# ---------- retry_feature ----------


class TestRetryFeature(_CommandTestBase):
    async def test_retry_feature_emits_event_then_response(self) -> None:
        env = build_envelope(
            "retry_feature",
            req_id="r1",
            project_id=self.project_id,
            feature_id=self.feature_id,
        )
        frames = await _collect_frames(self.daemon, env)

        self.assertGreaterEqual(len(frames), 2)
        resp = frames[-1]
        self.assertEqual(resp.type, "retry_feature_response")
        self.assertTrue(resp.extra.get("ok"))
        self.assertEqual(resp.extra["data"]["action"], "retry")
        self.assertEqual(resp.extra["data"]["status"], "in_progress")

        # feat-031: at least one feature_attempt_started event; a
        # follow-up ``llm_resolved`` event is also expected (see the
        # sibling TestStartFeature / TestStartFeatureEmitsLlmResolved).
        events = [f for f in frames[:-1] if f.type == "event"]
        self.assertGreaterEqual(len(events), 1, frames)
        self.assertEqual(events[0].extra.get("event"), "feature_attempt_started")


# ---------- dialog_turn emits dialog_done ----------


class TestDialogTurnEvent(_CommandTestBase):
    async def test_dialog_turn_emits_dialog_done_event(self) -> None:
        env = build_envelope(
            "dialog_turn",
            req_id="d1",
            project_id=self.project_id,
            message="hello",
        )
        frames = await _collect_frames(self.daemon, env)

        resp = frames[-1]
        self.assertEqual(resp.type, "dialog_turn_response")
        self.assertTrue(resp.extra.get("ok"), resp.extra)

        events = [f for f in frames[:-1] if f.type == "event"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].extra.get("event"), "dialog_done")
        self.assertEqual(events[0].extra.get("project_id"), self.project_id)
        # feat-044: dialog_done payload carries the friendly chat
        # text and the classified intent ("chat" for a plain greeting).
        self.assertIn("add or change", events[0].extra["payload"]["full_text"])
        self.assertEqual(events[0].extra["payload"]["intent"], "chat")
