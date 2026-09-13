# SPDX-License-Identifier: Apache-2.0
"""Integration tests for feat-044 intent-classification wiring.

Round-trips ``dialog_turn`` through a real ``Daemon`` + ``websockets``
client and asserts:

  * chat-classified messages return ``kind: "chat"`` with a friendly
    reply (no draft cards) and skip the work flow entirely;
  * work-classified messages are classified correctly (the
    ``intent`` field carries ``"work"``) and for v0.1 still return
    ``kind: "chat"`` with a placeholder text because feat-045 has not
    landed yet — but the ``intent`` field is the contract that the
    Web UI inspects;
  * the old stub's ``echo: <message>`` text is gone — every reply
    goes through the intent-classifier path.

Mirrors the style of ``test_routes_smoke.py``: ``unittest`` style,
isolated asyncio test case, tmp projects.json + tmp project
directory so the user's real ``~/.heddle/`` is untouched.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import websockets

from heddle_daemon.intent import classify_intent
from heddle_daemon.server import (
    Daemon,
    DaemonConfig,
    JsonEnvelope,
    build_envelope,
    parse_envelope,
)


# ---------- helpers (kept local — test_routes_smoke.py is a sibling
# but keeping these private avoids a refactor cycle just to share
# three helpers between the two files). ----------


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


# ---------- tests ----------


class TestIntentWiring(unittest.IsolatedAsyncioTestCase):
    """Round-trip dialog_turn under feat-044's classifier."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.projects_path = self.tmpdir / "projects.json"
        self.project_dir = self.tmpdir / "proj-intent"
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
            name="proj-intent",
        )
        self.project_id = self.project.id

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

    # ----- chat side -----

    async def test_chat_message_returns_chat_kind_and_friendly_text(self) -> None:
        data = await self._dialog("hi how are you", req_id="c1")
        self.assertEqual(data["intent"], "chat")
        self.assertEqual(data["kind"], "chat")
        self.assertIn("add or change", data["text"])
        # Regression guard for feat-028's stub echo behaviour:
        # the old "echo: <message>" reply must be gone.
        self.assertFalse(data["text"].startswith("echo: "))

    async def test_chat_question_returns_chat_kind(self) -> None:
        data = await self._dialog("how does this work?", req_id="c2")
        self.assertEqual(data["intent"], "chat")
        self.assertEqual(data["kind"], "chat")
        # Friendly reply does NOT echo the user's text.
        self.assertNotIn("how does this work", data["text"])

    async def test_greeting_returns_chat_kind(self) -> None:
        data = await self._dialog("hello there", req_id="c3")
        self.assertEqual(data["intent"], "chat")
        self.assertEqual(data["kind"], "chat")

    # ----- work side -----

    async def test_imperative_verb_message_classified_as_work(self) -> None:
        data = await self._dialog("add OAuth login", req_id="w1")
        self.assertEqual(data["intent"], "work")
        # feat-045: work-classified messages now return kind="work"
        # with a drafts: [...] payload (the Web UI's draft tray
        # lights up — feat-040). Under HEDDLE_FAKE_LLM=1 the
        # decomposer returns at least one draft card.
        self.assertEqual(data["kind"], "work")
        self.assertNotEqual(data["text"], "")
        # Chat text is NOT used for work messages.
        self.assertNotIn("add or change", data["text"])
        # feat-045: drafts array is present (length 1+ in fake mode;
        # in real mode the test runs without HEDDLE_FAKE_LLM so we
        # only assert the field exists).
        self.assertIn("drafts", data)
        self.assertIsInstance(data["drafts"], list)

    async def test_feat_mention_message_classified_as_work(self) -> None:
        data = await self._dialog("see feat-007 for context", req_id="w2")
        self.assertEqual(data["intent"], "work")
        # feat-045: kind="work" with drafts.
        self.assertEqual(data["kind"], "work")
        self.assertIn("drafts", data)

    async def test_at_mention_message_classified_as_work(self) -> None:
        data = await self._dialog("@feat-007 diagnose", req_id="w3")
        self.assertEqual(data["intent"], "work")

    # ----- end-to-end classifier parity -----

    async def test_response_intent_matches_classify_intent(self) -> None:
        """The handler's classification must match the pure function.

        Locks down the wiring: if the handler ever bypasses the
        classifier (e.g. a future refactor that calls the LLM
        directly), this test fails.
        """
        for msg in (
            "hi",
            "please",
            "add OAuth login",
            "fix the bug",
            "see feat-007",
            "@help",
            "  @feat-044  ",
        ):
            with self.subTest(message=msg):
                expected = classify_intent(msg)
                data = await self._dialog(msg, req_id=f"par-{msg[:6]}")
                self.assertEqual(
                    data["intent"],
                    expected,
                    f"handler disagreed with classify_intent for {msg!r}",
                )

    async def test_response_carries_project_id(self) -> None:
        data = await self._dialog("hi", req_id="pj1")
        self.assertEqual(data["project_id"], self.project_id)


class TestIntentWiringUnknownProject(unittest.IsolatedAsyncioTestCase):
    """Unknown project_id still returns not_found regardless of intent."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.projects_path = self.tmpdir / "projects.json"

        self.daemon = Daemon(DaemonConfig(port=0))
        self.daemon.enable_routes()
        self.daemon._routes.projects_path = self.projects_path
        await self.daemon.start()
        self.addCleanup(self.daemon.stop)

    async def test_chat_message_unknown_project_is_not_found(self) -> None:
        env = build_envelope(
            "dialog_turn", req_id="x1", project_id="nope", message="hi"
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "not_found")

    async def test_work_message_unknown_project_is_not_found(self) -> None:
        env = build_envelope(
            "dialog_turn",
            req_id="x2",
            project_id="nope",
            message="add OAuth login",
        )
        resp = await _round_trip(self.daemon, env)
        self.assertFalse(resp.extra.get("ok"))
        self.assertEqual(resp.extra["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()