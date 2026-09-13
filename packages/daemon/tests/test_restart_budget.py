# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_daemon.restart_budget — feat-024 (D-051).

Covers:
  * Pure ``should_block`` decision function (table-driven)
  * ``RestartBudgetCounter`` deque mechanics + sliding window
  * ``RestartBudgetConfig`` env-var resolver + eager validation
  * ``RestartBudgetExceededError`` structured cause field
  * Explicit spec tests from the plan:
      - 3 restarts at t=0, t=300, t=600 (all in 10min) -> blocked
      - 2 at t=0, t=300 + 1 at t=1200 (11min later) -> NOT blocked
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from heddle_daemon.restart_budget import (
    DEFAULT_MAX_RESTARTS,
    DEFAULT_WINDOW_MINUTES,
    ENV_MAX_RESTARTS,
    ENV_WINDOW_MINUTES,
    RestartBudgetConfig,
    RestartBudgetCounter,
    RestartBudgetExceededError,
    should_block,
)

# ---------- helpers ----------


def _at(base: datetime, seconds: float) -> datetime:
    """Return ``base + timedelta(seconds=seconds)``."""
    return base + timedelta(seconds=seconds)


# A frozen "now" so window tests are deterministic. UTC-aware.
NOW: datetime = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


# ---------- should_block ----------


class TestShouldBlockPure(unittest.TestCase):
    """Pure decision function: ``len(restarts_in_window) >= max_restarts``."""

    def test_three_restarts_in_10_min_blocks(self):
        """Spec test 1: 3 at t=0, t=300, t=600 -> blocked."""
        times = [
            NOW,
            _at(NOW, 300),
            _at(NOW, 600),
        ]
        self.assertTrue(should_block(times, now=NOW, window_minutes=10, max_restarts=3))

    def test_two_restarts_in_10_min_then_one_after_11_min_does_not_block(self):
        """Spec test 2: 2 at t=0, t=300 + 1 at t=1200 -> NOT blocked.

        ``now`` is the moment of the third restart (t=1200). The 10-min
        window then covers [t=600, t=1200]; only the third restart
        (at t=1200) is inside the window, so the feature is NOT blocked.
        """
        times = [
            NOW,
            _at(NOW, 300),
            _at(NOW, 1200),  # 20 min after NOW; the "now" reference
        ]
        # Query as-of t=1200: the 10-min window back from there
        # excludes t=0 and t=300. Only the third restart survives,
        # so 1 < 3 and we are NOT blocked.
        self.assertFalse(
            should_block(times, now=_at(NOW, 1200), window_minutes=10, max_restarts=3)
        )

    def test_two_restarts_in_window_does_not_block(self):
        times = [NOW, _at(NOW, 60)]
        self.assertFalse(should_block(times, now=NOW, window_minutes=10, max_restarts=3))

    def test_zero_restarts_never_blocks(self):
        self.assertFalse(should_block([], now=NOW, window_minutes=10, max_restarts=3))

    def test_exactly_max_restarts_in_window_blocks(self):
        times = [NOW, _at(NOW, 60), _at(NOW, 120)]
        self.assertTrue(should_block(times, now=NOW, window_minutes=10, max_restarts=3))

    def test_restart_at_exact_window_edge_is_included(self):
        """Left edge of the window is closed: ``t == now - window`` counts."""
        times = [_at(NOW, -600)]  # exactly 10 min before NOW
        self.assertTrue(
            should_block(times, now=NOW, window_minutes=10, max_restarts=1)
        )

    def test_restart_just_outside_window_is_excluded(self):
        """Anything strictly older than ``now - window`` is excluded."""
        times = [_at(NOW, -601)]  # 10 min + 1 sec before NOW
        self.assertFalse(
            should_block(times, now=NOW, window_minutes=10, max_restarts=1)
        )

    def test_naive_datetime_is_treated_as_utc(self):
        """A naive ``datetime`` input is coerced to UTC for the comparison."""
        naive_now = NOW.replace(tzinfo=None)
        naive_t = NOW.replace(tzinfo=None)  # exactly equal to naive_now
        self.assertTrue(
            should_block([naive_t], now=naive_now, window_minutes=10, max_restarts=1)
        )

    def test_default_window_and_max_restarts_are_10_minutes_and_3(self):
        """Sanity check: the documented defaults match the constants."""
        self.assertEqual(DEFAULT_WINDOW_MINUTES, 10)
        self.assertEqual(DEFAULT_MAX_RESTARTS, 3)

    def test_default_arguments_use_documented_values(self):
        """Spec test 1 with the defaults: 3 in 10min blocks."""
        times = [NOW, _at(NOW, 300), _at(NOW, 600)]
        self.assertTrue(should_block(times, now=NOW))

    def test_window_minutes_zero_raises(self):
        with self.assertRaises(ValueError):
            should_block([], now=NOW, window_minutes=0)

    def test_max_restarts_zero_raises(self):
        with self.assertRaises(ValueError):
            should_block([], now=NOW, max_restarts=0)

    def test_now_defaults_to_utc_now_when_omitted(self):
        """The default ``now`` is a UTC-aware ``datetime.now()``."""
        result = should_block([])
        self.assertFalse(result)
        # We can't pin the value, but we can verify no exception.


# ---------- RestartBudgetCounter ----------


class TestRestartBudgetCounter(unittest.TestCase):
    """Sliding-window counter deque mechanics."""

    def test_empty_counter_is_not_exhausted(self):
        c = RestartBudgetCounter(feature_id="feat-024")
        self.assertFalse(c.is_exhausted(NOW))
        self.assertEqual(c.recent_restarts(NOW), [])

    def test_record_then_recent(self):
        c = RestartBudgetCounter(feature_id="feat-024")
        c.record_restart(NOW)
        c.record_restart(_at(NOW, 60))
        self.assertEqual(len(c.recent_restarts(NOW)), 2)

    def test_three_records_in_10_min_exhausts(self):
        """Spec test 1 via the counter."""
        c = RestartBudgetCounter(feature_id="feat-024")
        c.record_restart(NOW)
        c.record_restart(_at(NOW, 300))
        c.record_restart(_at(NOW, 600))
        self.assertTrue(c.is_exhausted(NOW))

    def test_two_records_then_old_one_after_11_min_does_not_exhaust(self):
        """Spec test 2 via the counter.

        We simulate the sliding window by recording an older
        timestamp, then advancing "now" past the window. The
        counter prunes the old entry on the next ``record_restart``
        or ``is_exhausted`` call.
        """
        c = RestartBudgetCounter(feature_id="feat-024")
        c.record_restart(NOW)
        c.record_restart(_at(NOW, 300))
        # Move "now" to t=1200 and record the third restart there.
        new_now = _at(NOW, 1200)
        c.record_restart(new_now)
        # The first two timestamps are now > 10 minutes old; they
        # should have been pruned by ``record_restart``'s left-edge
        # sweep. The deque should hold exactly one entry.
        self.assertEqual(len(c.recent_restarts(new_now)), 1)
        self.assertFalse(c.is_exhausted(new_now))

    def test_record_prunes_old_entries(self):
        """Recording with a fresh ``now`` sweeps older-than-window entries."""
        c = RestartBudgetCounter(feature_id="feat-024")
        c.record_restart(NOW)
        c.record_restart(_at(NOW, 60))
        c.record_restart(_at(NOW, 120))
        # Advance well past the window and record again.
        future = _at(NOW, 7200)  # 2 hours later
        c.record_restart(future)
        self.assertEqual(len(c.recent_restarts(future)), 1)

    def test_is_exhausted_prunes_old_entries(self):
        c = RestartBudgetCounter(feature_id="feat-024")
        c.record_restart(NOW)
        c.record_restart(_at(NOW, 60))
        c.record_restart(_at(NOW, 120))
        # Jump "now" 2h forward; is_exhausted prunes + returns False.
        future = _at(NOW, 7200)
        self.assertFalse(c.is_exhausted(future))

    def test_clear_empties_the_deque(self):
        c = RestartBudgetCounter(feature_id="feat-024")
        c.record_restart(NOW)
        c.record_restart(_at(NOW, 60))
        c.clear()
        self.assertEqual(c.recent_restarts(NOW), [])
        self.assertFalse(c.is_exhausted(NOW))

    def test_construct_rejects_empty_feature_id(self):
        with self.assertRaises(ValueError):
            RestartBudgetCounter(feature_id="")

    def test_construct_rejects_zero_window(self):
        with self.assertRaises(ValueError):
            RestartBudgetCounter(feature_id="feat-024", window_minutes=0)

    def test_construct_rejects_zero_max_restarts(self):
        with self.assertRaises(ValueError):
            RestartBudgetCounter(feature_id="feat-024", max_restarts=0)

    def test_recent_restarts_returns_independent_copy(self):
        """Mutating the returned list must not affect the counter."""
        c = RestartBudgetCounter(feature_id="feat-024")
        c.record_restart(NOW)
        snapshot = c.recent_restarts(NOW)
        snapshot.clear()
        self.assertEqual(len(c.recent_restarts(NOW)), 1)


# ---------- RestartBudgetConfig ----------


class TestRestartBudgetConfig(unittest.TestCase):
    """Eager validation + env-var resolution."""

    def test_defaults_are_documented(self):
        cfg = RestartBudgetConfig()
        self.assertEqual(cfg.window_minutes, DEFAULT_WINDOW_MINUTES)
        self.assertEqual(cfg.max_restarts, DEFAULT_MAX_RESTARTS)

    def test_rejects_non_positive_window(self):
        with self.assertRaises(ValueError):
            RestartBudgetConfig(window_minutes=0)
        with self.assertRaises(ValueError):
            RestartBudgetConfig(window_minutes=-1)

    def test_rejects_non_positive_max_restarts(self):
        with self.assertRaises(ValueError):
            RestartBudgetConfig(max_restarts=0)

    def test_rejects_non_int_window(self):
        with self.assertRaises(ValueError):
            RestartBudgetConfig(window_minutes="10")  # type: ignore[arg-type]

    def test_rejects_non_int_max_restarts(self):
        with self.assertRaises(ValueError):
            RestartBudgetConfig(max_restarts="3")  # type: ignore[arg-type]

    def test_rejects_bool_int_window(self):
        # ``bool`` is a subclass of ``int``; reject explicitly so a
        # caller passing ``True`` doesn't get ``window_minutes=1``
        # silently.
        with self.assertRaises(ValueError):
            RestartBudgetConfig(window_minutes=True)  # type: ignore[arg-type]

    def test_from_env_defaults_when_unset(self):
        cfg = RestartBudgetConfig.from_env({})
        self.assertEqual(cfg.window_minutes, DEFAULT_WINDOW_MINUTES)
        self.assertEqual(cfg.max_restarts, DEFAULT_MAX_RESTARTS)

    def test_from_env_reads_overrides(self):
        env = {ENV_WINDOW_MINUTES: "5", ENV_MAX_RESTARTS: "7"}
        cfg = RestartBudgetConfig.from_env(env)
        self.assertEqual(cfg.window_minutes, 5)
        self.assertEqual(cfg.max_restarts, 7)

    def test_from_env_rejects_non_integer(self):
        with self.assertRaises(ValueError):
            RestartBudgetConfig.from_env({ENV_WINDOW_MINUTES: "five"})

    def test_from_env_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            RestartBudgetConfig.from_env({ENV_MAX_RESTARTS: "0"})

    def test_env_var_names_match_documented(self):
        """Single source of truth: env var names must match the plan."""
        self.assertEqual(ENV_WINDOW_MINUTES, "HEDDLE_RESTART_WINDOW_MINUTES")
        self.assertEqual(ENV_MAX_RESTARTS, "HEDDLE_RESTART_MAX_COUNT")


# ---------- RestartBudgetExceededError ----------


class TestRestartBudgetExceededError(unittest.TestCase):
    """Structured failure carries ``cause='restart_budget'``."""

    def test_cause_is_restart_budget(self):
        err = RestartBudgetExceededError(window_count=3, window_minutes=10)
        self.assertEqual(err.cause, "restart_budget")

    def test_carries_window_count_and_window_minutes(self):
        err = RestartBudgetExceededError(window_count=4, window_minutes=5)
        self.assertEqual(err.window_count, 4)
        self.assertEqual(err.window_minutes, 5)

    def test_message_mentions_window_count_and_minutes(self):
        err = RestartBudgetExceededError(window_count=3, window_minutes=10)
        self.assertIn("3", str(err))
        self.assertIn("10", str(err))


if __name__ == "__main__":
    unittest.main()
