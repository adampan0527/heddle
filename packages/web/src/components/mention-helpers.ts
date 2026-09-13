// SPDX-License-Identifier: Apache-2.0
/**
 * Pure helpers for the `@feat-XXX` mention autocomplete (feat-039).
 *
 * Kept in a separate module from the React component so each file
 * stays small (CODE_STYLE.md Part 1: "Short files. Aim for ≤ 200
 * lines per file excluding comments and blank lines") and so the
 * helpers can be unit-tested without React or jsdom. Mirrors the
 * `decideDrop` / `columnOf` / `laneOf` pattern in Kanban.tsx, where
 * the pure functions live alongside the component but are also
 * re-exported for testability.
 *
 * Public API:
 *   - `extractMention(content, cursor)`  → parses an in-progress @-token
 *   - `filterFeatures(features, query)`  → case-insensitive substring filter
 *   - `insertMention(content, ctx, f)`   → compose the post-mention state
 *   - `clampSelection(selected, len)`    → keep highlight inside list
 *   - `MAX_CANDIDATES`                   → hard cap on filtered results
 */

import type { Feature } from "@heddle/shared";

export interface MentionContext {
  /** `@`-index in the textarea content (inclusive). */
  start: number;
  /** Cursor index in the textarea content (exclusive end of query). */
  end: number;
  /** Text after the `@`, lowercase, used for filtering. */
  query: string;
}

/**
 * Parse the textarea content + selectionStart into a mention context.
 *
 * Returns `null` when the cursor is not on / after an `@` token. An
 * `@` is "active" when the character immediately before the cursor is
 * `@`, or when the cursor is in the middle of a contiguous
 * `@<letters/digits/dashes>` token (so `f` after typing `@fe` keeps the
 * dropdown open while filtering on `fe`).
 *
 * Match characters: alphanumerics + `-` + `_` (covers `feat-039`,
 * `temp_001`). Whitespace, `@`, or any other punctuation closes the
 * mention window.
 */
export function extractMention(
  content: string,
  cursor: number,
): MentionContext | null {
  if (cursor < 1) return null;
  if (cursor > content.length) return null;
  let i = cursor - 1;
  while (i >= 0) {
    const ch = content[i];
    if (ch === "@") {
      const query = content.slice(i + 1, cursor);
      return { start: i, end: cursor, query };
    }
    if (ch === undefined) return null;
    // Mention-query characters are ASCII alphanumerics + `-` + `_`.
    // Anything else (whitespace, punctuation, a second `@`) closes the
    // window without a match.
    if (!/[A-Za-z0-9_-]/.test(ch)) return null;
    i--;
  }
  return null;
}

/**
 * Case-insensitive substring filter over `id` and `description`.
 * Empty query matches everything (so `@` alone shows the full list,
 * capped at MAX_CANDIDATES below).
 */
export function filterFeatures(
  features: readonly Feature[],
  query: string,
): Feature[] {
  const q = query.toLowerCase();
  const matched: Feature[] = [];
  for (const f of features) {
    if (q.length === 0) {
      matched.push(f);
      continue;
    }
    if (f.id.toLowerCase().includes(q)) {
      matched.push(f);
      continue;
    }
    if (f.description.toLowerCase().includes(q)) {
      matched.push(f);
    }
  }
  return matched;
}

/**
 * Compose the post-mention textarea state: replace `[start..end]` with
 * `@<id> ` and return a new `content` string plus the cursor index
 * (placed after the inserted space).
 */
export function insertMention(
  content: string,
  ctx: MentionContext,
  feature: Feature,
): { content: string; cursor: number } {
  const replacement = `@${feature.id} `;
  const next =
    content.slice(0, ctx.start) + replacement + content.slice(ctx.end);
  return { content: next, cursor: ctx.start + replacement.length };
}

/** Clamp `selected` into `[0, candidates.length - 1]`. Used after the
 *  candidate list changes shape (e.g. user backspaces the query down
 *  to 3 results). */
export function clampSelection(
  selected: number,
  candidatesLength: number,
): number {
  if (candidatesLength === 0) return 0;
  if (selected < 0) return 0;
  if (selected >= candidatesLength) return candidatesLength - 1;
  return selected;
}

/** Hard cap on filtered results — protects against huge projects. */
export const MAX_CANDIDATES = 50;
