# SPDX-License-Identifier: Apache-2.0
"""``heddle dev`` subcommand — feat-051 (T-019).

Spawns three subprocesses in parallel:

1. **Vite dev server** (``pnpm --filter web dev``) on ``127.0.0.1:5173``.
2. **Node.js backend** (``pnpm --filter node dev``, tsx under the
   hood per ``packages/node/package.json``) on ``127.0.0.1:5174``.
3. **Python daemon** (``python -m heddle_daemon``) on a loopback
   port chosen by the Node.js supervisor.

Vite proxies ``/api`` and ``/ws`` to the Node.js backend so the
browser always talks to a single origin (TECH.md T-026). The
orchestrator refuses to start when the proxy is misconfigured or
missing — a silent proxy miss is a debugging nightmare.

This module is the *orchestrator*: it owns subprocess + signal +
proxy side effects, but delegates config parsing to
``dev_config.py``, signal handling to ``dev_signals.py``, and the
default collaborators to ``dev_defaults.py``. Splitting those
concerns keeps the orchestrator under the 200-line soft cap from
CODE_STYLE.md.

Design notes (T-019 / feat-051):

- Every side-effecting collaborator is injectable so tests can
  drive every branch without spinning up real processes.
- The orchestrator exits when ANY child dies — vite or node dying
  without a SIGINT/SIGTERM is the canonical "dev session broken"
  signal and deserves a non-zero exit code so CI / dev-loop
  scripts can react.
- On Ctrl+C / SIGTERM the OS signal is forwarded to all three
  children (reverse-spawn order, see ``dev_signals``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .dev_config import (
    DEFAULT_NODE_PORT,
    ChildCommand,
    DevCommandError,
    DevConfig,
    resolve_config,
)
from .dev_defaults import (
    LineSink,
    PopenFactoryLike,
    ProxyConfig,
    ProxyConfigReaderLike,
    assert_proxy_matches_backend,
    default_line_streamer,
    default_popen,
    default_proxy_reader,
)
from .dev_signals import forward_signals_to_all


# ---------- injectable collaborator types ----------

#: Signature for the proxy-config reader injection point. Maps to
#: ``dev_defaults.default_proxy_reader`` in production.
ProxyReader = ProxyConfigReaderLike

#: Signature for the line streamer injection point. Maps to
#: ``dev_defaults.default_line_streamer`` in production.
Streamer = Callable[[str, Iterable[str] | None, Iterable[str] | None], None]


# ---------- exit codes ----------

#: Proxy config missing or mis-targeted.
EXIT_PROXY_MISCONFIGURED: int = 2
#: Failed to spawn one of the three children.
EXIT_SPAWN_FAILED: int = 3
#: A child died without a forwarded signal — dev session broken.
EXIT_CHILD_DIED: int = 4


# ---------- helpers ----------


def _spawn_one(
    child: ChildCommand,
    popen_factory: PopenFactoryLike,
) -> subprocess.Popen:
    """Spawn one child command; return the live Popen handle.

    Uses ``bufsize=1`` text mode on stdout/stderr so the line
    streamer can split on ``\\n`` without buffering surprises.
    """
    return popen_factory(
        list(child.argv),
        cwd=str(child.cwd),
        env=dict(child.env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        text=True,
    )


def _start_streamers(
    procs: Iterable[tuple[str, subprocess.Popen]],
    streamer: Streamer,
) -> list[threading.Thread]:
    """Spawn a daemon thread per child that pumps stdout + stderr.

    Returns the list of started threads so the caller can join them
    on shutdown. ``streamer`` is invoked synchronously inside each
    thread; when the child closes its pipe, ``streamer`` returns
    and the thread exits.
    """
    threads: list[threading.Thread] = []
    for label, proc in procs:
        t = threading.Thread(
            target=streamer,
            args=(label, proc.stdout, proc.stderr),
            name=f"dev-stream-{label}",
            daemon=True,
        )
        t.start()
        threads.append(t)
    return threads


def _wait_for_first_exit(
    procs: Sequence[tuple[str, subprocess.Popen]],
    poll_interval_s: float = 0.1,
) -> tuple[str, subprocess.Popen] | None:
    """Poll every child until one exits; return the first to die.

    Returns ``None`` if every child is still alive — the caller
    loops until either a child dies or an OS signal handler wakes
    the supervisor up to forward SIGTERM.
    """
    while True:
        for label, proc in procs:
            if proc.poll() is not None:
                return label, proc
        threading.Event().wait(poll_interval_s)


def _terminate_all(procs: Sequence[tuple[str, subprocess.Popen]]) -> None:
    """SIGTERM every child, swallowing errors for already-dead ones."""
    for _, proc in procs:
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass


def _wait_all(procs: Sequence[tuple[str, subprocess.Popen]], timeout_s: float) -> None:
    """Best-effort ``wait()`` on every child with a hard deadline."""
    for _, proc in procs:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass


# ---------- main entry ----------


def dev_cmd(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    popen_factory: PopenFactoryLike = default_popen,
    proxy_reader: ProxyReader = default_proxy_reader,
    streamer: Streamer = default_line_streamer,
    sink: LineSink = print,
    open_browser: bool = True,
    browser_opener: Callable[[str, int], bool] | None = None,
    child_exit_timeout_s: float = 5.0,
) -> int:
    """Run the ``heddle dev`` command.

    Returns the process exit code:

    - ``0`` on a clean shutdown initiated by SIGINT / SIGTERM
      forwarded to the children, OR if the operator exits cleanly.
    - ``EXIT_PROXY_MISCONFIGURED`` when the Vite proxy config does
      not point at the loopback backend.
    - ``EXIT_SPAWN_FAILED`` when a subprocess could not be spawned.
    - ``EXIT_CHILD_DIED`` when a child exits without a forwarded
      signal — dev session is broken, surface the failure.

    Every side-effecting collaborator is injectable so tests can
    drive every branch without spinning up real processes or
    browsers.
    """
    effective_argv = list(argv) if argv is not None else sys.argv[1:]
    effective_env: Mapping[str, str] = os.environ if env is None else env
    effective_root = repo_root if repo_root is not None else Path.cwd()

    try:
        cfg = resolve_config(effective_argv, effective_env, effective_root)
    except DevCommandError as exc:
        print(f"heddle dev: {exc}", file=sys.stderr)
        return 2

    vite_config = cfg.repo_root / "packages" / "web" / "vite.config.ts"
    proxy = proxy_reader(vite_config)
    mismatch = assert_proxy_matches_backend(
        proxy,
        expected_host="127.0.0.1",
        expected_port=DEFAULT_NODE_PORT,
    )
    if mismatch is not None:
        reason, detail = mismatch
        print(
            f"heddle dev: Vite proxy misconfigured ({reason}): {detail}. "
            f"Edit {vite_config} so /api and /ws proxy to "
            f"http://127.0.0.1:{DEFAULT_NODE_PORT}.",
            file=sys.stderr,
        )
        return EXIT_PROXY_MISCONFIGURED

    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        for child in cfg.children:
            try:
                proc = _spawn_one(child, popen_factory)
            except OSError as exc:
                print(
                    f"heddle dev: failed to spawn {child.label} "
                    f"({' '.join(child.argv)}): {exc}",
                    file=sys.stderr,
                )
                _terminate_all(procs)
                _wait_all(procs, child_exit_timeout_s)
                return EXIT_SPAWN_FAILED
            procs.append((child.label, proc))
    except Exception:
        _terminate_all(procs)
        _wait_all(procs, child_exit_timeout_s)
        raise

    # The streaming threads invoke ``streamer`` which defaults to
    # ``default_line_streamer``; that helper calls ``sink`` per
    # line. Tests inject a streamer that records directly into a
    # list, bypassing the sink entirely.
    _ = sink  # kept for parity with start_cmd's API surface

    _start_streamers(procs, streamer)

    if open_browser and browser_opener is not None:
        try:
            browser_opener(cfg.frontend_url, 1)
        except Exception as exc:  # noqa: BLE001 — browser failure is non-fatal
            print(
                f"heddle dev: could not open browser ({exc}); "
                f"visit {cfg.frontend_url} manually",
                file=sys.stderr,
            )

    print(
        f"heddle dev: vite + node + daemon up; "
        f"frontend at {cfg.frontend_url}; press Ctrl+C to stop"
    )

    restore_handlers = forward_signals_to_all([p for _, p in procs])
    try:
        dead = _wait_for_first_exit(procs)
    finally:
        restore_handlers()

    if dead is None:
        # Unreachable while we always return a child or signal-driven
        # exit; treat as a defensive no-op.
        return 0

    label, proc = dead
    code = proc.returncode
    if code is None:
        # Killed by a signal we forwarded; treat as clean shutdown.
        print(f"heddle dev: child [{label}] terminated by signal", file=sys.stderr)
        _terminate_all(procs)
        _wait_all(procs, child_exit_timeout_s)
        return 0
    if code < 0:
        # Popen convention: negative returncode means the process
        # was killed by signal ``-code``. Treat as a clean
        # forwarded-signal exit (returns 0). Forwarding SIGINT /
        # SIGTERM is what ``forward_signals_to_all`` does; this
        # branch is the natural result of Ctrl+C.
        print(
            f"heddle dev: child [{label}] terminated by signal "
            f"{-code}",
            file=sys.stderr,
        )
        _terminate_all(procs)
        _wait_all(procs, child_exit_timeout_s)
        return 0
    if code == 0:
        # Clean exit of one child (rare — usually only happens when
        # the user runs `pnpm --filter web dev` in another shell).
        print(f"heddle dev: child [{label}] exited cleanly", file=sys.stderr)
        _terminate_all(procs)
        _wait_all(procs, child_exit_timeout_s)
        return 0

    print(
        f"heddle dev: child [{label}] died with exit code {code}; "
        f"shutting down siblings",
        file=sys.stderr,
    )
    _terminate_all(procs)
    _wait_all(procs, child_exit_timeout_s)
    return EXIT_CHILD_DIED


__all__ = [
    "EXIT_CHILD_DIED",
    "EXIT_PROXY_MISCONFIGURED",
    "EXIT_SPAWN_FAILED",
    "ProxyReader",
    "Streamer",
    "dev_cmd",
]
