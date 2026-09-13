# SPDX-License-Identifier: Apache-2.0
"""Configuration resolution for ``heddle start`` — feat-050.

Holds the constants, types, and pure functions needed to turn argv +
environment variables into a frozen ``StartConfig``. Kept separate
from ``start.py`` so the config layer can be unit-tested in isolation
(no subprocess, no signal handlers, no urllib) and so each module
stays under the 200-line cap from CODE_STYLE.md.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


# ---------- constants (single source of truth) ----------

#: Name of the env var that controls the Node.js backend bind host.
ENV_NODE_HOST: str = "HEDDLE_NODE_HOST"
#: Name of the env var that controls the Node.js backend bind port.
ENV_NODE_PORT: str = "HEDDLE_NODE_PORT"
#: Legacy / upstream name (mirrors daemon env convention) — refused
#: identically to ``HEDDLE_NODE_HOST`` to keep one rule everywhere.
ENV_BIND: str = "HEDDLE_BIND"

#: Default backend host (loopback).
DEFAULT_NODE_HOST: str = "127.0.0.1"
#: Default backend port (matches ``packages/node/src/server.ts``).
DEFAULT_NODE_PORT: int = 5174
#: Default frontend URL the browser is opened to.
#: The Node backend (feat-026) serves BOTH the REST/WS API at
#: ``/api/*`` and ``/ws`` AND the static SPA bundle at every other
#: path (feat-032 web build → packages/web/dist, served via
#: @fastify/static with ``index: ["index.html"]``). So the browser
#: URL points at the SAME port as the backend, not a separate Vite
#: dev server. The 5173 default from earlier drafts predated the
#: ``packages/node serve web dist`` wiring and was a holdover from
#: the dev-mode (pnpm --filter web dev) layout.
DEFAULT_FRONTEND_URL: str = f"http://localhost:{DEFAULT_NODE_PORT}"
#: Default health endpoint polled until the backend is reachable.
DEFAULT_HEALTH_URL: str = f"http://{DEFAULT_NODE_HOST}:{DEFAULT_NODE_PORT}/"


# ---------- errors ----------


class StartCommandError(RuntimeError):
    """Raised on a fatal precondition failure (non-loopback, missing dist)."""


# ---------- config record ----------


@dataclass(frozen=True)
class StartConfig:
    """Resolved start-time configuration.

    Frozen so a misconfigured field can't be mutated after the loopback
    check passes. The Node subprocess receives ``HEDDLE_NODE_HOST`` /
    ``HEDDLE_NODE_PORT`` from these values, never from ``os.environ``
    directly, so a stale export can't slip through.
    """

    host: str
    port: int
    frontend_url: str
    node_dist: Path
    web_dist: Path
    health_url: str


# ---------- loopback / path helpers ----------


def _is_loopback(host: str) -> bool:
    """Loopback check mirroring ``DaemonConfig.__post_init__``.

    Accepts ``localhost``, ``::1`` and the full 127.0.0.0/8 block. We
    deliberately do NOT resolve DNS — the CLI only ever passes a
    literal to the Node process, and a hostname like ``example.com``
    would widen the surface.
    """
    if host == "localhost":
        return True
    if host == "::1":
        return True
    if host.startswith("127."):
        return True
    return False


def _resolve_node_dist(repo_root: Path) -> Path:
    """Return the path to the built Node.js entry.

    ``pnpm --filter node build`` emits the bundle under
    ``packages/node/dist/``; with the workspace's current
    ``tsconfig.json`` (rootDir inferred from the shared ``include``)
    TypeScript nests the output as ``dist/node/src/main.js`` rather
    than the flat ``dist/main.js`` we used to emit. We probe both
    locations and return whichever exists — flat first so a future
    tsconfig cleanup doesn't need an extra CLI change.

    The start command does NOT auto-build the Node bundle — auto-
    building a transpiled-binary sidecar on every ``heddle start`` is
    too surprising; the web bundle auto-builds because it's a single
    ``vite build`` and rarely changes, but the Node transpile cycle
    is a developer action.
    """
    candidates = [
        repo_root / "packages" / "node" / "dist" / "main.js",
        repo_root / "packages" / "node" / "dist" / "node" / "src" / "main.js",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _resolve_web_dist(repo_root: Path) -> Path:
    """Return the directory holding the Vite-built web bundle."""
    return repo_root / "packages" / "web" / "dist"


# ---------- resolution ----------


def _resolve_argv(argv: Sequence[str], host: str, port: int, frontend_url: str) -> tuple[str, int, str]:
    """Parse recognised flags out of argv.

    Returns ``(host, port, frontend_url)`` after applying any
    ``--host`` / ``--port`` / ``--frontend-url`` overrides. Unknown
    flags raise so a typo never silently falls through.
    """
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--host":
            if i + 1 >= len(argv):
                raise StartCommandError("--host requires a value")
            host = argv[i + 1]
            i += 2
            continue
        if token == "--port":
            if i + 1 >= len(argv):
                raise StartCommandError("--port requires a value")
            try:
                port = int(argv[i + 1])
            except ValueError as exc:
                raise StartCommandError(
                    f"--port {argv[i + 1]!r} is not an integer"
                ) from exc
            if not (1 <= port <= 65535):
                raise StartCommandError(
                    f"--port {port} is outside [1, 65535]"
                )
            i += 2
            continue
        if token == "--frontend-url":
            if i + 1 >= len(argv):
                raise StartCommandError("--frontend-url requires a value")
            frontend_url = argv[i + 1]
            i += 2
            continue
        raise StartCommandError(f"unknown argument: {token}")
    return host, port, frontend_url


def _resolve_env_bind(env: Mapping[str, str], host: str) -> str:
    """Read ``HEDDLE_BIND`` or ``HEDDLE_NODE_HOST`` and validate.

    Refuses non-loopback addresses BEFORE anything else runs — a
    misconfigured public bind is a security invariant violation, not a
    runtime error.
    """
    bind_host = env.get(ENV_BIND, host)
    if not _is_loopback(bind_host):
        raise StartCommandError(
            f"{ENV_BIND}={bind_host!r} (or {ENV_NODE_HOST}) is not a "
            f"loopback address; heddle start refuses non-loopback binds. "
            f"Use 127.0.0.1, ::1, or localhost."
        )
    return bind_host


def _resolve_env_port(env: Mapping[str, str]) -> int:
    """Read ``HEDDLE_NODE_PORT`` and validate it is a valid TCP port."""
    port_raw = env.get(ENV_NODE_PORT, str(DEFAULT_NODE_PORT))
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise StartCommandError(
            f"{ENV_NODE_PORT}={port_raw!r} is not an integer"
        ) from exc
    if not (1 <= port <= 65535):
        raise StartCommandError(
            f"{ENV_NODE_PORT}={port} is outside [1, 65535]"
        )
    return port


def resolve_config(
    argv: Sequence[str],
    env: Mapping[str, str],
    repo_root: Path,
) -> StartConfig:
    """Resolve the start config from argv + env.

    Recognised flags: ``--host``, ``--port``, ``--frontend-url``.
    Anything else is rejected so a typo doesn't silently get ignored.
    """
    host = env.get(ENV_NODE_HOST, DEFAULT_NODE_HOST)
    # Validate the env-supplied bind before any argv parsing so a
    # misconfigured export never reaches the subprocess.
    _resolve_env_bind(env, host)
    port = _resolve_env_port(env)
    host, port, frontend_url = _resolve_argv(argv, host, port, DEFAULT_FRONTEND_URL)
    if not _is_loopback(host):
        raise StartCommandError(
            f"--host {host!r} is not a loopback address; heddle start "
            f"refuses non-loopback binds."
        )
    node_dist = _resolve_node_dist(repo_root)
    web_dist = _resolve_web_dist(repo_root)
    health_url = f"http://{host}:{port}/"
    return StartConfig(
        host=host,
        port=port,
        frontend_url=frontend_url,
        node_dist=node_dist,
        web_dist=web_dist,
        health_url=health_url,
    )


def resolve_from_click(
    *,
    host: str | None,
    port: int | None,
    frontend_url: str | None,
    repo_root: Path | None = None,
) -> StartConfig:
    """Click-friendly wrapper that builds argv from explicit kwargs.

    ``__main__.py`` translates Click flags into a small argv list and
    hands it to ``start_cmd``. This helper hides the conversion so
    `__main__.py` doesn't need to know the flag names.
    """
    argv: list[str] = []
    if host is not None:
        argv += ["--host", host]
    if port is not None:
        argv += ["--port", str(port)]
    if frontend_url is not None:
        argv += ["--frontend-url", frontend_url]
    root = repo_root if repo_root is not None else Path.cwd()
    return resolve_config(argv, os.environ, root)
