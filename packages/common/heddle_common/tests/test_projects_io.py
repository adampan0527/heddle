# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_common.projects_io — feat-012."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from heddle_common import projects_io as pi

# Common path used by every test to override the default ~/.heddle location.
TMP_ROOT = Path(sys.argv[0]).resolve().parent


class TestProjectsIO(unittest.TestCase):
    """feat-012 / D-057 — ~/.heddle/projects.json registry."""

    def setUp(self):
        # Per-test scratch file under the tests dir (deleted in tearDown).
        self.tmp = TMP_ROOT / "_test_projects_io.json"

    def tearDown(self):
        if self.tmp.exists():
            self.tmp.unlink()

    # ---- helpers ----

    def _scratch_dir(self) -> Path:
        """Return a freshly-created real directory the tests can register.

        Each call creates a new `tempfile.TemporaryDirectory` rooted at
        `TMP_ROOT`; the directory lives until garbage collection at the
        next test boundary. This satisfies add_project's "path must
        exist" invariant without depending on any other test's fixture.
        """
        d = Path(tempfile.mkdtemp(prefix="proj_io_", dir=TMP_ROOT))
        self.addCleanup(lambda: _rm_tree(d))
        return d

    # ---- step 2: loader/writer ----

    def test_load_returns_empty_when_file_missing(self):
        """The registry file may not exist (first-run state)."""
        self.assertFalse(self.tmp.exists())
        result = pi.load_projects(self.tmp)
        self.assertEqual(result, {})

    def test_save_then_load_round_trip(self):
        d = self._scratch_dir()
        proj = pi.add_project(self.tmp, project_path=str(d), name="round-trip")
        loaded = pi.load_projects(self.tmp)
        self.assertIn(proj.id, loaded)
        self.assertEqual(loaded[proj.id].name, "round-trip")
        self.assertEqual(loaded[proj.id].path, str(d))
        self.assertEqual(loaded[proj.id].id, proj.id)

    def test_save_stamps_schema_version(self):
        d = self._scratch_dir()
        pi.add_project(self.tmp, project_path=str(d))
        raw = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], pi.PROJECTS_SCHEMA_VERSION)
        self.assertIn("projects", raw)

    def test_load_refuses_unknown_future_version(self):
        """Mirrors feature_list_io's schema_version rejection policy."""
        self.tmp.write_text(
            json.dumps(
                {"version": pi.PROJECTS_SCHEMA_VERSION_MAX + 100, "projects": []}
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(pi.ProjectsError) as ctx:
            pi.load_projects(self.tmp)
        msg = str(ctx.exception)
        self.assertIn(str(pi.PROJECTS_SCHEMA_VERSION_MAX), msg)
        self.assertIn("upgrade heddle", msg.lower())

    def test_load_legacy_file_without_version_works(self):
        """Pre-versioned file (no `version` key) loads with version=0 sentinel."""
        d = self._scratch_dir()
        legacy_payload = {
            "projects": [
                {
                    "id": "abc-123",
                    "name": "legacy",
                    "path": str(d),
                    "added_at": "2026-07-15T00:00:00Z",
                    "last_accessed_at": "2026-07-15T00:00:00Z",
                }
            ]
        }
        self.tmp.write_text(json.dumps(legacy_payload) + "\n", encoding="utf-8")
        loaded = pi.load_projects(self.tmp)
        self.assertIn("abc-123", loaded)
        self.assertEqual(loaded["abc-123"].name, "legacy")

    def test_load_rejects_non_integer_version(self):
        self.tmp.write_text(
            json.dumps({"version": "1", "projects": []}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(pi.ProjectsError):
            pi.load_projects(self.tmp)

    # ---- step 3: add_project ----

    def test_add_project_validates_path_exists(self):
        """add_project refuses a path that is not an existing directory."""
        bogus = TMP_ROOT / "_definitely_does_not_exist_xyz_12345"
        with self.assertRaises(pi.ProjectsError) as ctx:
            pi.add_project(self.tmp, project_path=str(bogus))
        self.assertIn("does not exist", str(ctx.exception))

    def test_add_project_validates_path_is_directory(self):
        """A path that exists but is a file is also rejected."""
        f = TMP_ROOT / "_a_regular_file_xyz_12345"
        f.write_text("not a directory", encoding="utf-8")
        self.addCleanup(lambda: f.unlink(missing_ok=True))
        with self.assertRaises(pi.ProjectsError):
            pi.add_project(self.tmp, project_path=str(f))

    def test_add_project_computes_uuid_and_defaults_name(self):
        d = self._scratch_dir()
        proj = pi.add_project(self.tmp, project_path=str(d))
        # id is a uuid4 (string of 36 chars including dashes)
        self.assertEqual(len(proj.id), 36)
        self.assertEqual(proj.id.count("-"), 4)
        # name defaults to the directory basename
        self.assertEqual(proj.name, d.name)
        # path is the absolute path
        self.assertEqual(proj.path, str(d.resolve()))

    def test_add_project_records_added_at_and_last_accessed_at(self):
        d = self._scratch_dir()
        proj = pi.add_project(self.tmp, project_path=str(d))
        # both timestamps are ISO 8601 Z-suffixed UTC
        self.assertRegex(proj.added_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(proj.added_at, proj.last_accessed_at)

    def test_add_project_accepts_explicit_name_and_id(self):
        d = self._scratch_dir()
        proj = pi.add_project(
            self.tmp,
            project_path=str(d),
            name="my-special-name",
            project_id="00000000-0000-4000-8000-000000000001",
        )
        self.assertEqual(proj.name, "my-special-name")
        self.assertEqual(proj.id, "00000000-0000-4000-8000-000000000001")

    def test_add_project_rejects_duplicate_id(self):
        d1 = self._scratch_dir()
        d2 = self._scratch_dir()
        proj = pi.add_project(self.tmp, project_path=str(d1))
        with self.assertRaises(pi.ProjectsError):
            pi.add_project(
                self.tmp,
                project_path=str(d2),
                project_id=proj.id,
            )

    def test_add_project_rejects_duplicate_path(self):
        d = self._scratch_dir()
        pi.add_project(self.tmp, project_path=str(d))
        with self.assertRaises(pi.ProjectsError):
            pi.add_project(self.tmp, project_path=str(d))

    def test_add_project_persists_across_reload(self):
        """step 5: add a project; assert projects.json has the entry."""
        d = self._scratch_dir()
        proj = pi.add_project(self.tmp, project_path=str(d), name="feature-list")
        # Reload from disk to confirm it's actually persisted.
        loaded = pi.load_projects(self.tmp)
        self.assertIn(proj.id, loaded)
        self.assertEqual(loaded[proj.id].name, "feature-list")
        self.assertEqual(loaded[proj.id].path, str(d.resolve()))

    # ---- step 4: remove_project ----

    def test_remove_project_returns_removed_entry(self):
        d = self._scratch_dir()
        proj = pi.add_project(self.tmp, project_path=str(d))
        removed = pi.remove_project(self.tmp, proj.id)
        self.assertIsNotNone(removed)
        self.assertEqual(removed.id, proj.id)
        self.assertEqual(removed.name, proj.name)

    def test_remove_project_returns_none_for_unknown_id(self):
        """Removing an id that was never registered is a no-op, not an error."""
        self.assertIsNone(pi.remove_project(self.tmp, "no-such-id"))

    def test_remove_project_drops_entry_from_disk(self):
        """step 6: remove a project; assert projects.json no longer has the entry."""
        d = self._scratch_dir()
        proj = pi.add_project(self.tmp, project_path=str(d))
        pi.remove_project(self.tmp, proj.id)
        loaded = pi.load_projects(self.tmp)
        self.assertNotIn(proj.id, loaded)
        # And the underlying file has no row for it.
        raw = json.loads(self.tmp.read_text(encoding="utf-8"))
        ids = [entry["id"] for entry in raw["projects"]]
        self.assertNotIn(proj.id, ids)

    def test_remove_project_does_not_touch_disk_outside_registry(self):
        """Registry-only side effect; log file cascade is feat-014's job.

        Documents the boundary: this function never deletes anything
        under ~/.heddle/logs/. feat-014 wires the cascade in (and
        ensures the daemon is no longer writing to those files).
        """
        d = self._scratch_dir()
        proj = pi.add_project(self.tmp, project_path=str(d))
        sentinel = TMP_ROOT / "_sentinel_should_not_be_deleted.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        self.addCleanup(lambda: sentinel.unlink(missing_ok=True))
        pi.remove_project(self.tmp, proj.id)
        self.assertTrue(sentinel.exists())

    # ---- touch_project ----

    def test_touch_updates_last_accessed_at(self):
        d = self._scratch_dir()
        proj = pi.add_project(self.tmp, project_path=str(d))
        # Insert a small gap so the new timestamp differs.
        import time as _time

        _time.sleep(1.05)
        updated = pi.touch_project(self.tmp, proj.id)
        self.assertEqual(updated.id, proj.id)
        self.assertEqual(updated.added_at, proj.added_at)
        self.assertNotEqual(updated.last_accessed_at, proj.last_accessed_at)
        self.assertGreater(updated.last_accessed_at, proj.last_accessed_at)

    def test_touch_unknown_id_raises(self):
        with self.assertRaises(pi.ProjectsError):
            pi.touch_project(self.tmp, "no-such-project")

    # ---- list_projects ----

    def test_list_projects_returns_insertion_order(self):
        d1 = self._scratch_dir()
        d2 = self._scratch_dir()
        d3 = self._scratch_dir()
        p1 = pi.add_project(self.tmp, project_path=str(d1))
        p2 = pi.add_project(self.tmp, project_path=str(d2))
        p3 = pi.add_project(self.tmp, project_path=str(d3))
        ids = [p.id for p in pi.list_projects(self.tmp)]
        self.assertEqual(ids, [p1.id, p2.id, p3.id])


def _rm_tree(p: Path) -> None:
    """Best-effort recursive delete for temp dirs created in setUp helpers."""
    import shutil as _shutil

    if p.exists():
        _shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
