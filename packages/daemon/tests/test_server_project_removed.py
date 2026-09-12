# SPDX-License-Identifier: Apache-2.0
"""Tests for ``Daemon._on_project_removed`` — feat-014 cascade hook.

The hook is what ``project_cascade.remove_project_with_cascade`` calls
*after* mutating the projects.json registry and *before* deleting the
on-disk log files. It must:

    * signal every in-flight ``AgentRuntime`` thread's stop event so
      any active run raises ``FeatureAbortedError`` promptly;
    * close the per-project LangGraph checkpoint store so the daemon
      stops writing to ``<project>/.heddle/checkpoints.db``.

The hook does NOT close the WS server itself; that is ``Daemon.stop()``
responsibility. v0.1 is single-project so the test exercises a single
daemon with one project.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from heddle_common.projects_io import Project

from heddle_daemon.server import Daemon, DaemonConfig


# ---------- helpers ----------


class _TempProject:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="heddle_proj_removed_"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _project_stub() -> Project:
    """Build a Project dataclass; only ``.id`` is read by the hook."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Project(
        id="test-project-id-0000",
        name="proj-removed-test",
        path="/tmp/anything",
        added_at=now,
        last_accessed_at=now,
    )


# ---------- tests ----------


class TestOnProjectRemovedClosesCheckpoint(unittest.IsolatedAsyncioTestCase):
    """The hook must close the per-project checkpoint store."""

    async def test_closes_checkpoint_store_after_project_removal(self):
        with _TempProject() as proj:
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            try:
                await daemon.start()
                self.assertIsNotNone(daemon.checkpoint_store)
                self.assertTrue(daemon.checkpoint_store.is_setup)
                await daemon._on_project_removed(_project_stub())
                self.assertIsNone(daemon.checkpoint_store)
            finally:
                await daemon.stop()


class TestOnProjectRemovedSetsStopEvents(unittest.IsolatedAsyncioTestCase):
    """The hook must set every in-flight thread's stop event."""

    async def test_sets_every_in_flight_stop_event(self):
        cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=None)
        daemon = Daemon(cfg)
        try:
            daemon._feature_stop_events = {
                "feat-001": asyncio.Event(),
                "feat-002": asyncio.Event(),
                "feat-003": asyncio.Event(),
            }
            self.assertFalse(
                any(e.is_set() for e in daemon._feature_stop_events.values())
            )
            await daemon._on_project_removed(_project_stub())
            self.assertTrue(
                all(e.is_set() for e in daemon._feature_stop_events.values())
            )
        finally:
            await daemon.stop()

    async def test_empty_feature_dict_is_safe(self):
        """No in-flight features: hook is a no-op (besides clearing)."""
        cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=None)
        daemon = Daemon(cfg)
        try:
            self.assertEqual(daemon._feature_stop_events, {})
            await daemon._on_project_removed(_project_stub())
            self.assertEqual(daemon._feature_stop_events, {})
        finally:
            await daemon.stop()


class TestOnProjectRemovedPreservesServer(unittest.IsolatedAsyncioTestCase):
    """The hook must NOT close the WS server itself."""

    async def test_ws_server_remains_bound_after_hook(self):
        with _TempProject() as proj:
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            try:
                await daemon.start()
                bound_port = daemon.bound_port
                self.assertIsNotNone(bound_port)
                self.assertIsNotNone(daemon._server)
                await daemon._on_project_removed(_project_stub())
                self.assertIsNotNone(daemon._server)
                self.assertEqual(daemon.bound_port, bound_port)
            finally:
                await daemon.stop()


if __name__ == "__main__":
    unittest.main()