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


class TestExtendedFields(unittest.TestCase):
    """feat-010 — kind / fixes / enhances / superseded_by / implementation_model.

    Covers every step in the feat-010 spec:

      1. The five fields are added to the per-feature schema.
      2. Defaults: kind=feature, all others None.
      3. `save()` always writes all five fields (even null) so the on-disk
         schema is always complete.
      4. Validation in `add()`: kind=bugfix requires fixes + target exists;
         kind=enhancement requires enhances + target exists.
      5. Round-trip: a feature with kind=bugfix, fixes=feat-X reads back
         with the same values.
      6. Old-format JSON (no kind field) loads with kind defaulting to
         "feature"; new-format JSON loads with all fields preserved.
      7. `save()` + `load()` yields byte-identical JSON (modulo schema_version).
    """

    def setUp(self):
        self.tmp = Path(sys.argv[0]).parent / "_test_extended_fields.json"
        self.tmp.write_text(
            json.dumps(
                {
                    "project_name": "ext-test",
                    "description": "feat-010",
                    "features": [
                        {
                            "id": "feat-existing",
                            "category": "functional",
                            "description": "existing",
                            "steps": ["step 1"],
                            "status": "pending",
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

    # ---- step 1 + 2: fields exist + defaults ----

    def test_load_backfills_defaults_for_old_format(self):
        # The on-disk feature has no kind/fixes/etc. — defaults must
        # be materialized in the loaded dict (so save() writes them).
        data = fl.load(self.tmp)
        feat = data["features"][0]
        self.assertEqual(feat["kind"], "feature")
        self.assertIsNone(feat["fixes"])
        self.assertIsNone(feat["enhances"])
        self.assertIsNone(feat["superseded_by"])
        self.assertIsNone(feat["implementation_model"])

    def test_load_preserves_existing_values(self):
        # Round-trip: write kind=bugfix + fixes; load returns the same.
        data = fl.load(self.tmp)
        data["features"][0]["kind"] = "bugfix"
        data["features"][0]["fixes"] = "feat-existing"
        fl.save(self.tmp, data)

        reloaded = fl.load(self.tmp)
        feat = reloaded["features"][0]
        self.assertEqual(feat["kind"], "bugfix")
        self.assertEqual(feat["fixes"], "feat-existing")
        self.assertIsNone(feat["enhances"])
        self.assertIsNone(feat["superseded_by"])
        self.assertIsNone(feat["implementation_model"])

    # ---- step 3: save() always writes all five fields ----

    def test_save_writes_all_five_fields_even_when_null(self):
        data = fl.load(self.tmp)
        # Add a feature with no extended fields populated.
        fl.add(self.tmp, feature_id="feat-new", category="functional", description="new")
        written = json.loads(self.tmp.read_text(encoding="utf-8"))
        new_feat = next(f for f in written["features"] if f["id"] == "feat-new")
        for key in ("kind", "fixes", "enhances", "superseded_by", "implementation_model"):
            self.assertIn(key, new_feat, f"save() must always include {key!r}")
        self.assertEqual(new_feat["kind"], "feature")
        self.assertIsNone(new_feat["fixes"])
        self.assertIsNone(new_feat["enhances"])
        self.assertIsNone(new_feat["superseded_by"])
        self.assertIsNone(new_feat["implementation_model"])

    # ---- step 4: validation in add() ----

    def test_add_bugfix_requires_fixes(self):
        with self.assertRaises(SystemExit):
            fl.add(
                self.tmp,
                feature_id="feat-bug",
                category="functional",
                description="a bugfix",
                kind="bugfix",
                # fixes missing
            )

    def test_add_bugfix_rejects_nonexistent_fixes_target(self):
        with self.assertRaises(SystemExit):
            fl.add(
                self.tmp,
                feature_id="feat-bug",
                category="functional",
                description="a bugfix",
                kind="bugfix",
                fixes="feat-does-not-exist",
            )

    def test_add_bugfix_rejects_self_target(self):
        with self.assertRaises(SystemExit):
            fl.add(
                self.tmp,
                feature_id="feat-existing",
                category="functional",
                description="self-fix",
                kind="bugfix",
                fixes="feat-existing",
            )

    def test_add_bugfix_with_valid_fixes_succeeds(self):
        fl.add(
            self.tmp,
            feature_id="feat-bug",
            category="functional",
            description="a bugfix",
            kind="bugfix",
            fixes="feat-existing",
        )
        data = fl.load(self.tmp)
        bug = next(f for f in data["features"] if f["id"] == "feat-bug")
        self.assertEqual(bug["kind"], "bugfix")
        self.assertEqual(bug["fixes"], "feat-existing")

    def test_add_enhancement_requires_enhances(self):
        with self.assertRaises(SystemExit):
            fl.add(
                self.tmp,
                feature_id="feat-enh",
                category="functional",
                description="an enhancement",
                kind="enhancement",
                # enhances missing
            )

    def test_add_enhancement_rejects_nonexistent_target(self):
        with self.assertRaises(SystemExit):
            fl.add(
                self.tmp,
                feature_id="feat-enh",
                category="functional",
                description="enhancement",
                kind="enhancement",
                enhances="feat-nope",
            )

    def test_add_enhancement_with_valid_target_succeeds(self):
        fl.add(
            self.tmp,
            feature_id="feat-enh",
            category="functional",
            description="an enhancement",
            kind="enhancement",
            enhances="feat-existing",
        )
        data = fl.load(self.tmp)
        enh = next(f for f in data["features"] if f["id"] == "feat-enh")
        self.assertEqual(enh["kind"], "enhancement")
        self.assertEqual(enh["enhances"], "feat-existing")

    def test_add_rejects_invalid_kind(self):
        with self.assertRaises(SystemExit):
            fl.add(
                self.tmp,
                feature_id="feat-x",
                category="functional",
                description="bad kind",
                kind="not-a-kind",
            )

    def test_add_defaults_kind_to_feature_when_omitted(self):
        fl.add(self.tmp, feature_id="feat-default", category="functional", description="d")
        data = fl.load(self.tmp)
        f = next(f for f in data["features"] if f["id"] == "feat-default")
        self.assertEqual(f["kind"], "feature")

    # ---- step 5: round-trip ----

    def test_round_trip_preserves_all_five_fields(self):
        fl.add(
            self.tmp,
            feature_id="feat-bug",
            category="functional",
            description="a bugfix",
            kind="bugfix",
            fixes="feat-existing",
            implementation_model="anthropic-claude-sonnet",
        )
        # Reload, save, reload — values must survive two cycles.
        fl.save(self.tmp, fl.load(self.tmp))
        feat = next(
            f for f in fl.load(self.tmp)["features"] if f["id"] == "feat-bug"
        )
        self.assertEqual(feat["kind"], "bugfix")
        self.assertEqual(feat["fixes"], "feat-existing")
        self.assertEqual(feat["implementation_model"], "anthropic-claude-sonnet")

    # ---- step 6: old-format loads with kind=feature ----

    def test_old_format_loads_with_kind_default(self):
        # Rewrite the fixture without the kind field.
        sample = json.loads(self.tmp.read_text(encoding="utf-8"))
        for f in sample["features"]:
            f.pop("kind", None)
            f.pop("fixes", None)
            f.pop("enhances", None)
            f.pop("superseded_by", None)
            f.pop("implementation_model", None)
        self.tmp.write_text(json.dumps(sample, indent=2), encoding="utf-8")

        data = fl.load(self.tmp)
        feat = data["features"][0]
        self.assertEqual(feat["kind"], "feature")
        self.assertIsNone(feat["fixes"])
        self.assertIsNone(feat["enhances"])
        self.assertIsNone(feat["superseded_by"])
        self.assertIsNone(feat["implementation_model"])

    # ---- step 7: save() + load() round-trip is stable ----

    def test_save_load_byte_identical_for_extended_feature(self):
        # Add a feature with every extended field populated.
        fl.add(
            self.tmp,
            feature_id="feat-full",
            category="functional",
            description="fully populated",
            kind="bugfix",
            fixes="feat-existing",
            implementation_model="openai-gpt-4",
        )
        before = self.tmp.read_bytes()
        # Round-trip via the library.
        fl.save(self.tmp, fl.load(self.tmp))
        after = self.tmp.read_bytes()
        # After the first load+save the on-disk schema gained the five
        # extended fields (and schema_version). A second load+save must
        # be a no-op.
        self.assertEqual(before, after)

    # ---- superseded_by validation + next_feature skip (D-054) ----

    def test_add_superseded_by_accepts_comma_separated_string(self):
        # First add a target feature so superseded_by can reference it.
        fl.add(self.tmp, feature_id="feat-replaced", category="functional", description="r")
        fl.add(
            self.tmp,
            feature_id="feat-replacer",
            category="functional",
            description="new",
            superseded_by="feat-replaced",
        )
        data = fl.load(self.tmp)
        replacer = next(f for f in data["features"] if f["id"] == "feat-replacer")
        self.assertEqual(replacer["superseded_by"], ["feat-replaced"])

    def test_add_superseded_by_rejects_unknown_id(self):
        with self.assertRaises(SystemExit):
            fl.add(
                self.tmp,
                feature_id="feat-x",
                category="functional",
                description="x",
                superseded_by=["feat-missing"],
            )

    def test_add_superseded_by_rejects_self(self):
        with self.assertRaises(SystemExit):
            fl.add(
                self.tmp,
                feature_id="feat-self",
                category="functional",
                description="self-supersede",
                superseded_by=["feat-self"],
            )

    def test_next_feature_skips_superseded_features(self):
        # feat-existing is pending. Add a superseded candidate first
        # (alphabetically earlier). next_feature must skip it.
        fl.add(
            self.tmp,
            feature_id="feat-aaa-superseded",
            category="functional",
            description="superseded; should be skipped",
            superseded_by="feat-existing",
        )
        fl.add(
            self.tmp,
            feature_id="feat-zzz-not-superseded",
            category="functional",
            description="normal; should be picked",
        )
        # Make sure deps_passing is satisfied for both.
        chosen = fl.next_feature(self.tmp)
        # feat-aaa-superseded is alphabetically first but must be skipped.
        self.assertNotEqual(chosen, "feat-aaa-superseded")
        self.assertIn(chosen, {"feat-existing", "feat-zzz-not-superseded"})


class TestBackwardCompat(unittest.TestCase):
    """feat-016 — HARNESS-format feature_list.json still loads + saves cleanly.

    Covers every step in the feat-016 spec:

      1. Fixture file (tests/fixtures/old-harness-format.json) carries the
         pre-v0.1 schema: no top-level `schema_version`, no per-feature
         `kind` / `fixes` / `enhances` / `superseded_by` /
         `implementation_model`.
      2. `load()` succeeds; every feature is readable with `kind` defaulting
         to "feature" and the other four extended fields defaulting to None.
      3. `save()` promotes the file: on-disk `schema_version == 1` and every
         feature gains the five extended fields (serialized as null where
         unset so the on-disk schema is always complete).
      4. The HARNESS CLI (`feature_list.py status`) reads the migrated file
         and reports metadata counts that match the actual feature list.

    This class never touches the live repo files; every test writes a copy
    of the fixture into a temp file so it can mutate freely.
    """

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "old-harness-format.json"

    def setUp(self):
        if not self.FIXTURE.exists():
            self.skipTest(f"fixture missing: {self.FIXTURE}")
        self.tmp = Path(sys.argv[0]).parent / "_test_backward_compat.json"
        # Copy the fixture verbatim so each test starts from a clean legacy file.
        self.tmp.write_bytes(self.FIXTURE.read_bytes())

    def tearDown(self):
        if self.tmp.exists():
            self.tmp.unlink()

    # ---- step 1+2: legacy file loads cleanly with extended-field defaults ----

    def test_legacy_fixture_has_no_schema_version_and_no_extended_fields(self):
        """Sanity: the fixture really is in the pre-v0.1 shape.

        If a future contributor adds the new fields to the fixture, this
        test fails and signals the fixture needs to be regenerated to
        actually exercise the backfill path.
        """
        raw = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        self.assertNotIn(
            "schema_version",
            raw,
            "fixture must not carry schema_version — that defeats the test",
        )
        for feat in raw["features"]:
            for field in ("kind", "fixes", "enhances", "superseded_by", "implementation_model"):
                self.assertNotIn(
                    field,
                    feat,
                    f"feature {feat.get('id')!r} must not carry {field!r} in the legacy fixture",
                )

    def test_load_backfills_extended_field_defaults(self):
        """step 2: load() returns a dict where every feature has the five
        extended fields with their default values (kind=feature, others=None).
        """
        data = fl.load(self.tmp)
        self.assertEqual(len(data["features"]), 3)
        # In-memory schema_version is the legacy sentinel 0; the next save()
        # promotes it to SCHEMA_VERSION.
        self.assertEqual(data["schema_version"], 0)
        for feat in data["features"]:
            self.assertEqual(feat["kind"], "feature", f"kind default for {feat['id']!r}")
            self.assertIsNone(feat["fixes"], f"fixes default for {feat['id']!r}")
            self.assertIsNone(feat["enhances"], f"enhances default for {feat['id']!r}")
            self.assertIsNone(feat["superseded_by"], f"superseded_by default for {feat['id']!r}")
            self.assertIsNone(feat["implementation_model"], f"implementation_model default for {feat['id']!r}")
            # Pre-existing fields must be preserved verbatim.
            self.assertIn("id", feat)
            self.assertIn("category", feat)
            self.assertIn("status", feat)
            self.assertIn("steps", feat)

    # ---- step 3: save() migrates the on-disk schema to v0.1 ----

    def test_save_promotes_schema_version_and_writes_extended_fields(self):
        """step 3: after load+save, the on-disk schema is v0.1.
        - top-level schema_version == SCHEMA_VERSION (= 1)
        - every feature serializes the five extended fields (null when unset)
        """
        data = fl.load(self.tmp)
        fl.save(self.tmp, data)

        written = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(written["schema_version"], fl.SCHEMA_VERSION)
        for feat in written["features"]:
            self.assertIn("kind", feat)
            self.assertIn("fixes", feat)
            self.assertIn("enhances", feat)
            self.assertIn("superseded_by", feat)
            self.assertIn("implementation_model", feat)
            # Original fields are preserved.
            self.assertEqual(feat["category"], "functional" if feat["id"] != "feat-legacy-003" else "ui")

    def test_save_then_load_round_trip_is_stable(self):
        """Round-trip is a no-op after the first save(): the on-disk file
        already carries the v0.1 schema, so a second load+save must produce
        byte-identical output. This is the durable invariant the library
        must preserve.
        """
        data = fl.load(self.tmp)
        fl.save(self.tmp, data)
        before = self.tmp.read_bytes()
        fl.save(self.tmp, fl.load(self.tmp))
        after = self.tmp.read_bytes()
        self.assertEqual(before, after)

    # ---- step 4: HARNESS-side status command reads the migrated file ----

    def test_harness_cli_status_reports_correct_metadata_after_migration(self):
        """step 4: pipe the (now-migrated) file through HARNESS/tools/feature_list.py
        status and assert the counters match the actual feature list. This
        verifies the HARNESS CLI works against v0.1-migrated legacy files
        without modification.

        The HARNESS CLI resolves its target path via `_resolve_path(None)`,
        which returns the module-level `DEFAULT_PATH` constant. To exercise
        cmd_status against our temp file we monkey-patch the constant for
        the duration of the call. This is the same path the CLI walks when
        it is invoked from the repo root against the live feature list.
        """
        # Migrate the file first so we exercise the v0.1-on-disk path.
        data = fl.load(self.tmp)
        fl.save(self.tmp, data)

        import io
        import contextlib

        from HARNESS.tools import feature_list as harness_fl

        # cmd_status delegates to load_features() (alias for fl.load) which
        # reads from DEFAULT_PATH when called with no argument. Patch the
        # constant in BOTH the library and the CLI shim so the lookup hits
        # our tmp file regardless of which module cmd_status reaches into.
        original_default = fl.DEFAULT_PATH
        original_cli_path = harness_fl.FEATURE_LIST_PATH
        fl.DEFAULT_PATH = self.tmp
        harness_fl.FEATURE_LIST_PATH = self.tmp
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                harness_fl.cmd_status(argparse_namespace_stub())  # type: ignore[arg-type]
            output = buf.getvalue()
        finally:
            fl.DEFAULT_PATH = original_default
            harness_fl.FEATURE_LIST_PATH = original_cli_path

        # Parse the `key: value` lines from cmd_status output.
        parsed: dict[str, str] = {}
        for line in output.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()

        # The migrated file has 3 features (1 passing + 1 pending + 1 deferred).
        self.assertEqual(parsed.get("total_features"), "3")
        self.assertEqual(parsed.get("passing"), "1")
        self.assertEqual(parsed.get("in_progress"), "0")
        self.assertEqual(parsed.get("blocked"), "0")
        self.assertEqual(parsed.get("deferred"), "1")
        # failing = total - passing per the HARNESS contract.
        self.assertEqual(parsed.get("failing"), "2")


def argparse_namespace_stub():
    """Return an empty argparse.Namespace.

    cmd_status (and the other HARNESS CLI command functions) ignore their
    `args` parameter — they pull everything off module-level globals. This
    stub exists only to satisfy the call signature without dragging in
    argparse machinery for a single test.
    """
    import argparse

    return argparse.Namespace()


if __name__ == "__main__":
    unittest.main()