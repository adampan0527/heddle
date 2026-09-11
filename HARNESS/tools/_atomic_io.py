#!/usr/bin/env python3
"""_atomic_io.py — crash-safe writers for harness data files.

Stdlib only. Three thin wrappers used by tools/feature_list.py,
tools/session_end.py, and tools/progress_rotate.py to keep their
write paths atomic:

  - atomic_write_text(path, text)        : write text atomically.
  - atomic_write_json(path, data, ...)  : dump JSON atomically.
  - load_json(path)                     : parse JSON (read-only,
                                          centralized for reuse).

Why atomic writes?
------------------
A naïve `Path.write_text(...)` truncates the target file the moment the
write starts. If the process crashes (or the host loses power, or the
agent's session is killed mid-write) between the truncate and the
`close()`, the file is left empty or half-written, and the harness's
canonical data files (`feature_list.json`, `current_progress.txt`) are
permanently corrupt until a human restores them from git.

The atomic recipe used below writes the new payload to a sibling
temporary file, `flush()`es Python buffers, calls `os.fsync()` to push
the bytes to disk, and finally renames the temp file over the target
via `os.replace()`. `os.replace` is atomic on both POSIX (where it
maps to `rename(2)`) and on Windows (where the underlying
`MoveFileExW(..., MOVEFILE_REPLACE_EXISTING)` is atomic on the same
volume). Because the temp file lives in `path.parent`, both paths
share a volume and the rename is guaranteed atomic. Only after the
rename succeeds is the caller allowed to see the new content.

`tempfile.NamedTemporaryFile(..., delete=False)` is used on purpose:
on Windows the default `delete=True` opens with `O_TEMPORARY`, which
prevents us from opening the path a second time for the rename.
`delete=False` + explicit `os.replace` + best-effort cleanup is the
robust cross-platform recipe.

No third-party deps; only `json`, `os`, `tempfile`, `pathlib`,
`typing` are pulled in.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


# Suffix used for the sibling temp file while a write is in flight.
# Kept as a module-level constant so tests / future cleanup hooks can
# refer to it without re-stringifying "tmp".
_TMP_SUFFIX = ".tmp"


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Write `text` to `path` atomically.

    Crash-safety contract: on return, either `path` already existed
    with its old contents, or it was fully replaced with `text`
    encoded as `encoding`. There is no observable intermediate state
    where the file is empty/truncated but not yet written.

    `newline=""` is passed to `NamedTemporaryFile` so Python does not
    translate `\n` to the host's native line endings on Windows;
    whatever we put in `text` is what hits disk.
    """
    path = Path(path)
    # Ensure parent dir exists; otherwise `dir=path.parent` in
    # NamedTemporaryFile raises on the first call against a brand-new
    # project tree. NamedTemporaryFile requires the dir to already
    # exist; we don't recursively create it ourselves here because
    # callers in this harness always operate on files whose parent
    # is the project root or `sessions/older/` (created elsewhere).
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp: Path | None = None
    try:
        # `mode="w"` opens in text mode (str), not bytes; `encoding`
        # is used for that decode/encode. `newline=""` preserves
        # exactly the bytes we write. `delete=False` is critical on
        # Windows (see module docstring).
        with tempfile.NamedTemporaryFile(
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=_TMP_SUFFIX,
            mode="w",
            encoding=encoding,
            newline="",
            delete=False,
        ) as fh:
            fh.write(text)
            # flush() drains Python's user-space buffer into the OS;
            # fsync() then asks the OS to flush its own buffer to
            # the physical disk. Without fsync a kernel panic or
            # power loss can still leave us with a zero-byte file
            # after the rename. On Linux this maps to fsync(2); on
            # Windows `_commit()` is called under the hood.
            fh.flush()
            os.fsync(fh.fileno())
            tmp = Path(fh.name)
        # os.replace is atomic on POSIX and on Windows (same volume).
        # It will happily overwrite an existing file, which is what
        # we want — this is a "replace" not an "add".
        os.replace(tmp, path)
    finally:
        # If we crashed before os.replace, leave the temp file in
        # place; the user's editor / `git clean` / next successful
        # write can deal with it. Trying to unlink here would race
        # with the rename on some Windows error paths and could
        # swallow a failure we want to surface.
        if tmp is not None and tmp.exists() and tmp != path:
            try:
                tmp.unlink()
            except OSError:
                # Best-effort cleanup; do not mask the real outcome.
                pass


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """Serialize `data` with `json.dumps` and write it atomically.

    The trailing newline is added to mirror the prior behavior of
    `tools/feature_list.py.save_features` (and the
    `step_backfill_attempts` writer in `tools/session_end.py`):
    callers in this harness treat the JSON files as text-ish
    artifacts and the blank line matters for `git diff` hygiene /
    POSIX convention.

    `indent=2, ensure_ascii=False` is the project's stable format
    contract for `feature_list.json` — do not change the defaults
    without also retraining every dependent reader.
    """
    text = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii) + "\n"
    atomic_write_text(path, text, encoding="utf-8")


def load_json(path: Path) -> Any:
    """Read and parse a JSON file. Thin wrapper around `json.load`.

    Centralized so callers don't repeat the
    `open(..., encoding="utf-8")` dance, and so a future move to
    e.g. `orjson` or comment-tolerant JSON can be made in one
    place. No atomicity on the read path — at worst a truncated
    read surfaces a `json.JSONDecodeError` to the caller, which
    is the right shape of error.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
