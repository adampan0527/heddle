# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``heddle_daemon.decompose`` (feat-045).

Covers the four behaviors of ``propose_drafts``:

  1. **Fake-LLM path** — ``HEDDLE_FAKE_LLM=1`` plus a fixture file
     returns 1-3 ``DraftCard`` instances with the documented schema
     (id prefix ``temp-``, title + description + steps + depends_on
     + kind).
  2. **Fake-LLM missing fixture** — same env var but no fixture on
     disk returns a deterministic single-draft fallback (sha256 of
     the message) so tests don't crash on a missing file.
  3. **Real-LLM placeholder path** — ``HEDDLE_FAKE_LLM`` unset
     returns a single placeholder draft and logs a warning.
  4. **Error paths** — empty message, fixture schema violations
     (unknown key, bad id prefix, empty title, wrong draft count).

The tests use a tmp fixture_root so the user's ``~/.heddle/`` is
never touched. No real LLM is invoked at any point.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from heddle_daemon.decompose import (
    DECOMPOSE_FIXTURE_NAME,
    MAX_DRAFTS,
    MIN_DRAFTS,
    PLACEHOLDER_DRAFT_DESCRIPTION,
    PLACEHOLDER_DRAFT_TITLE,
    DecomposeError,
    DraftCard,
    EmptyMessageError,
    propose_drafts,
)


FAKE_ENV = {"HEDDLE_FAKE_LLM": "1"}


def _write_decompose_fixture(tmpdir: Path, drafts: list[dict[str, Any]]) -> Path:
    p = tmpdir / DECOMPOSE_FIXTURE_NAME
    p.write_text(
        json.dumps(drafts, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return p


def _minimal_draft(idx: int = 1, **overrides: Any) -> dict[str, Any]:
    """One minimal well-formed draft entry for tests."""
    base: dict[str, Any] = {
        "id": f"temp-{idx:03d}",
        "title": f"Draft {idx}",
        "description": f"Description for draft {idx}",
        "steps": [f"Step {idx}.1", f"Step {idx}.2"],
        "depends_on": [],
        "kind": "feature",
    }
    base.update(overrides)
    return base


# ---------- tests ----------


class TestProposeDraftsFake(unittest.TestCase):
    """Fake-LLM path: load ``<fixture_root>/decompose.json``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def test_single_draft_fixture_returns_one_card(self) -> None:
        _write_decompose_fixture(self.tmpdir, [_minimal_draft(1)])
        drafts = propose_drafts(
            "add OAuth login", [], fixture_root=self.tmpdir, env=FAKE_ENV
        )
        self.assertEqual(len(drafts), 1)
        self.assertIsInstance(drafts[0], DraftCard)
        d = drafts[0]
        self.assertEqual(d.id, "temp-001")
        self.assertEqual(d.title, "Draft 1")
        self.assertEqual(d.description, "Description for draft 1")
        self.assertEqual(tuple(d.steps), ("Step 1.1", "Step 1.2"))
        self.assertEqual(d.depends_on, ())
        self.assertEqual(d.kind, "feature")

    def test_three_drafts_fixture_returns_three_cards(self) -> None:
        drafts_in = [
            _minimal_draft(1),
            _minimal_draft(2, depends_on=["temp-001"]),
            _minimal_draft(3, kind="bugfix"),
        ]
        _write_decompose_fixture(self.tmpdir, drafts_in)
        drafts = propose_drafts(
            "anything", [], fixture_root=self.tmpdir, env=FAKE_ENV
        )
        self.assertEqual(len(drafts), 3)
        self.assertEqual([d.id for d in drafts], ["temp-001", "temp-002", "temp-003"])
        self.assertEqual(drafts[1].depends_on, ("temp-001",))
        self.assertEqual(drafts[2].kind, "bugfix")

    def test_to_dict_round_trips_the_v01_wire_shape(self) -> None:
        _write_decompose_fixture(
            self.tmpdir,
            [_minimal_draft(7, steps=["a", "b"], depends_on=["temp-006"])],
        )
        drafts = propose_drafts(
            "x", [], fixture_root=self.tmpdir, env=FAKE_ENV
        )
        wire = drafts[0].to_dict()
        self.assertEqual(
            set(wire.keys()),
            {"id", "title", "description", "steps", "depends_on", "kind"},
        )
        self.assertEqual(wire["id"], "temp-007")
        self.assertEqual(wire["steps"], ["a", "b"])
        self.assertEqual(wire["depends_on"], ["temp-006"])

    def test_min_steps_is_one(self) -> None:
        _write_decompose_fixture(self.tmpdir, [_minimal_draft(1)])
        drafts = propose_drafts(
            "x", [], fixture_root=self.tmpdir, env=FAKE_ENV
        )
        self.assertGreaterEqual(len(drafts), MIN_DRAFTS)

    def test_max_drafts_is_three(self) -> None:
        drafts_in = [_minimal_draft(i) for i in range(1, MAX_DRAFTS + 1)]
        _write_decompose_fixture(self.tmpdir, drafts_in)
        drafts = propose_drafts(
            "x", [], fixture_root=self.tmpdir, env=FAKE_ENV
        )
        self.assertLessEqual(len(drafts), MAX_DRAFTS)


class TestProposeDraftsFakeMissingFixture(unittest.TestCase):
    """HEDDLE_FAKE_LLM=1 with no fixture → deterministic hash fallback."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def test_returns_single_deterministic_draft(self) -> None:
        drafts1 = propose_drafts(
            "add OAuth login", [], fixture_root=self.tmpdir, env=FAKE_ENV
        )
        drafts2 = propose_drafts(
            "add OAuth login", [], fixture_root=self.tmpdir, env=FAKE_ENV
        )
        # Same input → same output (id derived from sha256(message)).
        self.assertEqual(len(drafts1), 1)
        self.assertEqual(len(drafts2), 1)
        self.assertEqual(drafts1[0].id, drafts2[0].id)

    def test_different_messages_yield_different_ids(self) -> None:
        drafts1 = propose_drafts(
            "add OAuth login", [], fixture_root=self.tmpdir, env=FAKE_ENV
        )
        drafts2 = propose_drafts(
            "fix the bug", [], fixture_root=self.tmpdir, env=FAKE_ENV
        )
        self.assertNotEqual(drafts1[0].id, drafts2[0].id)
        # Both still temp-XXX prefixed.
        self.assertTrue(drafts1[0].id.startswith("temp-"))
        self.assertTrue(drafts2[0].id.startswith("temp-"))


class TestProposeDraftsRealPlaceholder(unittest.TestCase):
    """HEDDLE_FAKE_LLM not set → single placeholder draft + warning."""

    def test_returns_placeholder_with_documented_title(self) -> None:
        env: dict[str, str] = {}  # HEDDLE_FAKE_LLM unset
        drafts = propose_drafts("add OAuth login", [], env=env)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, PLACEHOLDER_DRAFT_TITLE)
        self.assertEqual(drafts[0].description, PLACEHOLDER_DRAFT_DESCRIPTION)
        self.assertEqual(drafts[0].kind, "feature")
        self.assertEqual(drafts[0].steps, ())
        self.assertEqual(drafts[0].depends_on, ())

    def test_placeholder_id_is_deterministic(self) -> None:
        env: dict[str, str] = {}
        a = propose_drafts("same message", [], env=env)
        b = propose_drafts("same message", [], env=env)
        self.assertEqual(a[0].id, b[0].id)
        self.assertTrue(a[0].id.startswith("temp-"))

    def test_truthy_but_unrecognized_env_value_means_fake(self) -> None:
        # Anything matching the truthy set is fake; confirm "yes"
        # also flips us to fixture mode (and a missing fixture falls
        # back to the deterministic single-draft path).
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)
            drafts = propose_drafts(
                "x", [], fixture_root=tmpdir, env={"HEDDLE_FAKE_LLM": "yes"}
            )
            self.assertEqual(len(drafts), 1)
            # The placeholder title would only appear under real-mode.
            self.assertNotEqual(drafts[0].title, PLACEHOLDER_DRAFT_TITLE)


class TestProposeDraftsErrors(unittest.TestCase):
    """Defensive guards + fixture-schema violations."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def test_empty_message_raises(self) -> None:
        with self.assertRaises(EmptyMessageError):
            propose_drafts("", [], env={})
        with self.assertRaises(EmptyMessageError):
            propose_drafts("   ", [], env={})

    def test_non_string_message_raises(self) -> None:
        with self.assertRaises(EmptyMessageError):
            propose_drafts(None, [], env={})  # type: ignore[arg-type]

    def test_fixture_with_zero_drafts_rejected(self) -> None:
        _write_decompose_fixture(self.tmpdir, [])
        with self.assertRaises(DecomposeError) as cm:
            propose_drafts("x", [], fixture_root=self.tmpdir, env=FAKE_ENV)
        self.assertIn("expected 1-3", str(cm.exception))

    def test_fixture_with_too_many_drafts_rejected(self) -> None:
        _write_decompose_fixture(
            self.tmpdir, [_minimal_draft(i) for i in range(1, 5)]
        )
        with self.assertRaises(DecomposeError):
            propose_drafts("x", [], fixture_root=self.tmpdir, env=FAKE_ENV)

    def test_fixture_with_unknown_key_rejected(self) -> None:
        _write_decompose_fixture(
            self.tmpdir, [_minimal_draft(1, bogus_field="x")]
        )
        with self.assertRaises(DecomposeError) as cm:
            propose_drafts("x", [], fixture_root=self.tmpdir, env=FAKE_ENV)
        self.assertIn("unknown keys", str(cm.exception))

    def test_fixture_with_bad_id_prefix_rejected(self) -> None:
        _write_decompose_fixture(self.tmpdir, [_minimal_draft(1, id="feat-001")])
        with self.assertRaises(DecomposeError) as cm:
            propose_drafts("x", [], fixture_root=self.tmpdir, env=FAKE_ENV)
        self.assertIn("temp-", str(cm.exception))

    def test_fixture_with_empty_title_rejected(self) -> None:
        _write_decompose_fixture(self.tmpdir, [_minimal_draft(1, title="")])
        with self.assertRaises(DecomposeError):
            propose_drafts("x", [], fixture_root=self.tmpdir, env=FAKE_ENV)

    def test_fixture_with_bad_kind_rejected(self) -> None:
        _write_decompose_fixture(self.tmpdir, [_minimal_draft(1, kind="not-a-kind")])
        with self.assertRaises(DecomposeError):
            propose_drafts("x", [], fixture_root=self.tmpdir, env=FAKE_ENV)

    def test_fixture_with_malformed_json_rejected(self) -> None:
        bad = self.tmpdir / DECOMPOSE_FIXTURE_NAME
        bad.write_text("{not json}", encoding="utf-8")
        with self.assertRaises(DecomposeError):
            propose_drafts("x", [], fixture_root=self.tmpdir, env=FAKE_ENV)


class TestProposeDraftsIntegrationShape(unittest.TestCase):
    """Sanity-check the integration: drafts from propose_drafts are
    dict-serializable and have the v0.1 keys the Web UI consumes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def test_drafts_round_trip_through_json(self) -> None:
        _write_decompose_fixture(
            self.tmpdir, [_minimal_draft(1), _minimal_draft(2)]
        )
        drafts = propose_drafts(
            "x", [], fixture_root=self.tmpdir, env=FAKE_ENV
        )
        wire = [d.to_dict() for d in drafts]
        # Round-trip through JSON to prove the wire shape is plain
        # JSON-serializable (the daemon crosses a WS boundary).
        as_json = json.dumps(wire)
        roundtrip = json.loads(as_json)
        self.assertEqual(roundtrip, wire)


if __name__ == "__main__":
    unittest.main()
