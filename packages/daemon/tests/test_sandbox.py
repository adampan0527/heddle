# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_daemon.sandbox — feat-021 (tool-dispatch middleware)."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from heddle_daemon.sandbox import (
    CONFIG_KEY,
    CONFIG_REL_PATH,
    DEFAULT_CONFIRM_TIMEOUT_SECONDS,
    DEFAULT_SANDBOX_LEVEL,
    MUTATING_TOOLS,
    SandboxConfig,
    SandboxConfigError,
    SandboxLevel,
    ToolDispatchMiddleware,
)


class _TempProject:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="heddle_sandbox_test_"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


# ---------- SandboxLevel + constants ----------


class TestSandboxLevel(unittest.TestCase):
    def test_string_values(self):
        self.assertEqual(SandboxLevel.READ_ONLY.value, "read-only")
        self.assertEqual(SandboxLevel.EDIT_WITH_CONFIRM.value, "edit-with-confirm")
        self.assertEqual(SandboxLevel.FULL.value, "full")

    def test_from_string_round_trip(self):
        for s in SandboxLevel:
            self.assertEqual(SandboxLevel(s.value), s)

    def test_from_invalid_string_raises(self):
        with self.assertRaises(ValueError):
            SandboxLevel("god-mode")

    def test_default_is_full(self):
        self.assertEqual(DEFAULT_SANDBOX_LEVEL, SandboxLevel.FULL)

    def test_mutating_tools_set_is_complete(self):
        # The six-tool set has three mutators: write, edit, bash.
        # Anything in the toolset that's NOT read-only must be in
        # MUTATING_TOOLS, otherwise read-only sandbox would let it
        # through silently.
        from heddle_daemon.tools import tool_names

        for name in tool_names():
            if name not in MUTATING_TOOLS:
                self.assertIn(
                    name,
                    ("read", "glob", "grep"),
                    f"{name!r} is not in MUTATING_TOOLS but isn't a known read-only tool",
                )


# ---------- SandboxConfig.from_yaml ----------


class TestSandboxConfig(unittest.TestCase):
    def test_default_when_no_config_file(self):
        with _TempProject() as proj:
            cfg = SandboxConfig.from_yaml(proj)
            self.assertEqual(cfg.level, SandboxConfig().level)
            self.assertEqual(cfg.level, DEFAULT_SANDBOX_LEVEL)

    def test_default_when_empty_config(self):
        with _TempProject() as proj:
            (proj / ".heddle").mkdir()
            (proj / ".heddle" / "config.yaml").write_text("", encoding="utf-8")
            cfg = SandboxConfig.from_yaml(proj)
            self.assertEqual(cfg.level, DEFAULT_SANDBOX_LEVEL)

    def test_default_when_config_has_other_keys(self):
        with _TempProject() as proj:
            (proj / ".heddle").mkdir()
            (proj / ".heddle" / "config.yaml").write_text(
                "some_other_key: 42\n", encoding="utf-8"
            )
            cfg = SandboxConfig.from_yaml(proj)
            self.assertEqual(cfg.level, DEFAULT_SANDBOX_LEVEL)

    def test_reads_explicit_readonly(self):
        with _TempProject() as proj:
            (proj / ".heddle").mkdir()
            (proj / ".heddle" / "config.yaml").write_text(
                f"{CONFIG_KEY}: read-only\n", encoding="utf-8"
            )
            cfg = SandboxConfig.from_yaml(proj)
            self.assertEqual(cfg.level, SandboxLevel.READ_ONLY)

    def test_reads_explicit_edit_with_confirm(self):
        with _TempProject() as proj:
            (proj / ".heddle").mkdir()
            (proj / ".heddle" / "config.yaml").write_text(
                f"{CONFIG_KEY}: edit-with-confirm\n", encoding="utf-8"
            )
            cfg = SandboxConfig.from_yaml(proj)
            self.assertEqual(cfg.level, SandboxLevel.EDIT_WITH_CONFIRM)

    def test_reads_explicit_full(self):
        with _TempProject() as proj:
            (proj / ".heddle").mkdir()
            (proj / ".heddle" / "config.yaml").write_text(
                f"{CONFIG_KEY}: full\n", encoding="utf-8"
            )
            cfg = SandboxConfig.from_yaml(proj)
            self.assertEqual(cfg.level, SandboxLevel.FULL)

    def test_invalid_level_raises(self):
        with _TempProject() as proj:
            (proj / ".heddle").mkdir()
            (proj / ".heddle" / "config.yaml").write_text(
                f"{CONFIG_KEY}: god-mode\n", encoding="utf-8"
            )
            with self.assertRaises(SandboxConfigError) as ctx:
                SandboxConfig.from_yaml(proj)
            self.assertIn("god-mode", str(ctx.exception))

    def test_invalid_yaml_raises(self):
        with _TempProject() as proj:
            (proj / ".heddle").mkdir()
            (proj / ".heddle" / "config.yaml").write_text(
                "sandbox_level: [\n", encoding="utf-8"  # broken YAML
            )
            with self.assertRaises(SandboxConfigError) as ctx:
                SandboxConfig.from_yaml(proj)
            self.assertIn("YAML", str(ctx.exception))

    def test_top_level_not_mapping_raises(self):
        with _TempProject() as proj:
            (proj / ".heddle").mkdir()
            (proj / ".heddle" / "config.yaml").write_text(
                "- just a list\n", encoding="utf-8"
            )
            with self.assertRaises(SandboxConfigError):
                SandboxConfig.from_yaml(proj)

    def test_custom_config_path(self):
        with _TempProject() as proj:
            custom = proj / "my-config.yaml"
            custom.write_text(f"{CONFIG_KEY}: read-only\n", encoding="utf-8")
            cfg = SandboxConfig.from_yaml(proj, config_path=custom)
            self.assertEqual(cfg.level, SandboxLevel.READ_ONLY)


# ---------- ToolDispatchMiddleware: construction ----------


class TestMiddlewareConstruction(unittest.TestCase):
    def test_default_config_is_full(self):
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
            mw.setup()
            self.assertEqual(mw.config.level, SandboxLevel.FULL)

    def test_rejects_nonexistent_project_root(self):
        with _TempProject() as proj:
            with self.assertRaises(SandboxConfigError):
                ToolDispatchMiddleware(
                    project_root=proj / "nope",
                    config=SandboxConfig(),
                )

    def test_rejects_non_positive_timeout(self):
        with _TempProject() as proj:
            with self.assertRaises(SandboxConfigError):
                ToolDispatchMiddleware(
                    project_root=proj,
                    config=SandboxConfig(),
                    confirm_timeout_seconds=0,
                )
            with self.assertRaises(SandboxConfigError):
                ToolDispatchMiddleware(
                    project_root=proj,
                    config=SandboxConfig(),
                    confirm_timeout_seconds=-1,
                )

    def test_setup_is_idempotent(self):
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
            mw.setup()
            mw.setup()  # second call no-ops
            self.assertEqual(set(mw._tools.keys()), {"read", "write", "edit", "bash", "glob", "grep"})


# ---------- full sandbox ----------


class TestFullSandbox(unittest.IsolatedAsyncioTestCase):
    async def test_full_allows_write(self):
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
            mw.setup()
            out = await mw.dispatch_async("write", {"path": "a.txt", "content": "x"})
            self.assertIn("wrote 1 bytes", out)
            self.assertTrue((proj / "a.txt").exists())

    async def test_full_allows_bash(self):
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
            mw.setup()
            out = await mw.dispatch_async("bash", {"command": "echo hi"})
            self.assertIn("hi", out)

    async def test_full_no_consults_confirm_callback(self):
        """Under sandbox=full the confirm_callback is irrelevant."""
        called = []
        async def cb(name, args):
            called.append(name)
            return True
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(
                project_root=proj,
                config=SandboxConfig(),
                confirm_callback=cb,
            )
            mw.setup()
            await mw.dispatch_async("write", {"path": "a.txt", "content": "x"})
            self.assertEqual(called, [])  # callback never invoked


# ---------- read-only sandbox ----------


class TestReadOnlySandbox(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_write(self):
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(
                project_root=proj, config=SandboxConfig(level=SandboxLevel.READ_ONLY)
            )
            mw.setup()
            out = await mw.dispatch_async("write", {"path": "a.txt", "content": "x"})
            self.assertIn("read-only", out)
            self.assertIn("refused", out)
            # No file was created.
            self.assertFalse((proj / "a.txt").exists())
            self.assertEqual(mw.rejected_readonly_count, 1)

    async def test_rejects_edit(self):
        with _TempProject() as proj:
            (proj / "a.txt").write_text("hello", encoding="utf-8")
            mw = ToolDispatchMiddleware(
                project_root=proj, config=SandboxConfig(level=SandboxLevel.READ_ONLY)
            )
            mw.setup()
            out = await mw.dispatch_async(
                "edit",
                {"path": "a.txt", "old_string": "hello", "new_string": "bye"},
            )
            self.assertIn("read-only", out)
            # Original file untouched.
            self.assertEqual((proj / "a.txt").read_text(encoding="utf-8"), "hello")

    async def test_rejects_bash(self):
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(
                project_root=proj, config=SandboxConfig(level=SandboxLevel.READ_ONLY)
            )
            mw.setup()
            out = await mw.dispatch_async("bash", {"command": "echo hi"})
            self.assertIn("read-only", out)

    async def test_allows_read(self):
        with _TempProject() as proj:
            # bytes so the test isn't subject to Windows newline
            # translation; ``read_text`` on Windows inserts \r\n.
            (proj / "a.txt").write_bytes(b"hello\n")
            mw = ToolDispatchMiddleware(
                project_root=proj, config=SandboxConfig(level=SandboxLevel.READ_ONLY)
            )
            mw.setup()
            out = await mw.dispatch_async("read", {"path": "a.txt"})
            self.assertEqual(out, "hello\n")
            self.assertEqual(mw.allowed_count, 1)

    async def test_allows_glob(self):
        with _TempProject() as proj:
            (proj / "a.txt").write_text("x", encoding="utf-8")
            mw = ToolDispatchMiddleware(
                project_root=proj, config=SandboxConfig(level=SandboxLevel.READ_ONLY)
            )
            mw.setup()
            out = await mw.dispatch_async("glob", {"pattern": "*.txt"})
            self.assertIn("a.txt", out)

    async def test_allows_grep(self):
        with _TempProject() as proj:
            (proj / "a.txt").write_text("hello world\n", encoding="utf-8")
            mw = ToolDispatchMiddleware(
                project_root=proj, config=SandboxConfig(level=SandboxLevel.READ_ONLY)
            )
            mw.setup()
            out = await mw.dispatch_async("grep", {"pattern": "world"})
            self.assertIn("a.txt", out)


# ---------- edit-with-confirm sandbox ----------


class TestEditWithConfirmSandbox(unittest.IsolatedAsyncioTestCase):
    async def test_no_callback_denies_mutating(self):
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(
                project_root=proj,
                config=SandboxConfig(level=SandboxLevel.EDIT_WITH_CONFIRM),
                # confirm_callback=None
            )
            mw.setup()
            out = await mw.dispatch_async("write", {"path": "a.txt", "content": "x"})
            self.assertIn("user denied", out)
            self.assertFalse((proj / "a.txt").exists())
            self.assertEqual(mw.rejected_confirm_count, 1)

    async def test_callback_yes_allows_write(self):
        async def yes(name, args):
            return True
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(
                project_root=proj,
                config=SandboxConfig(level=SandboxLevel.EDIT_WITH_CONFIRM),
                confirm_callback=yes,
            )
            mw.setup()
            out = await mw.dispatch_async("write", {"path": "a.txt", "content": "x"})
            self.assertIn("wrote", out)
            self.assertTrue((proj / "a.txt").exists())

    async def test_callback_no_denies(self):
        async def no(name, args):
            return False
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(
                project_root=proj,
                config=SandboxConfig(level=SandboxLevel.EDIT_WITH_CONFIRM),
                confirm_callback=no,
            )
            mw.setup()
            out = await mw.dispatch_async("write", {"path": "a.txt", "content": "x"})
            self.assertIn("user denied", out)
            self.assertFalse((proj / "a.txt").exists())

    async def test_callback_timeout_denies(self):
        async def slow(name, args):
            await asyncio.sleep(5)
            return True
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(
                project_root=proj,
                config=SandboxConfig(level=SandboxLevel.EDIT_WITH_CONFIRM),
                confirm_callback=slow,
                confirm_timeout_seconds=0.05,
            )
            mw.setup()
            out = await mw.dispatch_async("write", {"path": "a.txt", "content": "x"})
            self.assertIn("user denied", out)
            self.assertEqual(mw.rejected_timeout_count, 1)

    async def test_read_only_tools_pass_through_without_consulting_callback(self):
        called = []
        async def cb(name, args):
            called.append(name)
            return True
        with _TempProject() as proj:
            (proj / "a.txt").write_text("hi\n", encoding="utf-8")
            mw = ToolDispatchMiddleware(
                project_root=proj,
                config=SandboxConfig(level=SandboxLevel.EDIT_WITH_CONFIRM),
                confirm_callback=cb,
            )
            mw.setup()
            await mw.dispatch_async("read", {"path": "a.txt"})
            await mw.dispatch_async("glob", {"pattern": "*"})
            await mw.dispatch_async("grep", {"pattern": "hi"})
            self.assertEqual(called, [])

    async def test_callback_receives_tool_name_and_args(self):
        received: list[tuple[str, dict]] = []
        async def cb(name, args):
            received.append((name, args))
            return True
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(
                project_root=proj,
                config=SandboxConfig(level=SandboxLevel.EDIT_WITH_CONFIRM),
                confirm_callback=cb,
            )
            mw.setup()
            await mw.dispatch_async(
                "edit",
                {"path": "x", "old_string": "a", "new_string": "b"},
            )
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][0], "edit")
            self.assertEqual(received[0][1]["path"], "x")
            self.assertEqual(received[0][1]["old_string"], "a")


# ---------- error surfacing ----------


class TestErrorSurfacing(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_tool_raises_keyerror(self):
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
            mw.setup()
            with self.assertRaises(KeyError):
                await mw.dispatch_async("rm-rf", {"path": "/"})

    async def test_real_tool_error_returned_as_string(self):
        """ToolError from the tool (e.g. file not found) is surfaced
        as a string result, not an exception, so the agent loop
        continues."""
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
            mw.setup()
            out = await mw.dispatch_async(
                "edit",
                {"path": "nonexistent.txt", "old_string": "x", "new_string": "y"},
            )
            self.assertIn("failed", out)
            self.assertIn("nonexistent.txt", out)


# ---------- end-to-end: dispatcher wraps real tools ----------


class TestEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_write_then_read_under_full(self):
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(project_root=proj, config=SandboxConfig())
            mw.setup()
            await mw.dispatch_async("write", {"path": "a.txt", "content": "x"})
            out = await mw.dispatch_async("read", {"path": "a.txt"})
            self.assertEqual(out, "x")

    async def test_rejected_write_creates_no_file(self):
        with _TempProject() as proj:
            mw = ToolDispatchMiddleware(
                project_root=proj, config=SandboxConfig(level=SandboxLevel.READ_ONLY)
            )
            mw.setup()
            await mw.dispatch_async("write", {"path": "a.txt", "content": "x"})
            self.assertFalse((proj / "a.txt").exists())

    async def test_glob_works_alongside_rejected_writes(self):
        with _TempProject() as proj:
            (proj / "x.py").write_text("", encoding="utf-8")
            mw = ToolDispatchMiddleware(
                project_root=proj, config=SandboxConfig(level=SandboxLevel.READ_ONLY)
            )
            mw.setup()
            await mw.dispatch_async("write", {"path": "x.txt", "content": "x"})
            out = await mw.dispatch_async("glob", {"pattern": "*.py"})
            self.assertIn("x.py", out)
            self.assertFalse((proj / "x.txt").exists())


if __name__ == "__main__":
    unittest.main()
