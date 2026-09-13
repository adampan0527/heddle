# SPDX-License-Identifier: Apache-2.0
"""Thread-presence lookups against a LangGraph checkpoint saver — feat-047.

The crash-recovery orchestrator (:mod:`heddle_daemon.crash_recovery`)
needs to know, for a given ``feature_id``, whether a persisted
LangGraph thread exists. LangGraph's ``AsyncSqliteSaver`` exposes
``alist()`` which iterates every persisted thread; this module is
the thin wrapper that:

  * performs a single ``alist(None)`` pass and returns the
    ``thread_id`` set, and
  * adapts the result into a sync callable the orchestrator can
    consult per-feature.

Mirrors the convention used elsewhere in the daemon:
``feature_id`` == LangGraph ``thread_id`` verbatim
(per ``ProjectCheckpointStore.thread_id_for_feature``).

Fail-closed: any exception from the saver (setup not called,
connection closed) returns an empty set. The orchestrator treats
an empty set as "no threads known" and blocks every in-flight
feature — the same fail-closed semantics as
:func:`heddle_daemon.checkpointing.is_checkpoint_db_healthy`.
"""

from __future__ import annotations

from typing import Protocol

from heddle_daemon.checkpointing import ProjectCheckpointStore

__all__ = [
    "ThreadPresence",
    "alist_thread_ids",
    "presence_from_set",
]


class ThreadPresence(Protocol):
    """Sync callable: ``True`` iff a checkpoint thread exists for the feature_id."""

    def __call__(self, feature_id: str) -> bool: ...


async def alist_thread_ids(
    checkpoint_store: ProjectCheckpointStore,
) -> set[str]:
    """Return the set of ``thread_id`` values known to ``checkpoint_store``.

    Performs a single ``alist(None)`` pass and returns the thread_ids
    verbatim. Fail-closed: any exception from the saver returns an
    empty set.

    The iteration is fully consumed into an in-memory set before
    returning so the underlying aiosqlite worker thread is released
    promptly — otherwise a Windows test that tries to delete the
    SQLite file in cleanup can hit a ``PermissionError``.
    """
    saver = checkpoint_store.get_checkpointer()
    ids: set[str] = set()
    try:
        async for tup in saver.alist(None):
            tid = tup.config["configurable"].get("thread_id")
            if isinstance(tid, str) and tid:
                ids.add(tid)
    except Exception:
        return set()
    return ids


def presence_from_set(known: set[str]) -> ThreadPresence:
    """Build a sync :class:`ThreadPresence` checker backed by a set.

    The closure captures ``known`` so repeated lookups stay O(1) and
    pure-function tests can drive the orchestrator without a real
    SQLite DB.
    """

    def _checker(feature_id: str) -> bool:
        return feature_id in known

    return _checker
