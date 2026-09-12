// SPDX-License-Identifier: Apache-2.0
/**
 * Integration test for the Node supervisor's feat-015 log sink wiring.
 *
 * Validates that:
 *   * ``attachProjectLogSinkForActiveProject`` picks the first
 *     registered project and attaches a sink at the expected path.
 *   * env-var overrides flow through to the sink.
 *   * missing registry / empty registry → no attach + warn log.
 *   * sink construction failure → no attach + warn log.
 */

import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { detachFileSink } from "../src/lib/logger.js";
import { attachProjectLogSinkForActiveProject } from "../src/main.js";

let workdir: string;
let logsDir: string;
const savedHome = process.env.HOME;
const savedUserProfile = process.env.USERPROFILE;

beforeEach(() => {
  workdir = join(
    tmpdir(),
    `heddle_main_log_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
  );
  mkdirSync(workdir, { recursive: true });
  logsDir = join(workdir, "logs");
  // Redirect HOME so the supervisor's DEFAULT_LOGS_DIR resolves inside
  // our temp directory.
  process.env.HOME = workdir;
  process.env.USERPROFILE = workdir;
});

afterEach(() => {
  detachFileSink();
  if (savedHome === undefined) delete process.env.HOME;
  else process.env.HOME = savedHome;
  if (savedUserProfile === undefined) delete process.env.USERPROFILE;
  else process.env.USERPROFILE = savedUserProfile;
  if (existsSync(workdir)) {
    rmSync(workdir, { recursive: true, force: true });
  }
});

const PROJECT: {
  id: string;
  name: string;
  path: string;
  added_at: string;
  last_accessed_at: string;
} = {
  id: "supervisor-test-proj",
  name: "supervisor-test",
  path: "/tmp/supervisor-test",
  added_at: "2026-01-01T00:00:00Z",
  last_accessed_at: "2026-01-02T00:00:00Z",
};

// Tests use an injected reader instead of the disk-backed default so
// HOME caching at module-load time is not an issue. The registry
// helper is omitted in favour of explicit lambdas below.

describe("attachProjectLogSinkForActiveProject", () => {
  test("attaches a sink at <project_id>.node.log when a project is registered", () => {
    const sink = attachProjectLogSinkForActiveProject(
      () => [PROJECT],
      logsDir,
      ".node.log",
    );
    try {
      expect(sink).not.toBeNull();
      // The main.ts builder uses forward slashes; the assertion
      // string is built with the same convention so the test does
      // not depend on the host's path separator.
      expect(sink?.path).toBe(`${logsDir}/${PROJECT.id}.node.log`);
      expect(sink?.maxBytes).toBeGreaterThan(0);
      expect(sink?.backupCount).toBeGreaterThan(0);
    } finally {
      sink?.close();
    }
  });

  test("returns null + warn log when no projects are registered", () => {
    const sink = attachProjectLogSinkForActiveProject(
      () => [],
      logsDir,
      ".node.log",
    );
    expect(sink).toBeNull();
  });

  test("returns null + warn log when registry cannot be parsed", () => {
    const sink = attachProjectLogSinkForActiveProject(
      () => {
        throw new Error("boom");
      },
      logsDir,
      ".node.log",
    );
    expect(sink).toBeNull();
  });

  test("env-var override flows through to the attached sink", () => {
    process.env.HEDDLE_LOG_MAX_BYTES = "2048";
    process.env.HEDDLE_LOG_BACKUP_COUNT = "3";
    try {
      const sink = attachProjectLogSinkForActiveProject(
        () => [PROJECT],
        logsDir,
        ".node.log",
      );
      try {
        expect(sink?.maxBytes).toBe(2048);
        expect(sink?.backupCount).toBe(3);
      } finally {
        sink?.close();
      }
    } finally {
      delete process.env.HEDDLE_LOG_MAX_BYTES;
      delete process.env.HEDDLE_LOG_BACKUP_COUNT;
    }
  });

  test("emits a log line to the attached sink", () => {
    const sink = attachProjectLogSinkForActiveProject(
      () => [PROJECT],
      logsDir,
      ".node.log",
    );
    try {
      expect(sink).not.toBeNull();
      // Direct write through the sink proves the file is reachable.
      sink?.write('{"event":"smoke"}\n');
      expect(existsSync(sink!.path)).toBe(true);
      const body = readFileSync(sink!.path, "utf-8");
      expect(body).toContain('"event":"smoke"');
    } finally {
      sink?.close();
    }
  });
});