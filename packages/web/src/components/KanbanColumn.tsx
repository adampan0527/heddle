// SPDX-License-Identifier: Apache-2.0
/**
 * Single kanban column — feat-035.
 *
 * Wraps a `<ul>` of cards in a `useDroppable` zone identified by
 * the column id (`in_progress` | `ready` | `blocked`). The drop
 * target id is the same string the `DndContext.onDragEnd` consumer
 * uses to look up the destination column, so the contract is one
 * string used in two places — keep them in sync.
 *
 * Visual highlight: when the dragged card is hovering this column
 * (read from `useKanbanStore.hoverColumn`), the border becomes
 * blue and the inner panel gets a subtle blue tint. CSS classes are
 * derived from the store value, not from `useDroppable.isOver`,
 * because the latter only fires when the pointer is over the drop
 * container's bounds — a moving pointer that briefly leaves the
 * bounds (which happens at the column edges during fast drags)
 * would otherwise toggle the highlight off.
 */

import { useDroppable } from "@dnd-kit/core";
import type { ReactNode } from "react";

import {
  useKanbanStore,
  type KanbanColumnId,
} from "../lib/state/kanban-store.ts";

interface KanbanColumnProps {
  id: KanbanColumnId;
  title: string;
  count: number;
  /** Subtle accent color for the column header. */
  accent: string;
  children: ReactNode;
}

export function KanbanColumn({
  id,
  title,
  count,
  accent,
  children,
}: KanbanColumnProps): React.ReactElement {
  const { setNodeRef, isOver } = useDroppable({ id });
  const hover = useKanbanStore((s) => s.hoverColumn);
  const highlighted = hover === id || isOver;

  return (
    <section
      aria-label={`${title} column`}
      className={`flex min-h-[24rem] flex-col rounded-md border bg-surface-card ${
        highlighted ? "border-primary" : "border-hairline"
      }`}
    >
      <header
        className={`flex items-center justify-between border-b border-hairline px-3 py-2 text-sm ${accent}`}
      >
        <span className="font-semibold uppercase tracking-wide">{title}</span>
        <span className="rounded-full bg-surface-bone px-2 py-0.5 text-xs text-charcoal">
          {count}
        </span>
      </header>
      <ul
        ref={setNodeRef}
        className="flex flex-1 flex-col gap-2 p-3"
        data-column-id={id}
      >
        {children}
      </ul>
    </section>
  );
}