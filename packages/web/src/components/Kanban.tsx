// SPDX-License-Identifier: Apache-2.0
/**
 * Top-level kanban board — feat-035.
 *
 * Layout: a 3-column responsive grid (`grid-cols-1 md:grid-cols-3`)
 * holding one `<KanbanColumn />` per `KanbanColumnId`. Each column
 * lists the features whose `status` matches its id.
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
 *
 * The data flow is deliberately unidirectional:
 *
 *   server → useFeatures → kanban store → columns → cards
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
} from "../lib/state/kanban-store.ts";
import { toastError } from "../lib/toast.ts";
import { KanbanCard } from "./KanbanCard.tsx";
import { KanbanColumn } from "./KanbanColumn.tsx";

interface KanbanProps {
  projectId: string | null;
}

const COLUMNS: ReadonlyArray<{
  id: KanbanColumnId;
  title: string;
  accent: string;
}> = [
  { id: "in_progress", title: "In progress", accent: "text-blue-400" },
  { id: "ready", title: "Ready", accent: "text-zinc-300" },
  { id: "blocked", title: "Blocked", accent: "text-amber-400" },
];

/**
 * Map a feature row to its column. `pending` and `deferred` both
 * surface in Ready; `passing` is hidden from the main view (it shows
 * up in the bottom-lane rendering that feat-036 will add).
 */
export function columnOf(feature: Feature): KanbanColumnId {
  const s: string = feature.status;
  if (s === "in_progress") return "in_progress";
  if (s === "blocked") return "blocked";
  // pending / deferred / passing all surface in Ready for v0.1;
  // feat-036 will lift `passing` into a bottom lane.
  return "ready";
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
        className="flex flex-1 items-center justify-center rounded border border-dashed border-zinc-700 text-zinc-500"
      >
        Select a project to view its kanban.
      </div>
    );
  }

  if (features.isPending) {
    return (
      <div className="flex flex-1 items-center justify-center text-zinc-500">
        Loading features…
      </div>
    );
  }

  if (features.isError) {
    return (
      <div
        role="alert"
        className="flex flex-1 items-center justify-center text-red-400"
      >
        Error: {features.error.message}
      </div>
    );
  }

  const list = features.data ?? [];
  const byColumn = new Map<KanbanColumnId, Feature[]>(
    COLUMNS.map((c) => [c.id, [] as Feature[]]),
  );
  for (const f of list) {
    byColumn.get(columnOf(f))?.push(f);
  }

  return (
    <DndContext
      sensors={sensors}
      onDragStart={onDragStart}
      onDragOver={(e) => setHover(e.over ? (String(e.over.id) as KanbanColumnId) : null)}
      onDragEnd={onDragEnd}
      onDragCancel={onDragCancel}
    >
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
                <li className="rounded border border-dashed border-zinc-700 p-4 text-center text-xs text-zinc-500">
                  No features
                </li>
              ) : (
                cards.map((f) => <KanbanCard key={f.id} feature={f} />)
              )}
            </KanbanColumn>
          );
        })}
      </div>
    </DndContext>
  );
}