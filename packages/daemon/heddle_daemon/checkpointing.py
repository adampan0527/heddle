# SPDX-License-Identifier: Apache-2.0
"""Per-project LangGraph checkpoint store — feat-018.

The heddle daemon runs as one asyncio process per project (v0.1
single-project, post-feat-012 `projects.json` registry). Every
project has its own working directory chosen by the user; the
daemon keeps a single SQLite checkpoint database at:

    <project_path>/.heddle/checkpoints.db

Each in-flight feature gets its own LangGraph thread, identified by
the feature_id directly (e.g. ``feat-018``). Thread state is
auto-persisted by LangGraph's ``AsyncSqliteSaver``; on daemon
restart the saver reconnects to the same DB and threads resume from
their last checkpoint — no per-feature resume protocol needed.

Why per-project, not per-daemon:
  * The user can wipe a project's ``.heddle/`` to start fresh
    without touching anyone else's work.
  * The OS-level path is unambiguous: a project registered in
    ``~/.heddle/projects.json`` (feat-012) owns exactly one DB.
  * Tests can spin up isolated project dirs under ``tmp_path``
    without races for a global DB file.

Crash safety (D-051):
  * SQLite is in WAL mode by default (LangGraph default), so a
    process killed mid-write leaves the DB consistent.
  * ``setup()`` is idempotent — re-running it after a clean shutdown
    just re-opens the connection.

This module is the single chokepoint for "where do I find the
project's LangGraph checkpointer?" — feat-019 (agent runtime) and
beyond import ``ProjectCheckpointStore`` here instead of constructing
``AsyncSqliteSaver.from_conn_string(...)`` themselves.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Final

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

__all__ = [
    "CHECKPOINTS_DIR_NAME",
    "DEFAULT_DB_FILENAME",
    "FEATURE_ID_THREAD_PATTERN",
    "ProjectCheckpointStore",
    "project_checkpoint_path",
]


# ---------- constants ----------

# Name of the per-project directory that holds heddle's local state.
# Matches the convention referenced in feat-014 / feat-028 / T-014.
CHECKPOINTS_DIR_NAME: Final[str] = ".heddle"

# Name of the SQLite checkpoint DB inside ``.heddle/``. Single file —
# LangGraph keeps checkpoints, writes, and channel history in tables
# inside this one DB.
DEFAULT_DB_FILENAME: Final[str] = "checkpoints.db"

# Thread IDs are feature IDs verbatim (e.g. ``feat-018``). The pattern
# is the same one ``feature_list_io.ID_REGEX`` enforces, but kept here
# as a separate constant so this module does not need to import the
# feature_list_io rules just to validate a thread_id.
FEATURE_ID_THREAD_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


# ---------- path helpers ----------


def project_checkpoint_path(project_path: Path | str, db_filename: str = DEFAULT_DB_FILENAME) -> Path:
    """Return ``<project_path>/.heddle/<db_filename>``.

    Resolves the project path so callers don't accidentally create
    the ``.heddle/`` directory at a symlink-resolved different
    location than the one registered in ``projects.json``. The
    parent directory is NOT created — that's ``setup()``'s job.
    """
    p = Path(project_path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        raise FileNotFoundError(
            f"project_path {p!r} does not exist or is not a directory; "
            f"refusing to create a checkpoint store against a non-existent project"
        )
    return p / CHECKPOINTS_DIR_NAME / db_filename


# ---------- store ----------


@dataclass
class ProjectCheckpointStore:
    """Owns the SQLite checkpoint DB for one project.

    One instance is created at daemon startup (in
    ``Daemon.start``) and lives for the lifetime of the process.
    ``setup()`` creates the ``.heddle/`` directory and runs
    LangGraph's schema migration; after that, ``get_checkpointer()``
    hands out the underlying ``AsyncSqliteSaver`` for any thread.

    Use as an async context manager:

        async with ProjectCheckpointStore(project_path).open() as store:
            checkpointer = store.get_checkpointer()
            graph = builder.compile(checkpointer=checkpointer)
            await graph.ainvoke(state, config={"configurable": {"thread_id": "feat-001"}})

    Or manage the lifecycle manually:

        store = ProjectCheckpointStore(project_path)
        await store.setup()
        try:
            ...
        finally:
            await store.close()
    """

    project_path: Path
    db_filename: str = DEFAULT_DB_FILENAME

    # Populated by ``setup()``; tests and callers should not touch
    # these directly. Use ``get_checkpointer()`` instead.
    #
    # We hold both the aiosqlite connection AND the wrapped
    # AsyncSqliteSaver because LangGraph's ``from_conn_string`` is a
    # context-manager-only convenience: it would close the underlying
    # connection on exit. Since the daemon needs the saver alive for
    # the lifetime of the process, we open the connection ourselves
    # and pass it directly to the saver.
    _saver: AsyncSqliteSaver | None = None
    _conn: object | None = None  # aiosqlite.Connection; typed loosely
                                  # to keep this module importable
                                  # without aiosqlite at type-check time.
    _setup_done: bool = False

    def __post_init__(self) -> None:
        # Validate project_path eagerly so callers see a clear error at
        # construction rather than at the first ``setup()`` call. The
        # resolved path is what we keep; this avoids a TOCTOU window
        # where a symlink is swapped between __post_init__ and setup.
        if not isinstance(self.project_path, Path):
            self.project_path = Path(self.project_path)
        self.project_path = self.project_path.expanduser().resolve()
        if not self.project_path.exists() or not self.project_path.is_dir():
            raise FileNotFoundError(
                f"project_path {self.project_path!r} does not exist or is not a directory; "
                f"refusing to create a checkpoint store"
            )
        if not isinstance(self.db_filename, str) or not self.db_filename:
            raise ValueError(f"db_filename must be a non-empty string; got {self.db_filename!r}")

    # ---- lifecycle ----

    @property
    def heddle_dir(self) -> Path:
        """``<project_path>/.heddle`` — created by ``setup()``."""
        return self.project_path / CHECKPOINTS_DIR_NAME

    @property
    def db_path(self) -> Path:
        """``<project_path>/.heddle/<db_filename>`` — created by ``setup()``."""
        return self.heddle_dir / self.db_filename

    @property
    def is_setup(self) -> bool:
        return self._setup_done

    async def setup(self) -> None:
        """Ensure ``.heddle/`` exists and the SQLite schema is initialized.

        Idempotent: calling twice with the same project_path is a
        no-op for the second call (the directory already exists and
        ``AsyncSqliteSaver.setup()`` is itself idempotent). Calling
        ``setup()`` again after ``close()`` re-opens the connection.
        """
        # 1. Create ``.heddle/`` if missing. ``parents=True`` covers
        #    the case where project_path itself is new; ``exist_ok``
        #    handles the (common) restart-after-clean-shutdown case.
        self.heddle_dir.mkdir(parents=True, exist_ok=True)

        # 2. Open the aiosqlite connection directly. We do NOT use
        #    ``AsyncSqliteSaver.from_conn_string`` because that helper
        #    is an async-context-manager that would close the
        #    connection on exit — incompatible with holding the saver
        #    for the whole daemon lifetime.
        import aiosqlite  # local import: keeps the module
                           # importable even if aiosqlite isn't installed
                           # (only needed at setup time).
        self._conn = await aiosqlite.connect(str(self.db_path))

        # 3. Wrap the connection in the LangGraph saver and run its
        #    schema migration (creates the checkpoints / writes /
        #    channel-history tables if absent).
        self._saver = AsyncSqliteSaver(self._conn)  # type: ignore[arg-type]
        await self._saver.setup()

        self._setup_done = True

    async def close(self) -> None:
        """Close the underlying SQLite connection.

        Safe to call multiple times. Safe to call without a prior
        ``setup()`` (no-op). On a daemon that is killed via SIGKILL
        before this runs, the OS reaps the file handles and SQLite's
        WAL mode keeps the DB consistent (D-051).
        """
        # Drop the saver first so a partially-closed connection can't
        # be handed back via get_checkpointer() during teardown.
        self._saver = None
        conn = self._conn
        self._conn = None
        if conn is not None:
            await conn.close()
        self._setup_done = False

    @asynccontextmanager
    async def open(self) -> AsyncIterator["ProjectCheckpointStore"]:
        """Context manager: ``setup()`` on enter, ``close()`` on exit.

        Equivalent to manually calling ``setup()`` / ``close()`` but
        in a ``with`` form that composes naturally with daemon
        startup code.
        """
        await self.setup()
        try:
            yield self
        finally:
            await self.close()

    # ---- checkpointer access ----

    def get_checkpointer(self) -> AsyncSqliteSaver:
        """Return the live ``AsyncSqliteSaver`` for compiling graphs.

        Raises ``RuntimeError`` if ``setup()`` has not been called.
        The returned object is shared — compiling multiple graphs
        against the same store is fine; LangGraph keys all writes by
        ``thread_id`` so they don't collide.
        """
        if self._saver is None:
            raise RuntimeError(
                "ProjectCheckpointStore.setup() must be called before get_checkpointer(); "
                "use `async with store.open() as s:` or call setup()/close() explicitly"
            )
        return self._saver

    # ---- thread-id mapping ----

    @staticmethod
    def thread_id_for_feature(feature_id: str) -> str:
        """Map a feature_id to its LangGraph thread_id.

        v0.1 mapping is identity: ``feat-018`` -> ``"feat-018"``.
        Centralized here so feat-019 (agent runtime) and the daemon's
        WS handlers share the same convention. If we ever need
        namespaces (e.g. per-attempt threads), this is the only
        place to change.

        Raises ``ValueError`` on malformed feature ids — defense in
        depth against a UI bug or a future feature-id rename
        accidentally leaking unsanitized input as a thread key.
        """
        if not isinstance(feature_id, str) or not FEATURE_ID_THREAD_PATTERN.match(feature_id):
            raise ValueError(
                f"feature_id {feature_id!r} is not a valid thread_id "
                f"(must match {FEATURE_ID_THREAD_PATTERN.pattern!r})"
            )
        return feature_id
