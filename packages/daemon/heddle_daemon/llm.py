# SPDX-License-Identifier: Apache-2.0
"""LLM provider factory — feat-023.

The single chokepoint for turning a ``heddle_common.configs_io.Config``
into a LangChain ``BaseChatModel``. All four v0.1 providers are
supported:

  * anthropic  -> ``langchain_anthropic.ChatAnthropic``
  * openai     -> ``langchain_openai.ChatOpenAI``
  * bedrock    -> ``langchain_aws.ChatBedrock``
  * ollama     -> ``langchain_ollama.ChatOllama`` (local)

Per T-015 / T-030 / D-053, API keys are resolved through
``heddle_common.configs_io.resolve_api_key`` (the single chokepoint
for "where does the secret come from?"). The resolved value is
NEVER logged by this module \u2014 the structured-logging layer in
``heddle_common.logging`` has a separate redaction pass for fields
named ``api_key`` / ``secret`` / ``token`` (case-insensitive), but
keeping secrets out of values is the first line of defense.

Per D-041, transient LLM failures are retried by the agent runtime
(``agent_runtime._call_llm``); this factory itself does NOT retry.
A missing API key is a config error, not a transient failure.

Design rule: this module knows about LangChain chat-model classes.
``agent_runtime.py`` does NOT \u2014 it consumes whatever
``BaseChatModel`` subclass this factory returns, plus the test-only
``FakeLLM`` shim from ``heddle_common.fake_llm``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from heddle_common import logging as _logging
from heddle_common.configs_io import (
    ALLOWED_PROVIDERS,
    Config,
    resolve_api_key,
)


__all__ = [
    "LLMConfigError",
    "build_chat_model",
    "chat_model_class_for_provider",
]


class LLMConfigError(ValueError):
    """Raised when a ``Config`` cannot be turned into a chat model.

    Surfaced by the daemon's WS layer as a structured failure with
    ``cause="llm_config_error"`` so the UI can render \"missing
    API key\" / \"unknown provider\" without parsing prose.

    ``.cause`` mirrors the v0.1 error-class taxonomy used elsewhere
    in the runtime (``recursion_limit``, ``llm_retry_exhausted``,
    ``llm_config_error`` here).
    """

    def __init__(self, message: str, *, cause: str = "llm_config_error") -> None:
        super().__init__(message)
        self.cause = cause


def chat_model_class_for_provider(provider: str) -> type:
    """Return the LangChain chat-model class for a provider name.

    Lazily imports the SDK so a deployment that only uses
    ``openai`` doesn't have to install ``anthropic`` / ``aws``
    / ``ollama``. Missing SDK → :class:`LLMConfigError` (the daemon
    surfaces \"provider SDK not installed\" as a structured
    failure).
    """
    if provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise LLMConfigError(
                "anthropic provider requested but langchain-anthropic is not installed; "
                "run `pip install langchain-anthropic`",
            ) from exc
        return ChatAnthropic
    if provider == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise LLMConfigError(
                "openai provider requested but langchain-openai is not installed; "
                "run `pip install langchain-openai`",
            ) from exc
        return ChatOpenAI
    if provider == "bedrock":
        try:
            from langchain_aws import ChatBedrock
        except ImportError as exc:
            raise LLMConfigError(
                "bedrock provider requested but langchain-aws is not installed; "
                "run `pip install langchain-aws`",
            ) from exc
        return ChatBedrock
    if provider == "ollama":
        try:
            from langchain_ollama import ChatOllama
        except ImportError as exc:
            raise LLMConfigError(
                "ollama provider requested but langchain-ollama is not installed; "
                "run `pip install langchain-ollama`",
            ) from exc
        return ChatOllama
    raise LLMConfigError(
        f"unknown provider {provider!r}; "
        f"expected one of {sorted(ALLOWED_PROVIDERS)}"
    )


def build_chat_model(
    config: Config,
    *,
    env: Optional[Mapping[str, str]] = None,
    **overrides: Any,
) -> Any:
    """Instantiate a LangChain chat model for ``config``.

    Args:
        config: A ``configs_io.Config`` (typically loaded from
            ``~/.heddle/configs.yaml``).
        env: Mapping to use as the process environment. Defaults to
            ``os.environ``; tests pass an explicit dict. The env is
            passed straight through to ``resolve_api_key`` (T-015
            chokepoint) without ever being merged with the real
            process environment \u2014 this is what makes
            ``HEDDLE_FAKE_LLM=1`` work without leaking real keys.
        **overrides: Extra kwargs forwarded to the LangChain chat
            constructor (e.g. ``temperature=0.0``). Used by future
            feat-052 (`heddle test`) and similar override sites.

    Returns:
        A ``BaseChatModel`` subclass instance ready to be passed to
        ``AgentRuntime.run_agent_step``.

    Raises:
        LLMConfigError: provider unknown, SDK missing, or the
            API key env var is unset. The exception's ``.cause``
            is always ``"llm_config_error"`` so the daemon can
            surface the failure with a stable classification.
    """
    provider = config.provider
    cls = chat_model_class_for_provider(provider)

    # Resolve the API key. Per T-015, missing keys are NOT errors
    # at the resolve step \u2014 they're errors at the build step. So
    # we resolve to None here and raise a structured error below
    # with the right context. We never echo the resolved value.
    api_key = resolve_api_key(config.api_key_env, env=env)

    # Bedrock uses AWS-native auth (boto3 credential chain), not a
    # single API key. The ``api_key_env`` field on Bedrock configs
    # is reserved for future use (e.g. STS session tokens); for
    # v0.1 we let boto3 / langchain-aws pick up credentials from
    # the standard AWS_* env vars / IMDS / ~/.aws/credentials.
    # ``region_name`` is the other AWS-native requirement; we read
    # it from AWS_REGION / AWS_DEFAULT_REGION (the standard names)
    # so callers don't have to edit the YAML.
    if provider == "bedrock":
        region_name = (env or {}).get("AWS_REGION") or (env or {}).get(
            "AWS_DEFAULT_REGION"
        )
        if not region_name:
            raise LLMConfigError(
                f"bedrock provider requires AWS_REGION or AWS_DEFAULT_REGION "
                f"in the environment (model={config.model!r})"
            )
        kwargs: dict[str, Any] = {
            "model_id": config.model,
            "endpoint_url": config.base_url,
            "region_name": region_name,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # Ollama runs locally and accepts no auth by default. The
    # ``api_key_env`` field is reserved for future per-deployment
    # auth (e.g. a remote ollama behind a reverse proxy); for v0.1
    # we ignore it and only fail loudly if a future ollama build
    # actually requires one.
    if provider == "ollama":
        kwargs = {
            "model": config.model,
            "base_url": config.base_url,
        }
        kwargs.update(overrides)
        # ``ChatOllama`` doesn't take an api_key kwarg; passing one
        # via overrides would raise TypeError. Defensive check:
        if "api_key" in kwargs:
            kwargs.pop("api_key")
        return cls(**kwargs)

    # anthropic + openai require an API key.
    if not api_key:
        _logging.warn(
            component="llm_factory",
            event="missing_api_key",
            msg=(
                f"provider={provider} requires {config.api_key_env} in the "
                f"process env; refusing to build chat model"
            ),
            provider=provider,
            config_name=config.name,
            env_var=config.api_key_env,
        )
        raise LLMConfigError(
            f"missing API key for provider {provider!r}: "
            f"set {config.api_key_env} in the environment "
            f"(see ~/.heddle/.env or your shell profile)"
        )

    kwargs = {
        "model": config.model,
        "api_key": api_key,
    }
    # base_url is optional in the LangChain chat-model constructors
    # \u2014 skip it when it's empty so we don't override a sane default
    # with an empty string (some providers treat that as \"connect
    # to localhost:0\").
    if config.base_url:
        kwargs["base_url"] = config.base_url
    kwargs.update(overrides)
    return cls(**kwargs)
