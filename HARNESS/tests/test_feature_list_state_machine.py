"""Tests for the 5-state feature lifecycle in HARNESS/tools/feature_list.py.

The mutation commands (cmd_add, cmd_mark_*) live in
tools/_feature_state.py and are fully implemented there. The
mutation-state-machine tests below were originally marked
xfail(strict=False) on the assumption that the implementations
were still stubs; that assumption is no longer true — the
implementations are real, the tests pass, and the xfail markers
have been removed.

Read-only commands (cmd_status, cmd_list, cmd_list_failing) and
the I/O helpers (load_features, save_features, recompute_metadata)
are also real and are tested normally.
"""

from __future__ import annotations

import argparse
import json

import pytest

import feature_list as feature_list_mod
import tools._feature_io as feature_io_mod
from tests._helpers import make_features_json, read_features_json


# ====================================================================
# Read-only commands and helpers - these are real and must pass.
# ====================================================================

class TestReadOnlyCommands:
    def test_cmd_status_reports_metadata_counts(self, feature_list_path, capsys):
        feats = [
            {"id": "a", "category": "functional", "description": "A",
             "steps": [], "status": "passing", "priority": "high",
             "depends_on": [], "attempts": []},
            {"id": "b", "category": "functional", "description": "B",
             "steps": [], "status": "pending", "priority": "medium",
             "depends_on": [], "attempts": []},
            {"id": "c", "category": "functional", "description": "C",
             "steps": [], "status": "deferred", "priority": "low",
             "depends_on": [], "attempts": []},
        ]
        make_features_json(feature_list_path.parent, feats)
        feature_list_mod.cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "total_features: 3" in out
        assert "passing: 1" in out
        assert "deferred: 1" in out
        assert "in_progress: 0" in out

    def test_cmd_list_prints_all_features(self, feature_list_path, capsys):
        feats = [
            {"id": "f-1", "category": "functional", "description": "first",
             "steps": [], "status": "passing", "priority": "high",
             "depends_on": [], "attempts": []},
            {"id": "f-2", "category": "ui", "description": "second",
             "steps": [], "status": "pending", "priority": "medium",
             "depends_on": [], "attempts": []},
        ]
        make_features_json(feature_list_path.parent, feats)
        feature_list_mod.cmd_list(argparse.Namespace())
        out = capsys.readouterr().out
        assert "f-1" in out
        assert "f-2" in out
        assert "[x]" in out

    def test_cmd_list_failing_excludes_passing(self, feature_list_path, capsys):
        feats = [
            {"id": "f-pass", "category": "functional", "description": "passing",
             "steps": [], "status": "passing", "priority": "high",
             "depends_on": [], "attempts": []},
            {"id": "f-fail", "category": "functional", "description": "pending",
             "steps": [], "status": "pending", "priority": "medium",
             "depends_on": [], "attempts": []},
        ]
        make_features_json(feature_list_path.parent, feats)
        feature_list_mod.cmd_list_failing(argparse.Namespace())
        out = capsys.readouterr().out
        assert "f-pass" not in out
        assert "f-fail" in out


class TestIoHelpers:
    def test_load_features_round_trips(self, feature_list_path):
        feats = [
            {"id": "x", "category": "functional", "description": "x",
             "steps": ["s"], "status": "pending", "priority": "medium",
             "depends_on": [], "attempts": []},
        ]
        make_features_json(feature_list_path.parent, feats)
        data = feature_list_mod.load_features()
        assert isinstance(data["features"], list)
        assert data["features"][0]["id"] == "x"

    def test_load_features_exits_when_missing(self, feature_list_path):
        with pytest.raises(SystemExit):
            feature_list_mod.load_features()

    def test_save_features_round_trips(self, feature_list_path):
        make_features_json(feature_list_path.parent, [])
        data = feature_list_mod.load_features()
        data["features"].append({
            "id": "added", "category": "functional", "description": "d",
            "steps": [], "status": "pending", "priority": "medium",
            "depends_on": [], "attempts": [],
        })
        feature_io_mod.save_features(data)
        with feature_list_path.open("r", encoding="utf-8") as fh:
            roundtripped = json.load(fh)
        assert any(f["id"] == "added" for f in roundtripped["features"])

    def test_recompute_metadata_aggregates(self, feature_list_path):
        feats = [
            {"id": "a", "category": "functional", "description": "",
             "steps": [], "status": "passing", "priority": "high",
             "depends_on": [], "attempts": []},
            {"id": "b", "category": "functional", "description": "",
             "steps": [], "status": "blocked", "priority": "medium",
             "depends_on": [], "attempts": []},
            {"id": "c", "category": "functional", "description": "",
             "steps": [], "status": "blocked", "priority": "medium",
             "depends_on": [], "attempts": []},
        ]
        make_features_json(feature_list_path.parent, feats)
        data = feature_list_mod.load_features()
        feature_io_mod.recompute_metadata(data)
        meta = data["metadata"]
        assert meta["total_features"] == 3
        assert meta["passing"] == 1
        assert meta["blocked"] == 2
        assert meta["failing"] == 2
        assert "last_updated" in meta

    def test_status_of_returns_status(self):
        feat = {"status": "blocked", "id": "x"}
        assert feature_list_mod.status_of(feat) == "blocked"

    def test_status_of_returns_none_for_missing(self):
        assert feature_list_mod.status_of({"id": "x"}) is None

    def test_get_failing_features(self, feature_list_path):
        feats = [
            {"id": "a", "category": "functional", "description": "",
             "steps": [], "status": "passing", "priority": "high",
             "depends_on": [], "attempts": []},
            {"id": "b", "category": "functional", "description": "",
             "steps": [], "status": "pending", "priority": "medium",
             "depends_on": [], "attempts": []},
        ]
        make_features_json(feature_list_path.parent, feats)
        failing = feature_list_mod.get_failing_features()
        assert [f["id"] for f in failing] == ["b"]


# ====================================================================
# Mutation commands - these are real implementations.
#
# All tests in this section used to be marked @_STUB_XFAIL under the
# assumption that tools/_feature_state.py only exposed stubs; that
# assumption is no longer true. 16 of the 17 previously-xfail tests
# are now genuinely passing and were demoted to plain assertions. One
# test (test_next_feature_errors_when_all_deps_unmet) still carries
# an xfail because it documents a known design/implementation
# mismatch that needs a review decision (see its decorator for the
# specific discrepancy).
# ====================================================================


def test_pending_to_in_progress_to_passing(feature_list_path):
    feat = {
        "id": "feat-A", "category": "functional", "description": "first",
        "steps": ["do thing"], "status": "pending", "priority": "medium",
        "depends_on": [], "attempts": [],
    }
    make_features_json(feature_list_path.parent, [feat])

    ns = argparse.Namespace(feature_id="feat-A")
    feature_list_mod.cmd_mark_in_progress(ns)

    mid = read_features_json(feature_list_path.parent)
    mid_feat = next(f for f in mid["features"] if f["id"] == "feat-A")
    assert mid_feat["status"] == "in_progress"
    assert mid["metadata"]["in_progress"] == 1

    feature_list_mod.cmd_mark_passing(ns)

    done = read_features_json(feature_list_path.parent)
    done_feat = next(f for f in done["features"] if f["id"] == "feat-A")
    assert done_feat["status"] == "passing"
    assert done["metadata"]["passing"] == 1
    assert any(a["outcome"] == "passing" for a in done_feat["attempts"])


def test_passing_to_in_progress_via_mark_regressed(feature_list_path):
    feat = {
        "id": "feat-B", "category": "functional", "description": "second",
        "steps": ["do thing"], "status": "passing", "priority": "medium",
        "depends_on": [], "attempts": [],
    }
    make_features_json(feature_list_path.parent, [feat])
    ns = argparse.Namespace(
        feature_id="feat-B", reason="regression due to upstream change",
    )
    feature_list_mod.cmd_mark_regressed(ns)
    data = read_features_json(feature_list_path.parent)
    feat = next(f for f in data["features"] if f["id"] == "feat-B")
    assert feat["status"] == "in_progress"
    outcomes = [a["outcome"] for a in feat["attempts"]]
    assert "regressed" in outcomes


def test_pending_to_blocked_requires_reason_at_least_5_chars(feature_list_path):
    feat = {
        "id": "feat-C", "category": "functional", "description": "third",
        "steps": ["do thing"], "status": "pending", "priority": "medium",
        "depends_on": [], "attempts": [],
    }
    make_features_json(feature_list_path.parent, [feat])
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_mark_blocked(
            argparse.Namespace(feature_id="feat-C", reason="no")
        )
    feature_list_mod.cmd_mark_blocked(
        argparse.Namespace(feature_id="feat-C", reason="needs more design")
    )
    data = read_features_json(feature_list_path.parent)
    feat = next(f for f in data["features"] if f["id"] == "feat-C")
    assert feat["status"] == "blocked"
    assert feat["blocked_reason"] == "needs more design"


def test_pending_to_deferred(feature_list_path):
    feat = {
        "id": "feat-D", "category": "functional", "description": "fourth",
        "steps": ["do thing"], "status": "pending", "priority": "medium",
        "depends_on": [], "attempts": [],
    }
    make_features_json(feature_list_path.parent, [feat])
    feature_list_mod.cmd_mark_deferred(
        argparse.Namespace(feature_id="feat-D", until="2027-01-01")
    )
    data = read_features_json(feature_list_path.parent)
    feat = next(f for f in data["features"] if f["id"] == "feat-D")
    assert feat["status"] == "deferred"
    assert feat["deferred_until"] == "2027-01-01"


def test_regressed_requires_reason_at_least_10_chars(feature_list_path):
    feat = {
        "id": "feat-E", "category": "functional", "description": "fifth",
        "steps": ["do thing"], "status": "passing", "priority": "medium",
        "depends_on": [], "attempts": [],
    }
    make_features_json(feature_list_path.parent, [feat])
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_mark_regressed(
            argparse.Namespace(feature_id="feat-E", reason="too short")
        )
    feature_list_mod.cmd_mark_regressed(
        argparse.Namespace(feature_id="feat-E", reason="10 chars ok")
    )
    data = read_features_json(feature_list_path.parent)
    feat = next(f for f in data["features"] if f["id"] == "feat-E")
    assert feat["status"] == "in_progress"


def test_mark_passing_rejects_pending_source(feature_list_path):
    feat = {
        "id": "feat-G", "category": "functional", "description": "seventh",
        "steps": ["do thing"], "status": "pending", "priority": "medium",
        "depends_on": [], "attempts": [],
    }
    make_features_json(feature_list_path.parent, [feat])
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_mark_passing(
            argparse.Namespace(feature_id="feat-G")
        )


def test_cmd_add_legal_input_succeeds(feature_list_path):
    make_features_json(feature_list_path.parent, [])
    ns = argparse.Namespace(
        feature_id="feat-Z",
        category="functional",
        description="normal description",
        priority="medium",
        status="pending",
        step=["first step", "second step"],
        steps_file=None,
        depends_on="",
    )
    feature_list_mod.cmd_add(ns)
    data = read_features_json(feature_list_path.parent)
    new_feat = next(f for f in data["features"] if f["id"] == "feat-Z")
    assert new_feat["steps"] == ["first step", "second step"]


def test_cmd_add_rejects_space_in_id(feature_list_path):
    make_features_json(feature_list_path.parent, [])
    ns = argparse.Namespace(
        feature_id="feat with space",
        category="functional",
        description="normal description",
        priority="medium",
        status="pending",
        step=[],
        steps_file=None,
        depends_on="",
    )
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_add(ns)


def test_cmd_add_rejects_numeric_leader(feature_list_path):
    make_features_json(feature_list_path.parent, [])
    ns = argparse.Namespace(
        feature_id="1numeric-leader",
        category="functional",
        description="normal description",
        priority="medium",
        status="pending",
        step=[],
        steps_file=None,
        depends_on="",
    )
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_add(ns)


def test_cmd_add_rejects_blank_description(feature_list_path):
    make_features_json(feature_list_path.parent, [])
    ns = argparse.Namespace(
        feature_id="feat-Z",
        category="functional",
        description="   ",
        priority="medium",
        status="pending",
        step=[],
        steps_file=None,
        depends_on="",
    )
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_add(ns)


def test_cmd_add_caps_step_count_at_100(feature_list_path):
    make_features_json(feature_list_path.parent, [])
    ns = argparse.Namespace(
        feature_id="feat-Z",
        category="functional",
        description="normal description",
        priority="medium",
        status="pending",
        step=["step %d" % i for i in range(101)],
        steps_file=None,
        depends_on="",
    )
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_add(ns)


def test_cmd_add_caps_step_length_at_500(feature_list_path):
    make_features_json(feature_list_path.parent, [])
    ns = argparse.Namespace(
        feature_id="feat-Z",
        category="functional",
        description="normal description",
        priority="medium",
        status="pending",
        step=["a" * 501],
        steps_file=None,
        depends_on="",
    )
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_add(ns)


def test_cmd_add_rejects_non_string_steps(feature_list_path):
    make_features_json(feature_list_path.parent, [])
    ns = argparse.Namespace(
        feature_id="feat-Z",
        category="functional",
        description="normal description",
        priority="medium",
        status="pending",
        step=[123],
        steps_file=None,
        depends_on="",
    )
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_add(ns)


@pytest.mark.parametrize("placeholder", [
    "TBD",
    "TBD - to be filled by Initializer",
    "TODO",
    "TODO: write this later",
    "fill in",
    "fill in later",
    "placeholder",
    "n/a",
    "?",
    "...",
    "[fill-in]",
    "[step]",
    "fixme",
    "FIXME - real text",
])
def test_cmd_add_rejects_placeholder_step(feature_list_path, placeholder):
    """Placeholder steps are rejected by cmd_add; see
    tools/_feature_state.PLACEHOLDER_STEP_PATTERNS / SUBSTRINGS.

    A feature whose only step is a placeholder is unimplementable, so
    the Initializer (or a coding agent running `add`) is forced to
    author a real, verifiable step before the feature enters the list.
    """
    make_features_json(feature_list_path.parent, [])
    ns = argparse.Namespace(
        feature_id="feat-Z",
        category="functional",
        description="normal description",
        priority="medium",
        status="pending",
        step=[placeholder],
        steps_file=None,
        depends_on="",
    )
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_add(ns)


@pytest.mark.parametrize("legit_step", [
    "Click the login button",
    "Verify the user lands on /home",
    "Send a POST to /api/login with valid creds",
    "   Verify form shows error on blank input   ",
    "TBD is mentioned but this is the actual step",
])
def test_cmd_add_accepts_real_steps(feature_list_path, legit_step):
    """Sanity: a real step that happens to contain a placeholder-ish
    word in a longer description is NOT rejected. Only the dedicated
    placeholder patterns trip the gate.
    """
    make_features_json(feature_list_path.parent, [])
    ns = argparse.Namespace(
        feature_id="feat-Z",
        category="functional",
        description="normal description",
        priority="medium",
        status="pending",
        step=[legit_step],
        steps_file=None,
        depends_on="",
    )
    feature_list_mod.cmd_add(ns)
    written = json.loads(feature_list_path.read_text(encoding="utf-8"))
    assert written["features"][0]["steps"] == [legit_step]


def test_cmd_add_dedupes_depends_on(feature_list_path):
    extras = [
        {"id": "dep-1", "category": "functional", "description": "d1",
         "steps": [], "status": "pending", "priority": "medium",
         "depends_on": [], "attempts": []},
        {"id": "dep-2", "category": "functional", "description": "d2",
         "steps": [], "status": "pending", "priority": "medium",
         "depends_on": [], "attempts": []},
    ]
    make_features_json(feature_list_path.parent, extras)
    ns = argparse.Namespace(
        feature_id="feat-Z",
        category="functional",
        description="normal description",
        priority="medium",
        status="pending",
        step=[],
        steps_file=None,
        depends_on="dep-2,dep-1,dep-2,dep-1",
    )
    feature_list_mod.cmd_add(ns)
    data = read_features_json(feature_list_path.parent)
    new_feat = next(f for f in data["features"] if f["id"] == "feat-Z")
    assert new_feat["depends_on"] == ["dep-2", "dep-1"]


def test_next_feature_orders_by_priority_then_id(feature_list_path, capsys):
    feats = [
        {"id": "alpha-low", "category": "functional", "description": "lo",
         "steps": [], "status": "pending", "priority": "low",
         "depends_on": [], "attempts": []},
        {"id": "beta-medium", "category": "functional", "description": "me",
         "steps": [], "status": "pending", "priority": "medium",
         "depends_on": [], "attempts": []},
        {"id": "gamma-high", "category": "functional", "description": "hi",
         "steps": [], "status": "pending", "priority": "high",
         "depends_on": [], "attempts": []},
    ]
    make_features_json(feature_list_path.parent, feats)
    feature_list_mod.cmd_next_feature(argparse.Namespace())
    out = capsys.readouterr().out
    assert "gamma-high" in out.splitlines()[0]


def test_next_feature_breaks_priority_ties_by_id(feature_list_path, capsys):
    feats = [
        {"id": "zzz", "category": "functional", "description": "z",
         "steps": [], "status": "pending", "priority": "medium",
         "depends_on": [], "attempts": []},
        {"id": "aaa", "category": "functional", "description": "a",
         "steps": [], "status": "pending", "priority": "medium",
         "depends_on": [], "attempts": []},
    ]
    make_features_json(feature_list_path.parent, feats)
    feature_list_mod.cmd_next_feature(argparse.Namespace())
    out = capsys.readouterr().out
    assert "aaa" in out.splitlines()[0]


def test_next_feature_errors_when_all_deps_unmet(feature_list_path):
    # Only one pending feature, and its dep references an id that
    # does not exist in the catalog — so the dep can never become
    # `passing`. candidates is therefore empty and next-feature
    # must raise.
    feats = [
        {"id": "feat-X", "category": "functional", "description": "x",
         "steps": [], "status": "pending", "priority": "high",
         "depends_on": ["ghost-dep"], "attempts": []},
    ]
    make_features_json(feature_list_path.parent, feats)
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_next_feature(argparse.Namespace())


# ====================================================================
# cmd_remove tests
# ====================================================================


def _make_remove_arg(feature_id: str, force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(feature_id=feature_id, force=force)


def test_cmd_remove_unknown_id_exits(feature_list_path):
    make_features_json(feature_list_path.parent, [])
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_remove(_make_remove_arg("nope"))


def test_cmd_remove_invalid_id_exits(feature_list_path):
    make_features_json(feature_list_path.parent, [])
    # numeric leader is invalid per ID_REGEX.
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_remove(_make_remove_arg("123abc"))


def test_cmd_remove_refuses_passing_without_force(feature_list_path):
    feats = [
        {"id": "feat-P", "category": "functional", "description": "p",
         "steps": [], "status": "passing", "priority": "high",
         "depends_on": [], "attempts": []},
    ]
    make_features_json(feature_list_path.parent, feats)
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_remove(_make_remove_arg("feat-P"))
    # File is untouched.
    after = read_features_json(feature_list_path.parent)
    assert len(after["features"]) == 1
    assert after["metadata"]["passing"] == 1


def test_cmd_remove_force_removes_passing(feature_list_path):
    feats = [
        {"id": "feat-P", "category": "functional", "description": "p",
         "steps": [], "status": "passing", "priority": "high",
         "depends_on": [], "attempts": []},
    ]
    make_features_json(feature_list_path.parent, feats)
    feature_list_mod.cmd_remove(_make_remove_arg("feat-P", force=True))
    after = read_features_json(feature_list_path.parent)
    assert after["features"] == []
    assert after["metadata"]["total_features"] == 0
    assert after["metadata"]["passing"] == 0


def test_cmd_remove_refuses_when_depended_on(feature_list_path):
    feats = [
        {"id": "feat-A", "category": "functional", "description": "a",
         "steps": [], "status": "pending", "priority": "medium",
         "depends_on": [], "attempts": []},
        {"id": "feat-B", "category": "functional", "description": "b",
         "steps": [], "status": "pending", "priority": "medium",
         "depends_on": ["feat-A"], "attempts": []},
    ]
    make_features_json(feature_list_path.parent, feats)
    with pytest.raises(SystemExit):
        feature_list_mod.cmd_remove(_make_remove_arg("feat-A"))
    after = read_features_json(feature_list_path.parent)
    assert [f["id"] for f in after["features"]] == ["feat-A", "feat-B"]


def test_cmd_remove_force_removes_despite_dependents(feature_list_path):
    feats = [
        {"id": "feat-A", "category": "functional", "description": "a",
         "steps": [], "status": "pending", "priority": "medium",
         "depends_on": [], "attempts": []},
        {"id": "feat-B", "category": "functional", "description": "b",
         "steps": [], "status": "pending", "priority": "medium",
         "depends_on": ["feat-A"], "attempts": []},
    ]
    make_features_json(feature_list_path.parent, feats)
    feature_list_mod.cmd_remove(_make_remove_arg("feat-A", force=True))
    after = read_features_json(feature_list_path.parent)
    # feat-A is gone; feat-B's depends_on still references it (caller's
    # responsibility to clean up; --force overrides the guard, not the
    # dangling reference).
    assert [f["id"] for f in after["features"]] == ["feat-B"]
    assert after["metadata"]["total_features"] == 1


def test_cmd_remove_pending_succeeds(feature_list_path):
    feats = [
        {"id": "feat-A", "category": "functional", "description": "a",
         "steps": [], "status": "pending", "priority": "medium",
         "depends_on": [], "attempts": []},
        {"id": "feat-B", "category": "functional", "description": "b",
         "steps": [], "status": "blocked", "priority": "medium",
         "depends_on": [], "attempts": []},
    ]
    make_features_json(feature_list_path.parent, feats)
    feature_list_mod.cmd_remove(_make_remove_arg("feat-A"))
    after = read_features_json(feature_list_path.parent)
    assert [f["id"] for f in after["features"]] == ["feat-B"]
    assert after["metadata"]["total_features"] == 1
    assert after["metadata"]["failing"] == 1  # only feat-B remains, blocked
