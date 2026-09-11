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
    def test_load_then_save_round_trip_stamps_schema_version(self):
        """feat-009: load+save migrates a legacy file to schema_version=1 in place.

        Pre-feat-009 behavior (byte-identical round-trip) is intentionally
        broken: every save() call must stamp the current SCHEMA_VERSION so
        downstream readers can refuse too-new files with a clear error.

        The test uses a temp file rather than HARNESS_FEATURE_LIST because
        once that file is migrated, further load+save cycles on it are
        no-ops; we need a fresh legacy file to exercise the migration path.
        """
        legacy = Path(sys.argv[0]).parent / "_test_legacy_feature_list.json"
        # Strip schema_version from a copy of the real HARNESS template.
        sample = json.loads(HARNESS_FEATURE_LIST.read_text(encoding="utf-8"))
        sample.pop("schema_version", None)
        legacy.write_text(json.dumps(sample, indent=2) + "\n", encoding="utf-8")
        try:
            original_bytes = legacy.read_bytes()
            data = fl.load(legacy)
            # After load(), a missing field is materialized as 0 (legacy sentinel).
            self.assertEqual(data["schema_version"], 0)
            fl.save(legacy, data)
            new_bytes = legacy.read_bytes()
            self.assertNotEqual(
                original_bytes,
                new_bytes,
                "round-trip must NOT be byte-identical once schema_version is stamped",
            )
            # After save(), the on-disk schema_version equals SCHEMA_VERSION.
            parsed = json.loads(new_bytes.decode("utf-8"))
            self.assertEqual(parsed["schema_version"], fl.SCHEMA_VERSION)
        finally:
            if legacy.exists():
                legacy.unlink()

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


class TestSchemaVersion(unittest.TestCase):
    """feat-009 / T-022 — top-level `schema_version` field on feature_list.json.

    Scenarios from the feat-009 steps array:

      1. load() an old file (no schema_version) -> dict has schema_version=0
         internally, then save() rewrites it as schema_version=1.
      2. load() a file with schema_version > SCHEMA_VERSION_MAX
         -> SchemaVersionError with a clear, actionable message.
      3. save() always stamps the current SCHEMA_VERSION, regardless of
         what the dict held on the way in.
    """

    def setUp(self):
        self.tmp = Path(sys.argv[0]).parent / "_test_schema_version.json"

    def tearDown(self):
        if self.tmp.exists():
            self.tmp.unlink()

    def _write(self, payload: dict) -> None:
        self.tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _minimal_payload(self) -> dict:
        return {
            "project_name": "sv-test",
            "description": "schema_version test",
            "features": [
                {
                    "id": "feat-x",
                    "category": "functional",
                    "description": "x",
                    "steps": ["step 1"],
                    "status": "pending",
                    "priority": "medium",
                    "depends_on": [],
                    "attempts": [],
                }
            ],
            "metadata": {},
        }

    def test_legacy_file_loads_with_default_zero_then_save_promotes(self):
        # File on disk has NO schema_version (legacy form).
        self._write(self._minimal_payload())
        raw = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertNotIn("schema_version", raw)

        data = fl.load(self.tmp)
        # Internal in-memory representation gets the legacy sentinel.
        self.assertEqual(data["schema_version"], 0)

        # Save promotes the on-disk value to the current SCHEMA_VERSION.
        fl.save(self.tmp, data)
        written = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(written["schema_version"], fl.SCHEMA_VERSION)
        # And the in-memory dict now also reflects the stamped value.
        self.assertEqual(data["schema_version"], fl.SCHEMA_VERSION)

    def test_future_schema_version_raises_clear_error(self):
        payload = self._minimal_payload()
        # Pick a value strictly greater than SCHEMA_VERSION_MAX so the
        # refusal branch is exercised regardless of how MAX moves later.
        payload["schema_version"] = fl.SCHEMA_VERSION_MAX + 100
        self._write(payload)

        with self.assertRaises(fl.SchemaVersionError) as ctx:
            fl.load(self.tmp)
        msg = str(ctx.exception)
        # Error must name BOTH versions and a recovery hint.
        self.assertIn(str(payload["schema_version"]), msg)
        self.assertIn(str(fl.SCHEMA_VERSION_MAX), msg)
        self.assertIn("upgrade heddle", msg.lower())

    def test_save_overwrites_stale_schema_version(self):
        # File on disk has an old (but valid) schema_version; the in-memory
        # value is mutated lower; save() must still stamp SCHEMA_VERSION.
        payload = self._minimal_payload()
        payload["schema_version"] = 0
        self._write(payload)

        data = fl.load(self.tmp)
        self.assertEqual(data["schema_version"], 0)

        # Mutate downward — save() must not propagate this.
        data["schema_version"] = -5
        fl.save(self.tmp, data)

        written = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(written["schema_version"], fl.SCHEMA_VERSION)

    def test_non_integer_schema_version_fails_loud(self):
        payload = self._minimal_payload()
        payload["schema_version"] = "1"  # string, not int
        self._write(payload)

        with self.assertRaises(SystemExit):
            fl.load(self.tmp)

    def test_boolean_schema_version_rejected_as_non_integer(self):
        # `bool` is a subclass of `int` in Python; the load() code explicitly
        # rejects booleans so a stray `True` cannot sneak through as 1.
        payload = self._minimal_payload()
        payload["schema_version"] = True
        self._write(payload)

        with self.assertRaises(SystemExit):
            fl.load(self.tmp)

    def test_negative_schema_version_fails_loud(self):
        payload = self._minimal_payload()
        payload["schema_version"] = -1
        self._write(payload)

        with self.assertRaises(SystemExit):
            fl.load(self.tmp)

    def test_schema_version_constants_are_sane(self):
        # Defensive: SCHEMA_VERSION_MAX must be >= SCHEMA_VERSION (otherwise
        # the loaded file can never be at-or-below MAX and every load fails).
        self.assertGreaterEqual(fl.SCHEMA_VERSION_MAX, fl.SCHEMA_VERSION)
        self.assertGreaterEqual(fl.SCHEMA_VERSION, 1)


if __name__ == "__main__":
    unittest.main()