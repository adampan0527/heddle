// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the kind filter dropdown — feat-056 (D-024).
 *
 * Two layers:
 *
 *   1. Pure helpers (`asKindFilterValue`, `applyKindFilter`) — table-
 *      driven so any future change to the valid value set fails loudly.
 *
 *   2. Component render + store integration — covers the render
 *      surface (default value, options list), the controlled `<select>`
 *      wiring, and the end-to-end "click an option → store updates"
 *      round-trip via `fireEvent.change`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import type { Feature } from "@heddle/shared";

import { KindFilter, applyKindFilter, asKindFilterValue } from "./KindFilter.tsx";
import { useKanbanStore } from "../lib/state/kanban-store.ts";

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

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
    deferred_until: null,
    ...overrides,
  };
}

// ---------- pure helpers ----------

describe("asKindFilterValue", () => {
  test("returns 'all' for the sentinel", () => {
    expect(asKindFilterValue("all")).toBe("all");
  });

  test("returns each FeatureKind verbatim", () => {
    expect(asKindFilterValue("feature")).toBe("feature");
    expect(asKindFilterValue("bugfix")).toBe("bugfix");
    expect(asKindFilterValue("enhancement")).toBe("enhancement");
  });

  test("falls back to 'all' for unknown values", () => {
    expect(asKindFilterValue("")).toBe("all");
    expect(asKindFilterValue("chore")).toBe("all");
    expect(asKindFilterValue("FEATURE")).toBe("all"); // case-sensitive
  });
});

describe("applyKindFilter", () => {
  const features: Feature[] = [
    makeFeature({ id: "feat-A", kind: "feature" }),
    makeFeature({ id: "feat-B", kind: "bugfix" }),
    makeFeature({ id: "feat-C", kind: "enhancement" }),
    makeFeature({ id: "feat-D", kind: "feature" }),
  ];

  test("'all' returns input untouched", () => {
    expect(applyKindFilter(features, "all")).toEqual(features);
  });

  test("'feature' filters to feature-kind rows", () => {
    const result = applyKindFilter(features, "feature");
    expect(result.map((f) => f.id)).toEqual(["feat-A", "feat-D"]);
  });

  test("'bugfix' filters to bugfix-kind rows", () => {
    const result = applyKindFilter(features, "bugfix");
    expect(result.map((f) => f.id)).toEqual(["feat-B"]);
  });

  test("'enhancement' filters to enhancement-kind rows", () => {
    const result = applyKindFilter(features, "enhancement");
    expect(result.map((f) => f.id)).toEqual(["feat-C"]);
  });

  test("empty input returns empty", () => {
    expect(applyKindFilter([], "feature")).toEqual([]);
  });
});

// ---------- component render ----------

describe("<KindFilter />", () => {
  test("renders the four options in the canonical order", () => {
    renderWithClient(<KindFilter />);
    const select = screen.getByTestId("kind-filter") as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toEqual(["all", "feature", "bugfix", "enhancement"]);
  });

  test("defaults to 'all'", () => {
    useKanbanStore.setState({ kindFilter: "all" });
    renderWithClient(<KindFilter />);
    const select = screen.getByTestId("kind-filter") as HTMLSelectElement;
    expect(select.value).toBe("all");
  });

  test("reflects an existing store value (controlled)", () => {
    useKanbanStore.setState({ kindFilter: "bugfix" });
    renderWithClient(<KindFilter />);
    const select = screen.getByTestId("kind-filter") as HTMLSelectElement;
    expect(select.value).toBe("bugfix");
  });

  test("changing the dropdown updates the store", () => {
    useKanbanStore.setState({ kindFilter: "all" });
    renderWithClient(<KindFilter />);
    const select = screen.getByTestId("kind-filter") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "enhancement" } });
    expect(useKanbanStore.getState().kindFilter).toBe("enhancement");
  });

  test("exposes a screen-reader-only label via aria-label", () => {
    renderWithClient(<KindFilter />);
    expect(screen.getByLabelText(/filter by kind/i)).toBeInTheDocument();
  });
});