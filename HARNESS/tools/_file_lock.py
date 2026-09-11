"""Small cross-platform advisory file-lock helper.

Lock target is a sibling sentinel ``<path>.lock``, NOT the data file
itself.

Why a sentinel
--------------
Windows ``msvcrt.locking`` is a *byte-range* OS lock on whatever file
descriptor you hand it, and it does not distinguish "my own lock" from
"someone else's". If we lock the data file itself (the obvious design),
the lock covers byte 0; any later ``open(path, "r")`` on the same file
- including the same process's own ``load_features()`` call, which has
to read byte 0 first - raises ``PermissionError`` on Windows. POSIX
``fcntl.flock`` is whole-file and process-level so it does not have this
problem, but mixing the two semantics across platforms is a bug magnet.

The sentinel decouples the two roles: the data file is never locked,
and only the sentinel is. ``path.with_name(path.name + ".lock")`` is
the lock target on both platforms. POSIX ``fcntl.flock`` on the sentinel
still provides whole-file cross-process coordination for cooperating
processes; Windows ``msvcrt.locking`` locks byte 0 of the sentinel,
which is fine because nothing else opens the sentinel for I/O.

The sentinel is materialised via ``Path.touch(exist_ok=True)`` so a
first mutator does not race a sibling. After a successful run the
sentinel is intentionally left on disk: a stale sentinel (process
killed mid-mutation) is harmless because the OS releases the byte lock
on handle close (``fcntl`` releases on process exit, ``msvcrt`` releases
on handle close); the next caller opens the same file and takes the
same byte lock. Unlinking at ``finally`` would race the unlock-then-
reacquire window and is not done here.

Why keep this at all
--------------------
The harness's documented deployment model is one Coding Agent session
at a time (Initializer runs once, Coding Agents run serially via
``session_end.py``'s atomic commit). Strictly under that contract,
advisory write-vs-write locks are redundant: ``os.replace`` in
``_atomic_io.atomic_write_json`` is already atomic on POSIX and on
NTFS, so any reader sees either the old file or the new file, never
half-written bytes, regardless of any ``flock``/``msvcrt.locking``.

The lock is kept anyway because:

1. **Concurrency accidents happen.** Two terminals, a Coding Agent
   and a human running ``vi``, a stray cron job, or a future
   multi-agent extension — none of these are the "one session at a
   time" model, but all of them are realistic. The lock turns "two
   writers race and the last ``os.replace`` silently wins, with no
   signal to either caller" into "one caller blocks up to five
   seconds and the other gets a clear ``TimeoutError``". The latter
   is recoverable; the former is silent corruption.

2. **Cost is negligible.** A sentinel ``touch`` + ``open("r+")`` +
   ``flock``/``msvcrt.locking`` + ``unlock`` + ``close`` is on the
   order of single-digit milliseconds. Mutating CLI commands already
   pay for ``load_features`` + ``atomic_write_json``; the lock
   overhead is a small fraction of the total.

3. **Removal would change every mutator's failure mode.** Today a
   concurrent attempt surfaces as a five-second hang followed by
   ``TimeoutError("another mutation in progress")``. Removing the
   lock would replace that with last-writer-wins ``os.replace`` —
   no signal at all, no log line, no error code. ``CODE_STYLE.md``'s
   "Data integrity via scripts" hard rule treats silent data loss
   as a code-review blocker; the lock is the runtime enforcement of
   that rule.

If you are considering deleting this module, first update
``CODE_STYLE.md`` to make the "serial writers only" constraint
explicit, audit every ``open(FEATURE_LIST_PATH, ...)`` call site for
unprotected writes (``tools/session_end.py``'s step_backfill_attempts
was the last one to be wrapped, see commit history), and accept that
two concurrent ``feature_list.py mark-passing`` invocations will then
silently overwrite each other.

Public API contract: ``file_lock(path, *, exclusive=True) -> Iterator``.
A five-second acquisition timeout raises ``TimeoutError``. A missing
data file at lock-time is NOT auto-created; ``mutating_fn`` will surface
the missing-file error from its own read path (``load_features`` /
``fail(...)``). A missing sentinel IS auto-created.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.05
_SENTINEL_SUFFIX = ".lock"


def sentinel_path(path: Path) -> Path:
    """Return the sentinel lock path associated with ``path``."""
    return path.with_name(path.name + _SENTINEL_SUFFIX)


def _try_lock(file: TextIO, *, exclusive: bool) -> None:
    """Attempt one non-blocking advisory lock acquisition on the sentinel."""
    fd = file.fileno()
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        mode = msvcrt.LK_NBLCK if exclusive else msvcrt.LK_NBRLCK
        msvcrt.locking(fd, mode, 1)
    else:
        import fcntl

        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, mode | fcntl.LOCK_NB)


def _unlock(file: TextIO) -> None:
    """Release the advisory lock held on the sentinel."""
    fd = file.fileno()
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Hold an advisory lock on ``<path>.lock``, waiting at most five seconds.

    The lock target is a sibling sentinel, not ``path`` itself; see
    the module docstring for the rationale. ``path`` is not opened or
    modified by this context manager. A missing sentinel is created
    lazily via ``Path.touch(exist_ok=True)``.
    """
    sentinel = sentinel_path(path)
    sentinel.touch(exist_ok=True)
    with sentinel.open("r+", encoding="utf-8") as file:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                _try_lock(file, exclusive=exclusive)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("another mutation in progress") from exc
                time.sleep(_LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            _unlock(file)
