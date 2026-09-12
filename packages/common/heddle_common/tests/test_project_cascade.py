# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``heddle_common.project_cascade`` — feat-014."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from heddle_common import (
    Project,
    ProjectsError,
    default_logs_dir,
    remove_project_with_cascade,
)
from heddle_common.projects_io import add_project, load_projects

# Project name (used by add_project → projects_io). On Windows the
# basename resolution strips trailing slashes; a flat name is safe.
_PROJ_NAME = "heddle-cascade-test"


class _TempLogs:
    """Isolated ``~/.heddle/logs`` replacement for a single test.

    Implemented as a context manager that swaps the cascade module's
    ``DEFAULT_LOGS_DIR`` by passing an explicit ``log_dir=`` to
    ``remove_project_with_cascade``. The home directory itself is
    never touched — the cascade is fully parameterized.
    """

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="heddle_logs_test_"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _new_projects_file() -> Path:
    """Return a path to an empty ``projects.json`` we can write into."""
    return Path(tempfile.mkdtemp(prefix="heddle_proj_test_")) / "projects.json"


def _register(projects_file: Path, project_id: str | None = None) -> Project:
    """Register a throwaway project into ``projects_file`` and return it."""
    proj_dir = Path(tempfile.mkdtemp(prefix="heddle_cascade_proj_"))
    proj = add_project(
        projects_file,
        project_path=str(proj_dir),
        name=_PROJ_NAME,
        project_id=project_id,
    )
    return proj


def _run(coro: object) -> object:
    """Run an awaitable under a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)  # type: ignore[arg-type]
    finally:
        loop.close()


class TestCascadeDeletesArtifacts(unittest.TestCase):
    """The headline scenarios from feat-014's steps array."""

    def test_cascade_deletes_log_file_and_dir_and_registry(self) -> None:
        projects_file = _new_projects_file()
        proj = _register(projects_file)
        with _TempLogs() as logs:
            (logs / f"{proj.id}.log").write_text("pretend this is a stderr mirror")
            (logs / proj.id).mkdir()
            (logs / proj.id / "llm-audit.jsonl").write_text("{}\n")
            self.assertTrue((logs / f"{proj.id}.log").exists())
            self.assertTrue((logs / proj.id).exists())

            removed = _run(
                remove_project_with_cascade(
                    projects_file,
                    proj.id,
                    log_dir=logs,
                )
            )

            self.assertIsNotNone(removed)
            self.assertEqual(removed.id, proj.id)
            # Registry entry is gone.
            self.assertNotIn(proj.id, load_projects(projects_file))
            # Log file + log dir are gone.
            self.assertFalse((logs / f"{proj.id}.log").exists())
            self.assertFalse((logs / proj.id).exists())

    def test_cascade_is_noop_when_id_absent(self) -> None:
        projects_file = _new_projects_file()
        # No registration — id is guaranteed absent.
        removed = _run(
            remove_project_with_cascade(
                projects_file,
                "ghost-project-id",
            )
        )
        self.assertIsNone(removed)
        # File may not even exist; the cascade must not crash either way.
        if projects_file.exists():
            self.assertNotIn("ghost-project-id", load_projects(projects_file))


class TestCascadeHook(unittest.TestCase):
    """The cascade must call on_remove with the removed Project."""

    def test_cascade_calls_on_remove_with_removed_project(self) -> None:
        projects_file = _new_projects_file()
        proj = _register(projects_file)

        seen: list[Project] = []

        async def hook(removed: Project) -> None:
            seen.append(removed)

        _run(
            remove_project_with_cascade(
                projects_file, proj.id, on_remove=hook,
                log_dir=Path(tempfile.mkdtemp(prefix="isolated_logs_")),
            )
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].id, proj.id)
        self.assertEqual(seen[0].name, proj.name)

    def test_cascade_does_not_call_hook_when_id_absent(self) -> None:
        projects_file = _new_projects_file()
        seen: list[Project] = []

        async def hook(removed: Project) -> None:
            seen.append(removed)

        result = _run(
            remove_project_with_cascade(
                projects_file, "ghost", on_remove=hook
            )
        )
        self.assertIsNone(result)
        self.assertEqual(seen, [])

    def test_cascade_on_remove_failure_propagates_as_ProjectsError(self) -> None:
        projects_file = _new_projects_file()
        proj = _register(projects_file)
        with _TempLogs() as logs:
            log_path = logs / f"{proj.id}.log"
            log_path.write_text("must NOT be deleted")

            async def boom(removed: Project) -> None:
                raise RuntimeError("hook intentionally failed")

            with self.assertRaises(ProjectsError) as ctx:
                _run(
                    remove_project_with_cascade(
                        projects_file, proj.id, on_remove=boom, log_dir=logs
                    )
                )
            # Registry is already gone (no rollback), but the log file
            # is left intact because the hook ran before deletion.
            self.assertNotIn(proj.id, load_projects(projects_file))
            self.assertTrue(log_path.exists())
            self.assertIn("hook intentionally failed", str(ctx.exception))


class TestCascadeMissingArtifacts(unittest.TestCase):
    """Best-effort deletion: missing files are not errors."""

    def test_cascade_handles_missing_log_file(self) -> None:
        projects_file = _new_projects_file()
        proj = _register(projects_file)
        with _TempLogs() as logs:
            # Note: NO log file written, NO log dir created.
            removed = _run(
                remove_project_with_cascade(
                    projects_file, proj.id, log_dir=logs
                )
            )
            self.assertIsNotNone(removed)
            self.assertNotIn(proj.id, load_projects(projects_file))

    def test_cascade_handles_missing_log_dir_entirely(self) -> None:
        projects_file = _new_projects_file()
        proj = _register(projects_file)
        # log_dir points at a path that has never been created.
        missing_logs = Path(tempfile.mkdtemp(prefix="heddle_missing_logs_"))
        shutil.rmtree(missing_logs)
        self.assertFalse(missing_logs.exists())
        removed = _run(
            remove_project_with_cascade(
                projects_file, proj.id, log_dir=missing_logs
            )
        )
        self.assertIsNotNone(removed)
        self.assertNotIn(proj.id, load_projects(projects_file))


class TestCascadeIsolation(unittest.TestCase):
    """Removing one project must not touch another project's logs."""

    def test_cascade_does_not_touch_other_projects_log_files(self) -> None:
        projects_file = _new_projects_file()
        keep = _register(projects_file)
        drop = _register(projects_file)
        self.assertNotEqual(keep.id, drop.id)

        with _TempLogs() as logs:
            keep_log = logs / f"{keep.id}.log"
            keep_subdir = logs / keep.id
            keep_log.write_text("keep me\n")
            (keep_subdir).mkdir()
            (keep_subdir / "audit.jsonl").write_text("keep me\n")

            drop_log = logs / f"{drop.id}.log"
            drop_subdir = logs / drop.id
            drop_log.write_text("drop me\n")
            (drop_subdir).mkdir()
            (drop_subdir / "audit.jsonl").write_text("drop me\n")

            _run(
                remove_project_with_cascade(
                    projects_file, drop.id, log_dir=logs
                )
            )

            self.assertFalse(drop_log.exists())
            self.assertFalse(drop_subdir.exists())
            self.assertTrue(keep_log.exists())
            self.assertTrue(keep_subdir.exists())
            self.assertTrue((keep_subdir / "audit.jsonl").exists())
            self.assertEqual(keep_log.read_text(), "keep me\n")


class TestDefaultLogsDir(unittest.TestCase):
    """``default_logs_dir()`` honors ``~/.heddle/logs``."""

    def test_default_logs_dir_ends_with_heddle_logs(self) -> None:
        d = default_logs_dir()
        self.assertTrue(str(d).replace("\\", "/").endswith(".heddle/logs"))

    def test_default_logs_dir_constant_matches(self) -> None:
        from heddle_common.project_cascade import DEFAULT_LOGS_DIR
        self.assertEqual(DEFAULT_LOGS_DIR, "~/.heddle/logs")


if __name__ == "__main__":
    unittest.main()