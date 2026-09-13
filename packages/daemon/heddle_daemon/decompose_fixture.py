# SPDX-License-Identifier: Apache-2.0
"""Fixture loader for ``decompose.json`` (feat-045).

The fake-LLM path of ``propose_drafts`` reads
``<fixture_root>/decompose.json`` and validates each entry against the
v0.1 schema. This module owns that loader so the main ``decompose.py``
stays focused on the public function.

Schema (one top-level array of objects)::

    [
      {
        "id": "temp-001",
        "title": "...",
        "description": "...",
        "steps": ["..."],
        "depends_on": [],
        "kind": "feature"
      },
      ...
    ]

Validation rules:
  - Root must be a JSON array of length 1-3.
  - Each entry has only the documented keys.
  - Required keys: ``id``, ``title``, ``description``. Optional
    with defaults: ``steps`` (empty), ``depends_on`` (empty),
    ``kind`` (``"feature"``).
  - ``id`` starts with ``temp-`` (the convention the Web UI's
    draft tray matches on per feat-040).
  - ``kind`` is in ``heddle_common.feature_list_io.KINDS``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from heddle_common.feature_list_io import DEFAULT_KIND, KINDS

from .decompose_types import DraftCard
from .decompose_errors import DecomposeError

# Fixture filename under ``<fixture_root>/`` consumed in fake-LLM mode
# (see ``decompose.propose_drafts``). Mirrors the per-feature fixture
# convention used by feat-031 / agent_runtime — the root just has a
# fixed name here because the LLM-call site is not feature-scoped
# (it's dialog-scoped).
DECOMPOSE_FIXTURE_NAME: str = "decompose.json"

# v0.1 envelope: the LLM is asked for 1-3 draft cards per work message
# (D-002 / D-006 / D-008). The fixture is validated against this band.
MIN_DRAFTS: int = 1
MAX_DRAFTS: int = 3

# Allowed top-level keys on a draft entry. Anything else is a typo that
# should fail loud at load time, not silently disappear.
_ALLOWED_DRAFT_KEYS: frozenset[str] = frozenset(
    {"id", "title", "description", "steps", "depends_on", "kind"}
)


def load_decompose_fixture(path: Path | str) -> list[DraftCard]:
    """Read and validate the ``decompose.json`` fixture."""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise DecomposeError(
            f"cannot read decompose fixture {p}: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DecomposeError(
            f"decompose fixture {p} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise DecomposeError(
            f"decompose fixture {p} root must be an array; "
            f"got {type(data).__name__}"
        )
    n = len(data)
    if n < MIN_DRAFTS or n > MAX_DRAFTS:
        raise DecomposeError(
            f"decompose fixture {p} has {n} drafts; "
            f"expected {MIN_DRAFTS}-{MAX_DRAFTS}"
        )
    return [_parse_draft_entry(p, i, entry) for i, entry in enumerate(data)]


def _parse_draft_entry(path: Path, idx: int, entry: Any) -> DraftCard:
    """Validate one fixture entry and return a ``DraftCard``."""
    if not isinstance(entry, dict):
        raise DecomposeError(
            f"decompose fixture {path}[{idx}] must be an object; "
            f"got {type(entry).__name__}"
        )
    extra = set(entry.keys()) - _ALLOWED_DRAFT_KEYS
    if extra:
        raise DecomposeError(
            f"decompose fixture {path}[{idx}] has unknown keys: "
            f"{sorted(extra)}. Allowed: {sorted(_ALLOWED_DRAFT_KEYS)}."
        )
    raw_id = entry.get("id", "")
    if not isinstance(raw_id, str) or not raw_id.startswith("temp-"):
        raise DecomposeError(
            f"decompose fixture {path}[{idx}].id must start with "
            f"'temp-'; got {raw_id!r}"
        )
    title = entry.get("title", "")
    if not isinstance(title, str) or not title.strip():
        raise DecomposeError(
            f"decompose fixture {path}[{idx}].title must be a "
            f"non-empty string; got {title!r}"
        )
    description = entry.get("description", "")
    if not isinstance(description, str) or not description.strip():
        raise DecomposeError(
            f"decompose fixture {path}[{idx}].description must be a "
            f"non-empty string; got {description!r}"
        )
    steps = entry.get("steps", [])
    if not isinstance(steps, list) or not all(
        isinstance(s, str) and s.strip() for s in steps
    ):
        raise DecomposeError(
            f"decompose fixture {path}[{idx}].steps must be a list "
            f"of non-empty strings; got {steps!r}"
        )
    deps = entry.get("depends_on", [])
    if not isinstance(deps, list) or not all(
        isinstance(d, str) and d.strip() for d in deps
    ):
        raise DecomposeError(
            f"decompose fixture {path}[{idx}].depends_on must be a "
            f"list of non-empty strings; got {deps!r}"
        )
    kind = entry.get("kind", DEFAULT_KIND)
    if kind not in KINDS:
        raise DecomposeError(
            f"decompose fixture {path}[{idx}].kind must be one of "
            f"{sorted(KINDS)}; got {kind!r}"
        )
    return DraftCard(
        id=raw_id,
        title=title.strip(),
        description=description.strip(),
        steps=tuple(s.strip() for s in steps),
        depends_on=tuple(d.strip() for d in deps),
        kind=kind,
    )
