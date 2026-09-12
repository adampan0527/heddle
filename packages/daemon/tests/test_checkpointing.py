# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_daemon.checkpointing — feat-018.

Covers:
  * ProjectCheckpointStore setup / close lifecycle
  * Per-project path resolution (.heddle/checkpoints.db)
  * LangGraph AsyncSqliteSaver integration (write / read / resume)
  * Crash recovery: stop without close, reopen, checkpoint survives
  * Thread-id mapping (feature_id -> thread_id)
  * Defensive validation (missing project, bad thread id, double setup, double close)
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Iterator

from heddle_daemon.checkpointing import (
    CHECKPOINTS_DIR_NAME,
    DEFAULT_DB_FILENAME,
    FEATURE_ID_THREAD_PATTERN,
    ProjectCheckpointStore,
    project_checkpoint_path,
)


# ---------- helpers ----------


class _TempProject:
    """Context manager that creates a tmp project dir and cleans up.

    Used by every test that needs a real on-disk project_path. The
    default `tempfile.TemporaryDirectory` isn't enough because some
    tests want to keep the dir around after the store closes (to
    simulate a daemon restart); use ``.cleanup()`` explicitly in
    those cases.
    """

    def __init__(self) -> None:
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="heddle_cp_test_"))
        return self.path

    def __exit__(self, *exc_info: object) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)


async def _make_minimal_graph(checkpointer):
    """Build a tiny StateGraph that increments a counter on each invoke.

    Returns ``(compiled_graph, initial_state)``. The graph has one
    node that reads the state, bumps the counter by one, and writes
    it back. Invoking it twice with the same thread_id should
    accumulate (state persists across invokes, proving the
    checkpointer is doing its job).
    """
    from typing_extensions import TypedDict

    from langgraph.graph import END, START, StateGraph

    class State(TypedDict, total=False):
        counter: int

    async def bump(state: State) -> State:
        return {"counter": state.get("counter", 0) + 1}

    builder = StateGraph(State)
    builder.add_node("bump", bump)
    builder.add_edge(START, "bump")
    builder.add_edge("bump", END)
    graph = builder.compile(checkpointer=checkpointer)
    return graph, {}


# ---------- ProjectCheckpointStore: construction & path ----------


class TestConstruction(unittest.TestCase):
    """Constructor validates project_path and normalizes to resolved Path."""

    def test_construct_with_existing_dir(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            self.assertEqual(store.project_path, proj.resolve())
            self.assertEqual(store.db_filename, DEFAULT_DB_FILENAME)

    def test_construct_rejects_nonexistent_path(self):
        with _TempProject() as proj:
            missing = proj / "does" / "not" / "exist"
            with self.assertRaises(FileNotFoundError) as ctx:
                ProjectCheckpointStore(project_path=missing)
            self.assertIn("does not exist", str(ctx.exception).lower())

    def test_construct_rejects_file_instead_of_dir(self):
        with _TempProject() as proj:
            f = proj / "i-am-a-file"
            f.write_text("x", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                ProjectCheckpointStore(project_path=f)

    def test_construct_accepts_string_path(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=str(proj))
            self.assertEqual(store.project_path, proj.resolve())

    def test_construct_rejects_empty_db_filename(self):
        with _TempProject() as proj:
            with self.assertRaises(ValueError):
                ProjectCheckpointStore(project_path=proj, db_filename="")

    def test_default_paths_under_heddle_dir(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            self.assertEqual(store.heddle_dir, proj.resolve() / CHECKPOINTS_DIR_NAME)
            self.assertEqual(
                store.db_path,
                proj.resolve() / CHECKPOINTS_DIR_NAME / DEFAULT_DB_FILENAME,
            )

    def test_custom_db_filename(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj, db_filename="alt.db")
            self.assertEqual(store.db_path, proj.resolve() / CHECKPOINTS_DIR_NAME / "alt.db")


class TestProjectCheckpointPathHelper(unittest.TestCase):
    """The free function matches the property path and refuses bad input."""

    def test_returns_path_under_heddle_dir(self):
        with _TempProject() as proj:
            p = project_checkpoint_path(proj)
            self.assertEqual(p, proj.resolve() / CHECKPOINTS_DIR_NAME / DEFAULT_DB_FILENAME)

    def test_respects_custom_db_filename(self):
        with _TempProject() as proj:
            p = project_checkpoint_path(proj, db_filename="foo.db")
            self.assertEqual(p, proj.resolve() / CHECKPOINTS_DIR_NAME / "foo.db")

    def test_refuses_nonexistent_path(self):
        with _TempProject() as proj:
            with self.assertRaises(FileNotFoundError):
                project_checkpoint_path(proj / "nope")

    def test_does_not_create_directory(self):
        with _TempProject() as proj:
            project_checkpoint_path(proj)
            self.assertFalse((proj / CHECKPOINTS_DIR_NAME).exists())


# ---------- ProjectCheckpointStore: lifecycle ----------


class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    """setup() / close() / open() async-context-manager semantics."""

    async def test_setup_creates_heddle_dir_and_db(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            self.assertFalse(store.heddle_dir.exists())
            await store.setup()
            try:
                self.assertTrue(store.heddle_dir.exists())
                self.assertTrue(store.db_path.exists())
                self.assertTrue(store.is_setup)
                # The SQLite file should be a real DB (not a 0-byte stub).
                self.assertGreater(store.db_path.stat().st_size, 0)
            finally:
                await store.close()

    async def test_setup_is_idempotent(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            await store.setup()
            try:
                # Second setup re-opens cleanly (no errors, db_path
                # unchanged, is_setup stays True).
                await store.setup()
                self.assertTrue(store.is_setup)
                self.assertTrue(store.db_path.exists())
            finally:
                await store.close()

    async def test_close_without_setup_is_noop(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            # No setup(); close() must not raise.
            await store.close()
            self.assertFalse(store.heddle_dir.exists())

    async def test_double_close_is_safe(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            await store.setup()
            await store.close()
            await store.close()  # second close: no error
            self.assertFalse(store.is_setup)

    async def test_setup_after_close_reopens(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            await store.setup()
            await store.close()
            self.assertFalse(store.is_setup)
            await store.setup()
            self.assertTrue(store.is_setup)
            self.assertTrue(store.db_path.exists())
            await store.close()

    async def test_db_persists_after_close(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            await store.setup()
            await store.close()
            # The file is still on disk; the daemon restart will
            # re-open the same path. We verify by opening a
            # sqlite3 connection directly (sync) and checking the
            # LangGraph schema tables exist.
            with sqlite3.connect(store.db_path) as conn:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                tables = {row[0] for row in cur.fetchall()}
            self.assertIn("checkpoints", tables)
            self.assertIn("writes", tables)

    async def test_get_checkpointer_before_setup_raises(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            with self.assertRaises(RuntimeError) as ctx:
                store.get_checkpointer()
            self.assertIn("setup()", str(ctx.exception))

    async def test_get_checkpointer_returns_saver(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            await store.setup()
            try:
                cp = store.get_checkpointer()
                # LangGraph's saver class; the exact name varies
                # across versions but the package is stable.
                self.assertIn("AsyncSqliteSaver", type(cp).__name__)
            finally:
                await store.close()

    async def test_open_context_manager(self):
        with _TempProject() as proj:
            store = ProjectCheckpointStore(project_path=proj)
            async with store.open() as s:
                self.assertTrue(s.is_setup)
                self.assertTrue(s.db_path.exists())
            # Outside the with-block: store torn down.
            self.assertFalse(store.is_setup)


# ---------- thread_id mapping ----------


class TestThreadIdMapping(unittest.TestCase):
    """The feature_id -> thread_id mapping is identity + validated."""

    def test_identity_for_typical_feature_id(self):
        self.assertEqual(
            ProjectCheckpointStore.thread_id_for_feature("feat-018"),
            "feat-018",
        )

    def test_identity_preserves_underscores_and_digits(self):
        self.assertEqual(
            ProjectCheckpointStore.thread_id_for_feature("feat-999_abc-def"),
            "feat-999_abc-def",
        )

    def test_rejects_empty_string(self):
        with self.assertRaises(ValueError):
            ProjectCheckpointStore.thread_id_for_feature("")

    def test_rejects_string_with_whitespace(self):
        with self.assertRaises(ValueError):
            ProjectCheckpointStore.thread_id_for_feature("feat 018")
        with self.assertRaises(ValueError):
            ProjectCheckpointStore.thread_id_for_feature(" feat-018")
        with self.assertRaises(ValueError):
            ProjectCheckpointStore.thread_id_for_feature("feat-018 ")

    def test_rejects_special_characters(self):
        for bad in ("feat/018", "feat:018", "feat.018!", "../etc/passwd", "feat;018"):
            with self.assertRaises(ValueError):
                ProjectCheckpointStore.thread_id_for_feature(bad)

    def test_rejects_non_string(self):
        for bad in (None, 18, ["feat-018"], {"id": "feat-018"}):
            with self.assertRaises(ValueError):
                ProjectCheckpointStore.thread_id_for_feature(bad)  # type: ignore[arg-type]

    def test_pattern_matches_feature_list_io_id_regex_shape(self):
        """Sanity check: the feat-XXX shape we generate is accepted."""
        self.assertTrue(FEATURE_ID_THREAD_PATTERN.match("feat-018"))
        self.assertTrue(FEATURE_ID_THREAD_PATTERN.match("feat-1"))
        # And a digit-led id is rejected (matches feature_list_io).
        self.assertFalse(FEATURE_ID_THREAD_PATTERN.match("018-feat"))


# ---------- LangGraph integration: end-to-end checkpoint round-trip ----------


class TestLangGraphRoundTrip(unittest.IsolatedAsyncioTestCase):
    """Write a checkpoint, close the store, reopen, read it back.

    Proves feat-018 step 1+3: SqliteSaver is configured correctly
    and threads auto-persist + resume.
    """

    async def test_single_invoke_writes_checkpoint(self):
        with _TempProject() as proj:
            async with ProjectCheckpointStore(project_path=proj).open() as store:
                cp = store.get_checkpointer()
                graph, initial = await _make_minimal_graph(cp)
                config = {
                    "configurable": {
                        "thread_id": "feat-018",
                        "checkpoint_ns": "",
                    }
                }
                await graph.ainvoke(initial, config=config)
                # Inspect the persisted state via the checkpointer.
                state = await graph.aget_state(config)
                self.assertIsNotNone(state)
                self.assertEqual(state.values.get("counter"), 1)

    async def test_thread_resumes_after_store_close_and_reopen(self):
        """Kill the daemon (close the store), restart, resume the thread.

        The second invoke on a freshly opened store against the same
        project path MUST continue from where the first left off —
        counter should go from 1 to 2, not from 0 to 1.
        """
        with _TempProject() as proj:
            # --- "first daemon lifetime": bump counter to 1 ---
            store1 = ProjectCheckpointStore(project_path=proj)
            await store1.setup()
            try:
                cp1 = store1.get_checkpointer()
                graph1, _ = await _make_minimal_graph(cp1)
                config = {
                    "configurable": {
                        "thread_id": "feat-018",
                        "checkpoint_ns": "",
                    }
                }
                await graph1.ainvoke({}, config=config)
                state_after_first = await graph1.aget_state(config)
                self.assertEqual(state_after_first.values.get("counter"), 1)
            finally:
                # Simulate clean daemon shutdown.
                await store1.close()

            # --- "second daemon lifetime": reopen, new graph, resume ---
            store2 = ProjectCheckpointStore(project_path=proj)
            await store2.setup()
            try:
                cp2 = store2.get_checkpointer()
                # NEW graph instance against the same DB.
                graph2, _ = await _make_minimal_graph(cp2)
                state_before_resume = await graph2.aget_state(config)
                # The state survives the daemon restart.
                self.assertEqual(state_before_resume.values.get("counter"), 1)
                # A second invoke should continue from 1, not restart at 0.
                await graph2.ainvoke({}, config=config)
                state_after_resume = await graph2.aget_state(config)
                self.assertEqual(state_after_resume.values.get("counter"), 2)
            finally:
                await store2.close()

    async def test_crash_recovery_without_clean_close(self):
        """Simulate SIGKILL: don't call close(), just drop the store.

        The DB must still be usable by a fresh process. The OS
        releases the file handle when the process dies; SQLite's WAL
        mode + aiosqlite's lack of fsync-during-close means an
        ungraceful exit can leave WAL pages, but a fresh connection
        transparently recovers them on open.
        """
        with _TempProject() as proj:
            # First lifetime: write a checkpoint, then "crash".
            store1 = ProjectCheckpointStore(project_path=proj)
            await store1.setup()
            cp1 = store1.get_checkpointer()
            graph1, _ = await _make_minimal_graph(cp1)
            config = {
                "configurable": {
                    "thread_id": "feat-018",
                    "checkpoint_ns": "",
                }
            }
            await graph1.ainvoke({}, config=config)
            # No close() — this is the SIGKILL simulation. Drop the
            # reference; aiosqlite's worker thread is still alive but
            # the connection is leaked at the OS level when the test
            # function returns. (We rely on the test process exit to
            # actually reap the handle.)
            store1._conn = None  # type: ignore[attr-defined]
            store1._saver = None  # type: ignore[attr-defined]

            # --- Restart: a fresh store reopens the same DB ---
            store2 = ProjectCheckpointStore(project_path=proj)
            await store2.setup()
            try:
                cp2 = store2.get_checkpointer()
                graph2, _ = await _make_minimal_graph(cp2)
                state = await graph2.aget_state(config)
                self.assertIsNotNone(state)
                self.assertEqual(state.values.get("counter"), 1)
            finally:
                await store2.close()

    async def test_distinct_threads_dont_collide(self):
        with _TempProject() as proj:
            async with ProjectCheckpointStore(project_path=proj).open() as store:
                cp = store.get_checkpointer()
                graph, _ = await _make_minimal_graph(cp)
                for thread_id, expected in [("feat-018", 1), ("feat-019", 1), ("feat-020", 1)]:
                    cfg = {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": "",
                        }
                    }
                    await graph.ainvoke({}, config=cfg)
                    state = await graph.aget_state(cfg)
                    self.assertEqual(state.values.get("counter"), expected)


# ---------- daemon integration: WS handshake + project_path ----------


class TestDaemonCheckpointIntegration(unittest.IsolatedAsyncioTestCase):
    """Daemon.start with project_path wires up the checkpoint store.

    feat-018 step 2: "On daemon startup for a project, ensure
    .heddle/ exists; create checkpoints.db if not present."
    """

    async def test_daemon_start_creates_heddle_dir_and_db(self):
        import websockets

        from heddle_daemon.server import Daemon, DaemonConfig

        with _TempProject() as proj:
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            try:
                self.assertFalse(daemon.checkpoint_store)
                await daemon.start()
                self.assertTrue(daemon.checkpoint_store)
                self.assertTrue(daemon.checkpoint_store.is_setup)
                self.assertTrue((proj / ".heddle").exists())
                self.assertTrue((proj / ".heddle" / "checkpoints.db").exists())
                # Also: the WS port is bound and accepting.
                port = daemon.bound_port
                self.assertIsNotNone(port)
                async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.send('{"v":1,"type":"hello","feature_id":"feat-018"}')
                    resp = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    import json
                    env = json.loads(resp)
                    self.assertEqual(env["type"], "echo")
                    self.assertEqual(env["original_type"], "hello")
            finally:
                await daemon.stop()

    async def test_daemon_without_project_path_skips_checkpoint_store(self):
        """Skeleton mode: no project_path, no checkpoint store.

        feat-017's existing skeleton tests still work; the new field
        is purely additive.
        """
        from heddle_daemon.server import Daemon, DaemonConfig

        cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=None)
        daemon = Daemon(cfg)
        await daemon.start()
        try:
            self.assertIsNone(daemon.checkpoint_store)
        finally:
            await daemon.stop()

    async def test_daemon_stop_closes_checkpoint_store(self):
        from heddle_daemon.server import Daemon, DaemonConfig

        with _TempProject() as proj:
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            await daemon.start()
            await daemon.stop()
            # After stop, the store is torn down; getting a checkpointer
            # raises (because close() cleared _saver).
            self.assertIsNone(daemon.checkpoint_store)

    async def test_daemon_refuses_nonexistent_project_path(self):
        """DaemonConfig validation: missing project_path fails fast."""
        from heddle_daemon.server import DaemonConfig

        with _TempProject() as proj:
            with self.assertRaises(ValueError) as ctx:
                DaemonConfig(host="127.0.0.1", port=0, project_path=proj / "nope")
            self.assertIn("not exist", str(ctx.exception).lower())

    async def test_daemon_checkpoint_survives_restart(self):
        """Full daemon process restart preserves thread state.

        Mirrors feat-018 step 4: "start a feature, interrupt it at
        step 3 (kill daemon), restart daemon, resume feature; assert
        it continues from step 3 not step 1."
        """
        from heddle_daemon.server import Daemon, DaemonConfig

        config_dict = {
            "configurable": {
                "thread_id": "feat-018",
                "checkpoint_ns": "",
            }
        }

        with _TempProject() as proj:
            # First "process": bump counter to 1, then stop.
            cfg1 = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            d1 = Daemon(cfg1)
            await d1.start()
            try:
                cp = d1.checkpoint_store.get_checkpointer()
                graph, _ = await _make_minimal_graph(cp)
                await graph.ainvoke({}, config=config_dict)
            finally:
                await d1.stop()

            # Second "process": same project, brand-new daemon, resume.
            cfg2 = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            d2 = Daemon(cfg2)
            await d2.start()
            try:
                cp2 = d2.checkpoint_store.get_checkpointer()
                graph2, _ = await _make_minimal_graph(cp2)
                state = await graph2.aget_state(config_dict)
                self.assertEqual(state.values.get("counter"), 1)
                await graph2.ainvoke({}, config=config_dict)
                state2 = await graph2.aget_state(config_dict)
                self.assertEqual(state2.values.get("counter"), 2)
            finally:
                await d2.stop()


if __name__ == "__main__":
    unittest.main()
