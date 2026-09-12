// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the feat-026 Fastify skeleton + feat-028 TypeBox wiring.
 *
 * What lives here (kept small per HARNESS "one responsibility per
 * file"):
 *   1. The buildServer skeleton + loopback gate (isLoopback).
 *   2. The TypeBox type-provider is wired (every registered route
 *      returns a JSON envelope on bad input).
 *
 * What moved out:
 *   - The 501 placeholder tests (deleted in feat-028: there are no
 *     placeholders left — every route is real).
 *   - Per-route happy paths live in tests/routes/{projects,features,
 *     dialog}.test.ts so a failure points at the specific area.
 *   - The subprocess refusal tests for ``main.ts`` stay here because
 *     they cover the cross-cutting "HEDDLE_NODE_HOST=0.0.0.0
 *     refuses" invariant which is independent of any route.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { beforeAll, describe, expect, test } from "vitest";

import {
  DEFAULT_HOST,
  buildServer,
  isLoopback,
} from "../src/server.js";

const REPO_ROOT = resolve(__dirname, "..", "..", "..");
const ENTRY_PATH = resolve(REPO_ROOT, "packages/node/src/main.ts");
const HAS_DEPS = existsSync(resolve(REPO_ROOT, "packages/node/node_modules"));

beforeAll(() => {
  if (!HAS_DEPS) {
    throw new Error(
      "packages/node/node_modules is missing — run `pnpm install` at the repo root before tests",
    );
  }
});

// ---------- in-process: skeleton + TypeBox wiring ----------


describe("buildServer", () => {
  test("builds without crashing when no supervisor is attached", async () => {
    const app = await buildServer();
    try {
      // The shape test: TypeBox provider installed means the route
      // exists (even if it returns 503 because no supervisor).
      const res = await app.inject({ method: "GET", url: "/api/projects" });
      expect(res.statusCode).toBe(503);
      const body = res.json();
      expect(body.ok).toBe(false);
      expect(body.error.code).toBe("daemon_unavailable");
    } finally {
      await app.close();
    }
  });

  test("TypeBox validates POST /api/projects body", async () => {
    const app = await buildServer();
    try {
      const res = await app.inject({
        method: "POST",
        url: "/api/projects",
        payload: {}, // missing required `path`
      });
      expect(res.statusCode).toBe(400);
      // Fastify renders validation errors as {statusCode, error, message}
      // by default; the route plugin's own mapping is tested separately.
      const body = res.json();
      expect(typeof body).toBe("object");
    } finally {
      await app.close();
    }
  });
});

// ---------- pure: isLoopback gate ----------


describe("isLoopback", () => {
  test("accepts canonical loopback literals", () => {
    expect(isLoopback("127.0.0.1")).toBe(true);
    expect(isLoopback("localhost")).toBe(true);
    expect(isLoopback("::1")).toBe(true);
  });

  test("accepts the full 127.0.0.0/8 block", () => {
    expect(isLoopback("127.0.0.2")).toBe(true);
    expect(isLoopback("127.255.255.254")).toBe(true);
  });

  test("rejects non-loopback hosts", () => {
    expect(isLoopback("0.0.0.0")).toBe(false);
    expect(isLoopback("192.168.1.1")).toBe(false);
    expect(isLoopback("10.0.0.1")).toBe(false);
    expect(isLoopback("example.com")).toBe(false);
    expect(isLoopback("")).toBe(false);
  });
});

// ---------- subprocess: refusal of non-loopback host ----------


describe("main.ts subprocess behaviour", () => {
  test(
    "refuses non-loopback HEDDLE_NODE_HOST with exit code 1",
    async () => {
      const proc = spawn(
        process.execPath,
        ["--import", "tsx", ENTRY_PATH],
        {
          env: {
            ...process.env,
            HEDDLE_NODE_HOST: "0.0.0.0",
            HEDDLE_NODE_PORT: "5174",
          },
        },
      );
      let stderr = "";
      proc.stderr.setEncoding("utf8");
      proc.stderr.on("data", (d) => {
        stderr += d;
      });
      const code: number = await new Promise((resolveCode) => {
        proc.on("exit", resolveCode);
      });
      expect(code).toBe(1);
      expect(stderr).toContain("non_loopback_host_refused");
    },
    20000,
  );

  test(
    "rejects invalid HEDDLE_NODE_PORT with exit code 1",
    async () => {
      const proc = spawn(
        process.execPath,
        ["--import", "tsx", ENTRY_PATH],
        {
          env: {
            ...process.env,
            HEDDLE_NODE_HOST: DEFAULT_HOST,
            HEDDLE_NODE_PORT: "not-a-number",
          },
        },
      );
      let stderr = "";
      proc.stderr.setEncoding("utf8");
      proc.stderr.on("data", (d) => {
        stderr += d;
      });
      const code: number = await new Promise((resolveCode) => {
        proc.on("exit", resolveCode);
      });
      expect(code).toBe(1);
      expect(stderr).toContain("invalid_port");
    },
    20000,
  );
});