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
    project + one feature_list with a pending feature registered."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.projects_path = self.tmpdir / "projects.json"
        self.project_dir = self.tmpdir / "proj-cmd"
        self.project_dir.mkdir()

        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
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
        self.feature_id = "feat-cmd-test"
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

        # Exactly one feature_attempt_started event, emitted BEFORE
        # the response. The event must carry the same attempt_id the
        # response echoes back.
        events = [
            f for f in frames[:-1] if f.type == "event"
        ]
        self.assertEqual(len(events), 1, frames)
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

        events = [f for f in frames[:-1] if f.type == "event"]
        self.assertEqual(len(events), 1)
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
        # Stub payload mirrors the chat echo.
        self.assertEqual(
            events[0].extra["payload"]["full_text"], "echo: hello"
        )
