#!/usr/bin/env python3
"""Mandatory CLI interface to feature_list.json."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools._feature_io import FEATURE_LIST_PATH, STATUSES, VALID_CATEGORIES, VALID_PRIORITIES, fail, load_features, status_of  # noqa: E402
from tools._feature_state import REGRESS_REASON_MIN_CHARS, cmd_add, cmd_mark_blocked, cmd_mark_deferred, cmd_mark_in_progress, cmd_mark_passing, cmd_mark_regressed, cmd_next_feature, cmd_remove, cmd_update_metadata  # noqa: E402
from tools._file_lock import file_lock  # noqa: E402

ADD_ALLOWED_STATUSES = tuple(s for s in STATUSES if s != "passing")


def _with_lock(mutating_fn: Callable[[argparse.Namespace], None]) -> Callable[[argparse.Namespace], None]:
    def locked(args: argparse.Namespace) -> None:
        try:
            with file_lock(FEATURE_LIST_PATH):
                mutating_fn(args)
        except TimeoutError:
            fail("another feature_list.json mutation in progress; try again later")
    return locked


def cmd_list(_: argparse.Namespace) -> None:
    data = load_features()
    if not data["features"]:
        print("(no features yet)")
        return
    for f in data["features"]:
        status = status_of(f) or "?"
        marker = "x" if status == "passing" else " "
        print(
            f"[{marker}] {f.get('id', '???'):<12} "
            f"{f.get('category', '???'):<16} "
            f"status={status:<12} pri={f.get('priority', '-'):<6} "
            f"{f.get('description', '')}"
        )



def get_failing_features(data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return features whose status is not 'passing'.

    If `data` is None, loads it from disk. Mirrors the predicate used
    by `recompute_metadata` so the count here always matches
    `metadata.failing`.
    """
    if data is None:
        data = load_features()
    non_passing = ("pending", "in_progress", "blocked", "deferred")
    return [f for f in data["features"] if status_of(f) in non_passing]



def cmd_list_failing(_: argparse.Namespace) -> None:
    failing = get_failing_features()
    if not failing:
        print("(no failing features)")
        return
    for f in failing:
        status = status_of(f) or "?"
        tag = " (deferred)" if status == "deferred" else ""
        print(
            f"{f.get('id', '???'):<12} "
            f"{f.get('category', '???'):<16} "
            f"status={status:<12} pri={f.get('priority', '-'):<6} "
            f"{f.get('description', '')}{tag}"
        )



def cmd_status(_: argparse.Namespace) -> None:
    data = load_features()
    meta = data.get("metadata", {})
    # Recompute to ensure freshness, but do not write back — this command
    # is read-only from the caller's perspective.
    counts = {s: 0 for s in STATUSES}
    for f in data["features"]:
        s = status_of(f)
        if s in counts:
            counts[s] += 1
    failing = counts["pending"] + counts["in_progress"] + counts["blocked"] + counts["deferred"]
    snapshot = {
        "total_features": len(data["features"]),
        "passing": counts["passing"],
        "failing": failing,
        "in_progress": counts["in_progress"],
        "blocked": counts["blocked"],
        "deferred": counts["deferred"],
        "last_updated": meta.get("last_updated", "?"),
    }
    # Surface broken metadata so the agent (or human) notices before
    # downstream tools (e.g. handoff_check) silently compare against
    # a stale block. The `?` in stdout stays as the "unknown" sentinel
    # for parsers; the warning is the user-facing signal.
    if not meta or not isinstance(meta, dict) or not meta.get("last_updated"):
        print(
            "warning: feature_list.json has no `metadata` block "
            "(or `last_updated` is unset); run "
            "`python HARNESS/tools/feature_list.py update-metadata` to repair",
            file=sys.stderr,
        )
    for key, value in snapshot.items():
        print(f"{key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="HARNESS/tools/feature_list.py",
        description="Mandatory interface to feature_list.json. "
                    "Direct edits are forbidden by CODE_STYLE.md.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Print all features.")
    p_list.set_defaults(func=cmd_list)

    p_failing = sub.add_parser("list-failing",
                               help="Print only features that are not passing.")
    p_failing.set_defaults(func=cmd_list_failing)

    p_status = sub.add_parser("status",
                              help="Print a snapshot of metadata counters.")
    p_status.set_defaults(func=cmd_status)

    p_pass = sub.add_parser("mark-passing",
                            help="Mark a feature status=passing "
                                 "(only allowed from in_progress).")
    p_pass.add_argument("feature_id")
    p_pass.set_defaults(func=_with_lock(cmd_mark_passing))

    p_in_progress = sub.add_parser("mark-in-progress",
                                   help="Mark a feature status=in_progress.")
    p_in_progress.add_argument("feature_id")
    p_in_progress.set_defaults(func=_with_lock(cmd_mark_in_progress))

    p_blocked = sub.add_parser("mark-blocked",
                               help="Mark a feature status=blocked.")
    p_blocked.add_argument("feature_id")
    p_blocked.add_argument("--reason", required=True, help="Why blocked (>=5 chars).")
    p_blocked.set_defaults(func=_with_lock(cmd_mark_blocked))

    p_deferred = sub.add_parser("mark-deferred",
                                help="Mark a feature status=deferred.")
    p_deferred.add_argument("feature_id")
    p_deferred.add_argument("--until", default=None,
                            help="Optional target date or condition.")
    p_deferred.set_defaults(func=_with_lock(cmd_mark_deferred))

    p_regress = sub.add_parser("mark-regressed",
                               help="Mark a previously-passing feature "
                                    "as regressed (status=in_progress).")
    p_regress.add_argument("feature_id")
    p_regress.add_argument("--reason", required=True,
                           help=f"Why regressed (>={REGRESS_REASON_MIN_CHARS} chars).")
    p_regress.set_defaults(func=_with_lock(cmd_mark_regressed))

    p_next = sub.add_parser("next-feature",
                            help="Pick highest-priority pending feature "
                                 "and atomically mark it in_progress.")
    p_next.set_defaults(func=_with_lock(cmd_next_feature))

    p_add = sub.add_parser("add", help="Append a new feature.")
    p_add.add_argument("feature_id")
    p_add.add_argument("category", choices=sorted(VALID_CATEGORIES))
    p_add.add_argument("description")
    p_add.add_argument("--priority", choices=list(VALID_PRIORITIES),
                       help="Default: medium")
    p_add.add_argument("--status", choices=list(ADD_ALLOWED_STATUSES), default="pending",
                       help="Initial status. Default: pending. "
                            "Cannot be 'passing' — use mark-passing instead.")
    p_add.add_argument("--step", action="append", default=[], metavar="STEP",
                       help="Add a single step. Repeatable. "
                            "Mutually exclusive with --steps-file.")
    p_add.add_argument("--steps-file", type=Path, default=None, metavar="PATH",
                       help="Read steps from a UTF-8 text file, one per line. "
                            "Mutually exclusive with --step.")
    p_add.add_argument("--depends-on", default="", metavar="DEPS",
                       help="Comma-separated feature_ids this feature requires "
                            "to be passing before it can be marked passing. "
                            "Every id must already exist in feature_list.json; "
                            "self-dependency is rejected.")
    p_add.set_defaults(func=_with_lock(cmd_add))

    p_meta = sub.add_parser("update-metadata",
                            help="Recompute metadata (use after manual edits).")
    p_meta.set_defaults(func=_with_lock(cmd_update_metadata))

    p_remove = sub.add_parser("remove",
                              help="Remove a feature from feature_list.json. "
                                   "Refuses to remove passing features or "
                                   "features referenced by others' depends_on "
                                   "unless --force is given.")
    p_remove.add_argument("feature_id")
    p_remove.add_argument("--force", action="store_true",
                          help="Override passing-status and depends_on guards.")
    p_remove.set_defaults(func=_with_lock(cmd_remove))

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        fail("no command given. Try: list / status / next-feature / "
             "mark-passing / mark-in-progress / mark-blocked / "
             "mark-deferred / mark-regressed / add / remove / "
             "update-metadata")
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())