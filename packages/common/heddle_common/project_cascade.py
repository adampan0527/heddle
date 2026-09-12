# SPDX-License-Identifier: Apache-2.0
"""Project removal cascade — feat-014.

When the user removes a project from the registry, three things must
happen, in order, before the registry entry is gone:

    1. Notify the running daemon (if any) so it can stop in-flight
       features for that project, close the per-project LangGraph
       checkpoint store, and stop writing to log files.
    2. Delete the per-project log file at
       ``~/.heddle/logs/<project_id>.log``.
    3. Delete the per-project log directory at
       ``~/.heddle/logs/<project_id>/``.

``projects_io.remove_project()`` (feat-012) only mutates the
registry. This module is the *cascade* that ties the registry
mutation to the on-disk side effects. The two halves are kept in
separate modules so ``projects_io`` stays focused on registry
correctness (it has no filesystem deps outside its own JSON file) and
this module can be imported only by callers that actually want the
side effects.

Why a hook (``on_remove``) instead of importing the daemon:

    The cascade needs to signal "stop the daemon's in-flight work
    for this project", but importing the daemon package from
    ``heddle_common`` would invert the dependency (daemon already
    imports common). The hook lets the daemon register its own
    teardown closure at startup, and CLI / tests pass synthetic
    hooks. ``on_remove=None`` skips the daemon side — the cascade
    still cleans up the on-disk logs.

Error strategy:

    - Step 1 (registry + hook) failure propagates as ``ProjectsError``:
      the registry is mutated (we deliberately do not roll back; the
      user already asked for the removal, and rolling back a partial
      cascade is worse than a partial failure with a structured log
      event).
    - Steps 2 / 3 (log file + log dir) failures are logged at warn
      level but do NOT raise — a missing log file is a valid
      end-state (the project may have been added but never run a
      feature). The caller still receives ``removed`` so the API
      shape is identical regardless of whether logs existed.

This module owns NO concurrency. The hook runs once per call; the
caller is responsible for not invoking this concurrently for the
same project_id.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Awaitable, Callable, Final, Optional

from . import logging as _logging
from . import projects_io

__all__ = [
    "DEFAULT_LOGS_DIR",
    "OnRemoveHook",
    "default_logs_dir",
    "remove_project_with_cascade",
]

# Where per-project log files live. Single source of truth — used by
# feat-025 (daemon-side log rotation) and feat-014's deletion logic.
# Per TECH.md T-017 / T-028: ``~/.heddle/logs/<project_id>.log`` for
# the structured stderr mirror, and ``~/.heddle/logs/<project_id>/``
# for per-feature sub-logs (``llm-audit.jsonl`` etc.).
DEFAULT_LOGS_DIR: Final[str] = "~/.heddle/logs"

# Signature of the daemon-side teardown hook. The daemon registers a
# callable that, given a removed Project, closes its checkpoint store
# and sets all in-flight thread stop events. Kept in this module (not
# in daemon) so library callers (CLI, tests) can pass any async
# callable without taking a dependency on the daemon package.
OnRemoveHook = Callable[[projects_io.Project], Awaitable[None]]


def default_logs_dir() -> Path:
    """Return the expanded ``~/.heddle/logs`` path.

    Single source of truth for tests + the CLI; both call this rather
    than re-deriving ``Path(DEFAULT_LOGS_DIR).expanduser()`` so a
    future rename is a one-line change.
    """
    return Path(DEFAULT_LOGS_DIR).expanduser()


def _resolve_logs_dir(log_dir: Path | str | None) -> Path:
    if log_dir is None:
        return default_logs_dir()
    return log_dir if isinstance(log_dir, Path) else Path(log_dir)


async def remove_project_with_cascade(
    projects_path: Path | str | None,
    project_id: str,
    *,
    on_remove: OnRemoveHook | None = None,
    log_dir: Path | str | None = None,
) -> Optional[projects_io.Project]:
    """Remove a project AND cascade the deletion to its log artifacts.

    Sequence:

        1. ``projects_io.remove_project(projects_path, project_id)``.
           Returns ``None`` if the id was not registered — the cascade
           is a no-op (no hook, no log deletion) in that case.
        2. If ``on_remove`` is set, await it with the removed Project.
           This is where the daemon closes its checkpoint store and
           stops in-flight threads. A hook failure propagates.
        3. Delete ``log_dir/<project_id>.log`` (best-effort).
        4. Delete ``log_dir/<project_id>/`` recursively (best-effort).
        5. Emit a structured ``project_removed`` event.

    Args:
        projects_path: location of ``projects.json`` (defaults to
            ``~/.heddle/projects.json``).
        project_id: the project to remove.
        on_remove: optional async hook to run before the file
            deletion. Used by the daemon to stop its own work for
            this project.
        log_dir: parent directory holding the per-project log file /
            sub-directory (defaults to ``~/.heddle/logs``).

    Returns:
        The removed ``Project`` (mirrors ``projects_io.remove_project``)
        or ``None`` if the id was not registered.

    Raises:
        ProjectsError: registry validation failed, the hook raised,
            or an unexpected I/O error happened. Log-file / log-dir
            *missing* errors are absorbed and logged at warn level — they
            are not exceptional.
    """
    removed = projects_io.remove_project(projects_path, project_id)
    if removed is None:
        # No-op for absent ids; mirrors projects_io.remove_project's
        # semantics and keeps the cascade contract identical for
        # callers that use the return value as "did anything happen?"
        return None

    # Step 2: notify the daemon (if any) BEFORE touching the
    # filesystem. Reasoning: the daemon may be holding open file
    # handles to the log we're about to delete; closing those handles
    # first is the only way to guarantee a clean unlink on Windows.
    if on_remove is not None:
        try:
            await on_remove(removed)
        except Exception as exc:
            # The registry entry is already gone — do NOT attempt to
            # roll back. Surface the failure with full context so the
            # caller can decide whether to retry.
            _logging.error(
                component="daemon",
                event="project_removal_hook_failed",
                msg=(
                    f"on_remove hook raised for project_id={removed.id!r}; "
                    f"registry already updated, files NOT deleted"
                ),
                project_id=removed.id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise projects_io.ProjectsError(
                f"on_remove hook for project_id={removed.id!r} raised "
                f"{type(exc).__name__}: {exc}; registry updated, log "
                f"files left intact (caller should investigate)"
            ) from exc

    logs = _resolve_logs_dir(log_dir)

    # Step 3: delete the per-project log file. Best-effort — a missing
    # file is a valid state (project may never have run a feature).
    log_file = logs / f"{removed.id}.log"
    try:
        log_file.unlink()
        _logging.info(
            component="daemon",
            event="project_log_file_deleted",
            msg=f"deleted {log_file}",
            project_id=removed.id,
            path=str(log_file),
        )
    except FileNotFoundError:
        _logging.info(
            component="daemon",
            event="project_log_file_absent",
            msg=f"log file already absent: {log_file}",
            project_id=removed.id,
            path=str(log_file),
        )
    except OSError as exc:
        # Permission / cross-device / locked-file etc. Log at warn,
        # do not raise — the registry removal is the user's intent
        # and we should not fail it because of log housekeeping.
        _logging.warn(
            component="daemon",
            event="project_log_file_delete_failed",
            msg=f"failed to delete {log_file}: {exc}",
            project_id=removed.id,
            path=str(log_file),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    # Step 4: delete the per-project log directory (recursively).
    # Same best-effort policy as step 3.
    log_subdir = logs / removed.id
    if log_subdir.exists():
        try:
            shutil.rmtree(log_subdir)
            _logging.info(
                component="daemon",
                event="project_log_dir_deleted",
                msg=f"deleted {log_subdir}",
                project_id=removed.id,
                path=str(log_subdir),
            )
        except OSError as exc:
            _logging.warn(
                component="daemon",
                event="project_log_dir_delete_failed",
                msg=f"failed to delete {log_subdir}: {exc}",
                project_id=removed.id,
                path=str(log_subdir),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    _logging.info(
        component="daemon",
        event="project_removed",
        msg=(
            f"project {removed.name!r} (id={removed.id}) removed with "
            f"cascade; logs cleaned up"
        ),
        project_id=removed.id,
        project_name=removed.name,
    )
    return removed