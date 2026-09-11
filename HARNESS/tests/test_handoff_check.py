"""Tests for HARNESS/tools/handoff_check.py.

We deliberately test pure helpers only: regex acceptance, file
walkers, and the per-check return shape. cmd_main is NOT
exercised here - it reads project-root files we deliberately want
to leave alone.
"""

from __future__ import annotations

import pytest

import handoff_check
from _constants import SESSION_HEADING_RE


class TestSession0Regex:
    @pytest.mark.parametrize(
        "line",
        [
            "SESSION 0 - Initializer Agent",
            "SESSION 0-Initializer",
            "SESSION 0",
            "  SESSION 0 - Initializer Agent  ",
            "### SESSION 0 - Initializer Agent",
        ],
    )
    def test_accepted(self, line):
        assert handoff_check._SESSION_0_RE.match(line) is not None

    @pytest.mark.parametrize(
        "line",
        [
            "SESSION 1 - something",
            "Just a regular line of prose",
            "",
        ],
    )
    def test_rejected(self, line):
        assert handoff_check._SESSION_0_RE.match(line) is None


class TestSessionHeadingRegex:
    @pytest.mark.parametrize(
        "line",
        [
            "### SESSION 3 — 2026-07-23 ",
            "### SESSION 3 – 2026-07-23 ",
            "### SESSION 3 - 2026-07-23 ",
        ],
    )
    def test_canonical_heading_accepted(self, line):
        m = SESSION_HEADING_RE.search(line)
        assert m is not None
        assert m.group(1) == "3"
        assert m.group(2) == "2026-07-23"

    def test_initializer_bare_session_0_rejected(self):
        bare = "SESSION 0 - Initializer Agent"
        assert SESSION_HEADING_RE.search(bare) is None

    def test_initializer_with_hash_prefix_also_rejected(self):
        with_prefix = "### SESSION 0 - Initializer Agent"
        assert SESSION_HEADING_RE.search(with_prefix) is None

    def test_non_session_h2_rejected(self):
        assert SESSION_HEADING_RE.search("### SUMMARY") is None
        assert SESSION_HEADING_RE.search("## Status values") is None


class TestWalkFeatures:
    def test_returns_features_when_records_ok(self, tmp_path, monkeypatch):
        import json
        path = tmp_path / "feature_list.json"
        path.write_text(json.dumps({
            "features": [{"id": "x", "depends_on": []}],
            "metadata": {},
        }), encoding="utf-8")
        monkeypatch.setattr(handoff_check, "FEATURE_LIST_PATH", path)
        records = [handoff_check.CheckRecord("json_parse", True, "")]
        result = handoff_check._walk_features(records)
        assert result == [{"id": "x", "depends_on": []}]

    def test_returns_none_when_json_parse_failed(self, tmp_path, monkeypatch):
        path = tmp_path / "feature_list.json"
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(handoff_check, "FEATURE_LIST_PATH", path)
        records = [handoff_check.CheckRecord("json_parse", False, "bad")]
        assert handoff_check._walk_features(records) is None


class TestCheckDependsOnAcyclic:
    def _seed(self, tmp_path, monkeypatch, features):
        import json
        path = tmp_path / "feature_list.json"
        path.write_text(json.dumps({"features": features, "metadata": {}}),
                        encoding="utf-8")
        monkeypatch.setattr(handoff_check, "FEATURE_LIST_PATH", path)

    def test_no_cycle_returns_ok(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch, [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["a", "b"]},
        ])
        records = [handoff_check.CheckRecord("json_parse", True, "")]
        rec = handoff_check.check_depends_on_acyclic(records)
        assert rec.ok is True
        assert rec.name == "depends_on_acyclic"

    def test_cycle_returns_fail_with_cycle_string(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch, [
            {"id": "a", "depends_on": ["b"]},
            {"id": "b", "depends_on": ["a"]},
        ])
        records = [handoff_check.CheckRecord("json_parse", True, "")]
        rec = handoff_check.check_depends_on_acyclic(records)
        assert rec.ok is False
        assert "dependency cycle" in rec.detail
        assert "a" in rec.detail
        assert "b" in rec.detail

    def test_skipped_when_json_parse_failed(self, tmp_path, monkeypatch):
        records = [handoff_check.CheckRecord("json_parse", False, "bad")]
        rec = handoff_check.check_depends_on_acyclic(records)
        assert rec.ok is False
        assert "skipped" in rec.detail


class TestCheckMetadataDrift:
    def test_metadata_drift_returns_check_record(self, tmp_path, monkeypatch):
        import json
        path = tmp_path / "feature_list.json"
        path.write_text(json.dumps({
            "features": [],
            "metadata": {
                "total_features": 0, "passing": 0, "failing": 0,
                "in_progress": 0, "blocked": 0, "deferred": 0,
            },
        }), encoding="utf-8")
        monkeypatch.setattr(handoff_check, "FEATURE_LIST_PATH", path)
        monkeypatch.setattr(handoff_check, "TOOLS_DIR", tmp_path / "no_tools")

        records = [handoff_check.CheckRecord("json_parse", True, "")]
        rec = handoff_check.check_metadata_drift(records)
        assert rec.name == "metadata_drift"
        assert isinstance(rec.ok, bool)


class TestCheck9FeatureSchemaPlaceholderSteps:
    """check_9_feature_schema rejects features whose steps are placeholders.

    See tools/_feature_state.PLACEHOLDER_STEP_PATTERNS for the canonical
    set. A feature whose only step is "TBD" or "[fill-in]" cannot be
    implemented, so the harness gate fails the schema check before any
    coding session starts.
    """

    def _seed(self, tmp_path, monkeypatch, features):
        import json
        path = tmp_path / "feature_list.json"
        path.write_text(json.dumps({"features": features, "metadata": {}}),
                        encoding="utf-8")
        monkeypatch.setattr(handoff_check, "FEATURE_LIST_PATH", path)

    @pytest.mark.parametrize("placeholder", [
        "TBD",
        "TBD - to be filled by Initializer",
        "TODO",
        "TODO: write this later",
        "[fill-in]",
        "placeholder",
        "?",
    ])
    def test_placeholder_step_fails_check(self, tmp_path, monkeypatch, placeholder):
        self._seed(tmp_path, monkeypatch, [
            {"id": "f1", "category": "functional", "status": "pending",
             "priority": "medium", "steps": [placeholder],
             "depends_on": [], "attempts": []},
        ])
        records = [handoff_check.CheckRecord("json_parse", True, "")]
        rec = handoff_check.check_9_feature_schema(records)
        assert rec.ok is False
        assert "placeholder" in rec.detail

    def test_real_steps_pass_check(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch, [
            {"id": "f1", "category": "functional", "status": "pending",
             "priority": "medium",
             "steps": ["Click the login button",
                       "Verify the user lands on /home"],
             "depends_on": [], "attempts": []},
        ])
        records = [handoff_check.CheckRecord("json_parse", True, "")]
        rec = handoff_check.check_9_feature_schema(records)
        assert rec.ok is True

    def test_step_with_placeholder_word_in_longer_text_passes(
        self, tmp_path, monkeypatch,
    ):
        """Sanity: a real step that happens to contain 'TBD' inside a
        longer description is NOT rejected (substring match is only for
        prefix tokens like 'TBD -' or 'TODO:')."""
        self._seed(tmp_path, monkeypatch, [
            {"id": "f1", "category": "functional", "status": "pending",
             "priority": "medium",
             "steps": ["TBD is mentioned but this is the actual step"],
             "depends_on": [], "attempts": []},
        ])
        records = [handoff_check.CheckRecord("json_parse", True, "")]
        rec = handoff_check.check_9_feature_schema(records)
        assert rec.ok is True


class TestCheckRecord:
    def test_human_line_ok(self):
        r = handoff_check.CheckRecord("json_parse", True, "looks good")
        assert r.human_line() == "[OK] json_parse: looks good"

    def test_human_line_fail_no_detail(self):
        r = handoff_check.CheckRecord("json_parse", False, "")
        assert r.human_line() == "[FAIL] json_parse"

    def test_json_obj_shape(self):
        r = handoff_check.CheckRecord("x", True, "detail")
        obj = r.json_obj()
        assert obj == {"check": "x", "ok": True, "detail": "detail"}
