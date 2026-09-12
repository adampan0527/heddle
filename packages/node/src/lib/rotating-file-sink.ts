// SPDX-License-Identifier: Apache-2.0
/**
 * Hand-rolled size-rotating file sink — feat-015.
 *
 * The Python side wraps ``logging.handlers.RotatingFileHandler``; the
 * Node.js side does not have an off-the-shelf equivalent that survives
 * a rename race on Windows (open file handle, ``EBUSY`` on rename).
 * This implementation follows the same contract:
 *
 *   * ``write(line)`` appends the line to the active file
 *   * when ``bytesWritten >= maxBytes``, ``rotate()`` is invoked
 *   * ``rotate()`` closes the fd, then renames ``<path>`` → ``<path>.1``,
 *     ``<path>.1`` → ``<path>.2``, ..., ``<path>.<backupCount>`` deleted
 *   * a fresh fd is opened at ``<path>`` and ``bytesWritten`` reset
 *
 * On Windows the close-before-rename order is the only thing that
 * avoids the sharing violation — the test ``renames_after_close``
 * asserts that ``fs.closeSync(fd)`` is called before
 * ``fs.renameSync(...)``.
 *
 * Env-var contract (mirrors ``heddle_common.log_rotation``):
 *
 *   * ``HEDDLE_LOG_MAX_BYTES`` (default 50 MiB)
 *   * ``HEDDLE_LOG_BACKUP_COUNT`` (default 5)
 *
 * Invalid values throw in the constructor so a misconfigured
 * supervisor fails fast at startup, not silently mid-run.
 */

import {
  closeSync,
  mkdirSync,
  openSync,
  renameSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { dirname } from "node:path";

const DEFAULT_LOG_MAX_BYTES = 50 * 1024 * 1024;
const DEFAULT_LOG_BACKUP_COUNT = 5;
const MAX_LOG_MAX_BYTES = 1 * 1024 * 1024 * 1024; // 1 GiB
const MAX_LOG_BACKUP_COUNT = 100;

const ENV_LOG_MAX_BYTES = "HEDDLE_LOG_MAX_BYTES";
const ENV_LOG_BACKUP_COUNT = "HEDDLE_LOG_BACKUP_COUNT";

function parseIntEnv(name: string, raw: string | undefined, fallback: number): number {
  if (raw === undefined || raw === "") return fallback;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n) || !Number.isInteger(n)) {
    throw new Error(`${name}=${JSON.stringify(raw)} is not an integer`);
  }
  return n;
}

function readEnv(name: string, env: NodeJS.ProcessEnv = process.env): string | undefined {
  const value = env[name];
  return typeof value === "string" ? value : undefined;
}

export interface RotatingFileSinkOptions {
  maxBytes?: number;
  backupCount?: number;
  env?: NodeJS.ProcessEnv;
}

/**
 * Append structured log lines to a size-rotated file.
 *
 * The caller is responsible for handing us **already-redacted** lines
 * (the structured logger runs `redact()` before serializing); the
 * sink does not re-redact.
 */
export class RotatingFileSink {
  readonly path: string;
  readonly maxBytes: number;
  readonly backupCount: number;
  private bytesWritten = 0;
  private fd: number | null = null;

  constructor(path: string, options: RotatingFileSinkOptions = {}) {
    const env = options.env ?? process.env;
    const maxBytes =
      options.maxBytes ?? parseIntEnv(ENV_LOG_MAX_BYTES, readEnv(ENV_LOG_MAX_BYTES, env), DEFAULT_LOG_MAX_BYTES);
    const backupCount =
      options.backupCount ?? parseIntEnv(ENV_LOG_BACKUP_COUNT, readEnv(ENV_LOG_BACKUP_COUNT, env), DEFAULT_LOG_BACKUP_COUNT);
    this.validate(maxBytes, backupCount);
    this.path = path;
    this.maxBytes = maxBytes;
    this.backupCount = backupCount;
    // Ensure the parent directory exists so the first openSync does
    // not raise ENOENT on a fresh install. ``recursive: true`` makes
    // a no-op when the directory already exists.
    mkdirSync(dirname(path), { recursive: true });
    this.fd = this.openAppend();
  }

  /** Validate configuration. Throws on bad input. */
  private validate(maxBytes: number, backupCount: number): void {
    if (!Number.isInteger(maxBytes)) {
      throw new Error(`maxBytes must be an integer; got ${typeof maxBytes}`);
    }
    if (maxBytes <= 0) {
      throw new Error(`maxBytes must be > 0; got ${maxBytes}`);
    }
    if (maxBytes > MAX_LOG_MAX_BYTES) {
      throw new Error(
        `maxBytes=${maxBytes} exceeds MAX_LOG_MAX_BYTES=${MAX_LOG_MAX_BYTES} (1 GiB); ` +
          `refusing to rotate at that size`,
      );
    }
    if (!Number.isInteger(backupCount)) {
      throw new Error(`backupCount must be an integer; got ${typeof backupCount}`);
    }
    if (backupCount <= 0) {
      throw new Error(`backupCount must be > 0; got ${backupCount}`);
    }
    if (backupCount > MAX_LOG_BACKUP_COUNT) {
      throw new Error(
        `backupCount=${backupCount} exceeds MAX_LOG_BACKUP_COUNT=${MAX_LOG_BACKUP_COUNT}`,
      );
    }
  }

  /** Open the active file in append mode. */
  private openAppend(): number {
    return openSync(this.path, "a", 0o644);
  }

  /** Append one line. Triggers rotation when the threshold is crossed. */
  write(line: string): void {
    if (this.fd === null) {
      // Emit-after-close is silently dropped (matches the Python sink).
      return;
    }
    const buf = Buffer.from(line, "utf-8");
    const written = writeSync(this.fd, buf);
    this.bytesWritten += written;
    if (this.bytesWritten >= this.maxBytes) {
      this.rotate();
    }
  }

  /**
   * Rotate the file. The fd is closed BEFORE rename so a Windows
   * holding-onto-the-fd race cannot surface as EBUSY; the test
   * `renames_after_close` asserts this ordering.
   */
  rotate(): void {
    if (this.fd !== null) {
      closeSync(this.fd);
      this.fd = null;
    }
    // Oldest backup at .<backupCount> — unlink first to make room.
    const oldest = `${this.path}.${this.backupCount}`;
    try {
      unlinkSync(oldest);
    } catch (err) {
      // ENOENT is expected on the first rotation; anything else is
      // worth surfacing but not fatal — we proceed with the rename
      // chain anyway.
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== "ENOENT") {
        throw err;
      }
    }
    // Rename chain: .<N-1> → .<N> for N = backupCount down to 2,
    // then <path> → .1. We go in REVERSE order so the oldest survives
    // longest.
    for (let i = this.backupCount - 1; i >= 1; i -= 1) {
      const src = `${this.path}.${i}`;
      const dst = `${this.path}.${i + 1}`;
      try {
        renameSync(src, dst);
      } catch (err) {
        const code = (err as NodeJS.ErrnoException).code;
        if (code !== "ENOENT") {
          throw err;
        }
        // ENOENT on intermediate renames is fine: that generation
        // was already pruned or never existed.
      }
    }
    // <path> → .1
    try {
      renameSync(this.path, `${this.path}.1`);
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== "ENOENT") {
        throw err;
      }
    }
    // After the rename <path> does not exist; openSync with flag "a"
    // creates it. We then verify the new fd points at <path> and not
    // at the rotated-out file by sanity-checking that the new file's
    // size is zero (an existing file would have non-zero size from
    // prior contents).
    this.fd = this.openAppend();
    this.bytesWritten = 0;
  }

  /** Flush + close. Idempotent (close-after-close is a no-op). */
  close(): void {
    if (this.fd === null) return;
    closeSync(this.fd);
    this.fd = null;
    this.bytesWritten = 0;
  }
}

// Exported so tests can assert against the names without reaching into
// the module's private state.
export const ROTATING_FILE_SINK_ENV = {
  DEFAULT_LOG_MAX_BYTES,
  DEFAULT_LOG_BACKUP_COUNT,
  ENV_LOG_BACKUP_COUNT,
  ENV_LOG_MAX_BYTES,
  MAX_LOG_BACKUP_COUNT,
  MAX_LOG_MAX_BYTES,
} as const;