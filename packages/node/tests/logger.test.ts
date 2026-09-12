// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the Node.js structured logger + rotating file sink integration.
 *
 * Per feat-015: the logger accepts an optional rotating file sink; the
 * stderr path stays byte-identical when no sink is attached, and
 * attach/detach are idempotent.
 */

import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, test } from "vitest";

import {
  attachFileSink,
  detachFileSink,
  logger,
} from "../src/lib/logger.js";
import { RotatingFileSink } from "../src/lib/rotating-file-sink.js";

let workdir: string;

beforeEach(() => {
  workdir = join(
    tmpdir(),
    `heddle_logger_test_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
  );
  mkdirSync(workdir, { recursive: true });
});

afterEach(() => {
  detachFileSink();
  if (existsSync(workdir)) {
    rmSync(workdir, { recursive: true, force: true });
  }
});

function captureStderr(): { writes: string[]; restore: () => void } {
  const writes: string[] = [];
  const original = process.stderr.write.bind(process.stderr);
  // process.stderr.write has a polymorphic signature; the simple
  // spy captures both string and buffer writes and serializes to text.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (process.stderr as unknown as { write: (chunk: unknown) => boolean }).write = ((
    chunk: unknown,
  ) => {
    writes.push(typeof chunk === "string" ? chunk : String(chunk));
    return true;
  }) as unknown as typeof process.stderr.write;
  return {
    writes,
    restore: () => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (process.stderr as any).write = original;
    },
  };
}

describe("logger + sink — stderr behavior unchanged when no sink attached", () => {
  test("stderr receives the JSON line", () => {
    const cap = captureStderr();
    try {
      logger.info("node", "test_event", "hello");
    } finally {
      cap.restore();
    }
    const out = cap.writes.join("");
    expect(out).toContain('"event":"test_event"');
    expect(out).toContain('"msg":"hello"');
    expect(out).toContain('"component":"node"');
  });

  test("redacts api_key in stderr output", () => {
    const cap = captureStderr();
    try {
      logger.info(
        "node",
        "auth_failed",
        "bad key",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        { api_key: "sk-supersecret-zzz" } as any,
      );
    } finally {
      cap.restore();
    }
    const out = cap.writes.join("");
    expect(out).not.toContain("sk-supersecret-zzz");
    expect(out).toContain("[REDACTED]");
  });
});

describe("logger + sink — file sink integration", () => {
  test("attach → emit writes to sink AND stderr", () => {
    const cap = captureStderr();
    const logPath = join(workdir, "test.log");
    attachFileSink(new RotatingFileSink(logPath, { maxBytes: 1024, backupCount: 2 }));
    try {
      logger.info("node", "sink_test", "hello");
    } finally {
      cap.restore();
      detachFileSink();
    }
    const stderrOut = cap.writes.join("");
    expect(stderrOut).toContain('"event":"sink_test"');
    const fileBody = readFileSync(logPath, "utf-8");
    expect(fileBody).toContain('"event":"sink_test"');
    expect(fileBody).toContain('"msg":"hello"');
  });

  test("sink receives redacted payload (api_key not present)", () => {
    const cap = captureStderr();
    const logPath = join(workdir, "redact.log");
    attachFileSink(new RotatingFileSink(logPath, { maxBytes: 1024, backupCount: 2 }));
    try {
      logger.info(
        "node",
        "auth",
        "bad key",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        { api_key: "sk-supersecret-redact" } as any,
      );
    } finally {
      cap.restore();
      detachFileSink();
    }
    const fileBody = readFileSync(logPath, "utf-8");
    expect(fileBody).not.toContain("sk-supersecret-redact");
    expect(fileBody).toContain("[REDACTED]");
  });

  test("double attach closes the first sink", () => {
    const cap = captureStderr();
    const log1 = join(workdir, "first.log");
    const log2 = join(workdir, "second.log");
    const firstSink = new RotatingFileSink(log1, { maxBytes: 1024, backupCount: 2 });
    attachFileSink(firstSink);
    // Second attach replaces the first; the first's fd must be closed.
    const secondSink = new RotatingFileSink(log2, { maxBytes: 1024, backupCount: 2 });
    attachFileSink(secondSink);
    try {
      logger.info("node", "after_double_attach", "msg");
    } finally {
      cap.restore();
      detachFileSink();
    }
    // First sink's fd is closed; if we wrote to it after close, no effect.
    // Reading the file system: first.log may have been created but should
    // not have content from "after_double_attach".
    const secondBody = readFileSync(log2, "utf-8");
    expect(secondBody).toContain('"event":"after_double_attach"');
    expect(firstSink["fd"]).toBeNull();
  });

  test("detach with no sink is no-op", () => {
    const cap = captureStderr();
    try {
      detachFileSink(); // already detached → no throw
      logger.info("node", "after_detach", "msg");
    } finally {
      cap.restore();
    }
    // No file should have been written. We just confirm no throw.
    expect(true).toBe(true);
  });

  test("detach closes the previous sink", () => {
    const cap = captureStderr();
    const logPath = join(workdir, "detach.log");
    const sink = new RotatingFileSink(logPath, { maxBytes: 1024, backupCount: 2 });
    attachFileSink(sink);
    detachFileSink();
    // The sink's fd is now closed.
    expect(sink["fd"]).toBeNull();
    cap.restore();
  });
});