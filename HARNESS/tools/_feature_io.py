#!/usr/bin/env python3
"""Shim — re-exports feature_list_io from heddle_common.

Per TECH.md T-014 and feat-008. The single source of truth lives in
heddle_common.feature_list_io. This shim exists so existing imports
(`from tools._feature_io import ...`) keep working across the HARNESS
codebase without rewriting every callsite.

The module also preserves the legacy module-level constants for
backward compatibility:

    FEATURE_LIST_PATH = HARNESS/feature_list.json

Callers in the HARNESS codebase that explicitly reference the
HARNESS-side path can keep using `FEATURE_LIST_PATH`; callers that want
a per-project path pass an explicit `path` argument to the library
functions.
"""

from __future__ import annotations

# Re-export the public surface of the library.
from heddle_common.feature_list_io import (  # noqa: F401
    BLOCK_REASON_MIN_CHARS,
    DEFAULT_PATH,
    INDENT,
    PLACEHOLDER_STEP_PATTERNS,
    PLACEHOLDER_STEP_SUBSTRINGS,
    PRIORITY_RANK,
    REGRESS_REASON_MIN_CHARS,
    STATUSES,
    STEP_MAX_CHARS,
    STEPS_MAX,
    VALID_ATTEMPT_OUTCOMES,
    VALID_CATEGORIES,
    VALID_PRIORITIES,
    ID_REGEX,
    add,
    fail,
    load,
    mark_blocked,
    mark_deferred,
    mark_in_progress,
    mark_passing,
    mark_regressed,
    next_feature,
    recompute_metadata,
    remove,
    save,
    status_of,
    update_metadata,
)

# Legacy aliases — the HARNESS codebase historically named these
# `load_features`, `save_features`, etc. The HARNESS CLI module
# (tools/feature_list.py) still uses the old names in some places;
# keep them alive here as one-line shims.
load_features = load  # noqa: F401
save_features = save  # noqa: F401

# Legacy path constant: HARNESS/feature_list.json. Equivalent to
# `heddle_common.feature_list_io.DEFAULT_PATH` but kept under the
# HARNESS-side name for any code that imports it explicitly.
from pathlib import Path  # noqa: E402
FEATURE_LIST_PATH: Path = DEFAULT_PATH