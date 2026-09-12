# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_daemon.tools — feat-020 (six self-written tools)."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from heddle_daemon.tools import (
    BashTool,
    EditTool,
    GlobTool,
    GrepTool,
    ReadTool,
    ToolError,
    ToolPathError,
    WriteTool,
    all_tools,
    tool_names,
)


class _TempProject:
    """Helper: per-test project dir under tmp."""

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="heddle_tools_test_"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


# ---------- factory ----------


class TestFactory(unittest.TestCase):
    def test_all_tools_returns_six(self):
        with _TempProject() as proj:
            tools = all_tools(proj)
        self.assertEqual(len(tools), 6)
        kinds = [type(t).__name__ for t in tools]
        self.assertEqual(
            sorted(kinds),
            sorted(["ReadTool", "WriteTool", "EditTool", "BashTool", "GlobTool", "GrepTool"]),
        )

    def test_all_tools_share_same_project_root(self):
        with _TempProject() as proj:
            for t in all_tools(proj):
                self.assertEqual(t.project_root, proj.resolve())

    def test_tool_names_returns_six_canonical_names(self):
        self.assertEqual(sorted(tool_names()), ["bash", "edit", "glob", "grep", "read", "write"])


# ---------- path-safety helper ----------


class TestPathSafety(unittest.TestCase):
    """_resolve_within_root is the chokepoint for sandboxing. Every
    tool routes through it, so its tests live in one place and the
    per-tool tests just assert the right exception is raised."""

    def test_relative_path_inside_root(self):
        from heddle_daemon.tools import _resolve_within_root

        with _TempProject() as proj:
            (proj / "a").mkdir()
            p = _resolve_within_root(proj, "a/b.txt")
            self.assertEqual(p, (proj / "a" / "b.txt").resolve())

    def test_rejects_relative_traversal(self):
        from heddle_daemon.tools import _resolve_within_root

        with _TempProject() as proj:
            with self.assertRaises(ToolPathError) as ctx:
                _resolve_within_root(proj, "../../../etc/passwd")
            self.assertIn("escapes", str(ctx.exception))

    def test_rejects_absolute_outside_root(self):
        from heddle_daemon.tools import _resolve_within_root

        with _TempProject() as proj:
            with self.assertRaises(ToolPathError):
                _resolve_within_root(proj, "/etc/passwd")

    def test_rejects_symlink_outside_root(self):
        from heddle_daemon.tools import _resolve_within_root

        # Windows requires admin / developer-mode to create symlinks.
        # Skip on Windows so the suite passes in CI without elevated
        # privileges; the symlink defense itself is exercised by the
        # manual test harness when running on Linux/macOS.
        if os.name == "nt":
            self.skipTest("symlink creation requires admin on Windows")
        with _TempProject() as outer:
            proj = outer / "proj"
            proj.mkdir()
            secret = outer / "secret.txt"
            secret.write_text("nope", encoding="utf-8")
            (proj / "link").symlink_to(secret)
            with self.assertRaises(ToolPathError):
                _resolve_within_root(proj, "link")

    def test_rejects_empty_string(self):
        from heddle_daemon.tools import _resolve_within_root

        with _TempProject() as proj:
            with self.assertRaises(ToolPathError):
                _resolve_within_root(proj, "")

    def test_rejects_non_string(self):
        from heddle_daemon.tools import _resolve_within_root

        with _TempProject() as proj:
            with self.assertRaises(ToolPathError):
                _resolve_within_root(proj, None)  # type: ignore[arg-type]


# ---------- Read ----------


class TestReadTool(unittest.TestCase):
    def test_read_existing_file(self):
        with _TempProject() as proj:
            # Use bytes to avoid OS-dependent newline translation
            # (write_text on Windows inserts \r\n).
            (proj / "f.txt").write_bytes(b"hello\nworld\n")
            t = ReadTool(project_root=proj)
            self.assertEqual(t._run(path="f.txt"), "hello\nworld\n")

    def test_read_with_limit_truncates(self):
        with _TempProject() as proj:
            (proj / "big.txt").write_text("X" * 1000, encoding="utf-8")
            t = ReadTool(project_root=proj)
            out = t._run(path="big.txt", limit=10)
            self.assertTrue(out.startswith("X" * 10))
            self.assertIn("truncated at 10 bytes", out)
            self.assertIn("total size 1000 bytes", out)

    def test_read_missing_file_raises(self):
        with _TempProject() as proj:
            t = ReadTool(project_root=proj)
            with self.assertRaises(ToolError):
                t._run(path="nope.txt")

    def test_read_directory_raises(self):
        with _TempProject() as proj:
            (proj / "subdir").mkdir()
            t = ReadTool(project_root=proj)
            with self.assertRaises(ToolError):
                t._run(path="subdir")

    def test_read_outside_project_raises_path_error(self):
        with _TempProject() as proj:
            t = ReadTool(project_root=proj)
            with self.assertRaises(ToolPathError):
                t._run(path="../something.txt")

    def test_read_returns_string_not_bytes(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("plain text", encoding="utf-8")
            t = ReadTool(project_root=proj)
            self.assertIsInstance(t._run(path="f.txt"), str)


# ---------- Write ----------


class TestWriteTool(unittest.TestCase):
    def test_write_creates_file(self):
        with _TempProject() as proj:
            t = WriteTool(project_root=proj)
            out = t._run(path="a/b/c.txt", content="hello")
            self.assertIn("wrote 5 bytes", out)
            self.assertEqual((proj / "a" / "b" / "c.txt").read_text(encoding="utf-8"), "hello")

    def test_write_overwrites_existing(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("old", encoding="utf-8")
            t = WriteTool(project_root=proj)
            t._run(path="f.txt", content="new")
            self.assertEqual((proj / "f.txt").read_text(encoding="utf-8"), "new")

    def test_write_outside_project_raises(self):
        with _TempProject() as proj:
            t = WriteTool(project_root=proj)
            with self.assertRaises(ToolPathError):
                t._run(path="../escape.txt", content="x")

    def test_write_reports_bytes_written(self):
        with _TempProject() as proj:
            t = WriteTool(project_root=proj)
            out = t._run(path="x.txt", content="héllo")  # multibyte
            self.assertIn("wrote 6 bytes", out)  # 'é' is 2 bytes in UTF-8


# ---------- Edit ----------


class TestEditTool(unittest.TestCase):
    def test_edit_unique_match_succeeds(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("hello world\n", encoding="utf-8")
            t = EditTool(project_root=proj)
            out = t._run(path="f.txt", old_string="hello", new_string="goodbye")
            self.assertIn("replaced 1 occurrence", out)
            self.assertEqual((proj / "f.txt").read_text(encoding="utf-8"), "goodbye world\n")

    def test_edit_non_unique_raises(self):
        with _TempProject() as proj:
            # Use exactly two occurrences of the search string so the
            # error message is unambiguous.
            (proj / "f.txt").write_text("foo bar foo", encoding="utf-8")
            t = EditTool(project_root=proj)
            with self.assertRaises(ToolError) as ctx:
                t._run(path="f.txt", old_string="foo", new_string="bar")
            self.assertIn("2 places", str(ctx.exception))

    def test_edit_replace_all_replaces_all(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("foo foo foo", encoding="utf-8")
            t = EditTool(project_root=proj)
            out = t._run(path="f.txt", old_string="foo", new_string="bar", replace_all=True)
            self.assertIn("replaced 3 occurrence", out)
            self.assertEqual((proj / "f.txt").read_text(encoding="utf-8"), "bar bar bar")

    def test_edit_no_match_raises(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("hello", encoding="utf-8")
            t = EditTool(project_root=proj)
            with self.assertRaises(ToolError) as ctx:
                t._run(path="f.txt", old_string="xyz", new_string="abc")
            self.assertIn("not found", str(ctx.exception))

    def test_edit_missing_file_raises(self):
        with _TempProject() as proj:
            t = EditTool(project_root=proj)
            with self.assertRaises(ToolError):
                t._run(path="missing.txt", old_string="a", new_string="b")

    def test_edit_outside_project_raises(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("hello", encoding="utf-8")
            t = EditTool(project_root=proj)
            with self.assertRaises(ToolPathError):
                t._run(path="../f.txt", old_string="hello", new_string="bye")


# ---------- Bash ----------


class TestBashTool(unittest.IsolatedAsyncioTestCase):
    async def test_bash_runs_command(self):
        with _TempProject() as proj:
            t = BashTool(project_root=proj)
            out = await t._arun(command='echo hello-from-bash')
            self.assertIn("exit_code: 0", out)
            self.assertIn("hello-from-bash", out)

    async def test_bash_captures_stderr(self):
        with _TempProject() as proj:
            t = BashTool(project_root=proj)
            # 'echo to stderr 1>&2' is portable on POSIX; on Windows we
            # use a Python one-liner via the shell.
            if os.name == "nt":
                cmd = 'python -c "import sys; sys.stderr.write(\'err-line\'); sys.stderr.flush()"'
            else:
                cmd = 'echo to-stderr >&2'
            out = await t._arun(command=cmd)
            self.assertIn("err-line", out)

    async def test_bash_propagates_exit_code(self):
        with _TempProject() as proj:
            t = BashTool(project_root=proj)
            cmd = 'python -c "import sys; sys.exit(7)"' if os.name == "nt" else 'exit 7'
            out = await t._arun(command=cmd)
            self.assertIn("exit_code: 7", out)

    async def test_bash_runs_in_project_root(self):
        with _TempProject() as proj:
            t = BashTool(project_root=proj)
            out = await t._arun(command='pwd' if os.name != "nt" else 'cd')
            self.assertIn(str(proj.resolve()), out)

    async def test_bash_timeout_raises(self):
        with _TempProject() as proj:
            t = BashTool(project_root=proj)
            # Use a portable sleep: 'ping -n 5 127.0.0.1' on Windows,
            # 'sleep 5' on POSIX. timeout_seconds=1 should fire.
            cmd = "ping -n 5 127.0.0.1" if os.name == "nt" else "sleep 5"
            with self.assertRaises(ToolError) as ctx:
                await t._arun(command=cmd, timeout_seconds=1)
            self.assertIn("timed out", str(ctx.exception))

    async def test_bash_truncates_huge_output(self):
        with _TempProject() as proj:
            t = BashTool(project_root=proj)
            # 60KB of stdout
            out = await t._arun(
                command='python -c "print(\'x\'*60000)"' if os.name == "nt"
                else "yes xxxxxxxxxx | head -c 60000"
            )
            self.assertIn("truncated", out)


# ---------- Glob ----------


class TestGlobTool(unittest.TestCase):
    def test_glob_finds_files(self):
        with _TempProject() as proj:
            for name in ("a.py", "b.txt", "c.md"):
                (proj / name).write_text("x", encoding="utf-8")
            t = GlobTool(project_root=proj)
            out = t._run(pattern="*.py")
            self.assertIn("a.py", out)
            self.assertNotIn("b.txt", out)

    def test_glob_recursive(self):
        with _TempProject() as proj:
            (proj / "src").mkdir()
            (proj / "src" / "a.py").write_text("x", encoding="utf-8")
            (proj / "src" / "nested").mkdir()
            (proj / "src" / "nested" / "b.py").write_text("x", encoding="utf-8")
            t = GlobTool(project_root=proj)
            out = t._run(pattern="**/*.py")
            self.assertIn("src/a.py", out)
            self.assertIn("src/nested/b.py", out)

    def test_glob_no_matches_returns_empty_string(self):
        with _TempProject() as proj:
            t = GlobTool(project_root=proj)
            self.assertEqual(t._run(pattern="*.nope"), "")

    def test_glob_rejects_escape_pattern(self):
        """A pattern starting with '..' that escapes the root is filtered."""
        with _TempProject() as outer:
            proj = outer / "proj"
            proj.mkdir()
            (outer / "outside.txt").write_text("x", encoding="utf-8")
            t = GlobTool(project_root=proj)
            # pathlib.glob against project_root with pattern "../*"
            # yields resolved paths that relative_to(project_root)
            # rejects — they get filtered out.
            out = t._run(pattern="../*")
            # The outside file should NOT appear.
            self.assertNotIn("outside.txt", out)

    def test_glob_caps_at_max_matches(self):
        with _TempProject() as proj:
            for i in range(GlobTool.MAX_MATCHES + 50):
                (proj / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
            t = GlobTool(project_root=proj)
            out = t._run(pattern="*.txt")
            self.assertIn("capped at", out)


# ---------- Grep ----------


class TestGrepTool(unittest.TestCase):
    def test_grep_finds_matches(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            t = GrepTool(project_root=proj)
            out = t._run(pattern="beta")
            self.assertEqual(out, "f.txt:2:beta")

    def test_grep_multiple_matches(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("a\nb\na\nc\na\n", encoding="utf-8")
            t = GrepTool(project_root=proj)
            out = t._run(pattern="a")
            lines = out.splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(all(":a" in line for line in lines))

    def test_grep_no_matches(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("hello\n", encoding="utf-8")
            t = GrepTool(project_root=proj)
            self.assertEqual(t._run(pattern="zzz"), "")

    def test_grep_skips_heddle_dir(self):
        with _TempProject() as proj:
            (proj / ".heddle").mkdir()
            (proj / ".heddle" / "secret.txt").write_text("needle\n", encoding="utf-8")
            (proj / "public.txt").write_text("needle\n", encoding="utf-8")
            t = GrepTool(project_root=proj)
            out = t._run(pattern="needle")
            self.assertIn("public.txt", out)
            self.assertNotIn("secret.txt", out)

    def test_grep_skips_binary_files(self):
        with _TempProject() as proj:
            (proj / "binary.bin").write_bytes(b"\x00\x01hello\x02world")
            (proj / "text.txt").write_text("hello\n", encoding="utf-8")
            t = GrepTool(project_root=proj)
            out = t._run(pattern="hello")
            self.assertIn("text.txt", out)
            self.assertNotIn("binary.bin", out)

    def test_grep_invalid_regex_raises(self):
        with _TempProject() as proj:
            t = GrepTool(project_root=proj)
            with self.assertRaises(ToolError):
                t._run(pattern="[unclosed")

    def test_grep_file_filter(self):
        with _TempProject() as proj:
            (proj / "a.py").write_text("foo\n", encoding="utf-8")
            (proj / "a.txt").write_text("foo\n", encoding="utf-8")
            t = GrepTool(project_root=proj)
            out = t._run(pattern="foo", file_filter="*.py")
            self.assertIn("a.py", out)
            self.assertNotIn("a.txt", out)

    def test_grep_skips_node_modules(self):
        with _TempProject() as proj:
            (proj / "node_modules").mkdir()
            (proj / "node_modules" / "dep.js").write_text("needle\n", encoding="utf-8")
            (proj / "app.js").write_text("needle\n", encoding="utf-8")
            t = GrepTool(project_root=proj)
            out = t._run(pattern="needle")
            self.assertIn("app.js", out)
            self.assertNotIn("node_modules", out)

    def test_grep_caps_matches(self):
        with _TempProject() as proj:
            (proj / "f.txt").write_text("\n".join(["x"] * 500), encoding="utf-8")
            t = GrepTool(project_root=proj)
            out = t._run(pattern="x", max_matches=10)
            self.assertIn("capped at 10", out)


# ---------- integration: tools survive multiple calls ----------


class TestToolsIntegration(unittest.TestCase):
    """A simulated mini-workflow: write, edit, read, glob, grep."""

    def test_round_trip_workflow(self):
        with _TempProject() as proj:
            write = WriteTool(project_root=proj)
            edit = EditTool(project_root=proj)
            read = ReadTool(project_root=proj)
            glob = GlobTool(project_root=proj)
            grep = GrepTool(project_root=proj)

            write._run(path="src/main.py", content="def hello():\n    return 42\n")
            write._run(path="src/util.py", content="def util():\n    pass\n")
            write._run(path="README.md", content="# Project\n\nhello world\n")

            # Verify all written
            files = glob._run(pattern="**/*")
            self.assertIn("src/main.py", files)
            self.assertIn("src/util.py", files)
            self.assertIn("README.md", files)

            # Edit main.py
            edit._run(
                path="src/main.py",
                old_string="return 42",
                new_string="return 'hello'",
            )
            content = read._run(path="src/main.py")
            self.assertIn("return 'hello'", content)

            # Grep finds the new hello across files
            matches = grep._run(pattern="hello")
            # main.py has it (return 'hello'), README.md has it
            self.assertIn("src/main.py", matches)
            self.assertIn("README.md", matches)


if __name__ == "__main__":
    unittest.main()
