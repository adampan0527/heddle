# SPDX-License-Identifier: Apache-2.0
"""Multi-process signal forwarding for ``heddle dev`` — feat-051 (T-019).

Wraps the install / dispatch / restore lifecycle for SIGINT and
SIGTERM handlers so ``dev.py`` can stay focused on orchestration.
Differs from ``start_signals.forward_signals_to`` (feat-050) in two
ways:

1. The dev supervisor owns THREE children (Vite, Node.js, daemon)
   instead of one; the dispatcher forwards the signal to ALL of
   them so Ctrl+C cleans up every process group.
2. The dispatch order is the *reverse* of spawn order so the
   daemon (innermost) gets the signal first and the Vite dev
   server (outermost) gets it last — matches the production
   shutdown order in ``packages/node/src/main.ts``.

Kept separate from ``dev.py`` so each module is under the 200-line
soft cap from CODE_STYLE.md.
"""

from __future__ import annotations

import signal
import subprocess
from typing import Callable, Iterable


def forward_signals_to_all(
    procs: Iterable[subprocess.Popen],
    signals: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM),
) -> Callable[[], None]:
    """Install SIGINT/SIGTERM handlers that forward to every proc.

    Returns a ``restore()`` callable the caller MUST invoke in a
    ``finally`` block so the original OS handlers come back even
    when one of the children exited on its own. ``send_signal``
    errors are swallowed — a dead child must not crash the
    supervisor that is busy shutting down siblings.

    ``procs`` is materialised once into a list so the handler
    closure does not iterate a generator more than once. Spawn
    order is reversed for the dispatch so the innermost child
    shuts down first.
    """
    procs_list = list(procs)

    def _on_signal(signum: int, _frame: object) -> None:
        # Reverse-spawn order: daemon first, Vite last. Matches the
        # production shutdown order in ``packages/node/src/main.ts``.
        for proc in reversed(procs_list):
            try:
                proc.send_signal(signum)
            except (ProcessLookupError, OSError):
                # Child already exited; nothing to forward.
                pass

    previous: dict[int, signal._HANDLER | int | None] = {}
    for sig in signals:
        previous[sig] = signal.signal(sig, _on_signal)

    def restore() -> None:
        for sig, prev in previous.items():
            signal.signal(sig, prev)

    return restore


__all__ = ["forward_signals_to_all"]
