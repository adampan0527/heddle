// SPDX-License-Identifier: Apache-2.0
/**
 * Ephemeral UI state for the heddle web client.
 *
 * Per TECH.md T-006: Zustand owns drag state, dialog draft state,
 * panel open/close, etc. — anything purely cosmetic that doesn't come
 * from the server. Server-derived data lives in TanStack Query.
 *
 * feat-032 ships one slice: `activeProjectId`. Future features will
 * extend this with dialog state, dnd state, DAG panel position, etc.
 * No `persist` middleware yet — feat-034 may add it.
 */

import { create } from "zustand";

export interface UiState {
  activeProjectId: string | null;
  setActiveProjectId: (id: string | null) => void;
}

export const useUiStore = create<UiState>((set) => ({
  activeProjectId: null,
  setActiveProjectId: (id) => set({ activeProjectId: id }),
}));
