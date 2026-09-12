// SPDX-License-Identifier: Apache-2.0
/**
 * Daemon event payload shapes — feat-030.
 *
 * One file, one job: declare every event the daemon can push onto the
 * Node.js supervisor's loopback WS. Each variant is a separate
 * interface; `DaemonEvent` is the discriminated union the
 * `daemon-event` EventEmitter listener narrows on via the `event`
 * literal.
 *
 * Wire shape (matches the Python side's `build_envelope("event", ...)`):
 *
 *     {v: 1, type: "event", event: "<event_name>",
 *      project_id: "<id>", feature_id?: "<id>" | null,
 *      payload: { ... per-event fields ... }}
 *
 * The `event` field is the discriminant. Adding a new event is a
 * non-breaking change: existing listeners `switch` on `event` with a
 * `default:` arm, so unknown events land in a warn log instead of
 * crashing the supervisor.
 *
 * v0.1 event set (the four driving events + a few that downstream
 * features will subscribe to once they land):
 *
 *   feature_attempt_started    — emitted by start_feature before its
 *                                response; payload: {attempt_id}
 *   feature_progress           — streamed per agent step; payload:
 *                                {step, message, attempt_id}
 *   feature_done               — terminal success; payload:
 *                                {attempt_id, feature: FeatureRow}
 *   feature_failed             — terminal failure; payload:
 *                                {attempt_id, error_code, error_message}
 *   feature_stopped            — emitted by stop_feature; payload:
 *                                {attempt_id}
 *   log_line                   — daemon-side structured log forwarded
 *                                verbatim; payload: {level, event, msg,
 *                                ...redaction-passed fields}
 *   dialog_token               — streamed LLM token from dialog_turn;
 *                                payload: {token}
 *   dialog_done                — terminal marker for dialog_turn's
 *                                stream; payload: {full_text}
 */

// Local alias so the protocol module does not have to know the full
// HTTP route schema. We only need a `FeatureRow` shape for the
// `feature_done` payload; using a type-only import keeps the wire
// contract independent of the route's TypeBox schema.
type FeatureRow = Record<string, unknown>;

/** Attempt id is a daemon-side UUID; opaque to Node.js. */
export type AttemptId = string;

/** Per-event payload interfaces. Keep one per event — narrow on `event`. */

export interface FeatureAttemptStartedPayload {
  attempt_id: AttemptId;
}

export interface FeatureProgressPayload {
  attempt_id: AttemptId;
  step: number;
  message: string;
}

export interface FeatureDonePayload {
  attempt_id: AttemptId;
  feature: FeatureRow;
}

export interface FeatureFailedPayload {
  attempt_id: AttemptId;
  error_code: string;
  error_message: string;
}

export interface FeatureStoppedPayload {
  attempt_id: AttemptId;
}

export interface LogLinePayload {
  level: "debug" | "info" | "warn" | "error";
  event: string;
  msg: string;
  feature_id?: string | null;
  // Anything else the daemon wants to attach. Logger forwards verbatim.
  [extra: string]: unknown;
}

export interface DialogTokenPayload {
  token: string;
}

export interface DialogDonePayload {
  full_text: string;
}

/**
 * The full discriminated union. Each variant's `event` literal is the
 * discriminant; TypeScript narrows the payload shape automatically when
 * a listener switches on `event`.
 */
export type DaemonEvent =
  | { event: "feature_attempt_started"; payload: FeatureAttemptStartedPayload }
  | { event: "feature_progress"; payload: FeatureProgressPayload }
  | { event: "feature_done"; payload: FeatureDonePayload }
  | { event: "feature_failed"; payload: FeatureFailedPayload }
  | { event: "feature_stopped"; payload: FeatureStoppedPayload }
  | { event: "log_line"; payload: LogLinePayload }
  | { event: "dialog_token"; payload: DialogTokenPayload }
  | { event: "dialog_done"; payload: DialogDonePayload };

export type DaemonEventName = DaemonEvent["event"];

/**
 * Exhaustive check helper — call from the `default:` arm of a switch.
 * If a future event lands in the union but a listener forgets to handle
 * it, this triggers a compile error at the listener site.
 */
export function assertNeverDaemonEvent(x: never): never {
  throw new Error(`unhandled daemon event: ${JSON.stringify(x)}`);
}

/**
 * The thin payload the supervisor's `"daemon-event"` EventEmitter
 * carries. We don't expose the full `DaemonEventEnvelope` directly
 * because callers only need the parsed fields; the raw envelope
 * structure stays in `types.ts`.
 *
 * `project_id` and `feature_id` are hoisted out of the envelope so
 * listeners don't have to re-read them per event. `payload` is
 * typed as `Record<string, unknown>` on the wire layer (the
 * supervisor cannot know the runtime variant without per-event
 * validation); each listener does a `switch (record.event)` and
 * narrows `record.payload` against the matching
 * `DaemonEvent` variant via the union's discriminant.
 */
export interface DaemonEventRecord {
  event: DaemonEventName;
  project_id: string;
  feature_id: string | null;
  payload: Record<string, unknown>;
  /** Wall-clock arrival time on the Node.js side (ms since epoch). */
  received_at: number;
}

// ---------------------------------------------------------------------------
// Browser-side command envelopes — feat-029 (Browser↔Node.js WS event stream).
//
// The browser sends these envelopes on the `/ws` connection. The
// `BrowserWsBridge` forwards `dialog_turn` to the daemon via
// `supervisor.request()` (awaited) and the other three via
// `supervisor.sendCommand()` (fire-and-forget; matching response
// envelope, if any, is routed back via req_id). All envelopes carry a
// `project_id` so the bridge can record which project the browser
// session is scoped to and use that as the fan-out filter for inbound
// daemon events.
//
// Per-command payloads:
//   dialog_turn    — {message: string}         (Chat/work — feat-044 will branch on this)
//   start_feature  — {feature_id: string}      (Drag-to-start execution model)
//   stop_feature   — {feature_id: string}      (User-initiated abort)
//   retry_feature  — {feature_id, hint?: string} (D-033: retry-with-hint optional)
//
// Wire shape (browser → server, mirror of types.ts:DaemonCommandEnvelope):
//
//     {v: 1, type: "<command>", project_id: "<id>", ...payload}
//
// We do NOT require the browser to send `req_id` — `BrowserWsBridge`
// allocates one when forwarding to the daemon and threads the reply
// back to the right client via an internal pending-request map keyed
// by the daemon-allocated `req_id`. This keeps the browser's API
// surface narrow (no per-call id bookkeeping) while preserving the
// request/response semantics of feat-028 on the daemon side.
// ---------------------------------------------------------------------------

export const BROWSER_COMMAND_TYPES = [
  "dialog_turn",
  "start_feature",
  "stop_feature",
  "retry_feature",
] as const;
export type BrowserCommandType = (typeof BROWSER_COMMAND_TYPES)[number];

export interface BrowserCommandEnvelope {
  v: 1;
  type: BrowserCommandType;
  project_id: string;
  // Per-command payloads. `message` is only meaningful for dialog_turn;
  // `feature_id` for start/stop/retry; `hint` only for retry_feature.
  message?: string;
  feature_id?: string;
  hint?: string;
  [extra: string]: unknown;
}

/**
 * Best-effort parser for an inbound browser WS frame. Returns `null`
 * on any malformed shape — the bridge drops the frame and never
 * raises, because a misbehaving client must not be able to crash the
 * bridge and disconnect every other connected browser.
 *
 * Validation rules (intentionally minimal — the daemon re-validates):
 *   - must be a JSON object
 *   - `v` must equal 1
 *   - `type` must be one of `BROWSER_COMMAND_TYPES`
 *   - `project_id` must be a non-empty string
 *   - per-type requirements:
 *       dialog_turn    requires `message: string` (length > 0)
 *       start_feature  requires `feature_id: string` (length > 0)
 *       stop_feature   requires `feature_id: string` (length > 0)
 *       retry_feature  requires `feature_id: string` (length > 0); `hint` optional
 */
export function parseBrowserCommand(
  raw: unknown,
): BrowserCommandEnvelope | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  if (obj["v"] !== 1) return null;
  const type = obj["type"];
  if (typeof type !== "string") return null;
  if (!(BROWSER_COMMAND_TYPES as ReadonlyArray<string>).includes(type)) {
    return null;
  }
  const projectId = obj["project_id"];
  if (typeof projectId !== "string" || projectId.length === 0) return null;

  const message = obj["message"];
  const featureId = obj["feature_id"];
  const hint = obj["hint"];

  if (type === "dialog_turn") {
    if (typeof message !== "string" || message.length === 0) return null;
  } else {
    // start_feature | stop_feature | retry_feature
    if (typeof featureId !== "string" || featureId.length === 0) return null;
  }

  const envelope: BrowserCommandEnvelope = {
    v: 1,
    type: type as BrowserCommandType,
    project_id: projectId,
  };
  if (type === "dialog_turn") {
    envelope.message = message as string;
  } else {
    envelope.feature_id = featureId as string;
    if (type === "retry_feature" && typeof hint === "string") {
      envelope.hint = hint;
    }
  }
  return envelope;
}

/**
 * Exhaustive check helper for the browser-command discriminator,
 * mirrors `assertNeverDaemonEvent`. Used in `BrowserWsBridge`'s
 * dispatch `default:` arm to catch a future command added to
 * `BROWSER_COMMAND_TYPES` without a matching case at compile time.
 */
export function assertNeverBrowserCommand(x: never): never {
  throw new Error(`unhandled browser command: ${JSON.stringify(x)}`);
}
