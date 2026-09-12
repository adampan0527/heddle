// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the feat-026 Fastify skeleton.
 *
 * Three concerns per feature spec:
 *   1. The three placeholder routes return 501 (in-process via
 *      fastify.inject()).
 *   2. The host gate refuses non-loopback binds at config time
 *      (subprocess test, since the refusal lives in main.ts and exits
 *      the process).
 *   3. The default config binds 127.0.0.1 successfully and serves 501
 *      to a real fetch from the bound port (subprocess test).
 *
 * Subprocess tests use child_process.spawn so the exit code is
 * observable; an in-process test of the refusal path would silently
 * short-circuit when process.exit is mocked.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { beforeAll, describe, expect, test } from "vitest";

import {
  DEFAULT_HOST,
  PLACEHOLDER_ROUTES,
  buildServer,
  isLoopback,
} from "../src/server.js";

// Path to the source entry point. We spawn `node --import tsx` so
// the test works without a prior tsc build step.
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

// ---------- in-process: 501 stub routes ----------


describe("buildServer", () => {
  test("each placeholder route returns 501", async () => {
    const app = await buildServer();
    try {
      for (const path of PLACEHOLDER_ROUTES) {
        const res = await app.inject({ method: "GET", url: path });
        expect(res.statusCode).toBe(501);
        const body = res.json();
        expect(body.error).toBe("not_implemented");
        expect(body.path).toBe(path);
      }
    } finally {
      await app.close();
    }
  });

  test("POST and DELETE also hit the same 501 handler", async () => {
    const app = await buildServer();
    try {
      for (const method of ["POST", "DELETE"] as const) {
        const res = await app.inject({
          method,
          url: "/api/projects",
        });
        expect(res.statusCode).toBe(501);
      }
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
            HEDDLE_NODE_HOST: "127.0.0.1",
            HEDDLE_NODE_PORT: "not-a-port",
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

  test(
    "binds 127.0.0.1 and serves 501 over real fetch",
    async () => {
      // Random ephemeral test port (14000-14999) to avoid CI collisions.
      const port = 14000 + Math.floor(Math.random() * 1000);
      const proc = spawn(
        process.execPath,
        ["--import", "tsx", ENTRY_PATH],
        {
          env: {
            ...process.env,
            HEDDLE_NODE_HOST: DEFAULT_HOST,
            HEDDLE_NODE_PORT: String(port),
          },
        },
      );
      let stderr = "";
      proc.stderr.setEncoding("utf8");
      proc.stderr.on("data", (d) => {
        stderr += d;
      });
      let procExited: { code: number | null } | null = null;
      proc.on("exit", (code) => {
        procExited = { code };
      });

      try {
        await new Promise<void>((resolveReady, rejectReady) => {
          const timer = setTimeout(() => {
            rejectReady(
              new Error(
                `server failed to start listening within 10s. stderr=${stderr}`,
              ),
            );
          }, 10000);
          const check = () => {
            if (procExited !== null) {
              clearTimeout(timer);
              rejectReady(
                new Error(
                  `server exited early with code=${procExited?.code}; stderr=${stderr}`,
                ),
              );
              return;
            }
            if (stderr.includes('"event":"listening"')) {
              clearTimeout(timer);
              resolveReady();
            } else {
              setTimeout(check, 50);
            }
          };
          check();
        });

        const res = await fetch(`http://127.0.0.1:${port}/api/projects`);
        expect(res.status).toBe(501);
        const body = (await res.json()) as { error: string; path: string };
        expect(body.error).toBe("not_implemented");
        expect(body.path).toBe("/api/projects");
      } finally {
        proc.kill("SIGTERM");
      }
    },
    25000,
  );
});