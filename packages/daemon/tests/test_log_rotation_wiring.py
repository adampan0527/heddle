# SPDX-License-Identifier: Apache-2.0
"""Tests for the daemon's feat-015 per-project rotating log sink wiring.

Covers the contract:

    * ``start()`` with a matching registered project attaches a sink
      at ``~/.heddle/logs/<project_id>.daemon.log``;
    * ``start()`` with no project_path does NOT attach (skeleton mode);
    * ``start()`` with a project_path that matches no registered project
      emits a ``project_log_sink_skipped`` warn event and continues;
    * the env-var override path (HEDDLE_LOG_MAX_BYTES) flows through to
      the attached sink;
    * ``stop()`` closes the sink + detaches the module-level sink;
    * ``_on_project_removed`` closes the sink BEFORE signaling stop
      events (so a stale handle cannot leak past project removal).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from heddle_common import logging as hlog
from heddle_common.log_rotation import DEFAULT_LOG_MAX_BYTES, RotatingFileSink
from heddle_common.projects_io import (
    DEFAULT_PROJECTS_PATH,
    Project,
    default_projects_path,
    save_projects,
)

from heddle_daemon.server import DAEMON_LOG_SUFFIX, DEFAULT_LOGS_DIR, Daemon, DaemonConfig

# ---------- helpers ----------


@contextmanager
def _projects_registry(registry_path: Path, projects: list[Project]) -> Iterator[None]:
    """Point ``heddle_common.projects_io`` at an isolated projects.json file.

    ``heddle_common.projects_io`` reads ``~/.heddle/projects.json`` by
    default; tests force it to a temp file by monkey-patching
    ``DEFAULT_PROJECTS_PATH`` for the duration of the test.
    """
    save_projects(registry_path, {p.id: p for p in projects})
    saved_default = DEFAULT_PROJECTS_PATH
    import heddle_common.projects_io as _pio

    object.__setattr__(_pio, "DEFAULT_PROJECTS_PATH", str(registry_path))
    try:
        yield
    finally:
        object.__setattr__(_pio, "DEFAULT_PROJECTS_PATH", saved_default)


@contextmanager
def _isolated_home(home_dir: Path) -> Iterator[None]:
    """Redirect ``~/.heddle`` to ``home_dir`` for the test's duration."""
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home_dir)
    # ``Path("~/...").expanduser()`` reads ``HOME``; we also need to
    # patch ``DEFAULT_LOGS_DIR`` because it is interpolated at module
    # import time as a literal string, not at runtime.
    saved_logs_default = DEFAULT_LOGS_DIR
    import heddle_daemon.server as _server

    object.__setattr__(_server, "DEFAULT_LOGS_DIR", str(home_dir / "logs"))
    try:
        yield
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        object.__setattr__(_server, "DEFAULT_LOGS_DIR", saved_logs_default)


def _project_stub(path: Path) -> Project:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Project(
        id="test-proj-id-0011",
        name="log-rotation-test",
        path=str(path),
        added_at=now,
        last_accessed_at=now,
    )


@contextmanager
def _capture_stderr() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    with patch.object(sys, "stderr", buf):
        yield buf


@contextmanager
def _sink_isolation() -> Iterator[None]:
    """Reset module-level sink + env vars between tests."""
    hlog.detach_file_sink()
    for var in ("HEDDLE_LOG_MAX_BYTES", "HEDDLE_LOG_BACKUP_COUNT"):
        os.environ.pop(var, None)
    try:
        yield
    finally:
        hlog.detach_file_sink()
        for var in ("HEDDLE_LOG_MAX_BYTES", "HEDDLE_LOG_BACKUP_COUNT"):
            os.environ.pop(var, None)


# ---------- tests ----------


class TestStartAttachesSink(unittest.IsolatedAsyncioTestCase):
    """``Daemon.start()`` attaches a sink when a matching project is registered."""

    async def test_sink_attached_on_start_with_matching_project(self):
        with _sink_isolation():
            from tempfile import TemporaryDirectory

            with TemporaryDirectory(prefix="heddle_daemon_log_") as tmp:
                tmp_path = Path(tmp)
                proj_dir = tmp_path / "project"
                proj_dir.mkdir()
                proj = _project_stub(proj_dir.resolve())
                registry = tmp_path / "projects.json"
                with _isolated_home(tmp_path):
                    with _projects_registry(registry, [proj]):
                        cfg = DaemonConfig(
                            host="127.0.0.1", port=0, project_path=proj_dir.resolve()
                        )
                        daemon = Daemon(cfg)
                        try:
                            await daemon.start()
                            self.assertIsNotNone(daemon._log_sink)
                            self.assertIsInstance(daemon._log_sink, RotatingFileSink)
                            # The log file path must follow the
                            # ``<project_id>.daemon.log`` contract.
                            expected_path = (
                                tmp_path / "logs" / f"{proj.id}{DAEMON_LOG_SUFFIX}"
                            )
                            self.assertEqual(
                                daemon._log_sink.path.resolve(),
                                expected_path.resolve(),
                            )
                        finally:
                            await daemon.stop()

    async def test_sink_attached_uses_env_var_override(self):
        with _sink_isolation():
            from tempfile import TemporaryDirectory

            with TemporaryDirectory(prefix="heddle_daemon_log_") as tmp:
                tmp_path = Path(tmp)
                proj_dir = tmp_path / "project"
                proj_dir.mkdir()
                proj = _project_stub(proj_dir.resolve())
                registry = tmp_path / "projects.json"
                os.environ["HEDDLE_LOG_MAX_BYTES"] = "4096"
                os.environ["HEDDLE_LOG_BACKUP_COUNT"] = "2"
                try:
                    with _isolated_home(tmp_path):
                        with _projects_registry(registry, [proj]):
                            cfg = DaemonConfig(
                                host="127.0.0.1",
                                port=0,
                                project_path=proj_dir.resolve(),
                            )
                            daemon = Daemon(cfg)
                            try:
                                await daemon.start()
                                self.assertEqual(daemon._log_sink.max_bytes, 4096)
                                self.assertEqual(daemon._log_sink.backup_count, 2)
                            finally:
                                await daemon.stop()
                finally:
                    os.environ.pop("HEDDLE_LOG_MAX_BYTES", None)
                    os.environ.pop("HEDDLE_LOG_BACKUP_COUNT", None)


class TestStartNoSinkOnSkeletonMode(unittest.IsolatedAsyncioTestCase):
    """No project_path → no sink; daemon still serves."""

    async def test_no_attach_when_project_path_is_none(self):
        with _sink_isolation():
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=None)
            daemon = Daemon(cfg)
            try:
                with _capture_stderr():
                    await daemon.start()
                self.assertIsNone(daemon._log_sink)
            finally:
                await daemon.stop()


class TestStartNoSinkWhenProjectUnregistered(unittest.IsolatedAsyncioTestCase):
    """Project_path set but no matching registered project → warn + continue."""

    async def test_warn_and_continue_when_project_id_resolution_fails(self):
        with _sink_isolation():
            from tempfile import TemporaryDirectory

            with TemporaryDirectory(prefix="heddle_daemon_log_") as tmp:
                tmp_path = Path(tmp)
                proj_dir = tmp_path / "project"
                proj_dir.mkdir()
                # Registry is empty — no project registered.
                registry = tmp_path / "projects.json"
                with _isolated_home(tmp_path):
                    with _projects_registry(registry, []):
                        cfg = DaemonConfig(
                            host="127.0.0.1",
                            port=0,
                            project_path=proj_dir.resolve(),
                        )
                        daemon = Daemon(cfg)
                        try:
                            with _capture_stderr() as buf:
                                await daemon.start()
                            self.assertIsNone(daemon._log_sink)
                            text = buf.getvalue()
                            self.assertIn("project_log_sink_skipped", text)
                            self.assertIn("no_matching_project", text)
                        finally:
                            await daemon.stop()


class TestStopClosesSink(unittest.IsolatedAsyncioTestCase):
    """``Daemon.stop()`` closes the per-project sink."""

    async def test_sink_closed_on_stop(self):
        with _sink_isolation():
            from tempfile import TemporaryDirectory

            with TemporaryDirectory(prefix="heddle_daemon_log_") as tmp:
                tmp_path = Path(tmp)
                proj_dir = tmp_path / "project"
                proj_dir.mkdir()
                proj = _project_stub(proj_dir.resolve())
                registry = tmp_path / "projects.json"
                with _isolated_home(tmp_path):
                    with _projects_registry(registry, [proj]):
                        cfg = DaemonConfig(
                            host="127.0.0.1",
                            port=0,
                            project_path=proj_dir.resolve(),
                        )
                        daemon = Daemon(cfg)
                        await daemon.start()
                        sink = daemon._log_sink
                        self.assertIsNotNone(sink)
                        await daemon.stop()
                        # After stop, the daemon-side sink object
                        # is cleared; the module-level sink is also
                        # detached so a stale reference cannot emit
                        # into a closed file handle.
                        self.assertIsNone(daemon._log_sink)
                        self.assertIsNone(hlog._file_sink)


class TestProjectRemovedClosesSink(unittest.IsolatedAsyncioTestCase):
    """``_on_project_removed`` closes the sink BEFORE signaling stop events."""

    async def test_sink_closed_on_project_removal(self):
        with _sink_isolation():
            from tempfile import TemporaryDirectory

            with TemporaryDirectory(prefix="heddle_daemon_log_") as tmp:
                tmp_path = Path(tmp)
                proj_dir = tmp_path / "project"
                proj_dir.mkdir()
                proj = _project_stub(proj_dir.resolve())
                registry = tmp_path / "projects.json"
                with _isolated_home(tmp_path):
                    with _projects_registry(registry, [proj]):
                        cfg = DaemonConfig(
                            host="127.0.0.1",
                            port=0,
                            project_path=proj_dir.resolve(),
                        )
                        daemon = Daemon(cfg)
                        try:
                            await daemon.start()
                            self.assertIsNotNone(daemon._log_sink)
                            await daemon._on_project_removed(proj)
                            # Sink closed; module-level state detached.
                            self.assertIsNone(daemon._log_sink)
                            self.assertIsNone(hlog._file_sink)
                        finally:
                            await daemon.stop()


if __name__ == "__main__":
    unittest.main()