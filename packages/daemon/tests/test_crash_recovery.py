# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``heddle_daemon.crash_recovery`` — feat-047.

Covers the three behavioural outcomes of
``resume_in_flight_features``:

  1. Healthy DB + thread present -> feature marked ``resumed``.
  2. Healthy DB + thread missing -> feature blocked with
     "no checkpoint thread found for <fid>".
  3. Unhealthy DB -> every in-flight feature blocked with
     "checkpoint loss detected on daemon respawn".

Plus defensive paths: missing ``feature_list.json``, no in-flight
features, ``checkpoint_store=None``, and restart-budget exhaustion
marking the feature blocked with "restart budget exhausted".

The pure-function tests inject deterministic ``known_thread_ids``
and a fake ``RestartBudgetCounter`` so no real SQLite DB is needed.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from heddle_common import feature_list_io as fl

from heddle_daemon.crash_recovery import (
    BLOCKED_REASON_CHECKPOINT_LOSS,
    BLOCKED_REASON_RESTART_BUDGET,
    BlockedReason,
    ResumeReport,
    mark_in_flight_blocked,
    resume_in_flight_features,
)
from heddle_daemon.crash_recovery_io import _in_progress_feature_ids


# ---------- helpers ----------


class _TempProject:
    """Context manager for an isolated tmp project dir."""

    def __init__(self) -> None:
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="heddle_crash_test_"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)


def _seed_feature_list(
    project_path: Path,
    *,
    feature_ids: list[str],
    status: str = "in_progress",
) -> Path:
    """Write a feature_list.json with the given feature_ids."""
    feature_list = project_path / "feature_list.json"
    fl.save(
        feature_list,
        {
            "features": [
                {
                    "id": fid,
                    "category": "functional",
                    "description": f"test feature {fid}",
                    "steps": [f"step one for {fid}"],
                    "status": status,
                    "priority": "medium",
                    "depends_on": [],
                    "attempts": [],
                    "kind": "feature",
                    "fixes": None,
                    "enhances": None,
                    "superseded_by": None,
                    "implementation_model": None,
                }
                for fid in feature_ids
            ],
        },
    )
    return feature_list


def _read_features(feature_list_path: Path) -> dict[str, dict[str, Any]]:
    """Return ``{feature_id: feature_dict}`` from feature_list.json."""
    data = json.loads(feature_list_path.read_text(encoding="utf-8"))
    return {f["id"]: f for f in data["features"]}


def _make_healthy_checkpoint_db(project_path: Path) -> None:
    """Create a real, integrity-check-passing SQLite DB.

    Needed by the healthy-DB tests because
    ``is_checkpoint_db_healthy`` is fail-closed — without an actual
    ``.heddle/checkpoints.db`` it returns False and every in-flight
    feature is blocked. We don't care about the schema contents
    (the orchestrator consults ``known_thread_ids`` directly via
    the test injection), only that the file passes integrity_check.
    """
    heddle = project_path / ".heddle"
    heddle.mkdir(parents=True, exist_ok=True)
    db_path = heddle / "checkpoints.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()


class _FakeCounter:
    """In-memory RestartBudgetCounter fake with explicit threshold.

    Tracks recorded restarts so tests can assert ``record_restart``
    was called exactly once per "healthy" classification.
    """

    def __init__(
        self,
        *,
        feature_id: str,
        is_exhausted: bool = False,
    ) -> None:
        self.feature_id = feature_id
        self._is_exhausted = is_exhausted
        self.recorded: list[datetime] = []

    def record_restart(self, now: datetime) -> None:
        self.recorded.append(now)

    def is_exhausted(self, now: datetime) -> bool:
        return self._is_exhausted


def _counter_factory(
    *,
    exhausted_for: set[str] | None = None,
) -> dict[str, _FakeCounter]:
    """Build a dict-backed counter factory keyed by feature_id."""
    exhausted = exhausted_for or set()
    store: dict[str, _FakeCounter] = {}

    def _get(feature_id: str) -> _FakeCounter:
        if feature_id not in store:
            store[feature_id] = _FakeCounter(
                feature_id=feature_id,
                is_exhausted=feature_id in exhausted,
            )
        return store[feature_id]

    _get.store = store  # type: ignore[attr-defined]
    return _get


def _run(coro):
    """Drive an async coroutine from a sync test method."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------- tests ----------


class TestResumeHealthyDb(unittest.TestCase):
    """Healthy DB + thread present -> feature marked resumed."""

    def test_healthy_db_with_thread_resumes_feature(self):
        with _TempProject() as proj:
            _make_healthy_checkpoint_db(proj)
            _seed_feature_list(proj, feature_ids=["feat-047"], status="in_progress")
            # checkpoint_store=None is acceptable when
            # known_thread_ids is supplied directly.
            getter = _counter_factory()
            report = _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                    known_thread_ids={"feat-047"},
                )
            )
            self.assertIsInstance(report, ResumeReport)
            self.assertEqual(report.resumed, ["feat-047"])
            self.assertEqual(report.blocked, [])
            # Counter must have been incremented exactly once.
            self.assertEqual(len(getter.store["feat-047"].recorded), 1)

    def test_healthy_db_no_in_flight_features_returns_empty_report(self):
        with _TempProject() as proj:
            _make_healthy_checkpoint_db(proj)
            _seed_feature_list(proj, feature_ids=["feat-001"], status="pending")
            getter = _counter_factory()
            report = _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                    known_thread_ids={"feat-001"},
                )
            )
            self.assertEqual(report.resumed, [])
            self.assertEqual(report.blocked, [])


class TestResumeMissingThread(unittest.TestCase):
    """Healthy DB but the feature has no checkpoint thread -> blocked."""

    def test_healthy_db_missing_thread_blocks_feature(self):
        with _TempProject() as proj:
            _make_healthy_checkpoint_db(proj)
            _seed_feature_list(proj, feature_ids=["feat-047"], status="in_progress")
            getter = _counter_factory()
            report = _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                    known_thread_ids=set(),  # no threads in the saver
                )
            )
            self.assertEqual(report.resumed, [])
            self.assertEqual(len(report.blocked), 1)
            block = report.blocked[0]
            self.assertIsInstance(block, BlockedReason)
            self.assertEqual(block.feature_id, "feat-047")
            self.assertIn(BLOCKED_REASON_CHECKPOINT_LOSS, block.reason)
            self.assertIn("no checkpoint thread found", block.reason)
            # feature_list.json reflects the transition.
            features = _read_features(proj / "feature_list.json")
            self.assertEqual(features["feat-047"]["status"], "blocked")
            self.assertIn("checkpoint loss", features["feat-047"]["blocked_reason"].lower())


class TestResumeCheckpointLoss(unittest.TestCase):
    """Unhealthy DB -> every in-flight feature blocked with checkpoint loss."""

    def test_missing_heddle_dir_blocks_all_in_flight(self):
        with _TempProject() as proj:
            _seed_feature_list(
                proj,
                feature_ids=["feat-047", "feat-024"],
                status="in_progress",
            )
            getter = _counter_factory()
            report = _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                    known_thread_ids={"feat-047", "feat-024"},
                )
            )
            self.assertEqual(report.resumed, [])
            self.assertEqual(len(report.blocked), 2)
            for b in report.blocked:
                self.assertEqual(b.reason, BLOCKED_REASON_CHECKPOINT_LOSS)
            # Both features blocked on disk.
            features = _read_features(proj / "feature_list.json")
            for fid in ("feat-047", "feat-024"):
                self.assertEqual(features[fid]["status"], "blocked")
                self.assertIn(
                    "checkpoint loss",
                    features[fid]["blocked_reason"].lower(),
                )

    def test_corrupt_checkpoints_db_blocks_all_in_flight(self):
        with _TempProject() as proj:
            _seed_feature_list(proj, feature_ids=["feat-047"], status="in_progress")
            heddle = proj / ".heddle"
            heddle.mkdir(parents=True)
            (heddle / "checkpoints.db").write_bytes(b"not a sqlite file")
            getter = _counter_factory()
            report = _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                    known_thread_ids={"feat-047"},
                )
            )
            self.assertEqual(report.resumed, [])
            self.assertEqual(len(report.blocked), 1)
            self.assertEqual(
                report.blocked[0].reason, BLOCKED_REASON_CHECKPOINT_LOSS,
            )

    def test_no_store_no_override_blocks_all(self):
        """No checkpoint_store AND no known_thread_ids -> checkpoint loss."""
        with _TempProject() as proj:
            _seed_feature_list(proj, feature_ids=["feat-047"], status="in_progress")
            # No ``.heddle/`` directory => unhealthy + no threads.
            getter = _counter_factory()
            report = _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                )
            )
            self.assertEqual(report.resumed, [])
            self.assertEqual(len(report.blocked), 1)
            self.assertEqual(
                report.blocked[0].reason, BLOCKED_REASON_CHECKPOINT_LOSS,
            )


class TestResumeBudgetExhausted(unittest.TestCase):
    """Healthy DB + thread + exhausted budget -> blocked with budget reason."""

    def test_exhausted_counter_blocks_feature(self):
        with _TempProject() as proj:
            _make_healthy_checkpoint_db(proj)
            _seed_feature_list(proj, feature_ids=["feat-047"], status="in_progress")
            getter = _counter_factory(exhausted_for={"feat-047"})
            report = _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                    known_thread_ids={"feat-047"},
                )
            )
            self.assertEqual(report.resumed, [])
            self.assertEqual(len(report.blocked), 1)
            block = report.blocked[0]
            self.assertEqual(block.feature_id, "feat-047")
            self.assertEqual(block.reason, BLOCKED_REASON_RESTART_BUDGET)
            # Counter was still incremented before the budget check.
            self.assertEqual(len(getter.store["feat-047"].recorded), 1)
            features = _read_features(proj / "feature_list.json")
            self.assertEqual(features["feat-047"]["status"], "blocked")
            self.assertEqual(
                features["feat-047"]["blocked_reason"],
                BLOCKED_REASON_RESTART_BUDGET,
            )

    def test_mixed_outcomes(self):
        """Multiple features -> some resumed, some blocked.

        feat-047 resumes (budget ok); feat-024 budget exhausted;
        feat-099 has no thread in the saver.
        """
        with _TempProject() as proj:
            _make_healthy_checkpoint_db(proj)
            _seed_feature_list(
                proj,
                feature_ids=["feat-047", "feat-024", "feat-099"],
                status="in_progress",
            )
            getter = _counter_factory(exhausted_for={"feat-024"})
            report = _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                    known_thread_ids={"feat-047", "feat-024"},
                    # feat-099 intentionally absent.
                )
            )
            self.assertEqual(report.resumed, ["feat-047"])
            self.assertEqual(
                {b.feature_id: b.reason for b in report.blocked},
                {
                    "feat-024": BLOCKED_REASON_RESTART_BUDGET,
                    "feat-099": (
                        f"{BLOCKED_REASON_CHECKPOINT_LOSS}: "
                        "no checkpoint thread found for feat-099"
                    ),
                },
            )


class TestDefensive(unittest.TestCase):
    """Edge cases that must not crash."""

    def test_missing_feature_list_returns_empty_report(self):
        with _TempProject() as proj:
            # No feature_list.json on disk.
            getter = _counter_factory()
            report = _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                    known_thread_ids=set(),
                )
            )
            self.assertEqual(report.resumed, [])
            self.assertEqual(report.blocked, [])

    def test_malformed_feature_list_does_not_raise(self):
        with _TempProject() as proj:
            (proj / "feature_list.json").write_text("{not json", encoding="utf-8")
            getter = _counter_factory()
            # feature_list_io.load raises SystemExit on bad JSON;
            # the orchestrator must catch it and return empty.
            report = _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                    known_thread_ids=set(),
                )
            )
            self.assertEqual(report.resumed, [])
            self.assertEqual(report.blocked, [])

    def test_frozen_now_is_passed_through(self):
        with _TempProject() as proj:
            _make_healthy_checkpoint_db(proj)
            _seed_feature_list(proj, feature_ids=["feat-047"], status="in_progress")
            getter = _counter_factory()
            now = datetime(2026, 1, 1, tzinfo=UTC)
            _run(
                resume_in_flight_features(
                    project_path=proj,
                    feature_list_path=proj / "feature_list.json",
                    checkpoint_store=None,
                    restart_counter_getter=getter,
                    known_thread_ids={"feat-047"},
                    now=now,
                )
            )
            # The frozen ``now`` is what the counter saw.
            self.assertEqual(getter.store["feat-047"].recorded, [now])


class TestInProgressFeatureIds(unittest.TestCase):
    """The helper that the orchestrator uses to enumerate candidates."""

    def test_returns_only_in_progress_ids(self):
        with _TempProject() as proj:
            _seed_feature_list(
                proj,
                feature_ids=["feat-a", "feat-b", "feat-c"],
                status="in_progress",
            )
            # Flip one to pending and one to passing on disk.
            data = fl.load(proj / "feature_list.json")
            data["features"][0]["status"] = "pending"
            data["features"][1]["status"] = "passing"
            fl.save(proj / "feature_list.json", data)
            ids = _in_progress_feature_ids(proj / "feature_list.json")
            self.assertEqual(ids, ["feat-c"])

    def test_missing_file_returns_empty(self):
        with _TempProject() as proj:
            self.assertEqual(_in_progress_feature_ids(proj / "feature_list.json"), [])


class TestMarkInFlightBlocked(unittest.TestCase):
    """The thin wrapper around feature_list_io.mark_blocked."""

    def test_marks_feature_blocked(self):
        with _TempProject() as proj:
            _seed_feature_list(proj, feature_ids=["feat-047"], status="in_progress")
            ok = mark_in_flight_blocked(
                proj / "feature_list.json", "feat-047", "test reason",
            )
            self.assertTrue(ok)
            features = _read_features(proj / "feature_list.json")
            self.assertEqual(features["feat-047"]["status"], "blocked")
            self.assertEqual(features["feat-047"]["blocked_reason"], "test reason")

    def test_unknown_feature_returns_false(self):
        with _TempProject() as proj:
            _seed_feature_list(proj, feature_ids=["feat-047"], status="in_progress")
            ok = mark_in_flight_blocked(
                proj / "feature_list.json", "feat-does-not-exist", "no such feature",
            )
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
