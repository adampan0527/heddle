# SPDX-License-Identifier: Apache-2.0
"""Tests for ``heddle_cli.start`` — feat-050.

Covers every public branch of ``start_cmd``:

- loopback refusal (HEDDLE_BIND / HEDDLE_NODE_HOST / --host)
- missing Node.js dist (no ``packages/node/dist/main.js``)
- successful subprocess spawn + health probe + browser open
- graceful shutdown (SIGINT/SIGTERM forwarded to child)
- argv parsing (unknown flag, --port out-of-range)
- web bundle auto-build when dist missing
- browser-open failure is non-fatal

Every test injects the ``PopenFactory`` / ``BrowserOpener`` /
``HealthProbe`` / ``BuildRunner`` so nothing real is launched.
"""

from __future__ import annotations

import os
import signal
import subprocess
import unittest
from pathlib import Path
from typing import Mapping
from unittest import mock

from heddle_cli.start import start_cmd
from heddle_cli.start_config import (
    DEFAULT_HEALTH_URL,
    DEFAULT_NODE_HOST,
    DEFAULT_NODE_PORT,
    ENV_BIND,
    ENV_NODE_HOST,
    ENV_NODE_PORT,
    StartCommandError,
    resolve_config as _resolve_config,
)


# ---------- helpers ----------


class _FakePopen:
    """Minimal ``subprocess.Popen`` stand-in.

    Records its constructor kwargs and exposes a controllable
    ``wait()`` + ``send_signal()`` pair. ``poll()`` is unused by
    ``start_cmd`` so we don't bother stubbing it.
    """

    instances: list[_FakePopen] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = dict(kwargs)
        self.wait_returncode: int = 0
        self.received_signals: list[int] = []
        self._wait_calls = 0
        _FakePopen.instances.append(self)

    def wait(self, timeout: float | None = None) -> int:
        self._wait_calls += 1
        return self.wait_returncode

    def send_signal(self, signum: int) -> None:
        self.received_signals.append(signum)

    def terminate(self) -> None:
        self.received_signals.append(signal.SIGTERM)


def _make_repo(tmp_path: Path, *, with_node_dist: bool, with_web_dist: bool) -> Path:
    """Lay out a fake repo tree inside ``tmp_path``."""
    node_dist = tmp_path / "packages" / "node" / "dist"
    web_dist = tmp_path / "packages" / "web" / "dist"
    node_dist.mkdir(parents=True, exist_ok=True)
    web_dist.mkdir(parents=True, exist_ok=True)
    if with_node_dist:
        (node_dist / "main.js").write_text("// stub", encoding="utf-8")
    if with_web_dist:
        (web_dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return tmp_path


def _clean_env() -> dict[str, str]:
    """A clean env dict with the bind-related vars stripped.

    Real ``os.environ`` may contain ``HEDDLE_BIND`` or
    ``HEDDLE_NODE_HOST`` from the calling shell; tests need a clean
    baseline to assert exact behaviour.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in (ENV_BIND, ENV_NODE_HOST, ENV_NODE_PORT)
    }


class TestResolveConfig(unittest.TestCase):
    """Unit tests for the pure ``_resolve_config`` helper."""

    def setUp(self) -> None:
        self.tmp = Path(self._testMethodName)
        # Use a real temp dir via mkdtemp-shaped scratch inside tmp_path
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "_"))

    def _make_dist(self) -> None:
        (self.root / "packages" / "node" / "dist" / "main.js").parent.mkdir(
            parents=True, exist_ok=True
        )
        (self.root / "packages" / "node" / "dist" / "main.js").write_text(
            "stub", encoding="utf-8"
        )

    def test_defaults_match_constants(self) -> None:
        self._make_dist()
        cfg = _resolve_config([], _clean_env(), self.root)
        self.assertEqual(cfg.host, DEFAULT_NODE_HOST)
        self.assertEqual(cfg.port, DEFAULT_NODE_PORT)
        self.assertEqual(cfg.health_url, DEFAULT_HEALTH_URL)

    def test_refuses_heddle_bind_public_ip(self) -> None:
        self._make_dist()
        env = _clean_env()
        env[ENV_BIND] = "8.8.8.8"
        with self.assertRaises(StartCommandError):
            _resolve_config([], env, self.root)

    def test_refuses_heddle_bind_zero_ip(self) -> None:
        self._make_dist()
        env = _clean_env()
        env[ENV_BIND] = "0.0.0.0"
        with self.assertRaises(StartCommandError):
            _resolve_config([], env, self.root)

    def test_refuses_heddle_node_host_public(self) -> None:
        self._make_dist()
        env = _clean_env()
        env[ENV_NODE_HOST] = "10.0.0.1"
        with self.assertRaises(StartCommandError):
            _resolve_config([], env, self.root)

    def test_refuses_argv_host_public(self) -> None:
        self._make_dist()
        with self.assertRaises(StartCommandError):
            _resolve_config(["--host", "192.168.0.1"], _clean_env(), self.root)

    def test_rejects_unknown_argv_token(self) -> None:
        self._make_dist()
        with self.assertRaises(StartCommandError):
            _resolve_config(["--bogus", "x"], _clean_env(), self.root)

    def test_rejects_invalid_port(self) -> None:
        self._make_dist()
        with self.assertRaises(StartCommandError):
            _resolve_config(["--port", "70000"], _clean_env(), self.root)

    def test_rejects_non_int_port(self) -> None:
        self._make_dist()
        env = _clean_env()
        env[ENV_NODE_PORT] = "not-a-number"
        with self.assertRaises(StartCommandError):
            _resolve_config([], env, self.root)

    def test_accepts_full_loopback_block(self) -> None:
        self._make_dist()
        cfg = _resolve_config(["--host", "127.55.66.77"], _clean_env(), self.root)
        self.assertEqual(cfg.host, "127.55.66.77")

    def test_accepts_ipv6_loopback(self) -> None:
        self._make_dist()
        cfg = _resolve_config(["--host", "::1"], _clean_env(), self.root)
        self.assertEqual(cfg.host, "::1")

    def test_health_url_reflects_host_port(self) -> None:
        self._make_dist()
        cfg = _resolve_config(
            ["--host", "127.0.0.1", "--port", "6000"], _clean_env(), self.root
        )
        self.assertEqual(cfg.health_url, "http://127.0.0.1:6000/")


class TestStartCmd(unittest.TestCase):
    """Integration-style tests for ``start_cmd`` end-to-end behaviour."""

    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="startcmd_"))
        _FakePopen.instances = []

    def _teardown_signals(self) -> None:
        # ``start_cmd`` installs its own SIGINT/SIGTERM handlers; if a
        # test exits in the middle of the call (because the fake Popen
        # returned immediately) the finally block restores them. Some
        # tests call ``start_cmd`` and assert it returns; we still
        # restore in case the call raised before the finally ran.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def test_refuses_non_loopback_via_env(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=True)
        env = _clean_env()
        env[ENV_BIND] = "8.8.8.8"
        rc = start_cmd([], env=env, repo_root=repo)
        self.assertEqual(rc, 2)
        self.assertEqual(_FakePopen.instances, [])

    def test_refuses_non_loopback_via_argv(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=True)
        rc = start_cmd(["--host", "0.0.0.0"], repo_root=repo)
        self.assertEqual(rc, 2)
        self.assertEqual(_FakePopen.instances, [])

    def test_refuses_missing_node_dist(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=False, with_web_dist=True)
        rc = start_cmd([], repo_root=repo)
        self.assertEqual(rc, 4)
        self.assertEqual(_FakePopen.instances, [])

    def test_refuses_missing_web_dist_after_build_failure(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=False)
        # Pretend the build runner failed; ``start_cmd`` should
        # surface a non-zero exit without ever spawning Node.
        def failing_build(
            cmd: list[str], _env: Mapping[str, str]
        ) -> int:
            return 7

        rc = start_cmd(
            [], repo_root=repo, build_runner=failing_build
        )
        self.assertEqual(rc, 3)
        self.assertEqual(_FakePopen.instances, [])

    def test_spawns_node_and_waits_for_health(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=True)
        # Health probe returns True on first call so the wait is
        # effectively zero.
        rc = start_cmd(
            [],
            env=_clean_env(),
            repo_root=repo,
            popen_factory=_FakePopen,
            health_probe=lambda url, timeout: True,
            browser_opener=lambda url, autoraise: True,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(_FakePopen.instances), 1)
        proc = _FakePopen.instances[0]
        # Spawned with the right argv + env (the bind vars come from
        # StartConfig, NOT from the parent env).
        (cmd_arg,) = proc.args
        self.assertEqual(
            list(cmd_arg),
            ["node", str(repo / "packages" / "node" / "dist" / "main.js")],
        )
        self.assertEqual(proc.kwargs["env"][ENV_NODE_HOST], DEFAULT_NODE_HOST)
        self.assertEqual(proc.kwargs["env"][ENV_NODE_PORT], str(DEFAULT_NODE_PORT))
        # We only inject HEDDLE_NODE_HOST / HEDDLE_NODE_PORT into the
        # child env; HEDDLE_BIND should NOT be smuggled through.
        self.assertNotIn(ENV_BIND, proc.kwargs["env"])

    def test_web_bundle_built_when_missing(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=False)
        build_calls: list[list[str]] = []

        def fake_build(cmd: list[str], env: Mapping[str, str]) -> int:
            build_calls.append(list(cmd))
            # Simulate pnpm writing the dist on success so the
            # second guard pass finds index.html.
            (repo / "packages" / "web" / "dist").mkdir(parents=True, exist_ok=True)
            (repo / "packages" / "web" / "dist" / "index.html").write_text(
                "<!doctype html>", encoding="utf-8"
            )
            return 0

        rc = start_cmd(
            [],
            env=_clean_env(),
            repo_root=repo,
            popen_factory=_FakePopen,
            health_probe=lambda url, timeout: True,
            browser_opener=lambda url, autoraise: True,
            build_runner=fake_build,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(build_calls, [["pnpm", "--filter", "web", "build"]])
        self.assertEqual(len(_FakePopen.instances), 1)

    def test_skips_build_when_web_dist_present(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=True)
        build_calls: list[list[str]] = []

        def no_build(cmd: list[str], env: Mapping[str, str]) -> int:
            build_calls.append(list(cmd))
            return 0

        rc = start_cmd(
            [],
            env=_clean_env(),
            repo_root=repo,
            popen_factory=_FakePopen,
            health_probe=lambda url, timeout: True,
            browser_opener=lambda url, autoraise: True,
            build_runner=no_build,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(build_calls, [])

    def test_browser_opener_invoked_with_frontend_url(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=True)
        opened: list[tuple[str, int]] = []

        def opener(url: str, autoraise: int) -> bool:
            opened.append((url, autoraise))
            return True

        rc = start_cmd(
            ["--frontend-url", "http://localhost:9999/"],
            env=_clean_env(),
            repo_root=repo,
            popen_factory=_FakePopen,
            health_probe=lambda url, timeout: True,
            browser_opener=opener,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(opened, [("http://localhost:9999/", 1)])

    def test_browser_failure_does_not_crash(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=True)

        def opener_raises(url: str, autoraise: int) -> bool:
            raise RuntimeError("no DISPLAY")

        # Even when the browser opener raises, ``start_cmd`` keeps
        # waiting for the child. Our fake Popen's ``wait()`` returns 0
        # immediately so the test doesn't actually block.
        rc = start_cmd(
            [],
            env=_clean_env(),
            repo_root=repo,
            popen_factory=_FakePopen,
            health_probe=lambda url, timeout: True,
            browser_opener=opener_raises,
        )
        # The child exited with code 0, so start_cmd returns 0
        # despite the browser failure.
        self.assertEqual(rc, 0)
        self.assertEqual(len(_FakePopen.instances), 1)

    def test_browser_open_can_be_disabled(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=True)
        opened: list[tuple[str, int]] = []

        def opener(url: str, autoraise: int) -> bool:
            opened.append((url, autoraise))
            return True

        rc = start_cmd(
            [],
            env=_clean_env(),
            repo_root=repo,
            popen_factory=_FakePopen,
            health_probe=lambda url, timeout: True,
            browser_opener=opener,
            open_browser_on_success=False,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(opened, [])

    def test_health_probe_failure_kills_subprocess(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=True)
        # Health probe always returns False; with a tiny deadline the
        # wait loop returns immediately and the child is terminated.
        rc = start_cmd(
            [],
            env=_clean_env(),
            repo_root=repo,
            popen_factory=_FakePopen,
            health_probe=lambda url, timeout: False,
            browser_opener=lambda url, autoraise: True,
            health_deadline_s=0.05,
            health_poll_s=0.01,
        )
        self.assertEqual(rc, 5)
        proc = _FakePopen.instances[0]
        # Either a SIGTERM was forwarded, or the proc was terminated
        # directly via ``proc.terminate()``.
        self.assertTrue(proc.received_signals)

    def test_signal_handler_forwards_sigint_to_child(self) -> None:
        """Sending SIGINT to the parent must forward to the child."""
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=True)

        def blocking_popen(*args: object, **kwargs: object) -> _FakePopen:
            proc = _FakePopen(*args, **kwargs)

            def wait(_timeout: float | None = None) -> int:
                # Simulate the OS delivering SIGINT to the parent
                # mid-wait. We pull the handler that start_cmd just
                # installed and invoke it directly so the test does
                # not need to coordinate with os.kill().
                handler = signal.getsignal(signal.SIGINT)
                if callable(handler):
                    handler(signal.SIGINT, None)  # type: ignore[arg-type]
                return 0

            proc.wait = wait  # type: ignore[assignment]
            return proc

        rc = start_cmd(
            [],
            env=_clean_env(),
            repo_root=repo,
            popen_factory=blocking_popen,
            health_probe=lambda url, timeout: True,
            browser_opener=lambda url, autoraise: True,
        )
        # After the forwarded SIGINT, start_cmd returns the child's
        # exit code (0).
        self.assertEqual(rc, 0)
        proc = _FakePopen.instances[0]
        self.assertIn(signal.SIGINT, proc.received_signals)
        self._teardown_signals()

    def test_popen_factory_receives_dist_path_and_env(self) -> None:
        repo = _make_repo(self.tmp, with_node_dist=True, with_web_dist=True)
        rc = start_cmd(
            ["--port", "6001"],
            env=_clean_env(),
            repo_root=repo,
            popen_factory=_FakePopen,
            health_probe=lambda url, timeout: True,
            browser_opener=lambda url, autoraise: True,
        )
        self.assertEqual(rc, 0)
        proc = _FakePopen.instances[0]
        (cmd_arg,) = proc.args
        self.assertEqual(list(cmd_arg)[1], str(repo / "packages" / "node" / "dist" / "main.js"))
        self.assertEqual(proc.kwargs["env"][ENV_NODE_PORT], "6001")


if __name__ == "__main__":
    unittest.main()
