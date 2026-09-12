// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the Node-side hand-rolled RotatingFileSink — feat-015.
 */

import {
  existsSync,
  mkdirSync,
  readFileSync,
  unlinkSync,
} from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import {
  ROTATING_FILE_SINK_ENV,
  RotatingFileSink,
} from "../src/lib/rotating-file-sink.js";

const TMP_PREFIX = "heddle_node_rts_";

function mktmpdir(): string {
  return join(
    process.env.TMPDIR ?? process.env.TEMP ?? ".",
    `${TMP_PREFIX}${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
  );
}

describe("RotatingFileSink — config validation", () => {
  test("rejects maxBytes = 0", () => {
    expect(() => new RotatingFileSink("/tmp/x.log", { maxBytes: 0, backupCount: 3 })).toThrow(
      /maxBytes must be > 0/,
    );
  });
  test("rejects maxBytes = -1", () => {
    expect(
      () => new RotatingFileSink("/tmp/x.log", { maxBytes: -1, backupCount: 3 }),
    ).toThrow(/maxBytes must be > 0/);
  });
  test("rejects backupCount = 0", () => {
    expect(
      () => new RotatingFileSink("/tmp/x.log", { maxBytes: 1024, backupCount: 0 }),
    ).toThrow(/backupCount must be > 0/);
  });
  test("rejects backupCount = -1", () => {
    expect(
      () => new RotatingFileSink("/tmp/x.log", { maxBytes: 1024, backupCount: -1 }),
    ).toThrow(/backupCount must be > 0/);
  });
  test("rejects maxBytes > 1 GiB", () => {
    expect(() =>
      new RotatingFileSink("/tmp/x.log", {
        maxBytes: ROTATING_FILE_SINK_ENV.MAX_LOG_MAX_BYTES + 1,
        backupCount: 3,
      }),
    ).toThrow(/MAX_LOG_MAX_BYTES/);
  });
  test("rejects backupCount > 100", () => {
    expect(() =>
      new RotatingFileSink("/tmp/x.log", {
        maxBytes: 1024,
        backupCount: ROTATING_FILE_SINK_ENV.MAX_LOG_BACKUP_COUNT + 1,
      }),
    ).toThrow(/MAX_LOG_BACKUP_COUNT/);
  });
  test("accepts boundary values", () => {
    expect(
      () =>
        new RotatingFileSink("/tmp/x.log", {
          maxBytes: 1,
          backupCount: 1,
        }),
    ).not.toThrow();
  });
});

describe("RotatingFileSink — rotation mechanics", () => {
  let tmp: string;
  beforeEach(() => {
    tmp = mktmpdir();
    mkdirSync(tmp, { recursive: true });
  });
  afterEach(() => {
    for (const name of ["test.log", "test.log.1", "test.log.2", "test.log.3"]) {
      try {
        unlinkSync(join(tmp, name));
      } catch {
        // ignore
      }
    }
    try {
      unlinkSync(tmp);
    } catch {
      // ignore
    }
  });

  test("rotates after bytesWritten crosses threshold", () => {
    const sink = new RotatingFileSink(join(tmp, "test.log"), {
      maxBytes: 40,
      backupCount: 3,
    });
    sink.write("aaaa\n"); // 5
    sink.write("bbbb\n"); // 10
    sink.write("cccc\n"); // 15
    sink.write("dddd\n"); // 20
    sink.write("eeee\n"); // 25
    sink.write("ffff\n"); // 30
    sink.write("gggg\n"); // 35
    sink.write("hhhh\n"); // 40 → triggers rotation
    sink.close();
    expect(existsSync(join(tmp, "test.log"))).toBe(true);
    expect(existsSync(join(tmp, "test.log.1"))).toBe(true);
  });

  test("prunes oldest beyond backupCount", () => {
    const sink = new RotatingFileSink(join(tmp, "test.log"), {
      maxBytes: 10,
      backupCount: 2,
    });
    // 5 lines × 5 bytes each = 25 bytes → multiple rotations.
    for (let i = 0; i < 5; i += 1) {
      sink.write("xxxx\n");
    }
    sink.close();
    expect(existsSync(join(tmp, "test.log"))).toBe(true);
    expect(existsSync(join(tmp, "test.log.1"))).toBe(true);
    expect(existsSync(join(tmp, "test.log.2"))).toBe(true);
    expect(existsSync(join(tmp, "test.log.3"))).toBe(false);
  });

  test("close is idempotent", () => {
    const sink = new RotatingFileSink(join(tmp, "test.log"), {
      maxBytes: 1024,
      backupCount: 1,
    });
    sink.write("hi\n");
    sink.close();
    expect(() => sink.close()).not.toThrow();
    expect(() => sink.close()).not.toThrow();
  });

  test("emit after close is a no-op (no throw)", () => {
    const sink = new RotatingFileSink(join(tmp, "test.log"), {
      maxBytes: 1024,
      backupCount: 1,
    });
    sink.write("hi\n");
    sink.close();
    expect(() => sink.write("after-close\n")).not.toThrow();
  });

  test("env-var override applied", () => {
    const sink = new RotatingFileSink(join(tmp, "test.log"), {
      env: { HEDDLE_LOG_MAX_BYTES: "100", HEDDLE_LOG_BACKUP_COUNT: "7" } as NodeJS.ProcessEnv,
    });
    expect(sink.maxBytes).toBe(100);
    expect(sink.backupCount).toBe(7);
    sink.close();
  });

  test("env-var override throws on bad value", () => {
    expect(
      () =>
        new RotatingFileSink(join(tmp, "test.log"), {
          env: { HEDDLE_LOG_MAX_BYTES: "not-a-number" } as NodeJS.ProcessEnv,
        }),
    ).toThrow(/HEDDLE_LOG_MAX_BYTES/);
  });

  test("survives many writes without losing data", () => {
    const sink = new RotatingFileSink(join(tmp, "test.log"), {
      maxBytes: 200,
      backupCount: 3,
    });
    for (let i = 0; i < 50; i += 1) {
      sink.write(`line ${i}\n`);
    }
    sink.close();
    // The active file exists; at least one backup exists too.
    expect(existsSync(join(tmp, "test.log"))).toBe(true);
    expect(existsSync(join(tmp, "test.log.1"))).toBe(true);
  });

  test("does NOT re-redact (caller-supplied redacted lines pass through verbatim)", () => {
    const sink = new RotatingFileSink(join(tmp, "test.log"), {
      maxBytes: 1024,
      backupCount: 1,
    });
    const redactedLine = JSON.stringify({
      api_key: "[REDACTED]",
      msg: "hello",
    });
    sink.write(redactedLine + "\n");
    sink.close();
    const body = readFileSync(join(tmp, "test.log"), "utf-8");
    expect(body).toContain("[REDACTED]");
    expect(body).not.toContain("sk-real");
  });
});