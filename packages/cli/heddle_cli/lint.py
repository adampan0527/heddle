# SPDX-License-Identifier: Apache-2.0
"""``heddle lint`` subcommand — feat-053.

Runs the project's three lint layers end-to-end: ESLint on
JavaScript/TypeScript (across ``packages/web`` + ``packages/node``),
TypeScript ``--noEmit`` type-checking (across ``packages/web`` +
``packages/node``), and Ruff on Python (across ``packages/``).
Aggregates every suite's exit code, prints a summary table, and
returns 0 iff **all** non-skipped suites exited with 0. Skipped
suites never contribute to the failure count. A failing ESLint
suite does NOT short-circuit the type-check or Ruff runs — the
pyramid runs to completion so the operator sees the full failure
set in a single invocation.

This module is the *orchestrator*: it owns subprocess + summary
side effects, but delegates config parsing to ``lint_config.py``
and the default collaborators to ``lint_defaults.py``. Splitting
those concerns keeps the orchestrator under the 200-line soft
cap from CODE_STYLE.md. Every side-effecting collaborator is
injectable so tests can drive every branch without spawning real
pnpm / tsc / ruff processes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

from .lint_config import (
    LintCommand,
    LintConfig,
    resolve_config,
)
from .lint_defaults import (
    LintRunner,
    SummarySink,
    default_lint_runner,
    default_summary_sink,
    print_summary,
)


# ---------- exit codes ----------

EXIT_OK: int = 0
EXIT_LINT_FAILED: int = 2
EXIT_BAD_CONFIG: int = 3

#: Truncation limit for the failed-suite detail column.
DETAIL_TRUNCATE_CHARS: int = 200


# ---------- helpers ----------


def _truncate(text: str, limit: int = DETAIL_TRUNCATE_CHARS) -> str:
    """Return ``text`` trimmed to ``limit`` chars with a marker."""
    if len(text) <= limit:
        return text.replace("\n", " | ").strip()
    return text[:limit].replace("\n", " | ").strip() + "..."


def _run_one(
    cmd: LintCommand,
    *,
    runner: LintRunner,
) -> tuple[bool, str, str]:
    """Run one suite; return ``(passed, detail, rc_str)``.

    Skipped suites return ``(True, "<reason>", "-")`` so they
    never contribute to the failure count. Failed suites include
    the last lines of stderr so the summary stays useful even
    when the operator isn't tailing the run.
    """
    if cmd.skipped:
        return True, cmd.skip_reason or "skipped", "-"

    rc, _stdout, stderr = runner(
        list(cmd.argv),
        cwd=cmd.cwd,
        env=dict(cmd.env),
    )
    if rc == 0:
        return True, "ok", str(rc)
    detail = _truncate(stderr) if stderr.strip() else "non-zero exit"
    return False, detail, str(rc)


def _execute(
    cfg: LintConfig,
    *,
    runner: LintRunner,
    summary_sink: SummarySink,
) -> tuple[int, int, int]:
    """Run every command in ``cfg.commands`` and emit the summary table.

    Returns ``(passed, failed, skipped)`` so ``lint_cmd`` can map
    the failed count to the appropriate exit code.
    """
    rows: list[tuple[str, str, str, str]] = []
    passed = failed = skipped = 0
    for cmd in cfg.commands:
        ok, detail, rc_str = _run_one(cmd, runner=runner)
        if cmd.skipped:
            status = "SKIP"
            skipped += 1
        elif ok:
            status = "PASS"
            passed += 1
        else:
            status = "FAIL"
            failed += 1
        rows.append((cmd.label, status, detail, rc_str))

    print_summary(
        rows,
        total_passed=passed,
        total_failed=failed,
        total_skipped=skipped,
        sink=summary_sink,
    )
    return passed, failed, skipped


def _bootstrap(
    argv: Sequence[str] | None,
    env: Mapping[str, str] | None,
    repo_root: Path | None,
):
    """Resolve argv + env + repo into a ``LintConfig``.

    Returns ``(cfg, error_message)``. When ``error_message`` is
    non-empty the orchestrator should print it to stderr and
    return ``EXIT_BAD_CONFIG``.
    """
    effective_argv = list(argv) if argv is not None else sys.argv[1:]
    effective_env: Mapping[str, str] = (
        dict(env) if env is not None else dict(os.environ)
    )
    effective_root = repo_root if repo_root is not None else Path.cwd()
    try:
        cfg = resolve_config(effective_argv, effective_env, effective_root)
    except Exception as exc:
        return None, str(exc)
    return cfg, ""


# ---------- main entry ----------


def lint_cmd(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    runner: LintRunner = default_lint_runner,
    summary_sink: SummarySink = default_summary_sink,
) -> int:
    """Run the ``heddle lint`` command.

    Returns ``EXIT_OK`` when every non-skipped suite passed,
    ``EXIT_LINT_FAILED`` when at least one suite exited non-zero,
    or ``EXIT_BAD_CONFIG`` when argv parsing or repo layout fails.
    """
    cfg, error_message = _bootstrap(argv, env, repo_root)
    if cfg is None:
        print(f"heddle lint: {error_message}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    print(
        f"heddle lint: {len(cfg.commands)} check(s)",
        file=sys.stderr,
    )

    _passed, failed, _skipped = _execute(
        cfg, runner=runner, summary_sink=summary_sink,
    )

    if failed > 0:
        return EXIT_LINT_FAILED
    return EXIT_OK


__all__ = [
    "EXIT_BAD_CONFIG",
    "EXIT_LINT_FAILED",
    "EXIT_OK",
    "LintRunner",
    "SummarySink",
    "lint_cmd",
]