# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for feat-028 daemon-side envelope handlers.

Exercises the six business commands (``project_list`` / ``project_add``
/ ``project_remove`` / ``feature_list`` / ``feature_transition`` /
``dialog_turn``) end-to-end through a real ``Daemon`` + ``websockets``
client, in process. The point is to catch drift between the daemon
handler's wire shape and what the Node.js supervisor / HTTP routes
will produce — the wire shape is the contract that ties feat-028 to
feat-029/030.

What this test does NOT cover:
  - The Node.js side. That's the ``packages/node/tests/`` suite.
  - LLM-backed dialog (feat-044). v0.1 dialog_turn is the chat stub.
  - The full cascade including the daemon hook (covered by feat-014's
    own test_server_project_removed.py).

We use a temp ``projects.json`` so the test never touches
``~/.heddle/`` (the real registry); same for ``feature_list.json``
per-project (a tmp directory).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import websockets

from heddle_daemon.server import (
    Daemon,
    DaemonConfig,
    JsonEnvelope,
    build_envelope,
    parse_envelope,
)
from heddle_daemon.routes import (
    ALLOWED_TRANSITION_ACTIONS,
    MAX_DIALOG_MESSAGE_CHARS,
    RouteHandler,
    RoutesError,
)


# ---------- helpers ----------


async def _round_trip(
    daemon: Daemon,
    env: JsonEnvelope,
    *,
    timeout: float = 2.0,
) -> JsonEnvelope:
    """Send one envelope; wait for the matching response; parse it."""
    url = f"ws://127.0.0.1:{daemon.bound_port}/ws"
    async with websockets.connect(url) as conn:
        await conn.send(env.to_json())
        raw = await asyncio.wait_for(conn.recv(), timeout=timeout)
        return parse_envelope(raw)


def _make_feature_list(project_dir: Path, features: list[dict[str, Any]]) -> Path:
    """Drop a minimal feature_list.json with one feature into project_dir."""
    fl = {
        "schema_version": 1,
        "features": features,
        "metadata": {
            "total_features": len(features),
            "passing": sum(1 for f in features if f.get("status") == "passing"),
            "failing": sum(1 for f in features if f.get("status") != "passing"),
            "in_progress": 0,
            "blocked": 0,
            "deferred": 0,
            "last_updated": "2026-09-12",
        },
    }
    p = project_dir / "feature_list.json"
    p.write_text(json.dumps(fl, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _make_feature(feature_id: str, status: str = "pending") -> dict[str, Any]:
    """Minimal well-formed feature row for ``_make_feature_list``."""
    return {
        "id": feature_id,
        "category": "functional",
        "description": f"test feature {feature_id}",
        "steps": [
            f"Step 1: validate {feature_id}",
            f"Step 2: confirm {feature_id}",
        ],
        "status": status,
        "priority": "medium",
        "depends_on": [],
        "attempts": [],
        "kind": "feature",
        "fixes": None,
        "enhances": None,
        "superseded_by": None,
        "implementation_model": None,
    }


# ---------- tests ----------


class TestProjectHandlers(unittest.IsolatedAsyncioTestCase):
    """Round-trip project_list / project_add / project_remove."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.projects_path = self.tmpdir / "projects.json"

        # Build a daemon with routes wired, projects.json redirected
        # to the temp file. We don't pass a custom MessageHandler, so
        # the skeleton's echo fallback is still in place for
        # unhandled envelope types — no regression risk for any test
        # that talks to the daemon but doesn't use one of the six
        # business commands.
        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        # Redirect the handler's projects.json to our tmp file.
        self.daemon._routes.projects_path = self.projects_path
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

    async def test_project_list_empty(self) -> None:
        env = build_envelope("project_list", req_id="r1")
        resp = await _round_trip(self.daemon, env)
        self.assertEqual(resp.type, "project_list_response")
        self.assertEqual(resp.extra.get("req_id"), "r1")
        self.assertTrue(resp.extra.get("ok"))
        self.assertEqual(resp.extra["data"]["projects"], [])

    async def test_project_add_then_list(self) -> None:
        project_dir = self.tmpdir / "proj-a"
        project_dir.mkdir()
        env = build_envelope(
            "project_add",
            req_id="r2",
            path=str(project_dir),
            name="proj-a",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertEqual(resp.type, "project_add_response")
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        proj = resp.extra["data"]["project"]
        self.assertEqual(proj["name"], "proj-a")
        self.assertTrue(proj["id"])  # uuid4 non-empty

        # Now project_list returns that one entry.
        env = build_envelope("project_list", req_id="r3")
        resp = await _round_trip(self.daemon, env)
        self.assertEqual(len(resp.extra["data"]["projects"]), 1)

    async def test_project_add_duplicate_path_returns_conflict(self) -> None:
        project_dir = self.tmpdir / "dup"
        project_dir.mkdir()
        env1 = build_envelope(
            "project_add", req_id="r4", path=str(project_dir)
        )
        resp1 = await _round_trip(self.daemon, env1)
        self.assertTrue(resp1.extra.get("ok"))

        env2 = build_envelope(
            "project_add", req_id="r5", path=str(project_dir)
        )
        resp2 = await _round_trip(self.daemon, env2)
        self.assertFalse(resp2.extra.get("ok"))
        self.assertEqual(resp2.extra["error"]["code"], "conflict")

    async def test_project_add_missing_path_returns_invalid_input(self) -> None:
        env = build_envelope("project_add", req_id="r6", path="")
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")

    async def test_project_remove_unknown_returns_not_found(self) -> None:
        env = build_envelope(
            "project_remove", req_id="r7", project_id="does-not-exist"
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "not_found")


class TestFeatureHandlers(unittest.IsolatedAsyncioTestCase):
    """Round-trip feature_list / feature_transition with a tmp project."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.projects_path = self.tmpdir / "projects.json"
        self.project_dir = self.tmpdir / "proj-x"
        self.project_dir.mkdir()
        self.feature_list_path = _make_feature_list(
            self.project_dir,
            [_make_feature("feat-A", "pending"),
             _make_feature("feat-B", "in_progress"),
             _make_feature("feat-C", "passing")],
        )

        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        # Default resolver is ``<project.path>/feature_list.json``,
        # which matches our tmp layout.
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

        # Register the project so project_id lookups succeed.
        from heddle_common import add_project

        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-x",
        )
        self.project_id = self.project.id

    async def test_feature_list_returns_three_rows(self) -> None:
        env = build_envelope(
            "feature_list", req_id="fl1", project_id=self.project_id
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        features = resp.extra["data"]["features"]
        self.assertEqual(len(features), 3)
        ids = {f["id"] for f in features}
        self.assertEqual(ids, {"feat-A", "feat-B", "feat-C"})

    async def test_feature_list_unknown_project_returns_not_found(self) -> None:
        env = build_envelope(
            "feature_list", req_id="fl2", project_id="nope"
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "not_found")

    async def test_feature_transition_retry_moves_pending_to_in_progress(
        self,
    ) -> None:
        env = build_envelope(
            "feature_transition",
            req_id="ft1",
            project_id=self.project_id,
            feature_id="feat-A",
            action="retry",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        self.assertEqual(resp.extra["data"]["feature"]["status"], "in_progress")

    async def test_feature_transition_abandon_blocks(self) -> None:
        env = build_envelope(
            "feature_transition",
            req_id="ft2",
            project_id=self.project_id,
            feature_id="feat-A",
            action="abandon",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        self.assertEqual(resp.extra["data"]["feature"]["status"], "blocked")
        self.assertIn(
            "abandoned by user",
            resp.extra["data"]["feature"]["blocked_reason"],
        )

    async def test_feature_transition_mark_done_passes(self) -> None:
        env = build_envelope(
            "feature_transition",
            req_id="ft3",
            project_id=self.project_id,
            feature_id="feat-B",
            action="mark-done",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        self.assertEqual(resp.extra["data"]["feature"]["status"], "passing")

    async def test_feature_transition_invalid_action_rejected(self) -> None:
        env = build_envelope(
            "feature_transition",
            req_id="ft4",
            project_id=self.project_id,
            feature_id="feat-A",
            action="nuke",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")
        self.assertIn("nuke", resp.extra["error"]["message"])

    async def test_feature_transition_unknown_feature_rejected(self) -> None:
        env = build_envelope(
            "feature_transition",
            req_id="ft5",
            project_id=self.project_id,
            feature_id="feat-ZZ",
            action="retry",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        # ``fail()`` inside mark_in_progress writes "not found" → maps
        # to not_found via _classify_fail_message.
        self.assertEqual(resp.extra["error"]["code"], "not_found")


class TestDialogStub(unittest.IsolatedAsyncioTestCase):
    """dialog_turn is the v0.1 chat stub; feat-044 replaces it."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.projects_path = self.tmpdir / "projects.json"
        self.project_dir = self.tmpdir / "proj-d"
        self.project_dir.mkdir()

        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

        from heddle_common import add_project

        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-d",
        )
        self.project_id = self.project.id

    async def test_dialog_turn_returns_chat_echo(self) -> None:
        env = build_envelope(
            "dialog_turn",
            req_id="d1",
            project_id=self.project_id,
            message="hello world",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        data = resp.extra["data"]
        self.assertEqual(data["kind"], "chat")
        self.assertEqual(data["text"], "echo: hello world")

    async def test_dialog_turn_empty_message_rejected(self) -> None:
        env = build_envelope(
            "dialog_turn",
            req_id="d2",
            project_id=self.project_id,
            message="",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")

    async def test_dialog_turn_oversize_message_rejected(self) -> None:
        env = build_envelope(
            "dialog_turn",
            req_id="d3",
            project_id=self.project_id,
            message="x" * (MAX_DIALOG_MESSAGE_CHARS + 1),
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")
        self.assertIn(
            str(MAX_DIALOG_MESSAGE_CHARS),
            resp.extra["error"]["message"],
        )

    async def test_dialog_turn_unknown_project_returns_not_found(self) -> None:
        env = build_envelope(
            "dialog_turn",
            req_id="d4",
            project_id="nope",
            message="hi",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "not_found")


class TestEchoFallback(unittest.IsolatedAsyncioTestCase):
    """Un-recognised envelope types still go through ``self._handler``.

    The skeleton's echo behaviour must remain so older / future test
    paths (and any pre-feat-028 client) keep working byte-for-byte.
    """

    async def asyncSetUp(self) -> None:
        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

    async def test_unknown_type_returns_echo(self) -> None:
        env = build_envelope("unhandled_type_xyz", req_id="u1", foo="bar")
        resp = await _round_trip(self.daemon, env)
        self.assertEqual(resp.type, "echo")
        self.assertEqual(resp.extra.get("original_type"), "unhandled_type_xyz")
        self.assertEqual(resp.extra.get("original_v"), 1)


class TestUnitDispatch(unittest.TestCase):
    """Unit tests against ``RouteHandler.dispatch_envelope`` directly.

    No socket — the handler is async because of ``project_remove``,
    so we drive it via ``asyncio.run``. Covers branches the round-trip
    suite can't easily exercise (e.g. handler-level exception path).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.projects_path = self.tmpdir / "projects.json"

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def test_internal_error_maps_to_internal_error_code(self) -> None:
        # Make the underlying add_project raise an unexpected
        # exception to exercise the broad ``except Exception`` branch.
        handler = RouteHandler(projects_path=self.projects_path)
        env = build_envelope(
            "project_add", req_id="ix1", path=str(self.tmpdir / "real")
        )
        (self.tmpdir / "real").mkdir()
        with patch(
            "heddle_daemon.routes.add_project",
            side_effect=RuntimeError("boom"),
        ):
            resp = self._run(handler.dispatch_envelope(env))
        self.assertIsNotNone(resp)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "internal_error")
        # The original message is hidden so internal exception text
        # cannot leak across the WS boundary.
        self.assertNotIn("boom", resp.extra["error"]["message"])

    def test_unhandled_type_returns_none(self) -> None:
        handler = RouteHandler(projects_path=self.projects_path)
        env = build_envelope("definitely_not_a_command", req_id="ix2")
        resp = self._run(handler.dispatch_envelope(env))
        self.assertIsNone(resp)


if __name__ == "__main__":
    unittest.main()