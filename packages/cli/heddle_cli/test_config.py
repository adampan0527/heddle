# SPDX-License-Identifier: Apache-2.0
"""Configuration resolution for ``heddle test`` — feat-052.

Pure functions that turn argv + env + repo layout into a frozen
``TestConfig`` describing which suites to run and which to skip.
Kept separate from ``test.py`` so the config layer can be
unit-tested in isolation (no subprocess, no Playwright probe) and
so each module stays under the 200-line cap from CODE_STYLE.md.

Design notes (feat-052):

- JS = Vitest in ``packages/web`` + ``packages/node``; Python =
  pytest in ``packages/common`` + ``packages/daemon`` +
  ``packages/cli``; optional Playwright E2E.
- ``HEDDLE_FAKE_LLM=1`` is injected by the orchestrator on every
  child unless ``--real-llm`` was passed. Per feat-006 the CI
  default is fake LLM; nightly live-LLM runs are gated on a repo
  secret.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


# ---------- constants (single source of truth) ----------

ENV_FAKE_LLM: str = "HEDDLE_FAKE_LLM"
PYTEST_VERBOSE_FLAG: str = "-q"
JS_PACKAGES: tuple[str, ...] = ("web", "node")
PY_PACKAGES: tuple[str, ...] = ("common", "daemon", "cli")


# ---------- errors ----------


class TestCommandError(RuntimeError):
    """Raised on a fatal precondition failure (bad argv, missing repo)."""


# ---------- records ----------


@dataclass(frozen=True)
class TestCommand:
    """One test-suite invocation the ``test`` orchestrator should run.

    ``env`` is a *complete* mapping; the orchestrator does not
    blend it with ``os.environ`` so the child sandbox is
    reproducible per test.
    """

    label: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    skipped: bool = False
    skip_reason: str = ""


@dataclass(frozen=True)
class TestConfig:
    """Resolved test-time configuration."""

    repo_root: Path
    commands: tuple[TestCommand, ...]
    js_skip: bool = False
    py_skip: bool = False
    e2e_skip: bool = False
    real_llm: bool = False


# ---------- argv parsing ----------


#: Flags accepted by ``heddle test``.
KNOWN_FLAGS: frozenset[str] = frozenset(
    {"--js-skip", "--py-skip", "--e2e-skip", "--real-llm"}
)


def _parse_argv(argv: Sequence[str]) -> dict[str, bool]:
    """Parse ``--js-skip``, ``--py-skip``, ``--e2e-skip``, ``--real-llm``.

    Unknown flags raise so a typo never silently falls through
    (mirrors ``start_config._resolve_argv``).
    """
    flags = {
        "js_skip": False,
        "py_skip": False,
        "e2e_skip": False,
        "real_llm": False,
    }
    for token in argv:
        if token not in KNOWN_FLAGS:
            raise TestCommandError(f"unknown argument: {token}")
        key = token.lstrip("-").replace("-", "_")
        flags[key] = True
    return flags


# ---------- suite builders ----------


def _build_suite(
    label: str,
    argv: tuple[str, ...],
    pkg_dir: Path,
    sentinel: Path,
    env: Mapping[str, str],
) -> TestCommand:
    """Build a ``TestCommand``; mark ``skipped`` when ``sentinel`` is missing."""
    skipped = not sentinel.exists()
    return TestCommand(
        label=label,
        argv=argv,
        cwd=pkg_dir,
        env=dict(env),
        skipped=skipped,
        skip_reason=f"missing {sentinel}" if skipped else "",
    )


def _js_cmd(pkg: str, repo_root: Path, env: Mapping[str, str]) -> TestCommand:
    """Build the ``pnpm --filter <pkg> test`` command, or a stub."""
    pkg_dir = repo_root / "packages" / pkg
    return _build_suite(
        label=f"js:{pkg}",
        argv=("pnpm", "--filter", pkg, "test"),
        pkg_dir=pkg_dir,
        sentinel=pkg_dir / "package.json",
        env=env,
    )


def _py_cmd(pkg: str, repo_root: Path, env: Mapping[str, str]) -> TestCommand:
    """Build the ``python -m pytest -q`` command, or a stub."""
    pkg_dir = repo_root / "packages" / pkg
    return _build_suite(
        label=f"py:{pkg}",
        argv=("python", "-m", "pytest", PYTEST_VERBOSE_FLAG),
        pkg_dir=pkg_dir,
        sentinel=pkg_dir / "pyproject.toml",
        env=env,
    )


def _e2e_cmd(
    repo_root: Path, env: Mapping[str, str], *, available: bool
) -> TestCommand:
    """Build the (stubbed) Playwright E2E command."""
    web_dir = repo_root / "packages" / "web"
    reason = (
        "Playwright not installed"
        if not available
        else "Playwright E2E not wired in v0.1"
    )
    return TestCommand(
        label="e2e:browser",
        argv=("pnpm", "--filter", "web", "test:e2e"),
        cwd=web_dir,
        env=dict(env),
        skipped=True,
        skip_reason=reason,
    )


# ---------- public resolver ----------


def resolve_config(
    argv: Sequence[str],
    env: Mapping[str, str],
    repo_root: Path,
    *,
    playwright_available: bool = True,
) -> TestConfig:
    """Resolve the test config from argv + env.

    The orchestrator computes the list of commands up front so it
    can summarise before any subprocess is launched. Missing
    package directories are recorded as skipped commands so the
    summary table still shows the layout intent.
    """
    if not repo_root.exists():
        raise TestCommandError(f"repo_root {repo_root} does not exist")
    flags = _parse_argv(argv)

    commands: list[TestCommand] = []
    if not flags["js_skip"]:
        commands += [_js_cmd(p, repo_root, env) for p in JS_PACKAGES]
    if not flags["py_skip"]:
        commands += [_py_cmd(p, repo_root, env) for p in PY_PACKAGES]
    if not flags["e2e_skip"]:
        commands.append(
            _e2e_cmd(repo_root, env, available=playwright_available)
        )

    return TestConfig(
        repo_root=repo_root,
        commands=tuple(commands),
        js_skip=flags["js_skip"],
        py_skip=flags["py_skip"],
        e2e_skip=flags["e2e_skip"],
        real_llm=flags["real_llm"],
    )


__all__ = [
    "ENV_FAKE_LLM",
    "JS_PACKAGES",
    "KNOWN_FLAGS",
    "PY_PACKAGES",
    "PYTEST_VERBOSE_FLAG",
    "TestCommand",
    "TestCommandError",
    "TestConfig",
    "resolve_config",
]
