#!/usr/bin/env python3
"""progress_rotate.py — archive old SESSION blocks out of current_progress.txt.

When `current_progress.txt` accumulates more SESSION blocks than the
project cares to keep in its hot file, this script moves the oldest
excess blocks into a per-session archive under `sessions/older/<N>.md`,
where `<N>` is the session number of the FIRST (oldest) block moved.

Why this exists
---------------
A long-running project can rack up hundreds of SESSION blocks. Reading
the whole `current_progress.txt` on every session start gets expensive
(token cost + parse time) and makes the SESSION N the next agent wants
to look at hard to find amid stale history. Rotation keeps the active
file bounded (default: keep 20 most recent SESSION blocks in place) and
moves older blocks to dated, greppable archive files.

The Initializer's SESSION 0 block is intentionally NEVER rotated — it's
the project seed, lives at the top of the file, and is required by
`tools/handoff_check.py` (check 3, `session_0_exists`). Rotation only
moves blocks that match the canonical `### SESSION <N> — <ISO date>`
shape produced by `tools/session_end.py`; SESSION 0 in the Initializer
template uses the bare `SESSION 0 - Initializer Agent` shape and is
therefore preserved in place.

Usage
-----
    python tools/progress_rotate.py [--keep N] [--dry-run]
    python tools/progress_rotate.py --keep 20
    python tools/progress_rotate.py --dry-run --keep 20

Flags
-----
    --keep N      How many most-recent SESSION blocks to keep in
                  current_progress.txt. Default: 20. Must be >= 1.
    --dry-run     Print what would be moved (block titles + target
                  archive filename) but make no writes.
    --progress-file PATH
                  Override the default progress file (default:
                  `current_progress.txt`). Lets ops rotate older
                  project snapshots from other paths.
    --older-dir PATH
                  Override the archive directory (default:
                  `sessions/older`). Created if missing.

Exit codes
----------
    0   success (or no-op when count <= keep)
    1   invalid arguments, unreadable input file, or write failure

Design notes
------------
- stdlib only — the harness explicitly avoids third-party deps.
- Block detection: regex `^### SESSION N — YYYY-MM-DD`. We capture the
  session number and ISO date. The Initializer's bare
  `SESSION 0 - Initializer Agent` line intentionally does NOT match
  this regex (no `###` prefix, `-` instead of `—`), so it stays put.
  This matches the canonical shape appended by `session_end.py` but
  does NOT match the Initializer's bare `SESSION 0 - Initializer Agent`
  line (no `###` prefix, different separator). So SESSION 0 stays put
  automatically.
- Block boundaries: a SESSION block is everything from its `### SESSION`
  heading up to (but not including) the next `### SESSION` heading, or
  EOF. The trailing `## SUMMARY` block (if present) is preserved at
  the end of current_progress.txt — rotation only moves the SESSION
  blocks, never the SUMMARY.
- Archive filename: `sessions/older/<oldest_moved_N>.md`. If a target
  file already exists, the new content is appended to it (rotations
  on different days may dump into the same archive when the oldest
  block's session number is the same across rotations). A short
  `<!-- archived at <ISO date> -->` marker is added as the first line
  of each append so successive dumps are visually separable.
- Single source of truth for the SESSION block shape is
  `docs/templates/progress_session_block.md`; we don't duplicate the
  field set here.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------- paths ----------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROGRESS_PATH = PROJECT_ROOT / "current_progress.txt"
DEFAULT_OLDER_DIR = PROJECT_ROOT / "sessions" / "older"

# Import the canonical SESSION heading regex from the shared _constants
# module. The regex lives in `_constants` so tools/progress_rotate.py,
# tools/next_session_number.py, and (in spirit) tools/session_end.py stay
# in sync. See _constants.SESSION_HEADING_RE for the rationale on why the
# Initializer's bare "SESSION 0 - Initializer Agent" line is excluded.
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from _constants import SESSION_HEADING_RE  # noqa: E402
from _atomic_io import atomic_write_text  # noqa: E402

# ---------- constants ----------

DEFAULT_KEEP = 20

# Any H2 (## ...) header is treated as the start of a section that
# rotation must not cross into. The trailing `## SUMMARY` block in
# particular must stay at the bottom of current_progress.txt.
H2_HEADER_RE = re.compile(r"^##\s+", re.MULTILINE)


# ---------- error helpers ----------

def fail(msg: str) -> None:
    """Print an error to stderr and exit with a non-zero status."""
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------- core helpers ----------

def _find_session_blocks(text: str) -> list[tuple[int, int, int, str]]:
    """Locate every canonical SESSION block in `text`.

    Returns a list of (session_number, start, end, heading_line) tuples,
    ordered by appearance (i.e. chronological, since blocks are
    appended in order). `start` is the byte offset of the heading line
    itself; `end` is the byte offset where the next `### SESSION`, the
    next `## ` H2 (e.g. a trailing `## SUMMARY`), or EOF begins — i.e.
    `text[start:end]` contains the full block including its trailing
    `\n`-separated `---` rule.

    Blocks that don't match the canonical `### SESSION N — YYYY-MM-DD`
    shape (e.g. the Initializer's bare `SESSION 0 - Initializer Agent`)
    are ignored on purpose — see module docstring for why.

    Complexity is O(n * m) in the worst case (n SESSION blocks, each
    triggering a fresh H2 search), but constants are tiny for typical
    files because each `H2_HEADER_RE.search` stops at the first H2
    after `start` — usually close.
    """
    matches = list(SESSION_HEADING_RE.finditer(text))
    if not matches:
        return []

    blocks: list[tuple[int, int, int, str]] = []
    for i, m in enumerate(matches):
        session_number = int(m.group(1))
        start = m.start()
        # Next SESSION block starts where its heading begins; if there
        # is no next SESSION, fall back to the next H2 (SUMMARY), or EOF.
        if i + 1 < len(matches):
            next_session_start = matches[i + 1].start()
        else:
            next_session_start = len(text)
        next_h2 = H2_HEADER_RE.search(text, start)
        next_h2_start = next_h2.start() if next_h2 else None
        end = min(next_session_start, next_h2_start or len(text))
        blocks.append((session_number, start, end, m.group(0)))
    return blocks


def _split_prelude(text: str, blocks: list[tuple[int, int, int, str]]) -> str:
    """Return the slice of `text` BEFORE the first SESSION block.

    This prelude (project header + Initializer SESSION 0) stays in
    current_progress.txt regardless of rotation. If there are no SESSION
    blocks at all, the whole text is the prelude.
    """
    if not blocks:
        return text
    first_start = blocks[0][1]
    return text[:first_start]


def _split_summary(text: str, blocks: list[tuple[int, int, int, str]]) -> str:
    """Return the trailing slice of `text` from the first H2 to EOF.

    This is the trailing `## SUMMARY` block (and any text after the last
    SESSION block that lives inside the H2 region). It stays in
    current_progress.txt. If no H2 follows the SESSION blocks, this is
    just an empty string.
    """
    # The earliest H2 strictly after the first SESSION block.
    scan = H2_HEADER_RE.search(text, blocks[0][1])
    return text[scan.start():] if scan else ""


def cmd_rotate(args: argparse.Namespace) -> None:
    """Run the rotation per the parsed CLI args."""
    progress_path: Path = args.progress_file
    older_dir: Path = args.older_dir
    keep: int = args.keep
    dry_run: bool = args.dry_run

    if keep < 1:
        fail(f"--keep must be >= 1, got {keep}")

    if not progress_path.exists():
        fail(
            f"{progress_path} not found. The Initializer is expected to "
            f"create {progress_path.name}; run `tools/handoff_check.py` "
            f"to diagnose."
        )
    try:
        text = progress_path.read_text(encoding="utf-8")
    except OSError as e:
        fail(f"cannot read {progress_path}: {e}")

    blocks = _find_session_blocks(text)
    if not blocks:
        print(f"no canonical SESSION blocks found in {progress_path.name}; "
              f"nothing to rotate.")
        return

    total = len(blocks)
    if total <= keep:
        print(f"{total} SESSION block(s) found in {progress_path.name}; "
              f"keep={keep} — nothing to rotate.")
        return

    # The OLDEST `total - keep` blocks get archived; the rest stay.
    to_move = blocks[: total - keep]
    to_keep_blocks = blocks[total - keep:]

    # Render the archive payload (all moved blocks concatenated as they
    # appear in the source — order must be preserved).
    prelude = _split_prelude(text, blocks)
    summary = _split_summary(text, blocks)

    moved_payload_parts: list[str] = []
    kept_payload_parts: list[str] = []
    for session_number, start, end, heading in to_move:
        moved_payload_parts.append(text[start:end])
    for session_number, start, end, heading in to_keep_blocks:
        kept_payload_parts.append(text[start:end])
    moved_payload = "".join(moved_payload_parts)
    kept_payload = "".join(kept_payload_parts)

    # Archive filename uses the FIRST (oldest) moved session number.
    oldest_n = to_move[0][0]
    archive_path = older_dir / f"{oldest_n}.md"

    print(f"would move {len(to_move)} SESSION block(s) from "
          f"{progress_path.name} -> {archive_path}")
    for session_number, _start, _end, heading in to_move:
        print(f"  - moving: {heading}")
    print(f"would keep {len(to_keep_blocks)} SESSION block(s) "
          f"in {progress_path.name}:")
    for session_number, _start, _end, heading in to_keep_blocks:
        print(f"  - keeping: {heading}")

    if dry_run:
        print("(dry-run) no files written.")
        return

    # Ensure the archive directory exists; refuse to write if we can't.
    try:
        older_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        fail(
            f"cannot create {older_dir}: {e}. "
            f"Try running from the project root, or check permissions."
        )

    # Append to archive if it already exists; otherwise create it. Add
    # a marker line so successive rotations into the same archive are
    # visually separable in `cat` / `git diff` output.
    from datetime import date
    today = date.today().isoformat()
    archive_marker = f"<!-- archived at {today} -->\n"
    try:
        if archive_path.exists():
            with archive_path.open("a", encoding="utf-8") as f:
                f.write("\n")
                f.write(archive_marker)
                f.write(moved_payload)
                if not moved_payload.endswith("\n"):
                    f.write("\n")
            print(f"  - appended to existing archive: {archive_path}")
        else:
            with archive_path.open("w", encoding="utf-8") as f:
                f.write(archive_marker)
                f.write(moved_payload)
                if not moved_payload.endswith("\n"):
                    f.write("\n")
            print(f"  - created new archive: {archive_path}")
    except OSError as e:
        fail(
            f"cannot write {archive_path}: {e}. "
            f"Check disk space and permissions."
        )

    # Rewrite current_progress.txt = prelude + kept_payload + summary.
    # Defensive: always end with a trailing newline so the file stays
    # well-formed.
    new_text = prelude + kept_payload + summary
    if not new_text.endswith("\n"):
        new_text += "\n"
    try:
        # Atomic write: keeps current_progress.txt either fully
        # pre-rotation or fully post-rotation, never truncated.
        atomic_write_text(progress_path, new_text, encoding="utf-8")
    except OSError as e:
        fail(
            f"cannot rewrite {progress_path}: {e}. "
            f"The archive was written; current_progress.txt is unchanged. "
            f"Run `python tools/progress_rotate.py --dry-run` to inspect."
        )

    print(f"done: moved {len(to_move)} block(s), kept {len(to_keep_blocks)}.")


# ---------- arg parsing ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="progress_rotate.py",
        description="Archive old SESSION blocks out of current_progress.txt "
                    "into sessions/older/<N>.md. Keeps the most recent "
                    "--keep blocks in place.",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        metavar="N",
        help=f"How many most-recent SESSION blocks to keep in "
             f"current_progress.txt (default: {DEFAULT_KEEP}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be moved and where, but make no writes.",
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        default=DEFAULT_PROGRESS_PATH,
        metavar="PATH",
        help=f"Path to the active progress file (default: "
             f"{DEFAULT_PROGRESS_PATH.name}).",
    )
    parser.add_argument(
        "--older-dir",
        type=Path,
        default=DEFAULT_OLDER_DIR,
        metavar="PATH",
        help=f"Archive directory (default: {DEFAULT_OLDER_DIR}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd_rotate(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())