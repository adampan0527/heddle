# SPDX-License-Identifier: Apache-2.0
"""Tests for ``heddle_daemon.llm_config`` — feat-031.

The resolver is the single chokepoint that turns a feature's
``implementation_model`` string (or None) into a registered
``Config``. These tests cover the three paths:

  * explicit name hit  → returns (Config, "explicit")
  * None → first entry → returns (Config, "default")
  * unknown name       → raises ``LLMConfigError``

plus three error paths:

  * registry file missing and ``ensure_configs`` succeeds but the
    resulting registry is empty (defensive — no templates seeded)
  * implementation_model is the empty string (normalized to None)
  * implementation_model is whitespace-only (normalized to None)

Each test drops a fresh tmp configs.yaml so the suite is hermetic.
The daemon-side test set (``test_commands_protocol.py``) covers the
WS round-trip; this file covers the pure-Python resolver.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from heddle_common.configs_io import DEFAULT_TEMPLATES, Config
from heddle_daemon.llm import LLMConfigError
from heddle_daemon.llm_config import (
    DEFAULT_SOURCE,
    EXPLICIT_SOURCE,
    resolve_feature_llm_config,
)

_ONE_ENTRY_YAML = (
    "configs:\n"
    "  - name: alpha\n"
    "    provider: anthropic\n"
    "    model: claude-sonnet-4-5\n"
    "    base_url: https://api.anthropic.com\n"
    "    api_key_env: ANTHROPIC_API_KEY\n"
)

_TWO_ENTRY_YAML = (
    "configs:\n"
    "  - name: alpha\n"
    "    provider: anthropic\n"
    "    model: claude-sonnet-4-5\n"
    "    base_url: https://api.anthropic.com\n"
    "    api_key_env: ANTHROPIC_API_KEY\n"
    "  - name: bravo\n"
    "    provider: openai\n"
    "    model: gpt-4\n"
    "    base_url: https://api.openai.com/v1\n"
    "    api_key_env: OPENAI_API_KEY\n"
)


class TestResolveFeatureLlmConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg_path = Path(self._tmp.name) / "configs.yaml"

    def _write(self, yaml_text: str) -> None:
        self.cfg_path.write_text(yaml_text, encoding="utf-8")

    # ---- happy paths ----

    def test_explicit_name_returns_config_and_explicit_source(self) -> None:
        self._write(_ONE_ENTRY_YAML)
        config, source = resolve_feature_llm_config(
            "alpha", configs_path=self.cfg_path
        )
        self.assertIsInstance(config, Config)
        self.assertEqual(config.name, "alpha")
        self.assertEqual(source, EXPLICIT_SOURCE)

    def test_none_returns_first_entry_with_default_source(self) -> None:
        self._write(_TWO_ENTRY_YAML)
        config, source = resolve_feature_llm_config(
            None, configs_path=self.cfg_path
        )
        # Insertion order: alpha is first.
        self.assertEqual(config.name, "alpha")
        self.assertEqual(source, DEFAULT_SOURCE)

    def test_empty_string_normalized_to_none(self) -> None:
        self._write(_ONE_ENTRY_YAML)
        config, source = resolve_feature_llm_config(
            "", configs_path=self.cfg_path
        )
        self.assertEqual(config.name, "alpha")
        self.assertEqual(source, DEFAULT_SOURCE)

    def test_whitespace_only_normalized_to_none(self) -> None:
        self._write(_ONE_ENTRY_YAML)
        config, source = resolve_feature_llm_config(
            "   ", configs_path=self.cfg_path
        )
        self.assertEqual(config.name, "alpha")
        self.assertEqual(source, DEFAULT_SOURCE)

    def test_second_entry_picked_when_named_explicitly(self) -> None:
        self._write(_TWO_ENTRY_YAML)
        config, source = resolve_feature_llm_config(
            "bravo", configs_path=self.cfg_path
        )
        self.assertEqual(config.name, "bravo")
        self.assertEqual(source, EXPLICIT_SOURCE)

    # ---- error paths ----

    def test_unknown_name_raises_llm_config_error(self) -> None:
        self._write(_ONE_ENTRY_YAML)
        with self.assertRaises(LLMConfigError) as ctx:
            resolve_feature_llm_config(
                "does-not-exist", configs_path=self.cfg_path
            )
        self.assertEqual(ctx.exception.cause, "llm_config_error")

    def test_missing_file_falls_through_ensure_configs(self) -> None:
        """No file → ``ensure_configs`` populates defaults → the
        resolver returns the first default (anthropic-claude-sonnet).

        feat-011 first-run UX: a user with a fresh ``~/.heddle`` does
        not need to populate ``configs.yaml`` before running their
        first feature.
        """
        if self.cfg_path.exists():
            self.cfg_path.unlink()
        config, source = resolve_feature_llm_config(
            None, configs_path=self.cfg_path
        )
        self.assertEqual(config.name, DEFAULT_TEMPLATES[0]["name"])
        self.assertEqual(source, DEFAULT_SOURCE)
        self.assertTrue(self.cfg_path.exists())
        if self.cfg_path.exists():
            self.cfg_path.unlink()

    def test_empty_registry_file_raises_llm_config_error(self) -> None:
        """An existing-but-empty configs.yaml cannot serve a
        default-fallback request — the user must add at least one
        named config. ``ensure_configs`` is a no-op when the file
        already exists (idempotent first-run UX), so a hand-emptied
        registry stays empty and the resolver raises."""
        self._write("configs: []\n")
        with self.assertRaises(LLMConfigError) as ctx:
            resolve_feature_llm_config(
                None, configs_path=self.cfg_path
            )
        self.assertEqual(ctx.exception.cause, "llm_config_error")

    def test_default_configs_path_used_when_none(self) -> None:
        """When ``configs_path=None`` the resolver falls back to the
        library's default path (``~/.heddle/configs.yaml``). The
        daemon's ``config_from_env`` is the layer that reads
        ``HEDDLE_CONFIGS_PATH``; ``llm_config.resolve_feature_llm_config``
        only respects the env var indirectly through that wiring.

        We exercise the parameter-less path by writing a configs.yaml
        at the resolved default path inside a hermetic HOME, then
        asserting the resolver picks it up. ``HEDDLE_CONFIGS_PATH`` is
        deliberately NOT tested here — it is a daemon-startup knob,
        not a per-call resolver knob (see
        ``heddle_daemon.server.config_from_env``)."""
        home = Path(self._tmp.name) / "home"
        home.mkdir()
        default_dir = home / ".heddle"
        default_dir.mkdir()
        default_cfg = default_dir / "configs.yaml"
        default_cfg.write_text(_ONE_ENTRY_YAML, encoding="utf-8")

        old_home = os.environ.get("HOME")
        old_userprofile = os.environ.get("USERPROFILE")
        # Windows uses ``USERPROFILE``, POSIX uses ``HOME``. Set both
        # so the test runs on either platform.
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        try:
            config, source = resolve_feature_llm_config("alpha")
            self.assertEqual(config.name, "alpha")
            self.assertEqual(source, EXPLICIT_SOURCE)
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            if old_userprofile is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = old_userprofile

    # ---- structured-log smoke ----

    def test_structured_log_emitted_on_each_path(self) -> None:
        """Sanity: the resolver emits one of the documented log events
        on every path. We don't assert on log content here; we only
        assert the call does not raise so a future logger swap
        doesn't break this module."""
        self._write(_ONE_ENTRY_YAML)
        resolve_feature_llm_config("alpha", configs_path=self.cfg_path)
        resolve_feature_llm_config(None, configs_path=self.cfg_path)
        with self.assertRaises(LLMConfigError):
            resolve_feature_llm_config(
                "missing", configs_path=self.cfg_path
            )


if __name__ == "__main__":
    unittest.main()
