// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for feat-030's daemon-event stream + sendCommand().
 *
 * Mirrors `supervisor-request.test.ts`'s harness: drive the supervisor
 * into RUNNING with a `FakeWs` (EventEmitter), exercise the inbound
 * dispatcher and outbound sender without a real daemon subprocess.
 *
 * Coverage:
 *   1. Inbound envelope WITHOUT req_id → emits "daemon-event" with the
 *      parsed fields (event, project_id, feature_id, payload, received_at).
 *   2. Inbound envelope WITH req_id → still routes to the pending-request
 *      Map (backward compat with feat-028).
 *   3. Inbound envelope missing required fields → silently dropped.
 *   4. sendCommand() flushes an outbound frame with the right shape.
 *   5. sendCommand() throws DaemonUnavailableError when not RUNNING.
 *   6. `received_at` is a finite number close to Date.now().
 */

import { EventEmitter } from "node:events";

import { describe, expect, test } from "vitest";

import { DaemonSupervisor } from "../src/supervisor.js";
import { DaemonUnavailableError } from "../src/types.js";
import type { DaemonEventRecord } from "../src/protocol.js";

// ---------- fake ws (matches supervisor-request.test.ts shape) ----------

class FakeWs extends EventEmitter {
  public sent: string[] = [];
  public readyState = 1;
  send(frame: string): void {
    this.sent.push(frame);
  }
  close(): void {
    this.readyState = 3;
    this.emit("close", 1000, "");
  }
  removeAllListeners(): void {
    super.removeAllListeners();
  }
}

// ---------- harness ----------

function buildRunningSupervisor(): {
  supervisor: DaemonSupervisor;
  ws: FakeWs;
} {
  const supervisor = new DaemonSupervisor({
    restartDelayMs: 10,
    pingIntervalMs: 999999,
    pingTimeoutMs: 999999,
    maxMissedPings: 999,
    connectTimeoutMs: 50,
    shutdownGraceMs: 10,
  });
  const ws = new FakeWs();
  // Drive the supervisor into RUNNING with our fake ws. Production
  // start() is covered by integration tests; the private mutation is
  // the documented test seam (see supervisor-request.test.ts).
  (supervisor as unknown as { _ws: FakeWs })._ws = ws;
  (supervisor as unknown as { _state: string })._state = "RUNNING";
  // Wire the inbound handler — the production code attaches this in
  // _tryConnectOnce, not in the public API. The test seam mirrors
  // that wiring without re-running the connect loop.
  ws.on("message", (data: unknown) => {
    (
      supervisor as unknown as { _handleInboundMessage: (d: unknown) => void }
    )._handleInboundMessage(data);
  });
  return { supervisor, ws };
}

function emitInbound(ws: FakeWs, obj: unknown): void {
  ws.emit("message", JSON.stringify(obj));
}

// ---------- tests ----------

describe("feat-030 daemon-event stream", () => {
  test("inbound envelope without req_id emits daemon-event", () => {
    const { supervisor, ws } = buildRunningSupervisor();
    const seen: DaemonEventRecord[] = [];
    supervisor.on("daemon-event", (r) => seen.push(r));

    emitInbound(ws, {
      v: 1,
      type: "event",
      event: "feature_progress",
      project_id: "proj-1",
      feature_id: "feat-007",
      payload: { attempt_id: "a-1", step: 3, message: "wrote file" },
    });

    expect(seen).toHaveLength(1);
    expect(seen[0]?.event).toBe("feature_progress");
    expect(seen[0]?.project_id).toBe("proj-1");
    expect(seen[0]?.feature_id).toBe("feat-007");
    expect(seen[0]?.payload).toEqual({
      attempt_id: "a-1",
      step: 3,
      message: "wrote file",
    });
    expect(typeof seen[0]?.received_at).toBe("number");
    expect(Math.abs((seen[0]?.received_at ?? 0) - Date.now())).toBeLessThan(
      1000,
    );
  });

  test("inbound envelope with req_id still routes to pending-request map", async () => {
    const { supervisor, ws } = buildRunningSupervisor();
    const eventSeen: DaemonEventRecord[] = [];
    supervisor.on("daemon-event", (r) => eventSeen.push(r));

    const reqPromise = supervisor.request<{ hello: string }>(
      "ping",
      {},
      { timeoutMs: 1000 },
    );

    // Find the req_id we just sent.
    const sentFrame = JSON.parse(ws.sent[ws.sent.length - 1] as string);
    const reqId = sentFrame["req_id"];
    expect(typeof reqId).toBe("string");

    // Respond with a matching envelope — should resolve the promise.
    emitInbound(ws, {
      v: 1,
      type: "ping_response",
      req_id: reqId,
      ok: true,
      data: { hello: "world" },
    });

    const data = await reqPromise;
    expect(data).toEqual({ hello: "world" });
    expect(eventSeen).toHaveLength(0); // req_id present → not an event
  });

  test("inbound envelope missing required fields is silently dropped", () => {
    const { supervisor, ws } = buildRunningSupervisor();
    const seen: DaemonEventRecord[] = [];
    supervisor.on("daemon-event", (r) => seen.push(r));

    // No `event` field → drop.
    emitInbound(ws, {
      v: 1,
      type: "event",
      project_id: "proj-1",
      payload: {},
    });
    // No `project_id` → drop.
    emitInbound(ws, {
      v: 1,
      type: "event",
      event: "feature_done",
      payload: {},
    });
    // Both present → emit.
    emitInbound(ws, {
      v: 1,
      type: "event",
      event: "feature_done",
      project_id: "proj-2",
      payload: { attempt_id: "a-2" },
    });

    expect(seen).toHaveLength(1);
    expect(seen[0]?.project_id).toBe("proj-2");
  });

  test("sendCommand flushes an outbound frame with the right shape", () => {
    const { supervisor, ws } = buildRunningSupervisor();
    const before = ws.sent.length;
    const reqId = supervisor.sendCommand("start_feature", {
      project_id: "proj-1",
      feature_id: "feat-030",
    });
    expect(ws.sent).toHaveLength(before + 1);
    const sent = JSON.parse(ws.sent[ws.sent.length - 1] as string);
    expect(sent).toEqual({
      v: 1,
      type: "start_feature",
      req_id: reqId,
      project_id: "proj-1",
      feature_id: "feat-030",
    });
  });

  test("sendCommand throws DaemonUnavailableError when not RUNNING", () => {
    const supervisor = new DaemonSupervisor({
      restartDelayMs: 10,
      pingIntervalMs: 999999,
      pingTimeoutMs: 999999,
      maxMissedPings: 999,
      connectTimeoutMs: 50,
      shutdownGraceMs: 10,
    });
    // Leave state at IDLE (default).
    expect(() => supervisor.sendCommand("start_feature", {})).toThrow(
      DaemonUnavailableError,
    );
  });

  test("feature_id is null when missing or empty", () => {
    const { supervisor, ws } = buildRunningSupervisor();
    const seen: DaemonEventRecord[] = [];
    supervisor.on("daemon-event", (r) => seen.push(r));

    emitInbound(ws, {
      v: 1,
      type: "event",
      event: "feature_done",
      project_id: "proj-1",
      payload: { attempt_id: "a-3" },
    });
    emitInbound(ws, {
      v: 1,
      type: "event",
      event: "feature_done",
      project_id: "proj-1",
      feature_id: "",
      payload: { attempt_id: "a-4" },
    });

    expect(seen).toHaveLength(2);
    expect(seen[0]?.feature_id).toBeNull();
    expect(seen[1]?.feature_id).toBeNull();
  });
});
