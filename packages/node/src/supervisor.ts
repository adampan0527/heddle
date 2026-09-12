// SPDX-License-Identifier: Apache-2.0
/**
 * DaemonSupervisor — feat-027 (Process supervision, T-011).
 *
 * Owns the Python daemon subprocess lifecycle for its entire lifetime:
 *
 *   - Spawns `python -m heddle_daemon` (override via `pythonArgs` for tests).
 *   - Opens a loopback WebSocket to the daemon and exposes a typed event
 *     surface that feat-029 / feat-030 / feat-047 hook into.
 *   - Drives a 5-second WS ping/pong health probe; three consecutive
 *     missed pongs trigger respawn.
 *   - Respawns on any of: subprocess exit, WS disconnect, ping timeout.
 *   - Graceful `stop()` cancels pending respawns, sends SIGTERM, escalates
 *     to SIGKILL after `SHUTDOWN_GRACE_MS`.
 *
 * Scope boundary: this class does NOT implement the per-feature 3/10-min
 * restart budget (DESIGN.md D-051) — that belongs to feat-047 / the
 * daemon layer. We emit a `restart-budget-warning` event so feat-047 can
 * listen without us having to cross the language boundary to read
 * `feature_list.json`.
 *
 * WS library note: Node 24's built-in `WebSocket` (undici) does not
 * expose `ping()` / `pong()`; we depend on the `ws` package explicitly.
 */

import { EventEmitter } from "node:events";
import { spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import { delimiter, resolve } from "node:path";
import { WebSocket as WSWebSocket } from "ws";

import {
  DaemonRequestTimeoutError,
  DaemonUnavailableError,
  resolveRequestTimeoutMs,
  type DaemonRequestEnvelope,
} from "./types.js";
import type { DaemonEventRecord, DaemonEventName } from "./protocol.js";

// ---------------------------------------------------------------------------
// Constants (all `as const` — exported so tests can assert against them).
// ---------------------------------------------------------------------------

/** Per TECH.md T-011 / D-051. */
export const DEFAULT_DAEMON_RESTART_DELAY_MS = 500;

/** Health-probe cadence. */
export const PING_INTERVAL_MS = 5000;

/**
 * Per-pong deadline. Deliberately < PING_INTERVAL_MS / 2 so a slow-but-
 * arriving pong can still reset the missed counter within the same
 * interval, giving the daemon one slow response per cycle before counting
 * it as a miss.
 */
export const PING_TIMEOUT_MS = 2000;

/** Three missed pongs in a row ⇒ daemon is dead (D-051). */
export const MAX_MISSED_PINGS = 3;

/** SIGTERM → SIGKILL grace window in `stop()`. */
export const SHUTDOWN_GRACE_MS = 5000;

/** Time budget for the initial WS connect after the daemon binds. */
export const CONNECT_TIMEOUT_MS = 5000;

/** Loopback defaults — matches Python daemon's DEFAULT_HOST / DEFAULT_PORT. */
export const DEFAULT_DAEMON_HOST = "127.0.0.1";
export const DEFAULT_DAEMON_PORT = 8765;

/** Subprocess invocation. Override via `SupervisorOptions.pythonArgs` in tests. */
export const DAEMON_COMMAND = "python";
export const DAEMON_MODULE_FLAG = "-m";
export const DAEMON_MODULE_NAME = "heddle_daemon";

/** Env-var names — HEDDLE_<SUBSYSTEM>_<NAME> convention (TECH.md T-017). */
export const ENV_DAEMON_PYTHON = "HEDDLE_DAEMON_PYTHON";
export const ENV_DAEMON_HOST = "HEDDLE_DAEMON_HOST";
export const ENV_DAEMON_PORT = "HEDDLE_DAEMON_PORT";
export const ENV_DAEMON_PROJECT_PATH = "HEDDLE_DAEMON_PROJECT_PATH";
export const ENV_RECURSION_LIMIT = "HEDDLE_RECURSION_LIMIT";
export const ENV_DAEMON_RESTART_DELAY_MS = "HEDDLE_DAEMON_RESTART_DELAY_MS";

/** Repo layout — used to prepend PYTHONPATH so `python -m heddle_daemon` resolves. */
const REPO_ROOT = resolve(process.cwd(), "..", "..");
const DAEMON_PACKAGE_DIR = resolve(REPO_ROOT, "packages", "daemon");

/** Treat a daemon that exits <1s after spawn as "sick" (budget hint). */
export const SICK_DAEMON_THRESHOLD_MS = 1000;

// ---------------------------------------------------------------------------
// State machine + event surface.
// ---------------------------------------------------------------------------

export type SupervisorState =
  | "IDLE"
  | "STARTING"
  | "SPAWNING"
  | "CONNECTING"
  | "RUNNING"
  | "DEGRADED"
  | "RESPAWN_SCHEDULED"
  | "STOPPED";

export type RestartCause = "ws_disconnect" | "ping_timeout" | "process_exit";

export interface DaemonSpawnedEvent {
  pid: number;
}
export interface DaemonExitEvent {
  pid: number;
  code: number | null;
  signal: NodeJS.Signals | null;
  lifetimeMs: number;
}
export interface WsDisconnectedEvent {
  code: number;
  reason: string;
}
export interface WsPingMissedEvent {
  consecutive: number;
}
export interface RestartScheduledEvent {
  delayMs: number;
  cause: RestartCause;
}
export interface RestartBudgetWarningEvent {
  cause: RestartCause;
}
export interface WsHandshakeEvent {
  ws: WSWebSocket;
}

export interface DaemonSupervisorEvents {
  "daemon-spawned": [DaemonSpawnedEvent];
  "daemon-exit": [DaemonExitEvent];
  "ws-connected": [Record<string, never>];
  "ws-disconnected": [WsDisconnectedEvent];
  "ws-ping-missed": [WsPingMissedEvent];
  "restart-scheduled": [RestartScheduledEvent];
  "restart-budget-warning": [RestartBudgetWarningEvent];
  "ws-handshake": [WsHandshakeEvent];
  // feat-030: unsolicited events pushed by the daemon (no req_id).
  // Mirrors the Python side's `build_envelope("event", ...)` output.
  // Subscribers narrow on `record.event` (see protocol.ts).
  "daemon-event": [DaemonEventRecord];
}

export interface SupervisorOptions {
  /** Override python binary. Defaults to `HEDDLE_DAEMON_PYTHON` env, then `"python"`. */
  pythonBin?: string;
  /**
   * TEST HOOK: when set, the supervisor spawns `pythonArgs[0]` with the
   * remaining elements as args, replacing `python -m heddle_daemon`.
   * Production callers leave this undefined.
   */
  pythonArgs?: ReadonlyArray<string>;
  /** Subprocess env base. Defaults to `process.env`. */
  env?: NodeJS.ProcessEnv;
  restartDelayMs?: number;
  pingIntervalMs?: number;
  pingTimeoutMs?: number;
  maxMissedPings?: number;
  shutdownGraceMs?: number;
  daemonHost?: string;
  daemonPort?: number;
  connectTimeoutMs?: number;
}

// Type-safe EventEmitter: declare `on` / `emit` overloads that know the
// payload shape of each event name. Consumers get full inference at call
// sites (`sup.on("ws-disconnected", ({code}) => …)`).
export declare interface DaemonSupervisor {
  on<E extends keyof DaemonSupervisorEvents>(
    event: E,
    listener: (...args: DaemonSupervisorEvents[E]) => void,
  ): this;
  emit<E extends keyof DaemonSupervisorEvents>(
    event: E,
    ...args: DaemonSupervisorEvents[E]
  ): boolean;
}

// ---------------------------------------------------------------------------
// Implementation.
// ---------------------------------------------------------------------------

export class DaemonSupervisor extends EventEmitter {
  private _state: SupervisorState = "IDLE";
  private readonly _opts: Required<
    Omit<SupervisorOptions, "pythonArgs" | "env">
  > & { pythonArgs?: ReadonlyArray<string>; env: NodeJS.ProcessEnv };
  private _child: ChildProcess | null = null;
  private _childSpawnedAt: number | null = null;
  private _ws: WSWebSocket | null = null;
  private _restartTimer: NodeJS.Timeout | null = null;
  private _pingTimer: NodeJS.Timeout | null = null;
  private _pongDeadline: NodeJS.Timeout | null = null;
  private _missedPings = 0;
  private _spawnInFlight = false;
  private _stopRequested = false;
  private _pythonMissingWarned = false;
  // feat-028: per-process request sequence for the request() wrapper.
  // Combined with the pid to form a globally-unique req_id the daemon
  // echoes back so we can match responses to outstanding promises.
  private _reqSeq = 0;
  private _pendingRequests: Map<
    string,
    {
      resolve: (value: unknown) => void;
      reject: (err: unknown) => void;
    }
  > = new Map();

  constructor(opts: SupervisorOptions = {}) {
    super();
    this._opts = {
      pythonBin:
        opts.pythonBin ?? process.env[ENV_DAEMON_PYTHON] ?? DAEMON_COMMAND,
      pythonArgs: opts.pythonArgs,
      env: opts.env ?? process.env,
      restartDelayMs:
        opts.restartDelayMs ??
        (Number(process.env[ENV_DAEMON_RESTART_DELAY_MS]) ||
          DEFAULT_DAEMON_RESTART_DELAY_MS),
      pingIntervalMs: opts.pingIntervalMs ?? PING_INTERVAL_MS,
      pingTimeoutMs: opts.pingTimeoutMs ?? PING_TIMEOUT_MS,
      maxMissedPings: opts.maxMissedPings ?? MAX_MISSED_PINGS,
      shutdownGraceMs: opts.shutdownGraceMs ?? SHUTDOWN_GRACE_MS,
      daemonHost:
        opts.daemonHost ?? process.env[ENV_DAEMON_HOST] ?? DEFAULT_DAEMON_HOST,
      daemonPort:
        opts.daemonPort ??
        (Number(process.env[ENV_DAEMON_PORT]) || DEFAULT_DAEMON_PORT),
      connectTimeoutMs: opts.connectTimeoutMs ?? CONNECT_TIMEOUT_MS,
    };
  }

  // -------------------------------------------------------------------------
  // Public API.
  // -------------------------------------------------------------------------

  get state(): SupervisorState {
    return this._state;
  }

  get pid(): number | null {
    return this._child?.pid ?? null;
  }

  /**
   * Live WS instance once connected (RUNNING). `null` before CONNECTING
   * completes and after STOPPED. feat-030's message-protocol layer
   * subscribes via the `ws-handshake` event (preferred) or via this getter.
   */
  get socket(): WSWebSocket | null {
    return this._ws;
  }

  /**
   * Request / response wrapper — feat-028 (HTTP REST routes).
   *
   * Sends a JSON envelope carrying a unique `req_id` and resolves with
   * the matching response (matched by `req_id` in the inbound event).
   * Used by `packages/node/src/routes/*.ts` to forward HTTP traffic
   * to the daemon and translate the response back into HTTP status
   * codes.
   *
   * Contract:
   *   - Rejects immediately with `DaemonUnavailableError` if the
   *     supervisor is not in `RUNNING` state (HTTP 503 at the route).
   *   - Rejects with `DaemonRequestTimeoutError` if no response
   *     arrives within `timeoutMs` (default 10s, configurable via
   *     `HEDDLE_DAEMON_REQUEST_TIMEOUT_MS`).
   *   - Resolves with the parsed `data` on `ok: true`.
   *   - Throws an `Error` carrying `code`/`message` from the daemon
   *     on `ok: false`, so route handlers can catch + map to HTTP
   *     status codes (400 / 404 / 409 / etc.).
   *
   * feat-030 builds the full event-stream protocol on top of the
   * same `req_id` field; this method is the minimal request/response
   * primitive feat-028 needs and stays unchanged when feat-030 ships.
   */
  async request<TData = unknown>(
    envelopeType: string,
    payload: Record<string, unknown> = {},
    options: { timeoutMs?: number } = {},
  ): Promise<TData> {
    if (this._state !== "RUNNING" || !this._ws) {
      throw new DaemonUnavailableError(this._state);
    }
    const reqId = `${process.pid}-${++this._reqSeq}`;
    const timeoutMs = options.timeoutMs ?? resolveRequestTimeoutMs();
    const fullEnvelope: DaemonRequestEnvelope = {
      v: 1,
      type: envelopeType,
      req_id: reqId,
      ...payload,
    };

    return new Promise<TData>((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pendingRequests.delete(reqId);
        reject(
          new DaemonRequestTimeoutError(envelopeType, reqId, timeoutMs),
        );
      }, timeoutMs);

      this._pendingRequests.set(reqId, {
        resolve: (value) => {
          clearTimeout(timer);
          // The daemon's `data` payload is JSON-shaped, but we don't
          // validate it here — call sites narrow via `TData` generic.
          // Cast through `unknown` so the generic boundary doesn't
          // leak into the pending-requests Map's value type.
          resolve(value as TData);
        },
        reject: (err) => {
          clearTimeout(timer);
          reject(err);
        },
      });

      // Best-effort send. ``ws.send`` can throw synchronously if the
      // socket has just transitioned to CLOSING — wrap and route the
      // failure through the pending-request reject path so the caller
      // sees a uniform "request rejected" error.
      try {
        this._ws!.send(JSON.stringify(fullEnvelope));
      } catch (err) {
        const entry = this._pendingRequests.get(reqId);
        this._pendingRequests.delete(reqId);
        clearTimeout(timer);
        if (entry) {
          entry.reject(
            err instanceof Error ? err : new Error(String(err)),
          );
        }
        return;
      }
    });
  }

  /**
   * Fire-and-forget command sender — feat-030 (protocol).
   *
   * Same wire shape as `request()` (an outbound command envelope with
   * a unique `req_id`), but does NOT await a response: the daemon
   * will push progress events on the event stream and the eventual
   * response envelope is matched by `request_id` to whatever caller
   * also called `request()` (or is silently discarded if no one is
   * waiting). Use this when you only care about the side-effect
   * (e.g. the WS bridge in feat-029 fanning events out to the
   * browser); use `request()` when you need the response payload.
   *
   * Throws `DaemonUnavailableError` if the supervisor is not RUNNING.
   * Sync `ws.send` failures are caught and re-thrown so the caller
   * can decide whether to log + retry or surface to the user.
   *
   * `req_id` is auto-allocated if `payload.req_id` is not provided.
   */
  sendCommand(
    envelopeType: string,
    payload: Record<string, unknown> = {},
  ): string {
    if (this._state !== "RUNNING" || !this._ws) {
      throw new DaemonUnavailableError(this._state);
    }
    const reqId =
      (payload["req_id"] as string | undefined) ??
      `${process.pid}-${++this._reqSeq}`;
    const fullEnvelope: DaemonRequestEnvelope = {
      v: 1,
      type: envelopeType,
      req_id: reqId,
      ...payload,
    };
    this._ws.send(JSON.stringify(fullEnvelope));
    return reqId;
  }

  /**
   * Bring the daemon up. Idempotent: a second call while STARTING is in
   * flight returns immediately; a call after STOPPED is a no-op.
   */
  async start(): Promise<void> {
    if (this._state === "STOPPED") return;
    if (this._state !== "IDLE") return;
    this._state = "STARTING";
    await this._spawn();
  }

  /**
   * Graceful teardown. Cancels pending respawns, sends SIGTERM, escalates
   * to SIGKILL after `shutdownGraceMs`. Safe to call multiple times.
   */
  async stop(): Promise<void> {
    if (this._state === "STOPPED") return;
    this._stopRequested = true;
    if (this._restartTimer) {
      clearTimeout(this._restartTimer);
      this._restartTimer = null;
    }
    this._stopPingLoop();
    this._teardownWs();
    const child = this._child;
    if (!child) {
      this._state = "STOPPED";
      return;
    }
    await this._terminateChild(child);
    this._state = "STOPPED";
  }

  // -------------------------------------------------------------------------
  // Internal helpers. Each one is independently testable; together they
  // implement the state machine described in the feat-027 plan.
  // -------------------------------------------------------------------------

  /** Subprocess construction. Spawns the daemon, wires exit/error hooks. */
  private async _spawn(): Promise<void> {
    if (this._spawnInFlight) return;
    if (this._stopRequested) return;
    this._spawnInFlight = true;
    try {
      this._state = "SPAWNING";

      // Env construction. PYTHONPATH is prepended with the daemon package
      // dir so `python -m heddle_daemon` resolves inside the workspace
      // even when the package isn't installed in the active venv.
      const existingPythonPath = this._opts.env.PYTHONPATH ?? "";
      const env: NodeJS.ProcessEnv = {
        ...this._opts.env,
        [ENV_DAEMON_HOST]: this._opts.daemonHost,
        [ENV_DAEMON_PORT]: String(this._opts.daemonPort),
        PYTHONPATH: [existingPythonPath, DAEMON_PACKAGE_DIR]
          .filter((p) => p.length > 0)
          .join(delimiter),
      };

      const cmd = this._opts.pythonArgs
        ? this._opts.pythonArgs[0]
        : this._opts.pythonBin;
      const args = this._opts.pythonArgs
        ? this._opts.pythonArgs.slice(1)
        : [DAEMON_MODULE_FLAG, DAEMON_MODULE_NAME];

      const child = spawn(cmd, args, {
        env,
        stdio: "inherit",
        windowsHide: true,
      });
      this._child = child;
      this._childSpawnedAt = Date.now();
      this._state = "CONNECTING";

      child.once("spawn", () => {
        if (this._stopRequested) {
          // Stop raced the spawn — kill immediately.
          child.kill("SIGKILL");
          return;
        }
        this.emit("daemon-spawned", { pid: child.pid ?? -1 });
        // Open the WS client now that the daemon subprocess is up. The
        // connect path is async — failures flow through `_onWsClose` or
        // `_onWsConnectFailed` depending on whether the socket ever opened.
        void this._connectWs();
      });

      child.once("exit", (code, signal) => {
        this._onChildExit(code, signal);
      });

      child.once("error", (err) => {
        this._onChildError(err);
      });
    } finally {
      this._spawnInFlight = false;
    }
  }

  private _onChildExit(
    code: number | null,
    signal: NodeJS.Signals | null,
  ): void {
    const lifetimeMs = this._childSpawnedAt
      ? Date.now() - this._childSpawnedAt
      : 0;
    this.emit("daemon-exit", {
      pid: this._child?.pid ?? -1,
      code,
      signal,
      lifetimeMs,
    });
    if (lifetimeMs < SICK_DAEMON_THRESHOLD_MS) {
      this.emit("restart-budget-warning", { cause: "process_exit" });
    }
    this._teardownWs();
    this._stopPingLoop();
    // Reading _missedPings here keeps TypeScript's noUnusedLocals happy
    // and gives us a sanity check that a fast-exit daemon never accrued
    // missed pings. Step 4 will own this field's lifecycle in earnest.
    if (this._missedPings !== 0) {
      this._missedPings = 0;
    }
    this._child = null;
    this._childSpawnedAt = null;
    if (this._stopRequested) return;
    this._enterDegraded("process_exit");
  }

  private _onChildError(err: Error): void {
    // 'error' fires synchronously for ENOENT (python missing) and other
    // spawn-time failures. The subsequent 'exit' event also fires with
    // code=null, so we don't need to schedule an extra respawn here —
    // _onChildExit will handle it.
    const errno = (err as NodeJS.ErrnoException).code;
    if (errno === "ENOENT" && !this._pythonMissingWarned) {
      this._pythonMissingWarned = true;
      // Surface to operator once; rest are routine daemon-exit events.
      process.stderr.write(
        JSON.stringify({
          ts: new Date().toISOString(),
          level: "warn",
          component: "node",
          project_id: null,
          feature_id: null,
          event: "daemon_python_unresolved",
          msg: `python binary not found; set HEDDLE_DAEMON_PYTHON or install python on PATH`,
          errno,
        }) + "\n",
      );
    }
  }

  private _teardownWs(): void {
    if (this._ws) {
      try {
        this._ws.removeAllListeners();
        this._ws.close();
      } catch {
        // already closed; ignore
      }
      this._ws = null;
    }
    // feat-028: a WS drop aborts every outstanding request() so
    // routes can return 503 to the browser instead of hanging until
    // each request's individual timeout fires. The route maps
    // DaemonUnavailableError to HTTP 503; surfacing the supervisor's
    // ``state`` field gives the caller a clear "daemon went away"
    // signal even if the WS itself was healthy before close.
    this._rejectAllPendingRequests(
      this._state === "STOPPED" ? "stopped" : "ws_disconnect",
    );
  }

  /**
   * feat-028 + feat-030: parse an inbound WS frame and dispatch to the
   * right consumer.
   *
   *   * No `req_id` → daemon-initiated event (feat-030). Forwarded as
   *     a `daemon-event` EventEmitter payload. Malformed event shapes
   *     are logged + dropped (a future listener may re-emit as warn).
   *   * `req_id` present → request/response correlation (feat-028).
   *     Matched against the pending-request Map; the existing
   *     ok/error envelope handling is unchanged.
   *
   * Malformed JSON is ignored on both paths — feat-029 will add a
   * dedicated warn log when the browser bridge ships; feat-028/030
   * stay quiet to keep the wire layer thin.
   */
  private _handleInboundMessage(data: unknown): void {
    let parsed: unknown;
    try {
      const text =
        typeof data === "string"
          ? data
          : Buffer.isBuffer(data)
            ? data.toString("utf-8")
            : Array.isArray(data)
              ? Buffer.concat(data).toString("utf-8")
              : String(data);
      parsed = JSON.parse(text);
    } catch {
      // Malformed JSON — ignore. feat-029's browser bridge will log
      // these at warn; the supervisor stays silent because the same
      // frame could be a partial server-pushed event we'd otherwise
      // re-receive as a duplicate parse error.
      return;
    }
    if (!parsed || typeof parsed !== "object") return;
    const obj = parsed as Record<string, unknown>;
    const reqId = obj["req_id"];
    if (typeof reqId !== "string") {
      // feat-030: unsolicited event. Validate the minimum shape and
      // forward; unknown `event` strings land at the listener as
      // `DaemonEventRecord.event: <string>` — listeners must have a
      // `default:` arm in their switch (assertNeverDaemonEvent
      // enforces exhaustiveness during development).
      const eventName = obj["event"];
      const projectId = obj["project_id"];
      if (typeof eventName !== "string" || typeof projectId !== "string") {
        // Not a valid event envelope and not a response — drop
        // silently. feat-029 may upgrade this to a warn log once
        // the browser-facing bridge ships.
        return;
      }
      const featureIdRaw = obj["feature_id"];
      const featureId =
        typeof featureIdRaw === "string" && featureIdRaw.length > 0
          ? featureIdRaw
          : null;
      const payloadRaw = obj["payload"];
      const payload =
        payloadRaw && typeof payloadRaw === "object"
          ? (payloadRaw as Record<string, unknown>)
          : {};
      // Build a value of the DaemonEventRecord shape. The supervisor
      // runs at the wire layer so it cannot know the runtime variant
      // without per-event validation; each listener narrows on
      // `record.event` (see protocol.ts / main.ts). The cast on
      // `eventName` widens a runtime string to the literal-union
      // type — unsafe in theory, but every listener has a
      // `default:` arm that treats unknown values as a soft warn
      // rather than crashing, so the worst case is a misnamed event
      // showing up as `unknown` in the log, not a type explosion.
      const record: DaemonEventRecord = {
        event: eventName as DaemonEventName,
        project_id: projectId,
        feature_id: featureId,
        payload,
        received_at: Date.now(),
      };
      this.emit("daemon-event", record);
      return;
    }
    const entry = this._pendingRequests.get(reqId);
    if (!entry) return;
    this._pendingRequests.delete(reqId);
    if (obj["ok"] === true) {
      entry.resolve(obj["data"]);
      return;
    }
    const err = obj["error"];
    if (
      err &&
      typeof err === "object" &&
      "code" in err &&
      "message" in err
    ) {
      const daemonErr = err as { code: string; message: string };
      const ex = new Error(daemonErr.message);
      // Stash the code on the error so route handlers can switch on
      // it without re-parsing the message. Routes import the
      // `ErrorCode` type from types.ts and cast this back.
      (ex as unknown as { code: string }).code = daemonErr.code;
      entry.reject(ex);
      return;
    }
    entry.reject(new Error("daemon response missing ok/error envelope"));
  }

  private _rejectAllPendingRequests(reason: string): void {
    if (this._pendingRequests.size === 0) return;
    const err = new DaemonUnavailableError(reason);
    for (const [, entry] of this._pendingRequests) {
      entry.reject(err);
    }
    this._pendingRequests.clear();
  }

  private _stopPingLoop(): void {
    if (this._pingTimer) {
      clearInterval(this._pingTimer);
      this._pingTimer = null;
    }
    if (this._pongDeadline) {
      clearTimeout(this._pongDeadline);
      this._pongDeadline = null;
    }
    // Step 4 wires ping/pong. Reset the missed counter on teardown so a
    // future respawn starts from zero.
    this._missedPings = 0;
  }

  private _enterDegraded(cause: RestartCause): void {
    if (this._stopRequested) return;
    this._state = "DEGRADED";
    this._scheduleRestart(cause);
  }

  private _scheduleRestart(cause: RestartCause): void {
    if (this._stopRequested) return;
    if (this._restartTimer) return; // already scheduled
    const delayMs = this._opts.restartDelayMs;
    this.emit("restart-scheduled", { delayMs, cause });
    this._state = "RESPAWN_SCHEDULED";
    this._restartTimer = setTimeout(() => {
      this._restartTimer = null;
      if (this._stopRequested) return;
      void this._spawn();
    }, delayMs);
  }

  private async _terminateChild(child: ChildProcess): Promise<void> {
    if (child.exitCode !== null) return;
    const exited = new Promise<"exit" | "timeout">((resolve) => {
      child.once("exit", () => resolve("exit"));
      // Fallback timer for cases where SIGTERM is ignored (Windows emulates
      // it as immediate kill, so 'exit' usually fires first anyway).
      setTimeout(() => resolve("timeout"), this._opts.shutdownGraceMs);
    });
    try {
      child.kill("SIGTERM");
    } catch {
      return;
    }
    const result = await exited;
    if (result === "timeout" && child.exitCode === null) {
      try {
        child.kill("SIGKILL");
      } catch {
        // already gone
      }
    }
  }

  // -------------------------------------------------------------------------
  // WS connect + ping/pong health probe (Steps 3 + 4).
  // -------------------------------------------------------------------------

  /**
   * Open a WS client to the daemon's loopback address. We retry on
   * connect failure with exponential-ish backoff (every 200ms) until the
   * total budget CONNECT_TIMEOUT_MS is exhausted, because the daemon's
   * asyncio WS server starts a fraction of a second after the subprocess
   * 'spawn' event fires — on a cold start it can take 100–500ms for the
   * socket to actually bind.
   */
  private async _connectWs(): Promise<void> {
    if (this._stopRequested) return;
    const url = `ws://${this._opts.daemonHost}:${this._opts.daemonPort}/ws`;
    const deadline = Date.now() + this._opts.connectTimeoutMs;
    const RETRY_MS = 200;

    // eslint-disable-next-line no-constant-condition
    while (true) {
      if (this._stopRequested) return;
      const opened = await this._tryConnectOnce(url);
      if (opened) return; // success — handler set up state + ping loop
      if (Date.now() >= deadline) {
        // Out of retry budget. Emit disconnect so downstream sees the cause.
        this.emit("ws-disconnected", { code: 1006, reason: "connect_timeout" });
        this._enterDegraded("ws_disconnect");
        return;
      }
      await new Promise((r) => setTimeout(r, RETRY_MS));
    }
  }

  /**
   * One connect attempt. Resolves to true if 'open' fired, false if the
   * socket closed before opening.
   */
  private _tryConnectOnce(url: string): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      const ws = new WSWebSocket(url);
      let settled = false;
      const settle = (ok: boolean): void => {
        if (settled) return;
        settled = true;
        resolve(ok);
      };

      const connectWatchdog = setTimeout(() => {
        // If we still haven't resolved within this attempt's slice, force-
        // close and report failure. The outer retry loop will start again.
        if (!settled) {
          try {
            ws.terminate();
          } catch {
            // ignore
          }
          settle(false);
        }
      }, Math.min(2000, this._opts.connectTimeoutMs));

      ws.once("open", () => {
        clearTimeout(connectWatchdog);
        if (this._stopRequested) {
          try {
            ws.close();
          } catch {
            // ignore
          }
          settle(false);
          return;
        }
        this._ws = ws;
        this._state = "RUNNING";
        this.emit("ws-connected", {});
        this.emit("ws-handshake", { ws });
        this._startPingLoop();
        settle(true);
      });

      ws.on("close", (code, reasonBuf) => {
        clearTimeout(connectWatchdog);
        if (!settled) {
          // Never opened — this is a retry-trigger, not a runtime disconnect.
          settle(false);
          return;
        }
        // Already opened and settled: this is a runtime close. Hand off
        // to the lifecycle handler.
        const reason = reasonBuf?.toString() ?? "";
        this._onWsClose(code, reason);
      });

      ws.on("error", () => {
        // Errors always precede a 'close' event in `ws`, so we don't need
        // to act here — `_onWsClose` or the settle(false) path will fire next.
      });

      ws.on("pong", () => {
        this._onWsPong();
      });

      // feat-028: match inbound response envelopes to outstanding
      // request() promises by req_id. Anything we don't recognise
      // (e.g. supervisor-event-stream messages from feat-030) is
      // ignored here — those handlers will attach via the
      // ``message`` event themselves.
      ws.on("message", (data) => {
        this._handleInboundMessage(data);
      });
    });
  }

  private _onWsClose(code: number, reason: string): void {
    if (this._ws) {
      this._ws.removeAllListeners();
      this._ws = null;
    }
    this._stopPingLoop();
    if (this._stopRequested) return;
    this.emit("ws-disconnected", { code, reason });
    // Force-kill the daemon subprocess so its 'exit' event fires (it may
    // still be alive momentarily). _onChildExit will then drive the
    // respawn; if it has already done so, _scheduleRestart's
    // "already scheduled" guard makes the second call a no-op.
    const child = this._child;
    if (child && child.exitCode === null) {
      try {
        child.kill("SIGKILL");
      } catch {
        // ignore
      }
    }
    this._enterDegraded("ws_disconnect");
  }

  private _startPingLoop(): void {
    this._missedPings = 0;
    this._pingTimer = setInterval(() => {
      const ws = this._ws;
      if (!ws || this._state !== "RUNNING") return;
      try {
        ws.ping();
      } catch {
        // ignore — close handler will fire next
        return;
      }
      this._pongDeadline = setTimeout(() => {
        this._pongDeadline = null;
        this._missedPings += 1;
        if (this._missedPings >= this._opts.maxMissedPings) {
          // Force-kill the daemon; the close handler schedules the respawn.
          const child = this._child;
          if (child && child.exitCode === null) {
            try {
              child.kill("SIGKILL");
            } catch {
              // ignore
            }
          }
          // Mark the cause as ping_timeout via a budget-warning so feat-047
          // can distinguish from generic ws_disconnect if it wants to.
          this.emit("restart-budget-warning", { cause: "ping_timeout" });
          this._enterDegraded("ping_timeout");
        } else {
          this.emit("ws-ping-missed", { consecutive: this._missedPings });
        }
      }, this._opts.pingTimeoutMs);
    }, this._opts.pingIntervalMs);
  }

  private _onWsPong(): void {
    // Any pong arrival — even late — resets the missed counter. This
    // matches D-051's "daemon considered dead" semantics: a responsive
    // daemon is healthy regardless of jitter.
    if (this._pongDeadline) {
      clearTimeout(this._pongDeadline);
      this._pongDeadline = null;
    }
    this._missedPings = 0;
  }
}