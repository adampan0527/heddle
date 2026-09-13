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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from heddle_common import logging as _logging
from heddle_common.configs_io import Config, default_configs_path
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

from .intent import classify_intent
from .llm import LLMConfigError
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

# feat-044: short friendly reply for messages classified as "chat".
# Surfaced in the Web UI dialog transcript; intentionally short so a
# chatty message doesn't bloat the transcript with a paragraph.
_CHAT_REPLY_TEXT: str = (
    "Got it. I'm here to help — what would you like to add or change?"
)

# feat-044 placeholder: a work-classified message gets this short
# text for v0.1 because feat-045 (LLM-driven decomposition) is not
# wired yet. The intent field in the response is what the Web UI
# inspects; the text is a courtesy so the dialog transcript is not
# silent. feat-045 will replace this with a draft-cards payload.
_WORK_PLACEHOLDER_TEXT: str = (
    "I'm classifying this as work — decomposition arrives in feat-045."
)


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


# Callback signature for unsolicited event emission (feat-030).
# The closure is invoked from a route handler to push an event
# envelope onto the active WS connection; it must NOT raise — a
# failed send should be logged, not propagated, because the handler
# is already on its way to building a response envelope.
EventEmitter = Optional["Any"]  # Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class RouteHandler:
    """Dispatches business envelopes to the right heddle_common helper.

    The handler holds no state of its own; every method is stateless
    except for the ``on_remove`` hook, which the daemon binds at
    startup so ``project_remove`` can cascade log cleanup + close
    the per-project LangGraph checkpoint store.

    feat-030 adds an optional ``event_emitter`` callable so
    per-feature commands (``start_feature``, ``stop_feature``,
    ``dialog_turn``) can push progress events onto the WS before
    returning their terminal response. The callable is wired by
    ``Daemon._handle_connection`` after constructing the handler;
    tests pass a stub that collects events into a list.

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

    # feat-030: optional async callable invoked by handlers to push
    # an unsolicited event envelope onto the WS. Receives a plain
    # dict (the event envelope body minus the ``type: "event"``
    # wrapper which the emitter adds). When None, ``emit_event``
    # is a no-op — keeps legacy tests that don't care about events
    # working byte-for-byte.
    event_emitter: EventEmitter = None

    # feat-031: path to the LLM-config registry (T-023 / feat-011).
    # ``None`` means "use the conventional location" (resolved lazily
    # in ``_resolve_configs_path``). Threaded in by ``Daemon.enable_routes``
    # from ``DaemonConfig.configs_path``; tests pass an explicit Path.
    configs_path: Any = None

    # feat-031: root directory for fake-LLM fixtures (feat-007).
    # When ``HEDDLE_FAKE_LLM=1``, ``_start_feature`` / ``_retry_feature``
    # look up ``<fixture_root>/<feature_id>.json`` and pass it to
    # ``build_llm_for_feature``. ``None`` means "use the default
    # location" (resolved lazily in ``_resolve_fixture_path``).
    fixture_root: Any = None

    # feat-031: per-attempt LLM registry. Keyed by ``attempt_id`` (the
    # UUID minted by ``_new_attempt_id``); value is
    # ``(feature_id, llm_callable)``. The actual ``run_agent_step``
    # invocation lands in a future session (feat-019 wiring); this
    # dict exists so the resolution + event emission path can be
    # tested today without driving a full agent loop. The dict is
    # best-effort cleanup-friendly: ``_drop_attempt_llm`` is exposed
    # for callers that want to release the LLM reference after the
    # agent step completes (the chat-model object holds no external
    # resources in v0.1, so forgetting to call it is benign).
    _attempt_llms: dict[str, tuple[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.projects_path is None:
            self.projects_path = default_projects_path()
        if self.feature_list_path_for is None:
            from pathlib import Path as _P

            def _default(project: Any) -> _P:
                return _P(project.path) / DEFAULT_FEATURE_LIST_PATH

            self.feature_list_path_for = _default

    # ----- event emission (feat-030) -----

    async def emit_event(
        self,
        event_name: str,
        project_id: str,
        feature_id: Optional[str],
        payload: dict[str, Any],
    ) -> None:
        """Push one unsolicited event envelope onto the WS.

        No-op when ``event_emitter`` is unset (tests). When set, the
        emitter is responsible for adding the ``v`` + ``type: "event"``
        envelope wrapper — we just hand it the body fields. Failures
        are swallowed because we're typically inside a handler that's
        about to return its terminal response envelope; an event-send
        failure must not turn a successful operation into an error.
        """
        if self.event_emitter is None:
            return
        body = {
            "event": event_name,
            "project_id": project_id,
            "feature_id": feature_id,
            "payload": payload,
        }
        try:
            await self.event_emitter(body)
        except Exception as exc:  # noqa: BLE001 — emit failures are best-effort
            _logging.warn(
                component="daemon",
                event="event_emit_failed",
                msg=f"event {event_name!r} emit failed: {exc}",
                event_name=event_name,
                project_id=project_id,
                feature_id=feature_id,
                error_type=type(exc).__name__,
            )

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
                data = await self._dialog_turn(env.extra)
                return self._ok_response(req_id, env.type, data)
            # feat-030: per-feature command handlers. Each one is a
            # wire-shape stub right now (validates input + emits a
            # progress event before the terminal response); feat-031
            # / feat-044 / feat-019 will fill in the actual LLM
            # orchestration. The point of feat-030 is the protocol
            # contract, not the runtime.
            if env.type == "start_feature":
                data = await self._start_feature(env.extra)
                return self._ok_response(req_id, env.type, data)
            if env.type == "stop_feature":
                data = await self._stop_feature(env.extra)
                return self._ok_response(req_id, env.type, data)
            if env.type == "retry_feature":
                data = await self._retry_feature(env.extra)
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

    async def _dialog_turn(self, extras: dict[str, Any]) -> dict[str, Any]:
        """feat-044: classify the user message as ``chat`` or ``work``.

        Behavior (D-017):

          * ``chat`` — return a short friendly text reply WITHOUT
            entering the draft-card flow. The Web UI renders the
            reply in the dialog transcript and the draft tray (feat-040)
            never lights up.
          * ``work`` — for v0.1 we return the same ``chat`` shape
            (no draft cards yet) so feat-040's tray stays dark.
            feat-045 will replace this branch with the LLM-driven
            decomposition that produces real draft cards.

        v0.1 classification is a heuristic (no LLM call); see
        ``heddle_daemon.intent`` for the full rule. The handler
        validates the message envelope + project_id, classifies,
        and returns ``{project_id, kind, text}`` regardless of
        intent — keeping the wire shape symmetric so feat-045 can
        extend the ``work`` branch without breaking the chat one.

        Async (not sync) because the handler emits a
        ``dialog_done`` event after constructing its reply —
        feat-030 wires the event_emitter so future streamed-token
        handlers can sit between the LLM call and the terminal
        response without a shape change.
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
        # (feat-045 needs the full feature-list context for LLM-driven
        # decomposition, and that's a downstream feature). The lookup
        # ensures a typo'd project_id gets a 404 instead of a friendly
        # reply for a project the user does not own.
        self._lookup_project(project_id)
        intent = classify_intent(message)
        if intent == "work":
            # feat-045 placeholder: the real LLM-driven decomposition
            # lands here. For v0.1 we still return kind="chat" so the
            # wire shape stays symmetric (feat-040's draft tray stays
            # dark until feat-045 wires draft cards).
            text = _WORK_PLACEHOLDER_TEXT
        else:
            text = _CHAT_REPLY_TEXT
        _logging.info(
            component="routes",
            event="dialog_intent_classified",
            msg=f"dialog_turn classified as {intent!r}",
            project_id=project_id,
            intent=intent,
        )
        # feat-030: emit a dialog_done marker so the Node.js side
        # sees the symmetric (token... done) shape even for the
        # heuristic path. A real LLM path will replace this with
        # per-token dialog_token events.
        await self.emit_event(
            "dialog_done",
            project_id=project_id,
            feature_id=None,
            payload={"full_text": text, "intent": intent},
        )
        return {
            "project_id": project_id,
            "kind": "chat",
            "intent": intent,
            "text": text,
        }

    # ----- feat-030 per-feature command handlers -----

    # ---- feat-031 helpers (shared by _start_feature / _retry_feature) ----

    def _resolve_configs_path(self) -> Path:
        """Return the configs registry path, falling back to the default."""
        if self.configs_path is None:
            return default_configs_path()
        p = Path(self.configs_path).expanduser()
        return p

    def _resolve_fixture_path(self, feature_id: str) -> Path | None:
        """Return ``<fixture_root>/<feature_id>.json`` or ``None``.

        ``None`` means "no fixture on disk for this feature" — the
        caller decides whether to fail (real-mode LLM, no fake fallback)
        or pass ``None`` to ``build_llm_for_feature`` (real-mode LLM,
        default fixtures directory). Under HEDDLE_FAKE_LLM=1 a missing
        fixture causes ``fake_llm_or_real`` to raise loudly, which is
        the desired "test bug, not silent fall-through" behaviour.
        """
        if self.fixture_root is None:
            root = Path("~/.heddle/fake_fixtures").expanduser()
        else:
            root = Path(self.fixture_root).expanduser()
        return root / f"{feature_id}.json"

    def _resolve_and_register_llm(
        self,
        *,
        feature_id: str,
        implementation_model: str | None,
        attempt_id: str,
    ) -> tuple[Config, str]:
        """Resolve ``implementation_model`` to a registered Config.

        Returns ``(config, source)`` and side-effects: registers the
        resulting LLM callable on ``self._attempt_llms`` keyed by
        ``attempt_id`` so a future feat-019 wiring can pick it up.

        Does NOT emit events or build the actual LLM — those are the
        caller's responsibility. The split lets ``_start_feature`` and
        ``_retry_feature`` share the resolution logic while keeping
        their event/response shapes distinct.
        """
        from heddle_daemon.agent_runtime import build_llm_for_feature
        from heddle_daemon.llm_config import resolve_feature_llm_config

        configs_path = self._resolve_configs_path()
        config, source = resolve_feature_llm_config(
            implementation_model,
            configs_path=configs_path,
        )
        fixture_path = self._resolve_fixture_path(feature_id)
        # When HEDDLE_FAKE_LLM is NOT set, ``fixture_path`` is ignored
        # by ``build_llm_for_feature``; passing the full path is fine.
        llm = build_llm_for_feature(
            config,
            fixture_path=fixture_path,
        )
        self._attempt_llms[attempt_id] = (feature_id, llm)
        return config, source

    def _drop_attempt_llm(self, attempt_id: str) -> None:
        """Release the LLM reference for ``attempt_id`` (best-effort)."""
        self._attempt_llms.pop(attempt_id, None)

    async def _start_feature(self, extras: dict[str, Any]) -> dict[str, Any]:
        """Wire-shape stub for feat-030, extended for feat-031.

        Validates the payload, marks the feature ``in_progress`` via
        the shared library, resolves the feature's
        ``implementation_model`` against the LLM-config registry, emits
        ``feature_attempt_started`` and ``llm_resolved`` events in that
        order, and returns the terminal response envelope.

        feat-031 wire shape changes (additive — feat-030 clients keep
        working):

          * New ``llm_resolved`` event between ``feature_attempt_started``
            and the response. Payload: ``{attempt_id, config_name,
            source}`` where ``source`` is ``"explicit"`` or
            ``"default"`` (see ``heddle_daemon.llm_config``).
          * On ``LLMConfigError`` (unknown config name, missing
            registry) the handler returns ``ok: false, error.code =
            "llm_config_error"`` and emits no ``llm_resolved``. The
            ``feature_attempt_started`` event IS still emitted so the
            browser can render "attempt failed during config resolution".
        """
        project_id = extras.get("project_id")
        feature_id = extras.get("feature_id")
        if not isinstance(project_id, str) or not project_id:
            raise RoutesError("project_id must be a non-empty string")
        if not isinstance(feature_id, str) or not feature_id:
            raise RoutesError("feature_id must be a non-empty string")
        project = self._lookup_project(project_id)
        path = self.feature_list_path_for(project)
        _safe_fail_call(lambda: _fl_mark_in_progress(path, feature_id))
        attempt_id = _new_attempt_id()
        await self.emit_event(
            "feature_attempt_started",
            project_id=project_id,
            feature_id=feature_id,
            payload={"attempt_id": attempt_id},
        )
        # feat-031: resolve implementation_model AFTER marking
        # in_progress so a missing config name still shows the
        # transition in feature_list.json (the user can see "yes the
        # daemon accepted the start, then refused to run on the chosen
        # model"). The LLMConfigError path below emits nothing
        # additional and rolls the response back to a structured error.
        # Reload the row to pick up the post-transition implementation_model
        # value — the caller may have just edited it via /api/features.
        post = _safe_fail_call(lambda: _fl_load(path))
        feature_row = _find_feature(post, feature_id)
        implementation_model = feature_row.get("implementation_model")
        try:
            config, source = self._resolve_and_register_llm(
                feature_id=feature_id,
                implementation_model=implementation_model,
                attempt_id=attempt_id,
            )
        except LLMConfigError as exc:
            # Best-effort cleanup of the attempt registry entry that
            # was never populated (defensive: register is the last
            # step, so the dict is untouched on this path).
            self._drop_attempt_llm(attempt_id)
            _logging.warn(
                component="routes",
                event="llm_config_error",
                msg=f"start_feature refused: {exc}",
                project_id=project_id,
                feature_id=feature_id,
                attempt_id=attempt_id,
            )
            # Raise so ``dispatch_envelope`` builds the structured
            # ``ok: false`` envelope with code ``llm_config_error``.
            # The ``feature_attempt_started`` event emitted above is
            # already on the wire (WS frames are buffered per
            # connection) so the browser can still render "attempt
            # started, then refused by config check".
            raise RoutesError(str(exc), code="llm_config_error") from exc
        await self.emit_event(
            "llm_resolved",
            project_id=project_id,
            feature_id=feature_id,
            payload={
                "attempt_id": attempt_id,
                "config_name": config.name,
                "source": source,
            },
        )
        return {
            "project_id": project_id,
            "feature_id": feature_id,
            "attempt_id": attempt_id,
            "status": "in_progress",
            "config_name": config.name,
            "config_source": source,
        }

    async def _stop_feature(self, extras: dict[str, Any]) -> dict[str, Any]:
        """Wire-shape stub for feat-030.

        Validates the payload and emits ``feature_stopped``. The
        actual signal to the running agent (via the daemon's
        ``_feature_stop_events`` map) lands in feat-031 once the
        runtime owns an asyncio.Task per attempt.
        """
        project_id = extras.get("project_id")
        feature_id = extras.get("feature_id")
        if not isinstance(project_id, str) or not project_id:
            raise RoutesError("project_id must be a non-empty string")
        if not isinstance(feature_id, str) or not feature_id:
            raise RoutesError("feature_id must be a non-empty string")
        attempt_id = extras.get("attempt_id") or _new_attempt_id()
        await self.emit_event(
            "feature_stopped",
            project_id=project_id,
            feature_id=feature_id,
            payload={"attempt_id": attempt_id},
        )
        return {
            "project_id": project_id,
            "feature_id": feature_id,
            "attempt_id": attempt_id,
            "status": "stop_requested",
        }

    async def _retry_feature(self, extras: dict[str, Any]) -> dict[str, Any]:
        """Wire-shape stub for feat-030, extended for feat-031.

        Equivalent to ``feature_transition retry`` but emits
        ``feature_attempt_started`` and ``llm_resolved`` so the
        browser-side progress UI can light up. Reuses
        ``_resolve_and_register_llm`` so the LLM-config error
        contract matches ``_start_feature``.
        """
        project_id = extras.get("project_id")
        feature_id = extras.get("feature_id")
        if not isinstance(project_id, str) or not project_id:
            raise RoutesError("project_id must be a non-empty string")
        if not isinstance(feature_id, str) or not feature_id:
            raise RoutesError("feature_id must be a non-empty string")
        project = self._lookup_project(project_id)
        path = self.feature_list_path_for(project)
        _safe_fail_call(lambda: _fl_mark_in_progress(path, feature_id))
        attempt_id = _new_attempt_id()
        await self.emit_event(
            "feature_attempt_started",
            project_id=project_id,
            feature_id=feature_id,
            payload={"attempt_id": attempt_id},
        )
        post = _safe_fail_call(lambda: _fl_load(path))
        feature_row = _find_feature(post, feature_id)
        implementation_model = feature_row.get("implementation_model")
        try:
            config, source = self._resolve_and_register_llm(
                feature_id=feature_id,
                implementation_model=implementation_model,
                attempt_id=attempt_id,
            )
        except LLMConfigError as exc:
            self._drop_attempt_llm(attempt_id)
            _logging.warn(
                component="routes",
                event="llm_config_error",
                msg=f"retry_feature refused: {exc}",
                project_id=project_id,
                feature_id=feature_id,
                attempt_id=attempt_id,
            )
            raise RoutesError(str(exc), code="llm_config_error") from exc
        await self.emit_event(
            "llm_resolved",
            project_id=project_id,
            feature_id=feature_id,
            payload={
                "attempt_id": attempt_id,
                "config_name": config.name,
                "source": source,
            },
        )
        return {
            "project_id": project_id,
            "feature_id": feature_id,
            "attempt_id": attempt_id,
            "action": "retry",
            "status": "in_progress",
            "config_name": config.name,
            "config_source": source,
        }

    def _err_response_for_command(
        self,
        *,
        project_id: str,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        """Return a partial ``ok: false`` dict for in-handler error returns.

        Reserved for future per-command error envelopes; current
        feat-031 handlers raise ``RoutesError`` and let
        ``dispatch_envelope`` build the structured envelope. Kept here
        so a future per-handler enrichment (e.g. attaching the
        attempt_id alongside the error code) has a single home.
        """
        return {
            "project_id": project_id,
            "ok": False,
            "error": {"code": code, "message": message},
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


def _new_attempt_id() -> str:
    """Return a fresh attempt id (UUID4 hex, 32 chars).

    feat-030 uses this on the daemon side to mint the
    ``attempt_id`` field that flows through every event and the
    terminal response envelope. The id is opaque to Node.js /
    browser — they just round-trip it back so the browser UI can
    group progress events with the originating command.

    ``uuid.uuid4().hex`` is stdlib-only (no extra dep) and is
    collision-safe for v0.1's single-process daemon. If we ever
    distribute the daemon across processes we'll switch to
    ``uuid.uuid7()`` for time-ordered ids, but that's a v0.2
    problem.
    """
    import uuid as _uuid

    return _uuid.uuid4().hex


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