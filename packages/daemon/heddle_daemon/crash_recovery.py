# SPDX-License-Identifier: Apache-2.0
"""Crash recovery via checkpoint resume — feat-047 (D-051).

The actual thread resumption is handled transparently by LangGraph's
``AsyncSqliteSaver``: as soon as the runtime is compiled against the
live saver, ``graph.ainvoke`` with the persisted thread_id picks up
the latest state.

This module is the orchestration glue between three moving parts:
``is_checkpoint_db_healthy`` (feat-024), the live ``AsyncSqliteSaver``
queried via :mod:`heddle_daemon.thread_presence`, and the per-feature
``RestartBudgetCounter`` (feat-024). Per-feature outcomes are:
resume, blocked (budget exhausted), blocked (checkpoint loss).
"""

from __future__ import annotations

from datetime import UTC, datetime

# Re-exported for callers that want to avoid importing two modules.
from heddle_daemon.checkpointing import (  # noqa: F401
    ProjectCheckpointStore,
    is_checkpoint_db_healthy,
)
from heddle_daemon.crash_recovery_io import (
    _in_progress_feature_ids,
    _mark_blocked_best_effort,
    mark_in_flight_blocked,
)
from heddle_daemon.crash_recovery_types import (  # noqa: F401
    BLOCKED_REASON_CHECKPOINT_LOSS,
    BLOCKED_REASON_RESTART_BUDGET,
    BlockedReason,
    ResumeReport,
    RestartBudgetLike,
    RestartCounterGetter,
)
from heddle_daemon.thread_presence import (
    ThreadPresence,
    alist_thread_ids,
    presence_from_set,
)

__all__ = [
    "BLOCKED_REASON_CHECKPOINT_LOSS",
    "BLOCKED_REASON_RESTART_BUDGET",
    "BlockedReason",
    "ProjectCheckpointStore",
    "ResumeReport",
    "RestartBudgetLike",
    "RestartCounterGetter",
    "ThreadPresence",
    "alist_thread_ids",
    "is_checkpoint_db_healthy",
    "mark_in_flight_blocked",
    "presence_from_set",
    "resume_in_flight_features",
]


# ---------- decision function ----------


async def resume_in_flight_features(
    *,
    project_path: "Path | str",
    feature_list_path: "Path | str",
    checkpoint_store: ProjectCheckpointStore | None,
    restart_counter_getter: RestartCounterGetter,
    thread_presence: ThreadPresence | None = None,
    known_thread_ids: set[str] | None = None,
    now: datetime | None = None,
) -> ResumeReport:
    """Resume in-flight features from the project's checkpoint DB.

    See the module docstring for the full per-feature decision tree.

    Args:
        project_path: the project's working directory. Used by
            :func:`is_checkpoint_db_healthy` to check the DB.
        feature_list_path: the ``feature_list.json`` on disk.
        checkpoint_store: live store from the daemon. ``None`` together
            with no ``thread_presence`` / ``known_thread_ids`` is
            treated as "checkpoint loss" for every in-flight feature.
        restart_counter_getter: returns the
            ``RestartBudgetCounter`` for a feature_id.
        thread_presence: optional sync callable for thread lookup.
        known_thread_ids: optional pre-materialised thread-id set.
        now: clock for the restart-counter ``is_exhausted`` call.

    Returns:
        :class:`ResumeReport` — the two parallel lists are always
        populated.
    """
    if now is None:
        now = datetime.now(UTC)

    # Fast-path: when there are no in-flight features, skip the
    # entire orchestrator — including the async ``alist`` query
    # against the SQLite checkpoint DB. The DB might not even exist
    # yet (first-run / fresh project), so this short-circuit avoids
    # unnecessary file IO and aiosqlite worker-thread churn.
    in_progress = _in_progress_feature_ids(feature_list_path)
    if not in_progress:
        return ResumeReport()

    resolved_presence = await _resolve_thread_presence(
        checkpoint_store=checkpoint_store,
        thread_presence=thread_presence,
        known_thread_ids=known_thread_ids,
    )

    # Checkpoint DB health is the gateway: a corrupt / missing DB
    # means we cannot tell which threads are resumable. Block every
    # in-flight feature with the canonical "checkpoint loss" reason
    # and skip the per-feature budget loop entirely.
    if resolved_presence is None or not is_checkpoint_db_healthy(project_path):
        return _block_all_in_flight(
            feature_list_path, in_progress, BLOCKED_REASON_CHECKPOINT_LOSS,
        )

    return _resume_or_block_per_feature(
        feature_list_path=feature_list_path,
        in_progress_ids=in_progress,
        thread_presence=resolved_presence,
        restart_counter_getter=restart_counter_getter,
        now=now,
    )


# ---------- helpers ----------


async def _resolve_thread_presence(
    *,
    checkpoint_store: ProjectCheckpointStore | None,
    thread_presence: ThreadPresence | None,
    known_thread_ids: set[str] | None,
) -> ThreadPresence | None:
    """Resolve the thread-presence callable. Returns None on fail-closed."""
    if thread_presence is not None:
        return thread_presence
    if known_thread_ids is not None:
        return presence_from_set(known_thread_ids)
    if checkpoint_store is None:
        return None
    ids = await alist_thread_ids(checkpoint_store)
    return presence_from_set(ids)


def _resume_or_block_per_feature(
    *,
    feature_list_path: "Path | str",
    in_progress_ids: list[str],
    thread_presence: ThreadPresence,
    restart_counter_getter: RestartCounterGetter,
    now: datetime,
) -> ResumeReport:
    """Walk the in-flight features and decide per feature (healthy DB path)."""
    report = ResumeReport()
    for fid in in_progress_ids:
        if not thread_presence(fid):
            # Healthy DB but no thread for this feature — the feature
            # was marked in_progress but never produced a checkpoint.
            reason = (
                f"{BLOCKED_REASON_CHECKPOINT_LOSS}: "
                f"no checkpoint thread found for {fid}"
            )
            _mark_blocked_best_effort(feature_list_path, fid, reason, report)
            continue

        counter = restart_counter_getter(fid)
        counter.record_restart(now)
        if counter.is_exhausted(now):
            _mark_blocked_best_effort(
                feature_list_path, fid,
                BLOCKED_REASON_RESTART_BUDGET, report,
            )
            continue

        report.resumed.append(fid)

    return report


def _block_all_in_flight(
    feature_list_path: "Path | str",
    in_progress_ids: list[str],
    reason: str,
) -> ResumeReport:
    """Block every in-flight feature with ``reason`` and return the report."""
    report = ResumeReport()
    for fid in in_progress_ids:
        _mark_blocked_best_effort(feature_list_path, fid, reason, report)
    return report
