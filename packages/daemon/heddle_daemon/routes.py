# SPDX-License-Identifier: Apache-2.0
"""Message-routing handler for heddle daemon — feat-028.

This module implements the *business* side of the daemon's WS message
loop. The skeleton (feat-017 / ``server.py``) owns the socket, the
envelope parser, and the ``_handle_connection`` loop; this module adds
the per-envelope-type dispatchers that the Node.js supervisor (feat-027)
and the browser-facing HTTP routes (feat-028) actually call.

Envelope contract (T-010):

    Request:
        {"v": 1, "type": "<command>", "req_id": "<int or str>",
         "project_id": "<uuid>?",  # when the command targets one
         ...command-specific extras...}

    Response:
        {"v": 1, "type": "<command>_response", "req_id": <echo>,
         "ok": true, "data": {...}}           # success
        {"v": 1, "type": "<command>_response", "req_id": <echo>,
         "ok": false, "error": {"code": "...", "message": "..."}}  # failure

`req_id` is echoed back so the supervisor can match responses to
outstanding requests (the supervisor's ``request<TReq, TRes>`` method
in feat-028 builds on this).

Why a separate module from server.py:
    - Keeps the skeleton's responsibilities (bind, dispatch, lifecycle)
      uncluttered by command-handler growth. feat-030/feat-044/feat-045
      each add new handlers here without touching server.py.
    - Test seams: a test can instantiate ``RouteHandler()`` and exercise
      ``dispatch_envelope`` directly with synthetic envelopes, no socket
      required.

Scope of feat-028 (six commands):

    project_list          — list every registered project
    project_add           — register a new project (path validation)
    project_remove        — remove + cascade (uses project_cascade)
    feature_list          — list features for a given project
    feature_transition    — retry | abandon | mark-done (delegates to
                            feature_list_io's 5-state machine)
    dialog_turn           — v0.1 STUB; feat-044 replaces with intent
                            classification + draft-card generation

Anything more complex (LLM orchestration, draft confirm / validate,
supervisor event streaming, etc.) is a downstream feature.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Optional

from heddle_common import logging as _logging
from heddle_common.feature_list_io import (
    SchemaVersionError,
    fail as _fl_fail,
    load as _fl_load,
    mark_blocked as _fl_mark_blocked,
    mark_in_progress as _fl_mark_in_progress,
    mark_passing as _fl_mark_passing,
    save as _fl_save,
)
from heddle_common.projects_io import (
    ProjectsError,
    add_project,
    default_projects_path,
    list_projects,
)
from heddle_common.project_cascade import (
    OnRemoveHook,
    remove_project_with_cascade,
)

from .server import JsonEnvelope, build_envelope

__all__ = [
    "ALLOWED_TRANSITION_ACTIONS",
    "DEFAULT_FEATURE_LIST_PATH",
    "MAX_DIALOG_MESSAGE_CHARS",
    "RouteHandler",
    "RoutesError",
]


# ---------- constants ----------

# Action values accepted by ``feature_transition``. Maps directly to the
# heddle_common helper functions so the wire vocabulary stays narrow.
#
# - retry      → mark_in_progress (re-open a deferred/blocked feature OR
#               re-mark a previously-failed-in_progress feature as
#               in_progress so the daemon can re-run the agent step).
# - abandon    → mark_blocked with a fixed reason ("abandoned by user").
# - mark-done  → mark_passing (manual override; the feature is treated
#               as completed without an LLM run).
#
# The 5-state machine itself is enforced inside each helper; this list
# is the public surface of which action strings we accept on the wire.
ALLOWED_TRANSITION_ACTIONS: frozenset[str] = frozenset(
    {"retry", "abandon", "mark-done"}
)

# Reason text used for ``abandon``. Must be >= 5 chars per
# feature_list_io.BLOCK_REASON_MIN_CHARS so mark_blocked accepts it.
_ABANDON_REASON: str = "abandoned by user via dialog"

# Max length of a single user message. Matches the Web UI dialog max
# (feat-038 step 2). Excess messages are rejected with ``invalid_input``
# before any LLM call can be made.
MAX_DIALOG_MESSAGE_CHARS: int = 8000

# Where per-project ``feature_list.json`` lives. We follow HARNESS
# convention: the file is at the *root* of the user-selected project
# directory, NOT under ``.heddle/``. This keeps HARNESS CLI and
# the heddle daemon interoperable on the same project (DESIGN.md D-057).
DEFAULT_FEATURE_LIST_PATH: str = "feature_list.json"


# ---------- error type ----------


class RoutesError(Exception):
    """Raised for any user-facing routes-handler problem.

    Distinct from ``ProjectsError`` because routes' errors need to
    round-trip across the WS as a structured envelope with our ``code``
    taxonomy, not bubble up as a stack trace. ``feature_list_io.fail``
    raises ``SystemExit`` (not a normal exception), so each handler
    that uses it must wrap it with ``_safe_fail_call`` to convert
    the exit into a ``RoutesError``.
    """

    def __init__(self, message: str, *, code: str = "invalid_input") -> None:
        super().__init__(message)
        self.code = code


# ---------- public handler ----------


@dataclass
class RouteHandler:
    """Dispatches business envelopes to the right heddle_common helper.

    The handler holds no state of its own; every method is stateless
    except for the ``on_remove`` hook, which the daemon binds at
    startup so ``project_remove`` can cascade log cleanup + close
    the per-project LangGraph checkpoint store.

    Tests construct a default ``RouteHandler()`` and call
    ``dispatch_envelope`` directly with synthetic envelopes. The
    daemon (``Daemon.__init__``) wires one in via
    ``register_message_handler`` so ``server._handle_connection``
    routes every well-formed inbound envelope here.
    """

    # Optional async hook called BEFORE log-file deletion on
    # project_remove. Matches ``project_cascade.OnRemoveHook``;
    # daemon's ``Daemon._on_project_removed`` satisfies the
    # signature. None in tests (cascade still deletes logs).
    on_remove: Optional[OnRemoveHook] = None

    # Path to ``projects.json``. Defaults to ``~/.heddle/projects.json``;
    # tests pass a tmp path. Loose type to avoid importing pathlib
    # into the type signature for no reason.
    projects_path: Any = None

    # Per-project path resolver. Defaults to
    # ``<project.path>/feature_list.json``; tests inject a custom
    # resolver to redirect to a tmp dir.
    feature_list_path_for: Any = None

    def __post_init__(self) -> None:
        if self.projects_path is None:
            self.projects_path = default_projects_path()
        if self.feature_list_path_for is None:
            from pathlib import Path as _P

            def _default(project: Any) -> _P:
                return _P(project.path) / DEFAULT_FEATURE_LIST_PATH

            self.feature_list_path_for = _default

    # ----- public dispatch -----

    async def dispatch_envelope(
        self, env: JsonEnvelope
    ) -> Optional[JsonEnvelope]:
        """Dispatch one inbound envelope. Returns the response envelope or None.

        Returns ``None`` only when the envelope ``type`` is unhandled;
        in that case the caller (``server._handle_connection``) treats
        it as a no-op. Any command we DO recognize returns a response
        — either ``ok: true`` with the data, or ``ok: false`` with an
        error envelope. Per-feature handlers translate
        ``ProjectsError`` into stable error codes so the Node.js side
        can map them to HTTP statuses (400 for invalid_input, 404 for
        not_found, 409 for conflict).
        """
        req_id = env.extra.get("req_id")
        try:
            if env.type == "project_list":
                data = self._project_list()
                return self._ok_response(req_id, env.type, data)
            if env.type == "project_add":
                data = self._project_add(env.extra)
                return self._ok_response(req_id, env.type, data)
            if env.type == "project_remove":
                data = await self._project_remove(env.extra)
                return self._ok_response(req_id, env.type, data)
            if env.type == "feature_list":
                data = self._feature_list(env.extra)
                return self._ok_response(req_id, env.type, data)
            if env.type == "feature_transition":
                data = self._feature_transition(env.extra)
                return self._ok_response(req_id, env.type, data)
            if env.type == "dialog_turn":
                data = self._dialog_turn_stub(env.extra)
                return self._ok_response(req_id, env.type, data)
        except RoutesError as exc:
            return self._err_response(req_id, env.type, exc.code, str(exc))
        except ProjectsError as exc:
            return self._err_response(
                req_id, env.type, _projects_error_code(exc), str(exc)
            )
        except Exception as exc:
            _logging.error(
                component="daemon",
                event="routes_handler_internal_error",
                msg=f"unhandled exception in {env.type}: {exc}",
                envelope_type=env.type,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return self._err_response(
                req_id, env.type, "internal_error", "internal error"
            )

        # Unhandled envelope type — caller treats as no-op.
        return None

    # ----- per-command handlers -----

    def _project_list(self) -> dict[str, Any]:
        """List every registered project."""
        projects = list_projects(self.projects_path)
        return {"projects": [p.to_dict() for p in projects]}

    def _project_add(self, extras: dict[str, Any]) -> dict[str, Any]:
        """Register a new project. Payload: ``{path, name?}``."""
        path = extras.get("path")
        if not isinstance(path, str) or not path:
            raise RoutesError("path must be a non-empty string")
        name = extras.get("name")
        if name is not None and not isinstance(name, str):
            raise RoutesError("name must be a string when provided")
        project = add_project(
            self.projects_path,
            project_path=path,
            name=name,
        )
        return {"project": project.to_dict()}

    async def _project_remove(self, extras: dict[str, Any]) -> dict[str, Any]:
        """Remove a project by id (with log cleanup + daemon hook)."""
        project_id = extras.get("project_id") or extras.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise RoutesError("project_id must be a non-empty string")
        removed = await remove_project_with_cascade(
            self.projects_path,
            project_id,
            on_remove=self.on_remove,
        )
        if removed is None:
            raise RoutesError(
                f"project {project_id!r} not found", code="not_found"
            )
        return {"project": removed.to_dict()}

    def _feature_list(self, extras: dict[str, Any]) -> dict[str, Any]:
        """List features for a given project. Payload: ``{project_id}``."""
        project_id = extras.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise RoutesError("project_id must be a non-empty string")
        project = self._lookup_project(project_id)
        path = self.feature_list_path_for(project)
        data = _safe_fail_call(lambda: _fl_load(path))
        return {
            "project_id": project_id,
            "features": list(data.get("features", [])),
        }

    def _feature_transition(self, extras: dict[str, Any]) -> dict[str, Any]:
        """Drive the 5-state machine. Payload: ``{project_id, feature_id, action}``."""
        project_id = extras.get("project_id")
        feature_id = extras.get("feature_id")
        action = extras.get("action")
        if not isinstance(project_id, str) or not project_id:
            raise RoutesError("project_id must be a non-empty string")
        if not isinstance(feature_id, str) or not feature_id:
            raise RoutesError("feature_id must be a non-empty string")
        if (
            not isinstance(action, str)
            or action not in ALLOWED_TRANSITION_ACTIONS
        ):
            raise RoutesError(
                f"action must be one of {sorted(ALLOWED_TRANSITION_ACTIONS)}; "
                f"got {action!r}"
            )
        project = self._lookup_project(project_id)
        path = self.feature_list_path_for(project)
        # Each branch re-loads + re-saves around the helper. The
        # helpers handle the 5-state-machine transitions atomically
        # (load → mutate → save), so we do not need to wrap the whole
        # branch in a transaction. The error wrapper converts the
        # helper's ``fail()`` SystemExit into a RoutesError so the
        # response envelope can carry the right code.
        if action == "retry":
            _safe_fail_call(lambda: _fl_mark_in_progress(path, feature_id))
        elif action == "abandon":
            _safe_fail_call(
                lambda: _fl_mark_blocked(path, feature_id, reason=_ABANDON_REASON)
            )
        else:  # "mark-done"
            _safe_fail_call(lambda: _fl_mark_passing(path, feature_id))
        # Reload to return the post-transition row (the helpers above
        # don't return the mutated dict).
        post = _safe_fail_call(lambda: _fl_load(path))
        feature_row = _find_feature(post, feature_id)
        return {
            "project_id": project_id,
            "feature_id": feature_id,
            "action": action,
            "feature": feature_row,
        }

    def _dialog_turn_stub(self, extras: dict[str, Any]) -> dict[str, Any]:
        """v0.1 stub — replaced by feat-044 intent classification.

        Validates the message envelope and echoes it back as a chat
        response so the Web UI can render something while the real
        LLM-backed path is in development. The stub deliberately
        returns ``kind: "chat"`` (no draft cards) so feat-040's draft
        tray never lights up for stub replies.
        """
        project_id = extras.get("project_id")
        message = extras.get("message")
        if not isinstance(project_id, str) or not project_id:
            raise RoutesError("project_id must be a non-empty string")
        if not isinstance(message, str) or not message:
            raise RoutesError("message must be a non-empty string")
        if len(message) > MAX_DIALOG_MESSAGE_CHARS:
            raise RoutesError(
                f"message length {len(message)} exceeds max "
                f"{MAX_DIALOG_MESSAGE_CHARS}",
                code="invalid_input",
            )
        # Validate the project exists; we don't read feature_list here
        # (feat-044 needs the full feature-list context, and that's a
        # downstream feature). The lookup ensures a typo'd project_id
        # gets a 404 instead of a stub reply.
        self._lookup_project(project_id)
        return {
            "project_id": project_id,
            "kind": "chat",
            "text": f"echo: {message}",
        }

    # ----- internal helpers -----

    def _lookup_project(self, project_id: str) -> Any:
        """Return the Project for ``project_id`` or raise RoutesError(not_found)."""
        from heddle_common import load_projects

        projects = load_projects(self.projects_path)
        if project_id not in projects:
            raise RoutesError(
                f"project {project_id!r} not found", code="not_found"
            )
        return projects[project_id]

    def _ok_response(
        self,
        req_id: Any,
        command_type: str,
        data: dict[str, Any],
    ) -> JsonEnvelope:
        return build_envelope(
            f"{command_type}_response",
            req_id=req_id,
            ok=True,
            data=data,
        )

    def _err_response(
        self,
        req_id: Any,
        command_type: str,
        code: str,
        message: str,
    ) -> JsonEnvelope:
        return build_envelope(
            f"{command_type}_response",
            req_id=req_id,
            ok=False,
            error={"code": code, "message": message},
        )


# ---------- module-level helpers ----------


def _safe_fail_call(fn: Any) -> Any:
    """Run ``fn`` and convert ``feature_list_io.fail()``'s SystemExit
    into a ``RoutesError``.

    ``feature_list_io.fail()`` is the project's single chokepoint for
    user-facing errors: it prints ``"error: <msg>"`` to stderr and
    raises ``SystemExit(1)``. That contract is fine for the CLI (where
    the process IS the unit of failure) but is hostile to a daemon
    handler — a SystemExit tears the entire process down. We capture
    stderr around the call, recover the message, and re-raise as
    ``RoutesError`` so ``dispatch_envelope`` can build the right
    response envelope without polluting the structured stderr stream.
    """
    import io as _io

    buf = _io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = buf
    try:
        try:
            return fn()
        except SystemExit as exc:
            raw = buf.getvalue().strip()
            msg = raw[len("error: "):] if raw.startswith("error: ") else raw
            code = _classify_fail_message(msg)
            raise RoutesError(
                msg or f"feature_list_io exit {exc.code}", code=code
            ) from None
    finally:
        sys.stderr = real_stderr


def _classify_fail_message(msg: str) -> str:
    """Map a recovered ``fail()`` message to a wire code.

    Heuristic (the message text is the only signal we have): failures
    that name a missing or wrong-state feature land as ``not_found``
    or ``invalid_state``; anything else is ``invalid_input`` since the
    daemon cannot distinguish without parsing more of the helper
    internals. The wire code is what the Node.js supervisor maps to
    HTTP 400/404/409, so the worst case is a 400 instead of a 404 —
    a UX-only miss, not a correctness bug.
    """
    if "not found" in msg:
        return "not_found"
    if "from status" in msg or "expected" in msg:
        return "invalid_state"
    return "invalid_input"


def _projects_error_code(exc: ProjectsError) -> str:
    """Map a ProjectsError's textual prefix to a stable wire code."""
    msg = str(exc)
    if "already registered" in msg:
        return "conflict"
    if "does not exist or is not a directory" in msg:
        return "invalid_input"
    if "not found" in msg:
        return "not_found"
    return "invalid_input"


def _find_feature(data: dict[str, Any], feature_id: str) -> dict[str, Any]:
    """Locate a feature row by id. Returns the dict reference (not a copy).

    Mirrors ``feature_list_io._find_feature`` (which is private). The
    caller (``_feature_transition``) does not mutate the returned dict,
    so the privacy boundary holds.
    """
    for f in data.get("features", []):
        if isinstance(f, dict) and f.get("id") == feature_id:
            return f
    raise RoutesError(
        f"feature {feature_id!r} not found after transition",
        code="not_found",
    )


# ``json`` and ``sys`` imports above are used by future extensions
# (e.g. pretty-printing the abandoned-blocked reason in error messages).
# Suppress unused-import warnings during the feat-028 review window.
_ = (json, sys)