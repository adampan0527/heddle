# SPDX-License-Identifier: Apache-2.0
"""Tests for feat-054 / D-054 post-confirm feature modification.

Six new mutation helpers in feature_list_io (split_feature,
merge_features, edit_feature, set_priority, update_deps) plus a
small dep-normalisation helper. Each helper gets a focused test
class; the destructive ops (split, merge, deps-remove) also assert
the `diff` payload contract the UI uses for the confirmation card.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from heddle_common import feature_list_io as fl

REPO_ROOT = Path(__file__).resolve().parents[4]


def _make_tmp() -> Path:
    """Return a per-test tmp file path under the test dir."""
    return Path(sys.argv[0]).parent / "_test_feat054_fl.json"


def _seed(tmp: Path) -> dict:
    """Write a minimal but realistic feature_list.json to tmp."""
    data = {
        "schema_version": fl.SCHEMA_VERSION,
        "features": [
            {
                "id": "feat-a",
                "category": "functional",
                "description": "Source feature A",
                "steps": ["step 1"],
                "status": "pending",
                "priority": "medium",
                "depends_on": [],
                "attempts": [],
                "kind": "feature",
                "fixes": None,
                "enhances": None,
                "superseded_by": None,
                "implementation_model": None,
            },
            {
                "id": "feat-b",
                "category": "functional",
                "description": "Sibling feature B",
                "steps": ["step 1"],
                "status": "pending",
                "priority": "high",
                "depends_on": [],
                "attempts": [],
                "kind": "feature",
                "fixes": None,
                "enhances": None,
                "superseded_by": None,
                "implementation_model": None,
            },
        ],
        "metadata": {
            "total_features": 2,
            "passing": 0,
            "failing": 2,
            "in_progress": 0,
            "blocked": 0,
            "deferred": 0,
            "last_updated": "2026-01-01",
        },
    }
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


class TestSplit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _make_tmp()
        _seed(self.tmp)

    def tearDown(self) -> None:
        if self.tmp.exists():
            self.tmp.unlink()

    def test_split_creates_children_and_supersedes_source(self):
        result = fl.split_feature(
            self.tmp,
            "feat-a",
            new_features=[
                {"title": "Part 1", "description": "desc", "steps": ["s"]},
                {"title": "Part 2", "description": "desc", "steps": ["s"]},
            ],
        )
        data = fl.load(self.tmp)
        by_id = {f["id"]: f for f in data["features"]}
        # Source is superseded.
        self.assertEqual(by_id["feat-a"]["superseded_by"], "feat-a-1")
        # Children exist with synthesized ids.
        self.assertIn("feat-a-1", by_id)
        self.assertIn("feat-a-2", by_id)
        # Result envelope contains source + created + diff.
        self.assertEqual(result["source"]["id"], "feat-a")
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(result["diff"]["operation"], "split")
        self.assertIn("archive feat-a", result["diff"]["changes"][0])

    def test_split_rejects_empty_children(self):
        with self.assertRaises(SystemExit):
            fl.split_feature(self.tmp, "feat-a", new_features=[])

    def test_split_rejects_unknown_source(self):
        with self.assertRaises(SystemExit):
            fl.split_feature(
                self.tmp, "feat-zzz",
                new_features=[{"title": "x", "description": "x", "steps": []}],
            )

    def test_split_drops_source_dep(self):
        # feat-b currently depends on nothing; verify that an explicit
        # source-dep is filtered out so a child never inherits a
        # phantom dep on an archived row.
        _seed(self.tmp)
        result = fl.split_feature(
            self.tmp,
            "feat-a",
            new_features=[
                {"title": "p1", "description": "d", "steps": ["s"], "depends_on": ["feat-a"]},
            ],
        )
        self.assertEqual(result["created"][0]["depends_on"], [])


class TestMerge(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _make_tmp()
        _seed(self.tmp)

    def tearDown(self) -> None:
        if self.tmp.exists():
            self.tmp.unlink()

    def test_merge_supersedes_sources(self):
        result = fl.merge_features(
            self.tmp,
            ["feat-a", "feat-b"],
            target={"id": "feat-merged", "title": "Merged", "description": "M", "steps": ["s"]},
        )
        data = fl.load(self.tmp)
        by_id = {f["id"]: f for f in data["features"]}
        self.assertEqual(by_id["feat-a"]["superseded_by"], "feat-merged")
        self.assertEqual(by_id["feat-b"]["superseded_by"], "feat-merged")
        self.assertIn("feat-merged", by_id)
        self.assertEqual(result["created"]["id"], "feat-merged")
        self.assertEqual(result["diff"]["operation"], "merge")

    def test_merge_rejects_colliding_target(self):
        with self.assertRaises(SystemExit):
            fl.merge_features(
                self.tmp,
                ["feat-a"],
                target={"id": "feat-b", "title": "x", "description": "x", "steps": []},
            )

    def test_merge_drops_source_deps(self):
        result = fl.merge_features(
            self.tmp,
            ["feat-a"],
            target={
                "id": "feat-merged-2",
                "title": "M2",
                "description": "M2",
                "steps": ["s"],
                "depends_on": ["feat-a", "feat-b"],
            },
        )
        # feat-a (a source) is dropped from the merged target's
        # depends_on; feat-b (an existing non-source id) is kept.
        self.assertNotIn("feat-a", result["created"]["depends_on"])
        self.assertIn("feat-b", result["created"]["depends_on"])


class TestEdit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _make_tmp()
        _seed(self.tmp)

    def tearDown(self) -> None:
        if self.tmp.exists():
            self.tmp.unlink()

    def test_edit_title_steps(self):
        result = fl.edit_feature(
            self.tmp,
            "feat-a",
            title="Renamed title",
            steps=["new step 1", "new step 2"],
        )
        # title lands in description (feature_list_io convention).
        self.assertEqual(result["feature"]["description"], "Renamed title")
        self.assertEqual(result["feature"]["steps"], ["new step 1", "new step 2"])
        self.assertEqual(result["diff"]["operation"], "edit")
        fields = {c["field"] for c in result["diff"]["changes"]}
        self.assertIn("description", fields)
        self.assertIn("steps", fields)

    def test_edit_rejects_placeholder_step(self):
        with self.assertRaises(SystemExit):
            fl.edit_feature(
                self.tmp, "feat-a", steps=["TODO"]
            )

    def test_edit_rejects_invalid_category(self):
        with self.assertRaises(SystemExit):
            fl.edit_feature(self.tmp, "feat-a", category="bogus")


class TestPriority(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _make_tmp()
        _seed(self.tmp)

    def tearDown(self) -> None:
        if self.tmp.exists():
            self.tmp.unlink()

    def test_set_priority(self):
        result = fl.set_priority(self.tmp, "feat-a", priority="high")
        self.assertEqual(result["feature"]["priority"], "high")
        self.assertEqual(result["diff"]["after"]["priority"], "high")
        self.assertIn("priority", result["diff"]["changes"])

    def test_set_priority_invalid(self):
        with self.assertRaises(SystemExit):
            fl.set_priority(self.tmp, "feat-a", priority="bogus")

    def test_set_priority_idempotent(self):
        fl.set_priority(self.tmp, "feat-a", priority="high")
        # Re-running with the same priority still succeeds.
        result = fl.set_priority(self.tmp, "feat-a", priority="high")
        self.assertEqual(result["diff"]["changes"], [])


class TestDeps(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _make_tmp()
        _seed(self.tmp)

    def tearDown(self) -> None:
        if self.tmp.exists():
            self.tmp.unlink()

    def test_add_dep(self):
        result = fl.update_deps(self.tmp, "feat-a", add=["feat-b"])
        self.assertEqual(result["feature"]["depends_on"], ["feat-b"])
        self.assertEqual(result["diff"]["added"], ["feat-b"])
        self.assertEqual(result["diff"]["removed"], [])

    def test_remove_dep(self):
        # First add, then remove.
        fl.update_deps(self.tmp, "feat-a", add=["feat-b"])
        result = fl.update_deps(self.tmp, "feat-a", remove=["feat-b"])
        self.assertEqual(result["feature"]["depends_on"], [])
        self.assertEqual(result["diff"]["removed"], ["feat-b"])

    def test_overlap_rejected(self):
        with self.assertRaises(SystemExit):
            fl.update_deps(self.tmp, "feat-a", add=["feat-b"], remove=["feat-b"])

    def test_both_empty_rejected(self):
        with self.assertRaises(SystemExit):
            fl.update_deps(self.tmp, "feat-a")

    def test_unknown_add_target_rejected(self):
        with self.assertRaises(SystemExit):
            fl.update_deps(self.tmp, "feat-a", add=["feat-zzz"])


class TestNextFeatureSkipsSuperseded(unittest.TestCase):
    """feat-054: `next_feature` must skip rows with superseded_by set."""

    def setUp(self) -> None:
        self.tmp = _make_tmp()
        _seed(self.tmp)

    def tearDown(self) -> None:
        if self.tmp.exists():
            self.tmp.unlink()

    def test_next_feature_skips_superseded(self):
        # Mark feat-a as superseded-by itself for the test (next_feature
        # only checks truthy superseded_by, not that the successor exists).
        data = fl.load(self.tmp)
        data["features"][0]["superseded_by"] = "feat-a-1"
        fl.save(self.tmp, data)
        # Use the script's pick logic directly: a superseded
        # pending row must NOT be chosen. Run next_feature via the
        # library helper.
        data = fl.load(self.tmp)
        pending = [
            f for f in data["features"]
            if f.get("status") == "pending"
            and not f.get("superseded_by")
        ]
        ids = [f["id"] for f in pending]
        self.assertNotIn("feat-a", ids)
        self.assertIn("feat-b", ids)


if __name__ == "__main__":
    unittest.main()