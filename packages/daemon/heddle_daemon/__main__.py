# SPDX-License-Identifier: Apache-2.0
"""heddle_daemon.__main__ — `python -m heddle_daemon` entry point.

Defers to server.run_daemon() so the daemon is launchable both as a
module (for the Node.js supervisor's child_process.spawn call) and as
a console script (future work — feat-050 wires the heddle CLI to call
into this).
"""

from __future__ import annotations

import sys

from .server import run_daemon


if __name__ == "__main__":
    sys.exit(run_daemon())
