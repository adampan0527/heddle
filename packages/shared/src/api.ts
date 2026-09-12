// SPDX-License-Identifier: Apache-2.0
/**
 * Wire-contract types shared between `packages/node/` (server) and
 * `packages/web/` (browser). Mirrors `packages/node/src/types.ts` —
 * the node module re-exports these from here so any change has exactly
 * one source of truth.
 *
 * The daemon's Python side (`heddle_daemon.routes`) mirrors the same
 * shapes — schema drift surfaces as a test failure on either side.
 */

/** All wire codes the daemon (or HTTP layer) can return. */
export const ERROR_CODES = [
  "invalid_input",
  "not_found",
  "conflict",
  "invalid_state",
  "schema_too_new",
  "envelope_malformed",
  "envelope_version_too_new",
  "handler_error",
  "internal_error",
  "daemon_unavailable",
  "request_timeout",
] as const;
export type ErrorCode = (typeof ERROR_CODES)[number];

/** The structured error returned by the daemon on `ok: false`. */
export interface DaemonError {
  code: ErrorCode;
  message: string;
}

/** Successful HTTP response. Browser does `if (resp.ok) ...`. */
export interface ApiOk<T> {
  ok: true;
  data: T;
}

/** Failed HTTP response. `code` is one of ErrorCode. */
export interface ApiErr {
  ok: false;
  error: DaemonError;
}

export type ApiResponse<T> = ApiOk<T> | ApiErr;
