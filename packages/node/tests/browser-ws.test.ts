// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for feat-029's BrowserWsBridge.
 *
 * Uses a fake `BrowserSocket` (EventEmitter) and a fake `DaemonSupervisor`
 * (also EventEmitter-based) so no real WS / subprocess is needed.
 *
 * Coverage:
 *   1. add() registers a client and emits client-added.
 *   2. add() on a closed bridge closes the socket with 1011.
 *   3. close() drops every client and emits bridge-closed.
 *   4. Daemon event for matching project_id → fan-out to client.
 *   5. Daemon event for non-matching project_id → not delivered.
 *   6. Multiple clients on same project_id all receive the event.
 *   7. Client without project_id set yet (no inbound frame) is skipped.
 *   8. browser-sent `start_feature` → supervisor.sendCommand called
 *      with correct payload (fire-and-forget).
 *   9. browser-sent `dialog_turn` → supervisor.request awaited,
 *      reply envelope `{ok:true, data:...}` written back.
 *  10. browser-sent `dialog_turn` when daemon unavailable → reply is
 *      `{ok:false, error:{code:"daemon_unavailable",...}}`.
 *  11. Malformed JSON inbound frame → dropped, no crash.
 *  12. Invalid envelope shape → dropped.
 *  13. Unknown `type` → dropped.
 *  14. ws.close → client removed from set; subsequent events skip it.
 *  15. ws.send throwing (broken pipe) → client dropped on next fan-out.
 */

import { EventEmitter } from "node:events";

import { describe, expect, test, vi } from "vitest";

import { BrowserWsBridge, type BrowserSocket } from "../src/browser-ws.js";
import type { DaemonSupervisor } from "../src/supervisor.js";
import type { DaemonEventRecord } from "../src/protocol.js";

// ---------- fakes ----------

class FakeBrowserSocket extends EventEmitter {
  public sent: string[] = [];
  public readyState = 1; // OPEN
  public closedWith: { code: number; reason: string } | null = null;
  send(frame: string): void {
    if (this.readyState !== 1) {
      throw new Error("socket not OPEN");
    }
    this.sent.push(frame);
  }
  close(code = 1000, reason = ""): void {
    this.readyState = 3;
    this.closedWith = { code, reason };
    this.emit("close", code, reason);
  }
}

class FakeSupervisor extends EventEmitter {
  public sendCommandCalls: Array<{
    type: string;
    payload: Record<string, unknown>;
  }> = [];
  public requestCalls: Array<{
    type: string;
    payload: Record<string, unknown>;
  }> = [];
  public requestImpl:
    | ((type: string, payload: Record<string, unknown>) => Promise<unknown>)
    | null = null;

  sendCommand(type: string, payload: Record<string, unknown>): string {
    this.sendCommandCalls.push({ type, payload });
    return `${process.pid}-fake-${this.sendCommandCalls.length}`;
  }

  async request<T = unknown>(
    type: string,
    payload: Record<string, unknown>,
  ): Promise<T> {
    this.requestCalls.push({ type, payload });
    if (this.requestImpl) {
      return (await this.requestImpl(type, payload)) as T;
    }
    return {} as T;
  }
}

function fakeRecord(
  projectId: string,
  eventName: string = "feature_progress",
): DaemonEventRecord {
  return {
    event: eventName as DaemonEventRecord["event"],
    project_id: projectId,
    feature_id: null,
    payload: { step: 1, message: "ok" },
    received_at: Date.now(),
  };
}

// ---------- tests ----------

describe("BrowserWsBridge (feat-029)", () => {
  test("add() registers client and emits client-added", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();

    const added: unknown[] = [];
    bridge.on("client-added", (c) => added.push(c));

    bridge.add(sock);
    expect(bridge.size).toBe(1);
    expect(added).toHaveLength(1);

    bridge.close();
  });

  test("add() on closed bridge closes the socket with 1011", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    bridge.close();

    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    expect(sock.closedWith?.code).toBe(1011);
    expect(bridge.size).toBe(0);
  });

  test("close() drops every client and emits bridge-closed", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const s1 = new FakeBrowserSocket();
    const s2 = new FakeBrowserSocket();
    bridge.add(s1);
    bridge.add(s2);
    expect(bridge.size).toBe(2);

    let closedEmitted = 0;
    bridge.on("bridge-closed", () => (closedEmitted += 1));
    bridge.close();

    expect(bridge.size).toBe(0);
    expect(closedEmitted).toBe(1);
    expect(s1.closedWith?.code).toBe(1001);
    expect(s2.closedWith?.code).toBe(1001);
  });

  test("event for matching project_id is fanned out to client", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    // Stamp project_id by sending one valid frame first.
    sock.emit("message", JSON.stringify({
      v: 1,
      type: "dialog_turn",
      project_id: "proj-A",
      message: "hi",
    }));
    // Drain the dialog_turn (won't resolve cleanly; we'll ignore).
    // Wait a tick for the async dispatch to settle.
    return new Promise<void>((resolve) => {
      setImmediate(() => {
        sock.sent = []; // ignore the dialog_turn reply envelope
        sup.emit("daemon-event", fakeRecord("proj-A"));

        expect(sock.sent).toHaveLength(1);
        const env = JSON.parse(sock.sent[0] as string);
        expect(env["type"]).toBe("event");
        expect(env["event"]).toBe("feature_progress");
        expect(env["project_id"]).toBe("proj-A");
        bridge.close();
        resolve();
      });
    });
  });

  test("event for non-matching project_id is NOT delivered", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", JSON.stringify({
      v: 1,
      type: "dialog_turn",
      project_id: "proj-A",
      message: "hi",
    }));
    return new Promise<void>((resolve) => {
      setImmediate(() => {
        sock.sent = [];
        sup.emit("daemon-event", fakeRecord("proj-B"));

        expect(sock.sent).toHaveLength(0);
        bridge.close();
        resolve();
      });
    });
  });

  test("multiple clients on same project_id all receive the event", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const s1 = new FakeBrowserSocket();
    const s2 = new FakeBrowserSocket();
    bridge.add(s1);
    bridge.add(s2);
    for (const s of [s1, s2]) {
      s.emit("message", JSON.stringify({
        v: 1,
        type: "dialog_turn",
        project_id: "proj-A",
        message: "hi",
      }));
    }

    return new Promise<void>((resolve) => {
      setImmediate(() => {
        s1.sent = [];
        s2.sent = [];
        sup.emit("daemon-event", fakeRecord("proj-A"));
        expect(s1.sent.length).toBeGreaterThanOrEqual(1);
        expect(s2.sent.length).toBeGreaterThanOrEqual(1);
        bridge.close();
        resolve();
      });
    });
  });

  test("client with no project_id set yet is skipped during fan-out", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);
    // No inbound frame → projectId is null.

    sup.emit("daemon-event", fakeRecord("proj-A"));
    expect(sock.sent).toHaveLength(0);

    bridge.close();
  });

  test("browser-sent start_feature → supervisor.sendCommand", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", JSON.stringify({
      v: 1,
      type: "start_feature",
      project_id: "proj-A",
      feature_id: "feat-007",
    }));

    expect(sup.sendCommandCalls).toHaveLength(1);
    expect(sup.sendCommandCalls[0]).toEqual({
      type: "start_feature",
      payload: { project_id: "proj-A", feature_id: "feat-007" },
    });
    bridge.close();
  });

  test("browser-sent retry_feature forwards hint when present", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", JSON.stringify({
      v: 1,
      type: "retry_feature",
      project_id: "proj-A",
      feature_id: "feat-007",
      hint: "fix the lint error",
    }));

    expect(sup.sendCommandCalls).toHaveLength(1);
    expect(sup.sendCommandCalls[0]?.payload).toEqual({
      project_id: "proj-A",
      feature_id: "feat-007",
      hint: "fix the lint error",
    });
    bridge.close();
  });

  test("browser-sent dialog_turn → supervisor.request awaited, reply envelope sent", async () => {
    const sup = new FakeSupervisor();
    sup.requestImpl = async () => ({
      project_id: "proj-A",
      kind: "chat",
      text: "echo",
      drafts: [],
    });
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", JSON.stringify({
      v: 1,
      type: "dialog_turn",
      project_id: "proj-A",
      message: "hello",
    }));

    // The bridge awaits; wait for it to settle.
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));

    expect(sup.requestCalls).toHaveLength(1);
    expect(sup.requestCalls[0]?.payload).toEqual({
      project_id: "proj-A",
      message: "hello",
    });
    // Find the response envelope (type=response, ok=true).
    const responseFrames = sock.sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .filter((env) => env["type"] === "response");
    expect(responseFrames).toHaveLength(1);
    expect(responseFrames[0]?.["ok"]).toBe(true);
    expect(responseFrames[0]?.["data"]).toEqual({
      project_id: "proj-A",
      kind: "chat",
      text: "echo",
      drafts: [],
    });

    bridge.close();
  });

  test("dialog_turn daemon-unavailable error → ok:false envelope with daemon_unavailable code", async () => {
    const sup = new FakeSupervisor();
    sup.requestImpl = async () => {
      const e = new Error("daemon not running");
      (e as unknown as { code?: string }).code = "daemon_unavailable";
      throw e;
    };
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", JSON.stringify({
      v: 1,
      type: "dialog_turn",
      project_id: "proj-A",
      message: "hi",
    }));

    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));

    const responseFrames = sock.sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .filter((env) => env["type"] === "response");
    expect(responseFrames).toHaveLength(1);
    expect(responseFrames[0]?.["ok"]).toBe(false);
    const err = responseFrames[0]?.["error"] as Record<string, unknown>;
    expect(err["code"]).toBe("daemon_unavailable");

    bridge.close();
  });

  test("malformed JSON inbound frame is dropped without crash", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", "this is not json {");
    sock.emit("message", Buffer.from("also not json", "utf-8"));

    // No reply written (malformed frames don't trigger dialog_turn path).
    // Bridge still alive, client still attached.
    expect(bridge.size).toBe(1);
    bridge.close();
  });

  test("invalid envelope shape is dropped", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", JSON.stringify({ v: 1, type: "unknown_type", project_id: "p" }));
    sock.emit("message", JSON.stringify({ v: 2, type: "dialog_turn", project_id: "p", message: "x" }));
    sock.emit("message", JSON.stringify({ v: 1, type: "dialog_turn" })); // missing project_id
    sock.emit("message", JSON.stringify({ v: 1, type: "dialog_turn", project_id: "" })); // empty project_id
    sock.emit("message", JSON.stringify({ v: 1, type: "dialog_turn", project_id: "p" })); // missing message

    // None of these should call sendCommand or request.
    expect(sup.sendCommandCalls).toHaveLength(0);
    expect(sup.requestCalls).toHaveLength(0);
    bridge.close();
  });

  test("ws.close removes client; subsequent events skip it", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", JSON.stringify({
      v: 1,
      type: "dialog_turn",
      project_id: "proj-A",
      message: "hi",
    }));
    return new Promise<void>((resolve) => {
      setImmediate(() => {
        // Close the client.
        sock.close(1000, "bye");
        expect(bridge.size).toBe(0);

        sock.sent = [];
        sup.emit("daemon-event", fakeRecord("proj-A"));
        expect(sock.sent).toHaveLength(0);

        bridge.close();
        resolve();
      });
    });
  });

  test("ws.send throwing on a broken socket drops the client", () => {
    const sup = new FakeSupervisor();
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", JSON.stringify({
      v: 1,
      type: "dialog_turn",
      project_id: "proj-A",
      message: "hi",
    }));
    return new Promise<void>((resolve) => {
      setImmediate(() => {
        // Mark socket CLOSED so the next send throws.
        sock.readyState = 3;

        let removed = 0;
        bridge.on("client-removed", () => (removed += 1));
        sup.emit("daemon-event", fakeRecord("proj-A"));

        expect(bridge.size).toBe(0);
        expect(removed).toBeGreaterThanOrEqual(1);
        bridge.close();
        resolve();
      });
    });
  });

  test("sendCommand throws → daemon_unavailable reply envelope", () => {
    const sup = new FakeSupervisor();
    // Override sendCommand to throw (simulates supervisor not in RUNNING).
    vi.spyOn(sup, "sendCommand").mockImplementation(() => {
      throw new Error("not running");
    });
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", JSON.stringify({
      v: 1,
      type: "start_feature",
      project_id: "proj-A",
      feature_id: "feat-007",
    }));

    // Reply envelope written back to the client.
    const responseFrames = sock.sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .filter((env) => env["type"] === "response");
    expect(responseFrames.length).toBeGreaterThanOrEqual(1);
    const last = responseFrames[responseFrames.length - 1];
    expect(last?.["ok"]).toBe(false);

    bridge.close();
  });

  test("BrowserSocket.send on a closed socket via _sendReply also drops the client", () => {
    const sup = new FakeSupervisor();
    sup.requestImpl = async () => {
      // Simulate a slow reply.
      return new Promise((resolve) =>
        setTimeout(() => resolve({ ok: true, data: { ping: 1 } }), 5),
      );
    };
    const bridge = new BrowserWsBridge(sup as unknown as DaemonSupervisor);
    const sock = new FakeBrowserSocket();
    bridge.add(sock);

    sock.emit("message", JSON.stringify({
      v: 1,
      type: "dialog_turn",
      project_id: "proj-A",
      message: "hi",
    }));

    return new Promise<void>((resolve) => {
      setTimeout(() => {
        // Close mid-reply.
        sock.readyState = 3;
        sock.close(1006, "");
        expect(bridge.size).toBe(0);
        bridge.close();
        resolve();
      }, 10);
    });
  });
});