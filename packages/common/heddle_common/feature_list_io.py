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

# Default path: project-root `feature_list.json` (the active project list).
# This project's own working feature list lives at the repo root; the
# `HARNESS/feature_list.json` next to the vendored harness is the
# initializer template, not the active list. See HARNESS/CONTRACT.md /
# HARNESS/HARNESS.md §Architecture for the contract.
#
# Daemon calls pass an explicit `path` (per-project
# `feature_list.json` in the user's selected project directory), so
# this default only matters for the CLI / library callers that omit
# the path argument.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DEFAULT_PATH: Final[Path] = _REPO_ROOT / "feature_list.json"

# Indent for save (matches HARNESS).
INDENT: Final[int] = 2

# ---------- constants (single source of truth) ----------

STATUSES: Final[tuple[str, ...]] = ("pending", "in_progress", "blocked", "deferred", "passing")
VALID_CATEGORIES: Final[frozenset[str]] = frozenset({
    "functional", "ui", "error-handling", "accessibility", "performance",
})
VALID_PRIORITIES: Final[frozenset[str]] = frozenset({"high", "medium", "low"})
VALID_ATTEMPT_OUTCOMES: Final[tuple[str, ...]] = ("passing", "blocked", "deferred", "regressed")

# Feature "kind" — the categorisation that drives the kanban filter and
# the bugfix / enhancement validation rules. Per D-018, D-023, D-060:
#   - "feature"    : new work (default; UI default chip is gray)
#   - "bugfix"     : fixes a specific feature; `fixes` must be set
#   - "enhancement": adds capability to an existing feature; `enhances`
#                    must be set. v0.1 UI does not render the enhancement
#                    kind (D-060 says it's post-v0.1), but the schema
#                    supports it so the data layer does not need to
#                    change when the UI catches up.
KINDS: Final[tuple[str, ...]] = ("feature", "bugfix", "enhancement")
DEFAULT_KIND: Final[str] = "feature"

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

    # feat-010: backfill the five extended fields on every feature so
    # downstream code (and `save()`'s "always write all fields" rule)
    # can rely on them being present. setdefault keeps user-supplied
    # values intact, so this is a no-op for already-extended files.
    for feat in data["features"]:
        if isinstance(feat, dict):
            _ensure_extras(feat)

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
    # feat-010 / D-018, D-023, D-054, D-055, D-060: every feature carries
    # the five extended fields with documented defaults. setdefault keeps
    # already-set values intact (so a load() + save() round-trip is
    # faithful to whatever the user wrote) but backfills missing keys.
    feature.setdefault("kind", DEFAULT_KIND)
    feature.setdefault("fixes", None)
    feature.setdefault("enhances", None)
    feature.setdefault("superseded_by", None)
    feature.setdefault("implementation_model", None)
    # Defensive: if a legacy file (or external editor) wrote
    # `superseded_by: []` instead of null, normalize to None so
    # `next_feature` skips it and consumers don't have to special-case.
    if feature.get("superseded_by") == []:
        feature["superseded_by"] = None


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
    kind: str | None = None,
    fixes: str | None = None,
    enhances: str | None = None,
    superseded_by: list[str] | tuple[str, ...] | str | None = None,
    implementation_model: str | None = None,
) -> None:
    """Append a new feature to feature_list.json at `path`.

    Validation matches the HARNESS CLI:
      - ID_REGEX match, unique id, non-empty description
      - category in VALID_CATEGORIES, status in STATUSES (not 'passing')
      - depends_on: no self-deps, dedup, all ids exist
      - STEPS_MAX steps, each string <= STEP_MAX_CHARS, no placeholders
      - kind in KINDS (defaults to DEFAULT_KIND)
      - if kind=bugfix then `fixes` is set and the target exists
      - if kind=enhancement then `enhances` is set and the target exists
      - superseded_by: list of existing feature ids (DAG re-link is
        handled by the LLM; the library only validates the entries exist)
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

    kind_norm = kind if kind in KINDS else DEFAULT_KIND
    if kind is not None and kind not in KINDS:
        fail(f"kind {kind!r} is not in {sorted(KINDS)}")

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

    # ---- feat-010 / D-023, D-060: kind-pointer validation ----
    # bugfix requires `fixes`; enhancement requires `enhances`. The
    # target feature must already exist (we cannot validate its status
    # at add-time because the bugfix could be added before the broken
    # feature is recorded; feat-016's DAG validation catches "fixes
    # already-passing" later).
    if kind_norm == "bugfix":
        if fixes is None or fixes == "":
            fail(f"kind=bugfix requires a non-empty `fixes` target (got {fixes!r})")
        if fixes not in known_ids:
            fail(f"bugfix `fixes` target {fixes!r} does not exist")
        if fixes == feature_id:
            fail(f"bugfix {feature_id!r} cannot fix itself")
    if kind_norm == "enhancement":
        if enhances is None or enhances == "":
            fail(f"kind=enhancement requires a non-empty `enhances` target (got {enhances!r})")
        if enhances not in known_ids:
            fail(f"enhancement `enhances` target {enhances!r} does not exist")
        if enhances == feature_id:
            fail(f"enhancement {feature_id!r} cannot enhance itself")

    # ---- feat-010 / D-054: superseded_by is a list of existing ids ----
    if isinstance(superseded_by, str):
        raw_supp = [s.strip() for s in superseded_by.split(",") if s.strip()]
    else:
        raw_supp = [str(s).strip() for s in (superseded_by or []) if str(s).strip()]
    supp = list(dict.fromkeys(raw_supp))
    for sid in supp:
        if sid == feature_id:
            fail(f"feature {feature_id!r} cannot supersede itself")
        if sid not in known_ids:
            fail(f"superseded_by references unknown feature: {sid!r}")
    # Normalize the empty case to None so that the on-disk JSON uses
    # `"superseded_by": null` instead of `"superseded_by": []`. Both
    # read back as "not superseded" but null matches the spec wording
    # (D-054: "nullable metadata field") and round-trips cleanly.
    if not supp:
        supp = None

    # implementation_model is opaque — the daemon / feat-031 validates it
    # against the named-config registry at drag-time. The library just
    # stores the string. Empty string is normalized to None.

    new_feature = {
        "id": feature_id,
        "category": category,
        "description": desc,
        "steps": steps,
        "status": status,
        "priority": priority_norm,
        "depends_on": deps,
        "attempts": [],
        "kind": kind_norm,
        "fixes": fixes,
        "enhances": enhances,
        "superseded_by": supp,
        "implementation_model": implementation_model or None,
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

    Skips features with `superseded_by != null` (per D-054: features
    replaced by a split/merge proposal are "treated as if absent from
    the work graph" — they live in the Archive lane, not in the main
    queue). Note that `superseded_by` does NOT auto-resolve their
    depends_on edges; downstream features still see the original dep
    as un-passing, which is the correct behaviour since the LLM-driven
    split/merge flow (feat-054) is responsible for re-linking those.
    """
    data = load(path)
    candidates = []
    for f in data["features"]:
        if status_of(f) != "pending":
            continue
        _ensure_extras(f)
        if f.get("superseded_by"):
            # Per D-054: skipped by next-feature.
            continue
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


def set_steps(
    path: Path | str | None,
    feature_id: str,
    *,
    step: list[str] | tuple[str, ...] = (),
    steps_file: Path | str | None = None,
) -> None:
    """Replace the ``steps`` array on an existing feature.

    Validation mirrors :func:`add` for the steps themselves:
      - ``step`` and ``steps_file`` are mutually exclusive.
      - len(steps) <= STEPS_MAX.
      - every step is a string, <= STEP_MAX_CHARS after CR/LF strip,
        and not a placeholder per :func:`_is_placeholder_step`.
      - empty step list is permitted (a feature may legitimately have
        no end-to-end test plan; the warning is informational).

    The function does NOT change ``status``, ``category``, ``priority``,
    ``depends_on``, ``kind``, or any of the extended fields added in
    feat-010 — it only replaces the ``steps`` array. Use :func:`add`
    for first-creation and :func:`set_steps` for follow-up edits of
    the test plan.

    Per CODE_STYLE.md "Data integrity via scripts", this is the ONLY
    sanctioned way to mutate an existing feature's ``steps`` —
    direct edits to ``feature_list.json`` are forbidden.

    Raises SystemExit via :func:`fail` on validation errors.
    """
    if not ID_REGEX.fullmatch(feature_id):
        fail(
            f"feature_id {feature_id!r} must match {ID_REGEX.pattern} "
            f"(start with a letter; then letters/digits/_/-)"
        )
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
                "author a real, verifiable step before setting steps."
            )
        steps[i] = cleaned
    data = load(path)
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    _ensure_extras(feature)
    feature["steps"] = steps
    recompute_metadata(data)
    save(path, data)


# ---------- mutations: post-confirm feature modification (feat-054 / D-054) ----------
#
# Six new ops let the user modify a confirmed feature in place via
# dialog commands. The non-destructive ops (`edit`, `set_priority`,
# `add_dep`, `remove_dep`) mutate a single feature's row. The
# destructive ops (`split`, `merge`) add new feature rows AND mark
# the source(s) as superseded by the new target(s), so the source
# hides from the main kanban (per D-054). Each destructive op also
# builds a `diff` object describing the before/after state — the
# dialog UI shows the diff in a confirmation card before invoking
# the mutation (D-054: "destructive ops MUST show diff before
# execution").
#
# ID assignment policy (split only):
#   The first new feature reuses a caller-supplied `id`; subsequent
#   siblings get the parent id + "-<index>" suffix. The daemon-side
#   caller is responsible for picking ids that don't collide with
#   existing features. The library only validates uniqueness.
#
# All operations below are idempotent-no-op when the data already
# matches (e.g. set_priority to the current priority); they still
# recompute metadata + save so a round-trip is safe for callers
# that want to use the response for a refresh signal.


def _next_id_for_split(base_id: str, index: int, existing_ids: set[str]) -> str:
    """Return a unique id for the Nth split child (1-indexed)."""
    suffix = f"-{index}"
    candidate = f"{base_id}{suffix}"
    # In practice split creates at most a handful of children, so a
    # simple loop is fine. If the user asks for more than 10 we'll
    # still produce something but the suffix gets longer.
    if candidate not in existing_ids:
        return candidate
    counter = 2
    while True:
        candidate = f"{base_id}{suffix}-{counter}"
        if candidate not in existing_ids:
            return candidate
        counter += 1


def _feature_to_dict(feature: dict[str, Any]) -> dict[str, Any]:
    """Snapshot a feature row for diff payloads.

    Returns a shallow copy so the caller can mutate it (e.g. to
    assemble a synthetic "after" view) without affecting the
    underlying dict.
    """
    return {k: v for k, v in feature.items()}


def _normalize_deps(deps: Any) -> list[str]:
    """Coerce an iterable / None / single-string input into a list[str]."""
    if deps is None:
        return []
    if isinstance(deps, str):
        return [d.strip() for d in deps.split(",") if d.strip()]
    return [str(d).strip() for d in deps if d and str(d).strip()]


def split_feature(
    path: Path | str | None,
    feature_id: str,
    *,
    new_features: list[dict[str, Any]],
) -> dict[str, Any]:
    """Split a feature into N new features (D-054 / feat-054).

    The original feature is marked ``superseded_by`` the first new
    feature (so it hides from the main kanban / ``next_feature``),
    and each entry in ``new_features`` is appended as a fresh
    ``feat-XXX`` row via :func:`add`. The caller supplies
    ``{title, description, steps, depends_on}`` for each child;
    ``category``, ``kind``, ``priority`` follow the parent's values.

    Returns a dict with:
      - ``source``: the post-mutation snapshot of the original
        (now superseded) row.
      - ``created``: list of new feature rows in submission order.
      - ``diff``: a human-readable before/after summary for the UI.

    Raises :func:`fail` (SystemExit) on validation errors
    (unknown id, empty children list, duplicate new ids, etc.).
    """
    if not ID_REGEX.fullmatch(feature_id):
        fail(
            f"feature_id {feature_id!r} must match {ID_REGEX.pattern}"
        )
    if not isinstance(new_features, list) or len(new_features) == 0:
        fail("new_features must be a non-empty list")
    if len(new_features) > 20:
        fail(f"too many split children ({len(new_features)}); max is 20")

    data = load(path)
    source = _find_feature(data, feature_id)
    if source is None:
        fail(f"feature {feature_id!r} not found")
    _ensure_extras(source)
    source_snapshot = _feature_to_dict(source)
    known_ids = {f.get("id") for f in data["features"]}

    # Resolve / validate each proposed child's id. Callers may pass
    # an explicit `id` per child; otherwise we synthesise a
    # `<base>-<index>` id and let add() do the uniqueness check.
    resolved_children: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(new_features, start=1):
        if not isinstance(raw, dict):
            fail(f"new_features[{idx - 1}] must be a dict")
        explicit_id = raw.get("id")
        if isinstance(explicit_id, str) and explicit_id.strip():
            new_id = explicit_id.strip()
        else:
            new_id = _next_id_for_split(feature_id, idx, known_ids)
        if new_id == feature_id:
            fail(f"split child id {new_id!r} must differ from source")
        if new_id in known_ids or new_id in seen_ids:
            fail(f"split child id {new_id!r} already exists")
        seen_ids.add(new_id)
        known_ids.add(new_id)
        depends = _normalize_deps(raw.get("depends_on"))
        # The source is about to be superseded; if a child claims a
        # dep on it, that would create a phantom dep on an archived
        # feature. Substitute the source's own superseded_by chain
        # (or just drop the dep and let the user re-add it).
        depends = [d for d in depends if d != feature_id]
        resolved_children.append({
            "id": new_id,
            "title": (raw.get("title") or "").strip(),
            "description": (raw.get("description") or "").strip(),
            "steps": list(raw.get("steps") or []),
            "depends_on": depends,
            "category": raw.get("category") or source.get("category") or "functional",
            "priority": raw.get("priority") or source.get("priority") or "medium",
            "kind": raw.get("kind") or source.get("kind") or DEFAULT_KIND,
        })
    # First child becomes the successor — the source's
    # superseded_by points at it.
    successor_id = resolved_children[0]["id"]

    # Build + add each child via the canonical `add()` helper so
    # validation (id regex, depends_on, step placeholders, etc.)
    # runs the same path as a fresh draft-confirm.
    for child in resolved_children:
        # Title → description for add(); add() only accepts
        # `description`, so we lift the child's title into it (the
        # stored row's `description` will be the title; a fuller
        # prose summary is the user's responsibility).
        desc = child["description"] or child["title"]
        add(
            path,
            feature_id=child["id"],
            category=child["category"],
            description=desc,
            priority=child["priority"],
            step=child["steps"],
            depends_on=",".join(child["depends_on"]),
            kind=child["kind"],
        )

    # Reload to capture the post-add state and stamp superseded_by.
    data = load(path)
    source = _find_feature(data, feature_id)
    if source is None:  # pragma: no cover — defensive
        fail(f"feature {feature_id!r} vanished mid-split")
    source["superseded_by"] = successor_id
    created = [
        _find_feature(data, c["id"])
        for c in resolved_children
        if _find_feature(data, c["id"]) is not None
    ]
    diff = _build_diff_split(source_snapshot, created, successor_id)
    recompute_metadata(data)
    save(path, data)
    return {
        "source": _feature_to_dict(source),
        "created": [_feature_to_dict(c) for c in created],
        "diff": diff,
    }


def merge_features(
    path: Path | str | None,
    source_ids: list[str],
    *,
    target: dict[str, Any],
) -> dict[str, Any]:
    """Merge N source features into one new target feature (D-054).

    Each source is marked ``superseded_by`` the new target's id and
    hides from the main kanban. The target row is added via the
    canonical :func:`add` path so its validation rules match a
    fresh draft.

    Returns a dict with:
      - ``sources``: post-mutation snapshots of the source rows.
      - ``created``: the new merged feature row.
      - ``diff``: a human-readable before/after summary for the UI.
    """
    if not isinstance(source_ids, list) or len(source_ids) == 0:
        fail("source_ids must be a non-empty list")
    if len(source_ids) > 20:
        fail(f"too many sources ({len(source_ids)}); max is 20")
    if not isinstance(target, dict):
        fail("target must be a dict")

    data = load(path)
    sources = [_find_feature(data, sid) for sid in source_ids]
    for sid, row in zip(source_ids, sources):
        if row is None:
            fail(f"feature {sid!r} not found")
    if any(s.get("id") == target.get("id") for s in sources):
        fail("target id must differ from every source id")
    source_snapshots = [_feature_to_dict(s) for s in sources]

    target_id = (target.get("id") or "").strip()
    if not target_id:
        fail("target.id must be a non-empty string")
    if target_id in {f.get("id") for f in data["features"]}:
        fail(f"target id {target_id!r} already exists")
    depends = _normalize_deps(target.get("depends_on"))
    # Drop any deps pointing at the sources themselves (the sources
    # will be archived).
    depends = [d for d in depends if d not in set(source_ids)]

    add(
        path,
        feature_id=target_id,
        category=target.get("category") or sources[0].get("category") or "functional",
        description=(target.get("description") or target.get("title") or "").strip(),
        priority=target.get("priority") or sources[0].get("priority") or "medium",
        step=list(target.get("steps") or []),
        depends_on=",".join(depends),
        kind=target.get("kind") or sources[0].get("kind") or DEFAULT_KIND,
    )

    # Reload and stamp superseded_by on each source.
    data = load(path)
    successor = _find_feature(data, target_id)
    if successor is None:  # pragma: no cover
        fail(f"target {target_id!r} vanished mid-merge")
    for sid in source_ids:
        src = _find_feature(data, sid)
        if src is None:  # pragma: no cover
            continue
        src["superseded_by"] = target_id
    updated_sources = [_find_feature(data, sid) for sid in source_ids]
    diff = _build_diff_merge(source_snapshots, successor)
    recompute_metadata(data)
    save(path, data)
    return {
        "sources": [_feature_to_dict(s) for s in updated_sources if s is not None],
        "created": _feature_to_dict(successor),
        "diff": diff,
    }


def edit_feature(
    path: Path | str | None,
    feature_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    steps: list[str] | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """Edit non-destructive fields on an existing feature (D-054).

    Supports: title (stored as `description` on disk per
    feature_list_io convention), description (longer prose kept as
    a synthetic extended field), steps, category. None means
    "leave unchanged". Empty string means "clear".

    The function is intentionally NOT destructive — it never
    touches ``status``, ``depends_on``, ``priority``, or
    ``superseded_by``. For those, use the dedicated ops.

    Returns ``{feature: <updated row>, diff: <before/after summary>}``.
    """
    if not ID_REGEX.fullmatch(feature_id):
        fail(f"feature_id {feature_id!r} must match {ID_REGEX.pattern}")
    data = load(path)
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    _ensure_extras(feature)
    before = _feature_to_dict(feature)

    # Step validation (mirrors add() / set_steps()).
    new_steps: list[str] | None = None
    if steps is not None:
        new_steps = []
        for i, raw_step in enumerate(steps):
            if not isinstance(raw_step, str):
                fail(f"step #{i} must be a string")
            cleaned = raw_step.rstrip("\r\n")
            if len(cleaned) > STEP_MAX_CHARS:
                fail(f"step #{i} is {len(cleaned)} chars; max is {STEP_MAX_CHARS}")
            if _is_placeholder_step(cleaned):
                fail(
                    f"step #{i} is a placeholder ({cleaned!r}); "
                    "author a real, verifiable step."
                )
            new_steps.append(cleaned)

    # Title semantics: feature_list_io stores the short summary as
    # `description`. We accept either `title` or `description` in
    # the body — title wins if both are passed.
    new_description: str | None = None
    if title is not None:
        new_description = title.strip()
    elif description is not None:
        new_description = description.strip()
    if new_description is not None and not new_description:
        fail("description / title must be non-empty when provided")

    if category is not None:
        if category not in VALID_CATEGORIES:
            fail(f"category {category!r} is not in {sorted(VALID_CATEGORIES)}")

    # Apply mutations.
    if new_description is not None:
        feature["description"] = new_description
    if new_steps is not None:
        feature["steps"] = new_steps
    if category is not None:
        feature["category"] = category

    diff = _build_diff_edit(before, feature)
    recompute_metadata(data)
    save(path, data)
    return {"feature": _feature_to_dict(feature), "diff": diff}


def set_priority(
    path: Path | str | None,
    feature_id: str,
    *,
    priority: str,
) -> dict[str, Any]:
    """Reprioritize an existing feature (D-054).

    Accepts "high" | "medium" | "low". Idempotent — passing the
    current priority still returns a diff (empty changes list) and
    saves so the caller can use the response as a refresh signal.
    """
    if not ID_REGEX.fullmatch(feature_id):
        fail(f"feature_id {feature_id!r} must match {ID_REGEX.pattern}")
    if priority not in VALID_PRIORITIES:
        fail(f"priority {priority!r} is not in {sorted(VALID_PRIORITIES)}")
    data = load(path)
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    before = _feature_to_dict(feature)
    feature["priority"] = priority
    diff = {
        "before": {"priority": before.get("priority")},
        "after": {"priority": priority},
        "changes": (
            [] if before.get("priority") == priority else ["priority"]
        ),
    }
    recompute_metadata(data)
    save(path, data)
    return {"feature": _feature_to_dict(feature), "diff": diff}


def update_deps(
    path: Path | str | None,
    feature_id: str,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """Add / remove depends_on entries on a feature (D-054).

    `add` and `remove` are optional; both default to no-op. Each
    is normalised as a list of feature ids. Entries appearing in
    BOTH lists are rejected (ambiguous). Removal of a dep that
    isn't present is silently ignored (idempotent).
    """
    if not ID_REGEX.fullmatch(feature_id):
        fail(f"feature_id {feature_id!r} must match {ID_REGEX.pattern}")
    add_list = _normalize_deps(add)
    remove_list = _normalize_deps(remove)
    overlap = set(add_list) & set(remove_list)
    if overlap:
        fail(f"add/remove both reference: {sorted(overlap)}")
    if not add_list and not remove_list:
        fail("at least one of add / remove must be a non-empty list")
    data = load(path)
    feature = _find_feature(data, feature_id)
    if feature is None:
        fail(f"feature {feature_id!r} not found")
    _ensure_extras(feature)
    known_ids = {f.get("id") for f in data["features"] if f is not feature}

    # Validate each add target exists + isn't self.
    for dep in add_list:
        if dep == feature_id:
            fail(f"feature {feature_id!r} cannot depend on itself")
        if dep not in known_ids:
            fail(f"add dep {dep!r} does not exist")

    before = list(feature.get("depends_on") or [])
    after = list(before)
    # Drop removes first (so an entry in both lists is rejected
    # above rather than silently net-zero'd).
    for dep in remove_list:
        if dep in after:
            after.remove(dep)
    # Append adds (deduped, in submission order).
    for dep in add_list:
        if dep not in after:
            after.append(dep)

    feature["depends_on"] = after
    diff = {
        "before": list(before),
        "after": list(after),
        "added": [d for d in add_list if d not in before],
        "removed": [d for d in remove_list if d in before],
    }
    recompute_metadata(data)
    save(path, data)
    return {"feature": _feature_to_dict(feature), "diff": diff}


# ---------- diff builders (D-054 / feat-054) ----------
#
# Each builder returns a small dict the dialog UI can render in a
# confirmation card. The shape is intentionally JSON-friendly
# (lists + dicts + primitives only — no Python objects) so the
# browser can map it straight onto a "before/after" display.

def _build_diff_split(
    source_snapshot: dict[str, Any],
    created: list[dict[str, Any]],
    successor_id: str,
) -> dict[str, Any]:
    return {
        "operation": "split",
        "source": {"id": source_snapshot.get("id"), "title": source_snapshot.get("description")},
        "successor_id": successor_id,
        "created": [
            {"id": c.get("id"), "title": c.get("description")}
            for c in created
        ],
        "changes": [
            f"archive {source_snapshot.get('id')} (superseded_by={successor_id})",
            *[f"add {c.get('id')}" for c in created],
        ],
    }


def _build_diff_merge(
    source_snapshots: list[dict[str, Any]],
    successor: dict[str, Any],
) -> dict[str, Any]:
    return {
        "operation": "merge",
        "sources": [
            {"id": s.get("id"), "title": s.get("description")}
            for s in source_snapshots
        ],
        "successor": {"id": successor.get("id"), "title": successor.get("description")},
        "changes": [
            f"archive {s.get('id')} (superseded_by={successor.get('id')})"
            for s in source_snapshots
        ] + [f"add {successor.get('id')}"],
    }


def _build_diff_edit(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Field-by-field edit diff. Skips unchanged keys."""
    changes: list[dict[str, Any]] = []
    for key in ("description", "steps", "category"):
        b = before.get(key)
        a = after.get(key)
        if b != a:
            changes.append({"field": key, "before": b, "after": a})
    return {"operation": "edit", "changes": changes}