// SPDX-License-Identifier: Apache-2.0
/**
 * Unit tests for logger.ts. Uses node:test (built into Node 20+ — no extra deps).
 */

import test from "node:test";
import assert from "node:assert/strict";
import { Writable } from "node:stream";
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

test("redact: api_key top-level", () => {
  const r = redact({ api_key: "secret-value-12345", other: "ok" });
  assert.equal((r as Record<string, unknown>).api_key, "[REDACTED]");
  assert.equal((r as Record<string, unknown>).other, "ok");
});

test("redact: API_KEY uppercase", () => {
  const r = redact({ API_KEY: "secret-value" });
  assert.equal((r as Record<string, unknown>).API_KEY, "[REDACTED]");
});

test("redact: apiKey camelCase", () => {
  const r = redact({ apiKey: "secret-value" });
  assert.equal((r as Record<string, unknown>).apiKey, "[REDACTED]");
});

test("redact: api-key dashed", () => {
  const r = redact({ "api-key": "secret-value" });
  assert.equal((r as Record<string, unknown>)["api-key"], "[REDACTED]");
});

test("redact: secret field variants", () => {
  const r = redact({ secret: "shh", client_secret: "shh2" });
  assert.equal((r as Record<string, unknown>).secret, "[REDACTED]");
  assert.equal((r as Record<string, unknown>).client_secret, "[REDACTED]");
});

test("redact: token field variants", () => {
  const r = redact({ token: "t", access_token: "t2", idToken: "t3" });
  assert.equal((r as Record<string, unknown>).token, "[REDACTED]");
  assert.equal((r as Record<string, unknown>).access_token, "[REDACTED]");
  assert.equal((r as Record<string, unknown>).idToken, "[REDACTED]");
});

test("redact: nested object/array values", () => {
  const obj = {
    outer: "keep",
    inner_dict: { api_key: "secret", keep: "yes" },
    inner_list: [{ token: "bad" }, { keep: "yes" }],
  };
  const r = redact(obj) as Record<string, unknown>;
  assert.equal(r.outer, "keep");
  assert.equal((r.inner_dict as Record<string, unknown>).api_key, "[REDACTED]");
  assert.equal((r.inner_dict as Record<string, unknown>).keep, "yes");
  assert.equal(((r.inner_list as unknown[])[0] as Record<string, unknown>).token, "[REDACTED]");
  assert.equal(((r.inner_list as unknown[])[1] as Record<string, unknown>).keep, "yes");
});

test("redact: non-sensitive fields unchanged", () => {
  const r = redact({ feature_id: "feat-001", msg: "hello" });
  assert.equal((r as Record<string, unknown>).feature_id, "feat-001");
  assert.equal((r as Record<string, unknown>).msg, "hello");
});

test("emit: basic info event to stderr", () => {
  const { stream, lines } = makeCapture();
  const original = process.stderr.write.bind(process.stderr);
  process.stderr.write = stream.write.bind(stream) as typeof process.stderr.write;
  try {
    logger.info("daemon", "started", "daemon started OK");
  } finally {
    process.stderr.write = original;
  }
  assert.equal(lines.length, 1);
  const line = lines[0]!.replace(/\n$/, "");
  const parsed = JSON.parse(line);
  assert.equal(parsed.level, "info");
  assert.equal(parsed.component, "daemon");
  assert.equal(parsed.event, "started");
  assert.equal(parsed.msg, "daemon started OK");
  assert.equal(parsed.project_id, null);
  assert.equal(parsed.feature_id, null);
  // ts is ISO8601 and parseable
  assert.ok(!Number.isNaN(Date.parse(parsed.ts)));
});

test("emit: project_id, feature_id, and extra fields propagate", () => {
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
  assert.equal(lines.length, 1);
  const parsed = JSON.parse(lines[0]!.replace(/\n$/, ""));
  assert.equal(parsed.project_id, "proj-1");
  assert.equal(parsed.feature_id, "feat-007");
  assert.equal(parsed.attempt, 3);
  assert.equal(parsed.component, "node");
});

test("emit: api_key field is redacted in serialized line", () => {
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
  assert.ok(!raw.includes("sk-supersecret-1234"), "sensitive value must not appear in raw line");
  const parsed = JSON.parse(raw.replace(/\n$/, ""));
  assert.equal(parsed.api_key, "[REDACTED]");
  assert.equal(parsed.project_id, "proj-1");
});

test("emit: 100 events produce 100 lines, each parseable", () => {
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
  assert.equal(lines.length, 100);
  for (let i = 0; i < 100; i++) {
    const parsed = JSON.parse(lines[i]!.replace(/\n$/, ""));
    assert.equal(parsed.index, i);
    assert.equal(parsed.level, "info");
    assert.equal(parsed.component, "daemon");
    assert.equal(parsed.event, "tick");
  }
});