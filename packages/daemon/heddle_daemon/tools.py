# SPDX-License-Identifier: Apache-2.0
"""Self-written LangChain tools — feat-020 (D-053).

Six tools implementing the LangChain ``BaseTool`` interface, one per
file-system / shell operation the agent runtime needs:

  * Read     — read a file, truncating at a configurable size
  * Write    — write content to a file
  * Edit     — string-replace edit (Claude Code semantics)
  * Bash     — run a subprocess command with a timeout
  * Glob     — pathlib glob patterns
  * Grep     — re patterns with optional file filter

Per D-053, these are written from scratch (no ``langchain_community``
tool imports). Each tool:

  * holds a ``project_root`` (the user-selected project directory;
    resolved at construction time so a symlink swap can't widen
    access mid-session);
  * resolves any caller-supplied path through ``_resolve_within_root``,
    which rejects path traversal escapes (``../foo``) and absolute
    paths outside the project;
  * returns a plain string (LangChain convention) with size /
    match-count annotations where useful;
  * raises ``ToolError`` (a stdlib ``Exception`` subclass defined here)
    on I/O / permission / timeout failures so the agent runtime can
    surface the failure back to the model.

The tools are stateless apart from ``project_root``. They are safe
to share across threads / tasks. The agent runtime (feat-019) wraps
each call in the sandbox middleware (feat-021), which can veto a
call before it reaches ``_run``.

Tool naming follows the convention used in Anthropic's Claude Code:
lower-case verb, no namespace. LLM providers see these names in the
``tool_choice`` payload.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any, ClassVar, Optional

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field


# ---------- exceptions ----------


class ToolError(Exception):
    """Raised by any tool when an operation fails.

    Wraps the underlying exception's message but never its traceback
    (the LLM shouldn't see Python frames). The agent runtime catches
    ``ToolError`` and feeds ``str(exc)`` back to the model as the
    tool result.
    """


class ToolPathError(ToolError):
    """Raised when a caller-supplied path escapes the project root.

    Per D-053, the tools must not read or write outside the
    user-selected project directory. ``ToolPathError`` is a separate
    subclass so the sandbox middleware (feat-021) can distinguish
    "I refused because this is outside the sandbox" from "I tried
    but the OS refused".
    """


# ---------- path-resolution helper ----------


def _resolve_within_root(project_root: Path, requested: str) -> Path:
    """Resolve ``requested`` against ``project_root`` and assert containment.

    Steps:
      1. Reject empty strings.
      2. ``Path(requested)`` (no leading slash unless absolute).
      3. Resolve relative paths against ``project_root`` and absolute
         paths verbatim.
      4. Verify the resolved path is ``project_root`` or a descendant.
      5. Return the resolved ``Path``.

    Symlinks are followed (``Path.resolve()`` walks them), so a
    malicious symlink inside the project pointing to ``/etc/passwd``
    is rejected at step 4. This matches D-053's intent.
    """
    if not isinstance(requested, str) or not requested:
        raise ToolPathError(f"path must be a non-empty string; got {requested!r}")
    raw = Path(requested)
    if not raw.is_absolute():
        raw = project_root / raw
    resolved = raw.resolve()
    # ``Path.is_relative_to`` is 3.9+; project requires 3.11.
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ToolPathError(
            f"path {resolved!s} escapes project root {project_root.resolve()!s}; "
            f"refused to operate outside the sandbox"
        ) from exc
    return resolved


# ---------- shared mixin ----------


class _ProjectScopedTool(BaseTool):
    """Base class for tools that operate within a project root.

    Holds ``project_root`` as a Pydantic-private attribute so it
    survives serialization but doesn't leak into the tool's public
    JSON schema (the LLM shouldn't see the absolute path).

    Subclasses override ``_run`` (and optionally ``_arun`` for
    true-async paths).
    """

    # Pydantic v2 private attributes are excluded from the schema.
    # ``project_root`` is set in __init__ and never serialized.
    project_root: Path = Field(exclude=True)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, project_root: Path | str, **kwargs: Any) -> None:
        super().__init__(project_root=Path(project_root).expanduser().resolve(), **kwargs)
        if not self.project_root.exists() or not self.project_root.is_dir():
            raise ToolError(
                f"project_root {self.project_root!r} does not exist or is not a directory"
            )


# ---------- Read ----------


class _ReadInput(BaseModel):
    path: str = Field(description="File path relative to the project root.")
    limit: Optional[int] = Field(
        default=None,
        gt=0,
        le=1_000_000,
        description="Maximum number of bytes to read. None reads the entire file.",
    )


class ReadTool(_ProjectScopedTool):
    """Read a file from the project directory."""

    name: ClassVar[str] = "read"
    description: ClassVar[str] = (
        "Read a file from the project. Returns the file content as a "
        "string, truncated at `limit` bytes if specified. Returns an "
        "error if the path is outside the project or the file does "
        "not exist."
    )
    args_schema: ClassVar[type[_ReadInput]] = _ReadInput

    def _run(self, path: str, limit: Optional[int] = None, **_: Any) -> str:
        resolved = _resolve_within_root(self.project_root, path)
        if not resolved.exists() or not resolved.is_file():
            raise ToolError(f"file not found: {path!r}")
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            raise ToolError(f"cannot read {path!r}: {exc}") from exc
        if limit is not None and len(data) > limit:
            truncated = data[:limit].decode("utf-8", errors="replace")
            return (
                f"{truncated}\n\n"
                f"... [truncated at {limit} bytes; total size {len(data)} bytes]"
            )
        return data.decode("utf-8", errors="replace")


# ---------- Write ----------


class _WriteInput(BaseModel):
    path: str = Field(description="File path relative to the project root.")
    content: str = Field(description="Full file content to write.")


class WriteTool(_ProjectScopedTool):
    """Write content to a file, creating parent directories if needed."""

    name: ClassVar[str] = "write"
    description: ClassVar[str] = (
        "Write content to a file at the given path. Overwrites any "
        "existing file. Creates parent directories as needed. Returns "
        "the number of bytes written, or an error if the path is "
        "outside the project or cannot be written."
    )
    args_schema: ClassVar[type[_WriteInput]] = _WriteInput

    def _run(self, path: str, content: str, **_: Any) -> str:
        resolved = _resolve_within_root(self.project_root, path)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"cannot write {path!r}: {exc}") from exc
        return f"wrote {len(content.encode('utf-8'))} bytes to {path}"


# ---------- Edit ----------


class _EditInput(BaseModel):
    path: str = Field(description="File path relative to the project root.")
    old_string: str = Field(description="The exact string to replace.")
    new_string: str = Field(description="The replacement string.")
    replace_all: bool = Field(
        default=False,
        description=(
            "If true, replace every occurrence. If false (default), "
            "the edit fails if `old_string` is not unique in the file."
        ),
    )


class EditTool(_ProjectScopedTool):
    """String-replace edit (matches Claude Code's Edit semantics)."""

    name: ClassVar[str] = "edit"
    description: ClassVar[str] = (
        "Replace `old_string` with `new_string` in a file. By default "
        "the edit fails if `old_string` is not unique — set "
        "`replace_all=true` to replace every occurrence. Returns the "
        "number of replacements made, or an error if the path is "
        "outside the project, the file does not exist, or the edit "
        "could not be applied."
    )
    args_schema: ClassVar[type[_EditInput]] = _EditInput

    def _run(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        **_: Any,
    ) -> str:
        resolved = _resolve_within_root(self.project_root, path)
        if not resolved.exists() or not resolved.is_file():
            raise ToolError(f"file not found: {path!r}")
        try:
            original = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"cannot read {path!r}: {exc}") from exc

        occurrences = original.count(old_string)
        if occurrences == 0:
            raise ToolError(
                f"old_string not found in {path!r}; verify exact whitespace and content"
            )
        if occurrences > 1 and not replace_all:
            raise ToolError(
                f"old_string matches {occurrences} places in {path!r}; "
                f"either narrow the match or pass replace_all=true"
            )

        if replace_all:
            new_content = original.replace(old_string, new_string)
            replacements = occurrences
        else:
            new_content = original.replace(old_string, new_string, 1)
            replacements = 1

        try:
            resolved.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"cannot write {path!r}: {exc}") from exc
        return f"replaced {replacements} occurrence(s) in {path}"


# ---------- Bash ----------


class _BashInput(BaseModel):
    command: str = Field(description="Shell command to execute.")
    timeout_seconds: int = Field(
        default=30,
        gt=0,
        le=600,
        description="Maximum execution time in seconds. Default 30, max 600.",
    )


class BashTool(_ProjectScopedTool):
    """Run a shell command in the project directory with a timeout."""

    name: ClassVar[str] = "bash"
    description: ClassVar[str] = (
        "Execute a shell command in the project directory. Returns "
        "(stdout, stderr, exit_code). Times out after `timeout_seconds` "
        "(default 30, max 600). This tool runs with the daemon's own "
        "permissions — prefer the more specific Read/Write/Edit/Glob/Grep "
        "tools when possible."
    )
    args_schema: ClassVar[type[_BashInput]] = _BashInput

    async def _arun(
        self,
        command: str,
        timeout_seconds: int = 30,
        **_: Any,
    ) -> str:
        # Async path: use asyncio subprocess directly so the event loop
        # is not blocked while the child runs. The default _arun in
        # BaseTool would push _run through a thread executor; we'd
        # rather keep one consistent async pipeline.
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.project_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ToolError(f"cannot start shell: {exc}") from exc
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise ToolError(
                f"command timed out after {timeout_seconds}s: {command!r}"
            ) from exc
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else -1
        # Format the result the way Claude Code does: stderr last,
        # exit code trailing. Cap output sizes to avoid context blow-up.
        max_chunk = 30_000
        if len(stdout) > max_chunk:
            stdout = stdout[:max_chunk] + f"\n...[truncated at {max_chunk} chars]"
        if len(stderr) > max_chunk:
            stderr = stderr[:max_chunk] + f"\n...[truncated at {max_chunk} chars]"
        return f"exit_code: {exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}"

    def _run(self, command: str, timeout_seconds: int = 30, **_: Any) -> str:
        # Sync fallback (rarely used; the runtime prefers _arun).
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.project_root),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(
                f"command timed out after {timeout_seconds}s: {command!r}"
            ) from exc
        except OSError as exc:
            raise ToolError(f"cannot run shell: {exc}") from exc
        return (
            f"exit_code: {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


# ---------- Glob ----------


class _GlobInput(BaseModel):
    pattern: str = Field(
        description=(
            "Glob pattern (Python `pathlib` semantics). Examples: "
            "'**/*.py', 'src/**/*.ts', 'README*'."
        ),
    )


class GlobTool(_ProjectScopedTool):
    """Return paths matching a glob pattern under the project root."""

    name: ClassVar[str] = "glob"
    description: ClassVar[str] = (
        "List files matching a glob pattern relative to the project "
        "root. Returns a newline-separated list of POSIX-style paths "
        "(e.g. 'packages/cli/src/main.ts'), one per match, capped at "
        "1000 results. Returns an empty string when no matches."
    )
    args_schema: ClassVar[type[_GlobInput]] = _GlobInput

    MAX_MATCHES: ClassVar[int] = 1000

    def _run(self, pattern: str, **_: Any) -> str:
        # pathlib doesn't have a "match-and-validate-stays-in-root"
        # primitive; we walk the project_root and filter manually so
        # a pattern that resolves outside the root is impossible.
        # Use Path.glob which respects the pattern literally.
        matches: list[str] = []
        try:
            for p in self.project_root.glob(pattern):
                # Defense in depth: even though we globbed against
                # project_root, a pattern like '../*' could escape in
                # theory. Re-resolve and verify containment.
                try:
                    rel = p.resolve().relative_to(self.project_root.resolve())
                except ValueError:
                    continue
                matches.append(rel.as_posix())
                if len(matches) >= self.MAX_MATCHES:
                    break
        except (OSError, ValueError) as exc:
            raise ToolError(f"glob failed: {exc}") from exc
        matches.sort()
        if len(matches) >= self.MAX_MATCHES:
            matches.append(f"...[capped at {self.MAX_MATCHES} matches]")
        return "\n".join(matches)


# ---------- Grep ----------


class _GrepInput(BaseModel):
    pattern: str = Field(description="Regular expression to search for.")
    path: Optional[str] = Field(
        default=None,
        description=(
            "Directory or file to search. Defaults to the project root. "
            "Must be within the project."
        ),
    )
    file_filter: Optional[str] = Field(
        default=None,
        description=(
            "If set, only files whose name matches this glob are "
            "searched (e.g. '*.py')."
        ),
    )
    max_matches: int = Field(
        default=200,
        gt=0,
        le=10_000,
        description="Maximum number of matches to return. Default 200.",
    )


class GrepTool(_ProjectScopedTool):
    """Search files for a regular expression."""

    name: ClassVar[str] = "grep"
    description: ClassVar[str] = (
        "Search for a regular expression in files under the project. "
        "Returns matching lines in 'path:line:content' format, one per "
        "line. Caps output at `max_matches` (default 200). Skips "
        "binary files."
    )
    args_schema: ClassVar[type[_GrepInput]] = _GrepInput

    # Directories we never descend into, regardless of pattern. These
    # are universal noise (.git history, build artefacts, vendored
    # deps); the LLM doesn't need them and they balloon the output.
    SKIP_DIRS: ClassVar[frozenset[str]] = frozenset({
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".heddle",  # the daemon's own state
        "venv",
        ".venv",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    })

    def _run(
        self,
        pattern: str,
        path: Optional[str] = None,
        file_filter: Optional[str] = None,
        max_matches: int = 200,
        **_: Any,
    ) -> str:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regex {pattern!r}: {exc}") from exc

        root = self.project_root
        if path is not None:
            root = _resolve_within_root(self.project_root, path)
            if root.is_file():
                files = [root]
            elif root.is_dir():
                files = list(self._walk(root))
            else:
                raise ToolError(f"path {path!r} is not a file or directory")
        else:
            files = list(self._walk(self.project_root))

        if file_filter is not None:
            files = [f for f in files if fnmatch.fnmatch(f.name, file_filter)]

        out_lines: list[str] = []
        binary_skip_note = 0
        for f in files:
            try:
                # Sniff the first 8192 bytes; if there's a NUL byte,
                # treat the file as binary and skip.
                with f.open("rb") as fh:
                    sample = fh.read(8192)
                if b"\x00" in sample:
                    binary_skip_note += 1
                    continue
                text = sample + f.read_bytes()[len(sample):]
                # decode for line iteration
                text = text.decode("utf-8", errors="replace")
            except OSError:
                continue
            try:
                rel = f.resolve().relative_to(self.project_root.resolve())
            except ValueError:
                continue
            rel_str = rel.as_posix()
            for line_no, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    out_lines.append(f"{rel_str}:{line_no}:{line}")
                    if len(out_lines) >= max_matches:
                        out_lines.append(
                            f"...[capped at {max_matches} matches; "
                            f"{binary_skip_note} binary file(s) skipped]"
                        )
                        return "\n".join(out_lines)
        if binary_skip_note and not out_lines:
            out_lines.append(
                f"[no matches; {binary_skip_note} binary file(s) skipped]"
            )
        return "\n".join(out_lines)

    def _walk(self, root: Path):
        """Yield files under ``root``, skipping ``SKIP_DIRS``."""
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            # If any ancestor directory is in SKIP_DIRS, skip the file.
            skip = False
            for ancestor in p.relative_to(root).parents:
                if ancestor.name in self.SKIP_DIRS:
                    skip = True
                    break
            if skip:
                continue
            yield p


# ---------- factory ----------


def all_tools(project_root: Path | str) -> list[_ProjectScopedTool]:
    """Return one instance of each tool, scoped to ``project_root``.

    The agent runtime (feat-019) imports this factory and hands the
    resulting list to LangGraph's ``create_react_agent`` (or
    equivalent) after the sandbox middleware has wrapped each tool.
    """
    root = Path(project_root)
    return [
        ReadTool(project_root=root),
        WriteTool(project_root=root),
        EditTool(project_root=root),
        BashTool(project_root=root),
        GlobTool(project_root=root),
        GrepTool(project_root=root),
    ]


def tool_names() -> list[str]:
    """Return the canonical names of the six tools (for the LLM tool spec)."""
    return ["read", "write", "edit", "bash", "glob", "grep"]


__all__ = [
    "BashTool",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "ReadTool",
    "ToolError",
    "ToolPathError",
    "WriteTool",
    "all_tools",
    "tool_names",
]
