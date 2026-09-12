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
import type { WebSocket as WSWebSocket } from "ws";

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
        // WS connect + ping loop are wired by `_connectWs()` (Step 3 / 4).
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
}