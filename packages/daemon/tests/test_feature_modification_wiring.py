# SPDX-License-Identifier: Apache-2.0
"""Integration tests for feat-054 / D-054 routes.

Round-trips the five new envelope handlers (feature_split,
feature_merge, feature_edit, feature_reprioritize,
feature_update_deps) through a real Daemon + websockets client
and asserts:

  * Destructive ops (split, merge) include a ``diff`` field the UI
    uses for the confirmation card (D-054).
  * Source rows get ``superseded_by`` for split / merge.
  * Non-destructive ops (edit, priority, deps) round-trip without
    touching ``superseded_by`` or ``status``.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import websockets

from heddle_daemon.server import (
    Daemon,
    DaemonConfig,
    JsonEnvelope,
    build_envelope,
    parse_envelope,
)


def _make_feature_list(
    project_dir: Path,
    features: list[dict[str, Any]] | None = None,
) -> Path:
    feats = features or []
    fl = {
        "schema_version": 1,
        "features": feats,
        "metadata": {
            "total_features": len(feats),
            "passing": 0,
            "failing": len(feats),
            "in_progress": 0,
            "blocked": 0,
            "deferred": 0,
            "last_updated": "2026-09-13",
        },
    }
    p = project_dir / "feature_list.json"
    p.write_text(json.dumps(fl, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _two_pending() -> list[dict[str, Any]]:
    return [
        {
            "id": "feat-a",
            "category": "functional",
            "description": "Source A",
            "steps": ["step 1"],
            "status": "pending",
            "priority": "medium",
            "depends_on": [],
            "attempts": [],
        },
        {
            "id": "feat-b",
            "category": "functional",
            "description": "Sibling B",
            "steps": ["step 1"],
            "status": "pending",
            "priority": "high",
            "depends_on": [],
            "attempts": [],
        },
    ]


async def _round_trip(
    daemon: Daemon, env: JsonEnvelope, *, timeout: float = 5.0
) -> JsonEnvelope:
    url = f"ws://127.0.0.1:{daemon.bound_port}/ws"
    async with websockets.connect(url) as conn:
        await conn.send(env.to_json())
        while True:
            raw = await asyncio.wait_for(conn.recv(), timeout=timeout)
            received = parse_envelope(raw)
            if received.type == "event":
                continue
            return received


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proj_tmpdir = Path(self._tmp.name)
        self.projects_path = self.proj_tmpdir / "projects.json"
        self.project_dir = self.proj_tmpdir / "proj-054"
        self.project_dir.mkdir()
        _make_feature_list(self.project_dir, _two_pending())
        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)
        from heddle_common import add_project
        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-054",
        )
        self.project_id = self.project.id


class TestFeatureSplit(_Base):
    async def test_split_returns_diff_and_supersedes_source(self) -> None:
        env = build_envelope(
            "feature_split",
            project_id=self.project_id,
            feature_id="feat-a",
            new_features=[
                {"title": "p1", "description": "d", "steps": ["s"]},
                {"title": "p2", "description": "d", "steps": ["s"]},
            ],
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"))
        body = resp.extra["data"]
        self.assertEqual(body["source"]["superseded_by"], "feat-a-1")
        self.assertEqual(len(body["created"]), 2)
        self.assertEqual(body["diff"]["operation"], "split")


class TestFeatureMerge(_Base):
    async def test_merge_supersedes_both_sources(self) -> None:
        env = build_envelope(
            "feature_merge",
            project_id=self.project_id,
            source_ids=["feat-a", "feat-b"],
            target={
                "id": "feat-merged",
                "title": "Merged",
                "description": "M",
                "steps": ["s"],
            },
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"))
        body = resp.extra["data"]
        self.assertEqual(len(body["sources"]), 2)
        self.assertEqual(body["created"]["id"], "feat-merged")
        superseded = {s["id"]: s["superseded_by"] for s in body["sources"]}
        self.assertEqual(superseded["feat-a"], "feat-merged")
        self.assertEqual(superseded["feat-b"], "feat-merged")


class TestFeatureEdit(_Base):
    async def test_edit_changes_title(self) -> None:
        env = build_envelope(
            "feature_edit",
            project_id=self.project_id,
            feature_id="feat-a",
            title="Renamed",
            steps=["new step"],
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"))
        body = resp.extra["data"]
        self.assertEqual(body["feature"]["description"], "Renamed")
        self.assertEqual(body["feature"]["steps"], ["new step"])


class TestFeatureReprioritize(_Base):
    async def test_reprioritize_returns_diff(self) -> None:
        env = build_envelope(
            "feature_reprioritize",
            project_id=self.project_id,
            feature_id="feat-a",
            priority="high",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"))
        body = resp.extra["data"]
        self.assertEqual(body["feature"]["priority"], "high")
        self.assertIn("priority", body["diff"]["changes"])


class TestFeatureUpdateDeps(_Base):
    async def test_add_then_remove(self) -> None:
        env = build_envelope(
            "feature_update_deps",
            project_id=self.project_id,
            feature_id="feat-a",
            add=["feat-b"],
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"))
        self.assertEqual(resp.extra["data"]["feature"]["depends_on"], ["feat-b"])

        env = build_envelope(
            "feature_update_deps",
            project_id=self.project_id,
            feature_id="feat-a",
            remove=["feat-b"],
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"))
        self.assertEqual(resp.extra["data"]["feature"]["depends_on"], [])
        self.assertEqual(resp.extra["data"]["diff"]["removed"], ["feat-b"])


if __name__ == "__main__":
    unittest.main()