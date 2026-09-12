# SPDX-License-Identifier: Apache-2.0
"""Log rotation — feat-015.

Per TECH.md T-017 and feat-015: append structured log lines to a per-project
file with size-based rotation. The Python side wraps
``logging.handlers.RotatingFileHandler`` (stdlib); the Node.js side
(``packages/node/src/lib/rotating-file-sink.ts``) hand-rolls the same
behaviour so the env-var contract is shared across both runtimes.

Defaults
    ``DEFAULT_LOG_MAX_BYTES``  — 50 MiB
    ``DEFAULT_LOG_BACKUP_COUNT`` — 5 generations

Env-var contract (shared with the Node.js side; see ``rotating-file-sink.ts``):

    ``HEDDLE_LOG_MAX_BYTES``    int >= 1; default 50 MiB
    ``HEDDLE_LOG_BACKUP_COUNT`` int >= 1; default 5

``RotatingFileSink`` writes the **already-redacted** payload that
``heddle_common.logging.emit`` produces (the caller is responsible for
calling ``redact`` — backups therefore inherit the same redaction
guarantees as the stderr stream).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DEFAULT_LOG_BACKUP_COUNT",
    "DEFAULT_LOG_MAX_BYTES",
    "ENV_LOG_BACKUP_COUNT",
    "ENV_LOG_MAX_BYTES",
    "MAX_LOG_BACKUP_COUNT",
    "MAX_LOG_MAX_BYTES",
    "RotatingFileSink",
    "get_log_backup_count",
    "get_log_max_bytes",
]

# ---------- constants ----------

# Per feat-015: default 50 MiB per file, 5 generations retained. Both
# are overridable via env vars. The defaults are picked to balance
# debuggability (a 50 MiB file fits in vim / less; 5 generations give
# ~250 MiB of recent history) with disk usage on long-lived sessions.
DEFAULT_LOG_MAX_BYTES: Final[int] = 50 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT: Final[int] = 5

# Hard upper bounds — anything beyond these is rejected at construction
# time so a typo can't request a 100 GB log file or a 1000-generation
# rotation cascade. The values are generous (1 GiB max per file,
# 100 generations max) so the only things they rule out are obvious
# mistakes.
MAX_LOG_MAX_BYTES: Final[int] = 1 * 1024 * 1024 * 1024  # 1 GiB
MAX_LOG_BACKUP_COUNT: Final[int] = 100

# Env-var names — single source of truth for the Python daemon. The
# Node.js side reads the same names (see packages/node/src/lib/
# rotating-file-sink.ts).
ENV_LOG_MAX_BYTES: Final[str] = "HEDDLE_LOG_MAX_BYTES"
ENV_LOG_BACKUP_COUNT: Final[str] = "HEDDLE_LOG_BACKUP_COUNT"


# ---------- env-var helpers ----------


def _parse_int_env(name: str, raw: str | None, default: int) -> int:
    """Parse an env-var int or raise ``ValueError`` with a clear message.

    Mirrors the typed env-var helper pattern used elsewhere in
    ``heddle_common`` (e.g. ``configs_io``).
    """
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name}={raw!r} is not an integer; fix the env var"
        ) from exc


def get_log_max_bytes(env: dict[str, str] | None = None) -> int:
    """Read ``HEDDLE_LOG_MAX_BYTES`` (default ``DEFAULT_LOG_MAX_BYTES``).

    Returns ``DEFAULT_LOG_MAX_BYTES`` when unset / empty.
    Raises ``ValueError`` when the env var is set to a non-integer.
    """
    src = os.environ if env is None else env
    return _parse_int_env(ENV_LOG_MAX_BYTES, src.get(ENV_LOG_MAX_BYTES), DEFAULT_LOG_MAX_BYTES)


def get_log_backup_count(env: dict[str, str] | None = None) -> int:
    """Read ``HEDDLE_LOG_BACKUP_COUNT`` (default ``DEFAULT_LOG_BACKUP_COUNT``).

    Returns ``DEFAULT_LOG_BACKUP_COUNT`` when unset / empty.
    Raises ``ValueError`` when the env var is set to a non-integer.
    """
    src = os.environ if env is None else env
    return _parse_int_env(
        ENV_LOG_BACKUP_COUNT, src.get(ENV_LOG_BACKUP_COUNT), DEFAULT_LOG_BACKUP_COUNT
    )


# ---------- sink ----------


class _JsonLineFormatter(logging.Formatter):
    """Render a ``LogRecord`` carrying a pre-serialized JSON line in ``msg``.

    ``RotatingFileSink`` formats payloads once (in ``emit``) so that
    redaction + line-shape match ``heddle_common.logging.emit`` exactly;
    the handler then writes the resulting string verbatim. ``format``
    here is a pass-through so the stdlib handler's
    ``emit`` → ``format`` → ``write`` chain does not double-encode.

    No trailing newline is appended: ``StreamHandler.emit`` already
    adds ``self.terminator`` (``\\n`` by default) after the formatted
    message, so doing it here would produce a blank line between
    every JSON record.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        return record.getMessage()


class RotatingFileSink:
    """Append structured log lines to a size-rotated file.

    Wraps ``logging.handlers.RotatingFileHandler`` so the heavy
    lifting (rename + reopen + size tracking) is the stdlib's. The
    caller hands us **already-redacted** dicts via ``emit``; we
    serialize them as JSON before delegating to the handler's
    ``emit`` (which applies the formatter and writes).

    Invariants:

    * ``emit`` is synchronous + flushed per call (matches the stderr
      contract in ``heddle_common.logging.emit``).
    * ``close`` is idempotent (second call is a no-op).
    * Rotation triggers at ``bytes_written + len(line) >= max_bytes``
      (a single line may push the file slightly over ``max_bytes`` —
      this matches ``RotatingFileHandler``'s stdlib behaviour and
      the Node.js sink's contract).
    * ``__init__`` validates ``max_bytes > 0``, ``max_bytes <= 1 GiB``,
      ``backup_count > 0``, ``backup_count <= 100`` and raises
      ``ValueError`` on violation — see the constructor docstring.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int | None = None,
        backup_count: int | None = None,
    ) -> None:
        if max_bytes is None:
            max_bytes = get_log_max_bytes()
        if backup_count is None:
            backup_count = get_log_backup_count()
        self._validate(max_bytes, backup_count)
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._handler: logging.handlers.RotatingFileHandler | None = (
            logging.handlers.RotatingFileHandler(
                filename=str(self._path),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
                delay=False,
            )
        )
        assert self._handler is not None  # for type-checker
        self._handler.setFormatter(_JsonLineFormatter())

    @staticmethod
    def _validate(max_bytes: int, backup_count: int) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
            raise ValueError(
                f"max_bytes must be an int; got {type(max_bytes).__name__}"
            )
        if max_bytes <= 0:
            raise ValueError(
                f"max_bytes must be > 0; got {max_bytes}"
            )
        if max_bytes > MAX_LOG_MAX_BYTES:
            raise ValueError(
                f"max_bytes={max_bytes} exceeds MAX_LOG_MAX_BYTES="
                f"{MAX_LOG_MAX_BYTES} (1 GiB); refusing to rotate at that size"
            )
        if not isinstance(backup_count, int) or isinstance(backup_count, bool):
            raise ValueError(
                f"backup_count must be an int; got {type(backup_count).__name__}"
            )
        if backup_count <= 0:
            raise ValueError(
                f"backup_count must be > 0; got {backup_count}"
            )
        if backup_count > MAX_LOG_BACKUP_COUNT:
            raise ValueError(
                f"backup_count={backup_count} exceeds MAX_LOG_BACKUP_COUNT="
                f"{MAX_LOG_BACKUP_COUNT}; refusing to retain that many generations"
            )

    @property
    def path(self) -> Path:
        """The active log file's path."""
        return self._path

    @property
    def max_bytes(self) -> int:
        """Configured max-bytes threshold."""
        return self._max_bytes

    @property
    def backup_count(self) -> int:
        """Configured generation count."""
        return self._backup_count

    def emit(self, record: dict[str, Any]) -> None:
        """Serialize ``record`` as one JSON line and write it to disk.

        ``record`` MUST already be redacted (the caller runs
        ``heddle_common.logging.redact`` before delegating here) so the
        redaction guarantee is identical to the stderr stream.
        """
        if self._handler is None:
            return
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        record_obj = logging.LogRecord(
            name="heddle_rotating_file_sink",
            level=logging.INFO,
            pathname=str(self._path),
            lineno=0,
            msg=line,
            args=None,
            exc_info=None,
        )
        self._handler.emit(record_obj)

    def close(self) -> None:
        """Flush + close. Idempotent."""
        if self._handler is None:
            return
        try:
            self._handler.flush()
        finally:
            self._handler.close()
            self._handler = None