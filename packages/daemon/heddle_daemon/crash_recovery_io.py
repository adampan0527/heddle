# SPDX-License-Identifier: Apache-2.0
"""On-disk feature_list.json transitions used by crash recovery — feat-047.

Wraps :func:`heddle_common.feature_list_io.mark_blocked` with the
fail-soft contract the daemon's respawn path needs: ``SystemExit``
and arbitrary exceptions are swallowed so a refused transition
never brings the daemon down.

Also exposes the "list in-progress feature ids" helper that the
orchestrator uses to enumerate the recovery candidates. Kept in a
dedicated module so :mod:`heddle_daemon.crash_recovery` stays
under the 200-line file size hard rule.
"""

from __future__ import annotations

from heddle_common import feature_list_io as _fl_io

from heddle_daemon.crash_recovery_types import BlockedReason, ResumeReport

__all__ = [
    "_in_progress_feature_ids",
    "_mark_blocked_best_effort",
    "mark_in_flight_blocked",
]


def _in_progress_feature_ids(feature_list_path: "Path | str") -> list[str]:
    """Return the ids of every feature with ``status == "in_progress"``.

    Reads via :func:`heddle_common.feature_list_io.load` (the only
    sanctioned reader). If the file is missing / unparseable, the
    function returns ``[]`` — the empty list is the right answer
    because there is no evidence the daemon should resume anything.
    """
    try:
        data = _fl_io.load(feature_list_path)
    except (SystemExit, Exception):
        return []
    out: list[str] = []
    for feat in data.get("features", []):
        if not isinstance(feat, dict):
            continue
        if feat.get("status") == "in_progress":
            fid = feat.get("id")
            if isinstance(fid, str) and fid:
                out.append(fid)
    return out


def _mark_blocked_best_effort(
    feature_list_path: "Path | str",
    feature_id: str,
    reason: str,
    report: ResumeReport,
) -> None:
    """Call :func:`mark_in_flight_blocked` and append to ``report`` on success."""
    if mark_in_flight_blocked(feature_list_path, feature_id, reason):
        report.blocked.append(BlockedReason(feature_id, reason))


def mark_in_flight_blocked(
    feature_list_path: "Path | str",
    feature_id: str,
    reason: str,
) -> bool:
    """Mark a single feature blocked; returns ``True`` on success.

    Thin wrapper over :func:`heddle_common.feature_list_io.mark_blocked`
    that swallows the ``SystemExit`` that ``fail()`` raises on a
    refusal (unknown feature id, schema mismatch, etc.). The crash
    recovery path is best-effort by design — a refused transition
    must not bring the daemon down.
    """
    try:
        _fl_io.mark_blocked(
            feature_list_path, feature_id, reason=reason  # type: ignore[arg-type]
        )
        return True
    except (SystemExit, Exception):
        return False
