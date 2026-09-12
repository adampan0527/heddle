// SPDX-License-Identifier: Apache-2.0
/**
 * WebSocket client with reconnect wrapper — feat-033 / TECH.md T-027.
 *
 * Thin glue (~150 LOC incl. types) around the native `WebSocket` API:
 *
 *   - State machine: `connecting` / `open` / `reconnecting` / `closed`.
 *   - Exponential backoff reconnect: 1s -> 2s -> 4s -> 8s, capped at 30s.
 *   - Outgoing message buffer while disconnected, capped at 1000 entries
 *     OR 30s age — whichever expires first. Oldest entries are dropped
 *     when the cap is hit.
 *   - Emits `state` events so the UI can render a connection-status pill.
 *
 * The client is intentionally framework-agnostic (no React imports); a
 * small `useWebSocket` hook in `App.tsx` wraps the singleton for React.
 *
 * @example
 *   const client = new WsClient({ url: "/ws" });
 *   client.on("state", (s) => console.log("ws state", s));
 *   client.on("message", (m) => console.log("recv", m));
 *   client.connect();
 *   client.send({ type: "ping" });
 */

export type WsState = "connecting" | "open" | "reconnecting" | "closed";

export interface WsClientOptions {
  /** WebSocket URL. In dev, the Vite proxy rewrites `/ws` to the backend. */
  url: string;
  /** Initial backoff delay in ms. Default 1000. */
  baseDelayMs?: number;
  /** Maximum backoff delay in ms. Default 30000. */
  maxDelayMs?: number;
  /** Max outgoing messages buffered while disconnected. Default 1000. */
  maxBufferSize?: number;
  /** Max age (ms) of buffered messages before drop. Default 30000. */
  maxBufferAgeMs?: number;
  /** Optional protocol list forwarded to native `WebSocket`. */
  protocols?: string | string[];
  /** Injectable WebSocket factory (tests). Defaults to the global. */
  socketFactory?: (url: string, protocols?: string | string[]) => WebSocket;
  /** Injectable clock for buffer-age expiry (tests). Defaults to Date.now. */
  now?: () => number;
  /** Injectable timer scheduler (tests). Defaults to global setTimeout/clearTimeout. */
  setTimeoutFn?: (cb: () => void, ms: number) => unknown;
  clearTimeoutFn?: (handle: unknown) => void;
}

export type WsClientEvent = "state" | "message" | "error";

export type WsClientHandler<S = unknown> = (payload: S) => void;

interface BufferedMessage {
  payload: string;
  enqueuedAt: number;
}

const DEFAULTS = {
  baseDelayMs: 1000,
  maxDelayMs: 30000,
  maxBufferSize: 1000,
  maxBufferAgeMs: 30000,
} as const;

/**
 * Browser-side WebSocket client with reconnect + buffering.
 *
 * `connect()` is idempotent when already open / connecting. `close()` is
 * terminal: once called the client will not reconnect.
 */
export class WsClient {
  private readonly opts: Required<
    Pick<WsClientOptions, "url" | "baseDelayMs" | "maxDelayMs" | "maxBufferSize" | "maxBufferAgeMs">
  > &
    Omit<WsClientOptions, "url" | "baseDelayMs" | "maxDelayMs" | "maxBufferSize" | "maxBufferAgeMs">;

  private socket: WebSocket | null = null;
  private state: WsState = "closed";
  private attempt = 0;
  private retryHandle: unknown = null;
  private buffer: BufferedMessage[] = [];
  private manuallyClosed = false;

  private readonly listeners: { [E in WsClientEvent]: WsClientHandler<unknown>[] } = {
    state: [],
    message: [],
    error: [],
  };

  constructor(options: WsClientOptions) {
    // exactOptionalPropertyTypes: assign optional fields through locals
    // so a missing-vs-undefined distinction doesn't break the type.
    const protocols = options.protocols;
    const socketFactory = options.socketFactory;
    const now = options.now;
    const setTimeoutFn = options.setTimeoutFn;
    const clearTimeoutFn = options.clearTimeoutFn;
    this.opts = {
      url: options.url,
      baseDelayMs: options.baseDelayMs ?? DEFAULTS.baseDelayMs,
      maxDelayMs: options.maxDelayMs ?? DEFAULTS.maxDelayMs,
      maxBufferSize: options.maxBufferSize ?? DEFAULTS.maxBufferSize,
      maxBufferAgeMs: options.maxBufferAgeMs ?? DEFAULTS.maxBufferAgeMs,
      ...(protocols !== undefined ? { protocols } : {}),
      ...(socketFactory !== undefined ? { socketFactory } : {}),
      ...(now !== undefined ? { now } : {}),
      ...(setTimeoutFn !== undefined ? { setTimeoutFn } : {}),
      ...(clearTimeoutFn !== undefined ? { clearTimeoutFn } : {}),
    };
  }

  /** Current connection state. */
  getState(): WsState {
    return this.state;
  }

  /** Current outgoing buffer length (visible for tests / status UI). */
  getBufferLength(): number {
    return this.buffer.length;
  }

  /** Register a listener for `state` / `message` / `error`. */
  on<E extends WsClientEvent>(event: E, handler: WsClientHandler<unknown>): void {
    this.listeners[event].push(handler as WsClientHandler<unknown>);
  }

  /** Remove a previously-registered listener. No-op if not found. */
  off<E extends WsClientEvent>(event: E, handler: WsClientHandler<unknown>): void {
    const arr = this.listeners[event];
    const idx = arr.indexOf(handler as WsClientHandler<unknown>);
    if (idx >= 0) arr.splice(idx, 1);
  }

  /** Open the underlying socket. Idempotent if already open/connecting. */
  connect(): void {
    if (this.manuallyClosed) return;
    if (this.state === "open" || this.state === "connecting") return;
    this.openSocket();
  }

  /**
   * Queue a message for delivery. Serializes non-strings to JSON.
   * When the socket is open the message is sent immediately; otherwise
   * it is buffered (subject to size/age caps).
   */
  send(payload: unknown): void {
    if (this.manuallyClosed) return;
    const text = typeof payload === "string" ? payload : JSON.stringify(payload);
    if (this.state === "open" && this.socket) {
      this.socket.send(text);
      return;
    }
    this.enqueue(text);
  }

  /**
   * Permanently close the client. Cancels any pending reconnect and
   * clears the outgoing buffer. After `close()` the client cannot be
   * reopened; construct a new instance instead.
   */
  close(): void {
    this.manuallyClosed = true;
    this.clearRetry();
    this.buffer = [];
    if (this.socket) {
      // Detach handlers so the onclose -> reconnect path is inert.
      this.socket.onopen = null;
      this.socket.onmessage = null;
      this.socket.onerror = null;
      this.socket.onclose = null;
      try {
        this.socket.close();
      } catch {
        // Native close() can throw if the socket is already gone.
      }
      this.socket = null;
    }
    this.setState("closed");
  }

  // ---- private ----

  private setState(next: WsState): void {
    if (this.state === next) return;
    this.state = next;
    for (const h of this.listeners.state) {
      try {
        h(next);
      } catch {
        // listeners must not break the state machine
      }
    }
  }

  private emit<E extends WsClientEvent>(event: E, payload: unknown): void {
    for (const h of this.listeners[event]) {
      try {
        h(payload);
      } catch {
        // swallow listener errors
      }
    }
  }

  private now(): number {
    return this.opts.now ? this.opts.now() : Date.now();
  }

  private openSocket(): void {
    this.setState(this.attempt === 0 ? "connecting" : "reconnecting");
    const factory =
      this.opts.socketFactory ??
      ((url: string, p?: string | string[]) => new WebSocket(url, p));
    let ws: WebSocket;
    try {
      ws = factory(this.opts.url, this.opts.protocols);
    } catch (err) {
      this.emit("error", err);
      this.scheduleReconnect();
      return;
    }
    this.socket = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.setState("open");
      this.drainBuffer();
    };
    ws.onmessage = (ev: MessageEvent) => {
      const data = typeof ev.data === "string" ? ev.data : "";
      if (data) this.emit("message", data);
    };
    ws.onerror = (ev: Event) => {
      this.emit("error", ev);
    };
    ws.onclose = () => {
      this.socket = null;
      if (this.manuallyClosed) {
        this.setState("closed");
        return;
      }
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.manuallyClosed) return;
    this.setState("reconnecting");
    const delay = Math.min(
      this.opts.baseDelayMs * 2 ** this.attempt,
      this.opts.maxDelayMs,
    );
    this.attempt += 1;
    const scheduler =
      this.opts.setTimeoutFn ??
      ((cb: () => void, ms: number) => setTimeout(cb, ms));
    this.retryHandle = scheduler(() => {
      this.retryHandle = null;
      if (this.manuallyClosed) return;
      this.openSocket();
    }, delay);
  }

  private clearRetry(): void {
    if (this.retryHandle == null) return;
    const clearer =
      this.opts.clearTimeoutFn ??
      ((h: unknown) => clearTimeout(h as ReturnType<typeof setTimeout>));
    clearer(this.retryHandle);
    this.retryHandle = null;
  }

  private enqueue(text: string): void {
    this.pruneBuffer();
    this.buffer.push({ payload: text, enqueuedAt: this.now() });
    while (this.buffer.length > this.opts.maxBufferSize) {
      this.buffer.shift();
    }
  }

  private pruneBuffer(): void {
    const cutoff = this.now() - this.opts.maxBufferAgeMs;
    // Drop entries older than the cutoff. Typically small, but if many
    // entries were queued during a long outage we may need to drop a
    // large prefix.
    let drop = 0;
    while (drop < this.buffer.length && this.buffer[drop]!.enqueuedAt < cutoff) {
      drop += 1;
    }
    if (drop > 0) this.buffer.splice(0, drop);
  }

  private drainBuffer(): void {
    this.pruneBuffer();
    if (!this.socket || this.state !== "open") return;
    const drained = this.buffer;
    this.buffer = [];
    for (const { payload } of drained) {
      try {
        this.socket.send(payload);
      } catch {
        // If send throws mid-drain, requeue the rest and bail.
        this.buffer = drained.slice(drained.indexOf({ payload, enqueuedAt: 0 }));
        return;
      }
    }
  }
}

/**
 * Singleton wired to the Vite-proxied backend URL. Lazy: the socket is
 * not opened until the first `connect()` call (typically from React's
 * mount effect). Tests should construct their own `WsClient` instances.
 */
let singleton: WsClient | null = null;

export function getDefaultWsClient(): WsClient {
  if (!singleton) {
    singleton = new WsClient({ url: "/ws" });
  }
  return singleton;
}
