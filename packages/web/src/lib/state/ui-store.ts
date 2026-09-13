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
 * feat-040 adds the draft tray state. Drafts are returned by the
 * daemon's dialog handler when `kind === "work"` and persist across
 * page reloads so a user who closes their browser while reviewing
 * decomposition suggestions comes back to the same set. The tray's
 * per-card "keep" selection is also persisted.
 *
 * Storage choice: `localStorage` (synchronous, no quota issues for a
 * single string). Key `heddle.ui.state` is namespaced so a host page
 * that also runs the heddle client (rare) cannot collide.
 */

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import type { DraftCard } from "@heddle/shared";

export interface UiState {
  activeProjectId: string | null;
  setActiveProjectId: (id: string | null) => void;
  /** Drafts keyed by their temp id (e.g. "temp-001"). feat-040. */
  drafts: Record<string, DraftCard>;
  /** Ids of drafts the user has chosen to keep (default = all unchecked). */
  selectedIds: string[];
  /** Replace the draft tray with the daemon's latest decomposition. */
  setDrafts: (drafts: DraftCard[]) => void;
  /** Toggle a single draft's "keep" state. */
  toggleDraft: (id: string) => void;
  /** Drop the tray entirely (Cancel or post-confirm). */
  clearDrafts: () => void;
  /**
   * Whether the right-side DAG view panel is open. feat-041. Persisted
   * across reloads via `partialize` so the user's preference survives
   * a hot reload or accidental tab close. Ephemeral-only would also be
   * defensible (the toggle is one click away) but persistence matches
   * the existing `activeProjectId` pattern and is friendlier when the
   * user navigates between projects and back.
   */
  dagViewOpen: boolean;
  setDagViewOpen: (open: boolean) => void;
  toggleDagView: () => void;
  /**
   * feat-054 / D-054: a destructive dialog command may park a
   * confirmation request here before invoking the mutation. The
   * dialog UI renders a card with the diff + Confirm / Cancel
   * buttons; clicking Confirm runs `apply`, Cancel runs `cancel`.
   * Only one pending confirmation at a time — a second command
   * overwrites the first (the user has to dismiss it explicitly).
   */
  pendingConfirmation: PendingConfirmation | null;
  setPendingConfirmation: (req: PendingConfirmation | null) => void;
}

export interface PendingConfirmation {
  id: string;
  featureId: string;
  command: string;
  /** Free-form diff object — the dialog renders its `changes` array. */
  diff: unknown;
  /** Confirm callback — runs the destructive mutation. */
  apply: () => Promise<void>;
  /** Cancel callback — drops the request, no mutation. */
  cancel: () => void;
}

/**
 * `Set` is not JSON-serialisable so we keep the kept-draft list as
 * a string array and surface it as a Set at the call site.
 */
function emptyDrafts(): Record<string, DraftCard> {
  return {};
}
function emptySelected(): string[] {
  return [];
}

export const useUiStore = create<UiState>()(
  persist(
    (set, get) => ({
      activeProjectId: null,
      setActiveProjectId: (id) => set({ activeProjectId: id }),
      drafts: emptyDrafts(),
      selectedIds: emptySelected(),
      setDrafts: (drafts) =>
        set(() => {
          const next: Record<string, DraftCard> = {};
          const selected: string[] = [];
          for (const d of drafts) {
            next[d.id] = d;
            // Default: every returned card is unchecked. The user
            // picks which to keep; unchecked cards will be removed
            // by the next decomposition round.
            if (!selected.includes(d.id)) selected.push(d.id);
          }
          // Default selected state: empty (kept list) — user opts in
          // by clicking the checkbox. This matches the spec's
          // "default unchecked = may be removed by next decomposition".
          return { drafts: next, selectedIds: [] };
        }),
      toggleDraft: (id) =>
        set((s) => {
          const has = s.selectedIds.includes(id);
          return {
            selectedIds: has
              ? s.selectedIds.filter((x) => x !== id)
              : [...s.selectedIds, id],
          };
        }),
      clearDrafts: () => set({ drafts: emptyDrafts(), selectedIds: emptySelected() }),
      // feat-041: DAG view toggle state.
      dagViewOpen: false,
      setDagViewOpen: (open) => set({ dagViewOpen: open }),
      toggleDagView: () => set({ dagViewOpen: !get().dagViewOpen }),
      // feat-054: destructive-op confirmation state. The dialog
      // writes a request before the user confirms; the dialog UI
      // reads it to render the card. We intentionally exclude this
      // from `partialize` — a confirm request must not survive a
      // page reload (the diff is stale, the apply callback is
      // closed over stale mutations).
      pendingConfirmation: null,
      setPendingConfirmation: (req) => set({ pendingConfirmation: req }),
    }),
    {
      name: "heddle.ui.state",
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        activeProjectId: state.activeProjectId,
        drafts: state.drafts,
        selectedIds: state.selectedIds,
        dagViewOpen: state.dagViewOpen,
      }),
      version: 1,
    },
  ),
);