// SPDX-License-Identifier: Apache-2.0
/**
 * Ephemeral UI state for the kanban — feat-035 / feat-036.
 *
 * Per TECH.md T-006, Zustand owns pure-UI state. The kanban's
 * in-flight drag (which card is being dragged, which column is the
 * current drop target) is *purely* a UI concern — the server is not
 * notified until the drop completes — so it lives here, not in
 * TanStack Query.
 *
 * Deliberately NOT wrapped in `persist` (cf. `ui-store.ts`'s persisted
 * `activeProjectId`). Two reasons:
 *
 *   1. Drag state must reset on every page load; a stale `draggingId`
 *      after a hot-reload would leave the kanban thinking a card is
 *      mid-drag when nothing actually is.
 *   2. Lane-expand state (feat-036) is session-only on purpose — the
 *      Done lane is meant to be "always visible" while Someday/Archive
 *      stay collapsed so the board does not feel cluttered on reload.
 *      Persisting these would surprise the user with whatever collapse
 *      pattern they last had, which is not the spec'd default.
 */

import { create } from "zustand";

export type KanbanColumnId = "in_progress" | "ready" | "blocked";

/**
 * Bottom-lane ids (feat-036). Disjoint from `KanbanColumnId` so
 * dnd-kit's `useDroppable` (which lives only on columns) cannot
 * resolve a lane id as a drop target — that is the spec's
 * "lanes are view-only" guarantee.
 */
export type KanbanLaneId = "done" | "someday" | "archive";

/** Initial expanded/collapsed state for the three bottom lanes. */
export const DEFAULT_LANES_EXPANDED: Readonly<Record<KanbanLaneId, boolean>> =
  {
    done: true,
    someday: false,
    archive: false,
  };

export interface KanbanState {
  /** Feature id currently being dragged, or null when no drag is active. */
  draggingId: string | null;
  /** Column id the dragged card is currently hovering over, or null. */
  hoverColumn: KanbanColumnId | null;

  /**
   * Whether each bottom lane is expanded. Mirrors the store on every
   * `setLaneExpanded` call; defaults to `DEFAULT_LANES_EXPANDED`.
   */
  lanesExpanded: Record<KanbanLaneId, boolean>;

  setDragging: (id: string | null) => void;
  setHover: (col: KanbanColumnId | null) => void;
  setLaneExpanded: (id: KanbanLaneId, expanded: boolean) => void;
  reset: () => void;
}

export const useKanbanStore = create<KanbanState>((set) => ({
  draggingId: null,
  hoverColumn: null,
  lanesExpanded: { ...DEFAULT_LANES_EXPANDED },
  setDragging: (id) => set({ draggingId: id }),
  setHover: (col) => set({ hoverColumn: col }),
  setLaneExpanded: (id, expanded) =>
    set((s) => ({ lanesExpanded: { ...s.lanesExpanded, [id]: expanded } })),
  reset: () => set({ draggingId: null, hoverColumn: null }),
}));