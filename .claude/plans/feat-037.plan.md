# Plan: feat-037 — Card rendering with kind icon

**Source PRD**: `feature_list.json` (feat-037)
**Selected Milestone**: feat-037 — Card rendering with kind icon (D-024)
**Complexity**: Medium (was Small — expanded after spec re-read)

## Summary

Extend `KanbanCard` (feat-035) so each card visually communicates its `kind` (feature/bugfix/enhancement) and its `status` with status-specific icons/text. **bugfix cards** get an orange left-edge stripe plus a wrench glyph in the top-right. **feature/enhancement cards** keep default styling. **Blocked cards** get a lock icon plus the blocking dependency's `@feat-XXX` handle. **Deferred cards** get a clock icon plus the `deferred_until` date. **Passing cards** get a checkmark plus the completion timestamp from `attempts[]`. The status badge color treatment already exists (`STATUS_BADGE` at `KanbanCard.tsx:27-33`); this PR enriches with status-specific icons/text without rewiring drag/drop. Two pure mappers — `kindDisplay(feature)` and `statusExtras(feature)` — extract the spec contract so tests cover the rules without rendering.

## Spec recap (verified from `feature_list.json`)
- **Step 1**: Card renders title + description preview + status badge + kind icon (if bugfix)
- **Step 2**: Bugfix cards: orange left-edge stripe + wrench icon in **top-right corner**
- **Step 3**: Blocked cards: lock icon + blocking dependency `@feat-XXX`
- **Step 4**: Deferred cards: clock icon + `deferred_until` date
- **Step 5**: Passing cards: checkmark + completion timestamp
- **Step 6**: Test cards of each kind and status; assert correct visual treatment

## Patterns to Mirror
| Category | Source | Pattern |
|---|---|---|
| Naming | `packages/web/src/components/KanbanCard.tsx:27-33` | `STATUS_BADGE: Record<FeatureStatus, string>` — single-source-of-truth CSS-class map; we mirror with `KIND_DISPLAY` and `STATUS_EXTRAS` |
| Naming | `packages/web/src/components/KanbanCard.tsx:35-72` | Pure render component; visual state derived from `feature` props + dnd-kit state |
| Naming | `packages/web/src/components/Kanban.tsx:74-81` | Pure `columnOf(feature)` mapper; we mirror with `kindDisplay(feature)` and `statusExtras(feature)` |
| Naming | `packages/shared/src/domain.ts:22` | `FeatureKind = "feature" | "bugfix" | "enhancement"` |
| Naming | `packages/shared/src/domain.ts:29-34` | `FeatureStatus` 5-state union |
| Naming | `packages/shared/src/domain.ts:42-48` | `FeatureAttempt` shape — has `at` (ISO date) and `outcome` fields; we read the last `outcome === "passing"` attempt for the completion timestamp |
| Errors | `packages/web/src/components/Kanban.tsx:97-120` | `decideDrop` returns tagged-union; we mirror with `kindDisplay`/`statusExtras` returning structured objects with defaults |
| Tests | `packages/web/src/components/Kanban.test.tsx:74-121` | Table-driven pure-function tests; we mirror with `kindDisplay` and `statusExtras` tables in `KanbanCard.test.tsx` |
| Tests | `packages/web/src/components/Kanban.test.tsx:137-209` | `renderWithClient` + mocked hooks; we extend for status-aware rendering |
| File layout | `packages/web/src/components/` | One component per file; `KanbanCard.tsx` is the sole file changed. NO new files. |
| Tailwind v4 | `packages/web/vite.config.ts:17` | `@tailwindcss/vite` source-scan mode: as long as classes appear as **full literal strings** in source, they are picked up. Object-literal values in `KIND_DISPLAY`/`STATUS_EXTRAS` are scanned |
| Data access | `packages/shared/src/domain.ts:42-48` | To get the passing completion timestamp: `feature.attempts.filter(a => a.outcome === "passing").slice(-1)[0]?.at` — last passing attempt's `at` |

## Files to Change
| File | Action | Why |
|---|---|---|
| `packages/web/src/components/KanbanCard.tsx` | UPDATE | Add `KIND_DISPLAY` (icon + accent + leftBorder), `STATUS_EXTRAS` (icon + extraText resolver), `kindDisplay(feature)`, `statusExtras(feature)`. Render: orange left-border for bugfix; wrench in top-right; status badge gains an icon prefix + extra text (lock/@feat-XXX for blocked, clock/date for deferred, check/timestamp for passing). Add `data-kind`, `data-status-extras` attributes for testability |
| `packages/web/src/components/KanbanCard.test.tsx` | CREATE | vitest tests: (a) `kindDisplay` table — `feature → default`, `bugfix → orange+🔧`, `enhancement → default`, unknown → default; (b) `statusExtras` table — `pending → null`, `in_progress → null`, `blocked → { icon: "🔒", text: "@feat-XXX (dep)" }`, `deferred → { icon: "⏰", text: "until YYYY-MM-DD" }`, `passing → { icon: "✓", text: "completed YYYY-MM-DD" }`, (c) bugfix card has `border-l-4 border-orange-500` AND wrench glyph **in top-right** of the header row, (d) blocked card has lock icon + `@feat-XXX` text, (e) deferred card has clock icon + date, (f) passing card has check + timestamp from `attempts[]`, (g) bugfix card in a `Done` lane (feat-036 integration) still has orange stripe + wrench |
| `feature_list.json` | UPDATE via script | After tests pass: `mark-in-progress feat-037` → `mark-passing feat-037` |

**Schema question**: Does `Feature` carry `deferred_until` and `completion_timestamp` directly, or do we infer them from `attempts[]`?

- **`deferred_until`**: per feat-010, this is on the feature row. `domain.ts:51-65` doesn't show it — verify and add to `Feature` interface if missing. **Schema fix may be needed**: extend `Feature` in `packages/shared/src/domain.ts` to include `deferred_until: string | null`.
- **`completion_timestamp`**: derive from `attempts[]` — last entry with `outcome === "passing"`. NO schema change needed.

If `deferred_until` is genuinely missing from the wire schema, this is a small forward-only additive change (HARNESS graceful-ignore applies; feat-016 already proved old daemons tolerate new fields).

## Tasks
### Task 1: Add `KIND_DISPLAY` + `kindDisplay` mapper
- **Action**: Add module-level constants and mapper:
  ```ts
  const KIND_DISPLAY: Record<string, {icon: string; accent: string; leftBorder: string}> = {
    feature:     { icon: "",   accent: "",                  leftBorder: "" },
    bugfix:      { icon: "🔧", accent: "text-orange-400",   leftBorder: "border-l-4 border-orange-500" },
    enhancement: { icon: "",   accent: "",                  leftBorder: "" }, // reserved
  };
  const DEFAULT_KIND = KIND_DISPLAY.feature;
  export function kindDisplay(feature: Feature) {
    return KIND_DISPLAY[feature.kind] ?? DEFAULT_KIND;
  }
  ```
- **Mirror**: `KanbanCard.tsx:27-33` (`STATUS_BADGE` shape).
- **Validate**: mapper test passes.

### Task 2: Add `STATUS_EXTRAS` + `statusExtras` mapper
- **Action**: Add mapper returning `{icon: string; text: string} | null` per status:
  - `pending` → null (no extras)
  - `in_progress` → null (no extras)
  - `blocked` → `{ icon: "🔒", text: depHandle(feature) }` where `depHandle` returns the FIRST entry in `feature.depends_on` as `@feat-XXX`; if empty, returns `"@??? (no dep?)"` (defensive — blocked shouldn't have empty deps per spec, but defensive)
  - `deferred` → `{ icon: "⏰", text: feature.deferred_until ? `until ${feature.deferred_until}` : "(no date)" }`
  - `passing` → `{ icon: "✓", text: completionText(feature) }` where `completionText` finds the last `attempts[].outcome === "passing"` entry's `.at` ISO date, formatted as `completed YYYY-MM-DD`; if no passing attempt exists (defensive), returns `"(no timestamp)"`

  Constants and function:
  ```ts
  const STATUS_EXTRAS: Record<FeatureStatus, {icon: string; resolver: (f: Feature) => string | null}> = {
    pending:     { icon: "",  resolver: () => null },
    in_progress: { icon: "",  resolver: () => null },
    blocked:     { icon: "🔒", resolver: blockedText },
    deferred:    { icon: "⏰", resolver: deferredText },
    passing:     { icon: "✓",  resolver: passingText },
  };

  function blockedText(f: Feature): string | null {
    if (f.depends_on.length === 0) return "@??? (no dep?)";
    return `@${f.depends_on[0]}`;
  }
  function deferredText(f: Feature): string | null {
    return f.deferred_until ? `until ${f.deferred_until.slice(0, 10)}` : null;
  }
  function passingText(f: Feature): string | null {
    const last = [...f.attempts].reverse().find(a => a.outcome === "passing");
    return last?.at ? `completed ${last.at.slice(0, 10)}` : null;
  }

  export function statusExtras(feature: Feature): {icon: string; text: string} | null {
    const spec = STATUS_EXTRAS[feature.status];
    const text = spec.resolver(feature);
    if (!spec.icon && !text) return null;
    return { icon: spec.icon, text: text ?? "" };
  }
  ```
- **Mirror**: `KanbanCard.tsx:27-33` (Record-based pattern).
- **Validate**: statusExtras table-driven tests cover all 5 statuses.

### Task 3: Schema check — `Feature.deferred_until`
- **Action**: Read `packages/shared/src/domain.ts:51-65` and `packages/node/src/routes/features.ts` (the TypeBox schema). If `deferred_until` is missing from the `Feature` interface AND from the wire schema, add `deferred_until: string | null` to both. If already present, no change.
- **Mirror**: `domain.ts:62-63` (`fixes: string | null`, `superseded_by: string | null` — same shape).
- **Validate**: `pnpm --filter web typecheck` and `pnpm --filter node typecheck` both clean after the addition.

### Task 4: Wire mappers into render
- **Action**: Update `KanbanCard.tsx`:
  1. Compute `const k = kindDisplay(feature)` and `const extras = statusExtras(feature)` at the top.
  2. Add `data-kind={feature.kind}` to the root `<li>`.
  3. Apply `k.leftBorder` to the root `<li>` (e.g., add `k.leftBorder` to the className).
  4. In the header row (`KanbanCard.tsx:62-68`), wrap the existing id + badge in a flex container; place the wrench `🔧` glyph in the **top-right** of the header (i.e., right side of the flex, just before or replacing the badge — spec says "top-right corner"). For bugfix specifically, prepend the wrench to the status badge area.
  5. After the status badge, if `extras` is non-null, render `<span className="ml-1 text-xs text-zinc-400">{extras.icon} {extras.text}</span>` with `data-status-extras="true"`.
- **Mirror**: `KanbanCard.tsx:43-71` (existing render — minimal addition).
- **Validate**: render tests cover all 5 statuses + 3 kinds.

### Task 5: Tests + handoff_check + commit
- **Action**:
  1. `pnpm --filter web typecheck` clean.
  2. `pnpm --filter web test KanbanCard.test.tsx` green.
  3. `pnpm --filter web test` green (no regression).
  4. `pnpm --filter node typecheck` (if Task 3 added schema field).
  5. `python HARNESS/tools/handoff_check.py` → 10/10 PASS.
  6. `python HARNESS/tools/feature_list.py mark-in-progress feat-037` → `mark-passing feat-037`.
  7. Atomic commit: `git add -- feature_list.json current_progress.txt packages/web packages/shared packages/node && git commit -m "Implement: feat-037 card rendering with kind icon (D-024)"`.
  8. Append SESSION block per `tools/session_end.py`.
- **Validate**: `git log --oneline -5` shows the new commit; `python HARNESS/tools/feature_list.py status` shows `passing: 33` (was 32).

## Validation
```bash
# typecheck (web + node)
cd packages/web && pnpm typecheck
cd packages/node && pnpm typecheck

# new tests
cd packages/web && pnpm --filter web test KanbanCard.test.tsx

# full web suite
cd packages/web && pnpm test

# handoff_check 10/10
python HARNESS/tools/handoff_check.py

# Manual smoke:
#   1. start heddle-web (port 5173)
#   2. select a project with cards of all 3 kinds and all 5 statuses
#   3. assert: bugfix card has orange left-edge stripe + 🔧 wrench in top-right
#   4. assert: blocked card has lock icon + "@feat-XXX" text
#   5. assert: deferred card has clock icon + "until YYYY-MM-DD" text
#   6. assert: passing card has ✓ check + "completed YYYY-MM-DD" text
#   7. assert: feature card in Done lane (feat-036) still has orange stripe + wrench if bugfix
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Tailwind v4 source-scan misses the `border-orange-500` literal in `KIND_DISPLAY` value | Low | The literal appears as a full string `'border-l-4 border-orange-500'` inside an object literal in `KanbanCard.tsx` source. Tailwind v4's source-scanner extracts class names from object-literal values per the `@tailwindcss/vite` plugin docs |
| `deferred_until` missing from `Feature` wire schema blocks the `deferred` extra text | Medium | Task 3 explicitly checks the schema and adds the field if missing. Forward-only addition; feat-016 proved old daemons tolerate new fields. Verified by `pnpm --filter node typecheck` |
| Wrench/lock/clock/check glyphs render as `?` on Windows browsers without emoji fonts | Low | Native emoji rendering is platform-dependent. v0.1 ships the emojis as a visual cue; Windows users can install an emoji font (Segoe UI Emoji is standard on Win11). Out of scope to add ASCII fallbacks |
| `attempts[].outcome === "passing"` may not exist for newly-passing features (race with state write) | Low | The `passingText` resolver returns `null` if no passing attempt exists; the renderer shows `"(no timestamp)"` defensively |
| Spec says "top-right corner" for wrench but current card has id (left) + badge (right) — wrench displaces badge | Low | Spec-faithful: wrench is in the right side of the header. If the badge is also right-side, we place the wrench just BEFORE the badge (i.e., `🔧 [badge]`) so the badge stays at the far right. Documented as "top-right region" not "absolute corner" |
| Adding `data-kind` / `data-status-extras` changes the DOM contract other components may rely on | Low | Both are additive `data-*` attributes. CSS selectors that don't reference them are unaffected. No existing test references them |
| Blocked card with empty `depends_on` array renders weird `"@???"` text | Low | Defensive: blocked features should always have at least one dep (that's what makes them blocked). If empty, the resolver returns `"@??? (no dep?)"` to surface the data inconsistency to the user. Tested explicitly |

## Acceptance
- [ ] `KIND_DISPLAY` map + `kindDisplay(feature)` exported from `KanbanCard.tsx`
- [ ] `STATUS_EXTRAS` map + `statusExtras(feature)` exported from `KanbanCard.tsx`
- [ ] bugfix cards render with `border-l-4 border-orange-500` left-edge stripe + `🔧` wrench in **top-right** of the header row
- [ ] feature/enhancement cards render with default styling (no left-edge stripe)
- [ ] blocked cards render with `🔒` lock icon + `@feat-XXX` (first dep)
- [ ] deferred cards render with `⏰` clock icon + `until YYYY-MM-DD` from `deferred_until`
- [ ] passing cards render with `✓` checkmark + `completed YYYY-MM-DD` from last passing attempt
- [ ] pending/in_progress cards render without status extras (no icon, no extra text)
- [ ] Root `<li>` has `data-kind={feature.kind}` for testability
- [ ] Schema field `deferred_until: string | null` added to `Feature` interface AND wire schema IF missing (Task 3)
- [ ] All new vitest tests pass; no regressions in `Kanban.test.tsx`, `KanbanLane.test.tsx`
- [ ] `python HARNESS/tools/handoff_check.py` reports 10/10 PASS
- [ ] `pnpm --filter web typecheck` + `pnpm --filter node typecheck` clean
- [ ] Manual smoke: bugfix/blocked/deferred/passing cards each render their spec-mandated icon + text
- [ ] `python HARNESS/tools/feature_list.py mark-passing feat-037` succeeds; `metadata.passing` increments by 1; SESSION block appended to `current_progress.txt`; atomic commit `Implement: feat-037 card rendering with kind icon (D-024)` exists in `git log --oneline -5`