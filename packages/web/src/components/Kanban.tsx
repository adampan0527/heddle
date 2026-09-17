// SPDX-License-Identifier: Apache-2.0
/**
 * Top-level kanban board — feat-035 / feat-036 / feat-056.
 *
 * Layout: a 3-column responsive grid (`grid-cols-1 md:grid-cols-3`)
 * holding one `<KanbanColumn />` per `KanbanColumnId`. Each column
 * lists the features whose `status` matches its id.
 *
 * Below the main grid (still inside the same `DndContext`) is a row
 * of three collapsible lanes — Done (passing), Someday (deferred),
 * Archive (`superseded_by != null`). Lanes are view-only: no
 * `useDroppable`, no inline edit, no drag target. See `KanbanLaneRow`.
 *
 * Drag rules (enforced here in `onDragEnd`):
 *
 *   1. Blocked → ready: rejected with a toast. Blocked features are
 *      locked per D-016; the only way out is to fix the upstream dep.
 *   2. Anything → in_progress: only allowed when no other feature is
 *      currently `in_progress` (single-active model, also D-016).
 *      A second drop into the in_progress column is rejected with a
 *      toast.
 *   3. Otherwise (ready → ready, blocked → blocked, in_progress →
 *      in_progress): no-op. The card stays where it is.
 *   4. Allowed drops: trigger `useStartFeature` for the in_progress
 *      column; otherwise no-op (no API exists for "ready ↔ blocked"
 *      transitions in v0.1).
 *   5. Drops onto lane ids are no-ops. Lanes do not register a
 *      droppable target, so `over` is only set when the cursor is
 *      actually over a column. As defense-in-depth, `decideDrop`
 *      casts `overId` as `KanbanColumnId`; if a lane id ever leaks
 *      through (e.g. via a future change), the cast + the column-only
 *      match would still cause `decideDrop` to return noop.
 *
 * feat-056: the kanban header renders a `<KindFilter />` dropdown
 * that drives `useKanbanStore.kindFilter`. The feature list is
 * filtered by `applyKindFilter` BEFORE bucketing into columns and
 * lanes, so the column cards, lane cards, and lane count badges all
 * reflect the same filtered set. Filtered-out features are hidden
 * from drag targets too — the user cannot drop a kind that's not
 * visible.
 *
 * The data flow is deliberately unidirectional:
 *
 *   server → useFeatures → applyKindFilter → kanban store → columns → cards
 *   drag end → useStartFeature → server → invalidate → re-fetch
 *
 * No state lives in this component; both server data (TanStack) and
 * drag state (Zustand) flow in from sibling hooks so this file stays
 * small and testable.
 */

import {
  DndContext,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import type { Feature } from "@heddle/shared";

import {
  useFeatures,
  useStartFeature,
} from "../lib/api/features.ts";
import {
  useKanbanStore,
  type KanbanColumnId,
  type KanbanLaneId,
} from "../lib/state/kanban-store.ts";
import { useUiStore } from "../lib/state/ui-store.ts";
import { toastError } from "../lib/toast.ts";
import { applyKindFilter, KindFilter } from "./KindFilter.tsx";
import { KanbanCard } from "./KanbanCard.tsx";
import { KanbanColumn } from "./KanbanColumn.tsx";
import { KanbanLaneRow } from "./KanbanLaneRow.tsx";

interface KanbanProps {
  projectId: string | null;
}

const COLUMNS: ReadonlyArray<{
  id: KanbanColumnId;
  title: string;
  accent: string;
}> = [
  { id: "in_progress", title: "In progress", accent: "text-primary" },
  { id: "ready", title: "Ready", accent: "text-charcoal" },
  { id: "blocked", title: "Blocked", accent: "text-amber-900" },
];

/**
 * Map a feature row to its main-column id. Returns `null` for
 * statuses that live in a bottom lane (`passing`, `deferred`,
 * or any feature with `superseded_by != null`); the main grid skips
 * these and `KanbanLaneRow` picks them up via `laneOf`.
 *
 * `pending` still surfaces in Ready so the user sees work that has
 * never been attempted.
 *
 * feat-054 (D-054): a feature with `superseded_by != null` is treated
 * as if it were absent from the work graph — it shows only in the
 * Archive lane, never in the main columns. This is what makes split /
 * merge ops feel "non-destructive but visible": the old row is still
 * on disk, just hidden from the active work area.
 */
export function columnOf(feature: Feature): KanbanColumnId | null {
  // feat-054: superseded features hide from the main view regardless
  // of their status. We check `superseded_by` BEFORE the status branch
  // so a feature that was `passing` AND superseded still ends up in
  // Archive rather than Done.
  if (feature.superseded_by != null) return null;
  const s: string = feature.status;
  if (s === "in_progress") return "in_progress";
  if (s === "blocked") return "blocked";
  if (s === "pending") return "ready";
  // passing / deferred → bottom lanes (feat-036)
  return null;
}

/**
 * Map a feature row to its bottom-lane id, or `null` if it does not
 * belong in any lane (it belongs in a main column instead — see
 * `columnOf`). Mirrors `columnOf`'s shape so both mappers stay
 * symmetric and easy to read side-by-side.
 *
 *   passing               → done
 *   deferred              → someday
 *   superseded_by != null → archive
 *   anything else         → null
 *
 * `superseded_by` is checked before status; a feature that is both
 * `passing` and superseded is considered archived.
 */
export function laneOf(feature: Feature): KanbanLaneId | null {
  if (feature.superseded_by != null) return "archive";
  const s: string = feature.status;
  if (s === "passing") return "done";
  if (s === "deferred") return "someday";
  return null;
}

/**
 * Pure decision function used by the drag handler — extracted so
 * tests can drive the rules without simulating pointer events.
 *
 * Returns one of:
 *   - { kind: "noop" }                     — drop ignored (no API call)
 *   - { kind: "start", featureId }         — call useStartFeature
 *   - { kind: "reject", message }          — show toast, no API call
 */
export type DropDecision =
  | { kind: "noop" }
  | { kind: "start"; featureId: string }
  | { kind: "reject"; message: string };

export function decideDrop(
  card: Feature,
  target: KanbanColumnId,
  others: readonly Feature[],
): DropDecision {
  // Rule 1: blocked → ready is locked.
  if (card.status === "blocked" && target === "ready") {
    return {
      kind: "reject",
      message: "Blocked features are locked. Resolve dependencies first.",
    };
  }
  // Rule 2: only one feature can be in_progress at a time.
  if (target === "in_progress" && card.status !== "in_progress") {
    const alreadyRunning = others.some(
      (f) => f.status === "in_progress" && f.id !== card.id,
    );
    if (alreadyRunning) {
      return { kind: "reject", message: "Only one feature can run at a time." };
    }
    return { kind: "start", featureId: card.id };
  }
  return { kind: "noop" };
}

export function Kanban({ projectId }: KanbanProps): React.ReactElement {
  const features = useFeatures(projectId);
  const startFeature = useStartFeature(projectId);
  const setDragging = useKanbanStore((s) => s.setDragging);
  const setHover = useKanbanStore((s) => s.setHover);
  const reset = useKanbanStore((s) => s.reset);
  // feat-041: DAG view toggle state.
  const dagViewOpen = useUiStore((s) => s.dagViewOpen);
  const toggleDagView = useUiStore((s) => s.toggleDagView);
  // feat-056: kind-filter dropdown value (all / feature / bugfix / enhancement).
  // The filter is applied to the rendering list below, AFTER the drag handler
  // has already snapshotted `features.data` for `decideDrop` validation. That
  // way drag rules still see the full feature set (so an in_progress feature
  // hidden by the filter still blocks starting another) but the visible board
  // matches the dropdown.
  const kindFilter = useKanbanStore((s) => s.kindFilter);

  // Require a small movement (~4px) before starting a drag so clicks
  // on a card still register as clicks (not as drag starts).
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
  );

  function onDragStart(e: DragStartEvent): void {
    setDragging(String(e.active.id));
  }

  function onDragCancel(): void {
    reset();
  }

  function onDragEnd(e: DragEndEvent): void {
    const activeId = String(e.active.id);
    const overId = e.over ? String(e.over.id) : null;
    reset();
    if (!overId) return; // dropped outside any column
    const target = overId as KanbanColumnId;

    const list = features.data ?? [];
    const card = list.find((f) => f.id === activeId);
    if (!card) return;

    const decision = decideDrop(card, target, list);
    if (decision.kind === "reject") {
      toastError(decision.message);
    } else if (decision.kind === "start") {
      startFeature.mutate({ featureId: decision.featureId });
    }
    // "noop" → card stays put. No toast, no API call.
  }

  if (!projectId) {
    return (
      <div
        aria-label="Kanban placeholder"
        className="flex flex-1 items-center justify-center rounded-md border border-dashed border-hairline text-ash"
      >
        Select a project to view its kanban.
      </div>
    );
  }

  if (features.isPending) {
    return (
      <div className="flex flex-1 items-center justify-center text-ash">
        Loading features…
      </div>
    );
  }

  if (features.isError) {
    return (
      <div
        role="alert"
        className="flex flex-1 items-center justify-center text-red-700"
      >
        Error: {features.error.message}
      </div>
    );
  }

  // feat-056: apply the kind filter BEFORE bucketing. `applyKindFilter`
  // returns the input untouched when `kindFilter === "all"`, so the
  // common case is a single spread + identical-array fast path.
  const filteredList = applyKindFilter(features.data ?? [], kindFilter);

  const byColumn = new Map<KanbanColumnId, Feature[]>(
    COLUMNS.map((c) => [c.id, [] as Feature[]]),
  );
  for (const f of filteredList) {
    // `columnOf` may return `null` for features that live in a bottom
    // lane (feat-036). Those are routed to <KanbanLaneRow /> below
    // via `laneOf`, so we silently skip them here.
    const cid = columnOf(f);
    if (cid === null) continue;
    byColumn.get(cid)?.push(f);
  }

  return (
    <DndContext
      sensors={sensors}
      onDragStart={onDragStart}
      onDragOver={(e) => setHover(e.over ? (String(e.over.id) as KanbanColumnId) : null)}
      onDragEnd={onDragEnd}
      onDragCancel={onDragCancel}
    >
      <div className="flex flex-1 flex-col gap-4" aria-label="Kanban board wrapper">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-charcoal">
            Kanban
          </h2>
          <div className="flex items-center gap-2">
            <KindFilter />
            <button
              type="button"
              data-kanban-dag-toggle
              aria-label={dagViewOpen ? "Close DAG view" : "Open DAG view"}
              aria-pressed={dagViewOpen}
              onClick={toggleDagView}
              className={`rounded-full border px-3 py-1 text-xs ${
                dagViewOpen
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-hairline-strong text-charcoal hover:bg-canvas"
              }`}
            >
              {dagViewOpen ? "Hide DAG" : "Show DAG"}
            </button>
          </div>
        </div>
        <div
          className="grid flex-1 grid-cols-1 gap-4 md:grid-cols-3"
          aria-label="Kanban board"
        >
          {COLUMNS.map((c) => {
            const cards = byColumn.get(c.id) ?? [];
            return (
              <KanbanColumn
                key={c.id}
                id={c.id}
                title={c.title}
                count={cards.length}
                accent={c.accent}
              >
                {cards.length === 0 ? (
                  <li className="rounded border border-dashed border-hairline p-4 text-center text-xs text-ash">
                    No features
                  </li>
                ) : (
                  cards.map((f) => <KanbanCard key={f.id} feature={f} />)
                )}
              </KanbanColumn>
            );
          })}
        </div>
        <KanbanLaneRow features={filteredList} />
      </div>
    </DndContext>
  );
}