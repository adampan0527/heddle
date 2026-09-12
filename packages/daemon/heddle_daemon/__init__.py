# SPDX-License-Identifier: Apache-2.0
"""heddle-daemon: asyncio + LangGraph workflow engine.

Public modules:

    server         — daemon lifecycle, JSON envelope protocol, WS handler
                     (feat-017 skeleton, extended in feat-018 for project
                     path / checkpoint store wiring, feat-022 for
                     recursion_limit + LLM retry config)
    checkpointing  — per-project LangGraph SqliteSaver wrapper
                     (feat-018); the single chokepoint for "where is
                     the project's checkpoint DB?"
    tools          — six self-written LangChain tools (Read/Write/Edit/
                     Bash/Glob/Grep) for the agent runtime (feat-020).
    sandbox        — tool-dispatch middleware enforcing per-project
                     sandbox level (read-only / edit-with-confirm /
                     full) per D-053 (feat-021).
    agent_runtime  — self-written LLM ↔ tools loop with checkpoint
                     persistence and RecursionLimitError / LLMRetry-
                     ExhaustedError structured failures (feat-019,
                     extended in feat-022).
    llm            — factory turning a ``configs_io.Config`` into a
                     LangChain chat model for any of the four v0.1
                     providers (anthropic / openai / bedrock / ollama)
                     (feat-023).
"""

from heddle_daemon.agent_runtime import (
    AgentRuntime,
    LLMRetryExhaustedError,
    RecursionLimitError,
)
from heddle_daemon.checkpointing import ProjectCheckpointStore
from heddle_daemon.llm import LLMConfigError, build_chat_model
from heddle_daemon.sandbox import SandboxConfig, SandboxLevel, ToolDispatchMiddleware
from heddle_daemon.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    Daemon,
    DaemonConfig,
    JsonEnvelope,
    build_envelope,
    get_recursion_limit_from_env,
    parse_envelope,
    run_daemon,
)
from heddle_daemon.tools import (
    BashTool,
    EditTool,
    GlobTool,
    GrepTool,
    ReadTool,
    WriteTool,
    all_tools,
)

__version__ = "0.0.1"

__all__ = [
    "AgentRuntime",
    "BashTool",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "Daemon",
    "DaemonConfig",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "JsonEnvelope",
    "LLMConfigError",
    "LLMRetryExhaustedError",
    "ProjectCheckpointStore",
    "ReadTool",
    "RecursionLimitError",
    "SandboxConfig",
    "SandboxLevel",
    "ToolDispatchMiddleware",
    "WriteTool",
    "all_tools",
    "build_chat_model",
    "build_envelope",
    "get_recursion_limit_from_env",
    "parse_envelope",
    "run_daemon",
]
