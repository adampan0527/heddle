// SPDX-License-Identifier: Apache-2.0
/**
 * Error type used by TanStack Query hooks when the daemon returns
 * `{ ok: false, error: { code, message } }`. Wraps the shared
 * `ApiErr` shape so consumers can do `e.code === "not_found"` rather
 * than poking at message strings.
 */

import type { ApiErr, DaemonError, ErrorCode } from "@heddle/shared";

export class ApiCallError extends Error {
  public readonly code: ErrorCode;
  public readonly httpStatus: number | undefined;

  constructor(error: DaemonError, httpStatus?: number) {
    super(error.message);
    this.name = "ApiCallError";
    this.code = error.code;
    // exactOptionalPropertyTypes: assign through a local to avoid
    // the | undefined not assignable to optional-property type error.
    this.httpStatus = httpStatus !== undefined ? httpStatus : undefined;
  }
}

/** Type-narrowing helper — throws on `ok: false`, returns `data` on `ok: true`. */
export function unwrap<T>(
  resp: { ok: true; data: T } | ApiErr,
  httpStatus?: number,
): T {
  if (resp.ok) return resp.data;
  throw new ApiCallError(resp.error, httpStatus);
}
