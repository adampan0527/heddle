# SPDX-License-Identifier: Apache-2.0
"""Tests for ``heddle_cli.test`` — feat-052.

Covers every public branch of ``test_cmd`` and its config helpers:

- argv parsing (--js-skip, --py-skip, --e2e-skip, --real-llm,
  unknown flag → ``EXIT_BAD_CONFIG``)
- per-layer inclusion (JS only when not --js-skip; same for py/e2e)
- stub suites for missing package directories
- Playwright stub detection (probe-true vs probe-false)
- runner injection: every child command is sent through the
  injected ``SuiteRunner`` so no real pnpm / pytest is spawned
- summary table aggregation (PASS / FAIL / SKIP counters)
- HEDDLE_FAKE_LLM=1 injection on every child unless --real-llm
- exit code mapping (EXIT_OK / EXIT_TEST_FAILED / EXIT_BAD_CONFIG)
- ``render_summary`` formatting (header, rows, totals)
- Playwright probe (``default_playwright_probe``) handles JSON5-ish
  ``packages/web/package.json`` content

Every test injects ``runner`` / ``playwright_probe`` / ``summary_sink``
so no real subprocess is launched and no real file is read.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Mapping

from heddle_cli.test import (
    EXIT_BAD_CONFIG,
    EXIT_OK,
    EXIT_TEST_FAILED,
)
from heddle_cli import test as _test_module
from heddle_cli.test_config import (
    ENV_FAKE_LLM,
    JS_PACKAGES,
    PY_PACKAGES,
    TestCommandError,
    resolve_config,
)
from heddle_cli.test_defaults import (
    default_playwright_probe,
    print_summary,
    render_summary,
)


# Tell pytest NOT to try collecting ``TestCommandError`` as a test
# class — it lives in ``heddle_cli.test_config`` and only happens
# to start with the ``Test`` prefix.
TestCommandError.__test__ = False  # type: ignore[attr-defined]


def _test_cmd(*args, **kwargs):
    """Module-level alias for ``heddle_cli.test.test_cmd``.

    Underscored so pytest's ``test_*`` collector does not pick it
    up. Tests reference ``_test_module.test_cmd`` directly
    instead.
    """
    return _test_module._test_module.test_cmd(*args, **kwargs)


# Some pytest versions still try to collect underscored test_* via
# custom hooks; belt-and-braces: mark the helper as not-a-test.
_test_cmd.__test__ = False  # type: ignore[attr-defined]


# ---------- helpers ----------


class _Recorder(list):
    """``list`` subclass that exposes ``append_record`` for tests."""

    def append_record(self, line: str) -> None:
        self.append(line)


def _fake_runner_factory(rcs: list[int]):
    """Return a ``SuiteRunner`` that pops the next ``rc`` per call.

    Records every ``(argv, cwd, env)`` triple so tests can assert
    on what the orchestrator actually invoked.
    """
    calls: list[tuple[list[str], Path, Mapping[str, str]]] = []
    iterator = iter(rcs)

    def runner(argv, *, cwd, env):
        calls.append((list(argv), cwd, dict(env)))
        rc = next(iterator, 0)
        return rc, "", "" if rc == 0 else f"stderr from {argv[0]}"

    return runner, calls


def _no_playwright(_path: Path) -> bool:
    """Probe stub: returns ``False`` so the E2E suite is skipped."""
    return False


def _yes_playwright(_path: Path) -> bool:
    """Probe stub: returns ``True`` so the E2E suite is skipped
    only because Playwright is unwired (not because it's missing).
    """
    return True


def _make_repo(tmp_path: Path) -> Path:
    """Lay out a fake repo tree inside ``tmp_path``.

    Creates ``packages/{web,node}`` with ``package.json`` and
    ``packages/{common,daemon,cli}`` with ``pyproject.toml`` so
    ``resolve_config`` records every suite as ``runnable``
    rather than skipped. ``packages/web`` is needed for the
    Playwright probe to be called even though the probe stub
    decides the result.
    """
    for pkg in JS_PACKAGES:
        d = tmp_path / "packages" / pkg
        d.mkdir(parents=True, exist_ok=True)
        (d / "package.json").write_text("{}", encoding="utf-8")
    for pkg in PY_PACKAGES:
        d = tmp_path / "packages" / pkg
        d.mkdir(parents=True, exist_ok=True)
        (d / "pyproject.toml").write_text("[project]\nname='x'\n",
                                           encoding="utf-8")
    return tmp_path


# ---------- config tests ----------


class TestResolveConfig(unittest.TestCase):
    """Unit tests for ``resolve_config``."""

    def test_all_suites_present(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config([], {}, root)
        labels = [c.label for c in cfg.commands]
        self.assertEqual(
            labels,
            ["js:web", "js:node", "py:common", "py:daemon", "py:cli",
             "e2e:browser"],
        )
        self.assertFalse(cfg.js_skip)
        self.assertFalse(cfg.py_skip)
        self.assertFalse(cfg.e2e_skip)
        self.assertFalse(cfg.real_llm)

    def test_js_skip_drops_js_layer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config(["--js-skip"], {}, root)
        labels = [c.label for c in cfg.commands]
        self.assertNotIn("js:web", labels)
        self.assertNotIn("js:node", labels)
        self.assertTrue(cfg.js_skip)

    def test_py_skip_drops_py_layer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config(["--py-skip"], {}, root)
        labels = [c.label for c in cfg.commands]
        self.assertNotIn("py:common", labels)
        self.assertNotIn("py:daemon", labels)
        self.assertNotIn("py:cli", labels)
        self.assertTrue(cfg.py_skip)

    def test_e2e_skip_drops_e2e_layer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config(["--e2e-skip"], {}, root)
        labels = [c.label for c in cfg.commands]
        self.assertNotIn("e2e:browser", labels)
        self.assertTrue(cfg.e2e_skip)

    def test_real_llm_flag_propagates(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config(["--real-llm"], {}, root)
        self.assertTrue(cfg.real_llm)

    def test_unknown_flag_raises(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            with self.assertRaises(TestCommandError) as ctx:
                resolve_config(["--nope"], {}, root)
        self.assertIn("unknown argument", str(ctx.exception))

    def test_missing_repo_raises(self) -> None:
        with self.assertRaises(TestCommandError) as ctx:
            resolve_config([], {}, Path("/no/such/dir/here"))
        self.assertIn("does not exist", str(ctx.exception))

    def test_missing_js_pkg_marked_skipped(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "packages").mkdir()
            # No package.json anywhere; every JS / PY command should
            # be skipped. The E2E stub is always skipped regardless
            # of layout, so it carries its own reason.
            cfg = resolve_config([], {}, root)
        for cmd in cfg.commands:
            if cmd.label.startswith("e2e:"):
                # E2E is always stubbed in v0.1; only the reason varies.
                self.assertTrue(cmd.skipped)
                continue
            self.assertTrue(cmd.skipped, f"{cmd.label} not skipped")
            self.assertIn("missing", cmd.skip_reason)

    def test_playwright_available_changes_skip_reason(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg_installed = resolve_config(
                [], {}, root, playwright_available=False,
            )
            cfg_unwired = resolve_config(
                [], {}, root, playwright_available=True,
            )
        for cfg, expected in (
            (cfg_installed, "Playwright not installed"),
            (cfg_unwired, "Playwright E2E not wired in v0.1"),
        ):
            e2e = [c for c in cfg.commands if c.label == "e2e:browser"][0]
            self.assertTrue(e2e.skipped)
            self.assertEqual(e2e.skip_reason, expected)


# ---------- default Playwright probe ----------


class TestPlaywrightProbe(unittest.TestCase):
    """Unit tests for ``default_playwright_probe``."""

    def _write(self, tmp: Path, body: str) -> Path:
        path = tmp / "package.json"
        path.write_text(body, encoding="utf-8")
        return path

    def test_missing_file_returns_false(self) -> None:
        self.assertFalse(default_playwright_probe(Path("/no/such/path")))

    def test_returns_true_when_dev_dep_present(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                '{"devDependencies": {"@playwright/test": "1.40.0"}}',
            )
            self.assertTrue(default_playwright_probe(path))

    def test_returns_false_when_absent(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                '{"devDependencies": {"vitest": "2.1.3"}}',
            )
            self.assertFalse(default_playwright_probe(path))

    def test_returns_true_when_in_dependencies(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                '{"dependencies": {"@playwright/test": "1.40.0"}}',
            )
            self.assertTrue(default_playwright_probe(path))

    def test_tolerates_json5_comments(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                (
                    "{\n"
                    "  // feature in dev only\n"
                    '  "devDependencies": { "@playwright/test": "1.40.0" }\n'
                    "}\n"
                ),
            )
            self.assertTrue(default_playwright_probe(path))

    def test_falls_back_to_regex_on_quoted_json(self) -> None:
        """A pathologically quoted file still matches the regex probe.

        The intent of this test is to assert that the regex
        fallback still catches the ``@playwright/test`` token
        when ``json.loads`` fails — defensive in case a future
        dev-dependency block slips in non-standard JSON syntax.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                '{ "@playwright/test": "1.40.0" /* trailing */ }',
            )
            self.assertTrue(default_playwright_probe(path))


# ---------- summary table ----------


class TestRenderSummary(unittest.TestCase):
    """Unit tests for ``render_summary`` and ``print_summary``."""

    def test_render_includes_header_and_totals(self) -> None:
        rows = [
            ("js:web", "PASS", "ok", "0"),
            ("py:common", "FAIL", "1 failed", "1"),
        ]
        out = render_summary(
            rows, total_passed=1, total_failed=1, total_skipped=0,
        )
        self.assertIn("heddle test: summary", out)
        self.assertIn("SUITE", out)
        self.assertIn("PASS", out)
        self.assertIn("FAIL", out)
        self.assertIn("passed=1 failed=1 skipped=0", out)

    def test_print_summary_invokes_sink_with_rendered_text(self) -> None:
        sink = _Recorder()
        print_summary(
            [("js:web", "PASS", "ok", "0")],
            total_passed=1, total_failed=0, total_skipped=0,
            sink=sink.append_record,
        )
        self.assertEqual(len(sink), 1)
        self.assertIn("passed=1 failed=0 skipped=0", sink[0])


# ---------- orchestrator tests ----------


class TestTestCmd(unittest.TestCase):
    """End-to-end tests for ``test_cmd`` with mocked subprocess."""

    def test_all_pass_returns_exit_ok(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0, 0, 0, 0])
            sink = _Recorder()
            rc = _test_module.test_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
                summary_sink=sink.append_record,
            )
        self.assertEqual(rc, EXIT_OK)
        # Five runnable + Playwright skipped → 5 actual runner calls.
        self.assertEqual(len(calls), 5)
        self.assertIn("passed=5 failed=0 skipped=1", "\n".join(sink))

    def test_any_failure_returns_exit_test_failed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            # Six suites; suite 2 (js:node) fails.
            runner, calls = _fake_runner_factory([0, 1, 0, 0, 0, 0])
            rc = _test_module.test_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
            )
        self.assertEqual(rc, EXIT_TEST_FAILED)
        self.assertEqual(len(calls), 5)

    def test_skipped_suites_do_not_count_as_failures(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, _calls = _fake_runner_factory([0, 0, 0, 0, 0])
            rc = _test_module.test_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
            )
        self.assertEqual(rc, EXIT_OK)

    def test_unknown_flag_returns_exit_bad_config(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            rc = _test_module.test_cmd(
                ["--bogus"], env={}, repo_root=root,
                runner=_fake_runner_factory([])[0],  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
            )
        self.assertEqual(rc, EXIT_BAD_CONFIG)

    def test_js_skip_skips_js_runner_calls(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0, 0])
            rc = _test_module.test_cmd(
                ["--js-skip"], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
            )
        self.assertEqual(rc, EXIT_OK)
        # JS layer dropped → only 3 py calls + e2e skip → 3 calls.
        self.assertEqual(len(calls), 3)

    def test_py_skip_skips_py_runner_calls(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0])
            rc = _test_module.test_cmd(
                ["--py-skip"], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
            )
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(len(calls), 2)

    def test_e2e_skip_skips_e2e_layer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0, 0, 0])
            rc = _test_module.test_cmd(
                ["--e2e-skip"], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
            )
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(len(calls), 5)
        # No command label is e2e:browser.
        for argv, _cwd, _env in calls:
            self.assertNotIn("test:e2e", argv)

    def test_fake_llm_env_injected_by_default(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0, 0, 0, 0])
            _test_module.test_cmd(
                [], env={"PATH": "/usr/bin"}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
            )
        for _argv, _cwd, env in calls:
            self.assertEqual(env.get(ENV_FAKE_LLM), "1")
            self.assertEqual(env["PATH"], "/usr/bin")

    def test_real_llm_strips_fake_flag(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0, 0, 0, 0])
            _test_module.test_cmd(
                ["--real-llm"], env={"PATH": "/usr/bin"}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
            )
        for _argv, _cwd, env in calls:
            self.assertNotIn(ENV_FAKE_LLM, env)

    def test_real_llm_overrides_exported_env(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0, 0, 0, 0])
            _test_module.test_cmd(
                ["--real-llm"],
                env={"PATH": "/usr/bin", ENV_FAKE_LLM: "1"},
                repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
            )
        for _argv, _cwd, env in calls:
            self.assertNotIn(ENV_FAKE_LLM, env)

    def test_cwd_is_package_directory(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0, 0, 0, 0])
            _test_module.test_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
            )
        cwds = sorted({str(cwd) for _argv, cwd, _env in calls})
        # Both JS package dirs and all three py package dirs were used.
        self.assertIn(str(root / "packages" / "web"), cwds)
        self.assertIn(str(root / "packages" / "node"), cwds)
        self.assertIn(str(root / "packages" / "common"), cwds)
        self.assertIn(str(root / "packages" / "daemon"), cwds)
        self.assertIn(str(root / "packages" / "cli"), cwds)

    def test_summary_table_marks_skipped_e2e(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, _calls = _fake_runner_factory([0, 0, 0, 0, 0, 0])
            sink = _Recorder()
            _test_module.test_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
                summary_sink=sink.append_record,
            )
        text = "\n".join(sink)
        self.assertIn("e2e:browser", text)
        self.assertIn("SKIP", text)

    def test_failure_detail_includes_stderr(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            # Force js:web to fail with a custom stderr.
            def runner(argv, *, cwd, env):
                if "test" in argv and "web" in str(cwd):
                    return 1, "", "AssertionError: 1 != 2\nlots of trace"
                return 0, "", ""
            sink = _Recorder()
            rc = _test_module.test_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
                summary_sink=sink.append_record,
            )
        self.assertEqual(rc, EXIT_TEST_FAILED)
        text = "\n".join(sink)
        self.assertIn("FAIL", text)
        self.assertIn("AssertionError", text)

    def test_playwright_probe_true_records_not_installed_reason(self) -> None:
        """When the probe says Playwright is available the skip
        reason is the v0.1-unwired one, not the not-installed one.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, _calls = _fake_runner_factory([0, 0, 0, 0, 0])
            sink = _Recorder()
            _test_module.test_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_yes_playwright,
                summary_sink=sink.append_record,
            )
        self.assertIn("Playwright E2E not wired in v0.1", "\n".join(sink))

    def test_playwright_probe_false_records_not_installed_reason(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, _calls = _fake_runner_factory([0, 0, 0, 0, 0])
            sink = _Recorder()
            _test_module.test_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                playwright_probe=_no_playwright,
                summary_sink=sink.append_record,
            )
        self.assertIn("Playwright not installed", "\n".join(sink))


if __name__ == "__main__":
    unittest.main()
