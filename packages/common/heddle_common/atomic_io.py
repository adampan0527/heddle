# SPDX-License-Identifier: Apache-2.0
"""atomic_io — crash-safe writers and readers for heddle data files.

Per TECH.md T-014. Mirrors HARNESS/tools/_atomic_io.py so the same
crash-safety guarantees apply to the heddle-common library (which
the daemon uses for feature_list.json I/O on user projects).

Why atomic writes?
A naïve `Path.write_text(...)` truncates the target file at write
start. If the process is killed mid-write, the file is left empty
or half-written. The atomic recipe writes to a sibling temp file,
fsyncs, then `os.replace`s over the target. `os.replace` is atomic
on POSIX and Windows (same volume). Stdlib only.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["atomic_write_text", "atomic_write_json", "load_json"]

_TMP_SUFFIX = ".tmp"


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path` atomically. Crash-safe."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
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
            fh.flush()
            os.fsync(fh.fileno())
            tmp = Path(fh.name)
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists() and tmp != path:
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """Serialize `data` and write atomically."""
    text = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii) + "\n"
    atomic_write_text(path, text, encoding="utf-8")


def load_json(path: Path) -> Any:
    """Read and parse a JSON file."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)