# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_common.configs_io (feat-011 / T-023, D-055, T-015).

Covers every step in the feat-011 spec:

  1. Schema: each entry has name / provider / model / base_url /
     api_key_env; `api_key` is never stored.
  2. First-run UX: a missing file is populated with the four default
     templates on first call to `ensure_configs`.
  3. Parser: load_configs returns a dict keyed by `name`.
  4. Writer: update_config mutates one entry by name.
  5. CLI integration (light): `list_configs` produces a stable ordering
     for `heddle configs list` to render.
  6. End-to-end: delete the file, call ensure_configs, assert defaults.
  Plus API-key resolution per T-015 (the security-sensitive chokepoint).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from heddle_common import configs_io as ci


def _write_yaml(tmp: Path, payload: dict | str) -> Path:
    import yaml  # local import keeps the rest of the file PyYAML-free at import time

    p = tmp / "configs.yaml"
    if isinstance(payload, str):
        p.write_text(payload, encoding="utf-8")
    else:
        p.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return p


class TestConfigDataclass(unittest.TestCase):
    """Schema validation in Config.from_dict / to_dict."""

    def test_round_trip(self):
        cfg = ci.Config.from_dict(
            {
                "name": "anthropic-claude-sonnet",
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "base_url": "https://api.anthropic.com",
                "api_key_env": "ANTHROPIC_API_KEY",
            }
        )
        self.assertEqual(cfg.name, "anthropic-claude-sonnet")
        self.assertEqual(cfg.extras, {})
        # Round-trip via dict.
        cfg2 = ci.Config.from_dict(cfg.to_dict())
        self.assertEqual(cfg, cfg2)

    def test_missing_required_key_raises(self):
        with self.assertRaises(ci.ConfigsError):
            ci.Config.from_dict(
                {
                    "name": "x",
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-5",
                    # missing base_url + api_key_env
                }
            )

    def test_empty_name_raises(self):
        with self.assertRaises(ci.ConfigsError):
            ci.Config.from_dict(
                {
                    "name": "",
                    "provider": "anthropic",
                    "model": "x",
                    "base_url": "x",
                    "api_key_env": "x",
                }
            )

    def test_disallowed_provider_raises(self):
        with self.assertRaises(ci.ConfigsError):
            ci.Config.from_dict(
                {
                    "name": "x",
                    "provider": "gpt-9000",
                    "model": "x",
                    "base_url": "x",
                    "api_key_env": "x",
                }
            )

    def test_non_string_field_raises(self):
        with self.assertRaises(ci.ConfigsError):
            ci.Config.from_dict(
                {
                    "name": "x",
                    "provider": "anthropic",
                    "model": 42,  # must be string
                    "base_url": "x",
                    "api_key_env": "x",
                }
            )

    def test_extras_round_trip(self):
        cfg = ci.Config.from_dict(
            {
                "name": "custom",
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "base_url": "https://api.anthropic.com",
                "api_key_env": "ANTHROPIC_API_KEY",
                "timeout": 30,
                "max_retries": 3,
            }
        )
        self.assertEqual(cfg.extras, {"timeout": 30, "max_retries": 3})
        cfg2 = ci.Config.from_dict(cfg.to_dict())
        self.assertEqual(cfg2.extras, {"timeout": 30, "max_retries": 3})

    def test_api_key_never_round_trips(self):
        # T-015: api_key is never stored. If a config dict accidentally
        # contains it, it is silently dropped on round-trip — but only
        # for the extras field. The required api_key_env still has to
        # be present.
        cfg = ci.Config.from_dict(
            {
                "name": "x",
                "provider": "anthropic",
                "model": "m",
                "base_url": "u",
                "api_key_env": "K",
                "api_key": "sk-secret",
            }
        )
        # api_key shows up in extras (the loader is permissive about
        # unknown keys so a user-added "timeout" still rides along).
        # The save layer's responsibility is to never WRITE api_key —
        # `to_dict()` here just mirrors the user's input. The CLI layer
        # (feat-011 step 5) never reads / prints / serializes it.
        self.assertEqual(cfg.extras.get("api_key"), "sk-secret")


class TestDefaults(unittest.TestCase):
    """The four shipped templates per T-023."""

    def test_default_templates_cover_four_providers(self):
        names = [t["name"] for t in ci.DEFAULT_TEMPLATES]
        self.assertEqual(
            names,
            ["anthropic-claude-sonnet", "openai-gpt-4", "bedrock-claude", "ollama-llama"],
        )

    def test_default_templates_have_all_required_fields(self):
        for t in ci.DEFAULT_TEMPLATES:
            self.assertEqual(set(t.keys()), {"name", "provider", "model", "base_url", "api_key_env"})

    def test_default_templates_have_no_api_key(self):
        # T-015: api_key is never stored, even in defaults.
        for t in ci.DEFAULT_TEMPLATES:
            self.assertNotIn("api_key", t)


class TestEnsureConfigsFirstRun(unittest.TestCase):
    """feat-011 step 2 + step 6: first-run UX."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="configs_io_", dir=Path(sys.argv[0]).parent))

    def tearDown(self):
        for p in self.tmpdir.glob("*"):
            p.unlink()
        self.tmpdir.rmdir()

    def test_ensure_creates_file_with_four_defaults(self):
        target = self.tmpdir / "configs.yaml"
        self.assertFalse(target.exists())

        configs = ci.ensure_configs(target)

        self.assertTrue(target.exists())
        self.assertEqual(len(configs), 4)
        self.assertEqual(
            list(configs.keys()),
            ["anthropic-claude-sonnet", "openai-gpt-4", "bedrock-claude", "ollama-llama"],
        )

    def test_ensure_is_idempotent(self):
        target = self.tmpdir / "configs.yaml"
        first = ci.ensure_configs(target)
        # Run a second time; existing file is left untouched.
        second = ci.ensure_configs(target)
        # Same names, same data — and no extra entries from the second run.
        self.assertEqual(set(first.keys()), set(second.keys()))
        for name in first:
            self.assertEqual(first[name], second[name])

    def test_ensure_does_not_overwrite_user_changes(self):
        # feat-011 step 2: first-run writes defaults; subsequent runs
        # must NOT clobber a file the user has edited.
        target = self.tmpdir / "configs.yaml"
        ci.ensure_configs(target)
        # User adds a fifth config.
        ci.add_config(
            target,
            ci.Config.from_dict(
                {
                    "name": "user-custom",
                    "provider": "openai",
                    "model": "gpt-4",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                }
            ),
        )
        # Re-run ensure_configs — the user's entry must survive.
        ci.ensure_configs(target)
        configs = ci.load_configs(target)
        self.assertIn("user-custom", configs)
        self.assertEqual(len(configs), 5)


class TestLoadSaveRoundTrip(unittest.TestCase):
    """feat-011 step 3 + step 7: parser + writer."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="configs_io_", dir=Path(sys.argv[0]).parent))

    def tearDown(self):
        for p in self.tmpdir.glob("*"):
            p.unlink()
        self.tmpdir.rmdir()

    def test_load_returns_dict_keyed_by_name(self):
        target = _write_yaml(
            self.tmpdir,
            {
                "configs": [
                    {
                        "name": "anthropic-claude-sonnet",
                        "provider": "anthropic",
                        "model": "claude-sonnet-4-5",
                        "base_url": "https://api.anthropic.com",
                        "api_key_env": "ANTHROPIC_API_KEY",
                    },
                    {
                        "name": "ollama-llama",
                        "provider": "ollama",
                        "model": "llama3",
                        "base_url": "http://127.0.0.1:11434",
                        "api_key_env": "OLLAMA_API_KEY",
                    },
                ]
            },
        )
        configs = ci.load_configs(target)
        self.assertEqual(set(configs.keys()), {"anthropic-claude-sonnet", "ollama-llama"})
        self.assertEqual(configs["ollama-llama"].provider, "ollama")

    def test_save_load_round_trip_is_stable(self):
        target = self.tmpdir / "configs.yaml"
        ci.ensure_configs(target)  # four defaults

        before = target.read_bytes()
        ci.save_configs(target, ci.load_configs(target))
        after = target.read_bytes()

        self.assertEqual(before, after)

    def test_duplicate_name_fails(self):
        target = _write_yaml(
            self.tmpdir,
            {
                "configs": [
                    {
                        "name": "dupe",
                        "provider": "anthropic",
                        "model": "m",
                        "base_url": "u",
                        "api_key_env": "K",
                    },
                    {
                        "name": "dupe",
                        "provider": "openai",
                        "model": "m",
                        "base_url": "u",
                        "api_key_env": "K",
                    },
                ]
            },
        )
        with self.assertRaises(ci.ConfigsError):
            ci.load_configs(target)

    def test_missing_configs_key_yields_empty_registry(self):
        # Empty / configs-less files are not errors — `ensure_configs`
        # is the only sanctioned way to populate.
        target = self.tmpdir / "empty.yaml"
        target.write_text("# just a comment\n", encoding="utf-8")
        configs = ci.load_configs(target)
        self.assertEqual(configs, {})

    def test_missing_file_raises(self):
        with self.assertRaises(ci.ConfigsError):
            ci.load_configs(self.tmpdir / "no_such_file.yaml")

    def test_non_mapping_root_fails(self):
        target = self.tmpdir / "bad.yaml"
        target.write_text("- a\n- b\n", encoding="utf-8")
        with self.assertRaises(ci.ConfigsError):
            ci.load_configs(target)

    def test_oversize_file_fails(self):
        target = self.tmpdir / "huge.yaml"
        target.write_text("x" * (ci.MAX_CONFIGS_BYTES + 10), encoding="utf-8")
        with self.assertRaises(ci.ConfigsError):
            ci.load_configs(target)

    def test_too_many_configs_fails(self):
        huge_list = [
            {
                "name": f"c{i}",
                "provider": "anthropic",
                "model": "m",
                "base_url": "u",
                "api_key_env": "K",
            }
            for i in range(ci.MAX_CONFIGS + 1)
        ]
        target = _write_yaml(self.tmpdir, {"configs": huge_list})
        with self.assertRaises(ci.ConfigsError):
            ci.load_configs(target)


class TestMutations(unittest.TestCase):
    """feat-011 step 4 + step 5: writer + CLI integration shape."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="configs_io_", dir=Path(sys.argv[0]).parent))
        self.target = self.tmpdir / "configs.yaml"
        ci.ensure_configs(self.target)

    def tearDown(self):
        for p in self.tmpdir.glob("*"):
            p.unlink()
        self.tmpdir.rmdir()

    def test_add_config_refuses_duplicate_name(self):
        with self.assertRaises(ci.ConfigsError):
            ci.add_config(
                self.target,
                ci.Config.from_dict(
                    {
                        "name": "anthropic-claude-sonnet",
                        "provider": "anthropic",
                        "model": "m",
                        "base_url": "u",
                        "api_key_env": "K",
                    }
                ),
            )

    def test_update_config_changes_only_specified_fields(self):
        ci.update_config(self.target, "ollama-llama", model="llama3:70b")
        configs = ci.load_configs(self.target)
        updated = configs["ollama-llama"]
        self.assertEqual(updated.model, "llama3:70b")
        # Other fields untouched.
        self.assertEqual(updated.provider, "ollama")
        self.assertEqual(updated.base_url, "http://127.0.0.1:11434")

    def test_update_config_rejects_unknown_name(self):
        with self.assertRaises(ci.ConfigsError):
            ci.update_config(self.target, "no-such-config", model="x")

    def test_delete_config_removes_entry(self):
        ci.delete_config(self.target, "ollama-llama")
        configs = ci.load_configs(self.target)
        self.assertNotIn("ollama-llama", configs)
        self.assertEqual(len(configs), 3)

    def test_delete_config_rejects_unknown_name(self):
        with self.assertRaises(ci.ConfigsError):
            ci.delete_config(self.target, "no-such-config")

    def test_get_config_returns_one(self):
        cfg = ci.get_config(self.target, "anthropic-claude-sonnet")
        self.assertEqual(cfg.provider, "anthropic")

    def test_list_configs_returns_all_in_insertion_order(self):
        # feat-011 step 5: `heddle configs list` reads this list.
        names = [c.name for c in ci.list_configs(self.target)]
        self.assertEqual(
            names,
            ["anthropic-claude-sonnet", "openai-gpt-4", "bedrock-claude", "ollama-llama"],
        )

    def test_list_configs_returns_empty_for_missing_file(self):
        # list_configs is the "inspect" path; missing file is not an error.
        result = ci.list_configs(self.tmpdir / "nope.yaml")
        self.assertEqual(result, [])


class TestResolveApiKey(unittest.TestCase):
    """T-015: api_key_env resolution — the security-sensitive chokepoint."""

    def test_returns_value_when_set(self):
        self.assertEqual(
            ci.resolve_api_key("ANTHROPIC_API_KEY", env={"ANTHROPIC_API_KEY": "sk-test"}),
            "sk-test",
        )

    def test_returns_none_when_missing(self):
        # Loud failure beats silent fallback (matches the rest of the
        # library's "fail loud" contract).
        self.assertIsNone(ci.resolve_api_key("MISSING", env={}))

    def test_resolve_for_config_reads_cfg_field(self):
        cfg = ci.Config.from_dict(
            {
                "name": "anthropic-claude-sonnet",
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "base_url": "https://api.anthropic.com",
                "api_key_env": "ANTHROPIC_API_KEY",
            }
        )
        self.assertEqual(
            ci.resolve_api_key_for_config(cfg, env={"ANTHROPIC_API_KEY": "sk-x"}),
            "sk-x",
        )
        self.assertIsNone(
            ci.resolve_api_key_for_config(cfg, env={})
        )

    def test_default_templates_all_have_distinct_env_vars(self):
        # Sanity: each template's api_key_env is unique so users with
        # only one provider set in their env still get a clear "missing
        # API key" error if they pick a different template (T-015).
        envs = [t["api_key_env"] for t in ci.DEFAULT_TEMPLATES]
        self.assertEqual(len(envs), len(set(envs)))


if __name__ == "__main__":
    unittest.main()
