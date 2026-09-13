# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_daemon.llm_audit — feat-025 (LLM audit log sidecar).

Covers the contract:

    * ``JsonLineAppender`` creates the parent directory on demand and
      writes one JSON object per line, flushing each call;
    * ``LlmAuditLogger`` builds the per-project directory and writes
      records with the exact schema (``ts``, ``feature_id``,
      ``model``, ``prompt_tokens``, ``completion_tokens``,
      ``latency_ms``, ``outcome``);
    * Records are passed through ``heddle_common.logging.redact`` so
      sensitive keys (``api_key``, ``token``, ``secret``) never reach
      disk;
    * Path-traversal-style project ids are rejected at construction;
    * ``close()`` is idempotent and the underlying file handle is
      flushed + closed.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from heddle_common.logging import redact

from heddle_daemon.llm_audit import (
    DEFAULT_LOGS_DIR,
    ENV_LOGS_DIR,
    LLM_AUDIT_FILENAME,
    LlmAuditLogger,
    JsonLineAppender,
    resolve_logs_dir,
)


# ---------- helpers ----------


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------- JsonLineAppender ----------


class TestJsonLineAppender(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="heddle_llm_audit_test_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_appender_creates_parent_dir_on_demand(self):
        nested = Path(self.tmp) / "deep" / "nest" / "audit.jsonl"
        appender = JsonLineAppender(nested)
        try:
            self.assertTrue(nested.parent.is_dir())
        finally:
            appender.close()

    def test_appender_writes_one_json_object_per_line(self):
        path = Path(self.tmp) / "a.jsonl"
        appender = JsonLineAppender(path)
        try:
            appender.append({"k": 1})
            appender.append({"k": 2})
            appender.append({"k": 3})
        finally:
            appender.close()
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0]), {"k": 1})
        self.assertEqual(json.loads(lines[1]), {"k": 2})
        self.assertEqual(json.loads(lines[2]), {"k": 3})

    def test_appender_appends_not_truncates(self):
        path = Path(self.tmp) / "a.jsonl"
        path.write_text('{"pre":"existing"}\n', encoding="utf-8")
        appender = JsonLineAppender(path)
        try:
            appender.append({"k": "new"})
        finally:
            appender.close()
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0]), {"pre": "existing"})
        self.assertEqual(json.loads(lines[1]), {"k": "new"})

    def test_appender_does_not_redact_caller_provided_records(self):
        """JsonLineAppender is a raw primitive — redaction is the
        caller's responsibility. The standard schema fields
        (``prompt_tokens`` etc.) MUST reach disk verbatim, so the
        appender does NOT walk the record with ``redact``.
        """
        path = Path(self.tmp) / "a.jsonl"
        appender = JsonLineAppender(path)
        try:
            appender.append(
                {"api_key": "sk-very-secret", "name": "ok", "prompt_tokens": 99}
            )
        finally:
            appender.close()
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        # appender writes what it was given — no redaction
        self.assertEqual(record["api_key"], "sk-very-secret")
        self.assertEqual(record["prompt_tokens"], 99)

    def test_appender_close_is_idempotent(self):
        path = Path(self.tmp) / "a.jsonl"
        appender = JsonLineAppender(path)
        appender.close()
        # Second close must not raise.
        appender.close()

    def test_appender_append_after_close_is_noop(self):
        path = Path(self.tmp) / "a.jsonl"
        appender = JsonLineAppender(path)
        appender.close()
        appender.append({"k": 1})  # must not raise
        self.assertEqual(path.read_text(encoding="utf-8"), "")


# ---------- LlmAuditLogger construction ----------


class TestLlmAuditLoggerConstruction(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="heddle_llm_audit_test_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_per_project_subdirectory(self):
        logs_dir = Path(self.tmp) / "logs"
        logger = LlmAuditLogger(logs_dir=logs_dir, project_id="proj-1")
        try:
            self.assertTrue(logger.project_dir.is_dir())
            self.assertEqual(
                logger.audit_path,
                logs_dir / "proj-1" / LLM_AUDIT_FILENAME,
            )
        finally:
            logger.close()

    def test_rejects_empty_project_id(self):
        with self.assertRaises(ValueError):
            LlmAuditLogger(logs_dir=Path(self.tmp), project_id="")

    def test_rejects_path_traversal_project_id(self):
        for bad in ("../escape", "..\\escape", "a/b", "a\\b", ".", ".."):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    LlmAuditLogger(logs_dir=Path(self.tmp), project_id=bad)


# ---------- LlmAuditLogger.record_call ----------


class TestLlmAuditLoggerRecordCall(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="heddle_llm_audit_test_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _new_logger(self, project_id: str = "proj-x") -> LlmAuditLogger:
        return LlmAuditLogger(
            logs_dir=Path(self.tmp) / "logs",
            project_id=project_id,
        )

    def test_record_call_writes_schema_fields(self):
        logger = self._new_logger()
        try:
            logger.record_call(
                feature_id="feat-001",
                model="claude-opus-4.8",
                prompt_tokens=123,
                completion_tokens=45,
                latency_ms=87,
                outcome="ok",
            )
        finally:
            logger.close()
        records = _read_lines(logger.audit_path)
        self.assertEqual(len(records), 1)
        record = records[0]
        for key in (
            "ts",
            "feature_id",
            "model",
            "prompt_tokens",
            "completion_tokens",
            "latency_ms",
            "outcome",
        ):
            self.assertIn(key, record, f"missing key {key!r}")
        self.assertEqual(record["feature_id"], "feat-001")
        self.assertEqual(record["model"], "claude-opus-4.8")
        self.assertEqual(record["prompt_tokens"], 123)
        self.assertEqual(record["completion_tokens"], 45)
        self.assertEqual(record["latency_ms"], 87)
        self.assertEqual(record["outcome"], "ok")
        # ts is a non-empty ISO8601 string
        self.assertIsInstance(record["ts"], str)
        self.assertGreater(len(record["ts"]), 0)

    def test_record_call_appends_multiple_records(self):
        logger = self._new_logger()
        try:
            for i in range(3):
                logger.record_call(
                    feature_id="feat-001",
                    model="claude-opus-4.8",
                    prompt_tokens=i,
                    completion_tokens=i,
                    latency_ms=10,
                    outcome="ok",
                )
        finally:
            logger.close()
        records = _read_lines(logger.audit_path)
        self.assertEqual(len(records), 3)
        self.assertEqual([r["prompt_tokens"] for r in records], [0, 1, 2])

    def test_record_call_preserves_extra_fields(self):
        logger = self._new_logger()
        try:
            logger.record_call(
                feature_id="feat-001",
                model="claude-opus-4.8",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=50,
                outcome="ok",
                stop_reason="end_turn",
                retry_attempt=1,
            )
        finally:
            logger.close()
        record = _read_lines(logger.audit_path)[0]
        self.assertEqual(record["stop_reason"], "end_turn")
        self.assertEqual(record["retry_attempt"], 1)

    def test_record_call_redacts_sensitive_extras(self):
        logger = self._new_logger()
        try:
            logger.record_call(
                feature_id="feat-001",
                model="claude-opus-4.8",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=50,
                outcome="ok",
                api_key="sk-very-secret",
            )
        finally:
            logger.close()
        record = _read_lines(logger.audit_path)[0]
        self.assertEqual(record["api_key"], "[REDACTED]")

    def test_record_call_latency_must_be_int(self):
        logger = self._new_logger()
        try:
            logger.record_call(
                feature_id="feat-001",
                model="claude-opus-4.8",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=12.7,  # cast to int by record_call
                outcome="ok",
            )
        finally:
            logger.close()
        record = _read_lines(logger.audit_path)[0]
        self.assertEqual(record["latency_ms"], 12)
        self.assertIsInstance(record["latency_ms"], int)


# ---------- env-var helpers ----------


class TestResolveLogsDir(unittest.TestCase):
    def test_default_when_env_unset(self):
        self.assertEqual(
            resolve_logs_dir(env={}),
            Path(DEFAULT_LOGS_DIR).expanduser(),
        )

    def test_default_when_env_empty_string(self):
        self.assertEqual(
            resolve_logs_dir(env={ENV_LOGS_DIR: ""}),
            Path(DEFAULT_LOGS_DIR).expanduser(),
        )

    def test_env_var_overrides_default(self):
        self.assertEqual(
            resolve_logs_dir(env={ENV_LOGS_DIR: "/var/log/heddle"}),
            Path("/var/log/heddle"),
        )

    def test_env_var_expands_tilde(self):
        self.assertEqual(
            resolve_logs_dir(env={ENV_LOGS_DIR: "~/alt-logs"}),
            Path("~/alt-logs").expanduser(),
        )


# ---------- redaction sanity check ----------


class TestRedactionIntegration(unittest.TestCase):
    """Sanity-check that the audit record keys flow through ``redact``."""

    def test_redact_replaces_api_key_value(self):
        out = redact({"api_key": "secret", "ok": 1})
        self.assertEqual(out["api_key"], "[REDACTED]")
        self.assertEqual(out["ok"], 1)

    def test_redact_replaces_token_value(self):
        out = redact({"prompt_token": "x", "ok": 1})
        self.assertEqual(out["prompt_token"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
