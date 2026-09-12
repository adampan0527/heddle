// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the feat-028 REST route plugins.
 *
 * Three plugins under test (one file per HARNESS "one responsibility
 * per file", but the assertion surface overlaps enough that a single
 * harness keeps things compact): projects.ts, features.ts, dialog.ts.
 *
 * Each test mounts the route plugin on a fresh buildServer() with a
 * fake supervisor that records ``request`` calls and emits canned
 * responses. The route's job — TypeBox validation, daemon-code → HTTP
 * status mapping, response shape — is exercised without spinning up
 * a real daemon (that's test_routes_smoke.py on the Python side).
 */

import { EventEmitter } from "node:events";

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  test,
} from "vitest";

import { buildServer } from "../src/server.js";
import { DaemonSupervisor } from "../src/supervisor.js";

// ---------- fake supervisor ----------

class FakeSupervisor extends EventEmitter {
  /** Per-call canned response. Test sets this before each call. */
  public nextResponses: Array<{
    ok: boolean;
    data?: unknown;
    error?: { code: string; message: string };
  }> = [];
  public callLog: Array<{
    envelopeType: string;
    payload: Record<string, unknown>;
  }> = [];

  /** Compatibility shim: real supervisor's request() signature. */
  async request<TData = unknown>(
    envelopeType: string,
    payload: Record<string, unknown> = {},
  ): Promise<TData> {
    this.callLog.push({ envelopeType, payload });
    const next = this.nextResponses.shift();
    if (!next) {
      throw new Error(
        `FakeSupervisor: no canned response for ${envelopeType}`,
      );
    }
    if (next.ok) {
      return next.data as TData;
    }
    const e = new Error(next.error?.message ?? "daemon error");
    (e as unknown as { code: string }).code =
      next.error?.code ?? "internal_error";
    throw e;
  }

  get state(): string {
    return "RUNNING";
  }
}

// ---------- helpers ----------

async function makeApp() {
  const supervisor = new FakeSupervisor();
  // Cast: FakeSupervisor structurally matches the DaemonSupervisor
  // surface the routes actually use (.request + .state). We do not
  // implement the full type — tests rely on the structural shape.
  const app = await buildServer({
    supervisor: supervisor as unknown as DaemonSupervisor,
  });
  return { app, supervisor };
}

// ---------- projects routes ----------

describe("routes/projects", () => {
  let app: Awaited<ReturnType<typeof buildServer>>;
  let supervisor: FakeSupervisor;
  beforeEach(async () => {
    ({ app, supervisor } = await makeApp());
  });
  afterEach(async () => {
    await app.close();
  });

  test("GET /api/projects forwards to daemon and returns 200", async () => {
    supervisor.nextResponses.push({
      ok: true,
      data: {
        projects: [
          {
            id: "p1",
            name: "n",
            path: "/p",
            added_at: "x",
            last_accessed_at: "y",
          },
        ],
      },
    });
    const res = await app.inject({ method: "GET", url: "/api/projects" });
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.ok).toBe(true);
    expect(body.data.projects).toHaveLength(1);
    expect(supervisor.callLog[0].envelopeType).toBe("project_list");
  });

  test("POST /api/projects forwards path + name; returns 201", async () => {
    supervisor.nextResponses.push({
      ok: true,
      data: {
        project: {
          id: "p1",
          name: "n",
          path: "/p",
          added_at: "x",
          last_accessed_at: "y",
        },
      },
    });
    const res = await app.inject({
      method: "POST",
      url: "/api/projects",
      payload: { path: "/p", name: "n" },
    });
    expect(res.statusCode).toBe(201);
    expect(res.json().data.project.name).toBe("n");
    expect(supervisor.callLog[0].envelopeType).toBe("project_add");
    expect(supervisor.callLog[0].payload.path).toBe("/p");
  });

  test("POST /api/projects body validation: missing path → 400", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/projects",
      payload: {},
    });
    expect(res.statusCode).toBe(400);
    // TypeBox validation may render either Fastify's default
    // {statusCode, error, message} shape or our
    // {ok:false, error:{code,message}} envelope depending on
    // whether a custom error handler is wired. Either way the
    // supervisor must not have been called.
    expect(supervisor.callLog).toHaveLength(0);
  });

  test("DELETE /api/projects/:id maps not_found → 404", async () => {
    supervisor.nextResponses.push({
      ok: false,
      error: { code: "not_found", message: "no such project" },
    });
    const res = await app.inject({
      method: "DELETE",
      url: "/api/projects/abc",
    });
    expect(res.statusCode).toBe(404);
    expect(res.json().error.code).toBe("not_found");
  });

  test("DELETE /api/projects/:id maps conflict → 409", async () => {
    supervisor.nextResponses.push({
      ok: false,
      error: { code: "conflict", message: "already registered" },
    });
    const res = await app.inject({
      method: "DELETE",
      url: "/api/projects/dup",
    });
    expect(res.statusCode).toBe(409);
  });
});

// ---------- features routes ----------

describe("routes/features", () => {
  let app: Awaited<ReturnType<typeof buildServer>>;
  let supervisor: FakeSupervisor;
  beforeEach(async () => {
    ({ app, supervisor } = await makeApp());
  });
  afterEach(async () => {
    await app.close();
  });

  test("GET /api/projects/:id/features forwards project_id", async () => {
    supervisor.nextResponses.push({
      ok: true,
      data: { project_id: "p1", features: [] },
    });
    const res = await app.inject({
      method: "GET",
      url: "/api/projects/p1/features",
    });
    expect(res.statusCode).toBe(200);
    expect(supervisor.callLog[0].envelopeType).toBe("feature_list");
    expect(supervisor.callLog[0].payload.project_id).toBe("p1");
  });

  test("POST transition with bad action → 400 (TypeBox)", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/projects/p1/features/feat-A/transition",
      payload: { action: "explode" },
    });
    expect(res.statusCode).toBe(400);
    expect(supervisor.callLog).toHaveLength(0);
  });

  test("POST transition with unknown feature → 404", async () => {
    supervisor.nextResponses.push({
      ok: false,
      error: { code: "not_found", message: "feature feat-ZZ not found" },
    });
    const res = await app.inject({
      method: "POST",
      url: "/api/projects/p1/features/feat-ZZ/transition",
      payload: { action: "retry" },
    });
    expect(res.statusCode).toBe(404);
    expect(res.json().error.code).toBe("not_found");
  });

  test("POST transition retry happy path", async () => {
    // Provide a full feature row so the TypeBox response schema passes.
    supervisor.nextResponses.push({
      ok: true,
      data: {
        project_id: "p1",
        feature_id: "feat-A",
        action: "retry",
        feature: {
          id: "feat-A",
          category: "functional",
          description: "test",
          steps: ["s1", "s2"],
          status: "in_progress",
          priority: "medium",
          depends_on: [],
          attempts: [],
          kind: "feature",
          fixes: null,
          enhances: null,
          superseded_by: null,
          implementation_model: null,
        },
      },
    });
    const res = await app.inject({
      method: "POST",
      url: "/api/projects/p1/features/feat-A/transition",
      payload: { action: "retry" },
    });
    expect(res.statusCode).toBe(200);
    expect(res.json().data.feature.status).toBe("in_progress");
  });
});

// ---------- dialog routes ----------

describe("routes/dialog", () => {
  let app: Awaited<ReturnType<typeof buildServer>>;
  let supervisor: FakeSupervisor;
  beforeEach(async () => {
    ({ app, supervisor } = await makeApp());
  });
  afterEach(async () => {
    await app.close();
  });

  test("POST dialog with chat stub response → 200", async () => {
    supervisor.nextResponses.push({
      ok: true,
      data: {
        project_id: "p1",
        kind: "chat",
        text: "echo: hi there",
      },
    });
    const res = await app.inject({
      method: "POST",
      url: "/api/projects/p1/dialog",
      payload: { message: "hi there" },
    });
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.data.kind).toBe("chat");
    expect(body.data.text).toBe("echo: hi there");
    expect(supervisor.callLog[0].envelopeType).toBe("dialog_turn");
  });

  test("POST dialog with empty message → 400", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/projects/p1/dialog",
      payload: { message: "" },
    });
    expect(res.statusCode).toBe(400);
  });

  test("POST dialog with oversize message → 400", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/projects/p1/dialog",
      payload: { message: "x".repeat(8001) },
    });
    expect(res.statusCode).toBe(400);
  });

  test("POST dialog maps not_found → 404", async () => {
    supervisor.nextResponses.push({
      ok: false,
      error: { code: "not_found", message: "no such project" },
    });
    const res = await app.inject({
      method: "POST",
      url: "/api/projects/nope/dialog",
      payload: { message: "hi" },
    });
    expect(res.statusCode).toBe(404);
  });
});

// ---------- supervisor-missing 503 path ----------

describe("routes/* without supervisor", () => {
  test("all routes return 503 + daemon_unavailable envelope", async () => {
    const app = await buildServer();
    try {
      const cases: Array<{
        method: "GET" | "POST" | "DELETE";
        url: string;
        body?: unknown;
      }> = [
        { method: "GET", url: "/api/projects" },
        { method: "POST", url: "/api/projects", body: { path: "/p" } },
        { method: "DELETE", url: "/api/projects/abc" },
        { method: "GET", url: "/api/projects/p1/features" },
        {
          method: "POST",
          url: "/api/projects/p1/features/feat-A/transition",
          body: { action: "retry" },
        },
        { method: "POST", url: "/api/projects/p1/dialog", body: { message: "hi" } },
      ];
      for (const c of cases) {
        const res = await app.inject({
          method: c.method,
          url: c.url,
          ...(c.body !== undefined ? { payload: c.body } : {}),
        });
        expect(res.statusCode).toBe(503);
        expect(res.json().error.code).toBe("daemon_unavailable");
      }
    } finally {
      await app.close();
    }
  });
});