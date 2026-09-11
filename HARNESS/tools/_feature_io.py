"""Feature-list storage, constants, and metadata helpers."""
from __future__ import annotations
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any
from ._atomic_io import atomic_write_json, load_json as _atomic_load_json
from ._constants import STATUSES, VALID_CATEGORIES, VALID_PRIORITIES
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FEATURE_LIST_PATH = PROJECT_ROOT / "feature_list.json"
INDENT = 2
VALID_ATTEMPT_OUTCOMES = ("passing", "blocked", "deferred", "regressed")
def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)
def load_features() -> dict[str, Any]:
    if not FEATURE_LIST_PATH.exists(): fail(f"{FEATURE_LIST_PATH} not found. Are you in the project root?")
    try: data = _atomic_load_json(FEATURE_LIST_PATH)
    except json.JSONDecodeError as exc: fail(f"{FEATURE_LIST_PATH} is not valid JSON: {exc}")
    if "features" not in data or not isinstance(data["features"], list): fail(f"{FEATURE_LIST_PATH} must contain a 'features' array.")
    return data
def save_features(data: dict[str, Any]) -> None:
    atomic_write_json(FEATURE_LIST_PATH, data, indent=INDENT, ensure_ascii=False)
def status_of(feature: dict[str, Any]) -> str | None:
    return feature.get("status")
def recompute_metadata(data: dict[str, Any]) -> None:
    features=data["features"]; counts={s:0 for s in STATUSES}
    for feature in features:
        status=status_of(feature)
        if status in counts: counts[status]+=1
    data["metadata"]={"total_features":len(features),"passing":counts["passing"],"failing":sum(counts[s] for s in STATUSES if s != "passing"),"in_progress":counts["in_progress"],"blocked":counts["blocked"],"deferred":counts["deferred"],"last_updated":date.today().isoformat()}
