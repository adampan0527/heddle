# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sandbox_io — feat-055 / D-053.

Tests cover the round-trip behaviour: read a config that does not
exist (returns defaults), read an existing config, write a new level
without disturbing other top-level keys, and refuse invalid levels.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from heddle_daemon.sandbox import (
    DEFAULT_SANDBOX_LEVEL,
    SandboxConfigError,
    SandboxLevel,
)
from heddle_daemon.sandbox_io import (
    config_path_for,
    read_sandbox_config,
    write_sandbox_level,
)


class TestSandboxIo(unittest.TestCase):
    def test_config_path_for(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            path = config_path_for(root)
            self.assertEqual(path, root / ".heddle" / "config.yaml")

    def test_read_missing_returns_default(self) -> None:
        with TemporaryDirectory() as td:
            cfg = read_sandbox_config(td)
            self.assertEqual(cfg.level, DEFAULT_SANDBOX_LEVEL)
            self.assertEqual(cfg.level, SandboxLevel.FULL)

    def test_read_existing_level(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            config_dir = root / ".heddle"
            config_dir.mkdir(parents=True)
            (config_dir / "config.yaml").write_text(
                "sandbox_level: edit-with-confirm\n",
                encoding="utf-8",
            )
            cfg = read_sandbox_config(root)
            self.assertEqual(cfg.level, SandboxLevel.EDIT_WITH_CONFIRM)

    def test_write_creates_directory_and_file(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            cfg = write_sandbox_level(root, "read-only")
            self.assertEqual(cfg.level, SandboxLevel.READ_ONLY)
            self.assertTrue((root / ".heddle" / "config.yaml").exists())
            data = yaml.safe_load((root / ".heddle" / "config.yaml").read_text())
            self.assertEqual(data["sandbox_level"], "read-only")

    def test_write_preserves_other_keys(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            config_dir = root / ".heddle"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.yaml"
            config_path.write_text(
                "future_flag: hello\nsandbox_level: full\n",
                encoding="utf-8",
            )
            write_sandbox_level(root, "edit-with-confirm")
            data = yaml.safe_load(config_path.read_text())
            self.assertEqual(data["future_flag"], "hello")
            self.assertEqual(data["sandbox_level"], "edit-with-confirm")

    def test_write_rejects_invalid_level(self) -> None:
        with TemporaryDirectory() as td:
            with self.assertRaises(SandboxConfigError):
                write_sandbox_level(td, "open-season")

    def test_write_rejects_non_string(self) -> None:
        with TemporaryDirectory() as td:
            with self.assertRaises(SandboxConfigError):
                write_sandbox_level(td, 42)  # type: ignore[arg-type]

    def test_write_round_trip_via_read(self) -> None:
        with TemporaryDirectory() as td:
            for level in SandboxLevel:
                write_sandbox_level(td, level.value)
                cfg = read_sandbox_config(td)
                self.assertEqual(cfg.level, level)


if __name__ == "__main__":
    unittest.main()
