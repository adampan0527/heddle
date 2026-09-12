// SPDX-License-Identifier: Apache-2.0
/**
 * Unit tests for logger.ts. Migrated from `node:test` to Vitest globals
 * in feat-032 so the whole web package runs under one framework.
 */

import { Writable } from "node:stream";
import { describe, expect, test } from "vitest";

import { logger, redact, type LogFields } from "./logger.ts";

/** Capture lines written to a Writable stream. */
function makeCapture(): { stream: Writable; lines: string[] } {
  const lines: string[] = [];
  const stream = new Writable({
    write(chunk, _enc, cb) {
      lines.push(chunk.toString());
      cb();
    },
  });
  return { stream, lines };
}

describe("redact", () => {
  test("api_key top-level", () => {
    const r = redact({ api_key: "secret-value-12345", other: "ok" });
    expect((r as Record<string, unknown>).api_key).toBe("[REDACTED]");
    expect((r as Record<string, unknown>).other).toBe("ok");
  });

  test("API_KEY uppercase", () => {
    const r = redact({ API_KEY: "secret-value" });
    expect((r as Record<string, unknown>).API_KEY).toBe("[REDACTED]");
  });

  test("apiKey camelCase", () => {
    const r = redact({ apiKey: "secret-value" });
    expect((r as Record<string, unknown>).apiKey).toBe("[REDACTED]");
  });

  test("api-key dashed", () => {
    const r = redact({ "api-key": "secret-value" });
    expect((r as Record<string, unknown>)["api-key"]).toBe("[REDACTED]");
  });

  test("secret field variants", () => {
    const r = redact({ secret: "shh", client_secret: "shh2" });
    expect((r as Record<string, unknown>).secret).toBe("[REDACTED]");
    expect((r as Record<string, unknown>).client_secret).toBe("[REDACTED]");
  });

  test("token field variants", () => {
    const r = redact({ token: "t", access_token: "t2", idToken: "t3" });
    expect((r as Record<string, unknown>).token).toBe("[REDACTED]");
    expect((r as Record<string, unknown>).access_token).toBe("[REDACTED]");
    expect((r as Record<string, unknown>).idToken).toBe("[REDACTED]");
  });

  test("nested object/array values", () => {
    const obj = {
      outer: "keep",
      inner_dict: { api_key: "secret", keep: "yes" },
      inner_list: [{ token: "bad" }, { keep: "yes" }],
    };
    const r = redact(obj) as Record<string, unknown>;
    expect(r.outer).toBe("keep");
    expect((r.inner_dict as Record<string, unknown>).api_key).toBe("[REDACTED]");
    expect((r.inner_dict as Record<string, unknown>).keep).toBe("yes");
    expect(
      ((r.inner_list as unknown[])[0] as Record<string, unknown>).token,
    ).toBe("[REDACTED]");
    expect(
      ((r.inner_list as unknown[])[1] as Record<string, unknown>).keep,
    ).toBe("yes");
  });

  test("non-sensitive fields unchanged", () => {
    const r = redact({ feature_id: "feat-001", msg: "hello" });
    expect((r as Record<string, unknown>).feature_id).toBe("feat-001");
    expect((r as Record<string, unknown>).msg).toBe("hello");
  });
});

describe("emit", () => {
  test("basic info event to stderr", () => {
    const { stream, lines } = makeCapture();
    const original = process.stderr.write.bind(process.stderr);
    process.stderr.write = stream.write.bind(stream) as typeof process.stderr.write;
    try {
      logger.info("daemon", "started", "daemon started OK");
    } finally {
      process.stderr.write = original;
    }
    expect(lines).toHaveLength(1);
    const line = lines[0]!.replace(/\n$/, "");
    const parsed = JSON.parse(line);
    expect(parsed.level).toBe("info");
    expect(parsed.component).toBe("daemon");
    expect(parsed.event).toBe("started");
    expect(parsed.msg).toBe("daemon started OK");
    expect(parsed.project_id).toBeNull();
    expect(parsed.feature_id).toBeNull();
    expect(Number.isNaN(Date.parse(parsed.ts))).toBe(false);
  });

  test("project_id, feature_id, and extra fields propagate", () => {
    const { stream, lines } = makeCapture();
    const original = process.stderr.write.bind(process.stderr);
    process.stderr.write = stream.write.bind(stream) as typeof process.stderr.write;
    try {
      logger.warn("node", "feature_failed", "agent stopped", {
        project_id: "proj-1",
        feature_id: "feat-007",
        attempt: 3,
      });
    } finally {
      process.stderr.write = original;
    }
    const parsed = JSON.parse(lines[0]!.replace(/\n$/, ""));
    expect(parsed.project_id).toBe("proj-1");
    expect(parsed.feature_id).toBe("feat-007");
    expect(parsed.attempt).toBe(3);
    expect(parsed.component).toBe("node");
  });

  test("api_key field is redacted in serialized line", () => {
    const { stream, lines } = makeCapture();
    const original = process.stderr.write.bind(process.stderr);
    process.stderr.write = stream.write.bind(stream) as typeof process.stderr.write;
    try {
      logger.error("daemon", "auth_failed", "bad key", {
        api_key: "sk-supersecret-1234",
        project_id: "proj-1",
      } satisfies LogFields);
    } finally {
      process.stderr.write = original;
    }
    const raw = lines[0]!;
    expect(raw.includes("sk-supersecret-1234")).toBe(false);
    const parsed = JSON.parse(raw.replace(/\n$/, ""));
    expect(parsed.api_key).toBe("[REDACTED]");
    expect(parsed.project_id).toBe("proj-1");
  });

  test("100 events produce 100 lines, each parseable", () => {
    const { stream, lines } = makeCapture();
    const original = process.stderr.write.bind(process.stderr);
    process.stderr.write = stream.write.bind(stream) as typeof process.stderr.write;
    try {
      for (let i = 0; i < 100; i++) {
        logger.info("daemon", "tick", `event ${i}`, { index: i });
      }
    } finally {
      process.stderr.write = original;
    }
    expect(lines).toHaveLength(100);
    for (let i = 0; i < 100; i++) {
      const parsed = JSON.parse(lines[i]!.replace(/\n$/, ""));
      expect(parsed.index).toBe(i);
      expect(parsed.level).toBe("info");
      expect(parsed.component).toBe("daemon");
      expect(parsed.event).toBe("tick");
    }
  });
});
