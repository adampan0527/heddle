// SPDX-License-Identifier: Apache-2.0
/**
 * Vitest config for the heddle web client — feat-032.
 *
 * Mirrors `packages/node/vitest.config.ts` but with `environment: "jsdom"`
 * so React component tests have DOM globals (document, window, etc.).
 * `setupFiles` registers `@testing-library/jest-dom` matchers like
 * `toBeInTheDocument()`.
 */

import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globals: true,
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test-setup.ts"],
    testTimeout: 20000,
  },
});
