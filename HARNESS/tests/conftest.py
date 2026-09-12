"""Shared pytest fixtures.

Every test gets an isolated ``tmp_path`` (pytest built-in) and the
canonical harness data paths are monkeypatched to point inside that
directory. This guarantees the suite NEVER touches the real
``feature_list.json`` / ``current_progress.txt`` at the project root.

The monkeypatching is per-test (pytest resets between tests), so each
test sees a clean, empty tmp_path and the tools' module-level path
constants are restored to their original values when the test ends.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make sure the ``tools/`` directory is importable as a namespace
# package so tools like ``from tools._feature_io import ...`` resolve.
# We do this here (before any test module is collected) so that
# imports inside fixtures and tests see the right sys.path.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_DIR = _PROJECT_ROOT / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
# Some tools (when run as scripts via `python HARNESS/tools/foo.py`) splice
# their own ``HARNESS/tools/`` directory onto sys.path so that ``from _x
# import y`` (the non-package form) works. To let tests import those
# modules too, also make the project root importable.
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture
def feature_list_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the canonical ``feature_list.json`` path inside
    ``tmp_path``.

    Patches every module-level binding that resolves to the canonical
    feature_list.json path, in three layers (each `from ... import X`
    copies the reference, so patching one binding does not propagate
    to others):

      1. ``tools.feature_list.FEATURE_LIST_PATH`` — imported by name
         into the CLI module.
      2. ``tools._feature_io.FEATURE_LIST_PATH`` — re-exported by the
         shim from ``heddle_common.feature_list_io.DEFAULT_PATH``.
      3. ``heddle_common.feature_list_io.DEFAULT_PATH`` — the actual
         source of truth; read by ``load()`` / ``save()`` etc. when
         callers (including the CLI's mutation commands) pass ``path=None``.

    Without (3), mutation commands like ``cmd_add`` fall through
    ``_resolve_path(None) -> DEFAULT_PATH`` and write to the real
    file at the repo root — a silent leak between test runs.
    """
    target = tmp_path / "feature_list.json"
    import feature_list as feature_list_mod
    import tools._feature_io as feature_io_mod
    from heddle_common import feature_list_io as feature_lib_mod
    monkeypatch.setattr(feature_list_mod, "FEATURE_LIST_PATH", target,
                        raising=True)
    monkeypatch.setattr(feature_io_mod, "FEATURE_LIST_PATH", target,
                        raising=True)
    monkeypatch.setattr(feature_lib_mod, "DEFAULT_PATH", target,
                        raising=True)
    return target


@pytest.fixture
def handoff_progress_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``tools/handoff_check.PROGRESS_PATH`` to ``tmp_path``."""
    target = tmp_path / "current_progress.txt"
    import handoff_check as handoff_check_mod
    monkeypatch.setattr(handoff_check_mod, "PROGRESS_PATH", target,
                        raising=True)
    monkeypatch.setattr(handoff_check_mod, "FEATURE_LIST_PATH",
                        tmp_path / "feature_list.json", raising=True)
    monkeypatch.setattr(handoff_check_mod, "CODE_STYLE_PATH",
                        tmp_path / "CODE_STYLE.md", raising=True)
    return target


@pytest.fixture
def session_end_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Return dict of tmp_path-based paths for session_end tests."""
    return {
        "FEATURE_LIST_PATH": tmp_path / "feature_list.json",
        "PROGRESS_PATH": tmp_path / "current_progress.txt",
    }


@pytest.fixture
def progress_file_path(tmp_path: Path) -> Path:
    """Return a tmp_path-based progress file path for progress_rotate."""
    return tmp_path / "current_progress.txt"
