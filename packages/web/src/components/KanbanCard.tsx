// SPDX-License-Identifier: Apache-2.0
/**
 * Single draggable kanban card — feat-035.
 *
 * Renders one feature row inside a column. Uses `useDraggable` so the
 * card is the drag *source*; the column owns the droppable target.
 *
 * Visual: feature id (small, mono) + title + a status badge. The full
 * kind-icon / status-aware badge treatment is feat-037; this card
 * keeps the visual minimal so the kanban-as-board structure ships
 * first and the cosmetic refinement layers on top without redoing the
 * drag wiring.
 *
 * While dragging: 50% opacity + slight rotation, so the source slot
 * remains visible but the dragged ghost reads as "in motion".
 */

import { useDraggable } from "@dnd-kit/core";
import type { Feature, FeatureStatus } from "@heddle/shared";

import { useKanbanStore } from "../lib/state/kanban-store.ts";

interface KanbanCardProps {
  feature: Feature;
}

const STATUS_BADGE: Record<FeatureStatus, string> = {
  pending: "bg-zinc-700 text-zinc-300",
  in_progress: "bg-blue-900 text-blue-200",
  blocked: "bg-amber-900 text-amber-200",
  deferred: "bg-zinc-700 text-zinc-400",
  passing: "bg-emerald-900 text-emerald-200",
};

export function KanbanCard({ feature }: KanbanCardProps): React.ReactElement {
  const { attributes, listeners, setNodeRef, transform, isDragging } =
    useDraggable({ id: feature.id });
  const draggingId = useKanbanStore((s) => s.draggingId);
  // The store drives our own opacity transform (in case parent reorders
  // during drag); dnd-kit's `isDragging` covers the canonical case.
  const storeDragging = draggingId === feature.id;

  const style: React.CSSProperties | undefined = transform
    ? {
        transform: `translate3d(${transform.x}px, ${transform.y}px, 0) rotate(1deg)`,
        zIndex: 50,
      }
    : undefined;

  return (
    <li
      ref={setNodeRef}
      style={style}
      data-feature-id={feature.id}
      data-status={feature.status}
      className={`cursor-grab select-none rounded border border-zinc-700 bg-zinc-800 p-2 text-sm text-zinc-100 shadow-sm transition-opacity ${
        isDragging || storeDragging ? "opacity-50" : "opacity-100"
      }`}
      {...attributes}
      {...listeners}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-xs text-zinc-500">{feature.id}</span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${STATUS_BADGE[feature.status]}`}
        >
          {feature.status.replace("_", " ")}
        </span>
      </div>
      <p className="mt-1 line-clamp-2 text-sm text-zinc-200">{feature.description}</p>
    </li>
  );
}