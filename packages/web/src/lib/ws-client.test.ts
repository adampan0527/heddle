// SPDX-License-Identifier: Apache-2.0
/**
 * Unit tests for the WebSocket client with reconnect wrapper (feat-033).
 *
 * The native `WebSocket` is mocked via a `MockSocket` factory that
 * implements the subset of the browser API our client touches
 * (`onopen`/`onmessage`/`onerror`/`onclose`/`send`/`close`). Time is
 * driven by `vi.useFakeTimers()` so backoff math is deterministic.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { WsClient, type WsState } from "./ws-client.ts";

class MockSocket {
  static instances: MockSocket[] = [];
  static reset(): void {
    MockSocket.instances = [];
  }

  public readyState = 0; // CONNECTING
  public sent: string[] = [];
  public closed = false;
  public onopen: ((ev: Event) => void) | null = null;
  public onmessage: ((ev: MessageEvent) => void) | null = null;
  public onerror: ((ev: Event) => void) | null = null;
  public onclose: ((ev: CloseEvent) => void) | null = null;

  constructor(public readonly url: string, _protocols?: string | string[]) {
    MockSocket.instances.push(this);
  }

  send(data: string): void {
    if (this.closed) throw new Error("send after close");
    this.sent.push(data);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3; // CLOSED
    // Defer to a microtask so callers can finish setup before close fires.
    queueMicrotask(() => this.onclose?.(new CloseEvent("close")));
  }

  // Test helpers ----

  /** Simulate the socket opening. */
  open(): void {
    this.readyState = 1; // OPEN
    this.onopen?.(new Event("open"));
  }

  /** Simulate the server closing the connection. */
  serverClose(): void {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.(new CloseEvent("close"));
  }

  /** Simulate the server sending a message. */
  receive(data: string): void {
    this.onmessage?.(new MessageEvent("message", { data }));
  }
}

function makeFactory() {
  return (url: string, protocols?: string | string[]) =>
    new MockSocket(url, protocols) as unknown as WebSocket;
}

beforeEach(() => {
  MockSocket.reset();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("WsClient — connect / state", () => {
  test("initial connect -> connecting -> open", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    const states: WsState[] = [];
    client.on("state", (s) => states.push(s as WsState));

    client.connect();
    expect(client.getState()).toBe("connecting");
    expect(MockSocket.instances).toHaveLength(1);

    MockSocket.instances[0]!.open();
    expect(client.getState()).toBe("open");
    expect(states).toEqual(["connecting", "open"]);
  });

  test("connect() is a no-op when already open/connecting", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    client.connect();
    client.connect();
    expect(MockSocket.instances).toHaveLength(1);
    MockSocket.instances[0]!.open();
    client.connect();
    expect(MockSocket.instances).toHaveLength(1);
  });

  test("close() prevents further reconnects", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    client.connect();
    const sock = MockSocket.instances[0]!;
    sock.open();
    client.close();
    expect(client.getState()).toBe("closed");
    // Buffer is cleared too.
    client.send({ after: "close" });
    expect(client.getBufferLength()).toBe(0);
  });
});

describe("WsClient — send / buffer", () => {
  test("send() while open writes directly to the socket", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    client.connect();
    MockSocket.instances[0]!.open();
    client.send({ type: "ping" });
    const sock = MockSocket.instances[0]! as unknown as MockSocket;
    expect(sock.sent).toEqual(['{"type":"ping"}']);
    expect(client.getBufferLength()).toBe(0);
  });

  test("send() while disconnected buffers and drains on reconnect", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    client.connect();
    MockSocket.instances[0]!.open();

    // Disconnect -> reconnecting.
    MockSocket.instances[0]!.serverClose();
    expect(client.getState()).toBe("reconnecting");

    // Queue two messages while disconnected.
    client.send("a");
    client.send("b");
    expect(client.getBufferLength()).toBe(2);

    // Advance to reconnect attempt and open.
    vi.advanceTimersByTime(1000);
    expect(MockSocket.instances).toHaveLength(2);
    MockSocket.instances[1]!.open();
    expect(client.getState()).toBe("open");

    const sock = MockSocket.instances[1]! as unknown as MockSocket;
    expect(sock.sent).toEqual(["a", "b"]);
    expect(client.getBufferLength()).toBe(0);
  });

  test("buffer cap of 1000 drops oldest", () => {
    const client = new WsClient({
      url: "/ws",
      socketFactory: makeFactory(),
      maxBufferSize: 1000,
      maxBufferAgeMs: 1_000_000,
    });
    // Connect & never open: stay in reconnecting forever.
    client.connect();
    MockSocket.instances[0]!.serverClose();

    for (let i = 0; i < 1500; i++) {
      client.send({ i });
    }
    expect(client.getBufferLength()).toBe(1000);

    // Open and inspect what was sent — the first 500 should have been dropped.
    vi.advanceTimersByTime(30_000);
    const sock = MockSocket.instances[MockSocket.instances.length - 1]! as unknown as MockSocket;
    sock.open();
    expect(sock.sent).toHaveLength(1000);
    expect(JSON.parse(sock.sent[0]!).i).toBe(500);
    expect(JSON.parse(sock.sent[999]!).i).toBe(1499);
  });

  test("buffer drops entries older than maxBufferAgeMs", () => {
    let fakeNow = 1_000_000;
    const client = new WsClient({
      url: "/ws",
      socketFactory: makeFactory(),
      now: () => fakeNow,
      maxBufferAgeMs: 30_000,
    });
    client.connect();
    MockSocket.instances[0]!.serverClose();

    client.send("old-1");
    vi.advanceTimersByTime(10_000);
    fakeNow += 10_000;
    client.send("old-2");
    vi.advanceTimersByTime(25_000);
    fakeNow += 25_000; // old-1 is now 35s old, old-2 is 25s old
    client.send("fresh");

    // Opening should drain only the non-stale entries.
    vi.advanceTimersByTime(30_000);
    const sock = MockSocket.instances[MockSocket.instances.length - 1]! as unknown as MockSocket;
    sock.open();
    expect(sock.sent).toEqual(["old-2", "fresh"]);
  });
});

describe("WsClient — reconnect backoff", () => {
  test("exponential backoff: 1s, 2s, 4s, 8s, capped at 30s", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    client.connect();
    MockSocket.instances[0]!.serverClose();
    expect(MockSocket.instances).toHaveLength(1);

    // Attempt #1 after 1s.
    vi.advanceTimersByTime(999);
    expect(MockSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(MockSocket.instances).toHaveLength(2);

    MockSocket.instances[1]!.serverClose();
    // Attempt #2 after 2s.
    vi.advanceTimersByTime(2000);
    expect(MockSocket.instances).toHaveLength(3);
    MockSocket.instances[2]!.serverClose();

    // Attempt #3 after 4s.
    vi.advanceTimersByTime(4000);
    expect(MockSocket.instances).toHaveLength(4);
    MockSocket.instances[3]!.serverClose();

    // Attempt #4 after 8s.
    vi.advanceTimersByTime(8000);
    expect(MockSocket.instances).toHaveLength(5);
    MockSocket.instances[4]!.serverClose();

    // Attempt #5 after 16s.
    vi.advanceTimersByTime(16000);
    expect(MockSocket.instances).toHaveLength(6);
    MockSocket.instances[5]!.serverClose();

    // Attempt #6 after 30s (capped, would be 32s without cap).
    vi.advanceTimersByTime(30000);
    expect(MockSocket.instances).toHaveLength(7);
    MockSocket.instances[6]!.serverClose();

    // Next attempt should also wait 30s (capped).
    vi.advanceTimersByTime(29999);
    expect(MockSocket.instances).toHaveLength(7);
    vi.advanceTimersByTime(1);
    expect(MockSocket.instances).toHaveLength(8);
  });

  test("attempt counter resets to 0 after a successful open", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    client.connect();
    MockSocket.instances[0]!.serverClose();

    vi.advanceTimersByTime(1000); // attempt 1
    MockSocket.instances[1]!.serverClose();

    vi.advanceTimersByTime(2000); // attempt 2
    MockSocket.instances[2]!.open(); // success

    // Next disconnect should restart at 1s, not continue from 4s.
    MockSocket.instances[2]!.serverClose();
    vi.advanceTimersByTime(999);
    expect(MockSocket.instances).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(MockSocket.instances).toHaveLength(4);
  });

  test("factory throwing triggers reconnect schedule", () => {
    const errors: unknown[] = [];
    const client = new WsClient({
      url: "/ws",
      socketFactory: () => {
        throw new Error("boom");
      },
    });
    client.on("error", (e) => errors.push(e));
    client.connect();
    expect(errors).toHaveLength(1);
    expect(client.getState()).toBe("reconnecting");

    // The scheduled retry will throw again on the next attempt.
    vi.advanceTimersByTime(1000);
    expect(errors).toHaveLength(2);
    expect(client.getState()).toBe("reconnecting");
  });
});

describe("WsClient — listeners", () => {
  test("state events fire in order across connect/disconnect/reconnect/close", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    const states: WsState[] = [];
    client.on("state", (s) => states.push(s as WsState));
    client.connect();
    MockSocket.instances[0]!.open();
    MockSocket.instances[0]!.serverClose();
    vi.advanceTimersByTime(1000);
    MockSocket.instances[1]!.open();
    client.close();
    expect(states).toEqual(["connecting", "open", "reconnecting", "open", "closed"]);
  });

  test("message events deliver server-sent payloads as strings", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    const msgs: string[] = [];
    client.on("message", (m) => msgs.push(m as string));
    client.connect();
    MockSocket.instances[0]!.open();
    MockSocket.instances[0]!.receive('{"type":"hello"}');
    MockSocket.instances[0]!.receive("plain");
    expect(msgs).toEqual(['{"type":"hello"}', "plain"]);
  });

  test("error events deliver native error events", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    const errs: unknown[] = [];
    client.on("error", (e) => errs.push(e));
    client.connect();
    MockSocket.instances[0]!.onerror?.(new Event("error"));
    expect(errs).toHaveLength(1);
  });

  test("off() removes a previously-registered listener", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    const states: WsState[] = [];
    const handler = (s: unknown) => states.push(s as WsState);
    client.on("state", handler);
    client.off("state", handler);
    client.connect();
    expect(states).toEqual([]);
  });

  test("a throwing listener does not break the state machine", () => {
    const client = new WsClient({ url: "/ws", socketFactory: makeFactory() });
    const states: WsState[] = [];
    client.on("state", () => {
      throw new Error("listener boom");
    });
    client.on("state", (s) => states.push(s as WsState));
    client.connect();
    expect(client.getState()).toBe("connecting");
    expect(states).toEqual(["connecting"]);
  });
});
