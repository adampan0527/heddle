# SPDX-License-Identifier: Apache-2.0
"""feature_list_io — single source of truth for feature_list.json I/O and mutation.

Per TECH.md T-014 and DESIGN.md §2 "Notes on existing HARNESS alignment":
the heddle daemon (and the HARNESS CLI) both call into this module so
there is one implementation of:

  - load / save (atomic)
  - schema validation (id regex, status whitelist, category whitelist,
    priority whitelist, depends_on sanity, step shape)
  - status transitions (mark_in_progress, mark_passing, mark_blocked,
    mark_deferred, mark_regressed)
  - automatic metadata recompute
  - audit log append (attempts[])
  - placeholder-step rejection (so features stay implementable)
  - next_feature selection (priority order, deps-respecting)

The HARNESS CLI (tools/feature_list.py) wraps each function with argparse
parsing; the daemon calls them directly with keyword arguments.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Final

from . import atomic_io

# ---------- path resolution ----------

# Default path: HARNESS/feature_list.json in this dev repo.
# Daemon calls pass an explicit path (per-project_path / feature_list.json
# in the user's selected project directory).
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DEFAULT_PATH: Final[Path] = _REPO_ROOT / "HARNESS" / "feature_list.json"

# Indent for save (matches HARNESS).
INDENT: Final[int] = 2

# ---------- constants (single source of truth) ----------

STATUSES: Final[tuple[str, ...]] = ("pending", "in_progress", "blocked", "deferred", "passing")
VALID_CATEGORIES: Final[frozenset[str]] = frozenset({
    "functional", "ui", "error-handling", "accessibility", "performance",
})
VALID_PRIORITIES: Final[frozenset[str]] = frozenset({"high", "medium", "low"})
VALID_ATTEMPT_OUTCOMES: Final[tuple[str, ...]] = ("passing", "blocked", "deferred", "regressed")

# ---------- feature_list.json schema version (T-022 / feat-009) ----------
#
# `feature_list.json` carries a top-level `schema_version: <int>` field.
# On read, a missing field is treated as version 0 (legacy, pre-v1 files).
# On write, the current SCHEMA_VERSION is always stamped so any subsequent
# read round-trips cleanly.
#
# `SCHEMA_VERSION_MAX` is the highest version this build of heddle_common
# understands. Bump it whenever a newer build reads an older file with
# new optional fields it does not yet know how to handle. Loading a file
# whose `schema_version` exceeds `SCHEMA_VERSION_MAX` raises
# `SchemaVersionError` so the caller can show the user an actionable
# "please upgrade heddle" message instead of silently corrupting data.
SCHEMA_VERSION: Final[int] = 1
SCHEMA_VERSION_MAX: Final[int] = 1


class SchemaVersionError(Exception):
    """Raised when `feature_list.json` carries a `schema_version` this build cannot read.

    The error message includes both the file's version and this build's
    `SCHEMA_VERSION_MAX`, plus a hint about how to recover (upgrade heddle,
    or migrate the file down with a future `heddle migrate` subcommand).
    """


BLOCK_REASON_MIN_CHARS: Final[int] = 5
REGRESS_REASON_MIN_CHARS: Final[int] = 10
PRIORITY_RANK: Final[dict[str, int]] = {"high": 0, "medium": 1, "low": 2}

STEPS_MAX: Final[int] = 100
STEP_MAX_CHARS: Final[int] = 500

ID_REGEX: Final[re.Pattern[str]] = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*")

PLACEHOLDER_STEP_PATTERNS: Final[frozenset[str]] = frozenset({
    "tbd", "tbd.", "todo", "todo.", "fill in", "fill in later",
    "fill me in", "fill me out", "placeholder", "n/a", "na",
    "?", "...", "[step]", "[steps]", "[fill-in]", "[fill in]",
    "to be filled by initializer", "to be filled in later",
})
PLACEHOLDER_STEP_SUBSTRINGS: Final[tuple[str, ...]] = (
    "tbd -", "tbd:", "todo -", "todo:", "fill-in:", "fill in:", "fixme",
)


# ---------- IO helpers ----------

def fail(msg: str) -> None:
    """Print an error to stderr and exit with code 1."""
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _resolve_path(path: Path | str | None) -> Path:
    if path is None:
        return DEFAULT_PATH
    return path if isinstance(path, Path) else Path(path)


def load(path: Path | str | None = None) -> dict[str, Any]:
    """Load and return the feature_list.json dict from `path`.

    Validates that the file exists and parses as JSON; that a top-level
    `features` array is present. Calls `fail()` (which exits 1) on error.

    Per T-022 / feat-009:
      - A top-level `schema_version` is required in the loaded dict. If the
        file omits it, the dict is stamped with `schema_version = 0` to
        mark the legacy form. (Saving back via `save()` then promotes it
        to `SCHEMA_VERSION`.)
      - If `schema_version > SCHEMA_VERSION_MAX`, `SchemaVersionError` is
        raised with a message naming both versions and a recovery hint.
    """
    p = _resolve_path(path)
    if not p.exists():
        fail(f"{p} not found. Are you in the project root?")
    try:
        data = atomic_io.load_json(p)
    except Exception as exc:
        fail(f"{p} is not valid JSON: {exc}")
    if "features" not in data or not isinstance(data["features"], list):
        fail(f"{p} must contain a 'features' array.")

    raw_version = data.get("schema_version")
    if raw_version is None:
        # Legacy file predates the schema_version field. Treat as v0
        # internally; the next save() promotes it to SCHEMA_VERSION.
        data["schema_version"] = 0
    elif isinstance(raw_version, bool) or not isinstance(raw_version, int):
        fail(
            f"{p} has a non-integer schema_version: {raw_version!r} "
            f"(type={type(raw_version).__name__}); fix the file by hand."
        )
    elif raw_version < 0:
        fail(
            f"{p} has a negative schema_version: {raw_version}; "
            "fix the file by hand."
        )
    elif raw_version > SCHEMA_VERSION_MAX:
        raise SchemaVersionError(
            f"{p} has schema_version={raw_version} but this build of "
            f"heddle_common supports at most schema_version="
            f"{SCHEMA_VERSION_MAX}. Please upgrade heddle, or run "
            f"`heddle migrate {raw_version} {SCHEMA_VERSION_MAX}` once "
            f"that subcommand is available."
        )
    return data


def save(path: Path | str | None, data: dict[str, Any]) -> None:
    """Atomically write `data` to feature_list.json at `path`.

    Always stamps the current `SCHEMA_VERSION` into the written payload
    (per T-022 / feat-009), regardless of what `data["schema_version"]`
    held on the way in. This means a load + save cycle migrates legacy
    (schema_version=0) files to schema_version=1 in place.
    """
    p = _resolve_path(path)
    data["schema_version"] = SCHEMA_VERSION
    atomic_io.atomic_write_json(p, data, indent=INDENT, ensure_ascii=False)


def status_of(feature: dict[str, Any]) -> str | None:
    return feature.get("status")


def recompute_metadata(data: dict[str, Any]) -> None:
    """Recompute the `metadata` block from the current features list."""
    features = data["features"]
    counts: dict[str, int] = {s: 0 for s in STATUSES}
    for feature in features:
        status = status_of(feature)
        if status in counts:
            counts[status] += 1
    data["metadata"] = {
        "total_features": len(features),
        "passing": counts["passing"],
        "failing": sum(counts[s] for s in STATUSES if s != "passing"),
        "in_progress": counts["in_progress"],
        "blocked": counts["blocked"],
        "deferred": counts["deferred"],
        "last_updated": date.today().isoformat(),
    }


# ---------- internal helpers (validation, audit log) ----------

def _is_placeholder_step(step: str) -> bool:
    """Return True if `step` (already cleaned of CR/LF) is a placeholder."""
    normalized = step.strip().lower()
    if not normalized:
        return True
    if normalized in PLACEHOLDER_STEP_PATTERNS:
        return True
    return any(token in normalized for token in PLACEHOLDER_STEP_SUBSTRINGS)


def _ensure_extras(feature: dict[str, Any]) -> None:
    feature.setdefault("steps", [])
    feature.setdefault("depends_on", [])
    feature.setdefault("attempts", [])


def _clear_status_extras(feature: dict[str, Any]) -> None:
    feature.pop("blocked_reason", None)
    feature.pop("deferred_until", None)


def _append_attempt(feature: dict[str, Any], outcome: str, note: str) -> None:
    if outcome not in VALID_ATTEMPT_OUTCOMES:
        fail(f"invalid attempt outcome: {outcome!r}; expected one of {VALID_ATTEMPT_OUTCOMES}")
    feature["attempts"].append({
        "session": None,
        "by": "coding-agent",
        "outcome": outcome,
        "note": note,
        "at": date.today().isoformat(),
    })


def _find_feature(data: dict[str, Any], feature_id: str) -> dict[str, Any] | None:
    for f in data["features"]:
        if f.get("id") == feature_id:
            return f
    return None


def _assert_deps_passing(feature: dict[str, Any], data: dict[str, Any]) -> None:
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


def _wake_downstream(feature: dict[str, Any], data: dict[str, Any]) -> None:
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


def _resolve_steps(
    step_args: list[str],
    steps_file_path: Path | str | None,
) -> list[str]:
    if step_args and steps_file_path is not None:
        fail("--step and --steps-file are mutually exclusive")
    if steps_file_path is not None:
        p = Path(steps_file_path) if isinstance(steps_file_path, str) else steps_file_path
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read --steps-file {p}: {exc}")
        steps = [ln.rstrip("\r\n") for ln in text.splitlines()]
        return [s for s in steps if s.strip()]
    return list(step_args)


# ---------- mutation: add ----------

def add(
    path: Path | str | None,
    *,
    feature_id: str,
    category: str,
    description: str,
    status: str = "pending",
    priority: str | None = None,
    step: list[str] | tuple[str, ...] = (),
    steps_file: Path | str | None = None,
    depends_on: str = "",
) -> None:
    """Append a new feature to feature_list.json at `path`.

    Validation matches the HARNESS CLI:
      - ID_REGEX match, unique id, non-empty description
      - category in VALID_CATEGORIES, status in STATUSES (not 'passing')
      - depends_on: no self-deps, dedup, all ids exist
      - STEPS_MAX steps, each string <= STEP_MAX_CHARS, no placeholders
    """
    if not ID_REGEX.fullmatch(feature_id):
        fail(
            f"feature_id {feature_id!r} must match {ID_REGEX.pattern} "
            f"(start with a letter; then letters/digits/_/-)"
        )
    if category not in VALID_CATEGORIES:
        fail(f"category {category!r} is not in {sorted(VALID_CATEGORIES)}")
    desc = (description or "").strip()
    if not desc:
        fail("description must be non-empty (after stripping whitespace)")
    if status not in STATUSES:
        fail(f"status {status!r} is not in {STATUSES}")
    if status == "passing":
        fail("status=passing is reserved for mark_passing; cannot be used at add")
    priority_norm = priority if priority in VALID_PRIORITIES else "medium"

    steps = _resolve_steps(list(step), steps_file)
    if len(steps) > STEPS_MAX:
        fail(f"too many steps ({len(steps)}); max is {STEPS_MAX}")
    for i, step_str in enumerate(steps):
        if not isinstance(step_str, str):
            fail(f"step #{i} must be a string; got {type(step_str).__name__}")
        cleaned = step_str.rstrip("\r\n")
        if len(cleaned) > STEP_MAX_CHARS:
            fail(f"step #{i} is {len(cleaned)} chars; max is {STEP_MAX_CHARS}")
        if _is_placeholder_step(cleaned):
            fail(
                f"step #{i} is a placeholder ({cleaned!r}); "
                "author a real, verifiable step before adding the feature."
            )
        steps[i] = cleaned
    if not steps:
        print(
            "warning: feature added with no steps - end-to-end test plan "
            "missing; fill in --step or --steps-file before the first "
            "coding session.",
            flush=True,
        )

    data = load(path)
    if _find_feature(data, feature_id) is not None:
        fail(f"feature_id {feature_id!r} already exists")
    raw_deps = [d.strip() for d in (depends_on or "").split(",") if d.strip()]
    deps = list(dict.fromkeys(raw_deps))
    if feature_id in deps:
        fail(f"feature {feature_id!r} cannot depend on itself")
    known_ids = {f.get("id") for f in data["features"]}
    missing = [d for d in deps if d not in known_ids]
    if missing:
        fail(f"depends_on references unknown feature(s): {', '.join(missing)}")

    new_feature = {
        "id": feature_id,
        "category": category,
        "description": desc,
        "steps": steps,
        "status": status,
        "priority": priority_norm,
        "depends_on": deps,
        "attempts": [],
    }
    data["features"].append(new_feature)
    recompute_metadata(data)
    save(path, data)


# ---------- mutation: status transitions ----------

def mark_in_progress(path: Path | str | None, feature_id: str) -> None:
    """Transition feature_id to in_progress (idempotent if already)."""
    data = load(path)
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    _ensure_extras(feature)
    current = status_of(feature)
    if current == "in_progress":
        return
    if current not in ("pending", "blocked"):
        fail(
            f"cannot mark {feature_id} in_progress from status={current!r}; "
            f"expected one of pending, blocked"
        )
    _clear_status_extras(feature)
    feature["status"] = "in_progress"
    recompute_metadata(data)
    save(path, data)


def mark_passing(path: Path | str | None, feature_id: str) -> None:
    """Transition feature_id from in_progress to passing."""
    data = load(path)
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    _ensure_extras(feature)
    current = status_of(feature)
    if current != "in_progress":
        fail(
            f"cannot mark {feature_id} passing from status={current!r}; "
            f"expected status=in_progress"
        )
    _assert_deps_passing(feature, data)
    _clear_status_extras(feature)
    _append_attempt(feature, "passing", feature_id)
    feature["status"] = "passing"
    _wake_downstream(feature, data)
    recompute_metadata(data)
    save(path, data)


def mark_blocked(path: Path | str | None, feature_id: str, *, reason: str) -> None:
    """Transition feature_id to blocked with a mandatory reason."""
    if len(reason or "") < BLOCK_REASON_MIN_CHARS:
        fail(
            f"--reason must be at least {BLOCK_REASON_MIN_CHARS} chars "
            f"(got {len(reason or '')})"
        )
    data = load(path)
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    _ensure_extras(feature)
    current = status_of(feature)
    if current not in ("pending", "in_progress"):
        fail(
            f"cannot mark {feature_id} blocked from status={current!r}; "
            f"expected one of pending, in_progress"
        )
    _clear_status_extras(feature)
    _append_attempt(feature, "blocked", reason)
    feature["status"] = "blocked"
    feature["blocked_reason"] = reason
    recompute_metadata(data)
    save(path, data)


def mark_deferred(path: Path | str | None, feature_id: str, *, until: str | None = None) -> None:
    """Transition feature_id to deferred (optionally with --until)."""
    data = load(path)
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    _ensure_extras(feature)
    current = status_of(feature)
    if current not in ("pending", "blocked"):
        fail(
            f"cannot mark {feature_id} deferred from status={current!r}; "
            f"expected one of pending, blocked"
        )
    _clear_status_extras(feature)
    _append_attempt(feature, "deferred", until or "unspecified")
    feature["status"] = "deferred"
    if until:
        feature["deferred_until"] = until
    recompute_metadata(data)
    save(path, data)


def mark_regressed(path: Path | str | None, feature_id: str, *, reason: str) -> None:
    """Re-open a previously-passing feature as in_progress."""
    if len(reason or "") < REGRESS_REASON_MIN_CHARS:
        fail(
            f"--reason must be at least {REGRESS_REASON_MIN_CHARS} chars "
            f"(got {len(reason or '')})"
        )
    data = load(path)
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    _ensure_extras(feature)
    current = status_of(feature)
    if current != "passing":
        fail(
            f"cannot mark {feature_id} regressed from status={current!r}; "
            f"expected status=passing"
        )
    _clear_status_extras(feature)
    _append_attempt(feature, "regressed", reason)
    feature["status"] = "in_progress"
    recompute_metadata(data)
    save(path, data)


def next_feature(path: Path | str | None) -> str:
    """Pick the highest-priority pending feature and mark it in_progress.

    Returns the chosen feature_id. Raises SystemExit (via fail) if no
    eligible feature exists.
    """
    data = load(path)
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
    save(path, data)
    return chosen.get("id", "?")


def update_metadata(path: Path | str | None) -> None:
    """Recompute the metadata block and write back."""
    data = load(path)
    recompute_metadata(data)
    save(path, data)


def remove(path: Path | str | None, feature_id: str, *, force: bool = False) -> None:
    """Remove a feature from feature_list.json.

    Rules (mirroring the HARNESS CLI):
      - feature_id matches ID_REGEX.
      - the feature exists.
      - status=passing requires force=True.
      - dependents (features whose depends_on references this id) block
        removal unless force=True.
      - no audit log entry: the whole feature is gone.
    """
    if not ID_REGEX.fullmatch(feature_id):
        fail(
            f"feature_id {feature_id!r} must match {ID_REGEX.pattern} "
            f"(start with a letter; then letters/digits/_/-)"
        )
    data = load(path)
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    current = status_of(feature)
    if current == "passing" and not force:
        fail(
            f"refusing to remove passing feature {feature_id!r} without force=True; "
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
            f"depends_on of: {', '.join(dependents)}. Use force=True to override."
        )
    data["features"].remove(feature)
    recompute_metadata(data)
    save(path, data)