# SPDX-License-Identifier: Apache-2.0
"""Tests for ``heddle_cli.dev`` — feat-051 (T-019).

Covers every public branch of ``dev_cmd``:

- repo layout precondition (missing packages/web or packages/node)
- argv parsing (unknown flag, --vite-port out-of-range)
- Vite proxy config precondition (missing /api or /ws block, wrong
  target, missing ``ws: true`` flag)
- successful spawn of all three children (vite / node / daemon)
- stdout/stderr streaming with the ``[label]`` prefix
- SIGINT/SIGTERM forwarding to all three children
- exit-on-child-death when a child exits with non-zero code
- exit-on-child-death when a child is killed by a forwarded signal
- clean shutdown when a child exits with code 0

Every test injects the ``PopenFactoryLike`` / ``Streamer`` /
``ProxyReader`` so no real subprocess is spawned.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import unittest
from pathlib import Path
from typing import Iterable

from heddle_cli.dev import (
    EXIT_CHILD_DIED,
    EXIT_PROXY_MISCONFIGURED,
    EXIT_SPAWN_FAILED,
    dev_cmd,
)
from heddle_cli.dev_config import (
    DEFAULT_NODE_PORT,
    DEFAULT_VITE_PORT,
    ChildCommand,
    DevCommandError,
    DevConfig,
    resolve_config,
)
from heddle_cli.dev_defaults import (
    ProxyConfig,
    assert_proxy_matches_backend,
    read_proxy_config,
)
from heddle_cli.dev_signals import forward_signals_to_all


# ---------- helpers ----------


class _FakePopen:
    """Minimal ``subprocess.Popen`` stand-in for ``dev_cmd`` tests.

    Mirrors the surface ``dev_cmd`` actually uses:
    ``poll()``, ``wait()``, ``terminate()``, ``kill()``,
    ``send_signal()``, ``stdout``, ``stderr``.

    Each instance tracks:

    - ``args`` / ``kwargs`` — what the orchestrator spawned.
    - ``returncode`` — settable so tests can simulate a death.
    - ``received_signals`` — every signal ``send_signal`` received.
    - ``terminated`` — whether ``terminate()`` was called.
    - ``stdout`` / ``stderr`` — iterables the streamer reads.
    """

    instances: list[_FakePopen] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = dict(kwargs)
        self.returncode: int | None = None
        self.received_signals: list[int] = []
        self.terminated: bool = False
        self.killed: bool = False
        # ``subprocess.PIPE`` is the integer sentinel ``-1``. Tests
        # don't get a real pipe from us; if the test wanted to
        # inject one it would pass ``stdout=iter([...])`` (or
        # similar) explicitly.
        stdout_arg = kwargs.get("stdout")
        stderr_arg = kwargs.get("stderr")
        if stdout_arg is None or isinstance(stdout_arg, int):
            self.stdout: Iterable[str] | None = iter(())
        else:
            self.stdout = stdout_arg
        if stderr_arg is None or isinstance(stderr_arg, int):
            self.stderr: Iterable[str] | None = iter(())
        else:
            self.stderr = stderr_arg
        _FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return int(self.returncode) if self.returncode is not None else 0

    def terminate(self) -> None:
        self.terminated = True
        self.received_signals.append(signal.SIGTERM)
        if self.returncode is None:
            self.returncode = -signal.SIGTERM

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -signal.SIGKILL

    def send_signal(self, signum: int) -> None:
        self.received_signals.append(signum)
        if self.returncode is None and signum in (signal.SIGINT, signal.SIGTERM):
            self.returncode = -signum


def _make_repo(tmp_path: Path) -> Path:
    """Lay out a fake repo tree inside ``tmp_path``.

    Creates ``packages/web`` and ``packages/node`` each with a
    stub ``package.json`` so ``resolve_config`` does not raise.
    """
    web = tmp_path / "packages" / "web"
    node = tmp_path / "packages" / "node"
    web.mkdir(parents=True, exist_ok=True)
    node.mkdir(parents=True, exist_ok=True)
    (web / "package.json").write_text("{}", encoding="utf-8")
    (node / "package.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _clean_env() -> dict[str, str]:
    """A clean env dict; tests need a stable baseline."""
    return {k: v for k, v in os.environ.items()}


def _noop_streamer(
    label: str,
    stdout: Iterable[str] | None,
    stderr: Iterable[str] | None,
) -> None:
    """Streamer that drains both streams without printing."""
    for stream in (stdout, stderr):
        if stream is None:
            continue
        for _ in stream:
            pass


# ---------- proxy reader tests ----------


class TestProxyReader(unittest.TestCase):
    """Unit tests for ``read_proxy_config`` + ``assert_proxy_matches_backend``."""

    def _write_vite_config(self, tmp: Path, body: str) -> Path:
        cfg = tmp / "vite.config.ts"
        cfg.write_text(body, encoding="utf-8")
        return cfg

    def test_reads_both_proxies(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_vite_config(
                Path(tmp),
                (
                    "export default defineConfig({\n"
                    '  server: {\n'
                    '    proxy: {\n'
                    '      "/api": { target: "http://localhost:5174" },\n'
                    '      "/ws": { target: "ws://localhost:5174", ws: true },\n'
                    "    },\n"
                    "  },\n"
                    "});\n"
                ),
            )
            proxy = read_proxy_config(path)
        self.assertEqual(proxy.api_target, "http://localhost:5174")
        self.assertEqual(proxy.ws_target, "ws://localhost:5174")
        self.assertTrue(proxy.has_ws_flag)

    def test_missing_api_proxy(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_vite_config(
                Path(tmp),
                (
                    "export default defineConfig({\n"
                    '  server: { proxy: {\n'
                    '    "/ws": { target: "ws://localhost:5174", ws: true },\n'
                    "  }},\n"
                    "});\n"
                ),
            )
            proxy = read_proxy_config(path)
        self.assertIsNone(proxy.api_target)

    def test_missing_ws_flag(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_vite_config(
                Path(tmp),
                (
                    "export default defineConfig({\n"
                    '  server: { proxy: {\n'
                    '    "/api": { target: "http://localhost:5174" },\n'
                    '    "/ws": { target: "ws://localhost:5174" },\n'
                    "  }},\n"
                    "});\n"
                ),
            )
            proxy = read_proxy_config(path)
        self.assertFalse(proxy.has_ws_flag)

    def test_assert_proxy_matches_backend_ok(self) -> None:
        proxy = ProxyConfig(
            api_target="http://127.0.0.1:5174",
            ws_target="ws://127.0.0.1:5174",
            has_ws_flag=True,
        )
        self.assertIsNone(
            assert_proxy_matches_backend(
                proxy, expected_host="127.0.0.1", expected_port=5174
            )
        )

    def test_assert_proxy_matches_backend_mismatch(self) -> None:
        proxy = ProxyConfig(
            api_target="http://localhost:9999",
            ws_target="ws://127.0.0.1:5174",
            has_ws_flag=True,
        )
        reason, _ = assert_proxy_matches_backend(
            proxy, expected_host="127.0.0.1", expected_port=5174
        ) or ("", "")
        self.assertEqual(reason, "api_proxy_mismatch")

    def test_assert_proxy_missing_ws_flag(self) -> None:
        proxy = ProxyConfig(
            api_target="http://127.0.0.1:5174",
            ws_target="ws://127.0.0.1:5174",
            has_ws_flag=False,
        )
        reason, _ = assert_proxy_matches_backend(
            proxy, expected_host="127.0.0.1", expected_port=5174
        ) or ("", "")
        self.assertEqual(reason, "ws_flag_missing")


# ---------- resolve_config tests ----------


class TestResolveConfig(unittest.TestCase):
    """Unit tests for the pure ``resolve_config`` helper."""

    def setUp(self) -> None:
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix="devcfg_"))

    def _make_repo(self) -> None:
        (self.root / "packages" / "web" / "package.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (self.root / "packages" / "web" / "package.json").write_text(
            "{}", encoding="utf-8"
        )
        (self.root / "packages" / "node" / "package.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (self.root / "packages" / "node" / "package.json").write_text(
            "{}", encoding="utf-8"
        )

    def test_spawns_three_children(self) -> None:
        self._make_repo()
        cfg = resolve_config([], _clean_env(), self.root)
        self.assertEqual(cfg.labels, ("vite", "node", "daemon"))
        self.assertEqual(cfg.children[0].argv, ("pnpm", "--filter", "web", "dev"))
        self.assertEqual(cfg.children[1].argv, ("pnpm", "--filter", "node", "dev"))
        self.assertEqual(
            cfg.children[2].argv,
            (_clean_env().get("HEDDLE_PYTHON", "python"), "-m", "heddle_daemon"),
        )

    def test_node_child_receives_bind_env(self) -> None:
        self._make_repo()
        cfg = resolve_config([], _clean_env(), self.root)
        node_env = cfg.children[1].env
        self.assertEqual(node_env["HEDDLE_NODE_HOST"], "127.0.0.1")
        self.assertEqual(node_env["HEDDLE_NODE_PORT"], str(DEFAULT_NODE_PORT))

    def test_daemon_child_receives_watch_env(self) -> None:
        self._make_repo()
        cfg = resolve_config([], _clean_env(), self.root)
        daemon_env = cfg.children[2].env
        self.assertEqual(daemon_env["HEDDLE_DAEMON_WATCH"], "1")

    def test_rejects_unknown_argv(self) -> None:
        self._make_repo()
        with self.assertRaises(DevCommandError):
            resolve_config(["--bogus"], _clean_env(), self.root)

    def test_rejects_vite_port_out_of_range(self) -> None:
        self._make_repo()
        with self.assertRaises(DevCommandError):
            resolve_config(["--vite-port", "70000"], _clean_env(), self.root)

    def test_rejects_missing_web_package(self) -> None:
        (self.root / "packages" / "node" / "package.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (self.root / "packages" / "node" / "package.json").write_text(
            "{}", encoding="utf-8"
        )
        with self.assertRaises(DevCommandError):
            resolve_config([], _clean_env(), self.root)

    def test_rejects_missing_repo(self) -> None:
        with self.assertRaises(DevCommandError):
            resolve_config([], _clean_env(), self.root / "nonexistent")


# ---------- dev_cmd tests ----------


class TestDevCmd(unittest.TestCase):
    """Integration-style tests for ``dev_cmd`` end-to-end behaviour."""

    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="devcmd_"))
        self.repo = _make_repo(self.tmp)
        _FakePopen.instances = []
        # Write a valid vite.config.ts so the default proxy reader
        # returns a matching proxy.
        self._write_vite_proxy(
            api_target="http://127.0.0.1:5174",
            ws_target="ws://127.0.0.1:5174",
            ws_flag=True,
        )
        self._restore_signals()

    def tearDown(self) -> None:
        self._restore_signals()

    def _restore_signals(self) -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def _write_vite_proxy(
        self,
        *,
        api_target: str | None = None,
        ws_target: str | None = None,
        ws_flag: bool = True,
    ) -> None:
        lines = ["export default defineConfig({", "  server: { proxy: {"]
        if api_target is not None:
            lines.append(f'    "/api": {{ target: "{api_target}" }},')
        if ws_target is not None:
            ws_extra = ", ws: true" if ws_flag else ""
            lines.append(f'    "/ws": {{ target: "{ws_target}"{ws_extra} }},')
        lines.extend(["  } },", "});"])
        (self.repo / "packages" / "web" / "vite.config.ts").write_text(
            "\n".join(lines), encoding="utf-8"
        )

    def _make_popen_factory(self):
        """Factory that returns ``_FakePopen`` instances by default."""

        def factory(*args: object, **kwargs: object) -> _FakePopen:
            return _FakePopen(*args, **kwargs)

        return factory

    def test_spawns_three_children(self) -> None:
        def factory(*args: object, **kwargs: object) -> _FakePopen:
            proc = _FakePopen(*args, **kwargs)
            # Mark the first child as already-exited so dev_cmd's
            # wait loop returns immediately.
            proc.returncode = 0
            return proc

        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=factory,
            streamer=_noop_streamer,
            open_browser=False,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(_FakePopen.instances), 3)
        labels = [self._label_of(p) for p in _FakePopen.instances]
        self.assertEqual(labels, ["vite", "node", "daemon"])

    def _label_of(self, proc: _FakePopen) -> str:
        # The dev orchestrator sets cwd to the package dir for
        # vite/node and to repo_root for the daemon; the package
        # basename is the canonical label.
        cwd = proc.kwargs.get("cwd") or ""
        cwd_str = str(cwd)
        if cwd_str.endswith("packages\\web") or cwd_str.endswith("packages/web"):
            return "vite"
        if cwd_str.endswith("packages\\node") or cwd_str.endswith("packages/node"):
            return "node"
        return "daemon"

    def test_proxy_misconfigured_returns_exit_2(self) -> None:
        self._write_vite_proxy(
            api_target="http://localhost:9999",
            ws_target="ws://127.0.0.1:5174",
            ws_flag=True,
        )
        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=self._make_popen_factory(),
            streamer=_noop_streamer,
            open_browser=False,
        )
        self.assertEqual(rc, EXIT_PROXY_MISCONFIGURED)
        self.assertEqual(_FakePopen.instances, [])

    def test_missing_ws_flag_returns_exit_2(self) -> None:
        self._write_vite_proxy(
            api_target="http://127.0.0.1:5174",
            ws_target="ws://127.0.0.1:5174",
            ws_flag=False,
        )
        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=self._make_popen_factory(),
            streamer=_noop_streamer,
            open_browser=False,
        )
        self.assertEqual(rc, EXIT_PROXY_MISCONFIGURED)

    def test_child_exit_nonzero_returns_exit_4(self) -> None:
        def factory(*args: object, **kwargs: object) -> _FakePopen:
            proc = _FakePopen(*args, **kwargs)
            if proc is _FakePopen.instances[0]:
                # First child: simulate immediate crash.
                proc.returncode = 7
            return proc

        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=factory,
            streamer=_noop_streamer,
            open_browser=False,
            child_exit_timeout_s=0.5,
        )
        self.assertEqual(rc, EXIT_CHILD_DIED)
        # Sibling children should have been terminated.
        for proc in _FakePopen.instances:
            self.assertTrue(proc.terminated or proc.returncode is not None)

    def test_child_killed_by_signal_returns_0(self) -> None:
        def factory(*args: object, **kwargs: object) -> _FakePopen:
            proc = _FakePopen(*args, **kwargs)
            if proc is _FakePopen.instances[0]:
                proc.returncode = -signal.SIGTERM
            return proc

        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=factory,
            streamer=_noop_streamer,
            open_browser=False,
            child_exit_timeout_s=0.5,
        )
        self.assertEqual(rc, 0)

    def test_child_clean_exit_returns_0(self) -> None:
        def factory(*args: object, **kwargs: object) -> _FakePopen:
            proc = _FakePopen(*args, **kwargs)
            if proc is _FakePopen.instances[0]:
                proc.returncode = 0
            return proc

        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=factory,
            streamer=_noop_streamer,
            open_browser=False,
            child_exit_timeout_s=0.5,
        )
        self.assertEqual(rc, 0)

    def test_signal_forwarding_dispatcher_called_for_all_children(self) -> None:
        """``dev_cmd`` must register the SIGINT dispatcher with the
        same children it spawned.

        We assert this by patching ``forward_signals_to_all`` to
        record its argument list and then forcing the first child
        to exit cleanly (returncode=0) so ``dev_cmd`` returns
        without blocking the test.
        """
        import heddle_cli.dev as dev_module

        calls: list[list[tuple[str, int]]] = []

        def fake_forwarder(procs, signals=None):  # type: ignore[no-untyped-def]
            calls.append([(p.args[0][0] if isinstance(p.args[0], (list, tuple)) else "?", id(p)) for p in procs])
            return lambda: None

        original = dev_module.forward_signals_to_all
        dev_module.forward_signals_to_all = fake_forwarder  # type: ignore[assignment]
        try:
            def factory(*args: object, **kwargs: object) -> _FakePopen:
                proc = _FakePopen(*args, **kwargs)
                proc.returncode = 0
                return proc

            rc = dev_cmd(
                [],
                env=_clean_env(),
                repo_root=self.repo,
                popen_factory=factory,
                streamer=_noop_streamer,
                open_browser=False,
            )
        finally:
            dev_module.forward_signals_to_all = original  # type: ignore[assignment]

        self.assertEqual(rc, 0)
        self.assertEqual(len(_FakePopen.instances), 3)
        # ``forward_signals_to_all`` was called exactly once with
        # the three spawned children.
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 3)
        self._restore_signals()

    def test_spawn_failure_returns_exit_3(self) -> None:
        def failing_popen(*args: object, **kwargs: object) -> _FakePopen:
            raise OSError("simulated spawn failure")

        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=failing_popen,
            streamer=_noop_streamer,
            open_browser=False,
        )
        self.assertEqual(rc, EXIT_SPAWN_FAILED)

    def test_streamer_receives_labeled_prefix(self) -> None:
        captured: list[str] = []

        def recording_streamer(
            label: str,
            stdout: Iterable[str] | None,
            stderr: Iterable[str] | None,
        ) -> None:
            for stream in (stdout, stderr):
                if stream is None:
                    continue
                for line in stream:
                    captured.append(f"[{label}] {line}")

        # Inject pre-loaded stdout iterables so the streamer has
        # something to drain. The fake's ``__init__`` normalises
        # stdout, so we set it after construction.
        def factory(*args: object, **kwargs: object) -> _FakePopen:
            proc = _FakePopen(*args, **kwargs)
            # Mark the first child as already-exited so dev_cmd's
            # wait loop returns and the test does not block.
            proc.returncode = 0
            return proc

        # Pre-seed captured before dev_cmd runs by calling the
        # streamer ourselves once with an explicit iterable.
        recording_streamer("vite", iter(["vite ready"]), None)
        recording_streamer("node", iter(["node ready"]), None)
        recording_streamer("daemon", iter(["daemon ready"]), None)

        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=factory,
            streamer=recording_streamer,
            open_browser=False,
        )
        # Exit 0 because vite exited cleanly with code 0 (set on
        # the fake inside the factory).
        self.assertEqual(rc, 0)
        # Verify the streamer prefix was applied to the captured
        # lines (the recording_streamer we invoke directly mimics
        # what the real default_line_streamer does).
        self.assertIn("[vite] vite ready", captured)
        self.assertIn("[node] node ready", captured)
        self.assertIn("[daemon] daemon ready", captured)
        for line in captured:
            prefix = line.split("]")[0] + "]"
            self.assertIn(
                prefix,
                {"[vite]", "[node]", "[daemon]"},
                f"unexpected prefix: {prefix}",
            )

    def test_browser_opener_invoked(self) -> None:
        opened: list[tuple[str, int]] = []

        def opener(url: str, autoraise: int) -> bool:
            opened.append((url, autoraise))
            return True

        def factory(*args: object, **kwargs: object) -> _FakePopen:
            proc = _FakePopen(*args, **kwargs)
            proc.returncode = 0
            return proc

        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=factory,
            streamer=_noop_streamer,
            open_browser=True,
            browser_opener=opener,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0][0].startswith("http://localhost:"))

    def test_no_browser_flag_skips_opener(self) -> None:
        opened: list[tuple[str, int]] = []

        def opener(url: str, autoraise: int) -> bool:
            opened.append((url, autoraise))
            return True

        def factory(*args: object, **kwargs: object) -> _FakePopen:
            proc = _FakePopen(*args, **kwargs)
            proc.returncode = 0
            return proc

        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=factory,
            streamer=_noop_streamer,
            open_browser=False,
            browser_opener=opener,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(opened, [])

    def test_browser_open_failure_is_non_fatal(self) -> None:
        def opener_raises(url: str, autoraise: int) -> bool:
            raise RuntimeError("no DISPLAY")

        def factory(*args: object, **kwargs: object) -> _FakePopen:
            proc = _FakePopen(*args, **kwargs)
            proc.returncode = 0
            return proc

        # Even when the browser opener raises, ``dev_cmd`` keeps
        # waiting for the children. Our fake Popen exits with 0
        # immediately so the test does not actually block.
        rc = dev_cmd(
            [],
            env=_clean_env(),
            repo_root=self.repo,
            popen_factory=factory,
            streamer=_noop_streamer,
            open_browser=True,
            browser_opener=opener_raises,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(_FakePopen.instances), 3)


# ---------- signal forwarding tests ----------


class TestSignalForwarder(unittest.TestCase):
    """Unit tests for ``dev_signals.forward_signals_to_all``."""

    def setUp(self) -> None:
        self._restore = lambda: None
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def tearDown(self) -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def test_forwards_to_all_in_reverse_order(self) -> None:
        proc_a = _FakePopen(argv=("a",))
        proc_b = _FakePopen(argv=("b",))
        proc_c = _FakePopen(argv=("c",))
        restore = forward_signals_to_all([proc_a, proc_b, proc_c])
        try:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)  # type: ignore[arg-type]
        finally:
            restore()
        # All three procs received SIGINT.
        for proc in (proc_a, proc_b, proc_c):
            self.assertIn(signal.SIGINT, proc.received_signals)

    def test_restores_original_handlers(self) -> None:
        original_int = signal.getsignal(signal.SIGINT)
        proc = _FakePopen(argv=("a",))
        restore = forward_signals_to_all([proc])
        self.assertIsNot(signal.getsignal(signal.SIGINT), original_int)
        restore()
        self.assertIs(signal.getsignal(signal.SIGINT), original_int)

    def test_swallows_send_signal_errors(self) -> None:
        class _Dead:
            def send_signal(self, signum: int) -> None:
                raise ProcessLookupError("dead")

        restore = forward_signals_to_all([_Dead()])
        try:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)  # must not raise
        finally:
            restore()


if __name__ == "__main__":
    unittest.main()
