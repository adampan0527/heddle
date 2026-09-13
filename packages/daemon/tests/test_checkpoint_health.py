# SPDX-License-Identifier: Apache-2.0
"""Tests for ``is_checkpoint_db_healthy`` — feat-024 (D-051).

Covers:
  * Missing project_path -> unhealthy (False, via fail-closed catch)
  * Missing ``.heddle/`` directory -> unhealthy (False)
  * Missing ``checkpoints.db`` file -> unhealthy (False)
  * Empty / corrupt / non-SQLite file -> unhealthy (False)
  * Healthy LangGraph schema -> healthy (True)
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from heddle_daemon.checkpointing import (
    DEFAULT_DB_FILENAME,
    ProjectCheckpointStore,
    is_checkpoint_db_healthy,
)


class _TempProject:
    """Context manager for an isolated tmp project dir."""

    def __init__(self) -> None:
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="heddle_health_test_"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)


def _run(coro):
    """Tiny helper to drive a coroutine from a sync test method.

    Creates a fresh event loop, runs the coroutine, then closes the
    loop. Avoids the asyncio.get_event_loop() quirk where the
    implicitly-created loop accumulates state across tests in a long
    pytest run.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestIsCheckpointDbHealthy(unittest.TestCase):
    """Healthy / unhealthy discrimination for the project checkpoint DB."""

    def test_missing_project_path_is_unhealthy(self):
        """A non-existent project_path -> False (fail-closed)."""
        with _TempProject() as proj:
            self.assertFalse(is_checkpoint_db_healthy(proj / "does" / "not" / "exist"))

    def test_missing_heddle_dir_is_unhealthy(self):
        """``.heddle/`` never created => no DB to check => unhealthy."""
        with _TempProject() as proj:
            self.assertFalse(is_checkpoint_db_healthy(proj))

    def test_missing_checkpoints_db_is_unhealthy(self):
        """``.heddle/`` exists but ``checkpoints.db`` does not => unhealthy."""
        with _TempProject() as proj:
            (proj / ".heddle").mkdir(parents=True)
            self.assertFalse(is_checkpoint_db_healthy(proj))

    def test_corrupt_non_sqlite_file_is_unhealthy(self):
        """A non-SQLite file masquerading as the DB is unhealthy."""
        with _TempProject() as proj:
            heddle = proj / ".heddle"
            heddle.mkdir(parents=True)
            (heddle / DEFAULT_DB_FILENAME).write_bytes(b"not a sqlite file at all")
            self.assertFalse(is_checkpoint_db_healthy(proj))

    def test_healthy_langgraph_schema_is_healthy(self):
        """A real LangGraph checkpoint DB is healthy."""
        with _TempProject() as proj:
            # Use ProjectCheckpointStore.setup() to write a real DB.
            store = ProjectCheckpointStore(project_path=proj)
            _run(store.setup())
            try:
                self.assertTrue(is_checkpoint_db_healthy(proj))
            finally:
                _run(store.close())

    def test_handcrafted_healthy_sqlite_db_is_healthy(self):
        """A minimal hand-written SQLite DB that passes integrity_check is healthy."""
        with _TempProject() as proj:
            heddle = proj / ".heddle"
            heddle.mkdir(parents=True)
            db_path = heddle / DEFAULT_DB_FILENAME
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("CREATE TABLE t (x INTEGER)")
                conn.execute("INSERT INTO t VALUES (1)")
                conn.commit()
            self.assertTrue(is_checkpoint_db_healthy(proj))

    def test_corrupted_db_pages_is_unhealthy(self):
        """A SQLite DB with corrupt pages fails integrity_check."""
        with _TempProject() as proj:
            heddle = proj / ".heddle"
            heddle.mkdir(parents=True)
            db_path = heddle / DEFAULT_DB_FILENAME
            # Make a healthy DB first, then scribble garbage over
            # the middle of it so integrity_check has something to
            # complain about.
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("CREATE TABLE t (x INTEGER, data BLOB)")
                # Insert a row with a sizeable payload so the file has
                # actual content to corrupt.
                conn.execute("INSERT INTO t VALUES (1, ?)", (b"x" * 4096,))
                conn.commit()
            raw = db_path.read_bytes()
            mid = len(raw) // 2
            corrupted = raw[:mid] + b"\x00\x00\x00\x00garbage" * 200 + raw[mid:]
            db_path.write_bytes(corrupted)
            self.assertFalse(is_checkpoint_db_healthy(proj))


if __name__ == "__main__":
    unittest.main()
