// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for ``DaemonSupervisor.request()`` — feat-028.
 *
 * We bypass the production ``start()`` path (which spawns a real
 * daemon subprocess) by manually driving the supervisor into a
 * RUNNING-equivalent state with a fake ``ws``. The fake captures
 * every outbound frame and lets the test emit inbound ``message``
 * events to drive the request/response plumbing. This keeps the
 * test fast + hermetic while still exercising the full
 * pending-request map + timeout + correlation logic.
 */

import { EventEmitter } from "node:events";

import { afterEach, describe, expect, test } from "vitest";

import { DaemonSupervisor } from "../src/supervisor.js";
import {
  DaemonRequestTimeoutError,
  DaemonUnavailableError,
  resetCachedTimeout,
} from "../src/types.js";

// ---------- fake ws ----------

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

interface HarnessOpts {
  requestTimeoutMs?: number;
}

class Harness {
  public supervisor: DaemonSupervisor;
  public ws: FakeWs;

  private constructor(supervisor: DaemonSupervisor, ws: FakeWs) {
    this.supervisor = supervisor;
    this.ws = ws;
  }

  static async create(opts: HarnessOpts = {}): Promise<Harness> {
    if (opts.requestTimeoutMs !== undefined) {
      process.env["HEDDLE_DAEMON_REQUEST_TIMEOUT_MS"] =
        String(opts.requestTimeoutMs);
      resetCachedTimeout();
    }
    const supervisor = new DaemonSupervisor({
      restartDelayMs: 10,
      pingIntervalMs: 999999,
      pingTimeoutMs: 999999,
      maxMissedPings: 999,
      connectTimeoutMs: 50,
      shutdownGraceMs: 10,
    });
    const ws = new FakeWs();
    const harness = new Harness(supervisor, ws);
    // Drive the supervisor into RUNNING with our fake ws attached.
    // The private state mutation is intentional and documented; the
    // production start() flow is covered by the supervisor's own
    // integration tests (which spawn a real daemon).
    (supervisor as unknown as { _ws: FakeWs })._ws = ws;
    (supervisor as unknown as { _state: string })._state = "RUNNING";
    ws.on("message", (data: unknown) => {
      (supervisor as unknown as {
        _handleInboundMessage: (d: unknown) => void;
      })._handleInboundMessage(data);
    });
    return harness;
  }

  reqIdOf(envelopeType: string, index: number = -1): string {
    // Find the n-th (0-based) outbound frame of envelopeType;
    // index=-1 (default) is the LAST one (most recent).
    let seen = -1;
    for (let i = 0; i < this.ws.sent.length; i++) {
      const frame = JSON.parse(this.ws.sent[i]) as {
        type: string;
        req_id: string;
      };
      if (frame.type === envelopeType) {
        if (index < 0) {
          // Keep scanning; we want the LAST match.
          seen = i;
        } else {
          seen++;
          if (seen === index) return frame.req_id;
        }
      }
    }
    if (seen >= 0) {
      const frame = JSON.parse(this.ws.sent[seen]) as { req_id: string };
      return frame.req_id;
    }
    throw new Error(`no frame of type ${envelopeType}`);
  }

  respond(payload: Record<string, unknown>, type?: string): void {
    // Default: respond to the last sent frame.
    const last = this.ws.sent[this.ws.sent.length - 1];
    const reqId = (JSON.parse(last) as { req_id: string }).req_id;
    void type;
    this.ws.emit("message", JSON.stringify({ v: 1, req_id: reqId, ...payload }));
  }

  async settle(): Promise<void> {
    await new Promise((r) => setImmediate(r));
  }
}

// ---------- tests ----------

describe("DaemonSupervisor.request", () => {
  let harness: Harness;
  afterEach(async () => {
    if (harness) await harness.supervisor.stop().catch(() => undefined);
    delete process.env["HEDDLE_DAEMON_REQUEST_TIMEOUT_MS"];
    resetCachedTimeout();
  });

  test("rejects with DaemonUnavailableError when not in RUNNING", async () => {
    const sup = new DaemonSupervisor();
    await expect(sup.request("project_list")).rejects.toBeInstanceOf(
      DaemonUnavailableError,
    );
  });

  test("happy-path round-trip resolves with parsed data", async () => {
    harness = await Harness.create();
    const p = harness.supervisor.request<{ projects: unknown[] }>(
      "project_list",
    );
    await harness.settle();
    harness.respond({ ok: true, data: { projects: [] } });
    await expect(p).resolves.toEqual({ projects: [] });
  });

  test("concurrent requests resolve independently", async () => {
    harness = await Harness.create();
    const p1 = harness.supervisor.request<{ a: number }>("cmd_a");
    const p2 = harness.supervisor.request<{ b: number }>("cmd_b");
    await harness.settle();
    const idA = harness.reqIdOf("cmd_a");
    const idB = harness.reqIdOf("cmd_b");
    harness.ws.emit(
      "message",
      JSON.stringify({ v: 1, req_id: idA, ok: true, data: { a: 1 } }),
    );
    harness.ws.emit(
      "message",
      JSON.stringify({ v: 1, req_id: idB, ok: true, data: { b: 2 } }),
    );
    await expect(p1).resolves.toEqual({ a: 1 });
    await expect(p2).resolves.toEqual({ b: 2 });
  });

  test("timeout rejects with DaemonRequestTimeoutError", async () => {
    harness = await Harness.create({ requestTimeoutMs: 30 });
    const p = harness.supervisor.request("never_answered");
    await expect(p).rejects.toBeInstanceOf(DaemonRequestTimeoutError);
  });

  test("ok:false envelope surfaces Error with .code attached", async () => {
    harness = await Harness.create();
    const p = harness.supervisor.request("project_list");
    await harness.settle();
    harness.respond({
      ok: false,
      error: { code: "not_found", message: "nope" },
    });
    await expect(p).rejects.toMatchObject({
      message: "nope",
      code: "not_found",
    });
  });

  test("unmatched req_id is silently dropped", async () => {
    harness = await Harness.create();
    // A response with an unknown req_id must not crash the supervisor
    // and must not affect any in-flight request (none here).
    harness.ws.emit(
      "message",
      JSON.stringify({ v: 1, req_id: "no-such-id", ok: true, data: {} }),
    );
    // No assertion — success is "didn't throw".
    await harness.settle();
  });
});