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
