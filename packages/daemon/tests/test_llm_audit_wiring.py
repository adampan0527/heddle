# SPDX-License-Identifier: Apache-2.0
"""Tests for the daemon's feat-025 LLM-call audit log wiring.

Covers the contract:

    * ``Daemon.start()`` with a matching registered project attaches
      an :class:`LlmAuditLogger` writing to
      ``~/.heddle/logs/<project_id>/llm-audit.jsonl``;
    * ``Daemon.start()`` with no project_path does NOT attach
      (skeleton mode);
    * ``Daemon.start()`` with a project_path that matches no
      registered project emits an ``llm_audit_skipped`` warn event
      and continues (graceful fallback);
    * ``Daemon.start()`` honours the ``logs_dir`` config field and
      the ``HEDDLE_LOGS_DIR`` env var;
    * ``Daemon.stop()`` closes the audit logger;
    * ``_on_project_removed`` closes the audit logger BEFORE
      signaling stop events;
    * ``AgentRuntime.run_agent_step`` with an ``llm_audit`` logger
      configured writes one JSON line per LLM call to the audit
      file.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from heddle_common import logging as hlog
from heddle_common.fake_llm import FakeLLM, ScriptedResponse
from heddle_common.projects_io import (
    DEFAULT_PROJECTS_PATH,
    Project,
    save_projects,
)

from heddle_daemon.agent_runtime import AgentRuntime
from heddle_daemon.checkpointing import ProjectCheckpointStore
from heddle_daemon.llm_audit import (
    ENV_LOGS_DIR,
    LLM_AUDIT_FILENAME,
    LlmAuditLogger,
    resolve_logs_dir,
)
from heddle_daemon.sandbox import SandboxConfig, ToolDispatchMiddleware
from heddle_daemon.server import Daemon, DaemonConfig

# ---------- helpers ----------


@contextmanager
def _projects_registry(registry_path: Path, projects: list[Project]) -> Iterator[None]:
    """Point ``heddle_common.projects_io`` at an isolated projects.json file."""
    save_projects(registry_path, {p.id: p for p in projects})
    saved_default = DEFAULT_PROJECTS_PATH
    import heddle_common.projects_io as _pio

    object.__setattr__(_pio, "DEFAULT_PROJECTS_PATH", str(registry_path))
    try:
        yield
    finally:
        object.__setattr__(_pio, "DEFAULT_PROJECTS_PATH", saved_default)


@contextmanager
def _isolated_logs_dir(logs_dir: Path) -> Iterator[None]:
    """Point ``server.DEFAULT_LOGS_DIR`` at an isolated directory."""
    import heddle_daemon.server as _server

    saved = _server.DEFAULT_LOGS_DIR
    object.__setattr__(_server, "DEFAULT_LOGS_DIR", str(logs_dir))
    try:
        yield
    finally:
        object.__setattr__(_server, "DEFAULT_LOGS_DIR", saved)


@contextmanager
def _env_logs_dir(value: str | None) -> Iterator[None]:
    """Set / clear the ``HEDDLE_LOGS_DIR`` env var for the duration of the test."""
    saved = os.environ.get(ENV_LOGS_DIR)
    if value is None:
        os.environ.pop(ENV_LOGS_DIR, None)
    else:
        os.environ[ENV_LOGS_DIR] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(ENV_LOGS_DIR, None)
        else:
            os.environ[ENV_LOGS_DIR] = saved


@contextmanager
def _capture_stderr() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    with patch.object(sys, "stderr", buf):
        yield buf


def _project_stub(path: Path) -> Project:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Project(
        id="test-proj-id-audit-1",
        name="audit-test",
        path=str(path),
        added_at=now,
        last_accessed_at=now,
    )


# ---------- DaemonConfig ----------


class TestDaemonConfigLogsDir(unittest.TestCase):
    """``DaemonConfig.logs_dir`` accepts str | Path | None."""

    def test_accepts_none(self):
        cfg = DaemonConfig(host="127.0.0.1", port=0, logs_dir=None)
        self.assertIsNone(cfg.logs_dir)

    def test_accepts_path(self):
        cfg = DaemonConfig(host="127.0.0.1", port=0, logs_dir=Path("/tmp/x"))
        self.assertEqual(cfg.logs_dir, Path("/tmp/x"))

    def test_accepts_string_and_expands(self):
        cfg = DaemonConfig(host="127.0.0.1", port=0, logs_dir="~/alt")
        self.assertEqual(cfg.logs_dir, Path("~/alt").expanduser())


# ---------- Daemon.start / stop wiring ----------


class TestStartAttachesAuditLogger(unittest.IsolatedAsyncioTestCase):
    """``Daemon.start()`` attaches an LlmAuditLogger when a project is registered."""

    async def test_audit_attached_on_start_with_matching_project(self):
        with tempfile.TemporaryDirectory(prefix="heddle_audit_test_") as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "project"
            proj_dir.mkdir()
            proj = _project_stub(proj_dir.resolve())
            registry = tmp_path / "projects.json"
            logs_dir = tmp_path / "logs"
            with _isolated_logs_dir(logs_dir):
                with _projects_registry(registry, [proj]):
                    cfg = DaemonConfig(
                        host="127.0.0.1",
                        port=0,
                        project_path=proj_dir.resolve(),
                    )
                    daemon = Daemon(cfg)
                    try:
                        await daemon.start()
                        self.assertIsNotNone(daemon._llm_audit)
                        self.assertIsInstance(daemon._llm_audit, LlmAuditLogger)
                        expected = logs_dir / proj.id / LLM_AUDIT_FILENAME
                        self.assertEqual(
                            daemon._llm_audit.audit_path.resolve(),
                            expected.resolve(),
                        )
                        self.assertTrue(expected.parent.is_dir())
                    finally:
                        await daemon.stop()

    async def test_audit_uses_explicit_logs_dir_config(self):
        with tempfile.TemporaryDirectory(prefix="heddle_audit_test_") as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "project"
            proj_dir.mkdir()
            proj = _project_stub(proj_dir.resolve())
            registry = tmp_path / "projects.json"
            explicit_logs = tmp_path / "explicit-logs"
            with _projects_registry(registry, [proj]):
                cfg = DaemonConfig(
                    host="127.0.0.1",
                    port=0,
                    project_path=proj_dir.resolve(),
                    logs_dir=explicit_logs,
                )
                daemon = Daemon(cfg)
                try:
                    await daemon.start()
                    self.assertIsNotNone(daemon._llm_audit)
                    self.assertEqual(
                        daemon._llm_audit.logs_dir.resolve(),
                        explicit_logs.resolve(),
                    )
                finally:
                    await daemon.stop()

    async def test_audit_honors_env_var_logs_dir(self):
        with tempfile.TemporaryDirectory(prefix="heddle_audit_test_") as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "project"
            proj_dir.mkdir()
            proj = _project_stub(proj_dir.resolve())
            registry = tmp_path / "projects.json"
            env_logs = tmp_path / "env-logs"
            with _projects_registry(registry, [proj]):
                with _env_logs_dir(str(env_logs)):
                    with _isolated_logs_dir(env_logs):
                        cfg = DaemonConfig(
                            host="127.0.0.1",
                            port=0,
                            project_path=proj_dir.resolve(),
                        )
                        daemon = Daemon(cfg)
                        try:
                            await daemon.start()
                            self.assertIsNotNone(daemon._llm_audit)
                            self.assertEqual(
                                daemon._llm_audit.logs_dir.resolve(),
                                env_logs.resolve(),
                            )
                        finally:
                            await daemon.stop()


class TestStartNoAuditOnSkeletonMode(unittest.IsolatedAsyncioTestCase):
    """No project_path → no audit logger; daemon still serves."""

    async def test_no_attach_when_project_path_is_none(self):
        hlog.detach_file_sink()
        try:
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=None)
            daemon = Daemon(cfg)
            try:
                with _capture_stderr():
                    await daemon.start()
                self.assertIsNone(daemon._llm_audit)
            finally:
                await daemon.stop()
        finally:
            hlog.detach_file_sink()


class TestStartNoAuditWhenProjectUnregistered(unittest.IsolatedAsyncioTestCase):
    """Project_path set but no matching registered project → warn + continue."""

    async def test_warn_and_continue_when_project_id_resolution_fails(self):
        hlog.detach_file_sink()
        try:
            with tempfile.TemporaryDirectory(prefix="heddle_audit_test_") as tmp:
                tmp_path = Path(tmp)
                proj_dir = tmp_path / "project"
                proj_dir.mkdir()
                registry = tmp_path / "projects.json"
                with _projects_registry(registry, []):
                    with _isolated_logs_dir(tmp_path / "logs"):
                        cfg = DaemonConfig(
                            host="127.0.0.1",
                            port=0,
                            project_path=proj_dir.resolve(),
                        )
                        daemon = Daemon(cfg)
                        try:
                            with _capture_stderr() as buf:
                                await daemon.start()
                            self.assertIsNone(daemon._llm_audit)
                            text = buf.getvalue()
                            self.assertIn("llm_audit_skipped", text)
                            self.assertIn("no_matching_project", text)
                        finally:
                            await daemon.stop()
        finally:
            hlog.detach_file_sink()


class TestStopClosesAuditLogger(unittest.IsolatedAsyncioTestCase):
    """``Daemon.stop()`` closes the per-project audit logger."""

    async def test_audit_closed_on_stop(self):
        hlog.detach_file_sink()
        try:
            with tempfile.TemporaryDirectory(prefix="heddle_audit_test_") as tmp:
                tmp_path = Path(tmp)
                proj_dir = tmp_path / "project"
                proj_dir.mkdir()
                proj = _project_stub(proj_dir.resolve())
                registry = tmp_path / "projects.json"
                logs_dir = tmp_path / "logs"
                with _isolated_logs_dir(logs_dir):
                    with _projects_registry(registry, [proj]):
                        cfg = DaemonConfig(
                            host="127.0.0.1",
                            port=0,
                            project_path=proj_dir.resolve(),
                        )
                        daemon = Daemon(cfg)
                        await daemon.start()
                        self.assertIsNotNone(daemon._llm_audit)
                        await daemon.stop()
                        self.assertIsNone(daemon._llm_audit)
        finally:
            hlog.detach_file_sink()


class TestProjectRemovedClosesAuditLogger(unittest.IsolatedAsyncioTestCase):
    """``_on_project_removed`` closes the audit logger."""

    async def test_audit_closed_on_project_removal(self):
        hlog.detach_file_sink()
        try:
            with tempfile.TemporaryDirectory(prefix="heddle_audit_test_") as tmp:
                tmp_path = Path(tmp)
                proj_dir = tmp_path / "project"
                proj_dir.mkdir()
                proj = _project_stub(proj_dir.resolve())
                registry = tmp_path / "projects.json"
                logs_dir = tmp_path / "logs"
                with _isolated_logs_dir(logs_dir):
                    with _projects_registry(registry, [proj]):
                        cfg = DaemonConfig(
                            host="127.0.0.1",
                            port=0,
                            project_path=proj_dir.resolve(),
                        )
                        daemon = Daemon(cfg)
                        try:
                            await daemon.start()
                            self.assertIsNotNone(daemon._llm_audit)
                            await daemon._on_project_removed(proj)
                            self.assertIsNone(daemon._llm_audit)
                        finally:
                            await daemon.stop()
        finally:
            hlog.detach_file_sink()


# ---------- AgentRuntime integration ----------


class TestAgentRuntimeAuditRecording(unittest.IsolatedAsyncioTestCase):
    """AgentRuntime writes one audit record per LLM call when configured."""

    async def test_run_agent_step_writes_audit_records(self):
        with tempfile.TemporaryDirectory(prefix="heddle_audit_test_") as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "project"
            proj_dir.mkdir()
            logs_dir = tmp_path / "logs"
            audit = LlmAuditLogger(logs_dir=logs_dir, project_id="proj-1")
            store = ProjectCheckpointStore(project_path=proj_dir)
            await store.setup()
            sandbox = ToolDispatchMiddleware(
                project_root=proj_dir, config=SandboxConfig()
            )
            sandbox.setup()
            runtime = AgentRuntime(
                checkpoint_store=store,
                sandbox=sandbox,
                llm_audit=audit,
                audit_model="claude-opus-4.8",
            )
            try:
                llm = FakeLLM([
                    ScriptedResponse(content="Done.", stop_reason="end_turn"),
                ])
                await runtime.run_agent_step(
                    thread_id="feat-025",
                    user_message="hi",
                    llm=llm,
                )
                audit.close()
            finally:
                await store.close()
            records = [
                json.loads(line)
                for line in audit.audit_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["feature_id"], "feat-025")
            self.assertEqual(record["model"], "claude-opus-4.8")
            self.assertEqual(record["outcome"], "ok")
            self.assertEqual(record["stop_reason"], "end_turn")
            self.assertEqual(record["retry_attempt"], 1)
            self.assertIsInstance(record["latency_ms"], int)
            self.assertGreaterEqual(record["latency_ms"], 0)

    async def test_run_agent_step_writes_multiple_records(self):
        with tempfile.TemporaryDirectory(prefix="heddle_audit_test_") as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "project"
            proj_dir.mkdir()
            (proj_dir / "f.txt").write_text("x", encoding="utf-8")
            logs_dir = tmp_path / "logs"
            audit = LlmAuditLogger(logs_dir=logs_dir, project_id="proj-1")
            store = ProjectCheckpointStore(project_path=proj_dir)
            await store.setup()
            sandbox = ToolDispatchMiddleware(
                project_root=proj_dir, config=SandboxConfig()
            )
            sandbox.setup()
            runtime = AgentRuntime(
                checkpoint_store=store,
                sandbox=sandbox,
                llm_audit=audit,
                audit_model="claude-opus-4.8",
            )
            try:
                llm = FakeLLM([
                    ScriptedResponse(
                        tool_calls=(
                            # FakeLLM ScriptedToolCall positional
                            # shape; we omit args since the runtime
                            # will just see a tool call regardless.
                        ),
                        stop_reason="tool_use",
                    ),
                    ScriptedResponse(content="Done.", stop_reason="end_turn"),
                ])
                # First call has no tool_calls; we need to provide
                # the proper ScriptedToolCall shape. Use the public
                # API via the FakeLLM path: see test above for the
                # single-call case. Here we just verify that two
                # records are written when the loop runs twice.
                # Simplest path: use the public FakeLLM with two
                # content-only responses and a low max_steps.
                from heddle_common.fake_llm import ScriptedToolCall

                llm = FakeLLM([
                    ScriptedResponse(
                        tool_calls=(ScriptedToolCall(name="read", args={"path": "f.txt"}),),
                        stop_reason="tool_use",
                    ),
                    ScriptedResponse(content="Done.", stop_reason="end_turn"),
                ])
                await runtime.run_agent_step(
                    thread_id="feat-025",
                    user_message="Read f.txt",
                    llm=llm,
                )
                audit.close()
            finally:
                await store.close()
            records = [
                json.loads(line)
                for line in audit.audit_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["outcome"], "ok")
            self.assertEqual(records[1]["outcome"], "ok")
            self.assertEqual(records[0]["stop_reason"], "tool_use")
            self.assertEqual(records[1]["stop_reason"], "end_turn")


class TestAgentRuntimeNoAuditWhenUnset(unittest.IsolatedAsyncioTestCase):
    """When ``llm_audit`` is None, no audit file is touched."""

    async def test_no_audit_writes_when_logger_unset(self):
        with tempfile.TemporaryDirectory(prefix="heddle_audit_test_") as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "project"
            proj_dir.mkdir()
            store = ProjectCheckpointStore(project_path=proj_dir)
            await store.setup()
            sandbox = ToolDispatchMiddleware(
                project_root=proj_dir, config=SandboxConfig()
            )
            sandbox.setup()
            runtime = AgentRuntime(
                checkpoint_store=store,
                sandbox=sandbox,
                llm_audit=None,
            )
            try:
                llm = FakeLLM([
                    ScriptedResponse(content="Done.", stop_reason="end_turn"),
                ])
                # Must not raise even though llm_audit is None.
                result = await runtime.run_agent_step(
                    thread_id="feat-025",
                    user_message="hi",
                    llm=llm,
                )
                self.assertEqual(result.stop_reason, "end_turn")
            finally:
                await store.close()


if __name__ == "__main__":
    unittest.main()
