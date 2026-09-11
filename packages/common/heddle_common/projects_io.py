# SPDX-License-Identifier: Apache-2.0
"""projects_io — read/write/manage the `~/.heddle/projects.json` registry.

Per feat-012 / DESIGN.md D-057 (multi-project registry) / T-015
(per-project data layout). The registry holds the list of projects the
heddle instance currently manages; v0.1 supports *one* project at a
time (the rest of the registry is present for post-v0.1 concurrent
execution, but only one row is "active" in the UI).

Schema (feat-012 step 1):

    {
      "version": 1,
      "projects": [
        {
          "id": "<uuid4>",
          "name": "<user-facing label; defaults to basename(path)>",
          "path": "<absolute filesystem path>",
          "added_at": "<ISO 8601 datetime, second precision>",
          "last_accessed_at": "<ISO 8601 datetime, second precision>"
        }
      ]
    }

Why JSON and not YAML:

  - This file is machine-managed (CLI adds, daemon reads) and never
    edited by hand. YAML's whitespace rules would only get in the way.
  - Atomic JSON writes (`atomic_io.atomic_write_json`) already exist;
    adding YAML here would double the dependency surface.

Why a `version` field:

  - Future schema additions (e.g. `default_model`, `tags[]`) bump this.
    `load_projects` rejects unknown versions with a clear error rather
    than silently dropping rows (mirrors `feature_list_io's
    `schema_version` pattern from feat-009 / T-022).

This module lives in `heddle_common/` (not the daemon package) so the
CLI (feat-050 / feat-012) and the daemon (feat-014 / feat-018) share
one implementation. Both pass an explicit `path`; the default is
`~/.heddle/projects.json` per D-057.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import atomic_io

__all__ = [
    "DEFAULT_PROJECTS_DIR",
    "DEFAULT_PROJECTS_PATH",
    "MAX_PROJECTS_BYTES",
    "MAX_PROJECTS",
    "PROJECTS_SCHEMA_VERSION",
    "Project",
    "ProjectsError",
    "add_project",
    "default_projects_path",
    "list_projects",
    "load_projects",
    "remove_project",
    "save_projects",
    "touch_project",
]

# ---------- constants ----------

DEFAULT_PROJECTS_DIR: str = "~/.heddle"
DEFAULT_PROJECTS_PATH: str = "~/.heddle/projects.json"

# Defensive caps against accidental /tmp mounts or symlink races.
MAX_PROJECTS_BYTES: int = 1_048_576  # 1 MiB
MAX_PROJECTS: int = 1000

# Schema version of projects.json itself. Bump when adding/removing
# a top-level field. Loaded files with a higher version than this
# build understands are rejected with a clear error (mirrors the
# feature_list_io `schema_version` pattern from feat-009).
PROJECTS_SCHEMA_VERSION: int = 1
PROJECTS_SCHEMA_VERSION_MAX: int = 1


# ---------- error type ----------


class ProjectsError(Exception):
    """Raised for any user-facing projects.json problem.

    Distinct from `fail()` (which SystemExits). ProjectsError lets
    callers in the CLI layer catch + render a friendly message instead
    of the raw stderr trace.
    """


# ---------- dataclass ----------


@dataclass(frozen=True)
class Project:
    """One registered project entry (D-057 / feat-012)."""

    id: str
    name: str
    path: str
    added_at: str  # ISO 8601 second-precision datetime
    last_accessed_at: str  # ISO 8601 second-precision datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "added_at": self.added_at,
            "last_accessed_at": self.last_accessed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        missing = {"id", "name", "path", "added_at", "last_accessed_at"} - set(data.keys())
        if missing:
            raise ProjectsError(
                f"project entry missing required keys {sorted(missing)}; "
                f"got keys {sorted(data.keys())}"
            )
        for key in ("id", "name", "path", "added_at", "last_accessed_at"):
            v = data[key]
            if not isinstance(v, str) or not v:
                raise ProjectsError(
                    f"project entry `{key}` must be a non-empty string; "
                    f"got {v!r}"
                )
        return cls(
            id=data["id"],
            name=data["name"],
            path=data["path"],
            added_at=data["added_at"],
            last_accessed_at=data["last_accessed_at"],
        )


# ---------- IO ----------


def default_projects_path() -> Path:
    """Return the expanded default `~/.heddle/projects.json` path."""
    return Path(DEFAULT_PROJECTS_PATH).expanduser()


def _resolve(path: Path | str | None) -> Path:
    if path is None:
        return default_projects_path()
    return path if isinstance(path, Path) else Path(path)


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 second-precision string.

    Always UTC + "Z" suffix for stable cross-platform diffs. The
    last_accessed_at / added_at fields are compared as strings for
    ordering, so consistent formatting matters.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fail(msg: str) -> None:
    """Print to stderr and exit (matches configs_io / feature_list_io style)."""
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def load_projects(path: Path | str | None = None) -> dict[str, Project]:
    """Read and parse projects.json into a dict keyed by `id`.

    Returns an empty dict if the file does not exist (the first-run
    state). Raises `ProjectsError` on schema / parse problems.
    """
    p = _resolve(path)
    if not p.exists():
        return {}
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise ProjectsError(f"cannot stat {p}: {exc}")
    if size > MAX_PROJECTS_BYTES:
        raise ProjectsError(
            f"{p} is {size} bytes; max is {MAX_PROJECTS_BYTES}. "
            "Check the path — large projects.json usually means a wrong file."
        )
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectsError(f"cannot read {p}: {exc}")
    try:
        import json as _json

        data = _json.loads(raw)
    except Exception as exc:
        raise ProjectsError(f"{p} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise ProjectsError(
            f"{p} root must be an object; got {type(data).__name__}"
        )
    # version gate — refuse unknown future versions explicitly so a
    # silent field drop can never happen.
    raw_version = data.get("version")
    if raw_version is None:
        # Pre-versioned file is treated as legacy (v0) and re-stamped
        # on the next save(). This matches the feature_list_io policy
        # for the `schema_version` field.
        data["version"] = 0
    elif not isinstance(raw_version, int) or isinstance(raw_version, bool):
        raise ProjectsError(
            f"{p} has a non-integer version: {raw_version!r}; fix the file by hand"
        )
    elif raw_version < 0:
        raise ProjectsError(f"{p} has a negative version: {raw_version}")
    elif raw_version > PROJECTS_SCHEMA_VERSION_MAX:
        raise ProjectsError(
            f"{p} has version={raw_version} but this build of "
            f"heddle_common supports at most version="
            f"{PROJECTS_SCHEMA_VERSION_MAX}. Please upgrade heddle."
        )

    projects_raw = data.get("projects")
    if projects_raw is None:
        return {}
    if not isinstance(projects_raw, list):
        raise ProjectsError(
            f"{p} `projects` must be a list; got {type(projects_raw).__name__}"
        )
    if len(projects_raw) > MAX_PROJECTS:
        raise ProjectsError(
            f"{p} has {len(projects_raw)} entries; max is {MAX_PROJECTS}"
        )
    out: dict[str, Project] = {}
    for i, entry in enumerate(projects_raw):
        if not isinstance(entry, dict):
            raise ProjectsError(
                f"{p} projects[{i}] must be an object; "
                f"got {type(entry).__name__}"
            )
        proj = Project.from_dict(entry)
        if proj.id in out:
            raise ProjectsError(f"{p} has duplicate project id {proj.id!r}")
        out[proj.id] = proj
    return out


def save_projects(
    path: Path | str | None, projects: dict[str, Project]
) -> None:
    """Atomically write projects to JSON.

    Always stamps the current `PROJECTS_SCHEMA_VERSION` so the on-disk
    schema is never stale. The order of the `projects` list follows
    the insertion order of the input dict; callers that want a stable
    ordering should pass an `OrderedDict`.
    """
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    payload = {
        "version": PROJECTS_SCHEMA_VERSION,
        "projects": [proj.to_dict() for proj in projects.values()],
    }
    text = _json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    atomic_io.atomic_write_text(p, text, encoding="utf-8")


# ---------- mutation helpers ----------


def add_project(
    path: Path | str | None,
    *,
    project_path: str,
    name: str | None = None,
    project_id: str | None = None,
) -> Project:
    """Register a new project. Returns the created Project.

    Args:
        path: location of projects.json (default `~/.heddle/projects.json`)
        project_path: absolute path of the user-selected project directory;
            must exist on disk.
        name: optional user-facing label; defaults to basename(project_path).
        project_id: optional explicit uuid4; defaults to a fresh uuid4.

    Validates that `project_path` exists as a directory (feat-012 step 3).
    Records added_at and last_accessed_at as the current UTC timestamp.
    """
    if not isinstance(project_path, str) or not project_path:
        raise ProjectsError(
            f"project_path must be a non-empty string; got {project_path!r}"
        )
    abs_path = os.path.abspath(project_path)
    if not os.path.isdir(abs_path):
        raise ProjectsError(
            f"project_path {abs_path!r} does not exist or is not a directory"
        )

    p = _resolve(path)
    projects = load_projects(p) if p.exists() else {}

    # Reject duplicate path registrations (a project folder can only
    # appear once in the registry). Compare against the absolute path
    # so `~/foo` and `/home/user/foo` collide as expected.
    for existing in projects.values():
        if os.path.abspath(existing.path) == abs_path:
            raise ProjectsError(
                f"project path {abs_path!r} is already registered "
                f"as {existing.name!r} (id={existing.id})"
            )

    resolved_id = project_id or str(uuid.uuid4())
    # Refuse to overwrite an existing id.
    if resolved_id in projects:
        raise ProjectsError(f"project id {resolved_id!r} already exists in {p}")

    resolved_name = name or os.path.basename(abs_path.rstrip(os.sep)) or abs_path
    now = _now_iso()
    proj = Project(
        id=resolved_id,
        name=resolved_name,
        path=abs_path,
        added_at=now,
        last_accessed_at=now,
    )
    projects[proj.id] = proj
    save_projects(p, projects)
    return proj


def remove_project(
    path: Path | str | None, project_id: str
) -> Project | None:
    """Remove a project entry by id.

    Returns the removed Project (so callers can inspect/clean up side
    effects like log files), or None if the id was not present.

    Note: this module does NOT cascade to log files
    (`~/.heddle/logs/<project_id>.log` and the directory). The cascade
    lives in feat-014 (which also stops any in-flight daemon work for
    the project). This separation keeps projects_io focused on
    registry correctness; the daemon-supervision package owns the
    lifecycle side effects.
    """
    if not isinstance(project_id, str) or not project_id:
        raise ProjectsError(
            f"project_id must be a non-empty string; got {project_id!r}"
        )
    p = _resolve(path)
    projects = load_projects(p) if p.exists() else {}
    removed = projects.pop(project_id, None)
    if removed is None:
        return None
    save_projects(p, projects)
    return removed


def touch_project(path: Path | str | None, project_id: str) -> Project:
    """Update `last_accessed_at` to now for the given project.

    Raises `ProjectsError` if the id is not registered. Used by the
    daemon / UI when a project is opened so the registry reflects
    recency (D-057).
    """
    if not isinstance(project_id, str) or not project_id:
        raise ProjectsError(
            f"project_id must be a non-empty string; got {project_id!r}"
        )
    p = _resolve(path)
    projects = load_projects(p) if p.exists() else {}
    if project_id not in projects:
        raise ProjectsError(f"project id {project_id!r} not found in {p}")
    existing = projects[project_id]
    updated = Project(
        id=existing.id,
        name=existing.name,
        path=existing.path,
        added_at=existing.added_at,
        last_accessed_at=_now_iso(),
    )
    projects[project_id] = updated
    save_projects(p, projects)
    return updated


def list_projects(path: Path | str | None = None) -> list[Project]:
    """Return the registered projects as a list (in registry insertion order).

    Convenience wrapper over `load_projects()` for callers that don't
    need the id-keyed dict.
    """
    return list(load_projects(path).values())
