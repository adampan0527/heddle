# SPDX-License-Identifier: Apache-2.0
"""Structured JSON logging — one event per line to stderr.

Per TECH.md T-017. Schema:

    {
      "ts":         ISO8601 string (UTC),
      "level":      "debug" | "info" | "warn" | "error",
      "component":  "web" | "node" | "daemon" | "langgraph",
      "project_id": string | null,
      "feature_id": string | null,
      "event":      string,
      "msg":        string,
      ... arbitrary additional fields ...
    }

Any field whose name matches the redaction patterns (`api_key`, `secret`,
`token`, case-insensitive, underscore-insensitive) is replaced with
"[REDACTED]" in the serialized line — including nested dict / list
values. See T-015 / T-030 for the security rationale.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal

from .log_rotation import RotatingFileSink

__all__ = [
    "Component",
    "Level",
    "REDACT_PATTERNS",
    "attach_file_sink",
    "debug",
    "detach_file_sink",
    "emit",
    "error",
    "info",
    "redact",
    "warn",
]

Level = Literal["debug", "info", "warn", "error"]
Component = Literal["web", "node", "daemon", "langgraph"]

REDACT_PATTERNS: Final[tuple[str, ...]] = ("api_key", "secret", "token")


def _normalize_key(key: str) -> str:
    """Strip `_` and `-` and lowercase for redaction matching."""
    return key.lower().replace("_", "").replace("-", "")


_NORMALIZED_PATTERNS: Final[tuple[str, ...]] = tuple(_normalize_key(p) for p in REDACT_PATTERNS)


def _key_is_sensitive(key: str) -> bool:
    n = _normalize_key(key)
    return any(p in n for p in _NORMALIZED_PATTERNS)


def redact(obj: Any) -> Any:
    """Recursively replace any sensitive dict key's value with the literal "[REDACTED]".

    Lists and tuples are walked; other values pass through unchanged.
    """
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if _key_is_sensitive(k) else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(item) for item in obj]
    return obj


def emit(
    level: Level,
    component: Component,
    event: str,
    msg: str,
    *,
    project_id: str | None = None,
    feature_id: str | None = None,
    **fields: Any,
) -> None:
    """Emit a single structured log event to stderr as one JSON line.

    The line is flushed immediately so callers do not need to manage
    buffering. All fields are passed through the `redact()` filter
    before serialization.
    """
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "component": component,
        "project_id": project_id,
        "feature_id": feature_id,
        "event": event,
        "msg": msg,
        **fields,
    }
    payload = redact(payload)
    sys.stderr.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stderr.flush()
    # feat-015: optional rotating file sink. The module-level sink is
    # set by ``attach_file_sink`` (called from the daemon's start() /
    # Node.js supervisor's bootstrap); when present, every emitted line
    # is also written to disk with redaction already applied. The sink
    # must NOT re-redact (that would double-strip; backups inherit the
    # redaction guarantees of the caller). Idempotent attach / detach
    # are documented on the helpers below.
    if _file_sink is not None:
        _file_sink.emit(payload)


# Module-level rotating file sink — ``None`` means "no sink attached",
# which is the default for tests and any caller that does not opt in.
# Attach / detach are idempotent so a misordered call sequence from
# the daemon or the Node.js supervisor cannot crash the process.
_file_sink: RotatingFileSink | None = None


def attach_file_sink(
    path: str | Path,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> None:
    """Attach a :class:`RotatingFileSink` to the stderr emit path.

    Idempotent: if a sink is already attached it is closed before the
    new one is constructed, so a second ``attach_file_sink`` call
    never leaks the previous file handle. ``max_bytes`` and
    ``backup_count`` default to ``RotatingFileSink``'s env-var-aware
    defaults (50 MiB / 5 generations).
    """
    global _file_sink
    if _file_sink is not None:
        _file_sink.close()
        _file_sink = None
    _file_sink = RotatingFileSink(
        path, max_bytes=max_bytes, backup_count=backup_count
    )


def detach_file_sink() -> None:
    """Close + clear the module-level sink. Idempotent (no-op when no sink).

    Returns ``None`` whether or not a sink was attached so callers can
    invoke this in shutdown paths without checking state.
    """
    global _file_sink
    if _file_sink is None:
        return
    try:
        _file_sink.close()
    finally:
        _file_sink = None


def debug(component: Component, event: str, msg: str, **kw: Any) -> None:
    """Emit a debug-level event."""
    emit("debug", component, event, msg, **kw)


def info(component: Component, event: str, msg: str, **kw: Any) -> None:
    """Emit an info-level event."""
    emit("info", component, event, msg, **kw)


def warn(component: Component, event: str, msg: str, **kw: Any) -> None:
    """Emit a warn-level event."""
    emit("warn", component, event, msg, **kw)


def error(component: Component, event: str, msg: str, **kw: Any) -> None:
    """Emit an error-level event."""
    emit("error", component, event, msg, **kw)