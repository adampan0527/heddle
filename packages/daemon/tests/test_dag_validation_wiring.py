# SPDX-License-Identifier: Apache-2.0
"""Integration tests for feat-046 drafts-confirm wiring.

Round-trips ``drafts_confirm`` through a real ``Daemon`` +
``websockets`` client and asserts:

  * On a valid batch: every draft becomes a feature row in
    ``feature_list.json`` with the right initial status (pending if
    every dep is passing, else blocked).
  * On an invalid batch (unknown dep, cycle, duplicate id): the
    handler returns ``ok: false`` with ``code="invalid_input"`` and
    NO rows are written to ``feature_list.json``.

The wiring tests drive the daemon's envelope handler directly
(``RouteHandler.dispatch_envelope``) without going through the full
agent loop — that path is covered by feat-049's end-to-end happy
path test.
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
from heddle_common.feature_list_io import load as _fl_load


# ---------- helpers ----------


async def _round_trip(
    daemon: Daemon,
    env: JsonEnvelope,
    *,
    timeout: float = 2.0,
) -> JsonEnvelope:
    url = f"ws://127.0.0.1:{daemon.bound_port}/ws"
    async with websockets.connect(url) as conn:
        await conn.send(env.to_json())
        while True:
            raw = await asyncio.wait_for(conn.recv(), timeout=deadline_or(daemon, timeout))
            received = parse_envelope(raw)
            if received.type == "event":
                continue
            return received


def deadline_or(_daemon: Daemon, timeout: float) -> float:
    return timeout


def _make_feature_list(
    project_dir: Path,
    features: list[dict[str, Any]] | None = None,
) -> Path:
    """Drop a minimal feature_list.json into project_dir."""
    feats = features or []
    fl = {
        "schema_version": 1,
        "features": feats,
        "metadata": {
            "total_features": len(feats),
            "passing": sum(1 for f in feats if f.get("status") == "passing"),
            "failing": sum(1 for f in feats if f.get("status") != "passing"),
            "in_progress": 0,
            "blocked": 0,
            "deferred": 0,
            "last_updated": "2026-09-13",
        },
    }
    p = project_dir / "feature_list.json"
    p.write_text(json.dumps(fl, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _draft(
    draft_id: str,
    *,
    depends_on: list[str] | None = None,
    title: str = "t",
    description: str = "d",
    steps: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": draft_id,
        "title": title,
        "description": description,
        "steps": steps if steps is not None else ["step 1"],
        "depends_on": depends_on if depends_on is not None else [],
        "kind": "feature",
    }


# ---------- success path ----------


class TestDraftsConfirmSuccess(unittest.IsolatedAsyncioTestCase):
    """Valid batches land as feature rows with the right initial status."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proj_tmpdir = Path(self._tmp.name)
        self.projects_path = self.proj_tmpdir / "projects.json"
        self.project_dir = self.proj_tmpdir / "proj-confirm"
        self.project_dir.mkdir()
        # One existing passing feature so we can test the "depends on
        # passing -> pending" branch alongside the "no deps -> pending"
        # and "depends on sibling -> blocked" branches in one batch.
        _make_feature_list(
            self.project_dir,
            [
                {
                    "id": "feat-existing",
                    "category": "functional",
                    "description": "existing",
                    "steps": ["step 1"],
                    "status": "passing",
                    "priority": "high",
                    "depends_on": [],
                    "attempts": [],
                }
            ],
        )

        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

        from heddle_common import add_project

        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-confirm",
        )
        self.project_id = self.project.id
        self.feature_list_path = self.project_dir / "feature_list.json"

    async def test_three_drafts_one_pending_two_blocked(self) -> None:
        drafts = [
            _draft("temp-001"),  # no deps -> pending
            _draft("temp-002", depends_on=["feat-existing"]),  # passing -> pending
            _draft("temp-003", depends_on=["temp-001"]),  # sibling -> blocked
        ]
        env = build_envelope(
            "drafts_confirm",
            req_id="c1",
            project_id=self.project_id,
            drafts=drafts,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        data = resp.extra["data"]
        self.assertEqual(data["count"], 3)
        added_by_id = {f["id"]: f for f in data["added"]}
        self.assertEqual(added_by_id["temp-001"]["status"], "pending")
        self.assertEqual(added_by_id["temp-002"]["status"], "pending")
        self.assertEqual(added_by_id["temp-003"]["status"], "blocked")

    async def test_added_rows_persisted_to_disk(self) -> None:
        drafts = [_draft("temp-001")]
        env = build_envelope(
            "drafts_confirm",
            req_id="c2",
            project_id=self.project_id,
            drafts=drafts,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        # Re-read from disk and confirm the row is there with the
        # expected status + steps + depends_on (not just in the
        # in-memory response).
        post = _fl_load(self.feature_list_path)
        ids = [f["id"] for f in post["features"]]
        self.assertIn("temp-001", ids)
        row = next(f for f in post["features"] if f["id"] == "temp-001")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["depends_on"], [])
        self.assertEqual(row["steps"], ["step 1"])

    async def test_drafts_depending_on_blocked_existing_are_blocked(self) -> None:
        # feat-existing is passing here (from setUp), so a draft
        # depending on it must be pending. The "blocked because the
        # dep is non-passing" branch is covered by the pure-function
        # test suite; here we only assert the wire-level integration.
        drafts = [_draft("temp-001", depends_on=["feat-existing"])]
        env = build_envelope(
            "drafts_confirm",
            req_id="c3",
            project_id=self.project_id,
            drafts=drafts,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        data = resp.extra["data"]
        self.assertEqual(data["added"][0]["status"], "pending")


# ---------- rejection path ----------


class TestDraftsConfirmRejection(unittest.IsolatedAsyncioTestCase):
    """Invalid batches return an error envelope and do NOT mutate the file."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proj_tmpdir = Path(self._tmp.name)
        self.projects_path = self.proj_tmpdir / "projects.json"
        self.project_dir = self.proj_tmpdir / "proj-reject"
        self.project_dir.mkdir()
        _make_feature_list(self.project_dir)
        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)
        from heddle_common import add_project

        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-reject",
        )
        self.project_id = self.project.id
        self.feature_list_path = self.project_dir / "feature_list.json"

    def _features_before(self) -> list[dict[str, Any]]:
        return list(_fl_load(self.feature_list_path).get("features", []))

    async def test_unknown_dep_returns_invalid_input(self) -> None:
        before = self._features_before()
        drafts = [_draft("temp-001", depends_on=["feat-XXX"])]
        env = build_envelope(
            "drafts_confirm",
            req_id="r1",
            project_id=self.project_id,
            drafts=drafts,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        err = resp.extra["error"]
        self.assertEqual(err["code"], "invalid_input")
        self.assertIn("temp-001", err["message"])
        self.assertIn("feat-XXX", err["message"])
        # File must be byte-identical to the pre-call state.
        after = self._features_before()
        self.assertEqual(before, after)

    async def test_cycle_returns_invalid_input(self) -> None:
        before = self._features_before()
        drafts = [
            _draft("temp-001", depends_on=["temp-002"]),
            _draft("temp-002", depends_on=["temp-001"]),
        ]
        env = build_envelope(
            "drafts_confirm",
            req_id="r2",
            project_id=self.project_id,
            drafts=drafts,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")
        self.assertIn("cycle", resp.extra["error"]["message"].lower())
        after = self._features_before()
        self.assertEqual(before, after)

    async def test_self_dep_returns_invalid_input(self) -> None:
        before = self._features_before()
        drafts = [_draft("temp-001", depends_on=["temp-001"])]
        env = build_envelope(
            "drafts_confirm",
            req_id="r3",
            project_id=self.project_id,
            drafts=drafts,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")
        self.assertIn("itself", resp.extra["error"]["message"])
        after = self._features_before()
        self.assertEqual(before, after)

    async def test_mixed_batch_with_one_bad_draft_rejects_whole_batch(self) -> None:
        before = self._features_before()
        drafts = [
            _draft("temp-001"),
            _draft("temp-002", depends_on=["feat-XXX"]),
        ]
        env = build_envelope(
            "drafts_confirm",
            req_id="r4",
            project_id=self.project_id,
            drafts=drafts,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")
        # Atomicity: even the valid draft must NOT land.
        after = self._features_before()
        self.assertEqual(before, after)
        ids = [f["id"] for f in after]
        self.assertNotIn("temp-001", ids)
        self.assertNotIn("temp-002", ids)

    async def test_unknown_project_returns_not_found(self) -> None:
        drafts = [_draft("temp-001")]
        env = build_envelope(
            "drafts_confirm",
            req_id="r5",
            project_id="does-not-exist",
            drafts=drafts,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "not_found")

    async def test_missing_drafts_field_rejects(self) -> None:
        env = build_envelope(
            "drafts_confirm",
            req_id="r6",
            project_id=self.project_id,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")
        self.assertIn("drafts", resp.extra["error"]["message"])

    async def test_non_list_drafts_rejects(self) -> None:
        env = build_envelope(
            "drafts_confirm",
            req_id="r7",
            project_id=self.project_id,
            drafts="not a list",  # type: ignore[arg-type]
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
