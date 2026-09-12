// SPDX-License-Identifier: Apache-2.0
/**
 * Barrel re-export for `@heddle/shared`.
 *
 * Consumed by both `packages/node/` and `packages/web/`. Anything that
 * crosses the wire (HTTP envelope shape, error code taxonomy, domain
 * types) lives here so the two sides cannot drift.
 */

export * from "./api.js";
export * from "./domain.js";
