"""Feature state mutation commands.

Holds every cmd_* subcommand that mutates feature_list.json (add /
mark-passing / mark-in-progress / mark-blocked / mark-deferred /
mark-regressed / next-feature / update-metadata). These are imported
into tools/feature_list.py and wrapped by _with_lock before being
attached to the subparser set_defaults(func=...).

The IO helpers (load / save / status_of / recompute_metadata / fail)
and constants (FEATURE_LIST_PATH / STATUSES / VALID_* / INDENT) live
in ._feature_io; we import them so the wiring in feature_list.py
keeps working unchanged.
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from typing import Any

from ._feature_io import (
    FEATURE_LIST_PATH,
    STATUSES,
    VALID_ATTEMPT_OUTCOMES,
    VALID_CATEGORIES,
    VALID_PRIORITIES,
    fail,
    load_features,
    recompute_metadata,
    save_features,
    status_of,
)


# ---------- constants (single source of truth for this module) ----------

# Minimum length for mark-blocked --reason.
BLOCK_REASON_MIN_CHARS = 5

# Minimum length for mark-regressed --reason.
REGRESS_REASON_MIN_CHARS = 10

# next-feature priority ordering: lower rank = higher priority.
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

# cmd_add step limits.
STEPS_MAX = 100
STEP_MAX_CHARS = 500

# Step strings whose *normalized* (stripped, lowercased) form matches
# one of these patterns are rejected as placeholders. The intent: a
# feature whose only step is "TBD" or "fill in later" is unimplementable;
# the Initializer must author real, verifiable steps before declaring
# setup complete. handoff_check.check_9_feature_schema reuses this set
# to gate pre-existing features with the same condition.
#
# Rules:
# - Exact match after normalization: e.g. "TBD", "todo", "n/a", "?"
# - Substring match for "TBD - ..." / "TODO: ..." style prefixes.
# A step that strips to e.g. "Verify the user can log in" is fine.
PLACEHOLDER_STEP_PATTERNS = frozenset({
    "tbd",
    "tbd.",
    "todo",
    "todo.",
    "fill in",
    "fill in later",
    "fill me in",
    "fill me out",
    "placeholder",
    "n/a",
    "na",
    "?",
    "...",
    "[step]",
    "[steps]",
    "[fill-in]",
    "[fill in]",
    "to be filled by initializer",
    "to be filled in later",
})
# Substring tokens — a step that contains any of these (case-insensitive,
# after stripping) is also rejected. Catches "TODO: write tests here" or
# "TBD - placeholder description".
PLACEHOLDER_STEP_SUBSTRINGS = (
    "tbd -",
    "tbd:",
    "todo -",
    "todo:",
    "fill-in:",
    "fill in:",
    "fixme",
)


def _is_placeholder_step(step: str) -> bool:
    """Return True if `step` (already cleaned of CR/LF) is a placeholder.

    A step is a placeholder when its normalized form equals one of
    PLACEHOLDER_STEP_PATTERNS, or when its normalized form contains any
    PLACEHOLDER_STEP_SUBSTRINGS token. The check is fail-loud: a
    placeholder step is treated as a hard validation error, not a
    warning, because handoff_check refuses to start a session when a
    feature is unimplementable.
    """
    normalized = step.strip().lower()
    if not normalized:
        return True
    if normalized in PLACEHOLDER_STEP_PATTERNS:
        return True
    return any(token in normalized for token in PLACEHOLDER_STEP_SUBSTRINGS)

# Slug regex for cmd_add --feature_id.
ID_REGEX = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*")


# ---------- internal helpers ----------

def _ensure_extras(feature):
    """Guarantee the canonical optional fields exist on feature."""
    feature.setdefault("steps", [])
    feature.setdefault("depends_on", [])
    feature.setdefault("attempts", [])


def _clear_status_extras(feature):
    """Strip blocked_reason / deferred_until from feature."""
    feature.pop("blocked_reason", None)
    feature.pop("deferred_until", None)


def _append_attempt(feature, outcome, note):
    """Append an audit-log entry to feature[attempts].

    Validates outcome against VALID_ATTEMPT_OUTCOMES.
    """
    if outcome not in VALID_ATTEMPT_OUTCOMES:
        fail(f"invalid attempt outcome: {outcome!r}; expected one of {VALID_ATTEMPT_OUTCOMES}")
    feature["attempts"].append({
        "session": None,
        "by": "coding-agent",
        "outcome": outcome,
        "note": note,
        "at": date.today().isoformat(),
    })


def _find_feature(data, feature_id):
    """Return the feature dict matching feature_id, or None if absent."""
    for f in data["features"]:
        if f.get("id") == feature_id:
            return f
    return None


def _assert_deps_passing(feature, data):
    """Refuse to mark feature passing while any dep is not passing."""
    deps = feature.get("depends_on") or []
    if not deps:
        return
    by_id = {f.get("id"): f for f in data["features"] if isinstance(f, dict)}
    not_passing = []
    for dep_id in deps:
        dep_feat = by_id.get(dep_id)
        if dep_feat is None:
            continue
        if status_of(dep_feat) != "passing":
            not_passing.append(f"{dep_id} ({status_of(dep_feat) or '?'})")
    if not_passing:
        fail(
            f"cannot mark {feature.get('id', '?')} passing; "
            f"dependencies not passing: {', '.join(not_passing)}"
        )


def _wake_downstream(feature, data):
    """Print a hint about downstream features whose deps just cleared."""
    passed_id = feature.get("id", "?")
    downstream = []
    for f in data["features"]:
        deps = f.get("depends_on") or []
        if passed_id in deps:
            downstream.append(str(f.get("id", "?")))
    if downstream:
        print(
            f"note: downstream features unblocked (deps now passing): "
            f"{', '.join(downstream)}",
            flush=True,
        )


def _resolve_steps(step_args, steps_file_path):
    """Return the merged step list for cmd_add."""
    if step_args and steps_file_path is not None:
        fail("--step and --steps-file are mutually exclusive")
    if steps_file_path is not None:
        try:
            text = steps_file_path.read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read --steps-file {steps_file_path}: {exc}")
        steps = [ln.rstrip(chr(13) + chr(10)) for ln in text.splitlines()]
        return [s for s in steps if s.strip()]
    return list(step_args)


# ---------- command: mark-passing ----------

def cmd_mark_passing(args):
    """Transition feature_id from in_progress to passing."""
    data = load_features()
    feature = _find_feature(data, args.feature_id)
    if feature is None:
        fail(f"feature {args.feature_id!r} not found")
    _ensure_extras(feature)
    current = status_of(feature)
    if current != "in_progress":
        fail(
            f"cannot mark {args.feature_id} passing from status={current!r}; "
            f"expected status=in_progress"
        )
    _assert_deps_passing(feature, data)
    _clear_status_extras(feature)
    _append_attempt(feature, "passing", args.feature_id)
    feature["status"] = "passing"
    _wake_downstream(feature, data)
    recompute_metadata(data)
    save_features(data)


# ---------- command: mark-in-progress ----------

def cmd_mark_in_progress(args):
    """Transition feature_id to in_progress (idempotent if already)."""
    data = load_features()
    feature = _find_feature(data, args.feature_id)
    if feature is None:
        fail(f"feature {args.feature_id!r} not found")
    _ensure_extras(feature)
    current = status_of(feature)
    if current == "in_progress":
        return
    if current not in ("pending", "blocked"):
        fail(
            f"cannot mark {args.feature_id} in_progress from status={current!r}; "
            f"expected one of pending, blocked"
        )
    _clear_status_extras(feature)
    feature["status"] = "in_progress"
    recompute_metadata(data)
    save_features(data)


# ---------- command: mark-blocked ----------

def cmd_mark_blocked(args):
    """Transition feature_id to blocked with a mandatory reason."""
    reason = args.reason or ""
    if len(reason) < BLOCK_REASON_MIN_CHARS:
        fail(
            f"--reason must be at least {BLOCK_REASON_MIN_CHARS} chars "
            f"(got {len(reason)})"
        )
    data = load_features()
    feature = _find_feature(data, args.feature_id)
    if feature is None:
        fail(f"feature {args.feature_id!r} not found")
    _ensure_extras(feature)
    current = status_of(feature)
    if current not in ("pending", "in_progress"):
        fail(
            f"cannot mark {args.feature_id} blocked from status={current!r}; "
            f"expected one of pending, in_progress"
        )
    _clear_status_extras(feature)
    _append_attempt(feature, "blocked", reason)
    feature["status"] = "blocked"
    feature["blocked_reason"] = reason
    recompute_metadata(data)
    save_features(data)


# ---------- command: mark-deferred ----------

def cmd_mark_deferred(args):
    """Transition feature_id to deferred (optionally with --until)."""
    data = load_features()
    feature = _find_feature(data, args.feature_id)
    if feature is None:
        fail(f"feature {args.feature_id!r} not found")
    _ensure_extras(feature)
    current = status_of(feature)
    if current not in ("pending", "blocked"):
        fail(
            f"cannot mark {args.feature_id} deferred from status={current!r}; "
            f"expected one of pending, blocked"
        )
    _clear_status_extras(feature)
    _append_attempt(feature, "deferred", args.until or "unspecified")
    feature["status"] = "deferred"
    if args.until:
        feature["deferred_until"] = args.until
    recompute_metadata(data)
    save_features(data)


# ---------- command: mark-regressed ----------

def cmd_mark_regressed(args):
    """Re-open a previously-passing feature as in_progress."""
    reason = args.reason or ""
    if len(reason) < REGRESS_REASON_MIN_CHARS:
        fail(
            f"--reason must be at least {REGRESS_REASON_MIN_CHARS} chars "
            f"(got {len(reason)})"
        )
    data = load_features()
    feature = _find_feature(data, args.feature_id)
    if feature is None:
        fail(f"feature {args.feature_id!r} not found")
    _ensure_extras(feature)
    current = status_of(feature)
    if current != "passing":
        fail(
            f"cannot mark {args.feature_id} regressed from status={current!r}; "
            f"expected status=passing"
        )
    _clear_status_extras(feature)
    _append_attempt(feature, "regressed", reason)
    feature["status"] = "in_progress"
    recompute_metadata(data)
    save_features(data)


# ---------- command: next-feature ----------

def cmd_next_feature(_args):
    """Pick the highest-priority pending feature and mark it in_progress."""
    data = load_features()
    candidates = []
    for f in data["features"]:
        if status_of(f) != "pending":
            continue
        _ensure_extras(f)
        deps = f.get("depends_on") or []
        deps_blocked = False
        for dep_id in deps:
            dep = _find_feature(data, dep_id)
            if dep is None or status_of(dep) != "passing":
                deps_blocked = True
                break
        if deps_blocked:
            continue
        candidates.append(f)
    if not candidates:
        fail("no eligible pending feature (all are blocked by unmet deps)")
    candidates.sort(
        key=lambda f: (
            PRIORITY_RANK.get(f.get("priority", "medium"), PRIORITY_RANK["medium"]),
            f.get("id", ""),
        )
    )
    chosen = candidates[0]
    _clear_status_extras(chosen)
    chosen["status"] = "in_progress"
    print(chosen.get("id", "?"))
    recompute_metadata(data)
    save_features(data)


# ---------- command: add ----------

def cmd_add(args):
    """Append a new feature to feature_list.json.

    Validation (per P2-14):
    - feature_id matches ID_REGEX end-to-end.
    - id does not already exist.
    - description non-empty after strip().
    - status in STATUSES and != passing.
    - depends_on: no self-deps, dedup preserving order, all ids exist.
    - Steps: STEPS_MAX total, each isinstance(str), <= STEP_MAX_CHARS.
    """
    feature_id = args.feature_id
    if not ID_REGEX.fullmatch(feature_id):
        fail(
            f"feature_id {feature_id!r} must match {ID_REGEX.pattern} "
            f"(start with a letter; then letters/digits/_/-)"
        )
    if args.category not in VALID_CATEGORIES:
        fail(f"category {args.category!r} is not in {sorted(VALID_CATEGORIES)}")
    description = (args.description or "").strip()
    if not description:
        fail("description must be non-empty (after stripping whitespace)")
    if args.status not in STATUSES:
        fail(f"status {args.status!r} is not in {STATUSES}")
    if args.status == "passing":
        fail("status=passing is reserved for mark-passing; cannot be used at add")
    priority = args.priority if args.priority in VALID_PRIORITIES else "medium"

    steps = _resolve_steps(args.step or [], args.steps_file)
    if len(steps) > STEPS_MAX:
        fail(f"too many steps ({len(steps)}); max is {STEPS_MAX}")
    for i, step in enumerate(steps):
        if not isinstance(step, str):
            fail(f"step #{i} must be a string; got {type(step).__name__}")
        cleaned = step.rstrip(chr(13) + chr(10))
        if len(cleaned) > STEP_MAX_CHARS:
            fail(
                f"step #{i} is {len(cleaned)} chars; max is {STEP_MAX_CHARS}"
            )
        if _is_placeholder_step(cleaned):
            fail(
                f"step #{i} is a placeholder ({cleaned!r}); "
                "author a real, verifiable step before adding the feature. "
                "See tools/_feature_state.py PLACEHOLDER_STEP_PATTERNS."
            )
        steps[i] = cleaned
    if not steps:
        print(
            "warning: feature added with no steps - end-to-end test plan "
            "missing; fill in --step or --steps-file before the first "
            "coding session.",
            flush=True,
        )

    data = load_features()
    if _find_feature(data, feature_id) is not None:
        fail(f"feature_id {feature_id!r} already exists")
    raw_deps = [d.strip() for d in (args.depends_on or "").split(",") if d.strip()]
    deps = list(dict.fromkeys(raw_deps))
    if feature_id in deps:
        fail(f"feature {feature_id!r} cannot depend on itself")
    known_ids = {f.get("id") for f in data["features"]}
    missing = [d for d in deps if d not in known_ids]
    if missing:
        fail(
            f"depends_on references unknown feature(s): {', '.join(missing)}"
        )

    new_feature = {
        "id": feature_id,
        "category": args.category,
        "description": description,
        "steps": steps,
        "status": args.status,
        "priority": priority,
        "depends_on": deps,
        "attempts": [],
    }
    data["features"].append(new_feature)
    recompute_metadata(data)
    save_features(data)


# ---------- command: update-metadata ----------

def cmd_update_metadata(_args):
    """Recompute the metadata block and write back."""
    data = load_features()
    recompute_metadata(data)
    save_features(data)


# ---------- command: remove ----------

def cmd_remove(args):
    """Remove a feature from feature_list.json.

    Rules:
    - feature_id must match ID_REGEX end-to-end.
    - the feature must exist.
    - status=passing requires --force (passing features are an
      audit signal; removing them is a deliberate human action).
    - removing a feature that other features' depends_on references
      is refused unless --force is given (downstream features would
      silently lose their dep).
    - no new attempt entry is appended: the whole feature is gone,
      so its audit log goes with it.
    """
    feature_id = args.feature_id
    if not ID_REGEX.fullmatch(feature_id):
        fail(
            f"feature_id {feature_id!r} must match {ID_REGEX.pattern} "
            f"(start with a letter; then letters/digits/_/-)"
        )
    force = bool(getattr(args, "force", False))
    data = load_features()
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    current = status_of(feature)
    if current == "passing" and not force:
        fail(
            f"refusing to remove passing feature {feature_id!r} without --force; "
            f"passing features are an audit signal"
        )
    dependents = [
        f.get("id", "?")
        for f in data["features"]
        if f is not feature
        and isinstance(f, dict)
        and feature_id in (f.get("depends_on") or [])
    ]
    if dependents and not force:
        fail(
            f"refusing to remove {feature_id!r}; still referenced by "
            f"depends_on of: {', '.join(dependents)}. Use --force to override."
        )
    data["features"].remove(feature)
    recompute_metadata(data)
    save_features(data)
