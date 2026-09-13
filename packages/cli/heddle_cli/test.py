# SPDX-License-Identifier: Apache-2.0
"""``heddle test`` subcommand — feat-052.

Runs the full test pyramid end-to-end: JS (Vitest in
``packages/web`` + ``packages/node``), Python (pytest in
``packages/common`` + ``packages/daemon`` + ``packages/cli``),
and (optionally) Playwright E2E under ``packages/web``. Every
child inherits ``HEDDLE_FAKE_LLM=1`` unless ``--real-llm`` was
passed (per feat-006 the CI default is fake-LLM; nightly
live-LLM runs are gated on a repo secret).

The orchestrator aggregates every suite's exit code, prints a
summary table, and returns 0 iff **all** non-skipped suites
exited with 0. Skipped suites never contribute to the failure
count. A failing JS suite does NOT short-circuit Python suites
— the pyramid runs to completion so the operator sees the full
failure set in a single invocation.

This module is the *orchestrator*: it owns subprocess + summary
side effects, but delegates config parsing to ``test_config.py``
and the default collaborators to ``test_defaults.py``. Splitting
those concerns keeps the orchestrator under the 200-line soft
cap from CODE_STYLE.md. Every side-effecting collaborator is
injectable so tests can drive every branch without spawning
real pnpm / pytest processes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

from .test_config import (
    ENV_FAKE_LLM,
    TestCommand,
    resolve_config,
)
from .test_defaults import (
    PlaywrightProbe,
    SuiteRunner,
    SummarySink,
    default_playwright_probe,
    default_suite_runner,
    default_summary_sink,
    print_summary,
)


# ---------- exit codes ----------

EXIT_OK: int = 0
EXIT_TEST_FAILED: int = 2
EXIT_BAD_CONFIG: int = 3

#: Truncation limit for the failed-suite detail column.
DETAIL_TRUNCATE_CHARS: int = 200


# ---------- helpers ----------


def _child_env(cmd: TestCommand, *, real_llm: bool) -> dict[str, str]:
    """Inject (or strip) ``HEDDLE_FAKE_LLM`` on ``cmd.env``.

    The injected var is *overwritten* on the child's env even if
    the operator pre-set it to a different value, so
    ``--real-llm`` is the single source of truth.
    """
    env = dict(cmd.env)
    if not real_llm:
        env[ENV_FAKE_LLM] = "1"
    else:
        env.pop(ENV_FAKE_LLM, None)
    return env


def _truncate(text: str, limit: int = DETAIL_TRUNCATE_CHARS) -> str:
    """Return ``text`` trimmed to ``limit`` chars with a marker."""
    if len(text) <= limit:
        return text.replace("\n", " | ").strip()
    return text[:limit].replace("\n", " | ").strip() + "…"


def _run_one(
    cmd: TestCommand,
    *,
    runner: SuiteRunner,
    real_llm: bool,
) -> tuple[bool, str, str]:
    """Run one suite; return ``(passed, detail, rc_str)``.

    Skipped suites return ``(True, "<reason>", "-")`` so they
    never contribute to the failure count. Failed suites include
    the last lines of stderr so the summary stays useful even
    when the operator isn't tailing the run.
    """
    if cmd.skipped:
        return True, cmd.skip_reason or "skipped", "-"

    child_env = _child_env(cmd, real_llm=real_llm)
    rc, _stdout, stderr = runner(
        list(cmd.argv),
        cwd=cmd.cwd,
        env=child_env,
    )
    if rc == 0:
        return True, "ok", str(rc)
    detail = _truncate(stderr) if stderr.strip() else "non-zero exit"
    return False, detail, str(rc)


def _execute(
    cfg: "object",
    *,
    runner: SuiteRunner,
    summary_sink: SummarySink,
) -> tuple[int, int, int]:
    """Run every command in ``cfg.commands`` and emit the summary table.

    Returns ``(passed, failed, skipped)`` so ``test_cmd`` can map
    the failed count to the appropriate exit code.
    """
    rows: list[tuple[str, str, str, str]] = []
    passed = failed = skipped = 0
    for cmd in cfg.commands:
        ok, detail, rc_str = _run_one(
            cmd, runner=runner, real_llm=cfg.real_llm,
        )
        if cmd.skipped:
            status, skipped = "SKIP", skipped + 1
        elif ok:
            status, passed = "PASS", passed + 1
        else:
            status, failed = "FAIL", failed + 1
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
    playwright_probe: PlaywrightProbe,
):
    """Resolve argv + env + repo + Playwright into a ``TestConfig``.

    Returns ``(cfg, error_message)``. When ``error_message`` is
    non-empty the orchestrator should print it to stderr and
    return ``EXIT_BAD_CONFIG``.
    """
    effective_argv = list(argv) if argv is not None else sys.argv[1:]
    effective_env: Mapping[str, str] = (
        dict(env) if env is not None else dict(os.environ)
    )
    effective_root = repo_root if repo_root is not None else Path.cwd()
    web_pkg_json = effective_root / "packages" / "web" / "package.json"
    playwright_ok = playwright_probe(web_pkg_json)
    try:
        cfg = resolve_config(
            effective_argv, effective_env, effective_root,
            playwright_available=playwright_ok,
        )
    except Exception as exc:
        return None, str(exc)
    return cfg, ""


# ---------- main entry ----------


def test_cmd(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    runner: SuiteRunner = default_suite_runner,
    playwright_probe: PlaywrightProbe = default_playwright_probe,
    summary_sink: SummarySink = default_summary_sink,
) -> int:
    """Run the ``heddle test`` command.

    Returns ``EXIT_OK`` when every non-skipped suite passed,
    ``EXIT_TEST_FAILED`` when at least one suite exited non-zero,
    or ``EXIT_BAD_CONFIG`` when argv parsing or repo layout fails.
    """
    cfg, error_message = _bootstrap(
        argv, env, repo_root, playwright_probe,
    )
    if cfg is None:
        print(f"heddle test: {error_message}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    print(
        f"heddle test: {len(cfg.commands)} suite(s); "
        f"HEDDLE_FAKE_LLM={'off' if cfg.real_llm else 'on'}",
        file=sys.stderr,
    )

    _passed, failed, _skipped = _execute(
        cfg, runner=runner, summary_sink=summary_sink,
    )

    if failed > 0:
        return EXIT_TEST_FAILED
    return EXIT_OK


__all__ = [
    "EXIT_BAD_CONFIG",
    "EXIT_OK",
    "EXIT_TEST_FAILED",
    "PlaywrightProbe",
    "SuiteRunner",
    "SummarySink",
    "test_cmd",
]
