# SPDX-License-Identifier: Apache-2.0
"""Tests for ``heddle_cli.lint`` — feat-053.

Covers every public branch of ``lint_cmd`` and its config helpers:

- argv parsing (--js-skip, --ts-skip, --py-skip, unknown flag ->
  ``EXIT_BAD_CONFIG``)
- per-layer inclusion (eslint when not --js-skip; tsc when not
  --ts-skip; ruff when not --py-skip)
- stub suites for missing package directories / tsconfig.json /
  packages/ directory
- runner injection: every child command is sent through the
  injected ``LintRunner`` so no real pnpm / tsc / ruff is spawned
- summary table aggregation (PASS / FAIL / SKIP counters)
- exit code mapping (EXIT_OK / EXIT_LINT_FAILED / EXIT_BAD_CONFIG)
- ``render_summary`` formatting (header, rows, totals)

Every test injects ``runner`` / ``summary_sink`` so no real
subprocess is launched and no real file is read.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Mapping

from heddle_cli.lint import (
    EXIT_BAD_CONFIG,
    EXIT_LINT_FAILED,
    EXIT_OK,
)
from heddle_cli import lint as _lint_module
from heddle_cli.lint_config import (
    JS_PACKAGES,
    LintCommandError,
    resolve_config,
)
from heddle_cli.lint_defaults import (
    print_summary,
    render_summary,
)


# Tell pytest NOT to try collecting ``LintCommandError`` as a test
# class — it lives in ``heddle_cli.lint_config`` and only happens
# to start with the ``Test`` prefix.
LintCommandError.__test__ = False  # type: ignore[attr-defined]


# ---------- helpers ----------


class _Recorder(list):
    """``list`` subclass that exposes ``append_record`` for tests."""

    def append_record(self, line: str) -> None:
        self.append(line)


def _fake_runner_factory(rcs: list[int]):
    """Return a ``LintRunner`` that pops the next ``rc`` per call.

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


def _make_repo(tmp_path: Path) -> Path:
    """Lay out a fake repo tree inside ``tmp_path``.

    Creates ``packages/{web,node}`` with ``package.json`` and
    ``tsconfig.json``, plus a ``packages/`` directory so Ruff has
    a sentinel. ``resolve_config`` records every check as
    ``runnable`` rather than skipped.
    """
    for pkg in JS_PACKAGES:
        d = tmp_path / "packages" / pkg
        d.mkdir(parents=True, exist_ok=True)
        (d / "package.json").write_text("{}", encoding="utf-8")
        (d / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "packages").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------- config tests ----------


class TestResolveConfig(unittest.TestCase):
    """Unit tests for ``resolve_config``."""

    def test_all_checks_present(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config([], {}, root)
        labels = [c.label for c in cfg.commands]
        self.assertEqual(
            labels,
            ["eslint:web", "eslint:node", "tsc:web", "tsc:node",
             "ruff:packages"],
        )
        self.assertFalse(cfg.js_skip)
        self.assertFalse(cfg.ts_skip)
        self.assertFalse(cfg.py_skip)

    def test_js_skip_drops_eslint_layer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config(["--js-skip"], {}, root)
        labels = [c.label for c in cfg.commands]
        self.assertNotIn("eslint:web", labels)
        self.assertNotIn("eslint:node", labels)
        self.assertTrue(cfg.js_skip)
        # tsc + ruff remain.
        self.assertIn("tsc:web", labels)
        self.assertIn("ruff:packages", labels)

    def test_ts_skip_drops_tsc_layer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config(["--ts-skip"], {}, root)
        labels = [c.label for c in cfg.commands]
        self.assertNotIn("tsc:web", labels)
        self.assertNotIn("tsc:node", labels)
        self.assertTrue(cfg.ts_skip)
        # eslint + ruff remain.
        self.assertIn("eslint:web", labels)
        self.assertIn("ruff:packages", labels)

    def test_py_skip_drops_ruff_layer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config(["--py-skip"], {}, root)
        labels = [c.label for c in cfg.commands]
        self.assertNotIn("ruff:packages", labels)
        self.assertTrue(cfg.py_skip)
        # eslint + tsc remain.
        self.assertIn("eslint:web", labels)
        self.assertIn("tsc:web", labels)

    def test_unknown_flag_raises(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            with self.assertRaises(LintCommandError) as ctx:
                resolve_config(["--nope"], {}, root)
        self.assertIn("unknown argument", str(ctx.exception))

    def test_missing_repo_raises(self) -> None:
        with self.assertRaises(LintCommandError) as ctx:
            resolve_config([], {}, Path("/no/such/dir/here"))
        self.assertIn("does not exist", str(ctx.exception))

    def test_missing_sentinels_mark_checks_skipped(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # No package.json / tsconfig.json / packages/ anywhere;
            # every check should be skipped with a ``missing`` reason.
            cfg = resolve_config([], {}, root)
        for cmd in cfg.commands:
            self.assertTrue(cmd.skipped, f"{cmd.label} not skipped")
            self.assertIn("missing", cmd.skip_reason)

    def test_eslint_uses_pnpm_per_package(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config([], {}, root)
        eslint_web = [c for c in cfg.commands if c.label == "eslint:web"][0]
        self.assertEqual(eslint_web.argv[:4],
                         ("pnpm", "--filter", "web", "lint"))
        self.assertEqual(eslint_web.cwd, root / "packages" / "web")

    def test_tsc_uses_pnpm_exec(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config([], {}, root)
        tsc_web = [c for c in cfg.commands if c.label == "tsc:web"][0]
        self.assertEqual(tsc_web.argv,
                         ("pnpm", "--filter", "web", "exec",
                          "tsc", "--noEmit"))
        self.assertEqual(tsc_web.cwd, root / "packages" / "web")

    def test_ruff_runs_from_repo_root(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            cfg = resolve_config([], {}, root)
        ruff = [c for c in cfg.commands if c.label == "ruff:packages"][0]
        self.assertEqual(ruff.argv, ("ruff", "check", "packages"))
        self.assertEqual(ruff.cwd, root)


# ---------- summary table ----------


class TestRenderSummary(unittest.TestCase):
    """Unit tests for ``render_summary`` and ``print_summary``."""

    def test_render_includes_header_and_totals(self) -> None:
        rows = [
            ("eslint:web", "PASS", "ok", "0"),
            ("ruff:packages", "FAIL", "1 file would reformat", "1"),
        ]
        out = render_summary(
            rows, total_passed=1, total_failed=1, total_skipped=0,
        )
        self.assertIn("heddle lint: summary", out)
        self.assertIn("CHECK", out)
        self.assertIn("PASS", out)
        self.assertIn("FAIL", out)
        self.assertIn("passed=1 failed=1 skipped=0", out)

    def test_print_summary_invokes_sink_with_rendered_text(self) -> None:
        sink = _Recorder()
        print_summary(
            [("eslint:web", "PASS", "ok", "0")],
            total_passed=1, total_failed=0, total_skipped=0,
            sink=sink.append_record,
        )
        self.assertEqual(len(sink), 1)
        self.assertIn("passed=1 failed=0 skipped=0", sink[0])


# ---------- orchestrator tests ----------


class TestLintCmd(unittest.TestCase):
    """End-to-end tests for ``lint_cmd`` with mocked subprocess."""

    def test_all_pass_returns_exit_ok(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0, 0, 0])
            sink = _Recorder()
            rc = _lint_module.lint_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                summary_sink=sink.append_record,
            )
        self.assertEqual(rc, EXIT_OK)
        # Five checks: eslint:web, eslint:node, tsc:web, tsc:node, ruff.
        self.assertEqual(len(calls), 5)
        self.assertIn("passed=5 failed=0 skipped=0", "\n".join(sink))

    def test_any_failure_returns_exit_lint_failed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            # Five checks; suite 2 (eslint:node) fails.
            runner, calls = _fake_runner_factory([0, 1, 0, 0, 0])
            rc = _lint_module.lint_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
            )
        self.assertEqual(rc, EXIT_LINT_FAILED)
        self.assertEqual(len(calls), 5)

    def test_skipped_suites_do_not_count_as_failures(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            # Empty repo -> every check skipped, runner never called.
            root = Path(tmp)
            runner, calls = _fake_runner_factory([])
            rc = _lint_module.lint_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
            )
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(len(calls), 0)

    def test_unknown_flag_returns_exit_bad_config(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            rc = _lint_module.lint_cmd(
                ["--bogus"], env={}, repo_root=root,
                runner=_fake_runner_factory([])[0],  # type: ignore[arg-type]
            )
        self.assertEqual(rc, EXIT_BAD_CONFIG)

    def test_js_skip_skips_eslint_runner_calls(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0])
            rc = _lint_module.lint_cmd(
                ["--js-skip"], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
            )
        self.assertEqual(rc, EXIT_OK)
        # eslint layer dropped -> tsc:web, tsc:node, ruff (3 calls).
        self.assertEqual(len(calls), 3)
        for argv, _cwd, _env in calls:
            self.assertNotIn("lint", argv)

    def test_ts_skip_skips_tsc_runner_calls(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0])
            rc = _lint_module.lint_cmd(
                ["--ts-skip"], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
            )
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(len(calls), 3)
        for argv, _cwd, _env in calls:
            # The tsc invocation is identifiable by ``--noEmit``.
            self.assertNotIn("--noEmit", argv)

    def test_py_skip_skips_ruff_runner_calls(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0, 0])
            rc = _lint_module.lint_cmd(
                ["--py-skip"], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
            )
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(len(calls), 4)
        for argv, _cwd, _env in calls:
            self.assertNotIn("ruff", argv)

    def test_all_skips_short_circuits_to_exit_ok(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([])
            rc = _lint_module.lint_cmd(
                ["--js-skip", "--ts-skip", "--py-skip"],
                env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
            )
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(len(calls), 0)

    def test_cwd_is_package_or_repo(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            runner, calls = _fake_runner_factory([0, 0, 0, 0, 0])
            _lint_module.lint_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
            )
        cwds = sorted({str(cwd) for _argv, cwd, _env in calls})
        self.assertIn(str(root / "packages" / "web"), cwds)
        self.assertIn(str(root / "packages" / "node"), cwds)
        self.assertIn(str(root), cwds)  # ruff runs from repo root

    def test_summary_table_marks_skipped_layer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)  # empty -> everything skipped
            runner = _fake_runner_factory([])[0]
            sink = _Recorder()
            _lint_module.lint_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                summary_sink=sink.append_record,
            )
        text = "\n".join(sink)
        self.assertIn("eslint:web", text)
        self.assertIn("ruff:packages", text)
        self.assertIn("SKIP", text)

    def test_failure_detail_includes_stderr(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(Path(tmp))
            # Force eslint:web to fail with a custom stderr.
            def runner(argv, *, cwd, env):
                if tuple(argv[:4]) == ("pnpm", "--filter", "web", "lint"):
                    return 1, "", "error: 'foo' is defined but never used\ntrace"
                return 0, "", ""
            sink = _Recorder()
            rc = _lint_module.lint_cmd(
                [], env={}, repo_root=root,
                runner=runner,  # type: ignore[arg-type]
                summary_sink=sink.append_record,
            )
        self.assertEqual(rc, EXIT_LINT_FAILED)
        text = "\n".join(sink)
        self.assertIn("FAIL", text)
        self.assertIn("error", text)


if __name__ == "__main__":
    unittest.main()