// SPDX-License-Identifier: Apache-2.0
/**
 * Bottom-lane row container — feat-036.
 *
 * Renders the three lanes (Done / Someday / Archive) below the main
 * 3-column kanban grid. Lives inside the parent `<DndContext>` for
 * visual consistency, but does not register any drop targets — the
 * lanes are view-only per spec Step 5.
 *
 * The single source of truth for lane identity is the `LANES`
 * constant below; new lanes are added by appending to the array. The
 * order is the render order (top to bottom).
 *
 * Why three lanes always render, even when `count === 0`? The spec
 * calls for "Archive (0)" to be visible so users know the lane exists
 * and what it will catch once features start getting superseded.
 */

import type { Feature } from "@heddle/shared";

import { KanbanCard } from "./KanbanCard.tsx";
import { KanbanLane } from "./KanbanLane.tsx";
import { laneOf } from "./Kanban.tsx";
import type { KanbanLaneId } from "../lib/state/kanban-store.ts";

const LANES: ReadonlyArray<{
  id: KanbanLaneId;
  title: string;
  icon: string;
  accent: string;
}> = [
  { id: "done", title: "Done", icon: "✓", accent: "text-emerald-400" },
  { id: "someday", title: "Someday", icon: "?", accent: "text-zinc-400" },
  { id: "archive", title: "Archive", icon: "\u{1F5C4}", accent: "text-zinc-500" },
];

const TOOLTIP_TITLE_LIMIT = 3;

interface KanbanLaneRowProps {
  features: readonly Feature[];
}

export function KanbanLaneRow({
  features,
}: KanbanLaneRowProps): React.ReactElement {
  const byLane = new Map<KanbanLaneId, Feature[]>(
    LANES.map((l) => [l.id, [] as Feature[]]),
  );
  for (const f of features) {
    const lid = laneOf(f);
    if (lid === null) continue;
    byLane.get(lid)?.push(f);
  }

  return (
    <div
      aria-label="Bottom lanes"
      className="flex flex-col gap-2"
      data-lane-droppable="false"
    >
      {LANES.map((l) => {
        const cards = byLane.get(l.id) ?? [];
        const topTitles = cards.slice(0, TOOLTIP_TITLE_LIMIT).map(
          (f) => f.description,
        );
        return (
          <KanbanLane
            key={l.id}
            laneId={l.id}
            title={l.title}
            icon={l.icon}
            accent={l.accent}
            count={cards.length}
            topTitlesForTooltip={topTitles}
          >
            {cards.length === 0 ? (
              <li className="rounded border border-dashed border-zinc-700 p-3 text-center text-xs text-zinc-500">
                No features
              </li>
            ) : (
              cards.map((f) => <KanbanCard key={f.id} feature={f} />)
            )}
          </KanbanLane>
        );
      })}
    </div>
  );
}