# SPDX-License-Identifier: Apache-2.0
"""Structured event log — feat-048.

Per feat-048 + DESIGN.md D-029: every state-changing operation
(status transition, attempt start / finish, draft confirm, dialog
submit) emits a structured event to BOTH the WebSocket stream AND a
per-project on-disk log file. This module owns the on-disk half; the
WS half is owned by ``heddle_daemon.routes.RouteHandler.emit_event``
(re-used, per feat-030).

The on-disk format is one JSON object per line, written to
``~/.heddle/logs/<project_id>/events.jsonl``. The schema is
intentionally narrow so downstream consumers (the Web UI debug
console, SFT data prep, regression diffing) can rely on a stable
shape:

    {
      "ts":         ISO 8601 UTC string,
      "event":      string (e.g. "feature_attempt_started"),
      "project_id": string,
      "feature_id": string | null,
      "payload":    {...}                  # event-specific extras
    }

Why a separate file from the rotating ``<project_id>.daemon.log``?
The rotating log is the human-readable structured stderr stream that
rotation policies shrink aggressively — it is the daemon's "console".
The events.jsonl sidecar is the durable, machine-readable record of
state changes; rotation would corrupt it by hiding historical slices
the same way it would corrupt the LLM audit log (feat-025). v0.1
keeps the file growing so the contract is "every state change gets a
record".

The events mirror the WS event names verbatim so the two streams
share a vocabulary. See feat-029 (event taxonomy) and feat-030 (WS
emitter) for the upstream design.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from heddle_common.logging import redact

__all__ = [
    "DEFAULT_EVENT_LOG_FILENAME",
    "EventLogLogger",
    "resolve_event_log_path",
]

# ---------- constants ----------


# Filename inside the per-project log directory. Sits next to
# ``llm-audit.jsonl`` (feat-025) and the rotating ``<project_id>.daemon.log``
# (feat-015) so an operator can ``ls`` the directory and see all
# three streams. Distinct filename keeps Windows file-sharing
# violations impossible and lets consumers subscribe to one stream
# without parsing the others.
DEFAULT_EVENT_LOG_FILENAME: Final[str] = "events.jsonl"


# ---------- path resolver ----------


def resolve_event_log_path(
    logs_dir: str | Path, project_id: str
) -> Path:
    """Return the on-disk path for ``<project_id>/events.jsonl``.

    The returned path is NOT created here — the caller (typically
    :class:`EventLogLogger`) is responsible for ``mkdir``. ``~`` is
    expanded via :meth:`Path.expanduser`.

    The path is computed as ``<logs_dir>/<project_id>/<FILENAME>``;
    this matches :mod:`heddle_daemon.llm_audit` so the directory
    layout stays uniform across the two sidecars.
    """
    return Path(logs_dir).expanduser() / project_id / DEFAULT_EVENT_LOG_FILENAME


# ---------- logger ----------


def _now_iso() -> str:
    """ISO 8601 UTC timestamp for the event record's ``ts`` field."""
    return datetime.now(timezone.utc).isoformat()


class EventLogLogger:
    """The per-project structured event-log writer.

    Owns one appender per project; the daemon builds one of these in
    ``Daemon.start()`` and stashes it on ``Daemon._event_log`` so
    ``RouteHandler.emit_event`` can dual-write to the file sidecar.
    ``None`` for skeleton-mode / project-less runs (the route handler
    guards against ``None``).

    Mirrors :class:`heddle_daemon.llm_audit.LlmAuditLogger`'s lifetime
    contract:

      * Construction creates the parent directory on demand.
      * :meth:`record` appends one JSON line, flushed per call.
      * :meth:`close` is idempotent (second call is a no-op).

    The event record schema (``ts`` / ``event`` / ``project_id`` /
    ``feature_id?`` / ``payload?``) is enforced by :meth:`record`;
    callers don't have to remember the field names. Extras flow
    through :func:`heddle_common.logging.redact` so secrets (API
    keys, bearer tokens) never reach disk.
    """

    def __init__(self, logs_dir: str | Path, project_id: str) -> None:
        if not isinstance(project_id, str) or not project_id:
            raise ValueError(
                f"project_id must be a non-empty string; got {project_id!r}"
            )
        # Reject path-traversal-style project ids so a misconfigured
        # supervisor cannot smuggle ``..`` into the log path.
        if "/" in project_id or "\\" in project_id or project_id in (".", ".."):
            raise ValueError(
                f"project_id {project_id!r} contains path separators; refusing"
            )
        self._logs_dir = Path(logs_dir)
        self._project_id = project_id
        self._project_dir = self._logs_dir / project_id
        self._path = self._project_dir / DEFAULT_EVENT_LOG_FILENAME
        self._project_dir.mkdir(parents=True, exist_ok=True)
        # ``encoding="utf-8"`` keeps non-ASCII content readable on
        # Windows + POSIX; ``newline=""`` is the documented idiom for
        # jsonl writers so ``\n`` is preserved verbatim instead of
        # being translated to ``\r\n`` on Windows.
        self._fh = open(self._path, mode="a", encoding="utf-8", newline="")

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def logs_dir(self) -> Path:
        return self._logs_dir

    @property
    def project_dir(self) -> Path:
        return self._project_dir

    @property
    def log_path(self) -> Path:
        """The active log file's path."""
        return self._path

    def record(
        self,
        *,
        event: str,
        project_id: str,
        feature_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one structured event record.

        Keyword-only signature mirrors the WS event shape so the
        caller (the route handler's ``emit_event``) hands the same
        dict in to both sinks.

        Field rules:

          * ``ts`` is stamped server-side via UTC ISO 8601 — callers
            MUST NOT pre-stamp it; the file is a single timestamp
            source so an out-of-order caller cannot desync the audit
            trail.
          * ``payload`` flows through :func:`redact` so a caller
            that mistakenly appends an ``api_key`` field gets the
            standard ``[REDACTED]`` substitution.
          * ``project_id`` on disk must match the logger's bound
            project; we assert equality (rather than silently
            accepting a mismatch) so a bug in the route handler
            cannot split one project's event stream across multiple
            files.
        """
        if self._fh is None:
            return
        if not isinstance(event, str) or not event:
            raise ValueError(f"event must be a non-empty string; got {event!r}")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError(
                f"project_id must be a non-empty string; got {project_id!r}"
            )
        if project_id != self._project_id:
            raise ValueError(
                f"project_id {project_id!r} does not match bound "
                f"project {self._project_id!r}; refusing to write "
                f"to events.jsonl for the wrong project"
            )
        if feature_id is not None and not isinstance(feature_id, str):
            raise ValueError(
                f"feature_id must be a string or None; got {type(feature_id).__name__}"
            )
        if payload is not None and not isinstance(payload, dict):
            raise ValueError(
                f"payload must be a dict or None; got {type(payload).__name__}"
            )
        record: dict[str, Any] = {
            "ts": _now_iso(),
            "event": event,
            "project_id": project_id,
            "feature_id": feature_id,
            "payload": redact(payload) if payload else {},
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self) -> None:
        """Flush + close the underlying file handle. Idempotent."""
        if self._fh is None:
            return
        try:
            self._fh.flush()
        finally:
            self._fh.close()
            self._fh = None  # type: ignore[assignment]
