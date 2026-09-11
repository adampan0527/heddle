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
from typing import Any, Final, Literal

__all__ = [
    "Component",
    "Level",
    "REDACT_PATTERNS",
    "debug",
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