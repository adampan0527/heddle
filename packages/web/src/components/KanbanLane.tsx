// SPDX-License-Identifier: Apache-2.0
/**
 * Single bottom-lane view — feat-036.
 *
 * Mirrors `KanbanColumn` but is intentionally **view-only**:
 *   - No `useDroppable` is registered. dnd-kit therefore cannot
 *     deliver a drop event to a lane id. The drop handler in
 *     `<Kanban />` rejects unknown targets via `decideDrop` returning
 *     noop, so a stray drop onto a lane is a silent no-op.
 *   - No inline edit affordance. Cards inside the lane are rendered
 *     but not draggable; users change feature state via dialog
 *     commands (feat-038..feat-043), not by dragging a card out of
 *     the lane.
 *
 * Visual hierarchy: lane bodies use `min-h-[8rem]` (smaller than
 * main columns' `min-h-[24rem]`) so the lanes read as secondary
 * content under the primary 3-column board.
 *
 * When collapsed: the body is **truly unmounted** (`null`), not
 * hidden via CSS, so the test suite can assert absence from the DOM.
 *
 * The count badge is always visible — even when collapsed — and
 * carries a `title` attribute listing the top 3 titles joined by
 * `\n`, so a hover surfaces a native browser tooltip with the
 * preview.
 */

import type { ReactNode } from "react";

import {
  useKanbanStore,
  type KanbanLaneId,
} from "../lib/state/kanban-store.ts";

interface KanbanLaneProps {
  laneId: KanbanLaneId;
  title: string;
  /** Single-character icon shown in the header label. */
  icon: string;
  /** Tailwind accent class for the header text. */
  accent: string;
  /** Always-visible count, formatted as "{icon} {title} ({count})". */
  count: number;
  /**
   * Top-N feature titles for the count-badge tooltip. Only attached
   * as a `title` attribute when the lane is collapsed (the spec's
   * "hover preview" semantic).
   */
  topTitlesForTooltip: string[];
  children: ReactNode;
}

const TOOLTIP_MAX_TITLES = 3;
const TITLE_PREVIEW_MAX_CHARS = 60;

export function truncateForTooltip(s: string): string {
  if (s.length <= TITLE_PREVIEW_MAX_CHARS) return s;
  return s.slice(0, TITLE_PREVIEW_MAX_CHARS);
}

export function buildTooltipText(titles: string[]): string {
  return titles
    .slice(0, TOOLTIP_MAX_TITLES)
    .map(truncateForTooltip)
    .join("\n");
}

export function KanbanLane({
  laneId,
  title,
  icon,
  accent,
  count,
  topTitlesForTooltip,
  children,
}: KanbanLaneProps): React.ReactElement {
  const expanded = useKanbanStore((s) => s.lanesExpanded[laneId]);
  const setLaneExpanded = useKanbanStore((s) => s.setLaneExpanded);

  const label = `${icon} ${title} (${count})`;
  const tooltipText = expanded ? "" : buildTooltipText(topTitlesForTooltip);
  const bodyId = `lane-body-${laneId}`;

  return (
    <section
      aria-label={`${title} lane`}
      data-lane-id={laneId}
      className="flex flex-col rounded border border-zinc-800 bg-zinc-900"
    >
      <header
        className={`flex items-center justify-between border-b border-zinc-800 px-3 py-1.5 text-sm ${accent}`}
      >
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setLaneExpanded(laneId, !expanded)}
          className="flex flex-1 items-center justify-between gap-2 text-left font-semibold uppercase tracking-wide"
        >
          <span>{label}</span>
          <span
            className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-400"
            title={tooltipText || undefined}
          >
            {count}
          </span>
        </button>
      </header>
      {expanded ? (
        <ul
          id={bodyId}
          data-lane-body={laneId}
          className="flex min-h-[8rem] flex-col gap-2 p-3"
        >
          {children}
        </ul>
      ) : null}
    </section>
  );
}