// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the Node-side projects registry reader — feat-015 / feat-012.
 */

import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, test } from "vitest";

import {
  defaultProjectsPath,
  listProjects,
} from "../src/lib/projects-registry.js";

let workdir: string;

beforeEach(() => {
  workdir = join(tmpdir(), `heddle_pr_test_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
  mkdirSync(workdir, { recursive: true });
});

afterEach(() => {
  if (existsSync(workdir)) {
    rmSync(workdir, { recursive: true, force: true });
  }
});

function writeProjects(entries: unknown): void {
  const path = join(workdir, "projects.json");
  writeFileSync(path, JSON.stringify(entries), "utf-8");
}

describe("listProjects", () => {
  test("returns [] when the file is missing", () => {
    const missing = join(workdir, "does-not-exist.json");
    expect(listProjects(missing)).toEqual([]);
  });

  test("returns parsed entries on a valid file", () => {
    writeProjects({
      version: 1,
      projects: [
        {
          id: "p1",
          name: "alpha",
          path: "/tmp/alpha",
          added_at: "2026-01-01T00:00:00Z",
          last_accessed_at: "2026-01-02T00:00:00Z",
        },
        {
          id: "p2",
          name: "beta",
          path: "/tmp/beta",
          added_at: "2026-01-03T00:00:00Z",
          last_accessed_at: "2026-01-04T00:00:00Z",
        },
      ],
    });
    const projects = listProjects(join(workdir, "projects.json"));
    expect(projects).toHaveLength(2);
    expect(projects[0].id).toBe("p1");
    expect(projects[0].name).toBe("alpha");
    expect(projects[1].path).toBe("/tmp/beta");
  });

  test("drops malformed entries with a warn log", () => {
    writeProjects({
      version: 1,
      projects: [
        {
          id: "p1",
          name: "alpha",
          path: "/tmp/alpha",
          added_at: "2026-01-01T00:00:00Z",
          last_accessed_at: "2026-01-02T00:00:00Z",
        },
        // Missing several required fields.
        { id: "broken" },
        // Wrong type for `name`.
        {
          id: "p3",
          name: 42,
          path: "/tmp/gamma",
          added_at: "2026-01-01T00:00:00Z",
          last_accessed_at: "2026-01-02T00:00:00Z",
        },
        // Valid entry should still appear.
        {
          id: "p4",
          name: "delta",
          path: "/tmp/delta",
          added_at: "2026-01-05T00:00:00Z",
          last_accessed_at: "2026-01-06T00:00:00Z",
        },
      ],
    });
    const projects = listProjects(join(workdir, "projects.json"));
    expect(projects).toHaveLength(2);
    expect(projects.map((p) => p.id)).toEqual(["p1", "p4"]);
  });

  test("returns [] when JSON is invalid", () => {
    const path = join(workdir, "projects.json");
    writeFileSync(path, "this is not json {", "utf-8");
    expect(listProjects(path)).toEqual([]);
  });

  test("returns [] when `projects` is missing or wrong shape", () => {
    const path = join(workdir, "projects.json");
    writeFileSync(path, JSON.stringify({ version: 1 }), "utf-8");
    expect(listProjects(path)).toEqual([]);
  });

  test("defaultProjectsPath() points to ~/.heddle/projects.json", () => {
    const path = defaultProjectsPath();
    expect(path).toMatch(/projects\.json$/);
  });
});