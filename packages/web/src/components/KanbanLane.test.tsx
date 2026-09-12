// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the bottom-lane components — feat-036.
 *
 * Coverage layers:
 *
 *   1. `laneOf` pure mapper (mirrors `columnOf`'s style).
 *   2. `KanbanLaneRow` end-to-end with the spec's Step 6 fixture
 *      (5 passing + 3 deferred + 2 superseded): lane counts and
 *      default expansion states are exactly what the spec promises.
 *   3. `KanbanLane` header behavior: click toggles via the store,
 *      count badge format is `"{icon} {title} ({count})"`, collapsed
 *      count badge carries the top-3 title tooltip, expanded body
 *      carries `min-h-[8rem]`.
 *   4. View-only guarantee: no `useDroppable` id attribute leaks into
 *      the rendered DOM.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, beforeEach } from "vitest";
import type { Feature } from "@heddle/shared";

import { KanbanLaneRow } from "./KanbanLaneRow.tsx";
import { KanbanLane, buildTooltipText } from "./KanbanLane.tsx";
import { laneOf } from "./Kanban.tsx";
import {
  DEFAULT_LANES_EXPANDED,
  useKanbanStore,
  type KanbanLaneId,
} from "../lib/state/kanban-store.ts";

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

function resetKanbanStore(): void {
  useKanbanStore.setState({
    draggingId: null,
    hoverColumn: null,
    lanesExpanded: { ...DEFAULT_LANES_EXPANDED },
  });
}

// ---------- laneOf (table) ----------

describe("laneOf", () => {
  test("passing -> done", () => {
    expect(laneOf(makeFeature({ status: "passing" }))).toBe("done");
  });

  test("deferred -> someday", () => {
    expect(laneOf(makeFeature({ status: "deferred" }))).toBe("someday");
  });

  test("superseded_by != null -> archive", () => {
    expect(
      laneOf(makeFeature({ superseded_by: "feat-099" })),
    ).toBe("archive");
  });

  test("superseded_by != null + passing -> archive (archive wins)", () => {
    expect(
      laneOf(
        makeFeature({ status: "passing", superseded_by: "feat-099" }),
      ),
    ).toBe("archive");
  });

  test("pending -> null (belongs in a column)", () => {
    expect(laneOf(makeFeature({ status: "pending" }))).toBeNull();
  });

  test("in_progress -> null", () => {
    expect(laneOf(makeFeature({ status: "in_progress" }))).toBeNull();
  });

  test("blocked -> null", () => {
    expect(laneOf(makeFeature({ status: "blocked" }))).toBeNull();
  });
});

// ---------- buildTooltipText helper ----------

describe("buildTooltipText", () => {
  test("joins top 3 titles with \\n", () => {
    expect(buildTooltipText(["a", "b", "c", "d"])).toBe("a\nb\nc");
  });

  test("returns empty string when titles array is empty", () => {
    expect(buildTooltipText([])).toBe("");
  });

  test("respects fewer-than-3 titles without padding", () => {
    expect(buildTooltipText(["only"])).toBe("only");
  });

  test("truncates each title to 60 chars", () => {
    const long = "x".repeat(100);
    expect(buildTooltipText([long])).toHaveLength(60);
  });
});

// ---------- <KanbanLaneRow /> end-to-end (spec Step 6) ----------

describe("<KanbanLaneRow /> end-to-end", () => {
  beforeEach(() => resetKanbanStore());

  test("spec Step 6: 5 passing + 3 deferred + 2 superseded", () => {
    const passing: Feature[] = [
      makeFeature({ id: "feat-P1", status: "passing", description: "pass 1" }),
      makeFeature({ id: "feat-P2", status: "passing", description: "pass 2" }),
      makeFeature({ id: "feat-P3", status: "passing", description: "pass 3" }),
      makeFeature({ id: "feat-P4", status: "passing", description: "pass 4" }),
      makeFeature({ id: "feat-P5", status: "passing", description: "pass 5" }),
    ];
    const deferred: Feature[] = [
      makeFeature({ id: "feat-D1", status: "deferred", description: "def 1" }),
      makeFeature({ id: "feat-D2", status: "deferred", description: "def 2" }),
      makeFeature({ id: "feat-D3", status: "deferred", description: "def 3" }),
    ];
    const superseded: Feature[] = [
      makeFeature({
        id: "feat-S1",
        status: "pending",
        superseded_by: "feat-099",
        description: "sup 1",
      }),
      makeFeature({
        id: "feat-S2",
        status: "passing",
        superseded_by: "feat-098",
        description: "sup 2",
      }),
    ];

    renderWithClient(
      <KanbanLaneRow features={[...passing, ...deferred, ...superseded]} />,
    );

    // Header labels.
    expect(screen.getByText("✓ Done (5)")).toBeInTheDocument();
    expect(screen.getByText("? Someday (3)")).toBeInTheDocument();
    expect(screen.getByText("\u{1F5C4} Archive (2)")).toBeInTheDocument();

    // Done is expanded (default) — its body UL is in the DOM.
    const doneLane = screen.getByLabelText(/done lane/i);
    expect(doneLane.querySelector("[data-lane-body]")).not.toBeNull();

    // Someday is collapsed — body is truly unmounted.
    const somedayLane = screen.getByLabelText(/someday lane/i);
    expect(somedayLane.querySelector("[data-lane-body]")).toBeNull();

    // Archive is collapsed — body is truly unmounted.
    const archiveLane = screen.getByLabelText(/archive lane/i);
    expect(archiveLane.querySelector("[data-lane-body]")).toBeNull();
  });

  test("renders all three lanes even when feature list is empty", () => {
    renderWithClient(<KanbanLaneRow features={[]} />);
    expect(screen.getByText("✓ Done (0)")).toBeInTheDocument();
    expect(screen.getByText("? Someday (0)")).toBeInTheDocument();
    expect(screen.getByText("\u{1F5C4} Archive (0)")).toBeInTheDocument();
  });

  test("view-only: no element carries a droppable id for a lane", () => {
    renderWithClient(
      <KanbanLaneRow
        features={[makeFeature({ id: "feat-A", status: "passing" })]}
      />,
    );
    // The wrapper announces itself as not-a-droppable region.
    expect(screen.getByLabelText(/bottom lanes/i)).toHaveAttribute(
      "data-lane-droppable",
      "false",
    );
    // The lane sections are <section>s; dnd-kit adds a data attribute
    // (or ref) only on useDroppable consumers. None should be present.
    expect(document.querySelector("[data-droppable]")).toBeNull();
  });
});

// ---------- <KanbanLane /> interactive behavior ----------

describe("<KanbanLane />", () => {
  beforeEach(() => resetKanbanStore());

  function readExpanded(laneId: KanbanLaneId): boolean {
    return useKanbanStore.getState().lanesExpanded[laneId];
  }

  test("Done renders expanded by default (body in DOM)", () => {
    renderWithClient(
      <KanbanLane
        laneId="done"
        title="Done"
        icon="✓"
        accent="text-emerald-400"
        count={3}
        topTitlesForTooltip={["a", "b", "c"]}
      >
        <li>card</li>
      </KanbanLane>,
    );
    expect(readExpanded("done")).toBe(true);
    expect(screen.getByText("✓ Done (3)")).toBeInTheDocument();
  });

  test("Someday renders collapsed by default (body unmounted)", () => {
    renderWithClient(
      <KanbanLane
        laneId="someday"
        title="Someday"
        icon="?"
        accent="text-zinc-400"
        count={3}
        topTitlesForTooltip={["a", "b", "c"]}
      >
        <li>card</li>
      </KanbanLane>,
    );
    expect(readExpanded("someday")).toBe(false);
    const lane = screen.getByLabelText(/someday lane/i);
    expect(lane.querySelector("[data-lane-body]")).toBeNull();
  });

  test("Archive renders collapsed by default", () => {
    renderWithClient(
      <KanbanLane
        laneId="archive"
        title="Archive"
        icon="\u{1F5C4}"
        accent="text-zinc-500"
        count={0}
        topTitlesForTooltip={[]}
      >
        <li>card</li>
      </KanbanLane>,
    );
    expect(readExpanded("archive")).toBe(false);
  });

  test("clicking the header toggles the lane's expanded state", () => {
    renderWithClient(
      <KanbanLane
        laneId="someday"
        title="Someday"
        icon="?"
        accent="text-zinc-400"
        count={3}
        topTitlesForTooltip={["a", "b", "c"]}
      >
        <li>card</li>
      </KanbanLane>,
    );
    // Initially collapsed.
    expect(readExpanded("someday")).toBe(false);
    const headerButton = screen.getByRole("button", {
      name: /\? Someday \(3\)/i,
    });
    fireEvent.click(headerButton);
    expect(readExpanded("someday")).toBe(true);
    fireEvent.click(headerButton);
    expect(readExpanded("someday")).toBe(false);
  });

  test("count badge format is '{icon} {title} ({count})'", () => {
    renderWithClient(
      <KanbanLane
        laneId="someday"
        title="Someday"
        icon="?"
        accent="text-zinc-400"
        count={3}
        topTitlesForTooltip={[]}
      >
        <li>card</li>
      </KanbanLane>,
    );
    expect(screen.getByText("? Someday (3)")).toBeInTheDocument();
  });

  test("collapsed count badge has a title attribute with the top 3 titles", () => {
    renderWithClient(
      <KanbanLane
        laneId="someday"
        title="Someday"
        icon="?"
        accent="text-zinc-400"
        count={4}
        topTitlesForTooltip={[
          "first deferred",
          "second deferred",
          "third deferred",
          "fourth deferred",
        ]}
      >
        <li>card</li>
      </KanbanLane>,
    );
    // The badge contains the literal count we passed in (4).
    const badge = screen.getByText("4");
    expect(badge).toHaveAttribute(
      "title",
      "first deferred\nsecond deferred\nthird deferred",
    );
  });

  test("expanded count badge has no title attribute", () => {
    renderWithClient(
      <KanbanLane
        laneId="done"
        title="Done"
        icon="✓"
        accent="text-emerald-400"
        count={2}
        topTitlesForTooltip={["x", "y"]}
      >
        <li>card</li>
      </KanbanLane>,
    );
    const badge = screen.getByText("2");
    // When expanded, tooltipText is "" → title attr is omitted (undefined).
    expect(badge).not.toHaveAttribute("title");
  });

  test("expanded body uses min-h-[8rem] for visual hierarchy", () => {
    renderWithClient(
      <KanbanLane
        laneId="done"
        title="Done"
        icon="✓"
        accent="text-emerald-400"
        count={0}
        topTitlesForTooltip={[]}
      >
        <li>card</li>
      </KanbanLane>,
    );
    const body = document.querySelector(
      "[data-lane-body='done']",
    ) as HTMLElement | null;
    expect(body).not.toBeNull();
    // jsdom does not parse Tailwind; assert the class is present.
    expect(body?.className).toContain("min-h-[8rem]");
  });

  test("lane-expand state is ephemeral: re-mounting resets to defaults", () => {
    // First mount, toggle Someday open.
    const first = renderWithClient(
      <KanbanLane
        laneId="someday"
        title="Someday"
        icon="?"
        accent="text-zinc-400"
        count={1}
        topTitlesForTooltip={["a"]}
      >
        <li>card</li>
      </KanbanLane>,
    );
    fireEvent.click(screen.getByRole("button", { name: /\? Someday \(1\)/i }));
    expect(readExpanded("someday")).toBe(true);
    first.unmount();

    // Reset the store to simulate a page reload (the slice is
    // NOT wrapped in persist, so a fresh store has DEFAULT_LANES_EXPANDED).
    resetKanbanStore();
    expect(readExpanded("someday")).toBe(false);
  });
});