// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the `@feat-XXX` mention autocomplete (feat-039 / D-034).
 *
 * Three layers of coverage:
 *
 *   1. **Pure helpers** (`extractMention`, `filterFeatures`,
 *      `insertMention`, `clampSelection`) — table-driven assertions
 *      over the function-level contracts. These are the load-bearing
 *      pieces of the spec; pure-function tests are cheap,
 *      deterministic, and stable across React/JSX changes.
 *
 *   2. **Component render** — given a fixed candidate list, the
 *      dropdown lists them, highlights the selected row, and reflects
 *      `selected`/`totalMatches` props.
 *
 *   3. **Interaction smoke test** — clicking a row calls `onSelect`
 *      with the corresponding Feature (we trust the parent Dialog to
 *      do the textarea mutation; that path is covered by
 *      `Dialog.test.tsx`'s existing send-message tests).
 *
 * Mouse + keyboard integration with the textarea is exercised at the
 * Dialog level via the existing `Dialog.test.tsx`; the new
 * `MentionAutocomplete.test.tsx` is the unit-level surface for the
 * feature's spec.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { Feature } from "@heddle/shared";

import {
  MentionAutocomplete,
  clampSelection,
  extractMention,
  filterFeatures,
  insertMention,
  MAX_CANDIDATES,
} from "./MentionAutocomplete.tsx";

function makeFeature(overrides: Partial<Feature>): Feature {
  return {
    id: "feat-001",
    category: "functional",
    description: "test feature",
    steps: [],
    status: "pending",
    priority: "high",
    depends_on: [],
    attempts: [],
    kind: "feature",
    fixes: null,
    enhances: null,
    superseded_by: null,
    implementation_model: null,
    ...overrides,
  };
}

// ---------- extractMention ----------

describe("extractMention", () => {
  test("returns null for empty content", () => {
    expect(extractMention("", 0)).toBeNull();
  });

  test("returns null when cursor is before the @", () => {
    expect(extractMention("hello world", 5)).toBeNull();
  });

  test("detects a bare @ at the cursor", () => {
    expect(extractMention("@", 1)).toEqual({
      start: 0,
      end: 1,
      query: "",
    });
  });

  test("captures the in-progress query between @ and the cursor", () => {
    expect(extractMention("@fe", 3)).toEqual({
      start: 0,
      end: 3,
      query: "fe",
    });
  });

  test("supports a query in the middle of a longer sentence", () => {
    // "hello @feat-03 world" indices: h=0..o=4, sp=5, @=6, f=7,
    // e=8, a=9, t=10, -=11, 0=12, 3=13. Cursor right after `3` is 14.
    expect(extractMention("hello @feat-03 world", 14)).toEqual({
      start: 6,
      end: 14,
      query: "feat-03",
    });
  });

  test("returns null when the cursor is preceded by whitespace", () => {
    expect(extractMention("hello ", 6)).toBeNull();
  });

  test("returns null when no @ precedes the cursor (only mid-word)", () => {
    expect(extractMention("fe", 2)).toBeNull();
  });

  test("matches alphanumerics + dash + underscore (covers feat-039 and temp_001)", () => {
    expect(extractMention("@temp_001 ", 9)).toEqual({
      start: 0,
      end: 9,
      query: "temp_001",
    });
  });

  test("a second @ before the cursor closes the window", () => {
    // "@feat-03@extra" indices: @=0, f=1, e=2, a=3, t=4, -=5, 0=6,
    // 3=7, @=8, e=9, x=10, t=11, r=12, a=13. Cursor after `a` is 14.
    expect(extractMention("@feat-03@extra", 14)).toEqual({
      start: 8,
      end: 14,
      query: "extra",
    });
  });
});

// ---------- filterFeatures ----------

describe("filterFeatures", () => {
  const sample: Feature[] = [
    makeFeature({ id: "feat-001", description: "Monorepo skeleton" }),
    makeFeature({ id: "feat-002", description: "Apache license headers" }),
    makeFeature({ id: "feat-039", description: "@mention autocomplete (D-034)" }),
    makeFeature({ id: "feat-200", description: "draft cards UI (D-013)" }),
  ];

  test("empty query returns every feature", () => {
    expect(filterFeatures(sample, "").map((f) => f.id)).toEqual([
      "feat-001",
      "feat-002",
      "feat-039",
      "feat-200",
    ]);
  });

  test("matches by id substring (case-insensitive)", () => {
    expect(filterFeatures(sample, "FEAT-0").map((f) => f.id)).toEqual([
      "feat-001",
      "feat-002",
      "feat-039",
    ]);
    expect(filterFeatures(sample, "039").map((f) => f.id)).toEqual([
      "feat-039",
    ]);
  });

  test("matches by description substring (case-insensitive)", () => {
    expect(filterFeatures(sample, "license").map((f) => f.id)).toEqual([
      "feat-002",
    ]);
    expect(filterFeatures(sample, "DRAFT").map((f) => f.id)).toEqual([
      "feat-200",
    ]);
  });

  test("returns empty when nothing matches", () => {
    expect(filterFeatures(sample, "xyzzy")).toEqual([]);
  });

  test("preserves input order (no re-sorting)", () => {
    const list = [
      makeFeature({ id: "feat-003", description: "third" }),
      makeFeature({ id: "feat-001", description: "first" }),
      makeFeature({ id: "feat-002", description: "second" }),
    ];
    expect(filterFeatures(list, "").map((f) => f.id)).toEqual([
      "feat-003",
      "feat-001",
      "feat-002",
    ]);
  });
});

// ---------- insertMention ----------

describe("insertMention", () => {
  test("replaces the @-token with `@<id> ` and returns the new cursor position", () => {
    const ctx = { start: 0, end: 3, query: "fe" };
    const f = makeFeature({ id: "feat-003" });
    const result = insertMention("@fe rest", ctx, f);
    // The replacement `@feat-003 ` (10 chars) takes the place of `@fe`
    // (3 chars); the trailing space that was at index 3 survives.
    expect(result).toEqual({ content: "@feat-003  rest", cursor: 10 });
  });

  test("inserts in the middle of a longer sentence", () => {
    // "please @fe do thing" — indices: p=0..e=5, space=6, @=7, f=8,
    // e=9, space=10, d=11, o=12, ... Cursor right after `fe` is 10.
    const content = "please @fe do thing";
    const ctx = extractMention(content, 10)!;
    expect(ctx).toEqual({ start: 7, end: 10, query: "fe" });
    const result = insertMention(
      content,
      ctx,
      makeFeature({ id: "feat-039" }),
    );
    expect(result).toEqual({
      content: "please @feat-039  do thing",
      cursor: 17,
    });
  });

  test("works when the query is empty (bare @)", () => {
    const ctx = { start: 0, end: 1, query: "" };
    const result = insertMention("@ rest", ctx, makeFeature({ id: "feat-005" }));
    expect(result).toEqual({ content: "@feat-005  rest", cursor: 10 });
  });
});

// ---------- clampSelection ----------

describe("clampSelection", () => {
  test("clamps negative to 0", () => {
    expect(clampSelection(-3, 5)).toBe(0);
  });

  test("clamps above range to length - 1", () => {
    expect(clampSelection(99, 5)).toBe(4);
  });

  test("returns the same index when in range", () => {
    expect(clampSelection(2, 5)).toBe(2);
  });

  test("returns 0 when there are no candidates", () => {
    expect(clampSelection(7, 0)).toBe(0);
  });
});

// ---------- MAX_CANDIDATES contract ----------

describe("MAX_CANDIDATES", () => {
  test("is a positive integer used by the caller for slicing", () => {
    expect(MAX_CANDIDATES).toBeGreaterThan(0);
  });
});

// ---------- MentionAutocomplete render ----------

describe("<MentionAutocomplete />", () => {
  const candidates: Feature[] = [
    makeFeature({ id: "feat-001", description: "alpha" }),
    makeFeature({ id: "feat-002", description: "beta" }),
    makeFeature({ id: "feat-003", description: "gamma" }),
  ];

  test("renders one row per candidate with the id as data-testid", () => {
    render(
      <MentionAutocomplete
        candidates={candidates}
        selected={0}
        onSelect={vi.fn()}
        onHover={vi.fn()}
        totalMatches={candidates.length}
      />,
    );
    expect(screen.getByTestId("mention-row-feat-001")).toBeInTheDocument();
    expect(screen.getByTestId("mention-row-feat-002")).toBeInTheDocument();
    expect(screen.getByTestId("mention-row-feat-003")).toBeInTheDocument();
  });

  test("marks the selected row with aria-selected=true", () => {
    render(
      <MentionAutocomplete
        candidates={candidates}
        selected={1}
        onSelect={vi.fn()}
        onHover={vi.fn()}
        totalMatches={candidates.length}
      />,
    );
    expect(
      screen.getByTestId("mention-row-feat-002"),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByTestId("mention-row-feat-001"),
    ).toHaveAttribute("aria-selected", "false");
  });

  test("calls onSelect with the clicked feature", () => {
    const onSelect = vi.fn();
    render(
      <MentionAutocomplete
        candidates={candidates}
        selected={0}
        onSelect={onSelect}
        onHover={vi.fn()}
        totalMatches={candidates.length}
      />,
    );
    fireEvent.mouseDown(screen.getByTestId("mention-row-feat-003"));
    expect(onSelect).toHaveBeenCalledWith(candidates[2]);
  });

  test("calls onHover when the mouse enters a row", () => {
    const onHover = vi.fn();
    render(
      <MentionAutocomplete
        candidates={candidates}
        selected={0}
        onSelect={vi.fn()}
        onHover={onHover}
        totalMatches={candidates.length}
      />,
    );
    fireEvent.mouseEnter(screen.getByTestId("mention-row-feat-002"));
    expect(onHover).toHaveBeenCalledWith(1);
  });

  test("renders an empty-state placeholder when there are no candidates", () => {
    render(
      <MentionAutocomplete
        candidates={[]}
        selected={0}
        onSelect={vi.fn()}
        onHover={vi.fn()}
        totalMatches={0}
      />,
    );
    expect(screen.getByTestId("mention-empty")).toHaveTextContent(
      /no matching features/i,
    );
  });

  test("the dropdown has role=listbox for screen readers", () => {
    render(
      <MentionAutocomplete
        candidates={candidates}
        selected={0}
        onSelect={vi.fn()}
        onHover={vi.fn()}
        totalMatches={candidates.length}
      />,
    );
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });
});
