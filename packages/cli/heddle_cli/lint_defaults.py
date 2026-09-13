# SPDX-License-Identifier: Apache-2.0
"""Default collaborators for ``heddle lint`` — feat-053.

Holds the *real-world* side-effecting implementations the lint
orchestrator accepts as injection points:

- ``LintRunner`` — wraps ``subprocess.run`` so the orchestrator
  never imports ``subprocess`` directly. Returns ``(returncode,
  stdout, stderr)`` so the summary printer can show the last few
  lines on failure.
- ``SummarySink`` — renders the summary table. The default prints
  to stdout via ``print``; tests substitute a recorder.

Kept separate from ``lint.py`` so the orchestrator stays focused on
control flow and each file remains under the 200-line cap from
CODE_STYLE.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Mapping, Sequence


# ---------- subprocess runner ----------


def default_lint_runner(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> tuple[int, str, str]:
    """Run one lint suite and return ``(returncode, stdout, stderr)``.

    ``cwd`` is the package directory the suite should run from
    (matches the JS workspace / Python package the orchestrator
    intends to lint). ``env`` is the complete env mapping the
    orchestrator built — never blended with ``os.environ`` so a
    test stub is reproducible.
    """
    completed = subprocess.run(
        list(argv),
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        int(completed.returncode),
        completed.stdout or "",
        completed.stderr or "",
    )


#: Callable alias for ``default_lint_runner``. Tests substitute a
#: stub that returns a fixed ``(rc, stdout, stderr)`` triple.
LintRunner = Callable[..., tuple[int, str, str]]


# ---------- summary printer ----------


#: Sentinel type alias for the summary sink. Receives the rendered
#: summary string and is expected to surface it (default: print to
#: stdout). Tests substitute a recorder that appends to a list.
SummarySink = Callable[[str], None]


def default_summary_sink(line: str) -> None:
    """Print ``line`` to stdout — the default summary sink."""
    print(line)


def render_summary(
    rows: Sequence[tuple[str, str, str, str]],
    *,
    total_passed: int,
    total_failed: int,
    total_skipped: int,
) -> str:
    """Render the summary table from ``rows`` and totals.

    ``rows`` is a sequence of ``(label, status, detail, rc)``
    tuples; ``status`` is one of ``"PASS"``, ``"FAIL"``,
    ``"SKIP"``. The output is plain text suitable for both humans
    and CI log scrapers.
    """
    header = (
        f"{'CHECK':<22} {'STATUS':<6} {'RC':<4} DETAIL"
    )
    lines = ["heddle lint: summary", "=" * 64, header, "-" * 64]
    for label, status, detail, rc in rows:
        lines.append(f"{label:<22} {status:<6} {rc:<4} {detail}")
    lines.append("-" * 64)
    lines.append(
        f"passed={total_passed} failed={total_failed} "
        f"skipped={total_skipped}"
    )
    return "\n".join(lines)


def print_summary(
    rows: Sequence[tuple[str, str, str, str]],
    *,
    total_passed: int,
    total_failed: int,
    total_skipped: int,
    sink: SummarySink = default_summary_sink,
) -> None:
    """Render and emit the summary table via ``sink``."""
    rendered = render_summary(
        rows,
        total_passed=total_passed,
        total_failed=total_failed,
        total_skipped=total_skipped,
    )
    sink(rendered)


__all__ = [
    "LintRunner",
    "SummarySink",
    "default_lint_runner",
    "default_summary_sink",
    "print_summary",
    "render_summary",
]