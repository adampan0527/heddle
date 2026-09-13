# SPDX-License-Identifier: Apache-2.0
"""``heddle start`` subcommand — feat-050 (T-003).

Spawns the Node.js backend (``packages/node``) which supervises the
Python daemon; once the backend has bound its HTTP port, opens the
default browser to ``http://localhost:5173`` (where the Fastify static
plugin serves the prebuilt Vite bundle from
``packages/web/dist/index.html``). Refuses any non-loopback binding
configuration up-front so a misconfigured ``HEDDLE_NODE_HOST`` never
silently widens the surface.

This module is the *orchestrator*: it owns subprocess + signal +
browser side effects, but delegates config parsing to
``start_config.py``, signal handling to ``start_signals.py``, and the
default collaborators to ``start_defaults.py``. Splitting those
concerns keeps the orchestrator under the 200-line soft cap from
CODE_STYLE.md.

Design notes (T-003 / feat-050):

- The CLI does NOT spin up the daemon directly — that responsibility
  belongs to ``DaemonSupervisor`` inside the Node.js backend (feat-027).
  The CLI is the outer supervisor: it owns the Node process tree and
  forwards SIGINT/SIGTERM to it.
- ``start_cmd`` is a pure function so unit tests can drive every
  branch (loopback refusal, missing dist, subprocess spawn, browser
  open, graceful shutdown) without going through Click.
- All side-effecting collaborators (``subprocess.Popen``,
  ``webbrowser.open``, the HTTP ``urllib`` health probe, the
  optional ``pnpm --filter web build`` invocation) are injectable so
  tests can stub them.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .start_config import (
    StartCommandError,
    StartConfig,
    resolve_config,
)
from .start_defaults import (
    default_browser_opener,
    default_build_runner,
    default_health_probe,
    default_popen,
)
from .start_signals import forward_signals_to


# ---------- injectable collaborator types ----------

PopenFactory = Callable[..., subprocess.Popen]
BrowserOpener = Callable[[str, int], bool]
HealthProbe = Callable[[str, float], bool]
BuildRunner = Callable[[Sequence[str], Mapping[str, str]], int]


# ---------- timing constants ----------

#: Per-attempt HTTP probe timeout, seconds.
HEALTH_PROBE_TIMEOUT_S: float = 1.0
#: Total wall-clock budget for waiting on the backend to come up.
HEALTH_WAIT_BUDGET_S: float = 30.0
#: Sleep between health probes.
HEALTH_POLL_INTERVAL_S: float = 0.5


# ---------- helpers (test seams) ----------


def _wait_for_health(
    cfg: StartConfig,
    probe: HealthProbe,
    deadline_s: float = HEALTH_WAIT_BUDGET_S,
    poll_s: float = HEALTH_POLL_INTERVAL_S,
) -> bool:
    """Poll the health URL until it answers or the deadline expires."""
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        if probe(cfg.health_url, HEALTH_PROBE_TIMEOUT_S):
            return True
        time.sleep(poll_s)
    return False


def _spawn_node(
    cfg: StartConfig,
    popen_factory: PopenFactory,
) -> subprocess.Popen:
    """Spawn the Node.js backend. Returns the live Popen handle."""
    if not cfg.node_dist.exists():
        raise StartCommandError(
            f"node backend dist not found at {cfg.node_dist}; "
            f"run `pnpm --filter node build` first."
        )
    return popen_factory(
        ["node", str(cfg.node_dist)],
        env={"HEDDLE_NODE_HOST": cfg.host, "HEDDLE_NODE_PORT": str(cfg.port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ensure_web_bundle(
    cfg: StartConfig,
    build_runner: BuildRunner,
    env: Mapping[str, str],
) -> None:
    """Build the web bundle if missing.

    First-run UX: ``pnpm --filter web build`` is run automatically when
    ``packages/web/dist/index.html`` does not exist; repeat invocations
    skip the build and go straight to ``webbrowser.open``.
    """
    if cfg.web_dist.exists() and (cfg.web_dist / "index.html").exists():
        return
    cmd = ["pnpm", "--filter", "web", "build"]
    rc = build_runner(cmd, env)
    if rc != 0:
        raise StartCommandError(
            f"web bundle build failed (exit={rc}); run "
            f"`pnpm --filter web build` by hand to inspect the error"
        )


def _open_browser_safely(opener: BrowserOpener, url: str) -> None:
    """Call ``opener``; surface failures as warnings, never as errors."""
    try:
        opener(url, autoraise=True)
    except Exception as exc:  # noqa: BLE001 — browser failure is non-fatal
        print(
            f"heddle start: could not open browser ({exc}); "
            f"visit {url} manually",
            file=sys.stderr,
        )


# ---------- main entry ----------


def start_cmd(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    popen_factory: PopenFactory = default_popen,
    browser_opener: BrowserOpener = default_browser_opener,
    health_probe: HealthProbe = default_health_probe,
    build_runner: BuildRunner = default_build_runner,
    open_browser_on_success: bool = True,
    health_deadline_s: float = HEALTH_WAIT_BUDGET_S,
    health_poll_s: float = HEALTH_POLL_INTERVAL_S,
) -> int:
    """Run the ``heddle start`` command.

    Returns the process exit code (0 = clean shutdown, non-zero =
    failure). On a clean SIGINT/SIGTERM the child Node process is
    forwarded the same signal and we wait for it to exit before
    returning 0.

    Every side-effecting collaborator is injectable so tests can drive
    every branch without spinning up real processes or browsers.
    """
    effective_argv = list(argv) if argv is not None else sys.argv[1:]
    effective_env: Mapping[str, str] = os.environ if env is None else env
    effective_root = repo_root if repo_root is not None else Path.cwd()

    try:
        cfg = resolve_config(effective_argv, effective_env, effective_root)
    except StartCommandError as exc:
        print(f"heddle start: {exc}", file=sys.stderr)
        return 2
    try:
        _ensure_web_bundle(cfg, build_runner, effective_env)
    except StartCommandError as exc:
        print(f"heddle start: {exc}", file=sys.stderr)
        return 3
    try:
        proc = _spawn_node(cfg, popen_factory)
    except StartCommandError as exc:
        print(f"heddle start: {exc}", file=sys.stderr)
        return 4

    if not _wait_for_health(cfg, health_probe, health_deadline_s, health_poll_s):
        proc.terminate()
        proc.wait(timeout=5)
        print(
            f"heddle start: backend at {cfg.health_url} did not become "
            f"reachable within {health_deadline_s:.0f}s",
            file=sys.stderr,
        )
        return 5

    if open_browser_on_success:
        _open_browser_safely(browser_opener, cfg.frontend_url)
    print(
        f"heddle start: backend ready at {cfg.health_url}; "
        f"frontend at {cfg.frontend_url}; press Ctrl+C to stop"
    )

    restore_handlers = forward_signals_to(proc)
    try:
        return int(proc.wait())
    finally:
        restore_handlers()


__all__ = [
    "BuildRunner",
    "BrowserOpener",
    "HealthProbe",
    "HEALTH_POLL_INTERVAL_S",
    "HEALTH_PROBE_TIMEOUT_S",
    "HEALTH_WAIT_BUDGET_S",
    "PopenFactory",
    "StartCommandError",
    "StartConfig",
    "start_cmd",
]
