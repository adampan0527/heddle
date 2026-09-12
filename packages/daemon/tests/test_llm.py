# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_daemon.llm — feat-023 (LLM provider factory)."""

from __future__ import annotations

import logging
import unittest
from typing import Any

from heddle_common.configs_io import Config
from heddle_daemon.llm import (
    LLMConfigError,
    build_chat_model,
    chat_model_class_for_provider,
)


# ---------- helpers ----------


def _make_config(
    name: str = "test-config",
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-5",
    base_url: str = "",
    api_key_env: str = "TEST_API_KEY",
) -> Config:
    return Config(
        name=name,
        provider=provider,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
    )


# ---------- construction (chat_model_class_for_provider) ----------


class TestProviderClassLookup(unittest.TestCase):
    """Each provider name resolves to a specific LangChain chat-model class."""

    def test_anthropic_resolves(self):
        cls = chat_model_class_for_provider("anthropic")
        self.assertEqual(cls.__name__, "ChatAnthropic")

    def test_openai_resolves(self):
        cls = chat_model_class_for_provider("openai")
        self.assertEqual(cls.__name__, "ChatOpenAI")

    def test_bedrock_resolves(self):
        cls = chat_model_class_for_provider("bedrock")
        self.assertEqual(cls.__name__, "ChatBedrock")

    def test_ollama_resolves(self):
        cls = chat_model_class_for_provider("ollama")
        self.assertEqual(cls.__name__, "ChatOllama")

    def test_unknown_provider_raises(self):
        with self.assertRaises(LLMConfigError) as ctx:
            chat_model_class_for_provider("bogus")
        self.assertIn("unknown provider", str(ctx.exception))
        self.assertIn("bogus", str(ctx.exception))
        self.assertEqual(ctx.exception.cause, "llm_config_error")


# ---------- build_chat_model: per-provider instantiation ----------


class TestBuildChatModel(unittest.TestCase):
    """feat-023 step 5: instantiate each provider with a mocked env
    var; assert the right ChatModel subclass is returned."""

    def test_anthropic(self):
        cfg = _make_config(provider="anthropic", api_key_env="ANTHROPIC_API_KEY")
        model = build_chat_model(cfg, env={"ANTHROPIC_API_KEY": "fake-key"})
        self.assertEqual(type(model).__name__, "ChatAnthropic")

    def test_openai(self):
        cfg = _make_config(provider="openai", api_key_env="OPENAI_API_KEY")
        model = build_chat_model(cfg, env={"OPENAI_API_KEY": "fake-key"})
        self.assertEqual(type(model).__name__, "ChatOpenAI")

    def test_bedrock(self):
        cfg = _make_config(
            provider="bedrock",
            model="anthropic.claude-sonnet-4-5",
            api_key_env="AWS_BEDROCK_API_KEY",
        )
        env = {
            "AWS_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
        }
        model = build_chat_model(cfg, env=env)
        self.assertEqual(type(model).__name__, "ChatBedrock")

    def test_ollama(self):
        cfg = _make_config(
            provider="ollama",
            model="llama3",
            base_url="http://127.0.0.1:11434",
            api_key_env="OLLAMA_API_KEY",
        )
        model = build_chat_model(cfg, env={"OLLAMA_API_KEY": "ignored"})
        self.assertEqual(type(model).__name__, "ChatOllama")

    def test_default_templates_all_instantiate(self):
        """End-to-end: every default template from configs_io produces
        a chat model when the env is filled in."""
        from heddle_common.configs_io import DEFAULT_TEMPLATES, Config

        env: dict[str, str] = {
            "ANTHROPIC_API_KEY": "k",
            "OPENAI_API_KEY": "k",
            "AWS_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "k",
            "AWS_SECRET_ACCESS_KEY": "k",
            "OLLAMA_API_KEY": "ignored",
        }
        for tmpl in DEFAULT_TEMPLATES:
            cfg = Config.from_dict(tmpl)
            model = build_chat_model(cfg, env=env)
            self.assertTrue(
                type(model).__name__.startswith("Chat"),
                f"{cfg.provider} returned {type(model).__name__}",
            )


# ---------- error paths ----------


class TestMissingAPIKey(unittest.TestCase):
    """feat-023 step 4: missing key → structured LLMConfigError."""

    def test_anthropic_missing_key_raises_with_cause(self):
        cfg = _make_config(provider="anthropic", api_key_env="ANTHROPIC_API_KEY")
        with self.assertRaises(LLMConfigError) as ctx:
            build_chat_model(cfg, env={})
        err = ctx.exception
        self.assertEqual(err.cause, "llm_config_error")
        self.assertIn("ANTHROPIC_API_KEY", str(err))
        self.assertIn("anthropic", str(err))

    def test_openai_missing_key_raises_with_cause(self):
        cfg = _make_config(provider="openai", api_key_env="OPENAI_API_KEY")
        with self.assertRaises(LLMConfigError) as ctx:
            build_chat_model(cfg, env={})
        self.assertEqual(ctx.exception.cause, "llm_config_error")

    def test_bedrock_missing_region_raises_with_cause(self):
        """Bedrock uses AWS-native auth; missing region is a config error."""
        cfg = _make_config(provider="bedrock", api_key_env="AWS_BEDROCK_API_KEY")
        with self.assertRaises(LLMConfigError) as ctx:
            build_chat_model(cfg, env={"AWS_ACCESS_KEY_ID": "t"})
        self.assertEqual(ctx.exception.cause, "llm_config_error")
        self.assertIn("region", str(ctx.exception).lower())

    def test_bedrock_uses_aws_default_region_alias(self):
        cfg = _make_config(provider="bedrock", api_key_env="AWS_BEDROCK_API_KEY")
        env = {
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_ACCESS_KEY_ID": "t",
            "AWS_SECRET_ACCESS_KEY": "t",
        }
        # Should NOT raise.
        model = build_chat_model(cfg, env=env)
        self.assertEqual(type(model).__name__, "ChatBedrock")


# ---------- override kwargs ----------


class TestOverrides(unittest.TestCase):
    def test_extra_kwargs_forwarded_to_chat_model(self):
        cfg = _make_config(provider="anthropic", api_key_env="ANTHROPIC_API_KEY")
        model = build_chat_model(
            cfg, env={"ANTHROPIC_API_KEY": "k"}, temperature=0.0, max_tokens=1024
        )
        # LangChain's ChatAnthropic stores these on the instance.
        self.assertEqual(model.temperature, 0.0)
        self.assertEqual(model.max_tokens, 1024)

    def test_ollama_rejects_api_key_override(self):
        """Ollama doesn't accept an api_key kwarg; the factory must
        strip it defensively rather than letting TypeError leak."""
        cfg = _make_config(
            provider="ollama",
            base_url="http://127.0.0.1:11434",
            api_key_env="OLLAMA_API_KEY",
        )
        # Should NOT raise even though we pass api_key via overrides.
        model = build_chat_model(cfg, env={"OLLAMA_API_KEY": "ignored"}, api_key="ignored")
        self.assertEqual(type(model).__name__, "ChatOllama")


# ---------- security: secrets never logged ----------


class TestSecretNotLogged(unittest.TestCase):
    """The factory MUST NOT log the resolved API key value.

    Defensive in-depth: even though ``heddle_common.logging`` has
    redaction for ``api_key`` / ``secret`` / ``token`` fields, we
    should never hand those values to the logger in the first place.
    """

    def test_missing_key_log_message_does_not_contain_secret(self):
        """The "missing API key" log line must name the env var but
        never include its resolved value."""
        import io
        import contextlib

        cfg = _make_config(provider="anthropic", api_key_env="ANTHROPIC_API_KEY")
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            try:
                build_chat_model(cfg, env={})
            except LLMConfigError:
                pass
        output = captured.getvalue()
        self.assertNotIn("ANTHROPIC_API_KEY=", output)
        # The env var NAME is fine to mention (it's not a secret);
        # only the VALUE is sensitive.
        self.assertIn("ANTHROPIC_API_KEY", output)

    def test_resolved_key_never_in_any_log(self):
        """Smoke: even with a key present, the success path must not
        log it. We can't easily check the success-path log without
        a real LangChain chat instance, but we can at least verify
        ``resolve_api_key`` gives us a string and assert the
        factory never passes that string to ``_logging.*``."""
        # This is a structural test: by reading the source, the
        # factory never calls ``_logging.*`` with the resolved key.
        # We assert that here by reading the module's source and
        # checking for the forbidden pattern.
        import inspect

        from heddle_daemon import llm

        src = inspect.getsource(llm)
        # The factory must not call ``_logging.<level>(... api_key ...``
        # anywhere. Other log calls are fine.
        for forbidden in ("api_key=", "api_key=%s", "api_key=%r"):
            self.assertNotIn(forbidden, src, f"factory logs {forbidden!r}")


if __name__ == "__main__":
    unittest.main()
