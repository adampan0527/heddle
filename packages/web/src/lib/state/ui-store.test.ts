// SPDX-License-Identifier: Apache-2.0
/**
 * Unit tests for the UI store. The store is a plain Zustand creator
 * — no React rendering required — so we exercise it directly via
 * `useUiStore.getState()` / `.setState()`.
 */

import { describe, expect, test } from "vitest";

import { useUiStore } from "./ui-store.ts";

describe("ui-store", () => {
  test("initial state has activeProjectId === null", () => {
    expect(useUiStore.getState().activeProjectId).toBeNull();
  });

  test("setActiveProjectId updates the active project id", () => {
    useUiStore.getState().setActiveProjectId("p1");
    expect(useUiStore.getState().activeProjectId).toBe("p1");

    // Reset for the next test so prior state never leaks.
    useUiStore.getState().setActiveProjectId(null);
    expect(useUiStore.getState().activeProjectId).toBeNull();
  });
});
