// SPDX-License-Identifier: Apache-2.0
/**
 * Shared envelopes + error taxonomy for packages/node — feat-028.
 *
 * Both the HTTP routes (`packages/node/src/routes/*.ts`) and the
 * supervisor's request/response wrapper (`DaemonSupervisor.request`)
 * speak the same envelopes, so this is the single source of truth for
 * the wire shape. The daemon side (feat-028 routes.py) mirrors the
 * shape in Python so a schema drift on either side surfaces as a test
 * failure rather than a runtime error.
 *
 * Wire contract (matches `heddle_daemon.routes`):
 *
 *     Request:  {v: 1, type: "<cmd>", req_id: string, ...extras}
 *     Response: {v: 1, type: "<cmd>_response", req_id: string,
 *                ok: true, data: {...}}  |  {ok: false, error: {code, message}}
 *
 * The HTTP layer wraps these in `{ok, data}` / `{ok, error}` envelopes
 * (matching the existing 501-stub pattern) so the browser never sees
 * the raw daemon shape.
 */

/** All wire codes the daemon (or HTTP layer) can return. */
export const ERROR_CODES = [
  "invalid_input",
  "not_found",
  "conflict",
  "invalid_state",
  "schema_too_new",
  "envelope_malformed",
  "envelope_version_too_new",
  "handler_error",
  "internal_error",
  "daemon_unavailable",
  "request_timeout",
] as const;
export type ErrorCode = (typeof ERROR_CODES)[number];

/** Per-call timeout. feat-044's LLM calls will override per envelope. */
export const DEFAULT_REQUEST_TIMEOUT_MS = 10_000;
export const ENV_REQUEST_TIMEOUT_MS = "HEDDLE_DAEMON_REQUEST_TIMEOUT_MS";

/** Cached env lookup; feat-028 calls into this once per process. */
let _cachedTimeoutMs: number | null = null;
export function resolveRequestTimeoutMs(): number {
  if (_cachedTimeoutMs !== null) return _cachedTimeoutMs;
  const raw = process.env[ENV_REQUEST_TIMEOUT_MS];
  if (raw == null) {
    _cachedTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS;
    return _cachedTimeoutMs;
  }
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n) || n <= 0) {
    _cachedTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS;
    return _cachedTimeoutMs;
  }
  _cachedTimeoutMs = n;
  return _cachedTimeoutMs;
}

/**
 * Test-only helper: clear the cached env-var lookup so the next call
 * re-reads ``process.env``. Used by supervisor-request.test.ts to
 * override the timeout per-test without bouncing the process. Not
 * part of the production API.
 */
export function resetCachedTimeout(): void {
  _cachedTimeoutMs = null;
}

/**
 * Raised when `DaemonSupervisor.request()` does not receive a matching
 * response within the timeout. Routes surface this as HTTP 504.
 */
export class DaemonRequestTimeoutError extends Error {
  public readonly envelopeType: string;
  public readonly timeoutMs: number;
  public readonly reqId: string;
  constructor(envelopeType: string, reqId: string, timeoutMs: number) {
    super(
      `daemon did not respond to ${envelopeType} (req_id=${reqId}) ` +
        `within ${timeoutMs}ms`,
    );
    this.name = "DaemonRequestTimeoutError";
    this.envelopeType = envelopeType;
    this.reqId = reqId;
    this.timeoutMs = timeoutMs;
  }
}

/**
 * Raised when `DaemonSupervisor.request()` is invoked while the
 * supervisor is not in `RUNNING` state (daemon WS down / connecting).
 * Routes surface this as HTTP 503.
 */
export class DaemonUnavailableError extends Error {
  public readonly supervisorState: string;
  constructor(supervisorState: string) {
    super(
      `daemon supervisor is not running (state=${supervisorState}); ` +
        `request rejected`,
    );
    this.name = "DaemonUnavailableError";
    this.supervisorState = supervisorState;
  }
}

/** The structured error returned by the daemon on `ok: false`. */
export interface DaemonError {
  code: ErrorCode;
  message: string;
}

export interface DaemonRequestEnvelope {
  v: 1;
  type: string;
  req_id: string;
  [extra: string]: unknown;
}

export interface DaemonResponseEnvelopeOk<T> {
  v: 1;
  type: string;
  req_id: string;
  ok: true;
  data: T;
}

export interface DaemonResponseEnvelopeErr {
  v: 1;
  type: string;
  req_id: string;
  ok: false;
  error: DaemonError;
}

export type DaemonResponseEnvelope<T> =
  | DaemonResponseEnvelopeOk<T>
  | DaemonResponseEnvelopeErr;

// ----- Wire contract additions for feat-030 (commands + events) -----
//
// feat-028 speaks request/response only. feat-030 extends the wire with:
//   1. Commands that elicit a stream of progress events before their
//      terminal response (start_feature / stop_feature / retry_feature /
//      dialog_turn). These look exactly like the existing request envelope
//      (v, type, req_id, ...) — no new shape, just new type strings.
//   2. Unsolicited events the daemon pushes without a req_id (feature
//      progress / done / failed / log_line / dialog token / etc.). The
//      supervisor's inbound dispatcher checks for req_id presence:
//      present → pending-request Map, absent → "daemon-event" EventEmitter.
//
// The full per-event payload shapes live in `protocol.ts` (one file,
// one job). This file declares the envelope-level types only so the
// supervisor's `_handleInboundMessage` can narrow on them.

/** All command types feat-030 owns. feat-044 may extend this list. */
export const COMMAND_TYPES = [
  "start_feature",
  "stop_feature",
  "retry_feature",
  "dialog_turn",
] as const;
export type CommandType = (typeof COMMAND_TYPES)[number];

/**
 * Outbound command envelope. Same shape as DaemonRequestEnvelope
 * (intentional — commands ARE requests, they just typically also
 * trigger event-stream side-effects before the response arrives).
 */
export interface DaemonCommandEnvelope {
  v: 1;
  type: CommandType | string; // widened to allow older daemon fallbacks
  req_id: string;
  project_id?: string;
  feature_id?: string;
  message?: string;
  hint?: string;
  [extra: string]: unknown;
}

/**
 * Inbound unsolicited event envelope. Has NO `req_id` — that's the
 * discriminator between "response to a request" and "fire-and-forget
 * event from the daemon". `event` is a string literal union; the
 * per-event payload shape is in `protocol.ts`.
 */
export interface DaemonEventEnvelope {
  v: 1;
  type: "event";
  event: string;
  project_id: string;
  feature_id?: string | null;
  payload: Record<string, unknown>;
}

// ----- HTTP-level envelopes (routes' shape, browser-facing) -----

/** Successful HTTP response. Browser does `if (resp.ok) ...`. */
export interface ApiOk<T> {
  ok: true;
  data: T;
}

/** Failed HTTP response. `code` is one of ERROR_CODES. */
export interface ApiErr {
  ok: false;
  error: DaemonError;
}

export type ApiResponse<T> = ApiOk<T> | ApiErr;