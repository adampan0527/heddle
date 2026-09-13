# SPDX-License-Identifier: Apache-2.0
"""Public types and constants for crash recovery — feat-047.

Kept separate from :mod:`heddle_daemon.crash_recovery` so the
orchestration logic file stays under the 200-line file size hard
rule (CODE_STYLE.md Part 1). Re-exports are surfaced via
``crash_recovery.__all__`` so callers continue to import from one
place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

# Stable reason strings. ``Daemon.start()`` matches on these via
# ``startswith`` for log event classification (don't grep for ad-hoc
# wording in tests — use the constants). Both reasons are also the
# exact substrings the existing ``_on_respawn`` hook emits, so the
# status-quo log events and the new ``restart_recovery_completed``
# summary event stay consistent.
BLOCKED_REASON_CHECKPOINT_LOSS: Final[str] = (
    "checkpoint loss detected on daemon respawn"
)
BLOCKED_REASON_RESTART_BUDGET: Final[str] = "restart budget exhausted"


@dataclass(frozen=True)
class BlockedReason:
    """A single (feature_id, reason) entry from a :class:`ResumeReport`."""

    feature_id: str
    reason: str


@dataclass
class ResumeReport:
    """The output of :func:`heddle_daemon.crash_recovery.resume_in_flight_features`.

    Two parallel lists keep the happy / sad paths disjoint. See
    the crash_recovery module docstring for the meaning of each.
    """

    resumed: list[str] = field(default_factory=list)
    blocked: list[BlockedReason] = field(default_factory=list)

    @property
    def blocked_pairs(self) -> list[tuple[str, str]]:
        """``[(feature_id, reason), ...]`` — handy for the public API."""
        return [(b.feature_id, b.reason) for b in self.blocked]


class RestartBudgetLike(Protocol):
    """Minimal interface we depend on from ``RestartBudgetCounter``."""

    def record_restart(self, now: datetime) -> None: ...
    def is_exhausted(self, now: datetime) -> bool: ...


class RestartCounterGetter(Protocol):
    """Returns the restart counter for a feature_id.

    The daemon owns a ``dict[str, RestartBudgetCounter]`` keyed by
    feature_id; this getter either fetches the existing counter or
    constructs a fresh one with the configured budget window. The
    constructor is deferred to the getter so test code can inject a
    fixed-budget counter without going through the env-var resolver.
    """

    def __call__(self, feature_id: str) -> RestartBudgetLike: ...


__all__ = [
    "BLOCKED_REASON_CHECKPOINT_LOSS",
    "BLOCKED_REASON_RESTART_BUDGET",
    "BlockedReason",
    "ResumeReport",
    "RestartBudgetLike",
    "RestartCounterGetter",
]
