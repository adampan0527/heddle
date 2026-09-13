# SPDX-License-Identifier: Apache-2.0
"""LLM-call audit log — feat-025.

Per feat-025 + TECH.md T-017: every LLM call the daemon makes is
recorded to a per-project ``~/.heddle/logs/<project_id>/llm-audit.jsonl``
sidecar. One JSON object per line; the schema is intentionally
narrow so downstream consumers (cost dashboards, SFT data prep,
anomaly detection) can rely on a stable shape:

    {
      "ts":                ISO8601 UTC string,
      "feature_id":        string,
      "model":             string,
      "prompt_tokens":     int | null,
      "completion_tokens": int | null,
      "latency_ms":        int,
      "outcome":           "ok" | "error" | "aborted",
      ... arbitrary extras ...
    }

The audit log is a **sidecar** to the daemon's stderr / rotating
``<project_id>.daemon.log`` stream (feat-015). It is **append-only**,
**non-rotating** in v0.1, and **not encrypted** — assume that the
audit file contents are visible to anyone who can read the project's
working directory. Sensitive fields passed in via ``**fields`` are
passed through :func:`heddle_common.logging.redact` so secrets (API
keys, bearer tokens) never appear on disk.

Why a separate file?
    The rotating ``<project_id>.daemon.log`` is human-readable
    structured logs (one JSON line per stderr event) that rotation
    policies shrink aggressively. The audit log is a long-lived,
    structured *dataset* — every LLM call is a sample, and rotating
    it would corrupt the dataset by hiding historical slices. v0.2
    will add size-based rotation; v0.1 keeps the file growing so the
    contract is "every call gets a record".
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from heddle_common.logging import redact

__all__ = [
    "DEFAULT_LOGS_DIR",
    "ENV_LOGS_DIR",
    "LLM_AUDIT_FILENAME",
    "LlmAuditLogger",
    "JsonLineAppender",
    "outcome_aborted",
    "outcome_error",
    "outcome_ok",
    "resolve_logs_dir",
]

# ---------- constants ----------

# Default root for per-project logs. ``~/.heddle/logs`` is the same
# root that feat-015 uses for the rotating ``<project_id>.daemon.log``
# file; the audit log lives in a per-project SUBDIRECTORY beneath
# this root (``<logs_dir>/<project_id>/llm-audit.jsonl``) so the two
# files never collide and can be inspected independently.
DEFAULT_LOGS_DIR: Final[str] = "~/.heddle/logs"

# Env-var name for overriding the logs root. Useful for tests and
# for users who want to relocate their logs off the home directory
# (e.g. on a read-only home or on a CI runner).
ENV_LOGS_DIR: Final[str] = "HEDDLE_LOGS_DIR"

# Filename inside the per-project directory. Different from
# feat-015's ``<project_id>.daemon.log`` so an operator can ``ls``
# the directory and see exactly one rotating log + one audit log.
LLM_AUDIT_FILENAME: Final[str] = "llm-audit.jsonl"


# ---------- outcome labels ----------


# Stable outcome strings so downstream consumers (cost dashboards,
# regression tests) can string-compare without parsing prose. Kept
# lowercase + ASCII so they survive any JSON consumer.
OUTCOME_OK: Final[str] = "ok"
OUTCOME_ERROR: Final[str] = "error"
OUTCOME_ABORTED: Final[str] = "aborted"


def outcome_ok() -> str:
    """The standard "LLM call succeeded" outcome label."""
    return OUTCOME_OK


def outcome_error() -> str:
    """The standard "LLM call raised an exception" outcome label."""
    return OUTCOME_ERROR


def outcome_aborted() -> str:
    """The standard "LLM call was cut short by external abort" outcome label."""
    return OUTCOME_ABORTED


# ---------- env-var helpers ----------


def resolve_logs_dir(env: dict[str, str] | None = None) -> Path:
    """Return the audit log root, honouring the env-var override.

    Falls back to :data:`DEFAULT_LOGS_DIR` when ``HEDDLE_LOGS_DIR`` is
    unset or empty. The returned path is **not** created here — the
    caller (typically :class:`LlmAuditLogger`) decides when to mkdir.
    ``~`` is expanded via :meth:`Path.expanduser`.
    """
    src = os.environ if env is None else env
    raw = src.get(ENV_LOGS_DIR)
    if raw is None or raw == "":
        return Path(DEFAULT_LOGS_DIR).expanduser()
    return Path(raw).expanduser()


# ---------- appender ----------


class JsonLineAppender:
    """Append a stream of pre-serialized dicts as one JSON object per line.

    Mirrors the role :class:`heddle_common.log_rotation.RotatingFileSink`
    plays for the stderr pipeline, but without rotation — the audit
    log is a long-lived dataset (see module docstring). Construction
    creates the parent directory on demand so the daemon never has
    to ``mkdir`` separately.

    Invariants:
        * ``append(record)`` is synchronous + flushed per call (same
          flush-per-line contract as the stderr emit pipeline so a
          daemon crash cannot lose a record that already happened).
        * ``close`` is idempotent (second call is a no-op).
        * The on-disk file is opened in append mode; concurrent writes
          from multiple threads are the caller's responsibility
          (v0.1 is single-active-project, so this is a non-issue).

    The appender applies :func:`heddle_common.logging.redact` to the
    incoming record before serialization so secrets never reach disk.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # ``encoding="utf-8"`` keeps non-ASCII content readable on
        # Windows + POSIX; ``newline=""`` is the documented idiom for
        # jsonl writers so ``\n`` is preserved verbatim instead of
        # being translated to ``\r\n`` on Windows.
        self._fh = open(self._path, mode="a", encoding="utf-8", newline="")

    @property
    def path(self) -> Path:
        """The active audit file's path."""
        return self._path

    def append(self, record: dict[str, Any]) -> None:
        """Serialize ``record`` as one JSON line and append it to disk.

        ``record`` may include arbitrary extras (e.g. ``stop_reason``,
        tool call counts); only the standard keys documented in the
        module docstring are required. This method does NOT apply
        redaction — the caller (``LlmAuditLogger.record_call``) is
        responsible for redacting extras before delegating here, so
        the standard schema fields (``prompt_tokens`` etc.) are
        preserved verbatim on disk.
        """
        if self._fh is None:
            return
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self) -> None:
        """Flush + close. Idempotent."""
        if self._fh is None:
            return
        try:
            self._fh.flush()
        finally:
            self._fh.close()
            self._fh = None  # type: ignore[assignment]


# ---------- audit logger ----------


def _now_iso() -> str:
    """ISO 8601 UTC timestamp for the audit record's ``ts`` field."""
    return datetime.now(timezone.utc).isoformat()


class LlmAuditLogger:
    """The per-project LLM-call audit logger.

    Owns one :class:`JsonLineAppender` per project; the daemon builds
    one of these in ``Daemon.start()`` and stashes it on
    ``Daemon._llm_audit`` so the agent runtime can call
    :meth:`record_call` after every LLM turn. ``None`` for
    skeleton-mode / project-less runs (the runtime guards against
    ``None``).

    The audit record shape is enforced by :meth:`record_call`; the
    runtime does not have to remember the field names. Extra metadata
    (e.g. ``stop_reason``, ``retry_attempt``) flows through
    ``**fields`` and lands in the JSON object verbatim (after
    redaction).
    """

    def __init__(self, logs_dir: str | Path, project_id: str) -> None:
        if not isinstance(project_id, str) or not project_id:
            raise ValueError(
                f"project_id must be a non-empty string; got {project_id!r}"
            )
        # Reject path-traversal-style project ids so a misconfigured
        # supervisor cannot smuggle ``..`` into the logs path.
        if "/" in project_id or "\\" in project_id or project_id in (".", ".."):
            raise ValueError(
                f"project_id {project_id!r} contains path separators; refusing"
            )
        self._logs_dir = Path(logs_dir)
        self._project_id = project_id
        self._project_dir = self._logs_dir / project_id
        self._appender = JsonLineAppender(self._project_dir / LLM_AUDIT_FILENAME)

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
    def audit_path(self) -> Path:
        return self._project_dir / LLM_AUDIT_FILENAME

    def record_call(
        self,
        *,
        feature_id: str,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_ms: int,
        outcome: str,
        **fields: Any,
    ) -> None:
        """Append one LLM-call audit record.

        Required positional fields are passed as keyword-only so the
        runtime's call site reads like a labelled tuple. ``**fields``
        accepts any extra metadata (``stop_reason``, ``retry_attempt``,
        ``error_type``, etc.) — these flow through :func:`redact` and
        are merged into the JSON line.

        ``latency_ms`` is an int measured via ``time.monotonic()`` so
        wall-clock jitter / NTP corrections do not bias the recorded
        duration. The audit ``ts`` field uses wall-clock UTC because
        downstream consumers want absolute time, not relative.

        The standard schema fields (``feature_id``, ``model``,
        ``prompt_tokens``, ``completion_tokens``, ``latency_ms``,
        ``outcome``) are written verbatim — they MUST NOT be
        redacted, otherwise the audit record's token counts would be
        lost. Only ``**fields`` flows through :func:`redact` so any
        extras like ``api_key`` (which a future caller might add by
        mistake) are still caught.
        """
        record: dict[str, Any] = {
            "ts": _now_iso(),
            "feature_id": feature_id,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": int(latency_ms),
            "outcome": outcome,
        }
        if fields:
            record.update(redact(fields))
        self._appender.append(record)

    def close(self) -> None:
        """Flush + close the underlying appender. Idempotent."""
        if self._appender is None:
            return
        self._appender.close()
