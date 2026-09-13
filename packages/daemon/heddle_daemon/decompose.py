# SPDX-License-Identifier: Apache-2.0
"""Auto-decomposition for work-classified dialog messages (feat-045).

Public entry point: :func:`propose_drafts`. When ``classify_intent``
(feat-044) returns ``"work"``, the daemon turns the user's
natural-language request into one or more *draft feature cards*.
The cards are surfaced in the Web UI's draft tray (feat-040); the
user reviews them, optionally edits, then confirms to commit them
into ``feature_list.json``.

v0.1 design (LLM-driven decomposition is post-v0.1):

  * Under ``HEDDLE_FAKE_LLM=1`` (T-018 / T-031) the function reads a
    fixture at ``<fixture_root>/decompose.json`` and returns its
    contents as the draft list. The fixture is the source of truth
    under fake mode; CI / tests pin its shape.
  * When ``HEDDLE_FAKE_LLM`` is NOT set (real LLM mode) the function
    returns a single placeholder draft and logs a warning. Wiring
    the real LLM decomposition is feat-046 — v0.1 is deterministic
    so e2e tests do not need provider SDKs.

This module owns the public entry point (``propose_drafts``) and the
two mode-specific dispatchers. The fixture loader lives in
``decompose_fixture.py``; the ``DraftCard`` dataclass and the error
hierarchy live in their own small modules to keep each file under
the 200-line limit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Optional

from heddle_common import logging as _logging
from heddle_common.fake_llm import is_fake_llm_enabled

from .decompose_errors import DecomposeError, EmptyMessageError
from .decompose_fixture import (
    DECOMPOSE_FIXTURE_NAME,
    MAX_DRAFTS,
    MIN_DRAFTS,
    load_decompose_fixture,
)
from .decompose_types import DraftCard

__all__ = [
    "DECOMPOSE_FIXTURE_NAME",
    "DraftCard",
    "MAX_DRAFTS",
    "MIN_DRAFTS",
    "PLACEHOLDER_DRAFT_DESCRIPTION",
    "PLACEHOLDER_DRAFT_TITLE",
    "propose_drafts",
]


PLACEHOLDER_DRAFT_TITLE: str = "Real LLM decomposition"
PLACEHOLDER_DRAFT_DESCRIPTION: str = (
    "TODO: feat-046 will wire real LLM-driven decomposition. "
    "For v0.1 this placeholder card is returned when "
    "HEDDLE_FAKE_LLM is not set."
)


# ---------- public function ----------


def propose_drafts(
    message: str,
    existing_features: list[dict[str, Any]],
    *,
    fixture_root: Optional[Path | str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> list[DraftCard]:
    """Return 1-3 draft cards for a work-classified user message.

    ``message`` is already classified as ``"work"`` — this function
    does NOT re-classify. ``existing_features`` is the current
    ``feature_list.json`` features array (the same one shown in the
    kanban); v0.1 only consumes its length (to pick the deterministic
    fallback id), but it is in the signature so a future feat-046
    LLM-driven path can build the D-029 context without an API break.

    Under ``HEDDLE_FAKE_LLM=1``: reads
    ``<fixture_root>/decompose.json`` (see ``decompose_fixture``).
    If the fixture is missing, returns a single deterministic
    fallback draft so tests do not crash.

    Otherwise: returns a single placeholder draft and logs a warning
    so the Web UI's draft tray still lights up (one card beats a
    dark tray).

    Raises:
        DecomposeError: When the fixture is present but malformed.
        EmptyMessageError: When ``message`` is empty after stripping
            whitespace. The routes layer rejects empty messages
            upstream; this is a defensive guard so library callers
            don't crash.
    """
    if not isinstance(message, str) or not message.strip():
        raise EmptyMessageError(
            "propose_drafts requires a non-empty message"
        )
    if not isinstance(existing_features, list):
        existing_features = list(existing_features)

    if is_fake_llm_enabled(env):
        return _propose_drafts_fake(
            message=message,
            existing_features=existing_features,
            fixture_root=fixture_root,
        )
    return _propose_drafts_real_placeholder(message=message)


# ---------- fake-LLM path ----------


def _resolve_fixture_root(fixture_root: Optional[Path | str]) -> Path:
    """Return the fixture root, expanding the user default if needed."""
    if fixture_root is None:
        return Path("~/.heddle/fake_fixtures").expanduser()
    return Path(fixture_root).expanduser()


def _propose_drafts_fake(
    *,
    message: str,
    existing_features: list[dict[str, Any]],
    fixture_root: Optional[Path | str],
) -> list[DraftCard]:
    """Fake-LLM path: load ``<fixture_root>/decompose.json``.

    If the fixture is missing we generate a deterministic single-draft
    fallback from the message hash so tests do not crash on a missing
    file. The fallback is NOT what real users see — under CI /
    local-test runs a fixture file is always present.
    """
    root = _resolve_fixture_root(fixture_root)
    fixture_path = root / DECOMPOSE_FIXTURE_NAME
    if fixture_path.exists():
        return load_decompose_fixture(fixture_path)
    _logging.warn(
        component="decompose",
        event="decompose_fixture_missing",
        msg=(
            f"HEDDLE_FAKE_LLM=1 but {fixture_path} not found; "
            "returning deterministic hash-derived fallback"
        ),
        fixture_path=str(fixture_path),
    )
    return [_hash_fallback_draft(message, existing_features)]


def _hash_fallback_draft(
    message: str, existing_features: list[dict[str, Any]]
) -> DraftCard:
    """Deterministic single-draft fallback when no fixture is on disk.

    Used only when ``HEDDLE_FAKE_LLM=1`` and ``decompose.json`` is
    missing — a test scenario, never a production path. The id is
    derived from a SHA-256 of the message so multiple invocations
    with the same input produce the same id (test stability).
    """
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:6]
    return DraftCard(
        id=f"temp-{digest}",
        title=f"Decompose: {message[:60]}",
        description=(
            "Deterministic fallback draft (no fixture on disk). "
            "Ship a decompose.json under the fake fixture root to "
            "pin the v0.1 output for tests."
        ),
        steps=(
            "Step 1: read the user's message and confirm scope.",
            "Step 2: write the corresponding code or config change.",
            "Step 3: run the relevant tests and verify the change.",
        ),
        depends_on=(),
        kind="feature",
    )


# ---------- real-LLM placeholder path ----------


def _propose_drafts_real_placeholder(*, message: str) -> list[DraftCard]:
    """Real-LLM mode placeholder — returns a single stub draft.

    feat-046 will replace this with the LLM-driven decomposition that
    includes the full feature_list context (D-029) and the
    granularity / classification heuristics (D-002 / D-006 / D-008).
    """
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:6]
    _logging.warn(
        component="decompose",
        event="real_llm_decompose_not_wired",
        msg=(
            "feat-045 v0.1: real-LLM decomposition is post-v0.1; "
            "returning a single placeholder draft. "
            "Set HEDDLE_FAKE_LLM=1 for fixture-driven tests."
        ),
        message_preview=message[:80],
    )
    return [
        DraftCard(
            id=f"temp-{digest}",
            title=PLACEHOLDER_DRAFT_TITLE,
            description=PLACEHOLDER_DRAFT_DESCRIPTION,
            steps=(),
            depends_on=(),
            kind="feature",
        ),
    ]
