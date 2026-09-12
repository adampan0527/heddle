// SPDX-License-Identifier: Apache-2.0
/**
 * Ephemeral UI state for the heddle web client.
 *
 * Per TECH.md T-006: Zustand owns drag state, dialog draft state,
 * panel open/close, etc. — anything purely cosmetic that doesn't come
 * from the server. Server-derived data lives in TanStack Query.
 *
 * feat-032 shipped one slice: `activeProjectId`. feat-034 wraps the
 * store with Zustand's `persist` middleware so the user's project
 * choice survives a page reload — picking a project is a session
 * choice, not pure ephemeral state. Only `activeProjectId` is
 * persisted; future drag/dialog state will stay ephemeral.
 *
 * Storage choice: `localStorage` (synchronous, no quota issues for a
 * single string). Key `heddle.ui.activeProjectId` is namespaced so a
 * host page that also runs the heddle client (rare) cannot collide.
 */

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export interface UiState {
  activeProjectId: string | null;
  setActiveProjectId: (id: string | null) => void;
}

export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      activeProjectId: null,
      setActiveProjectId: (id) => set({ activeProjectId: id }),
    }),
    {
      name: "heddle.ui.activeProjectId",
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({ activeProjectId: state.activeProjectId }),
      version: 1,
    },
  ),
);
