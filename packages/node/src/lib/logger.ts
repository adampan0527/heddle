// SPDX-License-Identifier: Apache-2.0
/**
 * Structured JSON logging — one event per line to stderr.
 *
 * Mirrors packages/web/src/lib/logger.ts (feat-005). Kept as a local
 * copy here because the heddle web bundle is browser-targeted; sharing
 * one TS module across browser + Node is a future packages/shared
 * refactor (post-feat-050).
 *
 * Schema (one JSON object per stderr line):
 *
 *   {
 *     "ts":         ISO8601 UTC string,
 *     "level":      "debug" | "info" | "warn" | "error",
 *     "component":  "node",
 *     "project_id": string | null,
 *     "feature_id": string | null,
 *     "event":      string,
 *     "msg":        string,
 *     ... additional fields (api_key/secret/token auto-redacted) ...
 *   }
 *
 * Per TECH.md T-017 / T-030. Defense-in-depth: redaction happens here
 * even when callers mean to redact, so a future caller typo cannot
 * silently leak a credential into the log stream.
 *
 * feat-015: an optional rotating file sink may be attached via
 * `attachFileSink(sink)`. When attached, every emitted event is
 * serialized to the sink after redaction (the sink receives the
 * redacted payload — it does NOT re-redact). The stderr path is
 * unchanged when no sink is attached.
 */

import type { RotatingFileSink } from "./rotating-file-sink.js";

export type Level = "debug" | "info" | "warn" | "error";
export type Component = "node";

const REDACT_PATTERNS = ["api_key", "secret", "token"];

function normalizeKey(key: string): string {
  return key.toLowerCase().replace(/[_-]/g, "");
}

const NORMALIZED_PATTERNS: string[] = REDACT_PATTERNS.map(normalizeKey);

function keyIsSensitive(key: string): boolean {
  const n = normalizeKey(key);
  return NORMALIZED_PATTERNS.some((p) => n.includes(p));
}

function redact(obj: unknown): unknown {
  if (obj === null || obj === undefined) return obj;
  if (Array.isArray(obj)) return obj.map(redact);
  if (typeof obj === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      out[k] = keyIsSensitive(k) ? "[REDACTED]" : redact(v);
    }
    return out;
  }
  return obj;
}

export interface LogFields {
  project_id?: string;
  feature_id?: string;
  [key: string]: unknown;
}

export interface LogRecord {
  ts: string;
  level: Level;
  component: Component;
  project_id: string | null;
  feature_id: string | null;
  event: string;
  msg: string;
  [key: string]: unknown;
}

function emit(
  level: Level,
  component: Component,
  event: string,
  msg: string,
  fields: LogFields = {},
): void {
  const payload: LogRecord = {
    ts: new Date().toISOString(),
    level,
    component,
    event,
    msg,
    project_id: fields.project_id ?? null,
    feature_id: fields.feature_id ?? null,
    ...fields,
  };
  const redacted = redact(payload);
  process.stderr.write(JSON.stringify(redacted) + "\n");
  // feat-015: forward the redacted payload to the optional rotating
  // file sink. The sink has already been opened via attachFileSink;
  // when no sink is attached the module-level state is null and the
  // hot path is a single null-check.
  if (_fileSink !== null) {
    _fileSink.write(JSON.stringify(redacted) + "\n");
  }
}

/** Module-level rotating file sink. Null means "no sink attached". */
let _fileSink: RotatingFileSink | null = null;

/**
 * Attach a rotating file sink to the stderr emit path.
 *
 * Idempotent: if a sink is already attached it is closed before the
 * new one is wired in, so a second call never leaks the previous
 * file handle. The stderr path itself is unchanged.
 */
export function attachFileSink(sink: RotatingFileSink): void {
  if (_fileSink !== null) {
    _fileSink.close();
    _fileSink = null;
  }
  _fileSink = sink;
}

/**
 * Detach the rotating file sink. Idempotent (no-op when no sink is
 * attached) so it is safe to call from shutdown paths without
 * checking state.
 */
export function detachFileSink(): void {
  if (_fileSink === null) return;
  _fileSink.close();
  _fileSink = null;
}

export const logger = {
  debug: (component: Component, event: string, msg: string, fields?: LogFields) =>
    emit("debug", component, event, msg, fields),
  info: (component: Component, event: string, msg: string, fields?: LogFields) =>
    emit("info", component, event, msg, fields),
  warn: (component: Component, event: string, msg: string, fields?: LogFields) =>
    emit("warn", component, event, msg, fields),
  error: (component: Component, event: string, msg: string, fields?: LogFields) =>
    emit("error", component, event, msg, fields),
};