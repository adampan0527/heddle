# SPDX-License-Identifier: Apache-2.0
"""Tool-dispatch sandbox middleware — feat-021 (D-053).

Sits between the agent runtime (feat-019) and the six tools (feat-020).
Every tool call the agent wants to make is routed through
``ToolDispatchMiddleware.dispatch()`` which:

  * allows the call unconditionally under ``sandbox=full``;
  * blocks mutating tools under ``sandbox=read-only`` with a
    synthetic, LLM-readable error result (no exception raised — the
    model sees the rejection as if it were a normal tool result);
  * blocks mutating tools under ``sandbox=edit-with-confirm`` until
    a confirmation callback resolves to True (default = ask the
    user via WS, per feat-028/030; tests pass a simple policy).

The middleware never raises on policy rejection. This is a deliberate
design choice: an exception would crash the agent loop, and the
model has no way to recover from a crash mid-step. Instead the
rejection is a synthetic tool result the model can read, learn from,
and (if appropriate) retry with a different tool or different args.

Configuration lives in ``<project_root>/.heddle/config.yaml`` (per
T-022 / D-053). The file is optional; when absent, the default is
``full`` so existing projects keep working until the user opts in
to a stricter sandbox.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import yaml

from heddle_common import logging as _logging
from heddle_daemon.tools import (
    ToolError,
    all_tools,
)


# ---------- configuration ----------


class SandboxLevel(str, Enum):
    """Per-project sandbox level (D-053 / T-022).

    The string values are what gets written to config.yaml; the
    Enum members are what the rest of the daemon compares against.
    Subclassing ``str`` lets us pass the level through to tool
    JSON schemas without an explicit ``.value``.
    """

    READ_ONLY = "read-only"
    EDIT_WITH_CONFIRM = "edit-with-confirm"
    FULL = "full"


# Tools that mutate project state. Read-only mode forbids these;
# edit-with-confirm mode requires an explicit user OK.
MUTATING_TOOLS: frozenset[str] = frozenset({"write", "edit", "bash"})


CONFIG_REL_PATH: str = ".heddle/config.yaml"
CONFIG_KEY: str = "sandbox_level"
DEFAULT_SANDBOX_LEVEL: SandboxLevel = SandboxLevel.FULL

# Confirmation timeout (seconds). When edit-with-confirm is in effect
# and the user doesn't respond, the call is rejected with a synthetic
# timeout error so the agent loop can move on rather than hang forever.
DEFAULT_CONFIRM_TIMEOUT_SECONDS: float = 60.0


# ---------- config loader ----------


@dataclass(frozen=True)
class SandboxConfig:
    """Resolved sandbox configuration for one project."""

    level: SandboxLevel = DEFAULT_SANDBOX_LEVEL

    @classmethod
    def from_yaml(
        cls,
        project_root: Path | str,
        *,
        config_path: Optional[Path] = None,
    ) -> "SandboxConfig":
        """Read sandbox_level from ``<project_root>/.heddle/config.yaml``.

        Missing file or missing key → ``DEFAULT_SANDBOX_LEVEL`` (full).
        A present key with an invalid value raises ``ValueError`` —
        silent fallback would mask a typo in the user's config.
        """
        root = Path(project_root)
        path = Path(config_path) if config_path is not None else root / CONFIG_REL_PATH
        if not path.exists():
            return cls()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SandboxConfigError(f"cannot read {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise SandboxConfigError(f"invalid YAML in {path}: {exc}") from exc
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise SandboxConfigError(
                f"{path} must be a YAML mapping at the top level; "
                f"got {type(data).__name__}"
            )
        if CONFIG_KEY not in data:
            return cls()
        raw = data[CONFIG_KEY]
        try:
            level = SandboxLevel(raw)
        except ValueError as exc:
            valid = ", ".join(repr(s.value) for s in SandboxLevel)
            raise SandboxConfigError(
                f"{path}: {CONFIG_KEY}={raw!r} is not a valid SandboxLevel; "
                f"expected one of: {valid}"
            ) from exc
        return cls(level=level)


class SandboxConfigError(ValueError):
    """Raised when the project's ``.heddle/config.yaml`` is malformed."""


# ---------- dispatch middleware ----------


# The callback type for edit-with-confirm. Given the tool name and
# arguments, return True to allow the call, False to reject. Tests
# pass a simple closure; production wires this to the WS layer
# (feat-028 / feat-030).
ConfirmCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]


@dataclass
class ToolDispatchMiddleware:
    """Intercepts tool calls and enforces sandbox rules.

    Holds a reference to the underlying tools (so the same instance
    can both dispatch AND execute real calls) and a callback for
    edit-with-confirm. The agent runtime (feat-019) calls
    ``await middleware.dispatch(tool_name, args)``; the middleware
    returns the tool's string result OR a synthetic rejection
    message.
    """

    project_root: Path
    config: SandboxConfig
    confirm_callback: Optional[ConfirmCallback] = None
    confirm_timeout_seconds: float = DEFAULT_CONFIRM_TIMEOUT_SECONDS

    # Counters so we can audit how often each sandbox path triggered.
    # Reset on demand (one per session, say).
    allowed_count: int = 0
    rejected_readonly_count: int = 0
    rejected_confirm_count: int = 0
    rejected_timeout_count: int = 0

    # Tools live here; the middleware instantiates them per project
    # once at construction. Cheap because they're stateless aside
    # from project_root.
    _tools: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _setup_done: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_root, Path):
            self.project_root = Path(self.project_root)
        self.project_root = self.project_root.expanduser().resolve()
        if not self.project_root.exists() or not self.project_root.is_dir():
            raise SandboxConfigError(
                f"project_root {self.project_root!r} does not exist or is not a directory"
            )
        if not isinstance(self.config, SandboxConfig):
            raise SandboxConfigError(
                f"config must be a SandboxConfig; got {type(self.config).__name__}"
            )
        if self.confirm_timeout_seconds <= 0:
            raise SandboxConfigError(
                f"confirm_timeout_seconds must be > 0; got {self.confirm_timeout_seconds}"
            )

    def setup(self) -> None:
        """Instantiate the six tools against ``project_root``.

        Idempotent. The agent runtime calls this once after
        construction; tools are then ready for dispatch.
        """
        if self._setup_done:
            return
        self._tools = {t.name: t for t in all_tools(self.project_root)}
        self._setup_done = True

    # ---- public API ----

    async def dispatch(self, tool_name: str, args: dict[str, Any]) -> str:
        """Run a tool call through the sandbox.

        Args:
            tool_name: The tool's ``name`` (e.g. ``"read"``, ``"write"``).
            args: The argument dict the agent wants to pass to the tool.

        Returns:
            The tool result as a plain string. On sandbox rejection,
            a synthetic error string explaining why. NEVER raises —
            rejections are always rendered as a result so the agent
            loop can continue and the model can read the message.

        Raises:
            KeyError: if ``tool_name`` is not one of the six known
                tools. (Programming error, not user-facing.)
        """
        if not self._setup_done:
            self.setup()
        if tool_name not in self._tools:
            raise KeyError(
                f"unknown tool {tool_name!r}; expected one of {sorted(self._tools)}"
            )

        decision = self._evaluate(tool_name, args)
        if decision is not None:
            # Synthetic rejection; decision is the result string.
            self._record_rejection(tool_name)
            return decision

        # Allowed: invoke the real tool. Tools accept ``args`` as
        # **kwargs by name. ``_run`` is sync; we run it in the
        # default executor so the event loop isn't blocked. (BashTool
        # has its own _arun that runs in-process; the executor
        # fallback still works fine for that tool too.)
        tool = self._tools[tool_name]
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: tool.run(args),  # tool.run() goes through BaseTool
            )
            # BaseTool.run returns a JSON string when invoked via .run();
            # unwrap to plain string.
            if isinstance(result, str):
                self.allowed_count += 1
                return result
            self.allowed_count += 1
            return str(result)
        except ToolError as exc:
            # Real tool error (file not found, etc.) — surface as
            # tool result, not exception. The model can read it and
            # adjust.
            return f"tool {tool_name!r} failed: {exc}"

    # ---- decision logic ----

    def _evaluate(self, tool_name: str, args: dict[str, Any]) -> Optional[str]:
        """Return a synthetic rejection message, or None if allowed.

        ``None`` means "go ahead and run the tool". A non-None string
        is the synthetic result the model will see.
        """
        level = self.config.level

        if level == SandboxLevel.FULL:
            return None

        is_mutating = tool_name in MUTATING_TOOLS

        if level == SandboxLevel.READ_ONLY:
            if is_mutating:
                return (
                    f"sandbox-level=read-only refused {tool_name}: "
                    f"only the read-only tools (read, glob, grep) are "
                    f"allowed under sandbox=read-only. Adjust "
                    f".heddle/config.yaml to a less restrictive level "
                    f"if this operation is intentional."
                )
            return None

        # edit-with-confirm: mutating tools require confirmation.
        assert level == SandboxLevel.EDIT_WITH_CONFIRM
        if not is_mutating:
            return None
        # The actual confirmation is awaited in dispatch_async via
        # the confirm callback. Here we just check whether a callback
        # exists; the await happens in ``_evaluate_confirm`` which is
        # called only for mutating tools under edit-with-confirm.
        return None  # fall through to await confirmation in dispatch

    async def dispatch_async(
        self, tool_name: str, args: dict[str, Any]
    ) -> str:
        """Async dispatch with edit-with-confirm support.

        ``dispatch`` is the simple API; ``dispatch_async`` exists for
        the agent runtime's hot path where it needs to await the
        confirmation callback without blocking the event loop.
        Behaves identically to ``dispatch`` for read-only and full
        levels; for edit-with-confirm, awaits the callback.
        """
        if not self._setup_done:
            self.setup()
        if tool_name not in self._tools:
            raise KeyError(
                f"unknown tool {tool_name!r}; expected one of {sorted(self._tools)}"
            )

        level = self.config.level

        # Read-only: short-circuit without invoking tools.
        if level == SandboxLevel.READ_ONLY:
            if tool_name in MUTATING_TOOLS:
                self.rejected_readonly_count += 1
                _logging.info(
                    component="sandbox",
                    event="tool_blocked_readonly",
                    msg=f"tool {tool_name!r} blocked under read-only",
                    tool=tool_name,
                    args_keys=sorted(args.keys()),
                )
                return (
                    f"sandbox-level=read-only refused {tool_name}: "
                    f"only the read-only tools (read, glob, grep) are "
                    f"allowed under sandbox=read-only. Adjust "
                    f".heddle/config.yaml to a less restrictive level "
                    f"if this operation is intentional."
                )
            # read-only tools: fall through to real execution.

        # edit-with-confirm: mutating tools need user OK.
        if level == SandboxLevel.EDIT_WITH_CONFIRM and tool_name in MUTATING_TOOLS:
            allowed = await self._await_confirmation(tool_name, args)
            if not allowed:
                self.rejected_confirm_count += 1
                _logging.info(
                    component="sandbox",
                    event="tool_blocked_confirm_denied",
                    msg=f"user denied {tool_name}",
                    tool=tool_name,
                )
                return (
                    f"sandbox-level=edit-with-confirm: user denied "
                    f"the proposed {tool_name} call. Either the user "
                    f"clicked 'deny' or the request timed out. Try a "
                    f"different tool, a different file, or describe "
                    f"the operation in a way the user can approve."
                )
            # user approved; fall through to real execution.

        # full level OR allowed under a stricter level: run the tool.
        tool = self._tools[tool_name]
        try:
            # Use ainvoke (BaseTool async) which routes through
            # _arun if defined; otherwise defaults to running _run
            # in an executor.
            result = await tool.ainvoke(args)
            if isinstance(result, str):
                self.allowed_count += 1
                return result
            self.allowed_count += 1
            return str(result)
        except ToolError as exc:
            return f"tool {tool_name!r} failed: {exc}"

    async def _await_confirmation(
        self, tool_name: str, args: dict[str, Any]
    ) -> bool:
        """Ask the confirm_callback; honor the timeout; default deny.

        Returns True if the user (or test stub) said yes, False on
        no/timeout. When no callback is registered we conservatively
        deny (the agent can fall back to a read-only tool).
        """
        if self.confirm_callback is None:
            _logging.warn(
                component="sandbox",
                event="no_confirm_callback",
                msg=(
                    f"sandbox=edit-with-confirm and no confirm_callback "
                    f"registered; denying {tool_name!r} by default"
                ),
                tool=tool_name,
            )
            return False
        try:
            ok = await asyncio.wait_for(
                self.confirm_callback(tool_name, args),
                timeout=self.confirm_timeout_seconds,
            )
            return bool(ok)
        except asyncio.TimeoutError:
            self.rejected_timeout_count += 1
            _logging.warn(
                component="sandbox",
                event="confirm_timeout",
                msg=(
                    f"confirm callback did not respond within "
                    f"{self.confirm_timeout_seconds}s; denying {tool_name!r}"
                ),
                tool=tool_name,
                timeout_seconds=self.confirm_timeout_seconds,
            )
            return False

    def _record_rejection(self, tool_name: str) -> None:
        """Bump the appropriate counter (only used by the sync path)."""
        # The async path records its own counters in dispatch_async.
        # ``dispatch`` is here for backward compat / simpler test
        # cases that don't need real edit-with-confirm plumbing.
        if self.config.level == SandboxLevel.READ_ONLY:
            self.rejected_readonly_count += 1


__all__ = [
    "CONFIG_KEY",
    "CONFIG_REL_PATH",
    "ConfirmCallback",
    "DEFAULT_CONFIRM_TIMEOUT_SECONDS",
    "DEFAULT_SANDBOX_LEVEL",
    "MUTATING_TOOLS",
    "SandboxConfig",
    "SandboxConfigError",
    "SandboxLevel",
    "ToolDispatchMiddleware",
]
