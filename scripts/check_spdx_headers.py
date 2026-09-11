#!/usr/bin/env python3
"""check_spdx_headers.py — assert every packages/* source file carries an SPDX-License-Identifier header.

Per feat-004. Scans packages/{cli,daemon,web}/** for .py/.ts/.tsx files and verifies each one contains
`SPDX-License-Identifier: Apache-2.0` somewhere in its first ~200 characters (right after any shebang line).
Exits 0 on full coverage, 1 with a per-file list of failures otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("packages/cli", "packages/daemon", "packages/web")
EXTENSIONS = {".py", ".ts", ".tsx"}
HEADER_LINE_RE = re.compile(r"SPDX-License-Identifier:\s*Apache-2\.0")


def has_header(path: Path) -> bool:
    """Return True iff the first ~200 chars contain the SPDX-Apache-2.0 line."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    head = text[:200]
    return bool(HEADER_LINE_RE.search(head))


def main() -> int:
    failures: list[Path] = []
    checked = 0
    for root in SCAN_ROOTS:
        for path in (REPO_ROOT / root).rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in EXTENSIONS:
                continue
            # Skip vendored or generated dirs (defensive — gitignore should already exclude)
            if "node_modules" in path.parts:
                continue
            checked += 1
            if not has_header(path):
                failures.append(path)

    if failures:
        print(f"FAIL: {len(failures)} of {checked} source file(s) missing SPDX-License-Identifier: Apache-2.0 header:")
        for f in failures:
            print(f"  - {f.relative_to(REPO_ROOT)}")
        return 1

    print(f"OK: all {checked} source file(s) under {', '.join(SCAN_ROOTS)} have SPDX-License-Identifier: Apache-2.0")
    return 0


if __name__ == "__main__":
    sys.exit(main())