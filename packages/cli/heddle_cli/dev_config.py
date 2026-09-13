# SPDX-License-Identifier: Apache-2.0
"""Configuration resolution for ``heddle dev`` — feat-051 (T-019).

The dev command spins up three child processes (Vite dev server,
Node.js backend with tsx --watch, Python daemon). This module holds
the constants + pure functions that turn argv + repo layout into a
frozen ``DevConfig`` describing the three commands to spawn. Kept
separate from ``dev.py`` so the config layer is unit-testable in
isolation and each module stays under the 200-line cap from
CODE_STYLE.md.

Design notes (T-019 / feat-051):

- The orchestrator does NOT own the daemon-supervision contract; the
  Node.js ``DaemonSupervisor`` already owns the daemon lifecycle in
  production (feat-027). ``heddle dev`` runs the daemon as a third
  top-level subprocess so the dev cycle stays transparent to the
  operator — every restart shows in the supervisor log.
- Port defaults mirror the canonical heddle allocation:
  Vite=5173, Node.js=5174. The daemon listens on the loopback port
  assigned by the Node.js supervisor — ``heddle dev`` does not
  need to know it.
- ``pnpm --filter web dev`` and ``pnpm --filter node dev`` are the
  two npm scripts shipped in ``packages/web/package.json`` /
  ``packages/node/package.json``. ``node dev`` invokes tsx per
  ``packages/node/package.json`` so TypeScript hot reload works
  without extra wiring.
- The daemon uses watchfiles for hot reload; the orchestrator
  passes ``HEDDLE_DAEMON_WATCH=1`` to opt in.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


# ---------- port constants (single source of truth) ----------

#: Vite dev server port — matches ``packages/web/vite.config.ts``.
DEFAULT_VITE_PORT: int = 5173
#: Node.js backend port — matches ``packages/node/src/server.ts``.
DEFAULT_NODE_PORT: int = 5174

#: Env var name toggling the daemon's watchfiles hot reload.
ENV_DAEMON_WATCH: str = "HEDDLE_DAEMON_WATCH"
#: Env var name selecting the Python interpreter for the daemon child.
ENV_PYTHON: str = "HEDDLE_PYTHON"

#: Frontend URL the operator visits after ``heddle dev`` brings up
#: the Vite dev server.
DEV_FRONTEND_URL: str = f"http://localhost:{DEFAULT_VITE_PORT}"


# ---------- errors ----------


class DevCommandError(RuntimeError):
    """Raised on a fatal precondition failure (missing workspace, bad argv)."""


# ---------- child command records ----------


@dataclass(frozen=True)
class ChildCommand:
    """One subprocess the ``dev`` orchestrator should spawn.

    Frozen so a child command cannot be mutated after the layout
    checks pass. ``env`` is a *complete* mapping; the orchestrator
    does not blend it with ``os.environ`` so the child sandbox is
    reproducible per test.
    """

    #: Short identifier for log prefixes and signal routing.
    label: str
    #: Full argv list to pass to ``subprocess.Popen`` (NOT shell-joined).
    argv: tuple[str, ...]
    #: Working directory for the child (``repo_root`` by default).
    cwd: Path
    #: Environment passed verbatim to the child.
    env: Mapping[str, str]


@dataclass(frozen=True)
class DevConfig:
    """Resolved dev-time configuration.

    Holds the three child commands plus the loopback URL the
    operator visits. The orchestrator iterates ``children`` in
    insertion order so log lines stream deterministically (vite
    first, then node, then daemon).
    """

    repo_root: Path
    frontend_url: str
    children: tuple[ChildCommand, ...]

    @property
    def labels(self) -> tuple[str, ...]:
        """The child labels in spawn order — useful in tests."""
        return tuple(child.label for child in self.children)


# ---------- repo layout helpers ----------


def _resolve_web_dir(repo_root: Path) -> Path:
    """Return the Vite workspace directory."""
    return repo_root / "packages" / "web"


def _resolve_node_dir(repo_root: Path) -> Path:
    """Return the Node.js backend workspace directory."""
    return repo_root / "packages" / "node"


# ---------- argv parsing ----------


def _resolve_argv(
    argv: Sequence[str],
    vite_port: int,
    node_port: int,
) -> tuple[int, int]:
    """Parse recognised flags out of argv.

    Recognised: ``--vite-port``, ``--node-port``. Anything else
    raises so a typo never silently falls through (mirrors
    ``start_config._resolve_argv`` discipline).
    """
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--vite-port":
            if i + 1 >= len(argv):
                raise DevCommandError("--vite-port requires a value")
            try:
                vite_port = int(argv[i + 1])
            except ValueError as exc:
                raise DevCommandError(
                    f"--vite-port {argv[i + 1]!r} is not an integer"
                ) from exc
            if not (1 <= vite_port <= 65535):
                raise DevCommandError(
                    f"--vite-port {vite_port} is outside [1, 65535]"
                )
            i += 2
            continue
        if token == "--node-port":
            if i + 1 >= len(argv):
                raise DevCommandError("--node-port requires a value")
            try:
                node_port = int(argv[i + 1])
            except ValueError as exc:
                raise DevCommandError(
                    f"--node-port {argv[i + 1]!r} is not an integer"
                ) from exc
            if not (1 <= node_port <= 65535):
                raise DevCommandError(
                    f"--node-port {node_port} is outside [1, 65535]"
                )
            i += 2
            continue
        raise DevCommandError(f"unknown argument: {token}")
    return vite_port, node_port


# ---------- public resolver ----------


def resolve_config(
    argv: Sequence[str],
    env: Mapping[str, str],
    repo_root: Path,
) -> DevConfig:
    """Resolve the dev config from argv + env.

    The Node.js child runs ``pnpm --filter node dev`` (which invokes
    tsx per ``packages/node/package.json``) so TypeScript hot reload
    works without extra wiring. The daemon child runs
    ``python -m heddle_daemon`` with ``HEDDLE_DAEMON_WATCH=1`` so the
    daemon process reloads on Python edits.
    """
    if not repo_root.exists():
        raise DevCommandError(
            f"repo_root {repo_root} does not exist"
        )
    if not (_resolve_web_dir(repo_root) / "package.json").exists():
        raise DevCommandError(
            f"missing packages/web/package.json under {repo_root}"
        )
    if not (_resolve_node_dir(repo_root) / "package.json").exists():
        raise DevCommandError(
            f"missing packages/node/package.json under {repo_root}"
        )
    vite_port, node_port = _resolve_argv(argv, DEFAULT_VITE_PORT, DEFAULT_NODE_PORT)

    vite_env = dict(env)
    # Vite needs to know the upstream backend port so its proxy is
    # correct; pass it explicitly even when it matches the default.
    vite_env["HEDDLE_NODE_PORT"] = str(node_port)

    node_env = dict(env)
    node_env["HEDDLE_NODE_HOST"] = "127.0.0.1"
    node_env["HEDDLE_NODE_PORT"] = str(node_port)

    daemon_env = dict(env)
    daemon_env["HEDDLE_DAEMON_WATCH"] = "1"
    daemon_env["HEDDLE_DAEMON_HOST"] = "127.0.0.1"

    children: tuple[ChildCommand, ...] = (
        ChildCommand(
            label="vite",
            argv=("pnpm", "--filter", "web", "dev"),
            cwd=_resolve_web_dir(repo_root),
            env=vite_env,
        ),
        ChildCommand(
            label="node",
            argv=("pnpm", "--filter", "node", "dev"),
            cwd=_resolve_node_dir(repo_root),
            env=node_env,
        ),
        ChildCommand(
            label="daemon",
            argv=(env.get(ENV_PYTHON, "python"), "-m", "heddle_daemon"),
            cwd=repo_root,
            env=daemon_env,
        ),
    )
    return DevConfig(
        repo_root=repo_root,
        frontend_url=DEV_FRONTEND_URL,
        children=children,
    )


__all__ = [
    "DEFAULT_NODE_PORT",
    "DEFAULT_VITE_PORT",
    "DEV_FRONTEND_URL",
    "ChildCommand",
    "DevCommandError",
    "DevConfig",
    "ENV_DAEMON_WATCH",
    "ENV_PYTHON",
    "resolve_config",
]
