#!/usr/bin/env python3
"""Shim — wraps heddle_common.feature_list_io for the HARNESS CLI's argparse layer.

Per TECH.md T-014 and feat-008. The single source of truth lives in
heddle_common.feature_list_io. This shim provides the `cmd_*`
functions the HARNESS CLI (`tools/feature_list.py`) binds to its
argparse subparsers; each one extracts the relevant fields from an
argparse.Namespace and delegates to the library.

The legacy re-exports (`BLOCK_REASON_MIN_CHARS`, `REGRESS_REASON_MIN_CHARS`,
`ID_REGEX`, etc.) are kept so any code that did
`from tools._feature_state import ...` continues to work.
"""

from __future__ import annotations

import argparse

from heddle_common import feature_list_io as _fl
from heddle_common.feature_list_io import (  # noqa: F401
    BLOCK_REASON_MIN_CHARS,
    ID_REGEX,
    PLACEHOLDER_STEP_PATTERNS,
    PLACEHOLDER_STEP_SUBSTRINGS,
    PRIORITY_RANK,
    REGRESS_REASON_MIN_CHARS,
    STEP_MAX_CHARS,
    STEPS_MAX,
    VALID_ATTEMPT_OUTCOMES,
    VALID_CATEGORIES,
    VALID_PRIORITIES,
)


def cmd_mark_passing(args: argparse.Namespace) -> None:
    _fl.mark_passing(None, args.feature_id)


def cmd_mark_in_progress(args: argparse.Namespace) -> None:
    _fl.mark_in_progress(None, args.feature_id)


def cmd_mark_blocked(args: argparse.Namespace) -> None:
    _fl.mark_blocked(None, args.feature_id, reason=args.reason)


def cmd_mark_deferred(args: argparse.Namespace) -> None:
    _fl.mark_deferred(None, args.feature_id, until=args.until)


def cmd_mark_regressed(args: argparse.Namespace) -> None:
    _fl.mark_regressed(None, args.feature_id, reason=args.reason)


def cmd_next_feature(_args: argparse.Namespace) -> None:
    _fl.next_feature(None)


def cmd_add(args: argparse.Namespace) -> None:
    _fl.add(
        None,
        feature_id=args.feature_id,
        category=args.category,
        description=args.description,
        status=args.status,
        priority=args.priority,
        step=args.step or [],
        steps_file=args.steps_file,
        depends_on=args.depends_on or "",
    )


def cmd_set_steps(args: argparse.Namespace) -> None:
    """Replace the ``steps`` array on an existing feature (no-op on fields).

    See :func:`heddle_common.feature_list_io.set_steps` for validation
    rules. Per CODE_STYLE.md "Data integrity via scripts", this is the
    ONLY sanctioned way to mutate an existing feature's ``steps``.
    """
    _fl.set_steps(
        None,
        args.feature_id,
        step=args.step or [],
        steps_file=args.steps_file,
    )


def cmd_update_metadata(_args: argparse.Namespace) -> None:
    _fl.update_metadata(None)


def cmd_remove(args: argparse.Namespace) -> None:
    _fl.remove(None, args.feature_id, force=bool(getattr(args, "force", False)))