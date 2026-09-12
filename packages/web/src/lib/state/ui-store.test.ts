// SPDX-License-Identifier: Apache-2.0
/**
 * Unit tests for the UI store. The store is a Zustand creator wrapped
 * in the `persist` middleware — no React rendering required — so we
 * exercise it directly via `useUiStore.getState()` / `.setState()`.
 *
 * jsdom (set up by `test-setup.ts` via Vitest's `environment: jsdom`)
 * provides `localStorage`, so the `persist` middleware round-trips
 * for real without stubbing.
 */

import { afterEach, describe, expect, test } from "vitest";

import { useUiStore } from "./ui-store.ts";

describe("ui-store", () => {
  afterEach(() => {
    // Clear localStorage between tests so prior writes don't leak.
    window.localStorage.clear();
    useUiStore.setState({ activeProjectId: null });
  });

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

  test("setActiveProjectId writes to localStorage under the namespaced key", () => {
    useUiStore.getState().setActiveProjectId("p2");
    const raw = window.localStorage.getItem("heddle.ui.activeProjectId");
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw ?? "{}") as {
      state: { activeProjectId: string | null };
      version: number;
    };
    expect(parsed.state.activeProjectId).toBe("p2");
    expect(parsed.version).toBe(1);
  });

  test("localStorage pre-populated value hydrates activeProjectId on first read", async () => {
    // Simulate a prior session that stored p3.
    window.localStorage.setItem(
      "heddle.ui.activeProjectId",
      JSON.stringify({ state: { activeProjectId: "p3" }, version: 1 }),
    );
    // Zustand's persist middleware hydrates asynchronously even with
    // synchronous localStorage — wait for it before asserting.
    await useUiStore.persist.rehydrate();
    expect(useUiStore.getState().activeProjectId).toBe("p3");
  });
});
