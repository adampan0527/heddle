# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_common.event_log — feat-048 (structured event log).

Covers the contract:

    * ``EventLogLogger`` builds the per-project directory and writes
      records with the exact schema (``ts``, ``event``,
      ``project_id``, ``feature_id``, ``payload``);
    * Records are passed through ``heddle_common.logging.redact`` so
      sensitive keys (``api_key``, ``token``, ``secret``) never reach
      disk;
    * Path-traversal-style project ids are rejected at construction;
    * ``record(...)`` rejects a project_id mismatch (so a route-handler
      bug cannot split one project's stream across multiple files);
    * ``close()`` is idempotent and the underlying file handle is
      flushed + closed.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from heddle_common.event_log import (
    DEFAULT_EVENT_LOG_FILENAME,
    EventLogLogger,
    resolve_event_log_path,
)


# ---------- helpers ----------


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------- resolve_event_log_path ----------


class TestResolveEventLogPath(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="heddle_event_log_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_expected_path_layout(self):
        result = resolve_event_log_path(self.tmp, "proj-abc")
        self.assertEqual(
            result, self.tmp / "proj-abc" / DEFAULT_EVENT_LOG_FILENAME
        )

    def test_expands_tilde_in_logs_dir(self):
        # When logs_dir starts with ``~`` it is expanded (Path.expanduser
        # is applied). We don't pin the user's home — we just assert
        # the resulting Path object no longer starts with a literal
        # ``~`` and still ends with the conventional tail.
        result = resolve_event_log_path("~/heddle_test_logs", "p")
        self.assertFalse(str(result).startswith("~/"))
        self.assertEqual(result.name, DEFAULT_EVENT_LOG_FILENAME)


# ---------- EventLogLogger construction ----------


class TestEventLogLoggerConstruction(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="heddle_event_log_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_per_project_subdirectory(self):
        logger = EventLogLogger(self.tmp, "proj-abc")
        try:
            expected = self.tmp / "proj-abc" / DEFAULT_EVENT_LOG_FILENAME
            self.assertTrue(expected.parent.is_dir())
            self.assertEqual(logger.log_path, expected)
            self.assertEqual(logger.project_dir, self.tmp / "proj-abc")
            self.assertEqual(logger.project_id, "proj-abc")
        finally:
            logger.close()

    def test_creates_deeply_nested_logs_dir(self):
        deep = self.tmp / "deep" / "nest" / "logs"
        logger = EventLogLogger(deep, "p")
        try:
            self.assertTrue((deep / "p").is_dir())
        finally:
            logger.close()

    def test_rejects_empty_project_id(self):
        with self.assertRaises(ValueError):
            EventLogLogger(self.tmp, "")

    def test_rejects_non_string_project_id(self):
        with self.assertRaises(ValueError):
            EventLogLogger(self.tmp, 123)  # type: ignore[arg-type]

    def test_rejects_path_traversal_project_id(self):
        with self.assertRaises(ValueError):
            EventLogLogger(self.tmp, "../escape")

    def test_rejects_path_separator_in_project_id(self):
        with self.assertRaises(ValueError):
            EventLogLogger(self.tmp, "a/b")

    def test_rejects_backslash_in_project_id(self):
        with self.assertRaises(ValueError):
            EventLogLogger(self.tmp, "a\\b")


# ---------- EventLogLogger.record ----------


class TestEventLogLoggerRecord(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="heddle_event_log_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_writes_schema_fields(self):
        logger = EventLogLogger(self.tmp, "p1")
        try:
            logger.record(
                event="feature_attempt_started",
                project_id="p1",
                feature_id="feat-001",
                payload={"attempt_id": "abc123"},
            )
        finally:
            logger.close()
        lines = _read_lines(self.tmp / "p1" / DEFAULT_EVENT_LOG_FILENAME)
        self.assertEqual(len(lines), 1)
        rec = lines[0]
        self.assertIn("ts", rec)
        self.assertEqual(rec["event"], "feature_attempt_started")
        self.assertEqual(rec["project_id"], "p1")
        self.assertEqual(rec["feature_id"], "feat-001")
        self.assertEqual(rec["payload"], {"attempt_id": "abc123"})

    def test_record_supports_null_feature_id(self):
        logger = EventLogLogger(self.tmp, "p1")
        try:
            logger.record(
                event="dialog_done",
                project_id="p1",
                feature_id=None,
                payload={"full_text": "ok"},
            )
        finally:
            logger.close()
        lines = _read_lines(self.tmp / "p1" / DEFAULT_EVENT_LOG_FILENAME)
        self.assertEqual(len(lines), 1)
        self.assertIsNone(lines[0]["feature_id"])

    def test_record_defaults_payload_to_empty_dict(self):
        logger = EventLogLogger(self.tmp, "p1")
        try:
            logger.record(
                event="draft_confirmed",
                project_id="p1",
                feature_id="feat-1",
            )
        finally:
            logger.close()
        lines = _read_lines(self.tmp / "p1" / DEFAULT_EVENT_LOG_FILENAME)
        self.assertEqual(lines[0]["payload"], {})

    def test_record_appends_multiple_records(self):
        logger = EventLogLogger(self.tmp, "p1")
        try:
            for i in range(3):
                logger.record(
                    event="feature_status_changed",
                    project_id="p1",
                    feature_id=f"feat-{i}",
                    payload={"from": "pending", "to": "in_progress"},
                )
        finally:
            logger.close()
        lines = _read_lines(self.tmp / "p1" / DEFAULT_EVENT_LOG_FILENAME)
        self.assertEqual(len(lines), 3)
        self.assertEqual([r["feature_id"] for r in lines], ["feat-0", "feat-1", "feat-2"])

    def test_record_redacts_sensitive_payload_keys(self):
        logger = EventLogLogger(self.tmp, "p1")
        try:
            logger.record(
                event="llm_resolved",
                project_id="p1",
                feature_id="feat-1",
                payload={"config_name": "gpt-4o", "api_key": "leak-me"},
            )
        finally:
            logger.close()
        lines = _read_lines(self.tmp / "p1" / DEFAULT_EVENT_LOG_FILENAME)
        self.assertEqual(lines[0]["payload"]["config_name"], "gpt-4o")
        self.assertEqual(lines[0]["payload"]["api_key"], "[REDACTED]")

    def test_record_rejects_empty_event(self):
        logger = EventLogLogger(self.tmp, "p1")
        try:
            with self.assertRaises(ValueError):
                logger.record(event="", project_id="p1", feature_id=None, payload={})
        finally:
            logger.close()

    def test_record_rejects_non_dict_payload(self):
        logger = EventLogLogger(self.tmp, "p1")
        try:
            with self.assertRaises(ValueError):
                logger.record(
                    event="feature_status_changed",
                    project_id="p1",
                    feature_id=None,
                    payload="not-a-dict",  # type: ignore[arg-type]
                )
        finally:
            logger.close()

    def test_record_rejects_non_string_feature_id(self):
        logger = EventLogLogger(self.tmp, "p1")
        try:
            with self.assertRaises(ValueError):
                logger.record(
                    event="feature_attempt_started",
                    project_id="p1",
                    feature_id=123,  # type: ignore[arg-type]
                    payload={},
                )
        finally:
            logger.close()

    def test_record_rejects_project_id_mismatch(self):
        logger = EventLogLogger(self.tmp, "p1")
        try:
            with self.assertRaises(ValueError):
                logger.record(
                    event="draft_confirmed",
                    project_id="different-project",
                    feature_id=None,
                    payload={},
                )
        finally:
            logger.close()

    def test_record_appends_not_truncates(self):
        # Pre-populate the file with a marker; opening the logger
        # again must NOT erase it (append-mode invariant).
        path = self.tmp / "p1" / DEFAULT_EVENT_LOG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"event":"pre_existing"}\n', encoding="utf-8")
        logger = EventLogLogger(self.tmp, "p1")
        try:
            logger.record(
                event="feature_status_changed",
                project_id="p1",
                feature_id="feat-2",
                payload={},
            )
        finally:
            logger.close()
        lines = _read_lines(path)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["event"], "pre_existing")
        self.assertEqual(lines[1]["event"], "feature_status_changed")


# ---------- EventLogLogger lifecycle ----------


class TestEventLogLoggerLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="heddle_event_log_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_close_is_idempotent(self):
        logger = EventLogLogger(self.tmp, "p1")
        logger.close()
        # Second close must be a no-op (no exception).
        logger.close()

    def test_record_after_close_is_noop(self):
        logger = EventLogLogger(self.tmp, "p1")
        logger.close()
        # After close, record() must not raise; the line is dropped.
        logger.record(
            event="feature_status_changed",
            project_id="p1",
            feature_id=None,
            payload={},
        )
        # The file should not have been created/touched after close
        # — file existence is best-effort in v0.1 so we just assert
        # no exception was raised and (if the file exists) no
        # ``feature_status_changed`` line landed on disk.
        path = self.tmp / "p1" / DEFAULT_EVENT_LOG_FILENAME
        if path.exists():
            lines = _read_lines(path)
            self.assertFalse(any(r["event"] == "feature_status_changed" for r in lines))


if __name__ == "__main__":
    unittest.main()
