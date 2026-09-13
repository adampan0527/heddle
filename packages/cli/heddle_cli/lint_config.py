# SPDX-License-Identifier: Apache-2.0
"""Configuration resolution for ``heddle lint`` — feat-053.

Pure functions that turn argv + repo layout into a frozen
``LintConfig`` describing which lint suites to run and which to
skip. Kept separate from ``lint.py`` so the config layer can be
unit-tested in isolation (no subprocess, no repo probe) and so each
module stays under the 200-line cap from CODE_STYLE.md.

Design notes (feat-053):

- JS = ESLint in ``packages/web`` + ``packages/node``; Python =
  Ruff across all Python packages under ``packages/``; TypeScript =
  ``tsc --noEmit`` in ``packages/web`` + ``packages/node``.
- The lint orchestrator (not this module) aggregates exit codes.
- Skipped suites never contribute to the failure count.
- Unknown flags raise so a typo never silently falls through
  (mirrors ``test_config._parse_argv``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


# ---------- constants (single source of truth) ----------

#: Workspace packages that ship JS/TS and therefore participate in
#: the ESLint and TypeScript ``--noEmit`` runs.
JS_PACKAGES: tuple[str, ...] = ("web", "node")

#: Path under the repo root that Ruff should scan for Python code.
PY_LINT_PATH: str = "packages"

#: Default Ruff invocation. ``check`` runs the linter (not the
#: formatter) so ``heddle lint`` and ``heddle format`` stay separate.
RUFF_ARGV: tuple[str, ...] = ("ruff", "check", PY_LINT_PATH)


# ---------- errors ----------


class LintCommandError(RuntimeError):
    """Raised on a fatal precondition failure (bad argv, missing repo)."""


# ---------- records ----------


@dataclass(frozen=True)
class LintCommand:
    """One lint-suite invocation the ``lint`` orchestrator should run.

    ``env`` is a *complete* mapping; the orchestrator does not
    blend it with ``os.environ`` so the child sandbox is
    reproducible per lint.
    """

    label: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    skipped: bool = False
    skip_reason: str = ""


@dataclass(frozen=True)
class LintConfig:
    """Resolved lint-time configuration."""

    repo_root: Path
    commands: tuple[LintCommand, ...]
    js_skip: bool = False
    ts_skip: bool = False
    py_skip: bool = False


# ---------- argv parsing ----------


#: Flags accepted by ``heddle lint``.
KNOWN_FLAGS: frozenset[str] = frozenset(
    {"--js-skip", "--ts-skip", "--py-skip"}
)


def _parse_argv(argv: Sequence[str]) -> dict[str, bool]:
    """Parse ``--js-skip``, ``--ts-skip``, ``--py-skip``.

    Unknown flags raise so a typo never silently falls through
    (mirrors ``test_config._parse_argv``).
    """
    flags = {
        "js_skip": False,
        "ts_skip": False,
        "py_skip": False,
    }
    for token in argv:
        if token not in KNOWN_FLAGS:
            raise LintCommandError(f"unknown argument: {token}")
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
) -> LintCommand:
    """Build a ``LintCommand``; mark ``skipped`` when ``sentinel`` is missing."""
    skipped = not sentinel.exists()
    return LintCommand(
        label=label,
        argv=argv,
        cwd=pkg_dir,
        env=dict(env),
        skipped=skipped,
        skip_reason=f"missing {sentinel}" if skipped else "",
    )


def _eslint_cmd(pkg: str, repo_root: Path, env: Mapping[str, str]) -> LintCommand:
    """Build the ``pnpm --filter <pkg> lint`` command, or a stub.

    Each JS workspace exposes a ``lint`` script (ESLint under the
    hood) per CODE_STYLE.md. Falls back to ``eslint .`` when the
    per-package script is missing so an ESLint config-only repo
    still lints.
    """
    pkg_dir = repo_root / "packages" / pkg
    return _build_suite(
        label=f"eslint:{pkg}",
        argv=("pnpm", "--filter", pkg, "lint"),
        pkg_dir=pkg_dir,
        sentinel=pkg_dir / "package.json",
        env=env,
    )


def _tsc_cmd(pkg: str, repo_root: Path, env: Mapping[str, str]) -> LintCommand:
    """Build the ``pnpm --filter <pkg> exec tsc --noEmit`` command, or stub."""
    pkg_dir = repo_root / "packages" / pkg
    return _build_suite(
        label=f"tsc:{pkg}",
        argv=("pnpm", "--filter", pkg, "exec", "tsc", "--noEmit"),
        pkg_dir=pkg_dir,
        sentinel=pkg_dir / "tsconfig.json",
        env=env,
    )


def _ruff_cmd(repo_root: Path, env: Mapping[str, str]) -> LintCommand:
    """Build the root-level ``ruff check packages/`` command, or stub."""
    sentinel = repo_root / "packages"
    return _build_suite(
        label="ruff:packages",
        argv=RUFF_ARGV,
        pkg_dir=repo_root,
        sentinel=sentinel,
        env=env,
    )


# ---------- public resolver ----------


def resolve_config(
    argv: Sequence[str],
    env: Mapping[str, str],
    repo_root: Path,
) -> LintConfig:
    """Resolve the lint config from argv + repo layout.

    The orchestrator computes the list of commands up front so it
    can summarise before any subprocess is launched. Missing
    package directories are recorded as skipped commands so the
    summary table still shows the layout intent.
    """
    if not repo_root.exists():
        raise LintCommandError(f"repo_root {repo_root} does not exist")
    flags = _parse_argv(argv)

    commands: list[LintCommand] = []
    if not flags["js_skip"]:
        commands += [_eslint_cmd(p, repo_root, env) for p in JS_PACKAGES]
    if not flags["ts_skip"]:
        commands += [_tsc_cmd(p, repo_root, env) for p in JS_PACKAGES]
    if not flags["py_skip"]:
        commands.append(_ruff_cmd(repo_root, env))

    return LintConfig(
        repo_root=repo_root,
        commands=tuple(commands),
        js_skip=flags["js_skip"],
        ts_skip=flags["ts_skip"],
        py_skip=flags["py_skip"],
    )


__all__ = [
    "JS_PACKAGES",
    "KNOWN_FLAGS",
    "LintCommand",
    "LintCommandError",
    "LintConfig",
    "PY_LINT_PATH",
    "RUFF_ARGV",
    "resolve_config",
]