"""Tests for tools/session_end._build_session_block and the
SESSION_STATUS_LABELS single-source-of-truth contract.

These tests deliberately do NOT exercise cmd_main - that walks
the project root for feature_list.json / current_progress.txt /
git. We only test the pure block-rendering function and the
status-key label lookup.
"""

from __future__ import annotations

import pytest

import session_end
from _constants import SESSION_STATUSES, SESSION_STATUS_LABELS


class TestBuildSessionBlockStatusLabel:
    """Every legal status produces exactly one line of the form
    **Status:** <Label> and the Label must come from
    SESSION_STATUS_LABELS.
    """

    @pytest.mark.parametrize("status", list(SESSION_STATUSES))
    def test_status_renders_label(self, status):
        block = session_end._build_session_block(
            session_number=1,
            feature_id="feat-x",
            commit_sha="abc1234",
            status=status,
            features_passing=["feat-x"],
            notes="",
            today="2026-07-23",
            passing=1,
            total=2,
        )
        expected_label = SESSION_STATUS_LABELS[status]
        assert f"**Status:** {expected_label}" in block
        # Every other label must NOT appear (one label only).
        for other_status in SESSION_STATUSES:
            if other_status == status:
                continue
            assert (
                f"**Status:** {SESSION_STATUS_LABELS[other_status]}"
                not in block
            ), f"block leaked another status label: {block}"

    def test_no_features_passing_renders_placeholder(self):
        block = session_end._build_session_block(
            session_number=1,
            feature_id="feat-x",
            commit_sha="abc1234",
            status="no_op",
            features_passing=[],
            notes="",
            today="2026-07-23",
            passing=0,
            total=0,
        )
        assert "**Features now passing:** (none)" in block


class TestBuildSessionBlockFailLoud:
    """P1-9: an out-of-set status must raise SystemExit (fail loud)
    rather than silently rendering the wrong label or KeyError-ing.
    """

    def test_bogus_status_raises_systemexit(self):
        # We bypass argparse's `choices` check by calling the
        # function directly with a status that is not in
        # SESSION_STATUSES. This simulates the script being
        # invoked programmatically with a bad value.
        with pytest.raises(SystemExit):
            session_end._build_session_block(
                session_number=1,
                feature_id="feat-x",
                commit_sha="abc1234",
                status="bogus",
                features_passing=[],
                notes="",
                today="2026-07-23",
                passing=0,
                total=0,
            )

    def test_none_status_raises_systemexit(self):
        with pytest.raises(SystemExit):
            session_end._build_session_block(
                session_number=1,
                feature_id="feat-x",
                commit_sha="abc1234",
                status=None,
                features_passing=[],
                notes="",
                today="2026-07-23",
                passing=0,
                total=0,
            )

    def test_empty_string_status_raises_systemexit(self):
        with pytest.raises(SystemExit):
            session_end._build_session_block(
                session_number=1,
                feature_id="feat-x",
                commit_sha="abc1234",
                status="",
                features_passing=[],
                notes="",
                today="2026-07-23",
                passing=0,
                total=0,
            )


class TestStatusLabelsContract:
    """SESSION_STATUS_LABELS is the single source of truth."""

    def test_labels_keys_match_statuses(self):
        assert set(SESSION_STATUS_LABELS.keys()) == set(SESSION_STATUSES)

    def test_labels_are_non_empty_strings(self):
        for k, v in SESSION_STATUS_LABELS.items():
            assert isinstance(v, str) and v, f"empty label for {k!r}"

    def test_labels_are_unique(self):
        assert len(set(SESSION_STATUS_LABELS.values())) == len(SESSION_STATUS_LABELS)


class TestFindSummarySection:
    """Regression: _find_summary_section used (?m)/(?im) inline flags
    inside alternation branches, which Python 3.12+ rejects with
    "global flags not at the start of the expression" at compile time.
    The whole module would then fail to import, blocking step 4b of
    session_end before it could refresh the SUMMARY block.

    These tests pin both halves of the contract:
      1. the function compiles on Python 3.12+ (no PatternError);
      2. it matches both ATX (`## SUMMARY`) and setext
         (`SUMMARY\\n=======`) header styles.
    """

    def test_compiles_and_returns_section_for_atx_header(self):
        text = (
            "# prelude\n"
            "\n"
            "## Session 1\n"
            "work\n"
            "## SUMMARY\n"
            "- **Total features**: 5\n"
            "- **Passing**: 2\n"
            "- **Failing**: 3\n"
            "\n"
            "## Another section\n"
            "more\n"
        )
        section = session_end._find_summary_section(text)
        assert section is not None, "should locate ## SUMMARY"
        start, end = section
        assert text[start:start + len("## SUMMARY")] == "## SUMMARY"

    def test_compiles_and_returns_section_for_setext_header(self):
        text = (
            "# prelude\n"
            "SUMMARY\n"
            "=======\n"
            "- Total features: 5\n"
        )
        section = session_end._find_summary_section(text)
        assert section is not None, "should locate setext-style SUMMARY"
        start, _end = section
        assert text[start:start + len("SUMMARY")] == "SUMMARY"

    def test_no_summary_returns_none(self):
        text = "# prelude\nno summary header here\n"
        assert session_end._find_summary_section(text) is None
