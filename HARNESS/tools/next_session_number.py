#!/usr/bin/env python3
"""next_session_number.py — compute the next session number from the progress file.

Reads `current_progress.txt` (default) or `--progress-file PATH`, finds the
highest canonical `### SESSION <N> — <ISO date>` block, and prints `N+1`.
If no SESSION blocks are found (fresh project, or progress file absent),
prints `1` — SESSION 0 is the Initializer, so the first Coding Agent is
SESSION 1.

Usage:
    python tools/next_session_number.py [--progress-file PATH]

Exit codes:
    0   success (printed number on stdout)
    1   invalid arguments or unreadable file

Design notes:
- stdlib only — harness convention.
- The canonical SESSION heading regex matches the shape produced by
  `tools/session_end.py._build_session_block`. The Initializer's bare
  `SESSION 0 - Initializer Agent` line does not match this regex and is
  correctly ignored — SESSION 0 belongs to the Initializer, not this counter.
- We consider every matching heading and return the highest number, which is
  robust to blocks appearing out of order.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------- paths ----------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROGRESS_PATH = PROJECT_ROOT / "current_progress.txt"

# Import the canonical SESSION heading regex from the shared _constants
# module. The regex lives in `_constants` so tools/next_session_number.py,
# tools/progress_rotate.py, and (in spirit) tools/session_end.py stay in
# sync. See _constants.SESSION_HEADING_RE for the rationale on why the
# Initializer's bare "SESSION 0 - Initializer Agent" line is excluded.
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from _constants import SESSION_HEADING_RE  # noqa: E402

# Coding Agent sessions start at 1 (SESSION 0 is the Initializer).
FIRST_CODING_SESSION = 1


def fail(msg: str) -> None:
    """Print error to stderr and exit with a non-zero status."""
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def find_highest_session(text: str) -> int | None:
    """Return the highest session N from canonical headings, else None."""
    highest: int | None = None
    for match in SESSION_HEADING_RE.finditer(text):
        number = int(match.group(1))
        if highest is None or number > highest:
            highest = number
    return highest


def compute_next_session_number(progress_path: Path) -> int:
    """Return the next session number, or 1 when no progress exists."""
    if not progress_path.exists():
        return FIRST_CODING_SESSION
    try:
        text = progress_path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read {progress_path.name}: {exc}")
    highest = find_highest_session(text)
    return FIRST_CODING_SESSION if highest is None else highest + 1


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="next_session_number.py",
        description=(
            "Compute the next session number from the progress file. "
            "Prints the number on stdout; exits 0."
        ),
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        default=DEFAULT_PROGRESS_PATH,
        metavar="PATH",
        help=(
            f"Path to the active progress file (default: "
            f"{DEFAULT_PROGRESS_PATH.name})."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, compute, and print the next session number."""
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    progress_path: Path = args.progress_file
    if not progress_path.is_absolute():
        progress_path = PROJECT_ROOT / progress_path

    print(compute_next_session_number(progress_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
