# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_daemon.server — feat-017."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

import websockets

from heddle_daemon.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    Daemon,
    DaemonConfig,
    EnvelopeSchemaVersionError,
    JsonEnvelope,
    JsonEnvelopeError,
    build_envelope,
    config_from_env,
    parse_envelope,
    _is_loopback,
)

# Path helpers for the port-conflict test below.
SCRATCH_DIR = Path(sys.argv[0]).resolve().parent


class TestConfig(unittest.TestCase):
    """DaemonConfig validation + loopback enforcement."""

    def test_defaults_to_loopback_and_default_port(self):
        cfg = DaemonConfig()
        self.assertEqual(cfg.host, DEFAULT_HOST)
        self.assertEqual(cfg.port, DEFAULT_PORT)

    def test_refuses_non_loopback_host(self):
        with self.assertRaises(ValueError) as ctx:
            DaemonConfig(host="0.0.0.0")
        self.assertIn("loopback", str(ctx.exception).lower())

    def test_refuses_public_ip(self):
        with self.assertRaises(ValueError) as ctx:
            DaemonConfig(host="8.8.8.8")
        self.assertIn("loopback", str(ctx.exception).lower())

    def test_accepts_127_0_0_1(self):
        cfg = DaemonConfig(host="127.0.0.1")
        self.assertEqual(cfg.host, "127.0.0.1")

    def test_accepts_ipv6_loopback(self):
        cfg = DaemonConfig(host="::1")
        self.assertEqual(cfg.host, "::1")

    def test_accepts_localhost(self):
        cfg = DaemonConfig(host="localhost")
        self.assertEqual(cfg.host, "localhost")

    def test_accepts_full_127_8_block(self):
        # 127.0.0.0/8 is the IANA loopback block; the daemon binds
        # any /8 literal because some OSes route them.
        cfg = DaemonConfig(host="127.55.66.77")
        self.assertEqual(cfg.host, "127.55.66.77")

    def test_refuses_invalid_port(self):
        with self.assertRaises(ValueError):
            DaemonConfig(port=70000)
        with self.assertRaises(ValueError):
            DaemonConfig(port=-1)
        # Port 0 IS allowed: it asks the OS for an ephemeral port.
        # The loopback check still gates the host.
        cfg = DaemonConfig(port=0)
        self.assertEqual(cfg.port, 0)

    def test_refuses_non_int_port(self):
        with self.assertRaises(ValueError):
            DaemonConfig(port="8765")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            DaemonConfig(port=True)  # type: ignore[arg-type]

    def test_refuses_empty_host(self):
        with self.assertRaises(ValueError):
            DaemonConfig(host="")

    def test_is_loopback_helper(self):
        self.assertTrue(_is_loopback("127.0.0.1"))
        self.assertTrue(_is_loopback("::1"))
        self.assertTrue(_is_loopback("localhost"))
        self.assertTrue(_is_loopback("127.0.0.99"))
        self.assertFalse(_is_loopback("0.0.0.0"))
        self.assertFalse(_is_loopback("10.0.0.1"))
        self.assertFalse(_is_loopback("example.com"))

    def test_config_from_env_reads_overrides(self):
        cfg = config_from_env({"HEDDLE_DAEMON_PORT": "9999", "HEDDLE_DAEMON_HOST": "127.0.0.1"})
        self.assertEqual(cfg.port, 9999)
        self.assertEqual(cfg.host, "127.0.0.1")

    def test_config_from_env_falls_back_to_defaults(self):
        cfg = config_from_env({})
        self.assertEqual(cfg.port, DEFAULT_PORT)

    def test_config_from_env_rejects_non_loopback(self):
        with self.assertRaises(ValueError):
            config_from_env({"HEDDLE_DAEMON_HOST": "0.0.0.0"})

    def test_config_from_env_rejects_non_int_port(self):
        with self.assertRaises(ValueError):
            config_from_env({"HEDDLE_DAEMON_PORT": "not-a-number"})


class TestEnvelope(unittest.TestCase):
    """JsonEnvelope parse / build round-trip + version gating."""

    def test_round_trip(self):
        env = build_envelope("hello", project_id="abc", feature_id="feat-001")
        text = env.to_json()
        parsed = parse_envelope(text)
        self.assertEqual(parsed.type, "hello")
        self.assertEqual(parsed.extra.get("project_id"), "abc")
        self.assertEqual(parsed.extra.get("feature_id"), "feat-001")
        self.assertEqual(parsed.v, 1)

    def test_parse_envelope_validates_v_is_int(self):
        with self.assertRaises(JsonEnvelopeError):
            parse_envelope(json.dumps({"v": "1", "type": "x"}))
        with self.assertRaises(JsonEnvelopeError):
            parse_envelope(json.dumps({"v": True, "type": "x"}))
        with self.assertRaises(JsonEnvelopeError):
            parse_envelope(json.dumps({"v": -1, "type": "x"}))

    def test_parse_envelope_rejects_unknown_future_version(self):
        with self.assertRaises(EnvelopeSchemaVersionError) as ctx:
            parse_envelope(json.dumps({"v": 999, "type": "x"}))
        self.assertIn("upgrade", str(ctx.exception).lower())

    def test_parse_envelope_defaults_missing_v_to_zero(self):
        env = parse_envelope(json.dumps({"type": "x"}))
        self.assertEqual(env.v, 0)

    def test_parse_envelope_requires_type(self):
        with self.assertRaises(JsonEnvelopeError):
            parse_envelope(json.dumps({"v": 1}))
        with self.assertRaises(JsonEnvelopeError):
            parse_envelope(json.dumps({"v": 1, "type": ""}))

    def test_parse_envelope_rejects_non_object(self):
        with self.assertRaises(JsonEnvelopeError):
            parse_envelope("[1, 2, 3]")
        with self.assertRaises(JsonEnvelopeError):
            parse_envelope("null")

    def test_parse_envelope_rejects_invalid_json(self):
        with self.assertRaises(JsonEnvelopeError):
            parse_envelope("not json at all")

    def test_build_envelope_drops_reserved_keywords(self):
        """A caller that passes `v=` or `type=` to build_envelope would
        otherwise clobber the typed fields silently. We drop them
        with a warning instead — the test confirms the typed fields
        are still correct.
        """
        env = build_envelope("real_type", v=999, type="shadowed")
        self.assertEqual(env.type, "real_type")
        self.assertNotEqual(env.v, 999)
        # The dropped keys are NOT in extras either.
        self.assertNotIn("type", env.extra)

    def test_from_dict_preserves_unknown_keys_in_extras(self):
        env = JsonEnvelope.from_dict(
            {"v": 1, "type": "ping", "project_id": "p", "extra": 42}
        )
        self.assertEqual(env.extra["project_id"], "p")
        self.assertEqual(env.extra["extra"], 42)


class TestDaemonLifecycle(unittest.TestCase):
    """Async start / stop / port-conflict / WS handshake tests."""

    def _ephemeral_port(self) -> int:
        """Return a port number unlikely to be in use.

        The kernel will assign a real ephemeral port when bind(0) is
        called; the test only needs a port that does not collide with
        another suite.
        """
        return 0

    async def _async_start_and_handshake(self) -> None:
        """Start the daemon on an ephemeral port and connect.

        step 5: start daemon; connect a websocket client from
        127.0.0.1; assert handshake succeeds.
        """
        daemon = Daemon(DaemonConfig(port=self._ephemeral_port()))
        await daemon.start()
        try:
            self.assertIsNotNone(daemon.bound_port)
            actual_port = daemon.bound_port
            self.assertGreater(actual_port, 0)
            # The default handler echoes the message; verify the
            # round-trip works end-to-end (parse → handler → reply).
            async with websockets.connect(f"ws://127.0.0.1:{actual_port}") as conn:
                await conn.send(build_envelope("ping", payload="hello").to_json())
                reply_text = await asyncio.wait_for(conn.recv(), timeout=2.0)
                reply = parse_envelope(reply_text)
                self.assertEqual(reply.type, "echo")
                self.assertEqual(reply.extra.get("original_type"), "ping")
        finally:
            await daemon.stop()

    def test_start_handshake_echo(self):
        """step 5: start daemon; connect a websocket client; handshake succeeds."""
        asyncio.run(self._async_start_and_handshake())

    async def _async_port_conflict(self) -> None:
        """step 6: start daemon on port 8765 twice; second exits with port-conflict error.

        We use an ephemeral port (the daemon binds port=0) to avoid
        clashing with a real long-running instance, then ask a second
        daemon to bind the SAME resolved port. The second daemon's
        start() must raise OSError.
        """
        # Start first daemon on an ephemeral port.
        first = Daemon(DaemonConfig(port=0))
        await first.start()
        try:
            actual_port = first.bound_port
            self.assertIsNotNone(actual_port)
            # Second daemon tries to bind that exact port — must fail.
            second = Daemon(DaemonConfig(port=actual_port))
            with self.assertRaises(OSError) as ctx:
                await second.start()
            # The OSError is EADDRINUSE on POSIX; on Windows it can
            # also be WSAEADDRINUSE. Just assert the OSError fires.
            self.assertIsInstance(ctx.exception, OSError)
            # The second daemon should NOT have a server attached.
            self.assertIsNone(second._server)  # noqa: SLF001 — test introspection
        finally:
            await first.stop()

    def test_port_conflict_raises_oserror(self):
        """step 6: two daemons on the same port → second raises OSError."""
        asyncio.run(self._async_port_conflict())

    async def _async_handler_error(self) -> None:
        """If a registered handler raises, the daemon replies with an
        error envelope and keeps the connection alive."""
        async def bad_handler(conn, env):
            raise RuntimeError("simulated handler explosion")

        daemon = Daemon(DaemonConfig(port=0), handler=bad_handler)
        await daemon.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{daemon.bound_port}") as conn:
                await conn.send(build_envelope("trigger_bug").to_json())
                reply_text = await asyncio.wait_for(conn.recv(), timeout=2.0)
                reply = parse_envelope(reply_text)
                self.assertEqual(reply.type, "error")
                self.assertEqual(reply.extra.get("code"), "handler_error")
        finally:
            await daemon.stop()

    def test_handler_exception_is_surfaced_as_error_envelope(self):
        asyncio.run(self._async_handler_error())

    async def _async_malformed_envelope(self) -> None:
        daemon = Daemon(DaemonConfig(port=0))
        await daemon.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{daemon.bound_port}") as conn:
                await conn.send("not json at all")
                reply_text = await asyncio.wait_for(conn.recv(), timeout=2.0)
                reply = parse_envelope(reply_text)
                self.assertEqual(reply.type, "error")
                self.assertEqual(reply.extra.get("code"), "envelope_malformed")
        finally:
            await daemon.stop()

    def test_malformed_envelope_returns_error_envelope(self):
        asyncio.run(self._async_malformed_envelope())

    async def _async_version_too_new(self) -> None:
        daemon = Daemon(DaemonConfig(port=0))
        await daemon.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{daemon.bound_port}") as conn:
                await conn.send(json.dumps({"v": 999, "type": "future_msg"}))
                reply_text = await asyncio.wait_for(conn.recv(), timeout=2.0)
                reply = parse_envelope(reply_text)
                self.assertEqual(reply.type, "error")
                self.assertEqual(reply.extra.get("code"), "envelope_version_too_new")
        finally:
            await daemon.stop()

    def test_version_too_new_returns_error_envelope(self):
        asyncio.run(self._async_version_too_new())

    def test_register_handler_after_start_raises(self):
        """The handler must be set before start() — runtime swap is not supported."""
        async def runner():
            daemon = Daemon(DaemonConfig(port=0))
            await daemon.start()
            try:
                async def h(conn, env):
                    pass
                with self.assertRaises(RuntimeError):
                    daemon.register_message_handler(h)
            finally:
                await daemon.stop()
        asyncio.run(runner())


if __name__ == "__main__":
    unittest.main()
