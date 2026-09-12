// SPDX-License-Identifier: Apache-2.0
/**
 * Node.js reader for the projects registry (`~/.heddle/projects.json`).
 *
 * Per feat-015 / feat-012: the Python daemon reads the registry via
 * `heddle_common.projects_io`. The Node.js supervisor (feat-027) needs
 * the same data to resolve the active project on startup so it can
 * attach a per-project rotating log sink (`packages/node/src/lib/
 * rotating-file-sink.ts`). Rather than re-implement the schema parser,
 * we read the same JSON file the Python side writes — the file is the
 * contract.
 *
 * This is intentionally a *thin* reader: it parses the file and returns
 * the array; per-feature behaviour (sorting, lookup, mutation) belongs
 * to the caller. Missing file → empty array (a fresh install has no
 * projects yet). Malformed entries are dropped with a warn log so a
 * bad row cannot crash the supervisor.
 */

import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import { logger } from "./logger.js";

/** Mirrors `heddle_common.projects_io.Project`. */
export interface Project {
  id: string;
  name: string;
  path: string;
  added_at: string;
  last_accessed_at: string;
}

const DEFAULT_PROJECTS_FILENAME = "projects.json";
const HEDDLE_DIR = ".heddle";

/**
 * Resolve the absolute path to `projects.json`.
 *
 * Mirrors `heddle_common.projects_io.default_projects_path` — the
 * Python side reads `~/.heddle/projects.json`; we replicate that on
 * the Node side so both processes agree on the registry location.
 * Test paths win over `HOME` so the suite can redirect the lookup.
 */
export function defaultProjectsPath(): string {
  return join(homedir(), HEDDLE_DIR, DEFAULT_PROJECTS_FILENAME);
}

/**
 * Read the projects registry. Returns `[]` when the file is missing
 * (a fresh install is the expected first-run state).
 *
 * Schema-mirrors `heddle_common.projects_io.list_projects`. Entries
 * missing any of the required fields are dropped with a warn log so a
 * partial / corrupted row never crashes the supervisor; the caller
 * sees the surviving entries.
 */
export function listProjects(path: string = defaultProjectsPath()): Project[] {
  if (!existsSync(path)) {
    return [];
  }
  let raw: string;
  try {
    raw = readFileSync(path, "utf-8");
  } catch (err) {
    logger.warn(
      "node",
      "projects_registry_read_failed",
      `cannot read ${path}: ${String(err)}`,
      { path, error_type: (err as { name?: string }).name ?? "Error" },
    );
    return [];
  }
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch (err) {
    logger.warn(
      "node",
      "projects_registry_parse_failed",
      `projects.json is not valid JSON: ${String(err)}`,
      { path },
    );
    return [];
  }
  if (!data || typeof data !== "object") {
    logger.warn(
      "node",
      "projects_registry_shape_unexpected",
      "projects.json root is not an object",
      { path },
    );
    return [];
  }
  const root = data as { projects?: unknown };
  if (!Array.isArray(root.projects)) {
    logger.warn(
      "node",
      "projects_registry_shape_unexpected",
      "projects.json `projects` is not an array",
      { path },
    );
    return [];
  }
  const requiredKeys: ReadonlyArray<keyof Project> = [
    "id",
    "name",
    "path",
    "added_at",
    "last_accessed_at",
  ];
  const out: Project[] = [];
  for (const entry of root.projects) {
    if (!entry || typeof entry !== "object") {
      logger.warn(
        "node",
        "projects_registry_entry_dropped",
        "entry is not an object",
        { path },
      );
      continue;
    }
    const rec = entry as Record<string, unknown>;
    let valid = true;
    for (const key of requiredKeys) {
      const value = rec[key];
      if (typeof value !== "string" || value.length === 0) {
        logger.warn(
          "node",
          "projects_registry_entry_dropped",
          `entry missing or invalid field ${String(key)}`,
          { path, field: String(key) },
        );
        valid = false;
        break;
      }
    }
    if (!valid) continue;
    out.push({
      id: rec.id as string,
      name: rec.name as string,
      path: rec.path as string,
      added_at: rec.added_at as string,
      last_accessed_at: rec.last_accessed_at as string,
    });
  }
  return out;
}