# SPDX-License-Identifier: Apache-2.0
"""Signal forwarding helper for ``heddle start`` — feat-050.

Wraps the install / dispatch / restore lifecycle for SIGINT and
SIGTERM handlers so ``start.py`` can stay focused on orchestration.
Kept separate from ``start.py`` so each file is under the 200-line
soft cap from CODE_STYLE.md.
"""

from __future__ import annotations

import signal
import subprocess
from typing import Callable


def forward_signals_to(
    proc: subprocess.Popen,
    signals: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM),
) -> Callable[[], None]:
    """Install SIGINT/SIGTERM handlers that forward to ``proc``.

    Returns a ``restore()`` callable the caller MUST invoke in a
    ``finally`` block so the original OS handlers come back even when
    the child exited on its own. ``proc.send_signal`` errors are
    swallowed — a dead child should not crash the supervisor.
    """

    def _on_signal(signum: int, _frame: object) -> None:
        try:
            proc.send_signal(signum)
        except (ProcessLookupError, OSError):
            pass

    previous: dict[int, signal._HANDLER | int | None] = {}
    for sig in signals:
        previous[sig] = signal.signal(sig, _on_signal)

    def restore() -> None:
        for sig, prev in previous.items():
            signal.signal(sig, prev)

    return restore


__all__ = ["forward_signals_to"]
