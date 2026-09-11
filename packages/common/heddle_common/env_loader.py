# SPDX-License-Identifier: Apache-2.0
"""env_loader — fallback loader for `~/.heddle/.env` (feat-013).

Per DESIGN.md D-055 / TECH.md T-015 / feat-013 / feat-023, heddle reads
LLM API keys from environment variables (one per named config: e.g.
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). To make first-time setup painless
on a fresh box, heddle also accepts a single `~/.heddle/.env` file
holding KEY=VALUE lines, parsed and merged into `os.environ` at startup.

Rules (feat-013 step 1-5):

  1. **Process env wins.** Existing `os.environ` entries are never
     overwritten. The .env is a fallback for *missing* keys only.
  2. **KEY=VALUE format.** Lines are `KEY=VALUE`. Whitespace around
     KEY is trimmed. `#` lines are comments (full-line only). Blank
     lines are skipped.
  3. **Quoted values.** A value may be wrapped in single or double
     quotes; the quotes are stripped on read. Escapes inside double
     quotes (`\"`, `\\`, `\n`) are honoured; single-quoted values are
     literal (per POSIX shell convention).
  4. **Permission check.** If the file is group- or world-readable
     (`stat.S_IMODE(mode) & 0o077 != 0`), heddle refuses to read it.
     This is the same rule python-dotenv and most secret managers
     enforce for `.env` files (defence in depth against accidental
     chmod).
  5. **First-run side effect.** When `~/.heddle/` does not exist,
     `ensure_env_loader()` creates it and writes a `.gitignore`
     containing `.env`. The .env file itself is **never** written
     by heddle — the user authors it by hand. The "never writes"
     rule is asserted by the comment in this module + the absence
     of any write call below.

This module lives in `heddle_common/` (not the daemon or CLI) so both
binary entry points can call `load_env_file()` once at startup with no
duplicated parsing logic.

Why a custom parser instead of python-dotenv:

  - feat-013 step 3 mandates a hard fail on loose permissions.
    python-dotenv does this, but its error type and message are
    library-version-dependent. A 50-line stdlib-only parser keeps
    the contract auditable and avoids adding a runtime dep for one
    feature.
  - feat-013 step 5 mandates heddle "never writes to ~/.heddle/.env".
    Pulling in a library that auto-writes a sample file would
    silently violate that rule.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

from . import logging as _logging
from .atomic_io import atomic_write_text

__all__ = [
    "DEFAULT_ENV_DIR",
    "DEFAULT_ENV_PATH",
    "DEFAULT_GITIGNORE_PATH",
    "EnvPermissionError",
    "EnvParseError",
    "ensure_env_loader",
    "load_env_file",
    "parse_env_lines",
]

# ---------- constants ----------

DEFAULT_ENV_DIR: str = "~/.heddle"
DEFAULT_ENV_PATH: str = "~/.heddle/.env"
DEFAULT_GITIGNORE_PATH: str = "~/.heddle/.gitignore"

# A normal user-only mode. If the file's mode has any group/other bit
# set, refuse to read.
SECURE_MODE_BITS: int = 0o077  # bits that MUST NOT be set
SECURE_MODE_MASK: int = 0o777

# Hard caps against accidental /tmp mounts or symlink races.
MAX_ENV_BYTES: int = 1_048_576  # 1 MiB
MAX_ENV_LINES: int = 10_000

# Matches KEY=value, KEY="value with spaces", KEY='literal value',
# KEY=unquoted_value. KEY is constrained to a conservative POSIX-ish
# shape (letters, digits, underscore; must start with a letter or
# underscore) so we don't accidentally treat arbitrary shell text as
# a definition.
_LINE_RE = re.compile(
    r"""^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)
        \s*=\s*
        (?:
          "(?P<dq>(?:[^"\\]|\\.)*)"
        | '(?P<sq>[^']*)'
        | (?P<bare>[^#\s][^\s#]*)
        )
        \s*(?:\#.*)?$""",
    re.VERBOSE,
)


# ---------- error types ----------


class EnvPermissionError(Exception):
    """Raised when ~/.heddle/.env is readable by group or other.

    Carries the offending mode so callers can render an actionable
    message ("run `chmod 600 ~/.heddle/.env`").
    """

    def __init__(self, path: Path, mode: int):
        self.path = path
        self.mode = mode
        super().__init__(
            f"{path} has mode {oct(mode & SECURE_MODE_MASK)}; "
            f"it must be readable only by the owner (mode 0o600 or stricter). "
            f"Refusing to load to avoid leaking secrets. "
            f"Fix: chmod 600 {path}"
        )


class EnvParseError(Exception):
    """Raised when a .env line is malformed (key/value shape, not YAML)."""


# ---------- path helpers ----------


def default_env_path() -> Path:
    """Return the expanded default `~/.heddle/.env` path."""
    return Path(DEFAULT_ENV_PATH).expanduser()


def default_gitignore_path() -> Path:
    """Return the expanded default `~/.heddle/.gitignore` path."""
    return Path(DEFAULT_GITIGNORE_PATH).expanduser()


def _resolve(path: Path | str | None) -> Path:
    if path is None:
        return default_env_path()
    return path if isinstance(path, Path) else Path(path)


def _fail(msg: str) -> None:
    """Print to stderr and exit (matches configs_io / projects_io style)."""
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


# ---------- parsing ----------


def parse_env_lines(text: str) -> dict[str, str]:
    """Parse the body of a .env file into a dict.

    Honours feat-013 step 1 (KEY=VALUE format), step 1b (quoted
    values), step 1c (full-line `#` comments). Whitespace and blank
    lines are skipped silently. Malformed lines raise `EnvParseError`
    with the offending line number so the user can fix the file.

    Empty input yields an empty dict — loading a brand-new .env is
    not an error.
    """
    out: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            raise EnvParseError(
                f"line {lineno}: cannot parse {line!r}; "
                f"expected KEY=VALUE (KEY = [A-Za-z_][A-Za-z0-9_]*)"
            )
        key = m.group("key")
        if m.group("dq") is not None:
            # Decode the documented escapes inside double quotes.
            value = _decode_dq(m.group("dq"))
        elif m.group("sq") is not None:
            # Single-quoted: literal, no escape processing.
            value = m.group("sq")
        else:
            value = m.group("bare")
        if key in out:
            # Same-key repetition is allowed in shell but the *last*
            # one wins; mirror that for predictability.
            out[key] = value
        else:
            out[key] = value
    return out


def _decode_dq(s: str) -> str:
    """Decode double-quoted escape sequences (subset of POSIX shell).

    Honoured escapes: \\ \" \n \r \t. Any other backslash is left
    verbatim so users can carry Windows-style paths through.
    """
    # Order matters: handle the double-backslash first so the others
    # can use the resulting single backslash as their introducer.
    return (
        s.replace("\\\\", "\x00")
        .replace("\\\"", "\"")
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace("\x00", "\\")
    )


# ---------- loading ----------


def _check_secure_mode(path: Path) -> int:
    """Stat the file and raise EnvPermissionError if its mode is too open.

    Returns the file's mode on success so the caller can log it.
    Silently returns 0 if the file does not exist (no leak to log).

    On Windows, POSIX-style permission bits are not enforced by the
    OS (Windows uses ACLs); the check is skipped there with a debug
    log. POSIX systems (Linux, macOS) enforce strictly.
    """
    if not path.exists():
        return 0
    try:
        st = path.stat()
    except OSError as exc:
        _fail(f"cannot stat {path}: {exc}")
    mode = st.st_mode & SECURE_MODE_MASK
    if sys.platform.startswith("win"):
        # Windows file ACLs are out of scope; we cannot reliably
        # determine "is this readable by another user" from the
        # POSIX-style mode bits. Skip the check and rely on the
        # OS-level file system permissions instead.
        return mode
    if mode & SECURE_MODE_BITS:
        raise EnvPermissionError(path, mode)
    return mode


def load_env_file(
    path: Path | str | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Load `path` (default ~/.heddle/.env) into the process environment.

    Rules:

      - **Never overwrite existing process env entries.** Existing keys
        in `os.environ` (or the supplied `environ` shim for tests)
        keep their value; the .env value is recorded in the returned
        dict so callers can see what was loaded but is NOT installed.
      - **Missing file is a no-op.** First-run state is "no .env yet";
        load_env_file returns an empty dict and does not touch the
        process environment.
      - **Permission check is fatal.** Group/world-readable .env
        files raise `EnvPermissionError` and never have their contents
        merged into the process env. The daemon / CLI catch this and
        exit 1 (feat-013 step 3).
      - **Heddle NEVER writes to the .env file.** This function has
        no write paths (enforced by code review + the absence of
        any open(path, "w") call in this module).

    Returns the dict of {key: value_loaded_from_file} — useful for
    tests and for the CLI's `--show-loaded` diagnostic (future work).
    The values that were actually installed into the environment are
    those whose keys were not already present.
    """
    p = _resolve(path)
    env = os.environ if environ is None else environ

    if not p.exists():
        return {}

    # Permission check happens BEFORE we open the file so a hostile
    # world-readable .env cannot leak via error messages (the error
    # names the path but not its contents).
    _check_secure_mode(p)

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot read {p}: {exc}")
    if len(text.encode("utf-8")) > MAX_ENV_BYTES:
        _fail(
            f"{p} is larger than {MAX_ENV_BYTES} bytes; refusing to load. "
            "Check the path — large .env usually means a wrong file."
        )

    parsed = parse_env_lines(text)
    if len(parsed) > MAX_ENV_LINES:
        _fail(
            f"{p} defines more than {MAX_ENV_LINES} variables; refusing to load."
        )

    installed: dict[str, str] = {}
    for key, value in parsed.items():
        if key in env:
            # Process env wins — do NOT overwrite.
            continue
        env[key] = value
        installed[key] = value

    return installed


# ---------- first-run UX ----------


def ensure_env_loader(path: Path | str | None = None) -> Path:
    """First-run side effect: ensure ~/.heddle/.gitignore exists.

    Does NOT create the .env file itself — feat-013 step 5 explicitly
    forbids heddle from writing to it. The user authors the .env by
    hand (or copies from a template).

    Returns the resolved .gitignore path so callers can log it.
    """
    p = _resolve(path)
    gitignore = default_gitignore_path()
    if not p.exists() and not gitignore.exists():
        # First-run: create ~/.heddle/ if needed, then drop a .gitignore
        # that ignores .env + projects.json + configs.yaml + logs/.
        # This matches the "user authors the env file" rule: we never
        # touch .env itself, but we DO make sure future `git init` /
        # `git add .` runs won't accidentally commit it.
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _fail(f"cannot create {p.parent}: {exc}")
        atomic_write_text(
            gitignore,
            "# heddle local-only files. The user writes .env by hand;\n"
            "# heddle never touches it. configs.yaml + projects.json +\n"
            "# logs/ are also local state.\n"
            ".env\n"
            "configs.yaml\n"
            "projects.json\n"
            "logs/\n",
            encoding="utf-8",
        )
        _logging.info(
            component="env_loader",
            event="gitignore_created",
            msg=f"created {gitignore}",
            path=str(gitignore),
        )
    return gitignore


# ---------- diagnostic helpers (CLI, future work) ----------


def describe(path: Path | str | None = None) -> dict[str, object]:
    """Return a JSON-serializable snapshot of the .env state.

    Used by `heddle env status` (post-feat-050). Never includes the
    *values* of variables — only keys, file size, and mode — so the
    snapshot is safe to log or print.

    Note: deliberately does NOT call _check_secure_mode so a
    diagnostic `describe()` on a too-permissive file does not raise.
    The permission check only fires at load time.
    """
    p = _resolve(path)
    info: dict[str, object] = {
        "path": str(p),
        "exists": p.exists(),
    }
    if p.exists():
        st = p.stat()
        info["size"] = st.st_size
        info["mode"] = oct(st.st_mode & SECURE_MODE_MASK)
        info["secure"] = (st.st_mode & SECURE_MODE_BITS) == 0
    return info
