// SPDX-License-Identifier: Apache-2.0
/**
 * MentionAutocomplete dropdown (feat-039 / D-034).
 *
 * Floating dropdown that appears below the dialog textarea when the
 * user has typed `@` followed by 0+ characters at the cursor position.
 * Pure-function helpers (`extractMention`, `filterFeatures`,
 * `insertMention`, `clampSelection`, `MAX_CANDIDATES`) live in
 * `mention-helpers.ts` so they can be unit-tested without React and
 * so this file can stay focused on rendering + interaction.
 *
 * The component is a presentational dropdown — it owns no mention-
 * detection state, no textarea ref. The parent (Dialog) owns the
 * textarea, calls `extractMention(text, cursor)` on every change, and
 * renders this dropdown when a mention is in progress.
 */

import { useEffect, useRef } from "react";
import type { Feature } from "@heddle/shared";

export type { MentionContext } from "./mention-helpers.ts";
export {
  MAX_CANDIDATES,
  clampSelection,
  extractMention,
  filterFeatures,
  insertMention,
} from "./mention-helpers.ts";

/** Maximum rows shown in the dropdown before scrolling. Matches the
 *  spec's "6 candidates at a time with scrolling". */
export const MAX_VISIBLE = 6;

export interface MentionAutocompleteProps {
  /** Filtered, ordered list of candidate features. */
  candidates: readonly Feature[];
  /** Currently highlighted index (0-based). */
  selected: number;
  /** Called when the user clicks a row. */
  onSelect: (feature: Feature) => void;
  /** Called when the user mouses over a row (so keyboard + mouse stay
   *  in sync — keyboard-down then mouseover replaces selection). */
  onHover: (index: number) => void;
  /** Total match count (may exceed `candidates.length` when MAX_CANDIDATES truncates). */
  totalMatches: number;
}

export function MentionAutocomplete({
  candidates,
  selected,
  onSelect,
  onHover,
  totalMatches: _totalMatches,
}: MentionAutocompleteProps): React.ReactElement | null {
  const listRef = useRef<HTMLUListElement | null>(null);

  // Keep the highlighted row in view as `selected` changes (keyboard nav).
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const row = list.querySelector<HTMLElement>(
      `[data-mention-index="${selected}"]`,
    );
    if (!row) return;
    const rowTop = row.offsetTop;
    const rowBottom = rowTop + row.offsetHeight;
    const viewTop = list.scrollTop;
    const viewBottom = viewTop + list.clientHeight;
    if (rowTop < viewTop) list.scrollTop = rowTop;
    else if (rowBottom > viewBottom) list.scrollTop = rowBottom - list.clientHeight;
  }, [selected]);

  if (candidates.length === 0) {
    return (
      <div
        data-testid="mention-empty"
        role="listbox"
        aria-label="Feature mentions"
        className="absolute z-10 mt-1 max-h-48 w-full overflow-y-auto rounded-md border border-hairline-strong bg-surface-card p-2 text-xs text-mute shadow"
      >
        No matching features.
      </div>
    );
  }

  return (
    <ul
      ref={listRef}
      data-testid="mention-list"
      role="listbox"
      aria-label="Feature mentions"
      className="absolute z-10 mt-1 max-h-48 w-full overflow-y-auto rounded-md border border-hairline-strong bg-surface-card text-sm shadow"
    >
      {candidates.map((f, idx) => {
        const isSelected = idx === selected;
        return (
          <li
            key={f.id}
            data-testid={`mention-row-${f.id}`}
            data-mention-index={idx}
            role="option"
            aria-selected={isSelected}
            onMouseDown={(e) => {
              // mousedown (not click) so the textarea's blur doesn't
              // unmount the dropdown before our insert runs.
              e.preventDefault();
              onSelect(f);
            }}
            onMouseEnter={() => onHover(idx)}
            className={
              "cursor-pointer px-3 py-1.5 " +
              (isSelected
                ? "bg-primary text-on-primary"
                : "text-ink hover:bg-canvas")
            }
          >
            <span className="font-code text-xs">{f.id}</span>
            <span className="ml-2 truncate text-xs text-mute">
              {f.description.length > 60
                ? f.description.slice(0, 57) + "…"
                : f.description}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
