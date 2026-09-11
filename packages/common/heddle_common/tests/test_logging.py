# SPDX-License-Identifier: Apache-2.0
"""Unit tests for heddle_common.logging. Stdlib unittest — no extra deps."""

from __future__ import annotations

import io
import json
import sys
import unittest
from datetime import datetime
from typing import Any
from unittest.mock import patch

from heddle_common import logging as hlog


class TestRedaction(unittest.TestCase):
    def test_api_key_top_level(self):
        result = hlog.redact({"api_key": "secret-value-12345", "other": "ok"})
        self.assertEqual(result["api_key"], "[REDACTED]")
        self.assertEqual(result["other"], "ok")

    def test_api_key_uppercase(self):
        result = hlog.redact({"API_KEY": "secret-value"})
        self.assertEqual(result["API_KEY"], "[REDACTED]")

    def test_api_key_camel_case(self):
        result = hlog.redact({"apiKey": "secret-value"})
        self.assertEqual(result["apiKey"], "[REDACTED]")

    def test_api_key_dashed(self):
        result = hlog.redact({"api-key": "secret-value"})
        self.assertEqual(result["api-key"], "[REDACTED]")

    def test_secret_field(self):
        result = hlog.redact({"secret": "shh", "client_secret": "shh2"})
        self.assertEqual(result["secret"], "[REDACTED]")
        self.assertEqual(result["client_secret"], "[REDACTED]")

    def test_token_field(self):
        result = hlog.redact({"token": "t", "access_token": "t2", "idToken": "t3"})
        self.assertEqual(result["token"], "[REDACTED]")
        self.assertEqual(result["access_token"], "[REDACTED]")
        self.assertEqual(result["idToken"], "[REDACTED]")

    def test_nested_redaction(self):
        obj = {
            "outer": "keep",
            "inner_dict": {"api_key": "secret", "keep": "yes"},
            "inner_list": [{"token": "bad"}, {"keep": "yes"}],
        }
        result = hlog.redact(obj)
        self.assertEqual(result["outer"], "keep")
        self.assertEqual(result["inner_dict"]["api_key"], "[REDACTED]")
        self.assertEqual(result["inner_dict"]["keep"], "yes")
        self.assertEqual(result["inner_list"][0]["token"], "[REDACTED]")
        self.assertEqual(result["inner_list"][1]["keep"], "yes")

    def test_non_sensitive_field_unchanged(self):
        result = hlog.redact({"feature_id": "feat-001", "msg": "hello"})
        self.assertEqual(result["feature_id"], "feat-001")
        self.assertEqual(result["msg"], "hello")

    def test_partial_match_not_redacted(self):
        # "key" alone isn't sensitive (no api_key/secret/token substring)
        result = hlog.redact({"key": "value", "tokens_count": 5})
        self.assertEqual(result["key"], "value")
        # "tokens_count" contains "token" → should be redacted
        self.assertEqual(result["tokens_count"], "[REDACTED]")


class TestEmit(unittest.TestCase):
    def _capture(self, fn) -> tuple[dict[str, Any], str]:
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            fn()
        line = buf.getvalue().rstrip("\n")
        return json.loads(line), line

    def test_basic_emit(self):
        line_str, raw = self._capture(lambda: hlog.info("daemon", "started", "daemon started OK"))
        self.assertEqual(line_str["level"], "info")
        self.assertEqual(line_str["component"], "daemon")
        self.assertEqual(line_str["event"], "started")
        self.assertEqual(line_str["msg"], "daemon started OK")
        self.assertIsNone(line_str["project_id"])
        self.assertIsNone(line_str["feature_id"])
        # ts is ISO8601 and parseable
        parsed = datetime.fromisoformat(line_str["ts"])
        self.assertIsNotNone(parsed)

    def test_emit_with_project_and_feature(self):
        line_str, _ = self._capture(
            lambda: hlog.warn(
                "node",
                "feature_failed",
                "agent stopped",
                project_id="proj-1",
                feature_id="feat-007",
                attempt=3,
            )
        )
        self.assertEqual(line_str["project_id"], "proj-1")
        self.assertEqual(line_str["feature_id"], "feat-007")
        self.assertEqual(line_str["attempt"], 3)
        self.assertEqual(line_str["component"], "node")

    def test_emit_redacts_sensitive_kwargs(self):
        line_str, raw = self._capture(
            lambda: hlog.error(
                "daemon",
                "auth_failed",
                "bad key",
                api_key="sk-supersecret-1234",
                project_id="proj-1",
            )
        )
        # The sensitive value must NOT appear anywhere in the raw line
        self.assertNotIn("sk-supersecret-1234", raw)
        # But the field name is still present with [REDACTED]
        self.assertEqual(line_str["api_key"], "[REDACTED]")
        self.assertEqual(line_str["project_id"], "proj-1")


class TestHighVolume(unittest.TestCase):
    def test_100_events_one_line_each_parseable(self):
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            for i in range(100):
                hlog.info("daemon", "tick", f"event {i}", index=i)
        lines = buf.getvalue().split("\n")
        # split produces 101 entries (100 events + trailing empty)
        non_empty = [line for line in lines if line]
        self.assertEqual(len(non_empty), 100)
        for idx, line in enumerate(non_empty):
            parsed = json.loads(line)  # must parse
            self.assertEqual(parsed["index"], idx)
            self.assertEqual(parsed["level"], "info")
            self.assertEqual(parsed["component"], "daemon")
            self.assertEqual(parsed["event"], "tick")


if __name__ == "__main__":
    unittest.main()