// SPDX-License-Identifier: Apache-2.0
/**
 * Kind filter dropdown — feat-056 (D-024).
 *
 * Renders a `<select>` in the kanban header that lets the user scope
 * the board to a single feature kind (`feature` / `bugfix` /
 * `enhancement`) or to all kinds (`all`). The selected value is
 * stored in the kanban store's `kindFilter` slice; `<Kanban />`
 * reads it and filters the feature list before bucketing into
 * columns + lanes.
 *
 * Why a native `<select>` and not a custom menu?
 *
 *   1. There are exactly four options, all single-word. A native
 *      dropdown is the right affordance — keyboard-navigable, screen
 *      reader-friendly, zero JS to open/close, and matches the same
 *      pattern used by `ProjectSwitcher`.
 *   2. We do not need icons, multi-select, or search. Adding a
 *      custom popover here would be YAGNI.
 *
 * The label "Kind" is hidden from the rendered output but attached
 * via `aria-label` so the select is discoverable by assistive tech
 * without polluting the header layout.
 */

import type { FeatureKind } from "@heddle/shared";

import {
  useKanbanStore,
  type KindFilterValue,
} from "../lib/state/kanban-store.ts";

/**
 * The four valid values for the filter dropdown. The first entry
 * (`"all"`) is the sentinel that disables filtering; the next three
 * are the real `FeatureKind` values from the shared domain. Order
 * matters: it is the order the user sees in the dropdown.
 */
const OPTIONS: ReadonlyArray<{ value: KindFilterValue; label: string }> = [
  { value: "all", label: "All kinds" },
  { value: "feature", label: "Features" },
  { value: "bugfix", label: "Bugfixes" },
  { value: "enhancement", label: "Enhancements" },
];

/**
 * Narrow an arbitrary string (e.g. from `<select>`'s `onChange`) to a
 * `KindFilterValue`. Falls back to `"all"` if the value is not one
 * of the four recognized strings — this keeps a stale `localStorage`
 * value or a malformed wire payload from breaking the board.
 *
 * Exported so tests can drive the same narrowing the component uses
 * without going through the DOM.
 */
export function asKindFilterValue(raw: string): KindFilterValue {
  for (const o of OPTIONS) {
    if (o.value === raw) return o.value;
  }
  return "all";
}

/**
 * Pure filter helper: returns the subset of `features` whose `kind`
 * matches `filter`. When `filter === "all"`, returns the input
 * untouched. Exported for tests so the rule is exercised directly.
 *
 * Defensive note: a feature whose `kind` is unknown (e.g. an old
 * daemon row with `"chore"`) is HIDDEN under `"all"`. We do that on
 * purpose — `FeatureKind` is a closed set per feat-010, so an
 * unknown value is a corruption, and silently showing it would
 * surprise the user later when they switched the filter.
 */
export function applyKindFilter<T extends { kind: string }>(
  features: readonly T[],
  filter: KindFilterValue,
): T[] {
  if (filter === "all") return [...features];
  return features.filter((f) => f.kind === filter);
}

/**
 * Narrow a `FeatureKind` to one of the three selectable kinds. We
 * deliberately do NOT include `"all"` here — `FeatureKind` is the
 * domain type and `"all"` is a UI-only sentinel that lives only on
 * the dropdown side.
 */
type SelectableKind = Exclude<FeatureKind, "all">;

export function KindFilter(): React.ReactElement {
  const value = useKanbanStore((s) => s.kindFilter);
  const setKindFilter = useKanbanStore((s) => s.setKindFilter);

  function onChange(e: React.ChangeEvent<HTMLSelectElement>): void {
    setKindFilter(asKindFilterValue(e.target.value));
  }

  // `<select>` carries the current value as a controlled prop, so
  // the dropdown always reflects the store. Empty-value option is
  // intentionally omitted; `DEFAULT_KIND_FILTER` guarantees one of
  // the four OPTIONS always matches.
  return (
    <label className="flex items-center gap-1 text-xs text-zinc-400">
      <span className="sr-only">Filter by kind</span>
      <select
        data-testid="kind-filter"
        aria-label="Filter by kind"
        value={value}
        onChange={onChange}
        className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800 focus:outline-none focus:ring-1 focus:ring-blue-500"
      >
        {OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

// Re-export the selectable-kind union for any sibling that wants to
// typecheck against it (kept here, not in the store, so the store
// does not need to know about the UI's three-pick list).
export type { SelectableKind };
