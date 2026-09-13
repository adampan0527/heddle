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
    useUiStore.setState({
      activeProjectId: null,
      drafts: {},
      selectedIds: [],
    });
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
    // feat-040 widened persistence from activeProjectId-only to the
    // full draft-tray state; the localStorage key moved to
    // `heddle.ui.state` and the partialize wrapper still includes
    // activeProjectId so the existing assertion holds.
    const raw = window.localStorage.getItem("heddle.ui.state");
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
      "heddle.ui.state",
      JSON.stringify({ state: { activeProjectId: "p3" }, version: 1 }),
    );
    // Zustand's persist middleware hydrates asynchronously even with
    // synchronous localStorage — wait for it before asserting.
    await useUiStore.persist.rehydrate();
    expect(useUiStore.getState().activeProjectId).toBe("p3");
  });

  test("setDrafts replaces the draft tray with the latest decomposition", () => {
    useUiStore.getState().setDrafts([
      {
        id: "temp-001",
        title: "OAuth login",
        description: "Allow sign-in via Google.",
        steps: ["Wire Google OAuth"],
        depends_on: [],
        kind: "feature",
      },
      {
        id: "temp-002",
        title: "Dark mode",
        description: "Theme switcher.",
        steps: ["Add theme context"],
        depends_on: [],
        kind: "feature",
      },
    ]);
    const state = useUiStore.getState();
    expect(Object.keys(state.drafts)).toHaveLength(2);
    expect(state.drafts["temp-001"]?.title).toBe("OAuth login");
    // Default: nothing kept. Users opt in by clicking the checkbox.
    expect(state.selectedIds).toEqual([]);
  });

  test("toggleDraft adds and removes ids from selectedIds", () => {
    useUiStore.getState().setDrafts([
      {
        id: "temp-100",
        title: "X",
        description: "y",
        steps: [],
        depends_on: [],
        kind: "feature",
      },
    ]);
    useUiStore.getState().toggleDraft("temp-100");
    expect(useUiStore.getState().selectedIds).toContain("temp-100");
    useUiStore.getState().toggleDraft("temp-100");
    expect(useUiStore.getState().selectedIds).not.toContain("temp-100");
  });

  test("clearDrafts resets both drafts and selectedIds", () => {
    useUiStore.getState().setDrafts([
      {
        id: "temp-200",
        title: "X",
        description: "y",
        steps: [],
        depends_on: [],
        kind: "feature",
      },
    ]);
    useUiStore.getState().toggleDraft("temp-200");
    useUiStore.getState().clearDrafts();
    expect(useUiStore.getState().drafts).toEqual({});
    expect(useUiStore.getState().selectedIds).toEqual([]);
  });
});
