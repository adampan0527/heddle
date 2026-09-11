"""Tiny helpers for building / reading feature_list.json in tests.

Kept intentionally minimal — these are not pytest fixtures so they can
be called from any test (including parametrized ones) without the
fixture-resolution ordering getting in the way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def make_features_json(
    tmp_path: Path,
    features: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a feature_list.json shaped file under ``tmp_path``.

    Returns the path to the created file. If ``features`` is None the
    file contains an empty ``features`` array plus minimal metadata so
    the on-disk shape matches what the real tool writes.
    """
    if features is None:
        features = []
    path = tmp_path / "feature_list.json"
    data = {
        "project_name": "test project",
        "features": features,
        "metadata": {
            "total_features": len(features),
            "passing": sum(1 for f in features if f.get("status") == "passing"),
            "failing": sum(
                1 for f in features
                if f.get("status") in ("pending", "in_progress", "blocked", "deferred")
            ),
            "in_progress": sum(1 for f in features if f.get("status") == "in_progress"),
            "blocked": sum(1 for f in features if f.get("status") == "blocked"),
            "deferred": sum(1 for f in features if f.get("status") == "deferred"),
            "last_updated": "2026-07-23",
        },
    }
    # Match the project's stable format: indent=2, ensure_ascii=False,
    # trailing newline. Keeping the same shape means tests that load
    # the file back via ``feature_list.load_features`` get identical
    # bytes to what the tool itself would write.
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def read_features_json(tmp_path: Path) -> dict[str, Any]:
    """Parse and return the feature_list.json file under ``tmp_path``.

    Raises FileNotFoundError if the file does not exist (matches
    ``_atomic_io.load_json`` behavior). Tests that need to assert
    against the on-disk shape after a tool call should use this
    rather than going through ``feature_list.load_features`` so the
    test sees the bytes the tool actually wrote.
    """
    path = tmp_path / "feature_list.json"
    return json.loads(path.read_text(encoding="utf-8"))
