# SPDX-License-Identifier: Apache-2.0
"""fake_llm — scripted-response LLM used when HEDDLE_FAKE_LLM is set.

Per feat-007 / TECH.md T-018 / T-031. CI and most local tests run with
`HEDDLE_FAKE_LLM=1` so that:

  - no network call leaves the host,
  - no provider API key is required,
  - every LLM turn produces a deterministic, scripted response pulled
    from a fixture file.

This module lives in `heddle_common/` (not `heddle_daemon/`) so tests in
any package can import it without pulling the daemon's full dependency
tree (langgraph, langchain-core, websockets, …).

The intended call site is `agent_runtime.py` (feat-019), which will:

    llm = fake_llm_or_real(
        real_factory=lambda: build_chat_model(config),
        fixture_path=project_path / "tests" / "fixtures" / "<feature>.json",
    )

The `fake_llm_or_real` helper (also in this module) is the single chokepoint
that decides based on `os.environ["HEDDLE_FAKE_LLM"]` whether to return a
`FakeLLM` (loaded from the fixture file) or the real provider-backed chat
model. Keeping the switch in one helper means feat-019 has no branching
of its own — it just calls `fake_llm_or_real(...)`.

The fixture format is documented in `_EXPECTED_FIXED_KEYS` and validated by
`load_fixture`. See `tests/test_fake_llm.py` for the end-to-end picture.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

__all__ = [
    "DEFAULT_FIXTURE_DIR",
    "FAKE_LLM_ENV_VAR",
    "FakeLLM",
    "ScriptedToolCall",
    "ScriptedResponse",
    "fake_llm_or_real",
    "is_fake_llm_enabled",
    "load_fixture",
]

# ---------- constants ----------

# Single source of truth for the env var name. feat-019 / feat-023 / feat-052
# all reference this constant rather than the string literal.
FAKE_LLM_ENV_VAR: str = "HEDDLE_FAKE_LLM"

# Truthy values for FAKE_LLM_ENV_VAR. Anything else (missing, "0", "false",
# "no", "") is treated as disabled. This matches the convention used by
# HEDDLE_FAKE_LLM in CI yaml (T-031: `HEDDLE_FAKE_LLM=1 pytest …`).
_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})

# Default location for sample fixtures shipped with the daemon package.
# feat-017+ will reference this when wiring per-project overrides.
DEFAULT_FIXTURE_DIR: str = "tests/fixtures"

# Hard caps on fixture size, defensive against accidental "load the world"
# mistakes (e.g. a CI matrix that points a fixture path at a checkout dir).
MAX_FIXTURE_BYTES: int = 1_048_576  # 1 MiB
MAX_FIXTURE_RESPONSES: int = 10_000


# ---------- fixture schema ----------

@dataclass(frozen=True)
class ScriptedToolCall:
    """A single scripted tool call within a scripted response.

    `name` matches the LangChain `BaseTool.name` of the six self-written
    tools (D-053): Read / Write / Edit / Bash / Glob / Grep. `args` is a
    JSON-serializable dict that the agent runtime passes verbatim to the
    tool's `_run()`.
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScriptedResponse:
    """One scripted LLM turn.

    Either `content` carries the assistant's text reply, `tool_calls`
    carries the structured tool-use decision, or both (LangChain allows
    text + tool calls in the same response). `stop_reason` mirrors the
    LangChain field so the agent runtime's loop terminator ("LLM
    signaled done" per D-053) keeps working under fake mode.
    """

    content: str = ""
    tool_calls: tuple[ScriptedToolCall, ...] = ()
    stop_reason: str = "end_turn"  # "end_turn" | "tool_use" | "max_tokens"


# Top-level keys the fixture JSON is allowed to carry. Anything else is a
# typo that should fail loud at load time, not silently disappear. Keep
# this list tight so future contributors don't quietly grow the schema.
_EXPECTED_FIXED_KEYS: frozenset[str] = frozenset({"schema_version", "responses"})
_ALLOWED_RESPONSE_KEYS: frozenset[str] = frozenset({"content", "tool_calls", "stop_reason"})


# ---------- env-var helper ----------

def is_fake_llm_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return True iff the `HEDDLE_FAKE_LLM` env var is set to a truthy value.

    `env` defaults to `os.environ`; tests pass an explicit mapping to
    avoid touching real process state.
    """
    src = env if env is not None else os.environ
    raw = src.get(FAKE_LLM_ENV_VAR)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY_ENV_VALUES


# ---------- loader ----------

def load_fixture(path: Path | str) -> list[ScriptedResponse]:
    """Read and parse a fixture JSON file.

    Fixture schema:

        {
          "schema_version": 1,
          "responses": [
            {"content": "Hello.", "tool_calls": [], "stop_reason": "end_turn"},
            {"content": "", "tool_calls": [{"name": "Read", "args": {"path": "x"}}], "stop_reason": "tool_use"},
            {"content": "Done.", "tool_calls": [], "stop_reason": "end_turn"}
          ]
        }

    Validation rules:

      - File size <= MAX_FIXTURE_BYTES; len(responses) <= MAX_FIXTURE_RESPONSES.
      - Top-level keys are exactly the allowed set (forward-compat: a new
        fixed key needs a version bump, not silent ignore).
      - Each response entry has only `content` / `tool_calls` / `stop_reason`.
      - `tool_calls[i].name` is a non-empty string; `args` must be a dict.
      - `stop_reason` ∈ {"end_turn", "tool_use", "max_tokens"}.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        fail(f"cannot stat fixture {p}: {exc}")
    if size > MAX_FIXTURE_BYTES:
        fail(
            f"fixture {p} is {size} bytes; max is {MAX_FIXTURE_BYTES}. "
            "Check the path — large fixtures usually mean a wrong file."
        )
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read fixture {p}: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"fixture {p} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        fail(f"fixture {p} root must be an object; got {type(data).__name__}")
    extra = set(data.keys()) - _EXPECTED_FIXED_KEYS
    if extra:
        fail(
            f"fixture {p} has unknown top-level keys: {sorted(extra)}. "
            f"Allowed: {sorted(_EXPECTED_FIXED_KEYS)}."
        )
    if "responses" not in data:
        fail(f"fixture {p} missing required 'responses' array")
    responses = data["responses"]
    if not isinstance(responses, list):
        fail(f"fixture {p} 'responses' must be a list; got {type(responses).__name__}")
    if len(responses) == 0:
        fail(
            f"fixture {p} has an empty 'responses' array; "
            "a fixture with zero turns is useless — add at least one."
        )
    if len(responses) > MAX_FIXTURE_RESPONSES:
        fail(
            f"fixture {p} has {len(responses)} responses; max is "
            f"{MAX_FIXTURE_RESPONSES}"
        )
    parsed: list[ScriptedResponse] = []
    for i, entry in enumerate(responses):
        parsed.append(_parse_response(p, i, entry))
    return parsed


def _parse_response(path: Path, idx: int, entry: Any) -> ScriptedResponse:
    if not isinstance(entry, dict):
        fail(
            f"fixture {path} responses[{idx}] must be an object; "
            f"got {type(entry).__name__}"
        )
    extra = set(entry.keys()) - _ALLOWED_RESPONSE_KEYS
    if extra:
        fail(
            f"fixture {path} responses[{idx}] has unknown keys: "
            f"{sorted(extra)}. Allowed: {sorted(_ALLOWED_RESPONSE_KEYS)}."
        )
    content = entry.get("content", "")
    if not isinstance(content, str):
        fail(
            f"fixture {path} responses[{idx}].content must be a string; "
            f"got {type(content).__name__}"
        )
    stop_reason = entry.get("stop_reason", "end_turn")
    if stop_reason not in ("end_turn", "tool_use", "max_tokens"):
        fail(
            f"fixture {path} responses[{idx}].stop_reason must be one of "
            f"end_turn / tool_use / max_tokens; got {stop_reason!r}"
        )
    tool_calls_raw = entry.get("tool_calls", [])
    if not isinstance(tool_calls_raw, list):
        fail(
            f"fixture {path} responses[{idx}].tool_calls must be a list; "
            f"got {type(tool_calls_raw).__name__}"
        )
    tool_calls: list[ScriptedToolCall] = []
    for j, tc in enumerate(tool_calls_raw):
        if not isinstance(tc, dict):
            fail(
                f"fixture {path} responses[{idx}].tool_calls[{j}] must be "
                f"an object; got {type(tc).__name__}"
            )
        name = tc.get("name", "")
        if not isinstance(name, str) or not name:
            fail(
                f"fixture {path} responses[{idx}].tool_calls[{j}].name "
                f"must be a non-empty string; got {name!r}"
            )
        args = tc.get("args", {})
        if not isinstance(args, dict):
            fail(
                f"fixture {path} responses[{idx}].tool_calls[{j}].args "
                f"must be an object; got {type(args).__name__}"
            )
        tool_calls.append(ScriptedToolCall(name=name, args=args))
    return ScriptedResponse(
        content=content,
        tool_calls=tuple(tool_calls),
        stop_reason=stop_reason,
    )


# ---------- FakeLLM ----------

class FakeLLM:
    """A scripted-response stand-in for a LangChain chat model.

    Constructed from a `Sequence[ScriptedResponse]` (typically the output
    of `load_fixture`). Each call to `invoke(messages)` returns the next
    scripted response, wrapping it in a `_FakeAIMessage` that mimics the
    subset of the LangChain `AIMessage` interface the agent runtime
    (feat-019) actually uses: `.content`, `.tool_calls`, and the
    response metadata.

    Two consumption modes:

      - Sequential (default): pop responses in order. When the list runs
        out, raise `FakeLLMExhausted` — loud failure beats silent
        infinite-loop.
      - Looping (explicit): re-cycle from the start. Useful for tests
        that exercise many turns of the same fixture without growing
        the fixture file.

    The class intentionally does NOT subclass LangChain's BaseChatModel —
    that pulls in `langchain-core` (a daemon-side dep) into heddle_common.
    feat-019's `agent_runtime.py` will use `isinstance(x, FakeLLM)` to
    detect fake mode and call `.invoke()` directly, falling through to the
    real BaseChatModel path otherwise.
    """

    def __init__(
        self,
        responses: Sequence[ScriptedResponse],
        *,
        loop: bool = False,
    ) -> None:
        if not responses:
            fail("FakeLLM requires at least one scripted response")
        self._responses: tuple[ScriptedResponse, ...] = tuple(responses)
        self._loop: bool = loop
        self._index: int = 0

    @property
    def remaining(self) -> int:
        """Number of scripted responses left before exhaustion (or infinity if looping)."""
        if self._loop:
            return -1
        return max(0, len(self._responses) - self._index)

    def invoke(self, messages: Sequence[Any]) -> _FakeAIMessage:
        """Return the next scripted response wrapped as an AI message.

        `messages` is accepted (and ignored) for interface compatibility
        with LangChain's `BaseChatModel.invoke`. The agent runtime will
        pass the conversation history; we don't key off it for now —
        sequential pop is the v0.1 semantics. A future feat may key off
        message content to allow branching scenarios per step.
        """
        if self._index >= len(self._responses):
            if not self._loop:
                raise FakeLLMExhausted(
                    f"FakeLLM exhausted after {len(self._responses)} responses; "
                    "either extend the fixture or pass loop=True at construction"
                )
            self._index = 0
        resp = self._responses[self._index]
        self._index += 1
        return _FakeAIMessage(
            content=resp.content,
            tool_calls=[
                {"name": tc.name, "args": tc.args, "id": f"call_{self._index}_{j}"}
                for j, tc in enumerate(resp.tool_calls)
            ],
            response_metadata={"stop_reason": resp.stop_reason},
        )

    def __iter__(self) -> Iterator[ScriptedResponse]:
        """Allow tests to iterate the scripted sequence directly without invoking."""
        return iter(self._responses)


class FakeLLMExhausted(Exception):
    """Raised when a non-looping FakeLLM runs out of scripted responses."""


@dataclass(frozen=True)
class _FakeAIMessage:
    """Subset of LangChain's `AIMessage` used by the agent runtime.

    Only the attributes that feat-019's `agent_step` loop actually reads
    are present. Adding more fields here is fine; removing fields is a
    breaking change for any feat that depends on this one.
    """

    content: str
    tool_calls: list[dict[str, Any]]
    response_metadata: dict[str, Any]


# ---------- the chokepoint helper ----------

def fake_llm_or_real(
    *,
    real_factory: Any,
    fixture_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Return either a FakeLLM (loaded from `fixture_path`) or `real_factory()`.

    Decision rule (single chokepoint):

      - if `HEDDLE_FAKE_LLM` is truthy AND `fixture_path` is provided,
        return `FakeLLM(load_fixture(fixture_path))`.
      - if `HEDDLE_FAKE_LLM` is truthy AND `fixture_path` is None,
        fail loudly — running fake mode without a fixture is a test bug,
        not a graceful degradation.
      - otherwise, call `real_factory()` and return whatever it produces.

    `real_factory` is invoked lazily (only when needed) so importing this
    module does not require the real provider SDK to be installed when
    fake mode is the only mode exercised.
    """
    if is_fake_llm_enabled(env):
        if fixture_path is None:
            fail(
                f"{FAKE_LLM_ENV_VAR}=1 but no fixture_path was provided; "
                "refusing to silently fall through to real LLM calls"
            )
        return FakeLLM(load_fixture(fixture_path))
    return real_factory()


# ---------- IO error helper ----------

def fail(msg: str) -> None:
    """Print an error to stderr and exit with code 1.

    Mirrors `heddle_common.feature_list_io.fail` so this module can be
    used standalone without importing the larger library.
    """
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)
