# SPDX-License-Identifier: Apache-2.0
"""Default collaborators for ``heddle test`` — feat-052.

Holds the *real-world* side-effecting implementations the test
orchestrator accepts as injection points:

- ``SuiteRunner`` — wraps ``subprocess.run`` so the orchestrator
  never imports ``subprocess`` directly. Returns ``(returncode,
  stdout, stderr)`` so the summary printer can show the last few
  lines on failure.
- ``PlaywrightProbe`` — checks whether Playwright is installed by
  looking for ``@playwright/test`` in ``packages/web/package.json``.
  The default returns ``False`` so v0.1 always treats E2E as
  skipped; wiring the actual E2E suite is out of scope for
  feat-052.
- ``SummaryPrinter`` — renders the summary table. The default
  prints to stdout via ``print``; tests substitute a recorder.

Kept separate from ``test.py`` so the orchestrator stays focused on
control flow and each file remains under the 200-line cap from
CODE_STYLE.md.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Sequence


# ---------- subprocess runner ----------


def default_suite_runner(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> tuple[int, str, str]:
    """Run one test suite and return ``(returncode, stdout, stderr)``.

    ``cwd`` is the package directory the suite should run from
    (matches the JS workspace / Python package the orchestrator
    intends to test). ``env`` is the complete env mapping the
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


#: Callable alias for ``default_suite_runner``. Tests substitute
#: a stub that returns a fixed ``(rc, stdout, stderr)`` triple.
SuiteRunner = Callable[..., tuple[int, str, str]]


# ---------- Playwright availability probe ----------


#: Matches ``"@playwright/test"`` inside ``packages/web/package.json``
#: — the canonical signal that the E2E suite is wired in. The match
#: is whitespace-tolerant (``"key"  :   "value"`` parses too).
_PLAYWRIGHT_KEY_RE = re.compile(
    r'"@playwright/test"\s*:\s*"',
)


def _read_text(path: Path) -> str:
    """Read a UTF-8 text file; missing files return empty string."""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def default_playwright_probe(web_pkg_json: Path) -> bool:
    """True iff ``@playwright/test`` appears in ``packages/web/package.json``.

    Pure parser (no subprocess, no network). The probe ignores the
    dev-dependency tree — a single ``@playwright/test`` reference is
    enough to say "wired in" — and never resolves through
    ``node_modules`` so it works in a clean checkout.
    """
    body = _read_text(web_pkg_json)
    if not body:
        return False
    # Strip comments to be tolerant of ``// …`` annotations inside
    # JSON5-ish files. JSON5 isn't standard JSON; use a permissive
    # pre-filter so the regex match itself does the structural work.
    body_no_comments = re.sub(r"//[^\n]*", "", body)
    try:
        parsed = json.loads(body_no_comments)
    except json.JSONDecodeError:
        # Fall back to the regex on raw text — handles quirky files
        # where ``json.loads`` rejects trailing commas.
        return bool(_PLAYWRIGHT_KEY_RE.search(body))
    if not isinstance(parsed, dict):
        return False
    # Check devDependencies first, then dependencies, then optional.
    for section in ("devDependencies", "dependencies", "optionalDependencies"):
        deps = parsed.get(section)
        if isinstance(deps, dict) and "@playwright/test" in deps:
            return True
    return False


#: Callable alias for the probe. Tests substitute a stub returning
#: a fixed boolean without going through disk I/O.
PlaywrightProbe = Callable[[Path], bool]


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
        f"{'SUITE':<22} {'STATUS':<6} {'RC':<4} DETAIL"
    )
    lines = ["heddle test: summary", "=" * 64, header, "-" * 64]
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
    "PlaywrightProbe",
    "SuiteRunner",
    "SummarySink",
    "default_playwright_probe",
    "default_suite_runner",
    "default_summary_sink",
    "print_summary",
    "render_summary",
]
