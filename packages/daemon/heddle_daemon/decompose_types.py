# SPDX-License-Identifier: Apache-2.0
"""Public types for the auto-decomposition module (feat-045).

Kept in its own module so the main ``decompose.py`` and the fixture
loader ``decompose_fixture.py`` can both import ``DraftCard`` without
creating a circular dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DraftCard:
    """One draft feature proposed by the auto-decomposer.

    Wire shape (dict form) matches the v0.1 contract; the dataclass
    is for internal use so type-checkers catch mistakes in tests.
    Conversion to a plain dict happens via ``to_dict()`` before the
    value crosses the WS boundary (the Web UI never sees the
    dataclass).
    """

    id: str
    title: str
    description: str
    steps: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    kind: str = "feature"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "steps": list(self.steps),
            "depends_on": list(self.depends_on),
            "kind": self.kind,
        }
