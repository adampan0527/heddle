#!/usr/bin/env python3
"""handoff_check.py — pre-flight check before a Coding Agent session starts.

This script is the mandatory gate at the start of every Coding Agent
session in the long-running agent harness. The Initializer Agent seeds
the project (SESSION 0) and then hands off; every subsequent Coding
Agent session MUST run this script before it is allowed to write
code. If any of the ten checks fail, the script exits 1 and the
agent must fix the issue (or escalate) before proceeding.

The ten checks, in order:

    1. json_parse           feature_list.json parses as JSON and has a
                            `features` array. A parse error means the
                            data file is corrupted; refuse to proceed.
    2. metadata_drift       feature_list.json's `metadata` block matches
                            the live counters from
                            `tools/feature_list.py status`. Drift means
                            someone bypassed the script; recompute before
                            starting work.
    3. session_0_exists     current_progress.txt contains a SESSION 0
                            block. Without it the Initializer handoff
                            contract is incomplete; later sessions have
                            no provenance for the project's starting
                            state.
    4. depends_on_acyclic   The `depends_on` graph across all features
                            has no cycles (A -> B -> A). A cycle makes
                            the priority-scheduling logic unsound and
                            is a Hard Rule violation in CODE_STYLE.md.
    5. depends_on_resolves  Every id referenced in any `depends_on`
                            list exists as a feature. A dangling id
                            means the catalog is broken; refuse to
                            pick next work from it.
    6. passing_deps_pass    Every feature with status=passing has all
                            of its dependencies also status=passing.
                            A passing feature that depends on a
                            pending one is a test-coverage lie.
    7. code_style_fillins   CODE_STYLE.md has fewer than
                            `--max-fillins` (default 0) `[fill-in]`
                            placeholders. The Initializer must fill
                            Part 2 of CODE_STYLE before any Coding
                            Agent starts; otherwise there are no
                            project-specific conventions to follow.
    8. git_readiness        Repository is a git working tree (or
                            explicitly skipped via HARNESS-level
                            opt-out). Without git, the WIP-commit
                            handoff primitive has no anchor.
    9. feature_schema       Every feature obeys the schema (id, status,
                            priority, etc.); required fields present,
                            enum values legal. A malformed entry would
                            let an inconsistent feature escape into
                            downstream tools.
   10. status_field_consistency
                            `attempts[].session` (when populated)
                            references a real SESSION N block in
                            current_progress.txt. Surfaces orphaned
                            or stale audit entries.

Usage:
    python tools/handoff_check.py [--strict] [--max-fillins N] [--json]

Flags:
    --strict               Default behavior is already strict. The flag
                            is reserved for future per-check skip flags
                            and is currently a no-op (all 10 checks
                            must pass).
    --max-fillins N        Override the CODE_STYLE.md [fill-in]
                            threshold. Default: 0.
    --json                 Emit one JSON object per check on stdout
                            (one line each), followed by a final
                            `summary` line. Useful for agents that
                            want to parse results without scraping
                            prose output.

Exit codes:
    0   all 10 checks passed (or only the JSON envelope printed and
        all `ok: true`)
    1   at least one check failed

Design notes:
    - stdlib only — no third-party deps. The harness explicitly avoids
      a runtime dependency on external packages for tools/ scripts.
    - The metadata check delegates to `tools/feature_list.py status`
      rather than re-implementing the counter logic. Single source of
      truth: if `feature_list.py status` changes its output keys, this
      script must be updated to match (intentional coupling).
    - The DAG check is intentionally permissive: it only walks
      features that exist and only follows `depends_on` edges. It does
      NOT detect orphan features (a feature with no path to passing);
      that is the agent's job during work, not at handoff time.
    - For the SESSION 0 check we accept a loose match (`SESSION 0`
      anywhere on the line, regardless of `### ` prefix or `—` vs
      `-` separator) because the Initializer template uses bare
      `SESSION 0 - Initializer Agent` rather than the canonical
      `### SESSION <N> — <ISO date>` heading used by `session_end.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from ._constants import STATUSES, VALID_CATEGORIES, VALID_PRIORITIES
    from ._feature_state import ID_REGEX
    from ._feature_state import _is_placeholder_step
except ImportError:
    from _constants import STATUSES, VALID_CATEGORIES, VALID_PRIORITIES
    ID_REGEX = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*")
    def _is_placeholder_step(step: str) -> bool:  # type: ignore[no-redef]
        # Fallback: same logic as _feature_state._is_placeholder_step.
        # Defined inline so handoff_check can run standalone (without
        # importing the full feature_state module).
        normalized = step.strip().lower()
        if not normalized:
            return True
        exact = frozenset({
            "tbd", "tbd.", "todo", "todo.", "fill in", "fill in later",
            "fill me in", "fill me out", "placeholder", "n/a", "na", "?",
            "...", "[step]", "[steps]", "[fill-in]", "[fill in]",
            "to be filled by initializer", "to be filled in later",
        })
        if normalized in exact:
            return True
        return any(t in normalized for t in (
            "tbd -", "tbd:", "todo -", "todo:", "fill-in:", "fill in:", "fixme",
        ))

# ---------- paths ----------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
FEATURE_LIST_PATH = PROJECT_ROOT / "feature_list.json"
PROGRESS_PATH = PROJECT_ROOT / "current_progress.txt"
CODE_STYLE_PATH = PROJECT_ROOT / "CODE_STYLE.md"

# Default cap on [fill-in] placeholders in CODE_STYLE.md. Part 2 of the
# style doc is supposed to be filled by the Initializer before any
# Coding Agent runs; if the count is over this, the project isn't
# ready for handoff.
#
# Default --max-fillins is 0 to match HARNESS.md: "every [fill-in] is a
# blocking state". Pass --max-fillins N to override during early init
# (e.g. immediately after `INITIALIZER_PROMPT.md` runs and Part 2 is
# not yet filled).
DEFAULT_MAX_FILLINS = 0

# ---------- check record ----------

# Each check writes one CheckRecord into the running list. After all
# checks run, we emit one line per record (or per-check JSON object)
# and a final summary. Keeping the records as a list (not a dict)
# preserves the order checks ran, which is the order we report.

class CheckRecord:
    __slots__ = ("name", "ok", "detail")

    def __init__(self, name: str, ok: bool, detail: str) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail

    def human_line(self) -> str:
        marker = "OK" if self.ok else "FAIL"
        # Detail is short by convention; if it's empty, the suffix is
        # just the check name. This keeps the default one-line output
        # scannable.
        suffix = f": {self.detail}" if self.detail else ""
        return f"[{marker}] {self.name}{suffix}"

    def json_obj(self) -> dict[str, object]:
        return {"check": self.name, "ok": self.ok, "detail": self.detail}


# ---------- error helpers ----------

def fail(msg: str) -> None:
    """Print an error to stderr and exit with a non-zero status."""
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------- subprocess helper ----------

def _run_status() -> tuple[bool, dict[str, str]]:
    """Run `feature_list.py status` and parse its key: value lines.

    Returns (ok, snapshot). `ok` is False if the subprocess failed
    or its output couldn't be parsed. `snapshot` maps metric name
    (e.g. "passing") to its string value as printed by `status`.

    We parse stdout line-by-line rather than scraping JSON so we don't
    have to keep two parsers in sync — `status` is the canonical
    formatter.
    """
    proc = subprocess.run(
        ["python", str(TOOLS_DIR / "feature_list.py"), "status"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return False, {"stderr": proc.stderr.strip()}
    snapshot: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        snapshot[key.strip()] = value.strip()
    return True, snapshot


# ---------- check 1: json_parse ----------

def check_json_parse() -> CheckRecord:
    """Load feature_list.json; report a parse error if it fails."""
    if not FEATURE_LIST_PATH.exists():
        return CheckRecord(
            "json_parse",
            False,
            f"{FEATURE_LIST_PATH.name} not found at project root.",
        )
    try:
        with FEATURE_LIST_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return CheckRecord(
            "json_parse",
            False,
            f"cannot parse feature_list.json: {e}",
        )
    except OSError as e:
        return CheckRecord(
            "json_parse",
            False,
            f"cannot read feature_list.json: {e}",
        )
    if not isinstance(data, dict) or "features" not in data or not isinstance(data["features"], list):
        return CheckRecord(
            "json_parse",
            False,
            "feature_list.json must be an object with a `features` array.",
        )
    return CheckRecord("json_parse", True, "")


# ---------- check 2: metadata_drift ----------

# Keys we expect to see in both feature_list.json's metadata block
# and the snapshot from `feature_list.py status`. Keeping this list
# explicit means a new key added on one side is a deliberate signal
# to update the other.
_METADATA_KEYS = (
    "total_features",
    "passing",
    "failing",
    "in_progress",
    "blocked",
    "deferred",
)


def check_metadata_drift(records: list[CheckRecord]) -> CheckRecord:
    """Compare on-disk `metadata` against the live counter snapshot.

    We rely on check 1 having already loaded and validated the file.
    If json_parse failed, this check is meaningless — mark it skipped
    via a fail (we want handoff to fail loudly if check 1 is broken).
    """
    prev = records[-1]
    if not prev.ok:
        return CheckRecord(
            "metadata_drift",
            False,
            "skipped: json_parse did not pass.",
        )
    with FEATURE_LIST_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("metadata", {})

    ok_sub, snapshot = _run_status()
    if not ok_sub:
        return CheckRecord(
            "metadata_drift",
            False,
            "feature_list.py status failed; cannot verify metadata.",
        )

    diffs: list[str] = []
    for key in _METADATA_KEYS:
        on_disk = meta.get(key)
        from_script = snapshot.get(key)
        if on_disk is None:
            diffs.append(f"missing metadata.{key}")
            continue
        if str(on_disk) != str(from_script):
            diffs.append(f"{key}: disk={on_disk} script={from_script}")

    if diffs:
        return CheckRecord(
            "metadata_drift",
            False,
            "; ".join(diffs) +
            " | metadata drift; run `tools/feature_list.py update-metadata` first",
        )
    return CheckRecord("metadata_drift", True, "")


# ---------- check 3: session_0_exists ----------

# Loose match: line must START with "SESSION 0" (with optional `### `
# prefix; no dash required at all). We also tolerate an optional
# em-dash / en-dash / ASCII hyphen separator (`[—–-]`) following
# the `0`, mirroring the relaxed SESSION heading regex in
# `tools/_constants.SESSION_HEADING_RE` (P1-10). The Initializer
# template uses `SESSION 0 - Initializer Agent` while session_end.py
# appends `### SESSION <N> — <ISO date>`. We accept both shapes, plus
# any auto-normalized variant produced by editors / CI formatters.
_SESSION_0_RE = re.compile(
    r"^\s*(?:###\s+)?SESSION\s+0[—–-]?\b",
    re.IGNORECASE,
)


def check_session_0_exists() -> CheckRecord:
    """Confirm current_progress.txt has a SESSION 0 block."""
    if not PROGRESS_PATH.exists():
        return CheckRecord(
            "session_0_exists",
            False,
            "current_progress.txt not found (Initializer must seed it).",
        )
    try:
        text = PROGRESS_PATH.read_text(encoding="utf-8")
    except OSError as e:
        return CheckRecord(
            "session_0_exists",
            False,
            f"cannot read current_progress.txt: {e}",
        )
    for line in text.splitlines():
        if _SESSION_0_RE.match(line):
            return CheckRecord("session_0_exists", True, "")
    return CheckRecord(
        "session_0_exists",
        False,
        "no SESSION 0 block in current_progress.txt "
        "(Initializer must seed it).",
    )


# ---------- check 4: depends_on_acyclic ----------

def _walk_features(records: list[CheckRecord]) -> list[dict[str, object]] | None:
    """Return the loaded features list, or None if json_parse failed."""
    if records and not records[0].ok:
        return None
    with FEATURE_LIST_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    features = data.get("features", [])
    return [f for f in features if isinstance(f, dict)]


def check_depends_on_acyclic(records: list[CheckRecord]) -> CheckRecord:
    """DFS the depends_on graph and report any cycle.

    A feature is a node; its `depends_on` list (defaults to []) is
    the out-edges. We use a standard three-color DFS (WHITE / GRAY /
    BLACK). A GRAY node we re-visit means we're inside its active
    DFS stack — that's a back-edge and a cycle.

    Cycle reporting: when we find a back-edge A -> B, B is currently
    on the stack. The cycle is the suffix of the stack starting at B,
    closed by A. We render it as "B -> ... -> A -> B" so the human
    reader sees the loop, not just one edge.
    """
    features = _walk_features(records)
    if features is None:
        return CheckRecord(
            "depends_on_acyclic",
            False,
            "skipped: json_parse did not pass.",
        )

    # Build id -> feature map for cheap lookup.
    by_id: dict[str, dict[str, object]] = {
        str(f.get("id", "")): f for f in features if f.get("id") is not None
    }

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {fid: WHITE for fid in by_id}
    stack: list[str] = []

    def visit(node: str) -> str | None:
        """Return the rendered cycle if a cycle is found, else None."""
        color[node] = GRAY
        stack.append(node)
        feat = by_id.get(node, {})
        deps = feat.get("depends_on", []) or []
        if not isinstance(deps, list):
            deps = []
        for dep in deps:
            dep_id = str(dep)
            # Ignore dangling deps in the cycle check; check 5 owns
            # those. We only fail here on actual cycles between
            # known-good edges.
            if dep_id not in by_id:
                continue
            dep_color = color.get(dep_id, WHITE)
            if dep_color == GRAY:
                # Cycle: from `dep_id` (already on stack) back through
                # `node` and the active edge to `dep_id`.
                start = stack.index(dep_id)
                cycle_nodes = stack[start:] + [dep_id]
                return " -> ".join(cycle_nodes)
            if dep_color == WHITE:
                found = visit(dep_id)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    # Sort ids for deterministic ordering — makes the cycle output
    # reproducible across runs, which helps when comparing logs.
    for fid in sorted(by_id):
        if color[fid] == WHITE:
            cycle = visit(fid)
            if cycle is not None:
                return CheckRecord(
                    "depends_on_acyclic",
                    False,
                    f"dependency cycle: {cycle}",
                )
    return CheckRecord("depends_on_acyclic", True, "")


# ---------- check 5: depends_on_resolves ----------

def check_depends_on_resolves(records: list[CheckRecord]) -> CheckRecord:
    """Every depends_on id must point to an existing feature."""
    features = _walk_features(records)
    if features is None:
        return CheckRecord(
            "depends_on_resolves",
            False,
            "skipped: json_parse did not pass.",
        )
    known_ids = {str(f.get("id", "")) for f in features if f.get("id") is not None}

    dangling: list[str] = []
    for f in features:
        fid = str(f.get("id", "?"))
        deps = f.get("depends_on", []) or []
        if not isinstance(deps, list):
            # A non-list depends_on is its own kind of broken; surface
            # it as a dangling entry so the agent sees both kinds of
            # breakage from this one check.
            dangling.append(f"{fid} -> <non-list depends_on>")
            continue
        for dep in deps:
            dep_id = str(dep)
            if dep_id not in known_ids:
                dangling.append(f"{fid} -> {dep_id}")

    if dangling:
        # Cap reported dangling edges at 5 to keep the detail string
        # short; if there are more, append a count.
        shown = "; ".join(dangling[:5])
        if len(dangling) > 5:
            shown += f" (... {len(dangling) - 5} more)"
        return CheckRecord(
            "depends_on_resolves",
            False,
            f"feature {shown} depends on non-existent feature",
        )
    return CheckRecord("depends_on_resolves", True, "")


# ---------- check 6: passing_deps_pass ----------

def check_passing_deps_pass(records: list[CheckRecord]) -> CheckRecord:
    """A passing feature's deps must all be passing too."""
    features = _walk_features(records)
    if features is None:
        return CheckRecord(
            "passing_deps_pass",
            False,
            "skipped: json_parse did not pass.",
        )

    by_id: dict[str, dict[str, object]] = {
        str(f.get("id", "")): f for f in features if f.get("id") is not None
    }

    violations: list[str] = []
    for f in features:
        fid = str(f.get("id", "?"))
        status = str(f.get("status", ""))
        if status != "passing":
            continue
        deps = f.get("depends_on", []) or []
        if not isinstance(deps, list):
            continue
        for dep in deps:
            dep_id = str(dep)
            dep_feat = by_id.get(dep_id)
            if dep_feat is None:
                # Dangling deps belong to check 5; don't double-report.
                continue
            dep_status = str(dep_feat.get("status", ""))
            if dep_status != "passing":
                violations.append(f"{fid} (passing) -> {dep_id} (status={dep_status})")

    if violations:
        shown = "; ".join(violations[:5])
        if len(violations) > 5:
            shown += f" (... {len(violations) - 5} more)"
        return CheckRecord(
            "passing_deps_pass",
            False,
            f"{shown}; cannot be passing",
        )
    return CheckRecord("passing_deps_pass", True, "")


# ---------- check 7: code_style_fillins ----------

_FILLIN_RE = re.compile(r"\[fill-in")


def check_code_style_fillins(max_fillins: int) -> CheckRecord:
    """Count `[fill-in]` placeholders in CODE_STYLE.md."""
    if not CODE_STYLE_PATH.exists():
        return CheckRecord(
            "code_style_fillins",
            False,
            "CODE_STYLE.md not found at project root.",
        )
    try:
        text = CODE_STYLE_PATH.read_text(encoding="utf-8")
    except OSError as e:
        return CheckRecord(
            "code_style_fillins",
            False,
            f"cannot read CODE_STYLE.md: {e}",
        )
    count = len(_FILLIN_RE.findall(text))
    if count > max_fillins:
        return CheckRecord(
            "code_style_fillins",
            False,
            f"too many [fill-in] placeholders (count={count}); codebase not ready",
        )
    return CheckRecord(
        "code_style_fillins",
        True,
        f"count={count} <= max={max_fillins}",
    )


# ---------- check 8: git_readiness ----------

def check_8_git_readiness() -> CheckRecord:
    """Require the directory containing HARNESS to be a usable git worktree."""
    project_root = PROJECT_ROOT.parent
    inside = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return CheckRecord(
            "git_readiness", False,
            "an outer git repository must exist before the current HARNESS/ directory",
        )
    status = subprocess.run(
        ["git", "-C", str(project_root), "status", "--porcelain"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if status.returncode != 0:
        return CheckRecord(
            "git_readiness", True,
            f"WARN: {status.stderr.strip() or 'git status --porcelain failed'}",
        )
    return CheckRecord("git_readiness", True, "")


# ---------- check 9: feature_schema ----------

def _feature_line(feature_id: object) -> int:
    needle = json.dumps(feature_id, ensure_ascii=False)
    lines = FEATURE_LIST_PATH.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        if '"id"' in line and needle in line:
            return line_number
    return 1


def check_9_feature_schema(records: list[CheckRecord]) -> CheckRecord:
    """Validate feature identity, enum fields, and executable steps."""
    features = _walk_features(records)
    if features is None:
        return CheckRecord("feature_schema", False, "skipped: json_parse did not pass.")
    failures: list[str] = []
    seen: set[str] = set()
    for feature in features:
        raw_id = feature.get("id")
        feature_id = str(raw_id) if raw_id is not None else "?"
        reasons: list[str] = []
        if not isinstance(raw_id, str) or not ID_REGEX.fullmatch(raw_id):
            reasons.append(f"id must match {ID_REGEX.pattern}")
        elif raw_id in seen:
            reasons.append("id must be globally unique")
        else:
            seen.add(raw_id)
        if feature.get("status") not in STATUSES:
            reasons.append(f"status must be one of {STATUSES}")
        if feature.get("category") not in VALID_CATEGORIES:
            reasons.append(f"category must be one of {sorted(VALID_CATEGORIES)}")
        if feature.get("priority") not in VALID_PRIORITIES:
            reasons.append(f"priority must be one of {VALID_PRIORITIES}")
        steps = feature.get("steps")
        if not isinstance(steps, list) or not steps:
            reasons.append("steps must be a non-empty array")
        elif isinstance(steps, list):
            # Per-step validation: placeholder steps are rejected because
            # a feature whose only step is "TBD" or "[fill-in]" is
            # unimplementable. Same rules as cmd_add — see
            # tools/_feature_state.PLACEHOLDER_STEP_PATTERNS.
            for j, step in enumerate(steps):
                if not isinstance(step, str):
                    reasons.append(f"step #{j} must be a string")
                    continue
                if _is_placeholder_step(step):
                    reasons.append(
                        f"step #{j} is a placeholder ({step!r}); "
                        "author a real, verifiable step"
                    )
        if reasons:
            failures.append(
                f"feature {feature_id} failed: {', '.join(reasons)} at "
                f"{FEATURE_LIST_PATH}:{_feature_line(raw_id)}"
            )
    return CheckRecord("feature_schema", not failures, "; ".join(failures))


# ---------- check 10: status_field_consistency ----------

def check_10_status_field_consistency(records: list[CheckRecord]) -> CheckRecord:
    """Validate status-only fields and warn about unowned attempts."""
    features = _walk_features(records)
    if features is None:
        return CheckRecord(
            "status_field_consistency", False, "skipped: json_parse did not pass."
        )
    failures: list[str] = []
    warnings: list[str] = []
    for feature in features:
        feature_id = str(feature.get("id", "?"))
        status = feature.get("status")
        if status == "blocked" and not feature.get("blocked_reason"):
            failures.append(f"feature {feature_id} failed: blocked status requires blocked_reason")
        elif status != "blocked" and "blocked_reason" in feature:
            failures.append(f"feature {feature_id} failed: blocked_reason requires blocked status")
        if status == "deferred" and not feature.get("deferred_until"):
            failures.append(f"feature {feature_id} failed: deferred status requires deferred_until")
        elif status != "deferred" and "deferred_until" in feature:
            failures.append(f"feature {feature_id} failed: deferred_until requires deferred status")
        attempts = feature.get("attempts", [])
        if isinstance(attempts, list):
            count = sum(
                1 for attempt in attempts
                if isinstance(attempt, dict) and attempt.get("session") is None
            )
            if count:
                warnings.append(f"feature {feature_id}: {count} attempt(s) have session=null")
    details = failures + (["WARN: " + "; ".join(warnings)] if warnings else [])
    return CheckRecord("status_field_consistency", not failures, "; ".join(details))


# ---------- arg parsing ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff_check.py",
        description="Pre-flight check before a Coding Agent session starts. "
                    "Refuses to proceed if any of the 10 checks fail.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Reserved for future per-check skip flags. Default is "
             "already strict (all 10 checks must pass).",
    )
    parser.add_argument(
        "--max-fillins",
        type=int,
        default=DEFAULT_MAX_FILLINS,
        metavar="N",
        help=f"Maximum allowed [fill-in] placeholders in CODE_STYLE.md "
             f"(default: {DEFAULT_MAX_FILLINS}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON-per-check output (one object per line) plus a "
             "final summary line.",
    )
    return parser


# ---------- orchestration ----------

def _run_all_checks(max_fillins: int) -> list[CheckRecord]:
    """Run the 10 checks in spec order; later checks can short-circuit
    on earlier failures but still produce a CheckRecord so the agent
    sees the full picture rather than stopping at the first error.
    """
    records: list[CheckRecord] = []

    r1 = check_json_parse()
    records.append(r1)

    r2 = check_metadata_drift(records)
    records.append(r2)

    r3 = check_session_0_exists()
    records.append(r3)

    # Checks 4-6 walk the depends_on graph. Each one needs the
    # features list. They tolerate json_parse failure (they report
    # "skipped") so the user still sees the full report.
    r4 = check_depends_on_acyclic(records)
    records.append(r4)

    r5 = check_depends_on_resolves(records)
    records.append(r5)

    r6 = check_passing_deps_pass(records)
    records.append(r6)

    r7 = check_code_style_fillins(max_fillins)
    records.append(r7)

    r8 = check_8_git_readiness()
    records.append(r8)

    r9 = check_9_feature_schema(records)
    records.append(r9)

    r10 = check_10_status_field_consistency(records)
    records.append(r10)

    return records


def _emit(records: list[CheckRecord], as_json: bool) -> int:
    """Print records and return the exit code."""
    if as_json:
        for rec in records:
            print(json.dumps(rec.json_obj(), ensure_ascii=False))
        passed = sum(1 for r in records if r.ok)
        failed = len(records) - passed
        overall = "PASS" if failed == 0 else "FAIL"
        print(json.dumps(
            {"summary": {"passed": passed, "failed": failed, "overall": overall}},
            ensure_ascii=False,
        ))
    else:
        for rec in records:
            print(rec.human_line())
        passed = sum(1 for r in records if r.ok)
        failed = len(records) - passed
        print()
        if failed == 0:
            print(f"PASS: {passed}/{len(records)} checks passed.")
        else:
            print(f"FAIL: {failed}/{len(records)} checks failed ({passed} passed).")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    # --strict is reserved for future per-check skip flags; today it is
    # accepted but a no-op (default behavior is already strict).
    # We keep the attribute read so a linter / future check that the
    # flag is recognized doesn't get tripped up.
    _ = args.strict

    if args.max_fillins < 0:
        fail(f"--max-fillins must be >= 0, got {args.max_fillins}")

    records = _run_all_checks(max_fillins=args.max_fillins)
    return _emit(records, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())