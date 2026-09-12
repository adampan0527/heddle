# SPDX-License-Identifier: Apache-2.0
"""Unit tests for heddle_common.log_rotation — feat-015."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from heddle_common import logging as hlog
from heddle_common.log_rotation import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    MAX_LOG_BACKUP_COUNT,
    MAX_LOG_MAX_BYTES,
    RotatingFileSink,
    get_log_backup_count,
    get_log_max_bytes,
)


class _TmpPath:
    """Tiny ``tempfile.TemporaryDirectory``-like context manager.

    Defined inline so the test class bodies read top-down without a
    separate fixture file. Returns a ``Path``.
    """

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="heddle_logrot_")
        return Path(self._tmp.name)

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


def _sink_isolation(cls: type) -> type:
    """Mixin: reset the module-level sink + env-var defaults per test.

    The module-level ``_file_sink`` is process-global; without a
    fixture a sink attached by one test leaks into another. We
    detach + reset on both setUp and tearDown so the order of test
    execution can never flip a result.
    """

    class _Isolated(cls):  # type: ignore[misc, valid-type]
        def setUp(self) -> None:  # noqa: N802 — unittest protocol
            hlog.detach_file_sink()
            for var in ("HEDDLE_LOG_MAX_BYTES", "HEDDLE_LOG_BACKUP_COUNT"):
                os.environ.pop(var, None)
            # unittest.TestCase provides setUp; skip the super() call
            # explicitly when the parent class doesn't define it (some
            # lightweight mixins don't).
            parent_setUp = getattr(super(), "setUp", None)
            if parent_setUp is not None:
                parent_setUp()

        def tearDown(self) -> None:  # noqa: N802
            hlog.detach_file_sink()
            for var in ("HEDDLE_LOG_MAX_BYTES", "HEDDLE_LOG_BACKUP_COUNT"):
                os.environ.pop(var, None)
            parent_tearDown = getattr(super(), "tearDown", None)
            if parent_tearDown is not None:
                parent_tearDown()

    _Isolated.__name__ = cls.__name__
    return _Isolated


# ---------- env-var parsing ----------


@_sink_isolation
class TestEnvVars(unittest.TestCase):
    def test_defaults_when_unset(self):
        self.assertEqual(get_log_max_bytes(), DEFAULT_LOG_MAX_BYTES)
        self.assertEqual(get_log_backup_count(), DEFAULT_LOG_BACKUP_COUNT)

    def test_override_via_explicit_env_dict(self):
        env = {"HEDDLE_LOG_MAX_BYTES": "1024", "HEDDLE_LOG_BACKUP_COUNT": "3"}
        self.assertEqual(get_log_max_bytes(env), 1024)
        self.assertEqual(get_log_backup_count(env), 3)

    def test_invalid_max_bytes_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            get_log_max_bytes({"HEDDLE_LOG_MAX_BYTES": "not-a-number"})
        self.assertIn("HEDDLE_LOG_MAX_BYTES", str(ctx.exception))

    def test_invalid_backup_count_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            get_log_backup_count({"HEDDLE_LOG_BACKUP_COUNT": "abc"})
        self.assertIn("HEDDLE_LOG_BACKUP_COUNT", str(ctx.exception))

    def test_empty_string_treated_as_unset(self):
        env = {"HEDDLE_LOG_MAX_BYTES": "", "HEDDLE_LOG_BACKUP_COUNT": ""}
        self.assertEqual(get_log_max_bytes(env), DEFAULT_LOG_MAX_BYTES)
        self.assertEqual(get_log_backup_count(env), DEFAULT_LOG_BACKUP_COUNT)


# ---------- config validation ----------


@_sink_isolation
class TestConfigValidation(unittest.TestCase):
    def test_max_bytes_zero_raises(self):
        with self.assertRaises(ValueError):
            RotatingFileSink("/tmp/x.log", max_bytes=0, backup_count=5)

    def test_max_bytes_negative_raises(self):
        with self.assertRaises(ValueError):
            RotatingFileSink("/tmp/x.log", max_bytes=-1, backup_count=5)

    def test_max_bytes_above_one_gib_raises(self):
        with self.assertRaises(ValueError) as ctx:
            RotatingFileSink(
                "/tmp/x.log",
                max_bytes=MAX_LOG_MAX_BYTES + 1,
                backup_count=5,
            )
        self.assertIn("MAX_LOG_MAX_BYTES", str(ctx.exception))

    def test_backup_count_zero_raises(self):
        with self.assertRaises(ValueError):
            RotatingFileSink("/tmp/x.log", max_bytes=1024, backup_count=0)

    def test_backup_count_negative_raises(self):
        with self.assertRaises(ValueError):
            RotatingFileSink("/tmp/x.log", max_bytes=1024, backup_count=-1)

    def test_backup_count_above_100_raises(self):
        with self.assertRaises(ValueError):
            RotatingFileSink(
                "/tmp/x.log",
                max_bytes=1024,
                backup_count=MAX_LOG_BACKUP_COUNT + 1,
            )

    def test_non_int_max_bytes_raises(self):
        with self.assertRaises(ValueError):
            RotatingFileSink("/tmp/x.log", max_bytes=1.5, backup_count=5)  # type: ignore[arg-type]


# ---------- rotation mechanics ----------


@_sink_isolation
class TestRotation(unittest.TestCase):
    """Verify the size-rotation contract end-to-end."""

    def _sink(self, tmp: Path, *, max_bytes: int, backup_count: int = 3) -> RotatingFileSink:
        return RotatingFileSink(
            tmp / "test.log", max_bytes=max_bytes, backup_count=backup_count
        )

    def test_rotation_creates_dot1_backup(self):
        with _TmpPath() as tmp:
            sink = self._sink(tmp, max_bytes=200)
            # Each "line" ~ 100 bytes; after two writes we cross 200.
            line = json.dumps({"i": 0, "msg": "x" * 80})
            sink.emit({"i": 1, "msg": line})
            sink.emit({"i": 2, "msg": line})
            sink.close()
            # File 1 has the last line, .1 has the first.
            files = sorted(p.name for p in tmp.iterdir())
            self.assertIn("test.log", files)
            self.assertIn("test.log.1", files)

    def test_oldest_pruned_at_backup_count_plus_one(self):
        with _TmpPath() as tmp:
            sink = self._sink(tmp, max_bytes=80, backup_count=2)
            line = json.dumps({"i": 0, "msg": "x" * 60})
            # 5 writes → expect backup_count=2 so .1 and .2 exist, .3 absent
            for i in range(5):
                sink.emit({"i": i, "msg": line})
            sink.close()
            files = sorted(p.name for p in tmp.iterdir())
            self.assertIn("test.log", files)
            self.assertIn("test.log.1", files)
            self.assertIn("test.log.2", files)
            self.assertNotIn("test.log.3", files)

    def test_generations_renamed_in_order(self):
        with _TmpPath() as tmp:
            sink = self._sink(tmp, max_bytes=80, backup_count=3)
            line = json.dumps({"i": 0, "msg": "x" * 60})
            for i in range(4):
                sink.emit({"i": i, "msg": line})
            sink.close()
            # Each .N file must contain exactly the line that was
            # active at generation N (oldest of the surviving set).
            for n in range(1, 4):
                body = (tmp / f"test.log.{n}").read_text(encoding="utf-8").strip()
                parsed = json.loads(body)
                self.assertIn("i", parsed)
                self.assertEqual(parsed["msg"], line)

    def test_close_is_idempotent(self):
        with _TmpPath() as tmp:
            sink = self._sink(tmp, max_bytes=200)
            sink.emit({"a": 1})
            sink.close()
            # Second close must not raise.
            sink.close()

    def test_emit_after_close_is_noop(self):
        with _TmpPath() as tmp:
            sink = self._sink(tmp, max_bytes=200)
            sink.emit({"a": 1})
            sink.close()
            # Emit after close is a no-op (we don't raise).
            sink.emit({"a": 2})
            body = (tmp / "test.log").read_text(encoding="utf-8")
            # Just the first line made it.
            self.assertEqual(body.count("\n"), 1)


# ---------- emit + redaction through emit ----------


@_sink_isolation
class TestRedactionBeforeWrite(unittest.TestCase):
    """The file sink MUST receive redacted payloads (T-015 / T-030).

    The contract is: ``heddle_common.logging.emit`` runs ``redact()``
    on the payload and then hands the **already-redacted** dict to
    ``RotatingFileSink.emit``. The sink does NOT re-redact (would
    double-strip), so backups inherit the same redaction guarantees
    as the stderr stream. These tests cover the end-to-end path via
    ``attach_file_sink``; the lower-level ``RotatingFileSink.emit``
    unit tests above use non-sensitive data so the redaction contract
    is tested at the right layer (the logging module, not the sink).
    """

    def _capture_emit(self, tmp: Path) -> str:
        """Capture the next emit to a temp file via attach_file_sink."""
        buf = io.StringIO()
        hlog.attach_file_sink(tmp / "redact.log", max_bytes=128, backup_count=3)
        try:
            with patch.object(sys, "stderr", buf):
                hlog.info(
                    "daemon",
                    "auth_failed",
                    "bad key",
                    api_key="sk-supersecret-redaction-test",
                )
            return (tmp / "redact.log").read_text(encoding="utf-8")
        finally:
            hlog.detach_file_sink()

    def test_redacted_field_does_not_appear_in_file(self):
        with _TmpPath() as tmp:
            body = self._capture_emit(tmp)
            self.assertNotIn("sk-supersecret-redaction-test", body)

    def test_redaction_marker_present_in_file(self):
        with _TmpPath() as tmp:
            body = self._capture_emit(tmp)
            self.assertIn('"api_key":"[REDACTED]"', body)

    def test_no_leak_across_rotations(self):
        """Multiple writes must rotate without leaking the secret."""
        with _TmpPath() as tmp:
            hlog.attach_file_sink(tmp / "redact.log", max_bytes=120, backup_count=3)
            try:
                buf = io.StringIO()
                with patch.object(sys, "stderr", buf):
                    # Many writes force at least one rotation.
                    for i in range(8):
                        hlog.info(
                            "daemon",
                            "auth",
                            "msg",
                            api_key=f"sk-secret-{i}",
                            i=i,
                        )
            finally:
                hlog.detach_file_sink()
            for path in tmp.iterdir():
                body = path.read_text(encoding="utf-8")
                # None of the per-iteration secrets may appear.
                for i in range(8):
                    self.assertNotIn(f"sk-secret-{i}", body)
                self.assertIn('"api_key":"[REDACTED]"', body)


# ---------- attach / detach idempotence ----------


@_sink_isolation
class TestAttachDetach(unittest.TestCase):
    """Module-level sink attachment contract (feat-015 task 2)."""

    def _capture(self, fn) -> tuple[dict[str, Any], str]:
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            fn()
        line = buf.getvalue().rstrip("\n")
        return json.loads(line), line

    def test_attach_writes_to_sink_and_stderr(self):
        with _TmpPath() as tmp:
            hlog.attach_file_sink(tmp / "both.log", max_bytes=1024, backup_count=2)
            try:
                line_str, _ = self._capture(
                    lambda: hlog.info("daemon", "tick", "msg")
                )
                self.assertEqual(line_str["msg"], "msg")
                # stderr saw it AND the file saw it (compact JSON form).
                body = (tmp / "both.log").read_text(encoding="utf-8")
                self.assertIn('"msg":"msg"', body)
            finally:
                hlog.detach_file_sink()

    def test_attach_forwards_redacted_payload(self):
        with _TmpPath() as tmp:
            hlog.attach_file_sink(tmp / "redact.log", max_bytes=1024, backup_count=2)
            try:
                self._capture(
                    lambda: hlog.info(
                        "daemon",
                        "auth",
                        "msg",
                        api_key="sk-supersecret-zzz",
                    )
                )
                body = (tmp / "redact.log").read_text(encoding="utf-8")
                self.assertNotIn("sk-supersecret-zzz", body)
                self.assertIn('"api_key":"[REDACTED]"', body)
            finally:
                hlog.detach_file_sink()

    def test_detach_with_no_sink_is_noop(self):
        # Module-level sink starts None → detach must not raise.
        self.assertIsNone(getattr(hlog, "_file_sink", None))
        hlog.detach_file_sink()  # should not raise

    def test_double_attach_closes_first_sink(self):
        with _TmpPath() as tmp:
            hlog.attach_file_sink(tmp / "first.log", max_bytes=1024, backup_count=2)
            first_sink = hlog._file_sink
            self.assertIsNotNone(first_sink)
            # Second attach replaces the first; the first's handler
            # must have been closed (the underlying stdlib handler's
            # ``stream`` is None after close()).
            hlog.attach_file_sink(tmp / "second.log", max_bytes=1024, backup_count=2)
            try:
                self.assertIsNot(first_sink, hlog._file_sink)
                self.assertIsNone(first_sink._handler)  # closed
            finally:
                hlog.detach_file_sink()


# ---------- helpers ----------


if __name__ == "__main__":
    unittest.main()