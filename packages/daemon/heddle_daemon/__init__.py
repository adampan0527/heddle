# SPDX-License-Identifier: Apache-2.0
"""heddle-daemon: asyncio + LangGraph workflow engine.

Public modules:

    server        — daemon lifecycle, JSON envelope protocol, WS handler
                    (feat-017 skeleton, extended in feat-018 for project
                    path / checkpoint store wiring)
    checkpointing — per-project LangGraph SqliteSaver wrapper
                    (feat-018); the single chokepoint for "where is
                    the project's checkpoint DB?"

Other features (agent_runtime, sandbox middleware, LLM factory,
four LLM providers, restart budget, etc.) live in feat-019+.
"""

from heddle_daemon.checkpointing import ProjectCheckpointStore
from heddle_daemon.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    Daemon,
    DaemonConfig,
    JsonEnvelope,
    build_envelope,
    parse_envelope,
    run_daemon,
)

__version__ = "0.0.1"

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "Daemon",
    "DaemonConfig",
    "JsonEnvelope",
    "ProjectCheckpointStore",
    "build_envelope",
    "parse_envelope",
    "run_daemon",
]