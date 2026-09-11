#!/usr/bin/env python3
"""_constants.py — shared constants for the long-running agent harness tools.

Single source of truth for cross-tool values that were previously
duplicated inline (and drifted). Stdlib only.

This module owns:
- The 5-state feature status set (`STATUSES`).
- The category whitelist (`VALID_CATEGORIES`).
- The priority levels (`VALID_PRIORITIES`).
- The canonical SESSION heading regex (`SESSION_HEADING_RE`).

Feature-list constants (VALID_CATEGORIES, VALID_PRIORITIES, STATUSES)
originated in `feature_list.py` — that file remains the home for the
JSON I/O helpers (load/save/recompute), and continues to re-export the
values from here for backward compatibility with any direct callers.

Tools that read the canonical SESSION heading shape (session_end,
progress_rotate, next_session_number, handoff_check) import
SESSION_HEADING_RE from this module to stay in sync.
"""

from __future__ import annotations

import re

# ---------- Feature status (5-state machine) ----------

# The canonical status set for `feature_list.json` features. See
# `tools/feature_list.py` and HARNESS.md § "Status values".
STATUSES = ("pending", "in_progress", "blocked", "deferred", "passing")

# Categories for `feature_list.json` features. See HARNESS.md §
# "Category values".
VALID_CATEGORIES = frozenset({
    "functional",
    "ui",
    "error-handling",
    "accessibility",
    "performance",
})

# Priority levels for `feature_list.json` features. See HARNESS.md §
# "Priority values".
VALID_PRIORITIES = ("high", "medium", "low")

# ---------- SESSION block status ----------

SESSION_STATUSES: tuple[str, ...] = (
    "implemented_and_tested",
    "implemented_not_tested",
    "in_progress_blocked",
    "deferred",
    "no_op",
)
SESSION_STATUS_LABELS: dict[str, str] = {
    "implemented_and_tested": "Implemented and tested",
    "implemented_not_tested": "Implemented, not yet tested",
    "in_progress_blocked": "In progress, blocked",
    "deferred": "Deferred",
    "no_op": "No-op",
}

# ---------- SESSION block heading regex ----------

# Canonical SESSION heading produced by `tools/session_end.py`:
#   "### SESSION 3 — 2026-07-23"
# Matches in MULTILINE mode (each line independently).
#
# The dash between the session number and the ISO date is matched
# permissively as `[—–-]` (em-dash U+2014, en-dash U+2013,
# ASCII hyphen-minus U+002D). Editor auto-normalization or CI
# formatters may swap the canonical em-dash for an en-dash or plain
# hyphen; we accept all three on the READ side so session numbering
# tools (next_session_number.py, progress_rotate.py, handoff_check.py)
# keep working. The WRITE side still emits the canonical U+2014 —
# see `tools/session_end._build_session_block` and the
# `docs/templates/progress_session_block.md` template.
#
# The Initializer's bare "SESSION 0 - Initializer Agent" line
# intentionally does NOT match this regex (no `###` prefix); that is
# by design — SESSION 0 is the Initializer's seed and must never be
# rotated or counted as a Coding Agent session.
SESSION_HEADING_RE = re.compile(
    r"^###\s+SESSION\s+(\d+)\s+[—–-]\s+(\d{4}-\d{2}-\d{2})\s+$",
    re.MULTILINE,
)