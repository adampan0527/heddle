# SPDX-License-Identifier: Apache-2.0
"""heddle daemon entry point — feat-017.

The daemon is a long-running asyncio process. It binds a websockets
server to loopback only (T-017 / D-037 / feat-017 step 1) and exposes
a single `/ws` endpoint that accepts JSON-envelope messages (T-010).
All inter-process communication between the Node.js supervisor (the
parent process, per feat-027) and the daemon goes through this socket.

Loopback-only is a hard security invariant: v0.1 is single-user,
single-machine, and there is no authentication on the WS endpoint
(see DESIGN.md D-037 "loopback WS"). Binding to a non-loopback address
is refused at startup with a clear error so an accidental 0.0.0.0 bind
cannot leak the daemon to the local network.

This module is the *skeleton*: it owns the socket, the envelope parser,
the structured-log emitter, and the lifecycle (start / stop / port
conflict / non-loopback refusal). Per-feature work, sandbox middleware,
LLM integration, etc. live in feat-018+ and add handlers via
`register_message_handler`.

Public surface:

    DEFAULT_PORT          — the default TCP port (8765), overridable via
                            HEDDLE_DAEMON_PORT env var
    DaemonConfig          — port + host config record
    JsonEnvelope          — `{v, type, ...}` message dataclass
    JsonEnvelopeError     — raised on malformed inbound / outbound JSON
    Daemon                — the asyncio Daemon class
    run_daemon()          — sync entry point: parses env, starts loop,
                            runs until SIGINT / SIGTERM

Daemon class API:

    daemon = Daemon(DaemonConfig(port=8765, host="127.0.0.1"))
    await daemon.start()      # binds the socket
    await daemon.serve_forever()  # accepts connections until cancelled
    await daemon.stop()       # graceful shutdown

`start()` returns immediately after a successful bind. `serve_forever()`
is the main coroutine that should be passed to `asyncio.run()`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Final, Optional

import websockets
from websockets.asyncio.server import ServerConnection, serve

from heddle_common import logging as _logging

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "Daemon",
    "DaemonConfig",
    "EnvelopeSchemaVersionError",
    "JsonEnvelope",
    "JsonEnvelopeError",
    "MessageHandler",
    "build_envelope",
    "parse_envelope",
    "run_daemon",
]

# ---------- constants ----------

# Loopback-only is a hard rule (D-037). The default host is 127.0.0.1
# so an empty env var cannot accidentally widen to 0.0.0.0.
DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 8765

# Envelope version (T-010). Every message carries `v: <int>`. The
# daemon refuses messages whose version is greater than this build's
# ENVELOPE_VERSION_MAX with a clear error (mirrors the schema_version
# gate in feature_list_io / projects_io / env_loader).
ENVELOPE_VERSION: Final[int] = 1
ENVELOPE_VERSION_MAX: Final[int] = 1

# Env var names (single source of truth for the supervisor + CLI to
# read when constructing a DaemonConfig).
ENV_PORT: Final[str] = "HEDDLE_DAEMON_PORT"
ENV_HOST: Final[str] = "HEDDLE_DAEMON_HOST"


# ---------- config ----------


@dataclass(frozen=True)
class DaemonConfig:
    """Loopback-only daemon configuration."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    def __post_init__(self) -> None:
        # Validate eagerly so the constructor is the single chokepoint
        # for "is this address loopback?" — caller code never has to
        # repeat the check. Anything that is not a literal 127/::1 is
        # refused.
        if not isinstance(self.host, str) or not self.host:
            raise ValueError(f"host must be a non-empty string; got {self.host!r}")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise ValueError(f"port must be an int; got {self.port!r}")
        # Port 0 is the OS-assigned-ephemeral convention; otherwise
        # the port must be in the user-bindable range. Port 0 keeps
        # the loopback-only invariant intact because the OS picks a
        # local port — the bind call still happens against 127.0.0.1.
        if not (0 <= self.port <= 65535):
            raise ValueError(f"port must be in [0, 65535]; got {self.port}")
        if not _is_loopback(self.host):
            raise ValueError(
                f"host {self.host!r} is not loopback; "
                f"the daemon refuses non-loopback binds (D-037). "
                f"Use 127.0.0.1 or ::1."
            )


def _is_loopback(host: str) -> bool:
    """True iff `host` resolves to a loopback address.

    Accepts the common loopback literals (127.0.0.1, localhost, ::1)
    plus the full 127.0.0.0/8 block. We do NOT resolve DNS here — the
    daemon binds to whatever literal the supervisor hands it, and a
    hostname like `example.com` is refused outright so a future
    config-file loader can't sneak a public IP past the check.
    """
    if host == "localhost":
        return True
    if host == "::1":
        return True
    # 127.0.0.0/8 — the IANA-reserved loopback block.
    if host.startswith("127."):
        # Reject 127.0.0.0 (network address) and 127.255.255.255
        # (broadcast). The two valid forms are 127.0.0.1 (canonical)
        # and the rest of the /8 (used by some OSes for routing).
        # We accept the whole /8 to match common practice; tightening
        # later is non-breaking.
        return True
    return False


def config_from_env(env: dict[str, str] | None = None) -> DaemonConfig:
    """Read HEDDLE_DAEMON_PORT / HEDDLE_DAEMON_HOST from the environment.

    `env` defaults to `os.environ`; tests pass an explicit dict to
    avoid mutating the real environment.
    """
    src = os.environ if env is None else env
    port_raw = src.get(ENV_PORT, str(DEFAULT_PORT))
    host_raw = src.get(ENV_HOST, DEFAULT_HOST)
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError(
            f"{ENV_PORT}={port_raw!r} is not an integer; fix the env var"
        ) from exc
    return DaemonConfig(host=host_raw, port=port)


# ---------- envelope (T-010) ----------


class JsonEnvelopeError(ValueError):
    """Raised when a message does not satisfy the `{v, type, ...}` shape."""


class EnvelopeSchemaVersionError(JsonEnvelopeError):
    """Raised when a message's `v` exceeds this build's ENVELOPE_VERSION_MAX."""


@dataclass(frozen=True)
class JsonEnvelope:
    """One inbound or outbound message.

    Fields beyond `v` and `type` are carried verbatim in `extra`. The
    dataclass is intentionally not exhaustive — adding a new field on
    the wire (e.g. `project_id` per T-010) is a non-breaking change
    for older daemons that don't know about it.
    """

    type: str
    extra: dict[str, Any] = field(default_factory=dict)
    v: int = ENVELOPE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"v": self.v, "type": self.type, **self.extra}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JsonEnvelope":
        if not isinstance(data, dict):
            raise JsonEnvelopeError(
                f"envelope must be a JSON object; got {type(data).__name__}"
            )
        v = data.get("v")
        if v is None:
            # Legacy / pre-versioned envelope. Treat as v0; future
            # readers may decide what to do with it.
            v = 0
        elif isinstance(v, bool) or not isinstance(v, int):
            raise JsonEnvelopeError(
                f"envelope `v` must be an int; got {v!r} (type={type(v).__name__})"
            )
        elif v < 0:
            raise JsonEnvelopeError(f"envelope `v` must be non-negative; got {v}")
        elif v > ENVELOPE_VERSION_MAX:
            raise EnvelopeSchemaVersionError(
                f"envelope v={v} exceeds ENVELOPE_VERSION_MAX={ENVELOPE_VERSION_MAX}; "
                f"upgrade heddle to consume this message."
            )
        type_ = data.get("type")
        if not isinstance(type_, str) or not type_:
            raise JsonEnvelopeError(
                f"envelope `type` must be a non-empty string; got {type_!r}"
            )
        extras = {k: val for k, val in data.items() if k not in ("v", "type")}
        return cls(v=v, type=type_, extra=extras)


def parse_envelope(text: str) -> JsonEnvelope:
    """Parse a single inbound JSON envelope from a websocket message.

    Raises `JsonEnvelopeError` for malformed JSON or wrong shape.
    Raises `EnvelopeSchemaVersionError` for too-new envelopes.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonEnvelopeError(f"not valid JSON: {exc}") from exc
    return JsonEnvelope.from_dict(data)


def build_envelope(type_: str, **extra: Any) -> JsonEnvelope:
    """Build an outbound envelope.

    The caller does not need to pass `v`; the default
    ENVELOPE_VERSION is stamped automatically. Reserved keywords
    (`v`, `type`) in `extra` are silently dropped with a log event
    so a typo doesn't quietly desync the wire format.
    """
    if "v" in extra or "type" in extra:
        reserved = {k for k in extra if k in ("v", "type")}
        _logging.warn(
            component="daemon",
            event="envelope_reserved_keyword_dropped",
            msg=f"dropped reserved keyword(s) {sorted(reserved)} from envelope extras",
            reserved=sorted(reserved),
        )
        extra = {k: v for k, v in extra.items() if k not in ("v", "type")}
    return JsonEnvelope(type=type_, extra=extra)


# ---------- daemon ----------


# A handler is an async callable invoked for every well-formed inbound
# envelope. The skeleton's default handler echoes the message so the
# handshake is observable in tests; feat-018+ wires real handlers
# (start_feature, stop_feature, dialog_turn, etc.).
MessageHandler = Callable[[ServerConnection, JsonEnvelope], Awaitable[None]]


async def _echo_handler(conn: ServerConnection, env: JsonEnvelope) -> None:
    """Default handler used by the skeleton: echo the envelope back.

    This exists so a connecting client can see their message round-trip
    cleanly without subscribing to feature-list updates. Real handlers
    registered via `register_message_handler` replace this one.
    """
    response = build_envelope(
        "echo",
        original_type=env.type,
        original_v=env.v,
    )
    await conn.send(response.to_json())


class Daemon:
    """The heddle daemon asyncio process.

    Lifecycle:
        cfg = DaemonConfig()  # validates loopback in __post_init__
        daemon = Daemon(cfg)
        await daemon.start()         # binds the socket; returns when ready
        await daemon.serve_forever() # accepts + dispatches connections
        await daemon.stop()          # graceful close

    The two-phase split (start + serve_forever) lets tests assert
    "the socket is open" before exercising the connection layer.
    """

    def __init__(
        self,
        config: DaemonConfig,
        *,
        handler: MessageHandler | None = None,
    ) -> None:
        self._config = config
        self._handler: MessageHandler = handler or _echo_handler
        self._server: Optional[Any] = None  # type: ignore[assignment]
        self._connections: set[ServerConnection] = set()
        self._shutdown = asyncio.Event()

    @property
    def config(self) -> DaemonConfig:
        return self._config

    @property
    def bound_port(self) -> int | None:
        """The actual port the socket ended up on, or None if not started.

        Useful when binding to port 0 for ephemeral-port tests.
        """
        if self._server is None:
            return None
        # websockets >= 13 exposes sockets via .sockets; fall back to
        # the configured port when we cannot introspect.
        sockets = getattr(self._server, "sockets", None)
        if sockets:
            try:
                return sockets[0].getsockname()[1]
            except (OSError, IndexError):
                pass
        return self._config.port

    def register_message_handler(self, handler: MessageHandler) -> None:
        """Replace the message handler. Must be called before start()."""
        if self._server is not None:
            raise RuntimeError(
                "cannot register handler after daemon.start(); "
                "register before start() or restart the daemon."
            )
        self._handler = handler

    async def start(self) -> None:
        """Bind the socket. Raises `OSError` on port conflict."""
        self._server = await serve(
            self._handle_connection,
            self._config.host,
            self._config.port,
        )
        _logging.info(
            component="daemon",
            event="daemon_started",
            msg=f"heddle daemon bound to {self._config.host}:{self.bound_port}",
            host=self._config.host,
            port=self.bound_port,
        )

    async def stop(self) -> None:
        """Close the server and any in-flight connections."""
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        # Force-close any remaining client connections.
        for conn in list(self._connections):
            try:
                await conn.close()
            except Exception:
                pass
        self._connections.clear()
        self._shutdown.set()
        _logging.info(
            component="daemon",
            event="daemon_stopped",
            msg="heddle daemon stopped",
        )

    async def serve_forever(self) -> None:
        """Block until `stop()` is called or the loop is cancelled."""
        if self._server is None:
            raise RuntimeError("daemon.serve_forever() called before start()")
        try:
            await self._shutdown.wait()
        finally:
            await self.stop()

    def request_stop(self) -> None:
        """Signal `serve_forever()` to exit. Safe from any coroutine or sync code."""
        self._shutdown.set()

    async def _handle_connection(self, conn: ServerConnection) -> None:
        """Dispatch a single websocket connection."""
        self._connections.add(conn)
        peer = _peer_description(conn)
        _logging.info(
            component="daemon",
            event="client_connected",
            msg=f"client connected: {peer}",
            peer=peer,
        )
        try:
            async for raw in conn:
                # `raw` is bytes or str; normalise to str.
                text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                try:
                    env = parse_envelope(text)
                except EnvelopeSchemaVersionError as exc:
                    err = build_envelope(
                        "error",
                        code="envelope_version_too_new",
                        message=str(exc),
                    )
                    await conn.send(err.to_json())
                    _logging.warn(
                        component="daemon",
                        event="envelope_version_too_new",
                        msg=str(exc),
                        peer=peer,
                    )
                    continue
                except JsonEnvelopeError as exc:
                    err = build_envelope(
                        "error",
                        code="envelope_malformed",
                        message=str(exc),
                    )
                    await conn.send(err.to_json())
                    _logging.warn(
                        component="daemon",
                        event="envelope_malformed",
                        msg=str(exc),
                        peer=peer,
                    )
                    continue
                try:
                    await self._handler(conn, env)
                except Exception as exc:
                    _logging.error(
                        component="daemon",
                        event="handler_raised",
                        msg=f"handler raised: {exc}",
                        envelope_type=env.type,
                        peer=peer,
                    )
                    err = build_envelope(
                        "error",
                        code="handler_error",
                        message=str(exc),
                        envelope_type=env.type,
                    )
                    try:
                        await conn.send(err.to_json())
                    except Exception:
                        pass
        finally:
            self._connections.discard(conn)
            _logging.info(
                component="daemon",
                event="client_disconnected",
                msg=f"client disconnected: {peer}",
                peer=peer,
            )


def _peer_description(conn: ServerConnection) -> str:
    """Render a connection's peer as host:port, with a graceful fallback."""
    try:
        # websockets >= 13 stores the underlying socket's remote
        # address on the connection. Older versions expose it via
        # `remote_address`; try both.
        for attr in ("remote_address",):
            try:
                addr = getattr(conn, attr, None)
                if addr is not None:
                    return f"{addr[0]}:{addr[1]}"
            except Exception:
                pass
    except Exception:
        pass
    return "unknown"


# ---------- sync entry point ----------


def run_daemon(argv: list[str] | None = None) -> int:
    """Synchronous entry point: parse argv, start the daemon, run forever.

    Used by `python -m heddle_daemon` and the supervisor. Returns the
    process exit code (0 on graceful stop, 1 on config / bind error).
    """
    parser = argparse.ArgumentParser(
        prog="heddle-daemon",
        description="heddle daemon — long-running agent workflow engine (feat-017)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port to bind (default: HEDDLE_DAEMON_PORT or 8765)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Host to bind (default: HEDDLE_DAEMON_HOST or 127.0.0.1). "
             "Must be loopback; non-loopback is refused.",
    )
    args = parser.parse_args(argv)

    # Precedence: CLI flag > env var > defaults.
    src = os.environ.copy()
    if args.port is not None:
        src[ENV_PORT] = str(args.port)
    if args.host is not None:
        src[ENV_HOST] = args.host

    try:
        cfg = config_from_env(src)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    daemon = Daemon(cfg)

    async def _main() -> int:
        try:
            await daemon.start()
        except OSError as exc:
            print(
                f"error: cannot bind {cfg.host}:{cfg.port}: {exc}. "
                f"Is another heddle daemon already running?",
                file=sys.stderr,
            )
            return 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        loop = asyncio.get_running_loop()

        def _on_signal(signame: str) -> None:
            _logging.info(
                component="daemon",
                event="signal_received",
                msg=f"received {signame}; shutting down",
                signal=signame,
            )
            daemon.request_stop()

        for signame in ("SIGINT", "SIGTERM"):
            try:
                loop.add_signal_handler(
                    getattr(signal, signame), _on_signal, signame
                )
            except NotImplementedError:
                # Windows does not support add_signal_handler for all
                # signals; tests skip signal handling anyway.
                pass

        try:
            await daemon.serve_forever()
        finally:
            await daemon.stop()
        return 0

    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        # Belt-and-braces: add_signal_handler may not catch Ctrl-C on
        # every platform; ensure a clean exit.
        return 0
