# SPDX-License-Identifier: Apache-2.0
"""Pytest configuration for the daemon test suite.

Registers the ``slow`` mark so a happy-path e2e test (feat-049) can
opt into being skipped under ``heddle test -m "not slow"`` without
spuriously warning. Without this registration, pytest emits
``PytestUnknownMarkWarning`` for any ``@pytest.mark.slow`` decorator
in the suite.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "slow: end-to-end tests that spin up a real Daemon + WS + AgentRuntime",
    )
