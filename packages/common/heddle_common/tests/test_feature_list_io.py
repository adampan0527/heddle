# SPDX-License-Identifier: Apache-2.0
"""Round-trip + mutation tests for heddle_common.feature_list_io."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from heddle_common import feature_list_io as fl

REPO_ROOT = Path(__file__).resolve().parents[4]
HARNESS_FEATURE_LIST = REPO_ROOT / "HARNESS" / "feature_list.json"


class TestRoundTrip(unittest.TestCase):
    def test_load_then_save_round_trip_is_byte_identical(self):
        original_bytes = HARNESS_FEATURE_LIST.read_bytes()
        data = fl.load(HARNESS_FEATURE_LIST)
        fl.save(HARNESS_FEATURE_LIST, data)
        new_bytes = HARNESS_FEATURE_LIST.read_bytes()
        self.assertEqual(original_bytes, new_bytes, "round-trip must be byte-identical")

    def test_recompute_metadata_preserves_keys(self):
        data = fl.load(HARNESS_FEATURE_LIST)
        meta_before = dict(data["metadata"])
        fl.recompute_metadata(data)
        meta_after = data["metadata"]
        self.assertEqual(set(meta_before), set(meta_after))
        # counts must match the actual feature list for the exposed keys
        exposed_keys = ("passing", "in_progress", "blocked", "deferred")
        for key in exposed_keys:
            self.assertEqual(
                meta_after[key],
                sum(1 for f in data["features"] if fl.status_of(f) == key),
            )
        # failing = total - passing (matches the HARNESS contract)
        self.assertEqual(
            meta_after["failing"],
            meta_after["total_features"] - meta_after["passing"],
        )


class TestMutations(unittest.TestCase):
    """Exercise add / status transitions on a temp file, never the real HARNESS one."""

    def setUp(self):
        self.tmp = Path(sys.argv[0]).parent / "_test_feature_list_io.json"
        # Start from a minimal valid feature_list.json
        self.tmp.write_text(
            json.dumps(
                {
                    "project_name": "test",
                    "description": "test",
                    "features": [
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
                    "metadata": {},
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        if self.tmp.exists():
            self.tmp.unlink()

    def test_add_minimal(self):
        fl.add(
            self.tmp,
            feature_id="feat-new",
            category="functional",
            description="new feature",
        )
        data = fl.load(self.tmp)
        ids = [f["id"] for f in data["features"]]
        self.assertIn("feat-new", ids)
        new = next(f for f in data["features"] if f["id"] == "feat-new")
        self.assertEqual(new["status"], "pending")
        self.assertEqual(new["priority"], "medium")
        self.assertEqual(new["depends_on"], [])

    def test_add_then_mark_in_progress(self):
        fl.add(self.tmp, feature_id="feat-x", category="functional", description="x")
        fl.mark_in_progress(self.tmp, "feat-x")
        f = next(f for f in fl.load(self.tmp)["features"] if f["id"] == "feat-x")
        self.assertEqual(f["status"], "in_progress")

    def test_mark_passing_requires_deps_passing(self):
        # feat-blocker: passing; feat-dependent: depends_on feat-blocker, pending
        fl.add(
            self.tmp,
            feature_id="feat-dependent",
            category="functional",
            description="depends on passing",
            depends_on="feat-existing",
        )
        with self.assertRaises(SystemExit):
            fl.mark_passing(self.tmp, "feat-dependent")

    def test_mark_passing_after_dep_already_passing(self):
        # feat-existing is already passing in setUp; add a fresh feature and walk it through
        fl.add(self.tmp, feature_id="feat-fresh", category="functional", description="f")
        fl.mark_in_progress(self.tmp, "feat-fresh")
        fl.mark_passing(self.tmp, "feat-fresh")
        f = next(f for f in fl.load(self.tmp)["features"] if f["id"] == "feat-fresh")
        self.assertEqual(f["status"], "passing")
        # attempts got the passing entry
        self.assertEqual(f["attempts"][-1]["outcome"], "passing")

    def test_mark_blocked_requires_reason(self):
        fl.add(self.tmp, feature_id="feat-b", category="functional", description="b")
        with self.assertRaises(SystemExit):
            fl.mark_blocked(self.tmp, "feat-b", reason="x")  # too short
        fl.mark_blocked(self.tmp, "feat-b", reason="needs upstream fix")
        f = next(f for f in fl.load(self.tmp)["features"] if f["id"] == "feat-b")
        self.assertEqual(f["status"], "blocked")
        self.assertEqual(f["blocked_reason"], "needs upstream fix")

    def test_mark_deferred_with_until(self):
        fl.add(self.tmp, feature_id="feat-d", category="functional", description="d")
        fl.mark_deferred(self.tmp, "feat-d", until="2026-12-31")
        f = next(f for f in fl.load(self.tmp)["features"] if f["id"] == "feat-d")
        self.assertEqual(f["status"], "deferred")
        self.assertEqual(f["deferred_until"], "2026-12-31")

    def test_next_feature_picks_highest_priority_eligible(self):
        fl.add(self.tmp, feature_id="feat-low", category="functional", description="l", priority="low")
        fl.add(self.tmp, feature_id="feat-high", category="functional", description="h", priority="high")
        chosen = fl.next_feature(self.tmp)
        self.assertEqual(chosen, "feat-high")

    def test_remove_refuses_passing(self):
        with self.assertRaises(SystemExit):
            fl.remove(self.tmp, "feat-existing")

    def test_remove_with_force_succeeds(self):
        fl.remove(self.tmp, "feat-existing", force=True)
        ids = [f["id"] for f in fl.load(self.tmp)["features"]]
        self.assertNotIn("feat-existing", ids)

    def test_update_metadata(self):
        fl.update_metadata(self.tmp)
        meta = fl.load(self.tmp)["metadata"]
        self.assertEqual(meta["total_features"], 1)
        self.assertEqual(meta["passing"], 1)


if __name__ == "__main__":
    unittest.main()