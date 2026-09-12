#!/usr/bin/env python3
"""session_end.py — atomic end-of-session bookkeeping.

This script is the mandatory end-of-session ritual for any coding agent
working in this long-running agent harness. It collapses the seven
things a coding session must do before terminating into one command so
no step gets skipped when an agent runs out of context or time.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

from _constants import SESSION_STATUSES, SESSION_STATUS_LABELS
from _atomic_io import atomic_write_json, atomic_write_text
from _file_lock import file_lock

# Project paths — script lives in tools/, assume caller ran from project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
# `feature_list.json` for the active project lives at the repo root,
# not inside HARNESS/. The single source of truth for that path is
# `heddle_common.feature_list_io.DEFAULT_PATH` (per feat-008 / T-014);
# import it so this script and `tools/feature_list.py` stay in sync.
try:
    from _feature_io import FEATURE_LIST_PATH as _LIB_FEATURE_LIST_PATH  # noqa: E402
except ImportError:
    # Fallback: derive the path the same way `feature_list_io` does.
    _REPO_ROOT = Path(__file__).resolve().parents[3]
    _LIB_FEATURE_LIST_PATH = _REPO_ROOT / "feature_list.json"
FEATURE_LIST_PATH = _LIB_FEATURE_LIST_PATH
DEFAULT_PROGRESS_PATH = PROJECT_ROOT / "current_progress.txt"
PROGRESS_PATH = DEFAULT_PROGRESS_PATH
TEMPLATE_PATH = PROJECT_ROOT / "docs" / "templates" / "progress_session_block.md"
INIT_SH = PROJECT_ROOT / "init.sh"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
PYTEST_INI = PROJECT_ROOT / "pytest.ini"
TESTS_DIR = PROJECT_ROOT / "tests"
PACKAGE_JSON = PROJECT_ROOT / "package.json"

NOTES_HARD_CAP = 500
SESSION_NUMBER_HARD_CAP = 10_000
SETEXT_UNDERLINE_MIN_LEN = 3


def _positive_session_number(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--session-number must be an integer, got {raw!r}."
        )
    if value < 1 or value > SESSION_NUMBER_HARD_CAP:
        raise argparse.ArgumentTypeError(
            f"--session-number must be between 1 and "
            f"{SESSION_NUMBER_HARD_CAP}, got {raw}."
        )
    return value


def _bounded_notes(raw: str) -> str:
    if len(raw) > NOTES_HARD_CAP:
        raise argparse.ArgumentTypeError(
            f"--notes is {len(raw)} chars; hard cap is {NOTES_HARD_CAP}."
        )
    return raw


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd is not None else None,
    )


def _step_header(label: str) -> None:
    print(label)
    sys.stdout.flush()



def step_precheck(
    steps_done: list[str],
    dry_run: bool,
    progress_path: Path,
) -> None:
    _step_header("[step 1/7] pre-check")

    r = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=PROJECT_ROOT)
    if r.returncode != 0 or r.stdout.strip() != "true":
        msg = (
            "not inside a git working tree. "
            "Run from the project root, or initialize git first."
        )
        _abort(steps_done, "pre-check:git", msg)
    print("  - git working tree: ok")

    if not FEATURE_LIST_PATH.exists():
        _abort(steps_done, "pre-check:files",
               f"{FEATURE_LIST_PATH.name} not found at project root.")
    if not progress_path.exists():
        hint = (
            f"{progress_path.name} not found at project root. "
            f"Pass --progress-file PATH to point at an existing progress file."
        )
        _abort(steps_done, "pre-check:files", hint)
    print(f"  - feature_list.json + {progress_path.name} present")

    r = run([sys.executable, str(TOOLS_DIR / "feature_list.py"), "status"],
            cwd=PROJECT_ROOT)
    if r.returncode != 0:
        _abort(
            steps_done,
            "pre-check:parse",
            f"`feature_list.py status` failed (exit {r.returncode}). "
            "feature_list.json is missing or malformed.\n"
            f"stderr: {r.stderr.strip()}",
        )
    print("  - feature_list.json parses: ok")


def step_refresh_metadata(steps_done: list[str], dry_run: bool, label: str) -> None:
    _step_header(f"[{label}] refresh metadata")
    if dry_run:
        print("  - (dry-run) would run: "
              "python tools/feature_list.py update-metadata")
        return
    r = run([sys.executable, str(TOOLS_DIR / "feature_list.py"), "update-metadata"],
            cwd=PROJECT_ROOT)
    if r.returncode != 0:
        _abort(
            steps_done,
            f"{label}:refresh-metadata",
            f"feature_list.py update-metadata failed (exit {r.returncode}).\n"
            f"stderr: {r.stderr.strip()}",
        )
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "ok"
    print(f"  - {last}")



def _detect_test_targets() -> list[tuple[str, list[str]]]:
    """Return the (label, argv) pairs for tests we should run, in order.

    Order, per spec: init.sh -> pytest -> npm test. Each entry is
    included only when its presence is confirmed.

    P2-11 (Windows compat): probe `bash` via shutil.which. On Windows
    or stripped-down POSIX systems where bash is not on PATH, the
    init.sh smoke test is skipped with a warning rather than failing.
    P2-12 (Windows compat): probe `pytest` via
    importlib.util.find_spec("pytest"). A bare `tests/` directory
    alone is no longer enough — if pytest is not actually importable,
    skip with a warning (this also covers systems where the tests/
    dir exists but pytest itself is uninstalled or lives in a venv
    not currently active).
    """
    targets: list[tuple[str, list[str]]] = []

    if INIT_SH.exists():
        try:
            executable = INIT_SH.stat().st_mode & 0o111
        except OSError:
            executable = 0
        if executable:
            if shutil.which("bash") is None:
                # P2-11: bash is missing on this PATH (typical on
                # stock Windows). Treat as a skip, not a failure.
                print(
                    "warning: bash not found on PATH; skipping init.sh "
                    "smoke test (--skip-tests equivalent for this step)."
                )
            else:
                targets.append(("init.sh", ["bash", str(INIT_SH)]))
        else:
            print(f"  - skipping {INIT_SH.name}: not executable")

    # P2-12: probe by trying to find the `pytest` module spec.
    if importlib.util.find_spec("pytest") is None:
        print("warning: pytest not importable; skipping pytest target.")
    else:
        targets.append(("pytest", [sys.executable, "-m", "pytest", "-x", "-q"]))

    if PACKAGE_JSON.exists():
        targets.append(("npm test", ["npm", "test", "--silent"]))

    return targets


def _tail(text: str, limit: int = 800) -> str:
    text = text.rstrip()
    return text[-limit:] if len(text) > limit else text


def step_run_tests(steps_done: list[str], dry_run: bool, skip_tests: bool) -> None:
    _step_header("[step 3/7] tests")
    if skip_tests:
        print("  - skipped via --skip-tests")
        return
    targets = _detect_test_targets()
    if not targets:
        print("  - no test runner detected (no init.sh, pyproject.toml, "
              "pytest.ini, tests/, or package.json); nothing to run")
        return
    if dry_run:
        for label, cmd in targets:
            pretty = " ".join(shlex.quote(c) for c in cmd)
            print(f"  - (dry-run) would run: {pretty}")
        return
    for label, cmd in targets:
        pretty = " ".join(shlex.quote(c) for c in cmd)
        print(f"  - running {label}: {pretty}")
        r = run(cmd, cwd=PROJECT_ROOT)
        if r.returncode != 0:
            hint = (
                "Re-run the failed command manually to inspect output, or"
                "    python tools/session_end.py --skip-tests ..."
                "to record this session without re-running the suite."
            )
            _abort(
                steps_done,
                f"tests:{label}",
                f"{label} exited with code {r.returncode}."
                f"stdout-tail: {_tail(r.stdout)}"
                f"stderr-tail: {_tail(r.stderr)}\n{hint}",
            )
        print(f"  - {label}: ok")



def _metadata_snapshot() -> dict[str, Any]:
    with FEATURE_LIST_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("metadata", {})
    return {
        "passing": meta.get("passing", 0),
        "total_features": meta.get("total_features", 0),
    }


def _build_session_block(
    *,
    session_number: int,
    feature_id: str,
    commit_sha: str,
    status: str,
    features_passing: list[str],
    notes: str,
    today: str,
    passing: int,
    total: int,
) -> str:
    fp_list = ", ".join(features_passing) if features_passing else "(none)"
    fp_total = f"({passing} / {total} total)"
    if status not in SESSION_STATUSES:
        raise SystemExit(
            f"invalid session status: {status!r}; expected one of {SESSION_STATUSES}"
        )
    label = SESSION_STATUS_LABELS[status]
    worked_on = f"{feature_id} @ {commit_sha}"
    notes_clean = notes.strip() or "(none)"
    block = (
        f"### SESSION {session_number} — {today}\n"
        f"\n\n"
        f"**Worked on:** {worked_on}"
        f"\n\n"
        f"**Status:** {label}"
        f"\n\n"
        f"**Features now passing:** {fp_list} {fp_total}"
        f"\n\n"
        f"**Notes:** {notes_clean}"
        f"\n\n"
        f"---"
    )
    return block


def step_append_progress(
    steps_done: list[str],
    dry_run: bool,
    *,
    session_number: int,
    feature_id: str,
    commit_sha: str,
    status: str,
    features_passing: list[str],
    notes: str,
    progress_path: Path,
) -> str:
    _step_header("[step 4/7] append progress")

    meta = _metadata_snapshot()
    passing, total = meta["passing"], meta["total_features"]

    today = date.today().isoformat()
    block = _build_session_block(
        session_number=session_number,
        feature_id=feature_id,
        commit_sha=commit_sha,
        status=status,
        features_passing=features_passing,
        notes=notes,
        today=today,
        passing=passing,
        total=total,
    )

    needs_leading_nl = False
    if progress_path.exists():
        existing = progress_path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            needs_leading_nl = True
    else:
        existing = ""

    print("  --- SESSION block to append ---")
    print(block.rstrip())
    print("  --- end block ---")

    if dry_run:
        print(f"  - (dry-run) would append to {progress_path.name}; "
              "SUMMARY block refresh runs separately in step 4b.")
        return block

    prefix = "\n" if needs_leading_nl else ""
    with progress_path.open("a", encoding="utf-8") as f:
        f.write(prefix)
        f.write(block)
    print(f"  - appended SESSION {session_number} to {progress_path.name}")
    return block



SUMMARY_FIELD_NAMES = (
    "Total features",
    "Passing",
    "Failing",
    "In progress",
    "Blocked",
    "Deferred",
    "Last updated",
    "Total Sessions",
    "Features now passing",
)


def _parse_feature_status(stdout: str) -> dict[str, str]:
    key_to_label = {
        "total_features": "Total features",
        "passing": "Passing",
        "failing": "Failing",
        "in_progress": "In progress",
        "blocked": "Blocked",
        "deferred": "Deferred",
        "last_updated": "Last updated",
    }
    parsed: dict[str, str] = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        label = key_to_label.get(key)
        if label and value:
            parsed[label] = value
    return parsed


def _find_summary_section(text: str) -> tuple[int, int] | None:
    # All regex flags are hoisted into the flags= parameter; inline
    # flags like (?im) inside an alternation branch are rejected by
    # Python 3.12+ as "global flags not at the start of the
    # expression".
    summary_header_re = re.compile(
        r"(?:"
        r"^##\s*SUMMARY\s*$"
        r"|"
        r"^SUMMARY[ 	]*\n^(?:={3,}|-{3,})[ 	]*$"
        r"|"
        r"^[^\n]+^(?:={3,}|-{3,})[ 	]*$"
        r")",
        re.MULTILINE | re.IGNORECASE,
    )
    matches = list(summary_header_re.finditer(text))
    if not matches:
        return None
    header_match = matches[-1]
    start = header_match.start()
    next_header_re = re.compile(
        r"(?:"
        r"^##(?!#)\s*\S"
        r"|"
        r"^[^\n]+^(?:={3,}|-{3,})[ 	]*$"
        r")",
        re.MULTILINE | re.IGNORECASE,
    )
    end_match = next_header_re.search(text, header_match.end())
    end = end_match.start() if end_match else len(text)
    return start, end



def _refresh_summary_block(progress_path: Path) -> bool:
    r = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "feature_list.py"), "status"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"`feature_list.py status` failed (exit {r.returncode}): "
            f"{r.stderr.strip()}"
        )
    new_values = _parse_feature_status(r.stdout)

    text = progress_path.read_text(encoding="utf-8")
    section = _find_summary_section(text)
    if section is None:
        print(
            f"SUMMARY block not found in {progress_path.name}; skipping refresh"
        )
        return False

    start, end = section
    head = text[:start]
    section_text = text[start:end]
    tail = text[end:]

    label_alt = "|".join(re.escape(name) for name in SUMMARY_FIELD_NAMES)
    field_re = re.compile(
        r"^(\s*-?\s*)(?:\*\*)?(" + label_alt + r")(?:\*\*)?:\s*.*$",
        re.MULTILINE,
    )

    changed = False
    new_section_parts: list[str] = []
    last_end = 0
    for m in field_re.finditer(section_text):
        prefix, label = m.group(1), m.group(2)
        if label not in new_values:
            continue
        if m.start() > last_end:
            new_section_parts.append(section_text[last_end:m.start()])
        replacement = f"{prefix}{label}: {new_values[label]}"
        if m.group(0) != replacement:
            changed = True
        new_section_parts.append(replacement)
        last_end = m.end()
    new_section = "".join(new_section_parts) + section_text[last_end:]

    if not changed:
        return False

    new_text = head + new_section + tail
    if not new_text.endswith("\n"):
        new_text += "\n"
    atomic_write_text(progress_path, new_text, encoding="utf-8")
    return True


def step_refresh_summary(
    steps_done: list[str],
    dry_run: bool,
    progress_path: Path,
) -> str:
    _step_header("[step 4b/7] refresh SUMMARY block")
    if dry_run:
        try:
            r = subprocess.run(
                [sys.executable, str(TOOLS_DIR / "feature_list.py"), "status"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if r.returncode != 0:
                print(f"  - (dry-run) feature_list.py status would fail: "
                      f"{r.stderr.strip()}")
                return "no"
            new_values = _parse_feature_status(r.stdout)
            text = progress_path.read_text(encoding="utf-8") if progress_path.exists() else ""
            section = _find_summary_section(text)
            if section is None:
                print(
                    f"  - (dry-run) SUMMARY block not found in "
                    f"{progress_path.name}; skipping refresh"
                )
                return "not-found"
            start, end = section
            section_text = text[start:end]
            label_alt = "|".join(re.escape(n) for n in SUMMARY_FIELD_NAMES)
            preview_re = re.compile(
                r"^.*?(" + label_alt + r").*?$",
                re.MULTILINE,
            )
            preview_match = preview_re.search(section_text)
            if preview_match:
                first_line = preview_match.group(0).strip()
                print(f"  - (dry-run) will refresh SUMMARY block "
                      f"({len(new_values)} fields): first line '{first_line}'")
            else:
                print(f"  - (dry-run) will refresh SUMMARY block "
                      f"({len(new_values)} fields)")
            return "yes"
        except Exception as e:
            print(f"  - (dry-run) refresh probe failed: {e}",
                  file=sys.stderr)
            return "no"

    try:
        did_refresh = _refresh_summary_block(progress_path)
    except Exception as e:
        _abort(
            steps_done,
            "summary-refresh",
            f"unexpected error refreshing SUMMARY block: {e}"
            f"Inspect {progress_path.name} manually; the SESSION "
            f"block has already been appended but the SUMMARY block "
            f"may be stale.",
        )
    if did_refresh:
        print("  - SUMMARY block refreshed")
        return "yes"
    try:
        text = progress_path.read_text(encoding="utf-8")
        section = _find_summary_section(text)
    except OSError:
        section = None
    if section is None:
        return "not-found"
    print("  - SUMMARY block already up-to-date; no rewrite needed")
    return "no"



def step_backfill_attempts(
    steps_done: list[str],
    dry_run: bool,
    *,
    session_number: int,
) -> str:
    _step_header("[step 4c/7] backfill attempts[].session")
    if not FEATURE_LIST_PATH.exists():
        print(f"  - {FEATURE_LIST_PATH.name} not found; nothing to backfill")
        return "no-op"

    try:
        with FEATURE_LIST_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _abort(
            steps_done,
            "backfill:load",
            f"cannot read {FEATURE_LIST_PATH.name}: {e}",
        )

    features = data.get("features", [])
    changed_entries: list[tuple[str, int]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        attempts = feature.get("attempts")
        if not isinstance(attempts, list):
            continue
        for idx, entry in enumerate(attempts):
            if not isinstance(entry, dict):
                continue
            if entry.get("session") is None:
                changed_entries.append((str(feature.get("id", "?")), idx))
                if not dry_run:
                    entry["session"] = session_number

    if not changed_entries:
        print("  - no attempts[] entries with session=null; nothing to backfill")
        return "no-op"

    per_feature: dict[str, int] = {}
    for fid, _idx in changed_entries:
        per_feature[fid] = per_feature.get(fid, 0) + 1
    print(f"  - would backfill {len(changed_entries)} attempt(s) across "
          f"{len(per_feature)} feature(s) with session={session_number}:")
    for fid in sorted(per_feature):
        print(f"      {fid}: {per_feature[fid]} attempt(s)")

    if dry_run:
        print("  - (dry-run) no write performed")
        return f"backfilled={len(changed_entries)} (dry-run)"

    try:
        with file_lock(FEATURE_LIST_PATH):
            atomic_write_json(FEATURE_LIST_PATH, data, indent=2, ensure_ascii=False)
    except TimeoutError:
        _abort(
            steps_done,
            "backfill:lock",
            f"could not acquire {FEATURE_LIST_PATH.name} lock within 5s; "
            "another mutation is in progress",
        )
    except OSError as e:
        _abort(
            steps_done,
            "backfill:write",
            f"cannot write {FEATURE_LIST_PATH.name}: {e}",
        )
    print(f"  - backfilled {len(changed_entries)} attempt(s) "
          f"with session={session_number}")
    return f"backfilled={len(changed_entries)}"



def step_commit_wip(
    steps_done: list[str],
    dry_run: bool,
    *,
    session_number: int,
    feature_id: str,
    progress_path: Path,
) -> str | None:
    _step_header("[step 5/7] commit WIP")
    # P2-13: use `-z` (NUL-separated) output. This is the
    # only porcelain format that round-trips filenames containing
    # spaces, quotes, or non-ASCII (e.g. Chinese filenames) without
    # ambiguity. The legacy `line[3:].strip().strip('"')`
    # approach silently mis-parsed `a file with spaces.txt` because
    # git normal --porcelain quotes only the path and only when
    # strictly necessary, and the per-character slicing assumed
    # a fixed-width status prefix.
    r = run(["git", "status", "--porcelain", "-z"], cwd=PROJECT_ROOT)
    if r.returncode != 0:
        _abort(
            steps_done,
            "commit:status",
            f"`git status` failed (exit {r.returncode}).\nstderr: {r.stderr.strip()}",
        )

    # P2-13: split the `-z` payload on NUL. Each entry has the
    # shape " XY path\x00" where XY or X Y are the 2-char status
    # codes (X = staged-index, Y = worktree-index), followed by
    # a space and the path. The trailing NUL after the last entry
    # produces an empty fragment after split; we filter it out.
    #
    # Note: subprocess.run with text=True + encoding="utf-8"
    # decodes raw NUL bytes (\x00) as the literal NUL character
    # U+0000, so splitting on the one-character string "\x00"
    # works on every platform (Linux, macOS, Windows). errors=
    # "replace" does NOT replace NUL -- Python preserves U+0000
    # through the codec.
    entries = r.stdout.split("\x00")
    targets = {FEATURE_LIST_PATH.name, progress_path.name}
    relevant: list[str] = []
    for entry in entries:
        if not entry:
            continue
        # Strip the status prefix (e.g. " M ", "M ", "A ", "MM",
        # "R  old -> new"). For renames, git `-z` emits the
        # rename as "R  old	new\x00" plus a SEPARATE bare
        # "new\x00" entry without the prefix; either way,
        # basename-matching the path portion handles both forms.
        m = re.match(r"^(?:[ MADRCU?!]{2})\s?(.*)", entry)
        if not m:
            continue
        path_str = m.group(1).strip()
        if Path(path_str).name in targets:
            relevant.append(entry)
    if not relevant:
        print(f"  - no changes to {FEATURE_LIST_PATH.name} / "
              f"{progress_path.name}; skipping commit")
        return None

    if dry_run:
        print("  - (dry-run) would run: "
              f"git add -- {FEATURE_LIST_PATH.name} {progress_path.name}")
        print("  - (dry-run) would commit with message:")
        print(f"      WIP: session {session_number} - {feature_id} partial")
        return None

    r = run(["git", "add", "--",
             FEATURE_LIST_PATH.name, progress_path.name], cwd=PROJECT_ROOT)
    if r.returncode != 0:
        _abort(
            steps_done,
            "commit:add",
            f"`git add` failed (exit {r.returncode}).\nstderr: {r.stderr.strip()}",
        )

    msg = f"WIP: session {session_number} - {feature_id} partial"
    r = run(["git", "commit", "-m", msg], cwd=PROJECT_ROOT)
    if r.returncode != 0:
        _abort(
            steps_done,
            "commit:commit",
            f"`git commit` failed (exit {r.returncode})."
            f"stderr: {r.stderr.strip()}",
        )
    r = run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT)
    sha = r.stdout.strip() if r.returncode == 0 else None
    print(f"  - committed: {sha or '(sha unavailable)'}")
    return sha



def step_print_summary(
    block_text: str,
    commit_sha: str | None,
    summary_status: str = "not-found",
    backfill_status: str = "no-op",
) -> None:
    _step_header("[step 7/7] summary")
    print("  SESSION block:")
    for line in block_text.rstrip().splitlines():
        print(f"    {line}")
    print(f"  commit SHA: {commit_sha or '(no commit)'}")
    print(f"  SUMMARY refreshed: {summary_status}")
    print(f"  attempts[].session backfill: {backfill_status}")
    print("  metadata snapshot:")
    r = run([sys.executable, str(TOOLS_DIR / "feature_list.py"), "status"],
            cwd=PROJECT_ROOT)
    if r.returncode == 0:
        for line in r.stdout.rstrip().splitlines():
            print(f"    {line}")
    else:
        print(f"    (could not read metadata: exit {r.returncode})")


def _abort(steps_done: list[str], where: str, detail: str) -> None:
    print("", file=sys.stderr)
    print("session_end.py: aborted", file=sys.stderr)
    print(f"  completed steps: {steps_done or '(none)'}", file=sys.stderr)
    print(f"  failed at:       {where}", file=sys.stderr)
    print("  detail:", file=sys.stderr)
    for line in detail.splitlines():
        print(f"    {line}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  recovery hints:", file=sys.stderr)
    print(f"    git status                       # see uncommitted state",
          file=sys.stderr)
    print(f"    git log --oneline -5             # see recent commits",
          file=sys.stderr)
    print(f"    tail -n 50 {PROGRESS_PATH.name}  # see last session block",
          file=sys.stderr)
    sys.exit(1)


def _validate_feature_id(feature_id: str) -> None:
    try:
        with FEATURE_LIST_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(f"cannot read {FEATURE_LIST_PATH.name}: {e}")
    ids = {f.get("id") for f in data.get("features", [])}
    if feature_id not in ids:
        fail(f"feature id {feature_id!r} not found in {FEATURE_LIST_PATH.name}.")


def _resolve_commit_sha(explicit: str | None) -> str:
    if explicit:
        return explicit
    r = run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT)
    if r.returncode != 0 or not r.stdout.strip():
        fail(
            "could not determine commit SHA: --commit-sha not given and "
            "`git rev-parse HEAD` failed. Pass --commit-sha explicitly."
        )
    return r.stdout.strip()



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session_end.py",
        description="Atomic end-of-session bookkeeping for the long-running "
                    "agent harness. After appending the SESSION block "
                    "(step 4), refreshes the trailing ## SUMMARY block "
                    "in the configured progress file (default "
                    "`current_progress.txt`; step 4b) using live "
                    "feature_list.py status output; a missing SUMMARY "
                    "section is left untouched.",
    )
    parser.add_argument(
        "--session-number",
        type=_positive_session_number,
        default=None,
        help="1-indexed session counter for this Coding Agent session "
             "(>= 1). Initializer session is SESSION 0 and is written by hand.",
    )
    parser.add_argument(
        "--feature-id",
        default="",
        help="The feature_id this session worked on. Must exist in "
             "feature_list.json.",
    )
    parser.add_argument(
        "--commit-sha",
        default=None,
        help="Commit SHA that ended the session. If omitted, uses "
             "`git rev-parse HEAD`.",
    )
    parser.add_argument(
        "--status",
        choices=list(SESSION_STATUSES),
        default=None,
        help="One of: " + ", ".join(SESSION_STATUSES),
    )
    parser.add_argument(
        "--features-passing",
        default="",
        help="Comma-separated feature_ids that flipped to passing THIS "
             "session. Empty = none.",
    )
    parser.add_argument(
        "--notes",
        type=_bounded_notes,
        default="",
        help=f"Free-form notes for the next session. Hard cap "
             f"{NOTES_HARD_CAP} chars; \"(none)\" if empty.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every step and the SESSION block that would be "
             "written, but make no writes and no commits.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip step 3 (tests).",
    )
    parser.add_argument(
        "--from-template",
        action="store_true",
        help="Print the SESSION block template (head 40 lines) at startup "
             "and exit. Does not require the other session fields.",
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        default=DEFAULT_PROGRESS_PATH,
        metavar="PATH",
        help=f"Override the progress file to append/refresh (default: "
             f"{DEFAULT_PROGRESS_PATH.name}).",
    )
    return parser


def _print_template() -> None:
    if not TEMPLATE_PATH.exists():
        print(f"warning: template not found at {TEMPLATE_PATH}",
              file=sys.stderr)
        return
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    head = lines[:40]
    for line in head:
        print(line)
    if len(lines) > 40:
        print(f"... ({len(lines) - 40} more lines omitted)")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.from_template:
        _print_template()
        return 0

    missing: list[str] = []
    if args.session_number is None:
        missing.append("--session-number")
    if not args.feature_id:
        missing.append("--feature-id")
    if not args.status:
        missing.append("--status")
    if missing:
        fail(f"the following arguments are required: {' '.join(missing)}")

    progress_path: Path = args.progress_file
    if not progress_path.is_absolute():
        progress_path = PROJECT_ROOT / progress_path

    _validate_feature_id(args.feature_id)
    features_passing = [
        s.strip() for s in args.features_passing.split(",") if s.strip()
    ]

    commit_sha = _resolve_commit_sha(args.commit_sha)

    steps_done: list[str] = []

    step_precheck(steps_done, dry_run=args.dry_run, progress_path=progress_path)
    steps_done.append("pre-check")

    step_refresh_metadata(steps_done, dry_run=args.dry_run,
                          label="step 2/7")
    steps_done.append("refresh-metadata")

    step_run_tests(steps_done, dry_run=args.dry_run, skip_tests=args.skip_tests)
    steps_done.append("tests")

    block_text = step_append_progress(
        steps_done, args.dry_run,
        session_number=args.session_number,
        feature_id=args.feature_id,
        commit_sha=commit_sha,
        status=args.status,
        features_passing=features_passing,
        notes=args.notes,
        progress_path=progress_path,
    )
    steps_done.append("append-progress")

    summary_status = step_refresh_summary(steps_done, args.dry_run,
                                          progress_path=progress_path)
    steps_done.append("summary-refresh")

    backfill_status = step_backfill_attempts(
        steps_done, args.dry_run,
        session_number=args.session_number,
    )
    steps_done.append("backfill-attempts")

    new_sha = step_commit_wip(
        steps_done, args.dry_run,
        session_number=args.session_number,
        feature_id=args.feature_id,
        progress_path=progress_path,
    )
    steps_done.append("commit")

    step_refresh_metadata(steps_done, dry_run=args.dry_run,
                          label="step 6/7")
    steps_done.append("refresh-metadata-final")

    step_print_summary(block_text, new_sha, summary_status,
                       backfill_status=backfill_status)
    steps_done.append("summary")

    print("\nsession_end.py: done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
