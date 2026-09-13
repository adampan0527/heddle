# SPDX-License-Identifier: Apache-2.0
"""Default collaborators for ``heddle dev`` — feat-051 (T-019).

Holds the *real-world* side-effecting implementations of the four
collaborator types the dev command accepts as injection points:

- ``PopenFactoryLike`` — wraps ``subprocess.Popen`` so the
  orchestrator never imports ``subprocess`` directly (keeps it
  testable).
- ``LineStreamer`` — pumps one child's stdout/stderr through a
  prefixing callback line-by-line; tests substitute a recording
  version that captures lines into a list instead of printing.
- ``ProxyConfigReader`` — reads ``packages/web/vite.config.ts``
  and asserts both ``/api`` and ``/ws`` proxies point at the
  Node.js backend. The orchestrator refuses to start dev mode
  when the proxy is missing so a misconfigured ``vite.config.ts``
  fails loudly instead of silently proxying to nowhere.

Kept separate from ``dev.py`` so the orchestrator stays focused
on control flow and each module remains under the 200-line cap
from CODE_STYLE.md.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


# ---------- subprocess factory ----------


def default_popen(*args: object, **kwargs: object) -> subprocess.Popen:
    """Real ``subprocess.Popen``; only used when no factory is injected.

    The ``args[0]`` and ``kwargs`` types are deliberately ``object``
    because the orchestrator hands in tuples + ``Mapping[str, str]``
    that the type-checker cannot otherwise prove compatible with
    ``subprocess.Popen``'s precise ``Sequence[str]`` signature.
    """
    return subprocess.Popen(*args, **dict(kwargs))  # type: ignore[arg-type]


# Sentinel type alias — referenced in __all__ for parity with start.
PopenFactoryLike = Callable[..., subprocess.Popen]


# ---------- log streamer ----------


#: Type alias for the prefix-printing callback. Receives the
#: line-with-prefix and is expected to print it (or record it).
LineSink = Callable[[str], None]


def _drain(stream: Iterable[str], label: str, sink: LineSink) -> None:
    """Synchronously drain ``stream`` into ``sink``.

    Splits on full lines so multi-line buffered output is emitted
    one line at a time with the ``[label]`` prefix applied
    uniformly. Stops when the iterator is exhausted (the child
    closed its stdout/stderr). Used as the target of a background
    thread in the real implementation; tests call it inline.
    """
    prefix = f"[{label}] "
    for raw in stream:
        for line in raw.splitlines() or [raw]:
            sink(f"{prefix}{line}")


def default_line_streamer(
    label: str,
    stdout: Iterable[str] | None,
    stderr: Iterable[str] | None,
    sink: LineSink = print,
) -> None:
    """Stream one child's stdout and stderr through ``sink``.

    ``stdout`` / ``stderr`` are typically file-like objects from a
    ``Popen`` (open in text mode with ``bufsize=1``). They are
    declared as ``Iterable[str]`` so tests can pass in plain
    iterables of pre-recorded lines.
    """
    if stdout is not None:
        _drain(stdout, label, sink)
    if stderr is not None:
        _drain(stderr, label, sink)


# ---------- Vite proxy config reader ----------


#: Match ``"/api": { target: "..." }`` blocks inside vite.config.ts.
_API_PROXY_RE = re.compile(
    r'"\s*/api\s*"\s*:\s*\{[^}]*?target\s*:\s*"([^"]+)"',
    re.DOTALL,
)
#: Match ``"/ws": { target: "...", ws: true }`` blocks.
_WS_PROXY_RE = re.compile(
    r'"\s*/ws\s*"\s*:\s*\{[^}]*?target\s*:\s*"(wss?://[^"]+)"',
    re.DOTALL,
)
#: Match the ``ws: true`` flag inside the ``/ws`` proxy block.
_WS_FLAG_RE = re.compile(
    r'"\s*/ws\s*"\s*:[^}]*?ws\s*:\s*true',
    re.DOTALL,
)


@dataclass(frozen=True)
class ProxyConfig:
    """Result of probing ``vite.config.ts`` for the dev proxies.

    ``api_target`` and ``ws_target`` carry the literal target
    strings (e.g. ``http://localhost:5174`` and
    ``ws://localhost:5174``). ``has_ws_flag`` is True iff the
    ``/ws`` block enables ``ws: true`` — Vite uses that flag to
    upgrade HTTP to WebSocket.
    """

    api_target: str | None
    ws_target: str | None
    has_ws_flag: bool


def _read_text(path: Path) -> str:
    """Read a UTF-8 text file; missing files return empty string."""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_proxy_config(vite_config_path: Path) -> ProxyConfig:
    """Parse the proxy blocks from ``vite.config.ts``.

    Returns a ``ProxyConfig`` with whichever proxies are present.
    The orchestrator treats ``api_target is None or ws_target is
    None`` as a fatal precondition failure. The ``has_ws_flag``
    boolean reports whether the ``ws: true`` upgrade flag is set
    (Vite requires it for WebSocket proxying).
    """
    body = _read_text(vite_config_path)
    api_match = _API_PROXY_RE.search(body)
    ws_match = _WS_PROXY_RE.search(body)
    has_ws = bool(_WS_FLAG_RE.search(body))
    return ProxyConfig(
        api_target=api_match.group(1) if api_match else None,
        ws_target=ws_match.group(1) if ws_match else None,
        has_ws_flag=has_ws,
    )


def default_proxy_reader(vite_config_path: Path) -> ProxyConfig:
    """Real proxy reader; thin wrapper so tests can substitute."""
    return read_proxy_config(vite_config_path)


def assert_proxy_matches_backend(
    proxy: ProxyConfig,
    *,
    expected_host: str,
    expected_port: int,
) -> tuple[str, str] | None:
    """Validate proxy targets point at the loopback backend.

    Returns ``None`` when both proxies target the expected backend,
    or a ``(reason, detail)`` tuple describing the first mismatch.
    Pure function — no I/O — so tests can drive it directly.
    """
    expected_api = f"http://{expected_host}:{expected_port}"
    expected_ws = f"ws://{expected_host}:{expected_port}"
    if proxy.api_target != expected_api:
        return (
            "api_proxy_mismatch",
            f"/api target {proxy.api_target!r} != {expected_api!r}",
        )
    if proxy.ws_target != expected_ws:
        return (
            "ws_proxy_mismatch",
            f"/ws target {proxy.ws_target!r} != {expected_ws!r}",
        )
    if not proxy.has_ws_flag:
        return (
            "ws_flag_missing",
            "/ws proxy block is missing `ws: true` (WebSocket upgrade)",
        )
    return None


__all__ = [
    "LineSink",
    "PopenFactoryLike",
    "ProxyConfig",
    "ProxyConfigReaderLike",  # type: ignore[name-defined]
    "assert_proxy_matches_backend",
    "default_line_streamer",
    "default_popen",
    "default_proxy_reader",
    "read_proxy_config",
]


#: Alias for the proxy-config reader — references the function
#: shape ``Callable[[Path], ProxyConfig]``. Tests can substitute
#: a stub returning a fixed ``ProxyConfig`` without going through
#: disk I/O.
ProxyConfigReaderLike = Callable[[Path], ProxyConfig]
