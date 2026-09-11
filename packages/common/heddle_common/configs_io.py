# SPDX-License-Identifier: Apache-2.0
"""configs_io — read/write/manage `~/.heddle/configs.yaml`.

Per feat-011 / TECH.md T-023 / DESIGN.md D-055. The configs file holds
named LLM provider templates — one per (provider, model) combination the
user wants to use. The v0.1 file ships pre-populated with four templates:

    anthropic-claude-sonnet, openai-gpt-4, bedrock-claude, ollama-llama

Users may add / clone / edit / delete entries (the CLI exposes
`heddle configs list | add | edit | delete` — feat-011 covers `list`).

Security rules (T-015):
  - `api_key` is never written by heddle. The configs file only stores
    `api_key_env`, the name of the env var the daemon should read at
    LLM-call time. `resolve_api_key()` (this module) is the single
    chokepoint that reads the env var; it returns `None` on missing
    instead of raising, so the daemon can surface a clear error.
  - The resolved key value is never logged or returned to callers in
    a place that could be serialized into an error message. Tests
    pin this contract (see test_configs_io).

This module lives in `heddle_common/` (not the daemon package) so both
the CLI (feat-050) and the daemon (feat-023 / feat-031) share one
implementation of the on-disk format. The CLI / daemon pass an explicit
`path`; the default is `~/.heddle/configs.yaml` per T-023.

Fixture schema (T-023 + D-053 + D-055):

    configs:
      - name: anthropic-claude-sonnet
        provider: anthropic
        model: claude-sonnet-4-5
        base_url: https://api.anthropic.com
        api_key_env: ANTHROPIC_API_KEY
      - name: ollama-llama
        provider: ollama
        model: llama3
        base_url: http://127.0.0.1:11434
        api_key_env: OLLAMA_API_KEY  # ollama ignores the value but
                                      # the field is required for
                                      # uniform schema

Note on `api_key_env`: ollama does not actually require an API key, but
T-023's "uniform schema" rule means every entry has the field; the
daemon's `resolve_api_key` returns `None` for unset env vars, which the
ollama provider integration accepts (no-auth local server).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import atomic_io

__all__ = [
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_TEMPLATES",
    "Config",
    "ConfigsError",
    "add_config",
    "default_configs_path",
    "delete_config",
    "ensure_configs",
    "get_config",
    "list_configs",
    "load_configs",
    "resolve_api_key",
    "save_configs",
    "update_config",
]

# ---------- constants ----------

# Default location per T-023. The daemon / CLI always read this file
# unless the user passes --config-path. Tests pass an explicit path.
DEFAULT_CONFIG_DIR: str = "~/.heddle"
DEFAULT_CONFIG_PATH: str = "~/.heddle/configs.yaml"

# v0.1 default templates. Per T-023: each entry has name / provider /
# model / base_url / api_key_env filled in. `api_key` is intentionally
# absent (never stored). The order here defines the default in the UI
# dropdown (D-055: "default to first entry in the registry").
DEFAULT_TEMPLATES: tuple[dict[str, str], ...] = (
    {
        "name": "anthropic-claude-sonnet",
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "base_url": "https://api.anthropic.com",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    {
        "name": "openai-gpt-4",
        "provider": "openai",
        "model": "gpt-4",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
    {
        "name": "bedrock-claude",
        "provider": "bedrock",
        "model": "anthropic.claude-sonnet-4-5",
        "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "api_key_env": "AWS_BEDROCK_API_KEY",
    },
    {
        "name": "ollama-llama",
        "provider": "ollama",
        "model": "llama3",
        "base_url": "http://127.0.0.1:11434",
        "api_key_env": "OLLAMA_API_KEY",
    },
)

# Hard caps, defensive against accidental /tmp mounts or symlink races.
MAX_CONFIGS_BYTES: int = 1_048_576  # 1 MiB
MAX_CONFIGS: int = 100

# Per-entry validation. Keep in sync with the dataclass + dataclass_to_dict.
_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"name", "provider", "model", "base_url", "api_key_env"}
)
# v0.1 providers — feat-023 will reference this whitelist too.
ALLOWED_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "openai", "bedrock", "ollama"}
)


# ---------- error type ----------

class ConfigsError(Exception):
    """Raised for any user-facing configs.yaml problem.

    Distinct from `fail()` (which SystemExits). ConfigsError lets callers
    in the CLI layer catch + render a friendly message instead of the
    raw stderr trace.
    """


# ---------- dataclass ----------

@dataclass(frozen=True)
class Config:
    """One named LLM provider configuration (D-053 / D-055 schema)."""

    name: str
    provider: str
    model: str
    base_url: str
    api_key_env: str
    # Free-form extras for forward compatibility (e.g. user-added
    # timeout, max_retries). The daemon/feat-023 reads from these via
    # dict access; new optional keys do not require a code change.
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
        }
        out.update(self.extras)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        missing = _REQUIRED_KEYS - set(data.keys())
        if missing:
            raise ConfigsError(
                f"config entry missing required keys {sorted(missing)}; "
                f"got keys {sorted(data.keys())}"
            )
        if not isinstance(data["name"], str) or not data["name"]:
            raise ConfigsError(
                f"config entry `name` must be a non-empty string; got {data['name']!r}"
            )
        if data["provider"] not in ALLOWED_PROVIDERS:
            raise ConfigsError(
                f"config entry `provider` must be one of {sorted(ALLOWED_PROVIDERS)}; "
                f"got {data['provider']!r}"
            )
        for key in ("model", "base_url", "api_key_env"):
            if not isinstance(data[key], str) or not data[key]:
                raise ConfigsError(
                    f"config entry `{key}` must be a non-empty string; got {data[key]!r}"
                )
        # Anything outside _REQUIRED_KEYS goes into extras (so a future
        # `timeout: 30` field can ride along without breaking older code).
        extras = {k: v for k, v in data.items() if k not in _REQUIRED_KEYS}
        return cls(
            name=data["name"],
            provider=data["provider"],
            model=data["model"],
            base_url=data["base_url"],
            api_key_env=data["api_key_env"],
            extras=extras,
        )


# ---------- IO ----------

def default_configs_path() -> Path:
    """Return the expanded default `~/.heddle/configs.yaml` path."""
    return Path(DEFAULT_CONFIG_PATH).expanduser()


def _resolve(path: Path | str | None) -> Path:
    if path is None:
        return default_configs_path()
    return path if isinstance(path, Path) else Path(path)


def _fail(msg: str) -> None:
    """Print to stderr and exit (matches feature_list_io / fake_llm style)."""
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def load_configs(path: Path | str | None = None) -> dict[str, Config]:
    """Read and parse configs.yaml into a dict keyed by `name`.

    Raises `ConfigsError` on schema / parse problems. The caller decides
    whether to surface (CLI) or fail-loud (daemon bootstrap).
    """
    p = _resolve(path)
    try:
        size = p.stat().st_size
    except FileNotFoundError:
        raise ConfigsError(f"{p} not found")
    except OSError as exc:
        raise ConfigsError(f"cannot stat {p}: {exc}")
    if size > MAX_CONFIGS_BYTES:
        raise ConfigsError(
            f"{p} is {size} bytes; max is {MAX_CONFIGS_BYTES}. "
            "Check the path — large configs usually mean a wrong file."
        )
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigsError(f"cannot read {p}: {exc}")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigsError(f"{p} is not valid YAML: {exc}")
    if data is None:
        # Empty file — treat as "no configs" rather than an error so
        # a hand-emptied file doesn't crash the daemon. The first-run
        # UX (ensure_configs) is the only sanctioned way to populate.
        return {}
    if not isinstance(data, dict):
        raise ConfigsError(f"{p} root must be a mapping; got {type(data).__name__}")
    configs_raw = data.get("configs")
    if configs_raw is None:
        # An empty configs file with no `configs:` key is the same as
        # an empty registry — not an error.
        return {}
    if not isinstance(configs_raw, list):
        raise ConfigsError(
            f"{p} `configs` must be a list; got {type(configs_raw).__name__}"
        )
    if len(configs_raw) > MAX_CONFIGS:
        raise ConfigsError(
            f"{p} has {len(configs_raw)} entries; max is {MAX_CONFIGS}"
        )
    out: dict[str, Config] = {}
    for i, entry in enumerate(configs_raw):
        if not isinstance(entry, dict):
            raise ConfigsError(
                f"{p} configs[{i}] must be a mapping; got {type(entry).__name__}"
            )
        cfg = Config.from_dict(entry)
        if cfg.name in out:
            raise ConfigsError(
                f"{p} has duplicate config name {cfg.name!r}"
            )
        out[cfg.name] = cfg
    return out


def save_configs(path: Path | str | None, configs: Mapping[str, Config]) -> None:
    """Atomically write configs to YAML.

    Writes a mapping with one key, `configs:`, holding a list of dicts
    (the canonical layout; matches the schema in T-023). Order of the
    list is the insertion order of the input mapping; for stable
    round-trips, callers that want a particular display order should
    pass an `OrderedDict`.
    """
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"configs": [cfg.to_dict() for cfg in configs.values()]}
    # Use safe_dump with sort_keys=False so insertion order is preserved
    # (matches what the user expects when reading the file).
    text = yaml.safe_dump(
        payload,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    atomic_io.atomic_write_text(p, text, encoding="utf-8")


def ensure_configs(path: Path | str | None = None) -> dict[str, Config]:
    """First-run UX (feat-011 step 2): populate defaults if file missing.

    Returns the loaded configs after the operation. Idempotent — running
    it on an already-populated file is a no-op.
    """
    p = _resolve(path)
    if p.exists():
        return load_configs(p)
    defaults = {t["name"]: Config.from_dict(t) for t in DEFAULT_TEMPLATES}
    save_configs(p, defaults)
    return defaults


# ---------- mutation helpers ----------

def add_config(
    path: Path | str | None,
    cfg: Config,
) -> dict[str, Config]:
    """Add a new config entry. Refuses if `cfg.name` already exists."""
    p = _resolve(path)
    configs = load_configs(p) if p.exists() else {}
    if cfg.name in configs:
        raise ConfigsError(f"config {cfg.name!r} already exists in {p}")
    configs[cfg.name] = cfg
    save_configs(p, configs)
    return configs


def update_config(
    path: Path | str | None,
    name: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Config]:
    """Update a single config entry by name.

    Only the kwargs explicitly provided are touched. `extras` replaces
    the existing extras mapping (pass-through semantics — there is no
    "merge" because extras is free-form and the user is responsible
    for its keys).
    """
    p = _resolve(path)
    configs = load_configs(p)
    if name not in configs:
        raise ConfigsError(f"config {name!r} not found in {p}")
    existing = configs[name]
    new_extras = dict(extras) if extras is not None else dict(existing.extras)
    updated = Config(
        name=existing.name,
        provider=provider if provider is not None else existing.provider,
        model=model if model is not None else existing.model,
        base_url=base_url if base_url is not None else existing.base_url,
        api_key_env=api_key_env if api_key_env is not None else existing.api_key_env,
        extras=new_extras,
    )
    # Validate the updated entry round-trips through Config.from_dict
    # so partial updates cannot produce an invalid record.
    Config.from_dict(updated.to_dict())
    configs[name] = updated
    save_configs(p, configs)
    return configs


def delete_config(path: Path | str | None, name: str) -> dict[str, Config]:
    """Remove a config entry by name. Raises if missing."""
    p = _resolve(path)
    configs = load_configs(p)
    if name not in configs:
        raise ConfigsError(f"config {name!r} not found in {p}")
    del configs[name]
    save_configs(p, configs)
    return configs


def get_config(
    path: Path | str | None, name: str
) -> Config:
    """Fetch a single config by name; raises ConfigsError if missing."""
    configs = load_configs(path)
    if name not in configs:
        raise ConfigsError(f"config {name!r} not found")
    return configs[name]


def list_configs(path: Path | str | None = None) -> list[Config]:
    """Return all configs as a list (in registry order).

    Returns `[]` for a missing file (callers that want first-run UX
    should call `ensure_configs` first instead).
    """
    p = _resolve(path)
    if not p.exists():
        return []
    return list(load_configs(p).values())


# ---------- API-key resolution (T-015 chokepoint) ----------

def resolve_api_key(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Read the env var named by `cfg.api_key_env`. Returns `None` on miss.

    Single chokepoint per T-015. The daemon MUST go through this
    function instead of calling `os.environ[...]` directly so that:
      - missing keys produce a clear `None` (not a KeyError), and
      - the resolved value is never accidentally logged (this function
        returns the value, but tests assert the caller pattern does
        not log it; the structured logger's redaction layer is the
        second line of defense).

    `env` defaults to `os.environ`; tests pass an explicit mapping.
    """
    src = env if env is not None else os.environ
    return src.get(name)


def resolve_api_key_for_config(
    cfg: Config,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Convenience: resolve the env var named by `cfg.api_key_env`."""
    return resolve_api_key(cfg.api_key_env, env=env)
