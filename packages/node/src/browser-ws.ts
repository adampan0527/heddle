// SPDX-License-Identifier: Apache-2.0
/**
 * BrowserWsBridge — feat-029 (Browser↔Node.js WS event stream).
 *
 * Fans daemon events out to every connected browser client whose
 * `project_id` matches, and forwards inbound browser commands to the
 * daemon via `DaemonSupervisor`. One class, one job: the bridge sits
 * between `@fastify/websocket`'s raw `WebSocket` instances on the
 * server side and the typed `supervisor.request()` / `sendCommand()`
 * API on the daemon side.
 *
 *   daemon WS event ──► supervisor.on("daemon-event") ──► BrowserWsBridge ──► browser WS
 *   browser WS frame ──► parseBrowserCommand ──► BrowserWsBridge ──► supervisor.request / sendCommand ──► daemon WS
 *
 * Per-client state:
 *   - `project_id`  : set from the first valid inbound envelope.
 *                     Clients that haven't sent yet receive nothing
 *                     (a daemon event would be discarded anyway since
 *                     there is no project filter to match).
 *   - `pending`     : a Map<reqId, {resolve, reject}> for in-flight
 *                     dialog_turn requests whose reply envelope is
 *                     expected back from the daemon.
 *   - `outstandingCount` : incremented per forwarded command so a
 *                     test can `await drain()` until 0.
 *
 * Loopback-only assumption:
 *   In v0.1 the Fastify server binds 127.0.0.1 (feat-026). Any client
 *   that connects is therefore trusted to claim any project_id. This
 *   matches the rest of the v0.1 threat model (single-user, local-only);
 *   a future auth layer would slot in here as a pre-check on the
 *   first inbound envelope.
 *
 * Error policy:
 *   - Inbound parse failure → drop the frame, log at debug.
 *   - `ws.send` throws (socket closed mid-write) → drop the client
 *     on the next fan-out tick.
 *   - Daemon unavailable / timeout on `dialog_turn` → emit a
 *     `{ok:false, error}` envelope back to the originating client,
 *     so the browser can surface it without waiting indefinitely.
 */

import type { WebSocket as WSWebSocket } from "ws";
import { EventEmitter } from "node:events";

import type { DaemonSupervisor } from "./supervisor.js";
import { logger } from "./lib/logger.js";
import type { DaemonEventRecord } from "./protocol.js";
import {
  parseBrowserCommand,
  type BrowserCommandEnvelope,
  type BrowserCommandType,
} from "./protocol.js";

// ---------------------------------------------------------------------------
// Constants.
// ---------------------------------------------------------------------------

/** Loopback default — daemon binds 127.0.0.1, browser connects to same. */
export const BROWSER_WS_PATH = "/ws";

// ---------------------------------------------------------------------------
// Per-client book-keeping.
// ---------------------------------------------------------------------------

/**
 * Minimal WebSocket surface used by the bridge. Matches
 * `ws@8.21.3`'s `WebSocket` (the only WS we ship on the server) and
 * lets the test suite substitute a fake without pulling in `ws`.
 */
export interface BrowserSocket {
  readonly readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  on(event: "message", listener: (data: unknown) => void): this;
  on(event: "close", listener: (code: number, reason: string) => void): this;
  off(event: "message", listener: (data: unknown) => void): this;
  off(event: "close", listener: (code: number, reason: string) => void): this;
}

interface BrowserClient {
  socket: BrowserSocket;
  /** Set on first valid inbound frame. Null until then. */
  projectId: string | null;
  /**
   * Counter incremented when a dialog_turn request begins and
   * decremented when its response envelope (success or failure)
   * has been written back. Lets tests `await drain()`.
   */
  outstandingCount: number;
}

const WS_OPEN = 1;

// ---------------------------------------------------------------------------
// Public event surface — mirrors `DaemonSupervisor` style for testability.
// ---------------------------------------------------------------------------

export interface BrowserWsBridgeEvents {
  "client-added": [client: BrowserClient];
  "client-removed": [client: BrowserClient];
  "command-forwarded": [envelope: BrowserCommandEnvelope];
  "event-fanned-out": [record: DaemonEventRecord, targetCount: number];
  "reply-sent": [reqId: string, ok: boolean];
  "bridge-closed": [Record<string, never>];
}

export interface BrowserWsBridge {
  on<E extends keyof BrowserWsBridgeEvents>(
    event: E,
    listener: (...args: BrowserWsBridgeEvents[E]) => void,
  ): this;
  emit<E extends keyof BrowserWsBridgeEvents>(
    event: E,
    ...args: BrowserWsBridgeEvents[E]
  ): boolean;
}

// ---------------------------------------------------------------------------
// Implementation.
// ---------------------------------------------------------------------------

export class BrowserWsBridge extends EventEmitter {
  private readonly _clients: Set<BrowserClient> = new Set();
  private _supervisorListener: ((record: DaemonEventRecord) => void) | null =
    null;
  private _closed = false;

  constructor(private readonly _supervisor: DaemonSupervisor) {
    super();
    this._subscribe();
  }

  // -------------------------------------------------------------------------
  // Public API.
  // -------------------------------------------------------------------------

  /** Read-only view of connected clients. Useful for tests. */
  get clients(): ReadonlySet<BrowserClient> {
    return this._clients;
  }

  /** Number of currently-tracked clients (incl. ones without a project_id). */
  get size(): number {
    return this._clients.size;
  }

  /** True if the bridge has been closed; new clients are rejected. */
  get closed(): boolean {
    return this._closed;
  }

  /**
   * Register a new browser socket. Wires `message` / `close` listeners
   * that forward commands and clean up on disconnect.
   *
   * If the bridge is already closed, the socket is closed immediately
   * with code 1011 ("server error") and no listeners are attached.
   */
  add(socket: BrowserSocket): void {
    if (this._closed) {
      try {
        socket.close(1011, "bridge_closed");
      } catch {
        // ignore
      }
      return;
    }

    const client: BrowserClient = {
      socket,
      projectId: null,
      outstandingCount: 0,
    };

    const onMessage = (data: unknown): void => {
      this._handleClientMessage(client, data);
    };
    const onClose = (code: number, reason: string): void => {
      this._removeClient(client, code, reason);
    };

    socket.on("message", onMessage);
    socket.on("close", onClose);

    this._clients.add(client);
    this.emit("client-added", client);
  }

  /**
   * Tear down the bridge: stop listening for daemon events, drop every
   * connected client, reject pending dialog_turn promises. Idempotent.
   *
   * Tests typically do NOT call this — the bridge lives for the full
   * process lifetime. `add()` checks `_closed` so a late `add()` after
   * a `close()` is a no-op rather than a leak.
   */
  close(): void {
    if (this._closed) return;
    this._closed = true;
    if (this._supervisorListener) {
      this._supervisor.off(
        "daemon-event",
        this._supervisorListener as never,
      );
      this._supervisorListener = null;
    }
    for (const client of Array.from(this._clients)) {
      this._removeClient(client, 1001, "bridge_closed");
    }
    this.emit("bridge-closed", {});
  }

  // -------------------------------------------------------------------------
  // Internal: subscribe to daemon events.
  // -------------------------------------------------------------------------

  private _subscribe(): void {
    this._supervisorListener = (record: DaemonEventRecord): void => {
      this._fanOut(record);
    };
    this._supervisor.on("daemon-event", this._supervisorListener);
  }

  // -------------------------------------------------------------------------
  // Internal: fan-out.
  // -------------------------------------------------------------------------

  /**
   * Send `record` to every connected client whose `projectId` matches.
   * Clients without a projectId set yet are skipped (they would discard
   * it anyway). A `ws.send` throwing on a dead client removes it
   * from the set so the next event doesn't waste time on it.
   *
   * Wire shape mirrors `DaemonEventEnvelope` (types.ts:188) so the
   * browser sees the same envelope the daemon emitted.
   */
  private _fanOut(record: DaemonEventRecord): void {
    const envelope: Record<string, unknown> = {
      v: 1,
      type: "event",
      event: record.event,
      project_id: record.project_id,
      feature_id: record.feature_id,
      payload: record.payload,
    };
    const text = JSON.stringify(envelope);
    let targetCount = 0;
    for (const client of Array.from(this._clients)) {
      if (client.projectId !== record.project_id) continue;
      if (client.socket.readyState !== WS_OPEN) {
        this._clients.delete(client);
        this.emit("client-removed", client);
        continue;
      }
      try {
        client.socket.send(text);
        targetCount += 1;
      } catch {
        // Closed mid-write — drop on the next tick.
        this._clients.delete(client);
        this.emit("client-removed", client);
      }
    }
    this.emit("event-fanned-out", record, targetCount);
  }

  // -------------------------------------------------------------------------
  // Internal: inbound browser frame.
  // -------------------------------------------------------------------------

  private _handleClientMessage(client: BrowserClient, data: unknown): void {
    if (this._closed) return;
    const text =
      typeof data === "string"
        ? data
        : Buffer.isBuffer(data)
          ? data.toString("utf-8")
          : Array.isArray(data)
            ? Buffer.concat(data).toString("utf-8")
            : String(data);

    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      logger.debug(
        "node",
        "browser_ws_malformed_json",
        `dropped non-JSON frame from browser (${text.length} chars)`,
        { project_id: client.projectId ?? undefined },
      );
      return;
    }
    const envelope = parseBrowserCommand(parsed);
    if (!envelope) {
      logger.debug(
        "node",
        "browser_ws_invalid_envelope",
        "dropped frame with invalid envelope shape",
        { project_id: client.projectId ?? undefined },
      );
      return;
    }

    // Stamp the client's project on first frame. Future frames from
    // the same client that change project_id are accepted (the daemon
    // re-validates) — useful when the browser switches projects in a
    // SPA without reconnecting.
    if (client.projectId === null) {
      client.projectId = envelope.project_id;
    }

    void this._dispatchCommand(client, envelope);
  }

  private async _dispatchCommand(
    client: BrowserClient,
    envelope: BrowserCommandEnvelope,
  ): Promise<void> {
    const cmd: BrowserCommandType = envelope.type;
    this.emit("command-forwarded", envelope);

    if (cmd === "dialog_turn") {
      await this._forwardDialogTurn(client, envelope);
      return;
    }
    // start_feature | stop_feature | retry_feature — fire-and-forget.
    const payload: Record<string, unknown> = {
      project_id: envelope.project_id,
      feature_id: envelope.feature_id,
    };
    if (envelope.hint !== undefined) payload["hint"] = envelope.hint;

    try {
      this._supervisor.sendCommand(cmd, payload);
    } catch (err) {
      // Daemon not running — surface to the originating client.
      this._sendReply(client, "0", false, "daemon_unavailable", String(err));
    }
  }

  /**
   * `dialog_turn` is the only command that needs a reply. We delegate
   * to `supervisor.request()` which already handles req_id allocation,
   * timeout (per HEDDLE_DAEMON_REQUEST_TIMEOUT_MS), and DaemonUnavailable
   * error mapping. The returned `data` is the daemon's reply payload
   * (whatever shape `dialog_turn` produces — currently a chat echo per
   * feat-028 routes.py; feat-044 will add the chat/work branch).
   */
  private async _forwardDialogTurn(
    client: BrowserClient,
    envelope: BrowserCommandEnvelope,
  ): Promise<void> {
    const payload: Record<string, unknown> = {
      project_id: envelope.project_id,
      message: envelope.message ?? "",
    };
    try {
      const data = await this._supervisor.request(
        "dialog_turn",
        payload,
      );
      this._sendReply(client, "__dialog_turn__", true, "", data);
    } catch (err) {
      const code =
        (err as unknown as { code?: string }).code ?? "request_timeout";
      this._sendReply(
        client,
        "__dialog_turn__",
        false,
        code,
        err instanceof Error ? err.message : String(err),
      );
    }
  }

  // -------------------------------------------------------------------------
  // Internal: client lifecycle.
  // -------------------------------------------------------------------------

  private _removeClient(
    client: BrowserClient,
    code: number,
    reason: string,
  ): void {
    if (!this._clients.delete(client)) return;
    logger.debug(
      "node",
      "browser_ws_closed",
      `browser disconnected code=${code} reason=${reason || "(empty)"}`,
      { project_id: client.projectId ?? undefined },
    );
    // Best-effort close. The socket may already be CLOSED (the
    // browser-initiated disconnect path), in which case ws.close()
    // is a no-op. We still call it so the bridge-initiated
    // teardown path (close() method) propagates the right code.
    try {
      client.socket.close(code, reason);
    } catch {
      // ignore — socket may be already torn down
    }
    this.emit("client-removed", client);
  }

  // -------------------------------------------------------------------------
  // Internal: reply delivery back to the originating client.
  // -------------------------------------------------------------------------

  private _sendReply(
    client: BrowserClient,
    reqId: string,
    ok: boolean,
    errCode: string,
    payload: unknown,
  ): void {
    if (client.socket.readyState !== WS_OPEN) return;
    const envelope: Record<string, unknown> = {
      v: 1,
      type: "response",
      req_id: reqId,
      ok,
    };
    if (ok) {
      envelope["data"] = payload;
    } else {
      envelope["error"] = { code: errCode, message: String(payload) };
    }
    try {
      client.socket.send(JSON.stringify(envelope));
      this.emit("reply-sent", reqId, ok);
    } catch {
      this._clients.delete(client);
    }
  }
}

// Silence unused-import warning when `WSWebSocket` is only a type
// reference for the file-level JSDoc.
void ({} as WSWebSocket);