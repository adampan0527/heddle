// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the kanban board — feat-035.
 *
 * Two layers of coverage:
 *
 *   1. `decideDrop` is a pure function exported alongside the kanban.
 *      We exercise the business rules (single-active, blocked-lock,
 *      no-op moves) with table-driven assertions on its output. This
 *      is the load-bearing part of the spec — every drag-time rule
 *      flows through this function — and a pure-function test is
 *      cheap, deterministic, and stable across dnd-kit upgrades.
 *
 *   2. A render test confirms the three columns appear in the right
 *      order, cards show up under the column matching their status,
 *      and the no-project / pending / error branches render without
 *      throwing.
 *
 * Simulating a real `pointerdown → pointermove → pointerup` drag in
 * jsdom is fragile (dnd-kit's PointerSensor reads getBoundingClientRect
 * offsets that jsdom reports as zero). The pure-function coverage
 * above is what guarantees the rules; the render test guards the
 * column-status wiring.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { Feature } from "@heddle/shared";

import { Kanban, columnOf, decideDrop, laneOf } from "./Kanban.tsx";

vi.mock("../lib/api/features.js", () => ({
  useFeatures: vi.fn(),
  useStartFeature: vi.fn(),
}));

import {
  useFeatures,
  useStartFeature,
} from "../lib/api/features.js";

const useFeaturesMock = vi.mocked(useFeatures);
const useStartFeatureMock = vi.mocked(useStartFeature);

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

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// ---------- decideDrop (pure function) ----------

describe("decideDrop", () => {
  test("ready → in_progress (no other running) returns start", () => {
    const card = makeFeature({ id: "feat-A", status: "pending" });
    expect(decideDrop(card, "in_progress", [card])).toEqual({
      kind: "start",
      featureId: "feat-A",
    });
  });

  test("blocked → ready is rejected with the locked message", () => {
    const card = makeFeature({ id: "feat-B", status: "blocked" });
    expect(decideDrop(card, "ready", [card])).toEqual({
      kind: "reject",
      message: "Blocked features are locked. Resolve dependencies first.",
    });
  });

  test("ready → in_progress with another already running is rejected", () => {
    const running = makeFeature({ id: "feat-RUN", status: "in_progress" });
    const card = makeFeature({ id: "feat-A", status: "pending" });
    expect(decideDrop(card, "in_progress", [running, card])).toEqual({
      kind: "reject",
      message: "Only one feature can run at a time.",
    });
  });

  test("ready → ready is a no-op", () => {
    const card = makeFeature({ id: "feat-A", status: "pending" });
    expect(decideDrop(card, "ready", [card])).toEqual({ kind: "noop" });
  });

  test("ready → blocked is a no-op (no API in v0.1)", () => {
    const card = makeFeature({ id: "feat-A", status: "pending" });
    expect(decideDrop(card, "blocked", [card])).toEqual({ kind: "noop" });
  });
});

// ---------- columnOf ----------

describe("columnOf", () => {
  test("buckets main-column statuses; passing/deferred go to lanes", () => {
    expect(columnOf(makeFeature({ status: "in_progress" }))).toBe("in_progress");
    expect(columnOf(makeFeature({ status: "blocked" }))).toBe("blocked");
    expect(columnOf(makeFeature({ status: "pending" }))).toBe("ready");
    // feat-036: passing and deferred now go to bottom lanes,
    // not the Ready column.
    expect(columnOf(makeFeature({ status: "passing" }))).toBeNull();
    expect(columnOf(makeFeature({ status: "deferred" }))).toBeNull();
  });
});

// ---------- laneOf ----------

describe("laneOf", () => {
  test("passing -> done", () => {
    expect(laneOf(makeFeature({ status: "passing" }))).toBe("done");
  });

  test("deferred -> someday", () => {
    expect(laneOf(makeFeature({ status: "deferred" }))).toBe("someday");
  });

  test("superseded_by != null -> archive (regardless of status)", () => {
    expect(
      laneOf(
        makeFeature({
          status: "pending",
          superseded_by: "feat-999",
        }),
      ),
    ).toBe("archive");
    // A passing + superseded feature also goes to Archive.
    expect(
      laneOf(
        makeFeature({
          status: "passing",
          superseded_by: "feat-999",
        }),
      ),
    ).toBe("archive");
  });

  test("pending / in_progress / blocked -> null (belong in a column)", () => {
    expect(laneOf(makeFeature({ status: "pending" }))).toBeNull();
    expect(laneOf(makeFeature({ status: "in_progress" }))).toBeNull();
    expect(laneOf(makeFeature({ status: "blocked" }))).toBeNull();
  });

  test("superseded_by === null + not passing/deferred -> null", () => {
    expect(
      laneOf(makeFeature({ status: "pending", superseded_by: null })),
    ).toBeNull();
  });
});

// ---------- render ----------

function setupApiMock(features: Feature[]) {
  useFeaturesMock.mockReturnValue({
    data: features,
    isPending: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useFeatures>);
  useStartFeatureMock.mockReturnValue({
    mutate: vi.fn(),
  } as unknown as ReturnType<typeof useStartFeature>);
}

describe("<Kanban /> render", () => {
  test("renders the no-project placeholder when projectId is null", () => {
    setupApiMock([]);
    renderWithClient(<Kanban projectId={null} />);
    expect(screen.getByText(/select a project/i)).toBeInTheDocument();
  });

  test("renders three columns in order: In progress, Ready, Blocked", () => {
    setupApiMock([
      makeFeature({ id: "feat-A", status: "in_progress" }),
      makeFeature({ id: "feat-B", status: "pending" }),
      makeFeature({ id: "feat-C", status: "blocked" }),
    ]);
    renderWithClient(<Kanban projectId="proj-1" />);
    const columns = screen.getAllByRole("region", { name: /column$/i });
    expect(columns).toHaveLength(3);
    expect(columns[0]).toHaveAccessibleName(/in progress column/i);
    expect(columns[1]).toHaveAccessibleName(/ready column/i);
    expect(columns[2]).toHaveAccessibleName(/blocked column/i);
  });

  test("places each card under the column matching its status", () => {
    setupApiMock([
      makeFeature({ id: "feat-A", status: "in_progress" }),
      makeFeature({ id: "feat-B", status: "pending" }),
      makeFeature({ id: "feat-C", status: "blocked" }),
    ]);
    renderWithClient(<Kanban projectId="proj-1" />);
    expect(
      screen.getByText("feat-A").closest("[data-feature-id]"),
    ).toHaveAttribute("data-status", "in_progress");
    expect(
      screen.getByText("feat-B").closest("[data-feature-id]"),
    ).toHaveAttribute("data-status", "pending");
    expect(
      screen.getByText("feat-C").closest("[data-feature-id]"),
    ).toHaveAttribute("data-status", "blocked");
  });

  test("shows loading state", () => {
    useFeaturesMock.mockReturnValue({
      data: undefined,
      isPending: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useFeatures>);
    useStartFeatureMock.mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof useStartFeature>);
    renderWithClient(<Kanban projectId="proj-1" />);
    expect(screen.getByText(/loading features/i)).toBeInTheDocument();
  });

  test("shows error state", () => {
    useFeaturesMock.mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: new Error("boom"),
    } as unknown as ReturnType<typeof useFeatures>);
    useStartFeatureMock.mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof useStartFeature>);
    renderWithClient(<Kanban projectId="proj-1" />);
    expect(screen.getByRole("alert")).toHaveTextContent(/boom/i);
  });

  test("renders an empty-state row in a column with no features", () => {
    setupApiMock([makeFeature({ id: "feat-A", status: "pending" })]);
    renderWithClient(<Kanban projectId="proj-1" />);
    // in_progress and blocked are empty for this fixture
    expect(screen.getAllByText(/no features/i).length).toBeGreaterThanOrEqual(2);
  });

  test("passing features show ONLY in the Done lane, not the Ready column", () => {
    setupApiMock([
      makeFeature({ id: "feat-PASS", status: "passing" }),
      makeFeature({ id: "feat-PEND", status: "pending" }),
    ]);
    renderWithClient(<Kanban projectId="proj-1" />);
    // feat-PASS is rendered (it lives in the Done lane).
    expect(screen.getByText("feat-PASS")).toBeInTheDocument();
    // The Done lane is expanded by default and contains the card.
    const doneLane = screen.getByLabelText(/done lane/i);
    expect(doneLane).toBeInTheDocument();
    expect(doneLane).toHaveTextContent("feat-PASS");
    // The Ready column should NOT contain feat-PASS — only feat-PEND.
    const readyColumn = screen.getByLabelText(/ready column/i);
    expect(readyColumn).toHaveTextContent("feat-PEND");
    expect(readyColumn).not.toHaveTextContent("feat-PASS");
  });

  test("deferred features show ONLY in the Someday lane, not the Ready column", () => {
    setupApiMock([
      makeFeature({ id: "feat-DEF", status: "deferred" }),
      makeFeature({ id: "feat-PEND", status: "pending" }),
    ]);
    renderWithClient(<Kanban projectId="proj-1" />);
    // Someday is collapsed by default; the lane header still renders.
    const somedayLane = screen.getByLabelText(/someday lane/i);
    expect(somedayLane).toBeInTheDocument();
    // The Done lane body exists with the "No features" placeholder;
    // the Someday lane body is null (truly unmounted, since collapsed).
    expect(somedayLane.querySelector("[data-lane-body]")).toBeNull();
    // Ready column should NOT contain feat-DEF.
    const readyColumn = screen.getByLabelText(/ready column/i);
    expect(readyColumn).not.toHaveTextContent("feat-DEF");
  });

  test("all three bottom lanes are rendered, even when empty", () => {
    setupApiMock([makeFeature({ id: "feat-A", status: "pending" })]);
    renderWithClient(<Kanban projectId="proj-1" />);
    expect(screen.getByLabelText(/done lane/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/someday lane/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/archive lane/i)).toBeInTheDocument();
  });

  test("lane row carries data-lane-droppable=false (no useDroppable anywhere)", () => {
    setupApiMock([makeFeature({ id: "feat-A", status: "passing" })]);
    renderWithClient(<Kanban projectId="proj-1" />);
    const laneRow = screen.getByLabelText(/bottom lanes/i);
    expect(laneRow).toHaveAttribute("data-lane-droppable", "false");
    // No element should carry a droppable-lane id attribute.
    expect(document.querySelector("[data-lane-id$='-droppable']")).toBeNull();
  });
});