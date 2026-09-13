# Plan: feat-036 — Bottom-lane rendering

**Source PRD**: `feature_list.json` (feat-036)
**Selected Milestone**: feat-036 — Bottom-lane rendering (D-056)
**Complexity**: Medium

## Summary

Lift `passing`, `deferred`, and superseded features out of the Ready column into three new collapsible lanes below the main kanban: Done (`passing`), Someday (`deferred`), Archive (`superseded_by != null`). Each lane is a row under the main three-column grid with its own header (always-visible card count, formatted as `? Someday (3)`) and a collapsible body (Done default-expanded; Someday/Archive default-collapsed). Hovering the count badge on a collapsed lane surfaces a native `title` tooltip listing the top 3 feature titles (Step 4 of the spec). Lane interactions are view-only — no drag in or out, no inline edit. The lane bodies render no `useDroppable` so dnd-kit cannot deliver a drop event to a lane id.

## Patterns to Mirror
| Category | Source | Pattern |
|---|---|---|
| Naming | `packages/web/src/components/Kanban.tsx:59-67` | `COLUMNS: ReadonlyArray<{id, title, accent}>` — single-source-of-truth list; we mirror with `LANES` (id, title, accent, defaultExpanded) |
| Naming | `packages/web/src/components/Kanban.tsx:74-81` | Pure `columnOf(feature)` mapper; we mirror with `laneOf(feature)` that returns null for non-lane features |
| Naming | `packages/web/src/components/KanbanColumn.tsx:38-71` | Drop-target wrapper with header + count badge; we mirror with `KanbanLane` (no `useDroppable` at all — lanes are view-only per spec Step 5) |
| Naming | `packages/web/src/lib/state/kanban-store.ts:19` | `KanbanColumnId` string-union type; we mirror with `KanbanLaneId = "done" | "someday" | "archive"` |
| Naming | `packages/web/src/lib/state/kanban-store.ts:32-38` | Zustand store for ephemeral UI state (NOT persisted); lane-expand state lives here too, following the same pattern. Default: `{done: true, someday: false, archive: false}` |
| Errors | `packages/web/src/components/KanbanColumn.tsx:45-47` | `useDroppable({id})` — we DELIBERATELY do NOT call this in `KanbanLane`. View-only = no droppable (spec Step 5) |
| Tests | `packages/web/src/components/Kanban.test.tsx:74-121` | Pure-function `decideDrop` / `columnOf` tested table-driven; we mirror with `laneOf` table |
| Tests | `packages/web/src/components/Kanban.test.tsx:137-209` | `renderWithClient` + mocked `useFeatures`/`useStartFeature`; we extend with lane-render assertions using the same helper |
| Tooltips | `packages/web/src/components/KanbanColumn.tsx` | We use a native HTML `title` attribute on the count badge (no `react-tooltip` dep). The `title` attr is accessible without JS and is the spec's "hover preview" semantic — no fancy popover needed for v0.1 |
| File layout | `packages/web/src/components/` | One component per file; new `KanbanLane.tsx` + `KanbanLaneRow.tsx` |

## Files to Change
| File | Action | Why |
|---|---|---|
| `packages/web/src/lib/state/kanban-store.ts` | UPDATE | Add `KanbanLaneId` type + `lanesExpanded: Record<KanbanLaneId, boolean>` slice + `setLaneExpanded(id, expanded)` action. Defaults: `{done: true, someday: false, archive: false}`. NOT persisted (session-only). Add `DEFAULT_LANES_EXPANDED` constant exported for testability |
| `packages/web/src/components/KanbanLane.tsx` | CREATE | Single-lane component. Props: `{laneId, title, icon, accent, count, topTitlesForTooltip, defaultExpanded}`. Reads expanded state from `useKanbanStore.lanesExpanded[laneId]`. Header is a `<button aria-expanded={expanded}>` with the count badge (`{title} ({count})`) — when collapsed, the count badge has a `title` attribute listing the top 3 feature titles (joined by `\n`). When collapsed, body is `null` (truly unmounted) |
| `packages/web/src/components/KanbanLaneRow.tsx` | CREATE | Top-level lane-row container. Props: `{features: Feature[]}`. Iterates the three lane configs, buckets features via `laneOf`, computes the top 3 titles per lane (for tooltip), renders three `<KanbanLane>`s. Renders inside the existing `DndContext` for consistency with the main grid (no droppable registration — view-only) |
| `packages/web/src/components/Kanban.tsx` | UPDATE | (a) `columnOf` no longer routes `passing`/`deferred` to `ready`. New mapping: `in_progress → "in_progress"`, `blocked → "blocked"`, `pending → "ready"`, all others → `null`. (b) Below the main 3-column grid, render `<KanbanLaneRow features={list} />` (still inside `DndContext` for consistency). (c) The main grid only shows `in_progress` / `pending` / `blocked` features now. (d) Update the `columnOf` test in `Kanban.test.tsx:113-121` to reflect the new mapping |
| `packages/web/src/components/KanbanLane.test.tsx` | CREATE | vitest tests: (a) `laneOf` table — `passing → done`, `deferred → someday`, `superseded_by != null → archive`, others → `null`; (b) Done renders expanded by default; Someday/Archive collapsed; (c) Header click toggles expanded state via the store; (d) Count badge is `? Someday (3)` formatted (icon + space + title + space + parens); (e) Collapsed count badge has a `title` attribute with the top 3 feature titles; (f) Expanded count badge has no `title` attribute (or has it but it's irrelevant since the body is visible); (g) Lane-expand state is ephemeral (re-mounting resets to defaults); (h) Visual hierarchy: lane body uses `min-h-[8rem]` (smaller than main column's `min-h-[24rem]`) |
| `packages/web/src/components/Kanban.test.tsx` | UPDATE | Update `columnOf` test expectations: `passing`/`deferred` no longer map to `ready` (they return null → routed to lanes). Add a render test: `passing` feature appears in Done lane, NOT in Ready column |
| `feature_list.json` | UPDATE via script | After tests pass: `mark-in-progress feat-036` → `mark-passing feat-036` |

## Tasks
### Task 1: Extend kanban store with `lanesExpanded` slice
- **Action**: Add `KanbanLaneId` type + `lanesExpanded: Record<KanbanLaneId, boolean>` + `setLaneExpanded(id, expanded)` to `useKanbanStore`. Defaults: `{done: true, someday: false, archive: false}`. Export `DEFAULT_LANES_EXPANDED` for tests. Do NOT wrap with `persist` (session-only, per the kanban-store design comment on lines 8-14: drag state resets on every page load; same applies to lane-expand state).
- **Mirror**: `kanban-store.ts:32-38` (existing `setDragging`/`setHover`/`reset` actions).
- **Validate**: Existing `pnpm --filter web test` passes; no regressions.

### Task 2: `laneOf` pure mapper
- **Action**: Export `laneOf(feature): KanbanLaneId | null`. Mapping: `feature.status === "passing" → "done"`; `feature.status === "deferred" → "someday"`; `feature.superseded_by != null → "archive"`; otherwise `null`. Mirror `columnOf`'s shape (`packages/web/src/components/Kanban.tsx:74-81`). Place in `Kanban.tsx` alongside `columnOf` (do not create a new module unless `Kanban.tsx` exceeds 250 lines after this PR).
- **Mirror**: `Kanban.tsx:74-81` (`columnOf`).
- **Validate**: New `laneOf` tests in `KanbanLane.test.tsx` cover all four branches.

### Task 3: `KanbanLane` component
- **Action**: `packages/web/src/components/KanbanLane.tsx`. Props: `{laneId: KanbanLaneId, title: string, icon: string, accent: string, count: number, topTitlesForTooltip: string[], defaultExpanded: boolean}`. Reads `lanesExpanded[laneId]` from the store via `useKanbanStore` selector; updates via `setLaneExpanded(laneId, !expanded)`. Header is a `<button aria-expanded={expanded} aria-controls="lane-body-{laneId}">` with the formatted label `"{icon} {title} ({count})"` — when collapsed, the count badge (`{count}`) carries a `title` attribute containing the top 3 titles joined by `"\n"`. When collapsed, body is `null` (truly unmounted — no `useDroppable`). When expanded, body is a `<ul>` containing the children (cards). Body uses `min-h-[8rem]` (smaller than main columns for visual hierarchy).
- **Mirror**: `KanbanColumn.tsx:38-71` (header + count badge) — but stripped of droppable wiring per spec Step 5.
- **Validate**: vitest render tests in `KanbanLane.test.tsx` (Task 5).

### Task 4: Update `Kanban.tsx` to use lanes
- **Action**: 
  1. `columnOf` no longer routes `passing` / `deferred` to `ready`. New mapping: `in_progress → "in_progress"`, `blocked → "blocked"`, `pending → "ready"`, all others → `null` (routed to lanes via `laneOf`).
  2. The main 3-column grid iterates features and skips those where `columnOf(f) === null` (they go to lanes, not columns).
  3. Below the grid (still inside `DndContext`, after the `<div className="grid">` closes but before `</DndContext>`), render `<KanbanLaneRow features={list} />`.
  4. The "No features" empty-state row in each main column only shows when the FILTERED list for that column is empty.
  5. Update the `columnOf` test in `Kanban.test.tsx:113-121` to reflect the new mapping.
- **Mirror**: `Kanban.tsx:74-81` (`columnOf`) + `Kanban.tsx:193-235` (render + filter).
- **Validate**: `pnpm --filter web test Kanban.test.tsx` green; the new test for `passing → Done lane, NOT Ready column` passes.

### Task 5: `KanbanLaneRow` + tests
- **Action**: `packages/web/src/components/KanbanLaneRow.tsx`. Iterates the three lane configs:
  ```ts
  const LANES: ReadonlyArray<{id: KanbanLaneId; title: string; icon: string; accent: string; defaultExpanded: boolean}> = [
    { id: "done", title: "Done", icon: "✓", accent: "text-emerald-400", defaultExpanded: true },
    { id: "someday", title: "Someday", icon: "⏳", accent: "text-zinc-400", defaultExpanded: false },
    { id: "archive", title: "Archive", icon: "🗄", accent: "text-zinc-500", defaultExpanded: false },
  ];
  ```
  Buckets features via `laneOf`. For each lane, computes `topTitlesForTooltip = features.slice(0, 3).map(f => f.description.slice(0, 60))` (truncate to 60 chars to keep tooltips readable). Renders a `<KanbanLane>` for each (even if `count === 0`, so the user sees "Archive (0)" exists). New tests in `KanbanLane.test.tsx`:
  - `laneOf` table: `passing → done`, `deferred → someday`, `superseded_by: "feat-XXX" → archive`, `pending → null`, `in_progress → null`, `blocked → null`.
  - Done renders expanded by default; Someday/Archive collapsed.
  - Header click toggles expanded state via `setLaneExpanded` (assert the store state changes).
  - Count badge format: `? Someday (3)` (icon + space + title + space + parens around count). Assert via `screen.getByText("? Someday (3)")` or equivalent.
  - Collapsed count badge has `title` attribute with the top 3 titles joined by `\n`. E.g. for features `[f1, f2, f3, f4]`, the title is `"f1 description\nf2 description\nf3 description"`.
  - When expanded, the body `<ul>` is in the DOM with `min-h-[8rem]`.
  - Visual hierarchy: lane body has `min-h-[8rem]` (vs main columns' `min-h-[24rem]`) — assert via className matching.
  - Render with 5 passing + 3 deferred + 2 superseded features → assert lane counts (Done=5, Someday=3, Archive=2) and default expansion (Done expanded, others collapsed). This is the explicit Step 6 test bullet.
  - **`useDroppable` is NOT used anywhere** — assert by checking the rendered DOM has no `[data-lane-droppable]` attribute.
- **Mirror**: `Kanban.test.tsx:74-109` (table-driven tests) + `Kanban.test.tsx:137-209` (render tests).
- **Validate**: `pnpm --filter web test KanbanLane.test.tsx` green.

### Task 6: End-to-end + handoff_check + commit
- **Action**:
  1. `pnpm --filter web typecheck` clean.
  2. `pnpm --filter web test` green (all existing + new tests).
  3. `python HARNESS/tools/handoff_check.py` → 10/10 PASS.
  4. `python HARNESS/tools/feature_list.py mark-in-progress feat-036` → `mark-passing feat-036`.
  5. Atomic commit: `git add -- feature_list.json current_progress.txt packages/web && git commit -m "Implement: feat-036 bottom-lane rendering (D-056)"`.
  6. Append SESSION block to `current_progress.txt` per `tools/session_end.py`.
- **Validate**: `git log --oneline -5` shows the new commit; `python HARNESS/tools/feature_list.py status` shows `passing: 32` (was 31).

## Validation
```bash
# typecheck
cd packages/web && pnpm typecheck

# unit tests for the new lane components + updated columnOf
cd packages/web && pnpm --filter web test KanbanLane.test.tsx Kanban.test.tsx

# full web suite (no regression)
cd packages/web && pnpm test

# handoff_check 10/10 PASS gate
python HARNESS/tools/handoff_check.py

# Manual smoke (in a separate shell):
#   1. start heddle-web (Vite dev server, port 5173)
#   2. open http://localhost:5173
#   3. select a project with 5 passing + 3 deferred + 2 superseded features
#   4. assert: Done lane header reads "? Done (5)" and body is expanded with 5 cards
#   5. assert: Someday lane header reads "? Someday (3)" and body is collapsed
#   6. assert: Archive lane header reads "? Archive (2)" and body is collapsed
#   7. hover the Someday count badge with mouse → tooltip shows top 3 deferred titles
#   8. click Someday header → body expands; assert 3 cards appear
#   9. click Done header → body collapses; count still visible
#   10. reload the page → Done is expanded again, Someday/Archive are collapsed
#   11. attempt to drag a Ready column card into a lane → no drop target visible, drag cancels silently
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Existing `columnOf` test breaks when mapping changes | Medium | Updated in Task 4; the existing assertions are rewritten to assert the new mapping (`passing → null`, `deferred → null`) |
| A `passing` feature shows up in BOTH the Ready column AND the Done lane | Low | The render loop in `Kanban.tsx` filters `null`-mapped features OUT of the main grid (Task 4 step 2). Verified by the new test `passing feature does not appear in ready column` |
| Archive lane surfaces NOTHING because `superseded_by` is never set in v0.1 (feat-054 hasn't shipped) | Low | This is expected — the Archive lane will be empty in v0.1, which matches the spec's intent. The lane still renders with "(0)" so the user sees it exists. feat-054 will start populating `superseded_by` |
| Tooltip text too long → native browser truncates it or breaks lines awkwardly | Low | Native `title` is a single line in most browsers; we use `\n` separators which most browsers respect. Each title is already truncated to 60 chars in `KanbanLaneRow`. Spec says "top 3 titles" which fits comfortably |
| Lane-expand state persists across page reload (UX surprise) | Low | We explicitly do NOT wrap the slice in `persist`; refresh resets to defaults (Task 1). Documented in `kanban-store.ts` with a comment explaining why (mirrors `draggingId` ephemerality) |
| Drag from main column into a lane silently fails (UX surprise) | Low | Lanes are intentionally view-only per spec Step 5. The drag handler's `decideDrop` returns `noop` for any overId that's not a `KanbanColumnId` (the lane ids are typed as `KanbanLaneId`, a disjoint union — TypeScript enforces this at compile time). The drag cancels silently. No toast, no error — lanes simply don't accept drops |
| Three new tests for lane state don't catch a regression where the lane body is rendered but hidden via `display:none` (vs truly unmounted) | Low | The test asserts the lane body element (`<ul>` inside `KanbanLane`) is NOT in the DOM when collapsed, not just hidden. This forces the implementation to truly unmount |
| `pnpm test -- file.test.ts` syntax error (Vitest doesn't accept `--`) | **Resolved by design** | Plan uses `pnpm --filter web test file.test.ts` (single positional, no `--`) |
| `columnOf` returning `null` for `passing`/`deferred` could mask a future refactor where someone expects a column id | Low | The function's JSDoc explicitly states the contract: "returns null for features that belong in a lane, not a column". The function is internal to the kanban — only `Kanban.tsx` and its test consume it |
| `superseded_by` field could be `""` (empty string) — null check `!= null` would still be truthy in JS | Low | Per feat-010/feat-016, `superseded_by` is either `null` or a feature-id string like `"feat-XXX"`. Empty strings are not in the schema. If we encounter them, the test asserts `f.superseded_by === null → null lane` |

## Acceptance
- [ ] `packages/web/src/lib/state/kanban-store.ts` exports `KanbanLaneId`, `lanesExpanded`, `setLaneExpanded`, `DEFAULT_LANES_EXPANDED`
- [ ] `packages/web/src/components/KanbanLane.tsx` exists; collapses/expands via store; count always visible as `"{icon} {title} ({count})"`; collapsed count badge has `title` attribute with top 3 titles joined by `\n`
- [ ] `packages/web/src/components/KanbanLaneRow.tsx` exists; renders Done/Someday/Archive with the correct mapping (`passing → done`, `deferred → someday`, `superseded_by != null → archive`)
- [ ] `packages/web/src/components/Kanban.tsx` updated: `passing` features show ONLY in Done lane (not Ready); `deferred` features show ONLY in Someday lane (not Ready); `superseded_by != null` features show ONLY in Archive lane
- [ ] Lanes are view-only (no `useDroppable`, no inline edit) per spec Step 5
- [ ] Lane expand state is ephemeral (NOT persisted via Zustand `persist` middleware)
- [ ] Visual hierarchy: lane body uses `min-h-[8rem]` (vs main columns' `min-h-[24rem]`)
- [ ] All new vitest tests pass; the explicit Step 6 test (5 passing + 3 deferred + 2 superseded) passes
- [ ] No regressions in `Kanban.test.tsx` (existing 9 tests stay green after `columnOf` mapping update)
- [ ] `python HARNESS/tools/handoff_check.py` reports 10/10 PASS
- [ ] `pnpm --filter web typecheck` clean
- [ ] Manual smoke: Done expanded by default with "✓ Done (5)"; Someday/Archive collapsed with "? Someday (3)" / "? Archive (2)"; hovering collapsed count shows tooltip with top 3 titles; clicking header toggles; page refresh resets state
- [ ] `python HARNESS/tools/feature_list.py mark-passing feat-036` succeeds; `metadata.passing` increments by 1; SESSION block appended to `current_progress.txt`; atomic commit `Implement: feat-036 bottom-lane rendering (D-056)` exists in `git log --oneline -5`