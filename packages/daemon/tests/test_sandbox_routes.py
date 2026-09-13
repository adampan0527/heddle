# SPDX-License-Identifier: Apache-2.0
"""Wiring tests for feat-055 sandbox-level endpoints on the daemon.

Exercises ``project_sandbox_get`` and ``project_sandbox_set`` through
the real daemon envelope loop. The unit tests for the underlying
``sandbox_io`` module live in ``test_sandbox_io.py``; this file
covers the envelope-shape contract the Node.js side relies on.
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


async def _round_trip(
    daemon: Daemon, env: JsonEnvelope, *, timeout: float = 2.0
) -> JsonEnvelope:
    url = f"ws://127.0.0.1:{daemon.bound_port}/ws"
    async with websockets.connect(url) as conn:
        await conn.send(env.to_json())
        raw = await asyncio.wait_for(conn.recv(), timeout=timeout)
        return parse_envelope(raw)


class TestSandboxRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.projects_path = self.tmpdir / "projects.json"
        self.project_dir = self.tmpdir / "proj-sb"
        self.project_dir.mkdir()

        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-sb",
        )
        self.project_id = self.project.id

    async def test_sandbox_get_returns_default_when_no_config(self) -> None:
        env = build_envelope(
            "project_sandbox_get",
            req_id="sg1",
            project_id=self.project_id,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertEqual(resp.type, "project_sandbox_get_response")
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        self.assertEqual(resp.extra["data"]["sandbox_level"], "full")

    async def test_sandbox_set_persists_to_yaml(self) -> None:
        env = build_envelope(
            "project_sandbox_set",
            req_id="ss1",
            project_id=self.project_id,
            sandbox_level="edit-with-confirm",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        self.assertEqual(
            resp.extra["data"]["sandbox_level"], "edit-with-confirm"
        )

        # The file is on disk; round-trip via a follow-up GET.
        config_path = self.project_dir / ".heddle" / "config.yaml"
        self.assertTrue(config_path.exists())
        raw: dict[str, Any] = json.loads(
            config_path.read_text(encoding="utf-8").replace("'", '"')
        ) if False else {}
        # YAML is fine to load; reuse the Python parser.
        import yaml

        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["sandbox_level"], "edit-with-confirm")

        env2 = build_envelope(
            "project_sandbox_get",
            req_id="sg2",
            project_id=self.project_id,
        )
        resp2 = await _round_trip(self.daemon, env2)
        self.assertEqual(
            resp2.extra["data"]["sandbox_level"], "edit-with-confirm"
        )

    async def test_sandbox_set_invalid_level_returns_invalid_input(self) -> None:
        env = build_envelope(
            "project_sandbox_set",
            req_id="ss2",
            project_id=self.project_id,
            sandbox_level="wild-west",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")

    async def test_sandbox_set_unknown_project_returns_not_found(self) -> None:
        env = build_envelope(
            "project_sandbox_set",
            req_id="ss3",
            project_id="does-not-exist",
            sandbox_level="read-only",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "not_found")

    async def test_sandbox_get_unknown_project_returns_not_found(self) -> None:
        env = build_envelope(
            "project_sandbox_get",
            req_id="sg3",
            project_id="missing",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "not_found")

    async def test_sandbox_set_missing_level_returns_invalid_input(self) -> None:
        env = build_envelope(
            "project_sandbox_set",
            req_id="ss4",
            project_id=self.project_id,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")

    async def test_project_list_includes_sandbox_level(self) -> None:
        # Set first to non-default, then list and confirm the level
        # rides along on the project row.
        env = build_envelope(
            "project_sandbox_set",
            req_id="ss5",
            project_id=self.project_id,
            sandbox_level="read-only",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"))

        env2 = build_envelope("project_list", req_id="pl1")
        resp2 = await _round_trip(self.daemon, env2)
        self.assertTrue(resp2.extra.get("ok"))
        rows = resp2.extra["data"]["projects"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sandbox_level"], "read-only")


if __name__ == "__main__":
    unittest.main()
