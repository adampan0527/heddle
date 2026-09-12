# SPDX-License-Identifier: Apache-2.0
"""Test fixture for feat-027 (DaemonSupervisor).

A stand-in for `python -m heddle_daemon` that exposes a WebSocket on
loopback and exhibits three behaviours selected by argv[1]:

    mode-a  bind ws://127.0.0.1:<port>, auto-reply to ping frames, exit
            cleanly after MODE_A_LIFETIME_MS.
    mode-b  bind ws://127.0.0.1:<port>, accept one connection, close it
            immediately.
    mode-c  bind ws://127.0.0.1:<port>, accept connections, ignore ping
            frames (no pongs ever).

Configuration env vars:

    HEDDLE_DAEMON_HOST    defaults to "127.0.0.1"
    HEDDLE_DAEMON_PORT    fixed by the supervisor (no random port — tests
                          need a deterministic port)
    FIXTURE_BOOT_DELAY_MS delay between socket bind and "ready" signal,
                          default 100

The fixture prints `READY <port>` to stdout once the socket is bound.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import NoReturn

from websockets.asyncio.server import ServerConnection, serve


MODE_A_LIFETIME_MS = 2000
FIXTURE_BOOT_DELAY_MS_DEFAULT = 100


def _log(event: str, msg: str, **fields: object) -> None:
    """One-line JSON to stderr — mirrors the daemon's structured-log shape."""
    record: dict[str, object] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": "info",
        "component": "daemon",
        "project_id": None,
        "feature_id": None,
        "event": event,
        "msg": msg,
    }
    record.update(fields)
    sys.stderr.write(json.dumps(record) + "\n")
    sys.stderr.flush()


def _resolve_host_port() -> tuple[str, int]:
    host = os.environ.get("HEDDLE_DAEMON_HOST", "127.0.0.1")
    port_raw = os.environ.get("HEDDLE_DAEMON_PORT", "8765")
    return host, int(port_raw)


# ---------------------------------------------------------------------------
# Mode handlers. Each is a coroutine that runs the mode's WS handler.
# The library auto-replies to client pings by default, so mode-c must
# disable that with ping_interval=None / ping_timeout=None.
# ---------------------------------------------------------------------------


async def _mode_a_handler(ws: ServerConnection) -> None:
    """mode-a: hold connection open until either side closes."""
    try:
        await ws.wait_closed()
    except Exception:  # noqa: BLE001
        pass


async def _mode_b_handler(ws: ServerConnection) -> None:
    """mode-b: close the connection immediately on accept."""
    try:
        await ws.close(code=1011, reason="mode_b_immediate_close")
    except Exception:  # noqa: BLE001
        pass


async def _mode_c_handler(ws: ServerConnection) -> None:
    """mode-c: hold connection open but never reply to pings manually.

    `ping_interval=None / ping_timeout=None` is set on `serve()` so the
    library's auto-pong is disabled — this is how mode-c becomes
    unresponsive.
    """
    try:
        await ws.wait_closed()
    except Exception:  # noqa: BLE001
        pass


def _select_handler(mode: str):
    if mode == "mode-a":
        return _mode_a_handler, {"ping_interval": 20, "ping_timeout": 20}
    if mode == "mode-b":
        return _mode_b_handler, {"ping_interval": 20, "ping_timeout": 20}
    if mode == "mode-c":
        return _mode_c_handler, {"ping_interval": None, "ping_timeout": None}
    raise ValueError(f"unknown mode: {mode}")


async def _run(mode: str) -> NoReturn:
    host, port = _resolve_host_port()
    boot_delay_ms = int(
        os.environ.get("FIXTURE_BOOT_DELAY_MS", FIXTURE_BOOT_DELAY_MS_DEFAULT)
    )

    handler, kwargs = _select_handler(mode)
    _log(
        "fixture_binding",
        f"mode={mode} host={host} port={port}",
        mode=mode,
        host=host,
        port=port,
    )

    # `serve()` blocks until `server.close()` is called. We schedule that
    # for mode-a after MODE_A_LIFETIME_MS; modes b/c run until killed.
    async with serve(handler, host=host, port=port, **kwargs) as server:
        print(f"READY {port}", flush=True)
        if boot_delay_ms > 0:
            await asyncio.sleep(boot_delay_ms / 1000.0)

        _log(f"fixture_{mode}_started", f"{mode}: serving")

        if mode == "mode-a":
            # Auto-exit after the configured lifetime.
            await asyncio.sleep(MODE_A_LIFETIME_MS / 1000.0)
            _log(
                "fixture_mode_a_exiting",
                f"mode-a: lifetime {MODE_A_LIFETIME_MS}ms elapsed",
            )
            server.close()
        else:
            # mode-b and mode-c: stay alive until the supervisor kills us.
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            _log(f"fixture_{mode}_stopped", f"{mode}: server closed")

    sys.exit(0)


def main() -> NoReturn:
    if len(sys.argv) < 2:
        _log("fixture_missing_mode", "usage: fake_daemon.py <mode-a|mode-b|mode-c>")
        sys.exit(2)
    mode = sys.argv[1]
    try:
        asyncio.run(_run(mode))
    except KeyboardInterrupt:
        _log("fixture_interrupted", "received KeyboardInterrupt")
        sys.exit(0)


if __name__ == "__main__":
    main()