# SPDX-License-Identifier: Apache-2.0
"""LLM-config resolution chokepoint for per-feature execution — feat-031.

When the daemon receives a ``start_feature`` or ``retry_feature``
envelope (feat-030) carrying a feature's ``implementation_model`` field
(feat-010), this module is the single place that turns that string into
a ``heddle_common.configs_io.Config``. The flow is:

    implementation_model (str | None)
      │
      ├─ None  ──► list_configs(configs_path)[0]   (the user's default)
      │
      └─ "name"──► configs_io.get_config(configs_path, name)

On success the caller gets a ``(Config, source)`` tuple where ``source``
is one of:

    "explicit" — the feature's implementation_model string matched a
                 named config entry verbatim.
    "default"  — the feature's implementation_model was None and the
                 first registry entry was used as a fallback.

The fallback is logged with the event name ``default_config_applied``
so an operator can audit which feature ran on which model when the
``implementation_model`` field is left at its v0.1 default of ``null``.

Failure modes (all raised as ``LLMConfigError(cause="llm_config_error")``
so the daemon can map them to a stable wire code):

    * the configs registry does not exist and ``ensure_configs`` could
      not create one (e.g. ``~/.heddle`` not writable)
    * the registry exists but is empty (no entries to fall back to)
    * the feature's ``implementation_model`` is a non-null string that
      does not match any registered config name

``ConfigsError`` from the underlying library is intentionally NOT
re-raised as-is; the daemon's WS layer expects a single classification
(``llm_config_error``), and exposing ``ConfigsError`` would force the
route handler to re-classify on every code path.

Design rule: this module knows about ``configs_io`` and the
``LLMConfigError`` from ``heddle_daemon.llm``. It does NOT know about
LangChain chat-model classes (``agent_runtime.build_llm_for_feature``
is the layer that wraps the resolved Config into an actual chat model).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from heddle_common import logging as _logging
from heddle_common.configs_io import (
    Config,
    ConfigsError,
    default_configs_path,
    ensure_configs,
    get_config,
)

from heddle_daemon.llm import LLMConfigError

__all__ = [
    "DEFAULT_SOURCE",
    "EXPLICIT_SOURCE",
    "resolve_feature_llm_config",
]


# Single source of truth for the source field on the returned tuple.
# Mirrors the ``source: "explicit" | "default"`` discriminant emitted
# in the ``llm_resolved`` WS event so the browser / supervisor can
# render "ran on default" vs "ran on user's choice" without re-parsing
# prose.
EXPLICIT_SOURCE: Final[str] = "explicit"
DEFAULT_SOURCE: Final[str] = "default"


def _log_explicit(config_name: str, configs_path: Path | None) -> None:
    """Single-source structured log for an explicit-name hit."""
    _logging.info(
        component="llm_config",
        event="explicit_config_resolved",
        msg=(
            f"resolved implementation_model to named config {config_name!r}"
        ),
        config_name=config_name,
        source=EXPLICIT_SOURCE,
        configs_path=str(configs_path) if configs_path is not None else None,
    )


def _log_default(config_name: str, configs_path: Path | None) -> None:
    """Single-source structured log for the None → first-entry fallback.

    Logging this is load-bearing: the v0.1 default for
    ``implementation_model`` is ``null`` for every feature, so a user
    audit of "which features actually ran on which model" depends on
    this event landing in the per-project log.
    """
    _logging.info(
        component="llm_config",
        event="default_config_applied",
        msg=(
            f"feature.implementation_model was None; falling back to first "
            f"registry entry {config_name!r}"
        ),
        config_name=config_name,
        source=DEFAULT_SOURCE,
        configs_path=str(configs_path) if configs_path is not None else None,
    )


def resolve_feature_llm_config(
    feature_implementation_model: str | None,
    *,
    configs_path: Path | str | None = None,
) -> tuple[Config, str]:
    """Resolve a feature's ``implementation_model`` to a registered Config.

    Args:
        feature_implementation_model: the value of the feature's
            ``implementation_model`` field. ``None`` (or empty string)
            triggers the default-fallback path; any other string must
            match a registered config name exactly.
        configs_path: path to the YAML registry. ``None`` means "use the
            default location" (``~/.heddle/configs.yaml`` per T-023).
            Tests pass an explicit path.

    Returns:
        ``(config, source)`` where ``source`` is :data:`EXPLICIT_SOURCE`
        or :data:`DEFAULT_SOURCE`.

    Raises:
        LLMConfigError: with ``cause="llm_config_error"``. Surfaced by
            the daemon's WS layer as ``ok: false, error.code =
            "llm_config_error"``. Possible triggers:

            - the registry does not exist and could not be created
              (``ensure_configs`` failed), or exists but is empty
              (no default available when ``implementation_model`` is None);
            - ``feature_implementation_model`` is a non-null string that
              does not match any registered config name.
    """
    if configs_path is None:
        resolved_path: Path | None = default_configs_path()
    else:
        resolved_path = Path(configs_path).expanduser()

    # Empty string is normalized to None so a hand-edited
    # ``implementation_model: ""`` does the same thing as the omitted
    # field. The library treats both as "no explicit choice".
    name = (feature_implementation_model or "").strip() or None

    if name is None:
        # First-run UX: if the file does not exist, populate the four
        # default templates so a user with a fresh ``~/.heddle`` does
        # not crash on the first drag-to-in_progress. ``ensure_configs``
        # is idempotent; an already-populated file is returned as-is.
        try:
            registry = ensure_configs(resolved_path)
        except ConfigsError as exc:
            _logging.error(
                component="llm_config",
                event="configs_ensure_failed",
                msg=f"ensure_configs failed for {resolved_path}: {exc}",
                configs_path=str(resolved_path),
                error_type=type(exc).__name__,
            )
            raise LLMConfigError(
                f"could not initialize configs registry at {resolved_path}: {exc}"
            ) from exc
        if not registry:
            raise LLMConfigError(
                f"configs registry at {resolved_path} is empty; "
                "add at least one named config under `configs:`"
            )
        # ``registry`` is a dict keyed by config name; insertion order
        # is preserved and the first key is the user's selected default
        # per D-055 / feat-011 ("default to first entry in the registry").
        first_name = next(iter(registry))
        config = registry[first_name]
        _log_default(config.name, resolved_path)
        return config, DEFAULT_SOURCE

    # Explicit name path.
    try:
        config = get_config(resolved_path, name)
    except ConfigsError as exc:
        # ``get_config`` raises ``ConfigsError("config 'X' not found")``
        # for unknown names; map it to a structured LLMConfigError so
        # the daemon can surface "unknown implementation_model" without
        # parsing prose.
        _logging.warn(
            component="llm_config",
            event="unknown_implementation_model",
            msg=(
                f"feature.implementation_model={name!r} is not a registered "
                f"config name in {resolved_path}: {exc}"
            ),
            config_name=name,
            configs_path=str(resolved_path),
            error_type=type(exc).__name__,
        )
        raise LLMConfigError(
            f"unknown implementation_model {name!r}; expected one of the "
            f"names in {resolved_path} (use `heddle configs list` to inspect)"
        ) from exc
    _log_explicit(config.name, resolved_path)
    return config, EXPLICIT_SOURCE
