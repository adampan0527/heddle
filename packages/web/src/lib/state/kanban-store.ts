// SPDX-License-Identifier: Apache-2.0
/**
 * Ephemeral drag state for the kanban — feat-035.
 *
 * Per TECH.md T-006, Zustand owns pure-UI state. The kanban's
 * in-flight drag (which card is being dragged, which column is the
 * current drop target) is *purely* a UI concern — the server is not
 * notified until the drop completes — so it lives here, not in
 * TanStack Query.
 *
 * Deliberately NOT wrapped in `persist` (cf. `ui-store.ts`'s persisted
 * `activeProjectId`). Drag state must reset on every page load; a
 * stale `draggingId` after a hot-reload would leave the kanban
 * thinking a card is mid-drag when nothing actually is.
 */

import { create } from "zustand";

export type KanbanColumnId = "in_progress" | "ready" | "blocked";

export interface KanbanState {
  /** Feature id currently being dragged, or null when no drag is active. */
  draggingId: string | null;
  /** Column id the dragged card is currently hovering over, or null. */
  hoverColumn: KanbanColumnId | null;

  setDragging: (id: string | null) => void;
  setHover: (col: KanbanColumnId | null) => void;
  reset: () => void;
}

export const useKanbanStore = create<KanbanState>((set) => ({
  draggingId: null,
  hoverColumn: null,
  setDragging: (id) => set({ draggingId: id }),
  setHover: (col) => set({ hoverColumn: col }),
  reset: () => set({ draggingId: null, hoverColumn: null }),
}));