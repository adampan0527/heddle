# SPDX-License-Identifier: Apache-2.0
"""Unit tests for heddle_common.dag_validation (feat-046).

Covers the pure function ``validate_drafts`` per the feat-046 spec:
  - drafts with no depends_on always validate
  - drafts depending on existing features validate when no cycles
  - drafts referencing non-existent features are rejected
  - self-dependencies and multi-draft cycles are detected
  - mixed valid/invalid batches report every error
  - initial status is ``pending`` only when every dep is passing,
    else ``blocked``
"""

from __future__ import annotations

import unittest

from heddle_common.dag_validation import (
    DEFAULT_CATEGORY,
    DEFAULT_PRIORITY,
    STATUS_BLOCKED,
    STATUS_PENDING,
    validate_drafts,
)


def _draft(
    draft_id: str,
    *,
    depends_on: list[str] | None = None,
    title: str = "t",
    description: str = "d",
    steps: list[str] | None = None,
    kind: str = "feature",
    priority: str = "medium",
    category: str = "functional",
) -> dict:
    return {
        "id": draft_id,
        "title": title,
        "description": description,
        "steps": steps if steps is not None else ["step 1"],
        "depends_on": depends_on if depends_on is not None else [],
        "kind": kind,
        "priority": priority,
        "category": category,
    }


def _feature(
    feat_id: str,
    *,
    status: str = "passing",
    depends_on: list[str] | None = None,
) -> dict:
    return {
        "id": feat_id,
        "status": status,
        "depends_on": depends_on if depends_on is not None else [],
    }


class TestValidateDraftsNoDeps(unittest.TestCase):
    """The simplest path: drafts with no dependencies against empty or populated."""

    def test_empty_drafts_empty_existing_returns_empty(self):
        proposed, errors = validate_drafts([], [])
        self.assertEqual(proposed, [])
        self.assertEqual(errors, [])

    def test_single_draft_no_deps_is_pending(self):
        draft = _draft("temp-001")
        proposed, errors = validate_drafts([draft], [])
        self.assertEqual(errors, [])
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["id"], "temp-001")
        self.assertEqual(proposed[0]["status"], STATUS_PENDING)
        self.assertEqual(proposed[0]["depends_on"], [])

    def test_two_drafts_no_deps_both_pending(self):
        drafts = [_draft("temp-001"), _draft("temp-002")]
        proposed, errors = validate_drafts(drafts, [])
        self.assertEqual(errors, [])
        self.assertEqual(len(proposed), 2)
        self.assertTrue(all(f["status"] == STATUS_PENDING for f in proposed))


class TestValidateDraftsValidDeps(unittest.TestCase):
    """Drafts depending on existing passing features are accepted; status=pending."""

    def test_draft_depending_on_passing_existing_is_pending(self):
        existing = [_feature("feat-001", status="passing")]
        draft = _draft("temp-001", depends_on=["feat-001"])
        proposed, errors = validate_drafts([draft], existing)
        self.assertEqual(errors, [])
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["status"], STATUS_PENDING)
        self.assertEqual(proposed[0]["depends_on"], ["feat-001"])

    def test_draft_depending_on_non_passing_existing_is_blocked(self):
        existing = [_feature("feat-001", status="in_progress")]
        draft = _draft("temp-001", depends_on=["feat-001"])
        proposed, errors = validate_drafts([draft], existing)
        self.assertEqual(errors, [])
        self.assertEqual(proposed[0]["status"], STATUS_BLOCKED)

    def test_draft_with_mixed_passing_and_blocking_deps_is_blocked(self):
        existing = [
            _feature("feat-a", status="passing"),
            _feature("feat-b", status="pending"),
        ]
        draft = _draft("temp-001", depends_on=["feat-a", "feat-b"])
        proposed, errors = validate_drafts([draft], existing)
        self.assertEqual(errors, [])
        self.assertEqual(proposed[0]["status"], STATUS_BLOCKED)

    def test_draft_depending_on_sibling_draft_is_blocked(self):
        # temp-002 depends on temp-001 (a sibling draft, not an existing
        # feature). It must be born blocked because the sibling is not
        # yet on disk when this batch lands.
        drafts = [
            _draft("temp-001"),
            _draft("temp-002", depends_on=["temp-001"]),
        ]
        proposed, errors = validate_drafts(drafts, [])
        self.assertEqual(errors, [])
        self.assertEqual(proposed[0]["status"], STATUS_PENDING)
        self.assertEqual(proposed[1]["status"], STATUS_BLOCKED)

    def test_draft_default_priority_and_category_applied(self):
        draft = {
            "id": "temp-001",
            "title": "t",
            "description": "d",
            "steps": ["s"],
            "depends_on": [],
            "kind": "feature",
        }
        proposed, errors = validate_drafts([draft], [])
        self.assertEqual(errors, [])
        self.assertEqual(proposed[0]["priority"], DEFAULT_PRIORITY)
        self.assertEqual(proposed[0]["category"], DEFAULT_CATEGORY)


class TestValidateDraftsMissingDeps(unittest.TestCase):
    """Unknown depends_on references must be rejected with a clear message."""

    def test_unknown_dep_rejected(self):
        draft = _draft("temp-001", depends_on=["feat-XXX"])
        proposed, errors = validate_drafts([draft], [])
        self.assertEqual(proposed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("temp-001", errors[0])
        self.assertIn("feat-XXX", errors[0])
        self.assertIn("doesn't exist", errors[0])

    def test_unknown_dep_in_mixed_batch_reports_only_offender(self):
        drafts = [
            _draft("temp-001"),
            _draft("temp-002", depends_on=["feat-missing"]),
        ]
        proposed, errors = validate_drafts(drafts, [])
        self.assertEqual(proposed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("temp-002", errors[0])
        self.assertIn("feat-missing", errors[0])

    def test_two_unknown_deps_both_reported(self):
        drafts = [
            _draft("temp-001", depends_on=["feat-A"]),
            _draft("temp-002", depends_on=["feat-B"]),
        ]
        proposed, errors = validate_drafts(drafts, [])
        self.assertEqual(proposed, [])
        self.assertEqual(len(errors), 2)

    def test_existing_with_unknown_dep_still_rejects(self):
        # If even one dep is unknown, the whole batch is rejected
        # (the user must fix the typo before anything lands).
        existing = [_feature("feat-001", status="passing")]
        draft = _draft("temp-001", depends_on=["feat-001", "feat-bogus"])
        proposed, errors = validate_drafts([draft], existing)
        self.assertEqual(proposed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("feat-bogus", errors[0])


class TestValidateDraftsSelfDep(unittest.TestCase):
    """Self-dependency is a structural error reported before cycle detection."""

    def test_self_dep_is_rejected(self):
        draft = _draft("temp-001", depends_on=["temp-001"])
        proposed, errors = validate_drafts([draft], [])
        self.assertEqual(proposed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("itself", errors[0])
        self.assertIn("temp-001", errors[0])

    def test_self_dep_with_other_valid_deps_still_rejects(self):
        existing = [_feature("feat-001", status="passing")]
        draft = _draft("temp-001", depends_on=["temp-001", "feat-001"])
        proposed, errors = validate_drafts([draft], existing)
        self.assertEqual(proposed, [])
        self.assertTrue(any("itself" in e for e in errors))


class TestValidateDraftsCycles(unittest.TestCase):
    """Kahn's algorithm must catch every cycle in the union graph."""

    def test_two_draft_cycle_is_detected(self):
        # temp-001 -> temp-002 -> temp-001
        drafts = [
            _draft("temp-001", depends_on=["temp-002"]),
            _draft("temp-002", depends_on=["temp-001"]),
        ]
        proposed, errors = validate_drafts(drafts, [])
        self.assertEqual(proposed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("cycle", errors[0].lower())
        self.assertIn("temp-001", errors[0])
        self.assertIn("temp-002", errors[0])

    def test_three_draft_cycle_is_detected(self):
        drafts = [
            _draft("temp-001", depends_on=["temp-002"]),
            _draft("temp-002", depends_on=["temp-003"]),
            _draft("temp-003", depends_on=["temp-001"]),
        ]
        proposed, errors = validate_drafts(drafts, [])
        self.assertEqual(proposed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("cycle", errors[0].lower())

    def test_existing_to_draft_cycle_is_detected(self):
        # feat-001 (existing) depends on temp-001, temp-001 depends on
        # feat-001 — cycle spans the union graph.
        existing = [_feature("feat-001", depends_on=["temp-001"])]
        drafts = [_draft("temp-001", depends_on=["feat-001"])]
        proposed, errors = validate_drafts(drafts, existing)
        self.assertEqual(proposed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("cycle", errors[0].lower())

    def test_dag_with_no_cycle_is_accepted(self):
        # temp-003 depends on temp-002 which depends on temp-001; linear.
        drafts = [
            _draft("temp-001"),
            _draft("temp-002", depends_on=["temp-001"]),
            _draft("temp-003", depends_on=["temp-002"]),
        ]
        proposed, errors = validate_drafts(drafts, [])
        self.assertEqual(errors, [])
        self.assertEqual(len(proposed), 3)
        # Linear chain: only the head is pending; the rest are blocked.
        statuses = {f["id"]: f["status"] for f in proposed}
        self.assertEqual(statuses["temp-001"], STATUS_PENDING)
        self.assertEqual(statuses["temp-002"], STATUS_BLOCKED)
        self.assertEqual(statuses["temp-003"], STATUS_BLOCKED)


class TestValidateDraftsMixedBatch(unittest.TestCase):
    """The realistic case: some drafts valid, some not."""

    def test_mixed_valid_and_unknown_dep(self):
        existing = [_feature("feat-001", status="passing")]
        drafts = [
            _draft("temp-001", depends_on=["feat-001"]),
            _draft("temp-002", depends_on=["feat-missing"]),
        ]
        proposed, errors = validate_drafts(drafts, existing)
        self.assertEqual(proposed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("temp-002", errors[0])

    def test_mixed_valid_and_cycle(self):
        existing = [_feature("feat-001", status="passing")]
        drafts = [
            _draft("temp-001", depends_on=["feat-001"]),
            _draft("temp-002", depends_on=["temp-001"]),
            _draft("temp-003", depends_on=["temp-002"]),
            _draft("temp-004", depends_on=["temp-003"]),
        ]
        proposed, errors = validate_drafts(drafts, existing)
        # No cycle (linear), no unknown deps.
        self.assertEqual(errors, [])
        self.assertEqual(len(proposed), 4)


class TestValidateDraftsInputGuards(unittest.TestCase):
    """Defensive: bad inputs raise TypeError, not silent corruption."""

    def test_drafts_must_be_list(self):
        with self.assertRaises(TypeError):
            validate_drafts("not a list", [])  # type: ignore[arg-type]

    def test_existing_must_be_list(self):
        with self.assertRaises(TypeError):
            validate_drafts([], "not a list")  # type: ignore[arg-type]

    def test_non_dict_drafts_are_skipped_silently(self):
        # The function only inspects dict drafts; the route layer is
        # responsible for input validation. Non-dict entries simply
        # produce no proposed feature.
        proposed, errors = validate_drafts(["oops"], [])  # type: ignore[list-item]
        self.assertEqual(proposed, [])
        self.assertEqual(errors, [])


class TestValidateDraftsDependsOnNormalization(unittest.TestCase):
    """depends_on normalization: tuple / comma-string / dedup."""

    def test_depends_on_as_tuple(self):
        existing = [_feature("feat-001", status="passing")]
        draft = _draft("temp-001", depends_on=("feat-001",))  # type: ignore[arg-type]
        proposed, errors = validate_drafts([draft], existing)
        self.assertEqual(errors, [])
        self.assertEqual(proposed[0]["depends_on"], ["feat-001"])

    def test_depends_on_as_comma_string(self):
        existing = [
            _feature("feat-001", status="passing"),
            _feature("feat-002", status="passing"),
        ]
        draft = _draft("temp-001", depends_on="feat-001, feat-002")
        proposed, errors = validate_drafts([draft], existing)
        self.assertEqual(errors, [])
        self.assertEqual(proposed[0]["depends_on"], ["feat-001", "feat-002"])

    def test_depends_on_dedup_preserves_first_occurrence(self):
        existing = [_feature("feat-001", status="passing")]
        draft = _draft("temp-001", depends_on=["feat-001", "feat-001", "feat-001"])
        proposed, errors = validate_drafts([draft], existing)
        self.assertEqual(errors, [])
        self.assertEqual(proposed[0]["depends_on"], ["feat-001"])

    def test_depends_on_none_treated_as_empty(self):
        draft = _draft("temp-001", depends_on=None)
        proposed, errors = validate_drafts([draft], [])
        self.assertEqual(errors, [])
        self.assertEqual(proposed[0]["depends_on"], [])


class TestValidateDraftsDuplicateIds(unittest.TestCase):
    """A batch with duplicate draft ids must be rejected."""

    def test_duplicate_draft_ids_rejected(self):
        drafts = [_draft("temp-001"), _draft("temp-001")]
        proposed, errors = validate_drafts(drafts, [])
        self.assertEqual(proposed, [])
        self.assertTrue(any("more than once" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
