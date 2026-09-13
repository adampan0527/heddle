# SPDX-License-Identifier: Apache-2.0
"""Integration tests for feat-045 dialog-decomposition wiring.

Round-trips ``dialog_turn`` through a real ``Daemon`` + ``websockets``
client and asserts the work-classified branch now returns:

  * ``kind: "work"`` (was ``"chat"`` under feat-044's placeholder)
  * ``drafts: [...]`` carrying 1-3 ``DraftCard`` dicts
  * each draft has the v0.1 schema the Web UI consumes
  * chat-classified messages still return ``kind: "chat"`` with
    an empty ``drafts`` array and the friendly text reply

Mirrors the style of ``test_intent_wiring.py``: ``unittest`` style,
isolated asyncio test case, tmp projects.json + tmp fixture_root so
the user's real ``~/.heddle/`` is untouched.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import websockets

from heddle_daemon.decompose import DECOMPOSE_FIXTURE_NAME, MIN_DRAFTS, MAX_DRAFTS
from heddle_daemon.server import (
    Daemon,
    DaemonConfig,
    JsonEnvelope,
    build_envelope,
    parse_envelope,
)

# Under HEDDLE_FAKE_LLM=1 the daemon reads the fixture from
# ``<fixture_root>/decompose.json``. Tests set this env var so the
# fixture path is deterministic.
FAKE_ENV = {"HEDDLE_FAKE_LLM": "1"}


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
        deadline = timeout
        while True:
            raw = await asyncio.wait_for(conn.recv(), timeout=deadline)
            received = parse_envelope(raw)
            if received.type == "event":
                continue
            return received


def _make_feature_list(project_dir: Path) -> Path:
    """Drop an empty-but-valid feature_list.json (no rows needed)."""
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


def _write_decompose_fixture(fixture_root: Path, drafts: list[dict[str, Any]]) -> Path:
    p = fixture_root / DECOMPOSE_FIXTURE_NAME
    p.write_text(
        json.dumps(drafts, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return p


def _minimal_draft(idx: int = 1) -> dict[str, Any]:
    return {
        "id": f"temp-{idx:03d}",
        "title": f"Draft {idx}",
        "description": f"Description for draft {idx}",
        "steps": [f"Step {idx}.1", f"Step {idx}.2"],
        "depends_on": [],
        "kind": "feature",
    }


# ---------- tests ----------


class TestDecomposeWiringFake(unittest.IsolatedAsyncioTestCase):
    """Round-trip dialog_turn with HEDDLE_FAKE_LLM=1 and a fixture on disk."""

    async def asyncSetUp(self) -> None:
        # Two tmp roots: one for projects.json + the per-project
        # feature_list.json, one for the fake fixture_root so the
        # decompose.json lookup is isolated.
        self._proj_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._proj_tmp.cleanup)
        self.proj_tmpdir = Path(self._proj_tmp.name)
        self.projects_path = self.proj_tmpdir / "projects.json"
        self.project_dir = self.proj_tmpdir / "proj-decompose"
        self.project_dir.mkdir()
        _make_feature_list(self.project_dir)

        self._fix_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._fix_tmp.cleanup)
        self.fixture_root = Path(self._fix_tmp.name)
        _write_decompose_fixture(
            self.fixture_root,
            [_minimal_draft(1), _minimal_draft(2)],
        )

        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        self.daemon._routes.fixture_root = self.fixture_root
        # Force the daemon (and the decomposer it calls) into fake-LLM
        # mode regardless of the host environment.
        import os as _os
        self._saved_env = _os.environ.get("HEDDLE_FAKE_LLM")
        _os.environ["HEDDLE_FAKE_LLM"] = "1"
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

        from heddle_common import add_project

        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-decompose",
        )
        self.project_id = self.project.id

    async def asyncTearDown(self) -> None:
        import os as _os
        if self._saved_env is None:
            _os.environ.pop("HEDDLE_FAKE_LLM", None)
        else:
            _os.environ["HEDDLE_FAKE_LLM"] = self._saved_env

    async def _dialog(self, message: str, *, req_id: str = "x") -> dict[str, Any]:
        env = build_envelope(
            "dialog_turn",
            req_id=req_id,
            project_id=self.project_id,
            message=message,
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        return resp.extra["data"]

    async def test_work_message_returns_work_kind_with_drafts(self) -> None:
        data = await self._dialog("add OAuth login", req_id="w1")
        self.assertEqual(data["intent"], "work")
        self.assertEqual(data["kind"], "work")
        drafts = data["drafts"]
        self.assertIsInstance(drafts, list)
        self.assertGreaterEqual(len(drafts), MIN_DRAFTS)
        self.assertLessEqual(len(drafts), MAX_DRAFTS)
        # Two-draft fixture → exactly two cards.
        self.assertEqual(len(drafts), 2)
        for d in drafts:
            self.assertTrue(d["id"].startswith("temp-"))
            self.assertIsInstance(d["title"], str)
            self.assertTrue(d["title"])
            self.assertIsInstance(d["description"], str)
            self.assertTrue(d["description"])
            self.assertIsInstance(d["steps"], list)
            self.assertIsInstance(d["depends_on"], list)
            self.assertIn(d["kind"], ("feature", "bugfix", "enhancement"))

    async def test_work_message_drafts_match_fixture(self) -> None:
        data = await self._dialog("fix the bug", req_id="w2")
        drafts = data["drafts"]
        self.assertEqual([d["id"] for d in drafts], ["temp-001", "temp-002"])
        self.assertEqual(drafts[0]["title"], "Draft 1")
        self.assertEqual(drafts[1]["title"], "Draft 2")

    async def test_chat_message_returns_chat_kind_with_empty_drafts(self) -> None:
        data = await self._dialog("hello there", req_id="c1")
        self.assertEqual(data["intent"], "chat")
        self.assertEqual(data["kind"], "chat")
        self.assertEqual(data["drafts"], [])
        # Friendly reply preserved from feat-044.
        self.assertIn("add or change", data["text"])

    async def test_work_text_is_the_decompose_courtesy_line(self) -> None:
        data = await self._dialog("add OAuth login", req_id="w3")
        # The text is the courtesy line that accompanies the draft
        # tray; feat-040's tray carries the actual card content.
        self.assertNotEqual(data["text"], "")
        self.assertNotIn("add or change", data["text"])


class TestDecomposeWiringFakeMissingFixture(
    unittest.IsolatedAsyncioTestCase,
):
    """HEDDLE_FAKE_LLM=1 but no fixture → deterministic fallback."""

    async def asyncSetUp(self) -> None:
        self._proj_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._proj_tmp.cleanup)
        self.proj_tmpdir = Path(self._proj_tmp.name)
        self.projects_path = self.proj_tmpdir / "projects.json"
        self.project_dir = self.proj_tmpdir / "proj-fb"
        self.project_dir.mkdir()
        _make_feature_list(self.project_dir)

        self._fix_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._fix_tmp.cleanup)
        self.fixture_root = Path(self._fix_tmp.name)
        # NOTE: deliberately NOT writing a decompose.json.

        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        self.daemon._routes.fixture_root = self.fixture_root
        import os as _os
        self._saved_env = _os.environ.get("HEDDLE_FAKE_LLM")
        _os.environ["HEDDLE_FAKE_LLM"] = "1"
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

        from heddle_common import add_project

        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-fb",
        )
        self.project_id = self.project.id

    async def asyncTearDown(self) -> None:
        import os as _os
        if self._saved_env is None:
            _os.environ.pop("HEDDLE_FAKE_LLM", None)
        else:
            _os.environ["HEDDLE_FAKE_LLM"] = self._saved_env

    async def test_work_message_returns_single_draft_fallback(self) -> None:
        env = build_envelope(
            "dialog_turn",
            req_id="fb1",
            project_id=self.project_id,
            message="add OAuth login",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        data = resp.extra["data"]
        self.assertEqual(data["intent"], "work")
        self.assertEqual(data["kind"], "work")
        # Missing fixture → exactly one deterministic fallback card.
        self.assertEqual(len(data["drafts"]), 1)
        d = data["drafts"][0]
        self.assertTrue(d["id"].startswith("temp-"))
        self.assertTrue(d["title"])


class TestDecomposeWiringRealPlaceholder(unittest.IsolatedAsyncioTestCase):
    """HEDDLE_FAKE_LLM NOT set → real-mode placeholder (single card)."""

    async def asyncSetUp(self) -> None:
        self._proj_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._proj_tmp.cleanup)
        self.proj_tmpdir = Path(self._proj_tmp.name)
        self.projects_path = self.proj_tmpdir / "projects.json"
        self.project_dir = self.proj_tmpdir / "proj-real"
        self.project_dir.mkdir()
        _make_feature_list(self.project_dir)

        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        # Force fake-mode OFF for the duration of this test, even if
        # the developer's shell happens to have HEDDLE_FAKE_LLM set.
        import os as _os
        self._saved_env = _os.environ.get("HEDDLE_FAKE_LLM")
        _os.environ["HEDDLE_FAKE_LLM"] = "0"
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

        from heddle_common import add_project

        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-real",
        )
        self.project_id = self.project.id

    async def asyncTearDown(self) -> None:
        import os as _os
        if self._saved_env is None:
            _os.environ.pop("HEDDLE_FAKE_LLM", None)
        else:
            _os.environ["HEDDLE_FAKE_LLM"] = self._saved_env

    async def test_work_message_returns_placeholder_draft(self) -> None:
        env = build_envelope(
            "dialog_turn",
            req_id="r1",
            project_id=self.project_id,
            message="add OAuth login",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertTrue(resp.extra.get("ok"), resp.extra)
        data = resp.extra["data"]
        self.assertEqual(data["intent"], "work")
        self.assertEqual(data["kind"], "work")
        # Real-mode placeholder → exactly one card with the
        # documented title that tells the user feat-046 is pending.
        self.assertEqual(len(data["drafts"]), 1)
        self.assertEqual(data["drafts"][0]["title"], "Real LLM decomposition")
        self.assertEqual(data["drafts"][0]["kind"], "feature")


class TestDecomposeWiringMalformedFixture(
    unittest.IsolatedAsyncioTestCase,
):
    """A malformed fixture must surface as an internal_error envelope."""

    async def asyncSetUp(self) -> None:
        self._proj_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._proj_tmp.cleanup)
        self.proj_tmpdir = Path(self._proj_tmp.name)
        self.projects_path = self.proj_tmpdir / "projects.json"
        self.project_dir = self.proj_tmpdir / "proj-bad"
        self.project_dir.mkdir()
        _make_feature_list(self.project_dir)

        self._fix_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._fix_tmp.cleanup)
        self.fixture_root = Path(self._fix_tmp.name)
        # Write a fixture that violates the schema (wrong id prefix).
        (self.fixture_root / DECOMPOSE_FIXTURE_NAME).write_text(
            json.dumps([{"id": "feat-001", "title": "x", "description": "y"}]),
            encoding="utf-8",
        )

        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        self.daemon._routes.fixture_root = self.fixture_root
        import os as _os
        self._saved_env = _os.environ.get("HEDDLE_FAKE_LLM")
        _os.environ["HEDDLE_FAKE_LLM"] = "1"
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

        from heddle_common import add_project

        self.project = add_project(
            self.projects_path,
            project_path=str(self.project_dir),
            name="proj-bad",
        )
        self.project_id = self.project.id

    async def asyncTearDown(self) -> None:
        import os as _os
        if self._saved_env is None:
            _os.environ.pop("HEDDLE_FAKE_LLM", None)
        else:
            _os.environ["HEDDLE_FAKE_LLM"] = self._saved_env

    async def test_malformed_fixture_returns_internal_error(self) -> None:
        env = build_envelope(
            "dialog_turn",
            req_id="bad1",
            project_id=self.project_id,
            message="add OAuth login",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "internal_error")
        self.assertIn("decompose failed", resp.extra["error"]["message"])


if __name__ == "__main__":
    unittest.main()
