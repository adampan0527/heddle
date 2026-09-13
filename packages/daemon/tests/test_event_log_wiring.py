# SPDX-License-Identifier: Apache-2.0
"""Tests for feat-048 structured event-log wiring.

Covers the end-to-end contract:

    * ``Daemon.start()`` with a matching registered project attaches
      an :class:`EventLogLogger` writing to
      ``<logs_dir>/<project_id>/events.jsonl``.
    * ``Daemon.start()`` with no project_path does NOT attach
      (skeleton mode).
    * ``Daemon.start()`` with a project_path that matches no
      registered project emits an ``event_log_skipped`` warn event
      and continues (graceful fallback).
    * ``Daemon.stop()`` closes the event-log writer.
    * A state-changing route handler (e.g. ``start_feature``) emits
      a matching line to ``events.jsonl`` via
      ``RouteHandler.emit_event`` (feat-030 + feat-048 dual-write).
    * Per-project log lines carry the ``{ts, event, project_id,
      feature_id, payload}`` schema exactly matching the WS event
      envelope's body.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

from heddle_common import logging as hlog
from heddle_common.event_log import DEFAULT_EVENT_LOG_FILENAME, EventLogLogger
from heddle_common.projects_io import (
    DEFAULT_PROJECTS_PATH,
    Project,
    save_projects,
)

from heddle_daemon.server import Daemon, DaemonConfig


# ---------- helpers ----------


@contextmanager
def _projects_registry(registry_path: Path, projects: list[Project]) -> Iterator[None]:
    """Point ``heddle_common.projects_io`` at an isolated projects.json file."""
    save_projects(registry_path, {p.id: p for p in projects})
    saved_default = DEFAULT_PROJECTS_PATH
    import heddle_common.projects_io as _pio

    object.__setattr__(_pio, "DEFAULT_PROJECTS_PATH", str(registry_path))
    try:
        yield
    finally:
        object.__setattr__(_pio, "DEFAULT_PROJECTS_PATH", saved_default)


@contextmanager
def _isolated_logs_dir(logs_dir: Path) -> Iterator[None]:
    """Point ``server.DEFAULT_LOGS_DIR`` at an isolated directory."""
    import heddle_daemon.server as _server

    saved = _server.DEFAULT_LOGS_DIR
    object.__setattr__(_server, "DEFAULT_LOGS_DIR", str(logs_dir))
    try:
        yield
    finally:
        object.__setattr__(_server, "DEFAULT_LOGS_DIR", saved)


@contextmanager
def _capture_stderr() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    with patch.object(sys, "stderr", buf):
        yield buf


def _project_stub(path: Path, pid: str = "test-proj-event-1") -> Project:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Project(
        id=pid,
        name="event-log-test",
        path=str(path),
        added_at=now,
        last_accessed_at=now,
    )


def _read_event_lines(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _write_feature_list(project_dir: Path, features: list[dict[str, Any]]) -> Path:
    """Drop a minimal feature_list.json into project_dir."""
    fl = {
        "schema_version": 1,
        "features": features,
        "metadata": {
            "total_features": len(features),
            "passing": sum(1 for f in features if f.get("status") == "passing"),
            "failing": sum(1 for f in features if f.get("status") != "passing"),
            "in_progress": 0,
            "blocked": 0,
            "deferred": 0,
            "last_updated": "2026-09-13",
        },
    }
    p = project_dir / "feature_list.json"
    p.write_text(json.dumps(fl), encoding="utf-8")
    return p


# ---------- Daemon.start wiring ----------


class TestStartAttachesEventLog(unittest.IsolatedAsyncioTestCase):
    """``Daemon.start()`` attaches an EventLogLogger when a project is registered."""

    async def test_event_log_attached_on_start_with_matching_project(self):
        with tempfile.TemporaryDirectory(prefix="heddle_event_log_wiring_") as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "project"
            proj_dir.mkdir()
            _write_feature_list(proj_dir, [])
            proj = _project_stub(proj_dir.resolve())
            registry = tmp_path / "projects.json"
            logs_dir = tmp_path / "logs"
            with _isolated_logs_dir(logs_dir):
                with _projects_registry(registry, [proj]):
                    cfg = DaemonConfig(
                        host="127.0.0.1",
                        port=0,
                        project_path=proj_dir.resolve(),
                    )
                    daemon = Daemon(cfg)
                    try:
                        await daemon.start()
                        self.assertIsNotNone(daemon._event_log)
                        self.assertIsInstance(daemon._event_log, EventLogLogger)
                        expected = logs_dir / proj.id / DEFAULT_EVENT_LOG_FILENAME
                        self.assertEqual(
                            daemon._event_log.log_path.resolve(),
                            expected.resolve(),
                        )
                        self.assertTrue(expected.parent.is_dir())
                    finally:
                        await daemon.stop()


class TestStartNoEventLogOnSkeletonMode(unittest.IsolatedAsyncioTestCase):
    """No project_path → no event log; daemon still serves."""

    async def test_no_attach_when_project_path_is_none(self):
        hlog.detach_file_sink()
        try:
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=None)
            daemon = Daemon(cfg)
            try:
                with _capture_stderr():
                    await daemon.start()
                self.assertIsNone(daemon._event_log)
            finally:
                await daemon.stop()
        finally:
            hlog.detach_file_sink()


class TestStartNoEventLogWhenProjectUnregistered(
    unittest.IsolatedAsyncioTestCase
):
    """Project_path set but no matching registered project → warn + continue."""

    async def test_warn_and_continue_when_project_id_resolution_fails(self):
        hlog.detach_file_sink()
        try:
            with tempfile.TemporaryDirectory(prefix="heddle_event_log_wiring_") as tmp:
                tmp_path = Path(tmp)
                proj_dir = tmp_path / "project"
                proj_dir.mkdir()
                _write_feature_list(proj_dir, [])
                registry = tmp_path / "projects.json"
                with _projects_registry(registry, []):
                    with _isolated_logs_dir(tmp_path / "logs"):
                        cfg = DaemonConfig(
                            host="127.0.0.1",
                            port=0,
                            project_path=proj_dir.resolve(),
                        )
                        daemon = Daemon(cfg)
                        try:
                            with _capture_stderr() as buf:
                                await daemon.start()
                            self.assertIsNone(daemon._event_log)
                            text = buf.getvalue()
                            self.assertIn("event_log_skipped", text)
                            self.assertIn("no_matching_project", text)
                        finally:
                            await daemon.stop()
        finally:
            hlog.detach_file_sink()


# ---------- Daemon.stop wiring ----------


class TestStopClosesEventLog(unittest.IsolatedAsyncioTestCase):
    """``Daemon.stop()`` closes the per-project event-log writer."""

    async def test_event_log_closed_on_stop(self):
        with tempfile.TemporaryDirectory(prefix="heddle_event_log_wiring_") as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "project"
            proj_dir.mkdir()
            _write_feature_list(proj_dir, [])
            proj = _project_stub(proj_dir.resolve())
            registry = tmp_path / "projects.json"
            logs_dir = tmp_path / "logs"
            with _isolated_logs_dir(logs_dir):
                with _projects_registry(registry, [proj]):
                    cfg = DaemonConfig(
                        host="127.0.0.1",
                        port=0,
                        project_path=proj_dir.resolve(),
                    )
                    daemon = Daemon(cfg)
                    await daemon.start()
                    self.assertIsNotNone(daemon._event_log)
                    await daemon.stop()
                    self.assertIsNone(daemon._event_log)


# ---------- End-to-end: state change → file sink ----------


class TestEmitEventDualWrites(unittest.IsolatedAsyncioTestCase):
    """``RouteHandler.emit_event`` dual-writes WS + events.jsonl.

    Drives ``emit_event`` directly with a fake WS emitter and asserts:

      * the WS emitter received the body dict with
        ``{event, project_id, feature_id, payload}``;
      * the on-disk ``events.jsonl`` contains a matching line with
        the ``{ts, event, project_id, feature_id, payload}`` schema.
    """

    async def test_emit_event_writes_to_file_sink(self):
        with tempfile.TemporaryDirectory(prefix="heddle_event_log_wiring_") as tmp:
            tmp_path = Path(tmp)
            logs_dir = tmp_path / "logs"
            proj_id = "test-proj-evt"
            logger = EventLogLogger(logs_dir, proj_id)
            try:
                # Build a route handler with a fake emitter + the
                # file sink wired in.
                from heddle_daemon.routes import RouteHandler

                handler = RouteHandler()
                handler.event_log = logger

                ws_received: list[dict] = []

                async def _fake_emitter(body: dict) -> None:
                    ws_received.append(body)

                handler.event_emitter = _fake_emitter

                await handler.emit_event(
                    "feature_status_changed",
                    project_id=proj_id,
                    feature_id="feat-001",
                    payload={"from_status": "pending", "to_status": "in_progress"},
                )
            finally:
                logger.close()

            # WS side received the event body.
            self.assertEqual(len(ws_received), 1)
            self.assertEqual(
                ws_received[0]["event"], "feature_status_changed"
            )
            self.assertEqual(ws_received[0]["project_id"], proj_id)
            self.assertEqual(ws_received[0]["feature_id"], "feat-001")
            self.assertEqual(
                ws_received[0]["payload"]["from_status"], "pending"
            )
            # File side received the matching JSON line.
            log_path = logs_dir / proj_id / DEFAULT_EVENT_LOG_FILENAME
            lines = _read_event_lines(log_path)
            self.assertEqual(len(lines), 1)
            rec = lines[0]
            self.assertEqual(rec["event"], "feature_status_changed")
            self.assertEqual(rec["project_id"], proj_id)
            self.assertEqual(rec["feature_id"], "feat-001")
            self.assertEqual(rec["payload"]["to_status"], "in_progress")
            self.assertIn("ts", rec)

    async def test_emit_event_file_only_when_emitter_unset(self):
        """File-sink write happens even when no WS emitter is attached.

        The on-disk audit log is the durable record; a future
        supervisor / CLI consumer may write directly via the file
        sink without an active WS client. The route handler must
        therefore still flush the line.
        """
        with tempfile.TemporaryDirectory(prefix="heddle_event_log_wiring_") as tmp:
            tmp_path = Path(tmp)
            logs_dir = tmp_path / "logs"
            proj_id = "test-proj-evt-file-only"
            logger = EventLogLogger(logs_dir, proj_id)
            try:
                from heddle_daemon.routes import RouteHandler

                handler = RouteHandler()
                handler.event_log = logger
                handler.event_emitter = None  # explicit: no WS

                await handler.emit_event(
                    "dialog_done",
                    project_id=proj_id,
                    feature_id=None,
                    payload={"full_text": "ok", "intent": "chat"},
                )
            finally:
                logger.close()
            log_path = logs_dir / proj_id / DEFAULT_EVENT_LOG_FILENAME
            lines = _read_event_lines(log_path)
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["event"], "dialog_done")
            self.assertIsNone(lines[0]["feature_id"])


if __name__ == "__main__":
    unittest.main()
