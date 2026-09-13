# SPDX-License-Identifier: Apache-2.0
"""Per-feature restart budget — feat-024 (D-051).

When the daemon crashes and the supervisor (feat-027) respawns it,
the per-feature restart counter increments. After 3 restarts within
any 10-minute sliding window, the affected feature transitions to
``blocked`` with a clear reason. The same happens on checkpoint loss
(missing/corrupt ``checkpoints.db``) — every in-flight feature is
blocked with "checkpoint loss".

Public surface:

    should_block                — pure decision function (test target)
    RestartBudgetCounter        — deque-backed sliding window per feature
    RestartBudgetConfig         — env-var resolver + eager validation
    RestartBudgetExceededError  — structured failure (cause="restart_budget")

The decision function is the load-bearing test target: it accepts the
already-restarted ``list[datetime]`` for one feature and returns True
iff the window is exhausted. This module owns ZERO side effects — all
mutation of ``feature_list.json`` is delegated to
``heddle_common.feature_list_io.mark_blocked`` in ``server.py``.

Time handling: ``datetime.now(timezone.utc)`` is the single source of
truth for "now" in production; tests inject a frozen ``now`` so the
10-minute window is asserted deterministically without sleeps.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

# ---------- constants ----------


# Default 10-minute sliding window per D-051: three restarts within any
# 10-minute wall-clock window => blocked. Override via the
# HEDDLE_RESTART_WINDOW_MINUTES env var (callers read through
# ``RestartBudgetConfig.from_env``).
DEFAULT_WINDOW_MINUTES: Final[int] = 10

# Default of 3 restarts within the window before blocking. Override via
# HEDDLE_RESTART_MAX_COUNT.
DEFAULT_MAX_RESTARTS: Final[int] = 3

# Hard cap on the deque length to bound memory if a daemon is somehow
# respawned repeatedly without anyone ever calling ``is_exhausted``
# (which prunes). The cap is large enough that the legitimate code
# path (record then prune-older-than-window) never trips it; it only
# matters as a defense-in-depth against a forgotten counter.
_INTERNAL_DEQUE_CAP: Final[int] = 100


# Env-var names — single source of truth for tests and supervisor.
ENV_WINDOW_MINUTES: Final[str] = "HEDDLE_RESTART_WINDOW_MINUTES"
ENV_MAX_RESTARTS: Final[str] = "HEDDLE_RESTART_MAX_COUNT"


# ---------- pure decision function ----------


def should_block(
    restart_times: list[datetime],
    *,
    now: datetime | None = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
) -> bool:
    """True iff ``restart_times`` contains ``>= max_restarts`` entries
    within the last ``window_minutes`` before ``now``.

    The pure-function spec from the plan:
      * 3 restarts at t=0, t=300, t=600 (all in 10min) -> blocked
      * 2 at t=0, t=300 + 1 at t=1200 (11min later) -> NOT blocked

    Args:
        restart_times: restart timestamps (UTC recommended; naive
            datetimes are treated as UTC for the comparison).
        now: reference "now". Defaults to ``datetime.now(timezone.utc)``.
            Tests inject a frozen ``now`` to make the window deterministic.
        window_minutes: how wide the sliding window is, in minutes.
        max_restarts: how many restarts inside the window trigger a block.

    Returns:
        True iff the count inside ``[now - window_minutes, now]`` is
        greater than or equal to ``max_restarts``.

    Notes:
        * A restart timestamp exactly equal to ``now - window_minutes``
          is included (closed interval on the left edge) — anything
          strictly older than that is excluded.
        * Naive ``datetime`` inputs are coerced to UTC via a defensive
          ``astimezone`` so the comparison is tz-aware on both sides.
    """
    if window_minutes <= 0:
        raise ValueError(f"window_minutes must be > 0; got {window_minutes}")
    if max_restarts <= 0:
        raise ValueError(f"max_restarts must be > 0; got {max_restarts}")
    if now is None:
        now = datetime.now(UTC)
    now_aware = _ensure_aware(now)
    cutoff = now_aware - timedelta(minutes=window_minutes)
    count = 0
    for t in restart_times:
        t_aware = _ensure_aware(t)
        # Closed on the left edge: ``>= cutoff``. Anything strictly
        # older than ``cutoff`` is excluded.
        if t_aware >= cutoff:
            count += 1
    return count >= max_restarts


def _ensure_aware(dt: datetime) -> datetime:
    """Coerce a naive datetime to UTC; leave an aware datetime alone.

    Defense-in-depth against a caller that hands us a naive
    ``datetime.now()`` (which is local-time on most platforms).
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ---------- sliding window counter ----------


@dataclass
class RestartBudgetCounter:
    """Per-feature deque of restart timestamps with sliding-window prune.

    Use as:

        counter = RestartBudgetCounter(feature_id="feat-024")
        counter.record_restart(now)
        ...
        if counter.is_exhausted(now):
            mark_blocked(feature_id, reason=...)

    Methods are pure (no I/O) — they only mutate ``self._times``.
    Concurrency: the daemon calls these from the supervisor hook on
    a single asyncio task, so a plain deque is sufficient (no lock).
    Cross-process callers must serialize externally.
    """

    feature_id: str
    window_minutes: int = DEFAULT_WINDOW_MINUTES
    max_restarts: int = DEFAULT_MAX_RESTARTS
    _times: deque[datetime] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if not isinstance(self.feature_id, str) or not self.feature_id:
            raise ValueError(
                f"feature_id must be a non-empty string; got {self.feature_id!r}"
            )
        if self.window_minutes <= 0:
            raise ValueError(f"window_minutes must be > 0; got {self.window_minutes}")
        if self.max_restarts <= 0:
            raise ValueError(f"max_restarts must be > 0; got {self.max_restarts}")

    def record_restart(self, now: datetime) -> None:
        """Append ``now`` to the deque and prune anything older than the window.

        Older entries are popped first so the deque size never grows
        past ``max_restarts`` in the steady state. The hard cap of
        ``_INTERNAL_DEQUE_CAP`` is a backstop against a forgotten
        counter (e.g. caller forgot to call ``is_exhausted`` between
        respawns); in normal use the cap never trips because we
        prune older-than-window entries on every ``record_restart``.
        """
        now_aware = _ensure_aware(now)
        cutoff = now_aware - timedelta(minutes=self.window_minutes)
        # Prune from the left. The deque's append order matches insert
        # order, so the oldest entry is on the left.
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
        self._times.append(now_aware)
        # Defense-in-depth: cap the deque length.
        while len(self._times) > _INTERNAL_DEQUE_CAP:
            self._times.popleft()

    def is_exhausted(self, now: datetime) -> bool:
        """True iff ``>= max_restarts`` timestamps remain in the window.

        Prunes older entries as a side effect — calling ``is_exhausted``
        first is safe (an empty deque returns False; an in-window
        deque returns True only when the threshold is met).
        """
        return should_block(
            list(self._times),
            now=now,
            window_minutes=self.window_minutes,
            max_restarts=self.max_restarts,
        )

    def recent_restarts(self, now: datetime) -> list[datetime]:
        """Return a snapshot of in-window restart timestamps (copy).

        Prunes older entries as a side effect, then copies the
        remaining deque into a list. The list is a fresh object so the
        caller can mutate it without affecting this counter.
        """
        now_aware = _ensure_aware(now)
        cutoff = now_aware - timedelta(minutes=self.window_minutes)
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
        return list(self._times)

    def clear(self) -> None:
        """Drop every recorded restart timestamp."""
        self._times.clear()


# ---------- config + error ----------


@dataclass(frozen=True)
class RestartBudgetConfig:
    """Env-var-resolved restart budget configuration.

    Eagerly validated: ``__post_init__`` raises ``ValueError`` on
    non-positive integers. ``from_env`` reads the two env vars
    (HEDDLE_RESTART_WINDOW_MINUTES / HEDDLE_RESTART_MAX_COUNT), falls
    back to the defaults on missing values, and rejects non-integer
    or non-positive values loudly. Mirrors the validation contract
    of ``DaemonConfig.__post_init__`` (server.py).
    """

    window_minutes: int = DEFAULT_WINDOW_MINUTES
    max_restarts: int = DEFAULT_MAX_RESTARTS

    def __post_init__(self) -> None:
        if not isinstance(self.window_minutes, int) or isinstance(self.window_minutes, bool):
            raise ValueError(
                f"window_minutes must be an int; got {type(self.window_minutes).__name__}"
            )
        if self.window_minutes <= 0:
            raise ValueError(f"window_minutes must be > 0; got {self.window_minutes}")
        if not isinstance(self.max_restarts, int) or isinstance(self.max_restarts, bool):
            raise ValueError(
                f"max_restarts must be an int; got {type(self.max_restarts).__name__}"
            )
        if self.max_restarts <= 0:
            raise ValueError(f"max_restarts must be > 0; got {self.max_restarts}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RestartBudgetConfig:
        """Read HEDDLE_RESTART_WINDOW_MINUTES / HEDDLE_RESTART_MAX_COUNT.

        Missing keys fall back to the defaults. Non-integer or
        non-positive values raise ``ValueError`` — the caller is the
        supervisor (which is expected to surface a startup error and
        abort) so we prefer fail-fast over silent default.
        """
        src = os.environ if env is None else env

        def _parse(name: str, default: int) -> int:
            raw = src.get(name)
            if raw is None:
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(
                    f"{name}={raw!r} is not an integer; fix the env var"
                ) from exc
            if value <= 0:
                raise ValueError(f"{name}={value} must be > 0")
            return value

        return cls(
            window_minutes=_parse(ENV_WINDOW_MINUTES, DEFAULT_WINDOW_MINUTES),
            max_restarts=_parse(ENV_MAX_RESTARTS, DEFAULT_MAX_RESTARTS),
        )


class RestartBudgetExceededError(Exception):
    """Raised when a feature's restart budget is exhausted (D-051).

    Mirrors :class:`heddle_daemon.agent_runtime.RecursionLimitError`:
    carries a structured ``cause="restart_budget"`` so the daemon /
    WS layer can render a stable classification without parsing the
    message. ``window_count`` and ``window_minutes`` are exposed for
    diagnostic logging.
    """

    def __init__(self, window_count: int, window_minutes: int) -> None:
        super().__init__(
            f"feature restart budget exhausted: {window_count} restarts "
            f"within {window_minutes} minutes"
        )
        self.window_count = window_count
        self.window_minutes = window_minutes
        self.cause = "restart_budget"


# Late import so the module is importable without ``os`` being
# unconditionally pulled in for the pure-function path. ``os`` is in
# the stdlib so the cost is negligible; the comment is a signpost for
# anyone trimming imports.
import os

__all__ = [
    "DEFAULT_MAX_RESTARTS",
    "DEFAULT_WINDOW_MINUTES",
    "ENV_MAX_RESTARTS",
    "ENV_WINDOW_MINUTES",
    "RestartBudgetConfig",
    "RestartBudgetCounter",
    "RestartBudgetExceededError",
    "should_block",
]
