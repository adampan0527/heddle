# SPDX-License-Identifier: Apache-2.0
"""sandbox_io — read/write the per-project ``.heddle/config.yaml`` (feat-055).

The sandbox level lives in ``<project_root>/.heddle/config.yaml`` per
D-053 / T-022. ``SandboxConfig.from_yaml`` (in ``sandbox.py``) is the
read path used by the agent runtime; this module adds the WRITE path
plus a tiny round-trip helper so the Web UI's PATCH endpoint can
update the level without duplicating the YAML schema.

Schema (matches what ``SandboxConfig.from_yaml`` accepts):

    sandbox_level: "read-only" | "edit-with-confirm" | "full"

Missing file is fine (default level = ``full``); missing key is fine
(same default). On write we PRESERVE any other keys the file already
holds so this module does not stomp user-added config. A missing
``.heddle/`` directory is created on demand.

Why this lives in the daemon (not ``heddle_common``):
    v0.1 only writes from the daemon's PATCH handler. If a CLI command
    ever needs the same write, this can move to ``heddle_common`` for
    parity with ``configs_io``; today the single writer keeps the
    surface tight.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from .sandbox import (
    CONFIG_KEY,
    CONFIG_REL_PATH,
    DEFAULT_SANDBOX_LEVEL,
    SandboxConfig,
    SandboxConfigError,
    SandboxLevel,
)

__all__ = [
    "config_path_for",
    "read_sandbox_config",
    "write_sandbox_level",
]


def config_path_for(project_root: Path | str) -> Path:
    """Return the canonical ``<root>/.heddle/config.yaml`` path."""
    root = Path(project_root)
    return root / CONFIG_REL_PATH


def read_sandbox_config(project_root: Path | str) -> SandboxConfig:
    """Read the project's sandbox config (defaults if absent)."""
    return SandboxConfig.from_yaml(project_root)


def write_sandbox_level(
    project_root: Path | str,
    level: str,
    *,
    config_path: Optional[Path] = None,
) -> SandboxConfig:
    """Write ``level`` to the project's config.yaml. Returns the parsed config.

    Preserves every other top-level key already in the file so this
    function is safe to call alongside unrelated config knobs the user
    may add later. Creates ``.heddle/`` if it does not exist.

    Validates ``level`` against the SandboxLevel enum; an invalid
    value raises ``SandboxConfigError`` (matches the read path's
    behavior — silent fallback would mask a typo).
    """
    if not isinstance(level, str):
        raise SandboxConfigError(
            f"sandbox_level must be a string; got {type(level).__name__}"
        )
    try:
        normalized = SandboxLevel(level)
    except ValueError as exc:
        valid = ", ".join(repr(s.value) for s in SandboxLevel)
        raise SandboxConfigError(
            f"sandbox_level={level!r} is not a valid SandboxLevel; "
            f"expected one of: {valid}"
        ) from exc

    path = config_path if config_path is not None else config_path_for(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SandboxConfigError(f"cannot read {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise SandboxConfigError(f"invalid YAML in {path}: {exc}") from exc
        if loaded is not None:
            if not isinstance(loaded, Mapping):
                raise SandboxConfigError(
                    f"{path} must be a YAML mapping at the top level; "
                    f"got {type(loaded).__name__}"
                )
            # Defensive copy — we do not mutate the caller's mapping.
            existing = dict(loaded)

    existing[CONFIG_KEY] = normalized.value
    text = yaml.safe_dump(
        existing,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    path.write_text(text, encoding="utf-8")
    return SandboxConfig(level=normalized)


def normalize_level(level: Optional[str]) -> SandboxLevel:
    """Return the SandboxLevel for ``level`` (or DEFAULT_SANDBOX_LEVEL when None)."""
    if level is None:
        return DEFAULT_SANDBOX_LEVEL
    return SandboxLevel(level)
