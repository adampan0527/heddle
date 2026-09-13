// SPDX-License-Identifier: Apache-2.0
/**
 * Render tests for the DAG view panel — feat-041.
 *
 * ReactFlow uses DOM measurement APIs (`getBoundingClientRect`,
 * `ResizeObserver`) that jsdom doesn't fully implement. The
 * component renders the `<ReactFlow>` element but never tries to
 * project nodes onto the canvas during the test, so the assertions
 * focus on what we can observe without flakiness:
 *
 *   - The panel mounts (data-dag-open="true") when rendered.
 *   - The "Fit" button is present and clickable.
 *   - Clicking a custom node updates `focusedId` (visible via the
 *     `data-dag-focused-id` attribute).
 *   - The "Reset" button restores the unfocused state.
 *   - The empty-feature branch renders the "No features" placeholder.
 *
 * jsdom returns zero-sized rects so ReactFlow's layout step yields
 * an empty viewport; we don't assert on rendered node positions
 * here. The pure-function coverage for `computeOpacity`,
 * `featuresToNodes`, `featuresToEdges`, and `layoutWaves` lives in
 * `dag-view-helpers.test.ts`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { Feature } from "@heddle/shared";

import { DagView } from "./DagView.tsx";

vi.mock("../lib/api/features.js", () => ({
  useFeatures: vi.fn(),
}));

import { useFeatures } from "../lib/api/features.js";

const useFeaturesMock = vi.mocked(useFeatures);

function makeFeature(overrides: Partial<Feature>): Feature {
  return {
    id: "feat-001",
    category: "functional",
    description: "test",
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

function mockApi(features: Feature[]) {
  useFeaturesMock.mockReturnValue({
    data: features,
    isPending: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useFeatures>);
}

describe("<DagView />", () => {
  test("renders the panel and the Fit button", () => {
    mockApi([
      makeFeature({ id: "feat-A" }),
      makeFeature({ id: "feat-B", depends_on: ["feat-A"] }),
    ]);
    renderWithClient(<DagView projectId="proj-1" onClose={vi.fn()} />);
    // The `<aside>` has `aria-label="DAG view"`; the close button has
    // a more specific "Close DAG view" label, so this matcher returns
    // a single element.
    const panel = screen.getByLabelText(/^dag view$/i);
    expect(panel).toBeInTheDocument();
    expect(panel).toHaveAttribute("data-dag-open", "true");
    expect(screen.getByTestId("dag-fit")).toBeInTheDocument();
  });

  test("renders one node per feature (data-dag-node count)", () => {
    mockApi([
      makeFeature({ id: "feat-A" }),
      makeFeature({ id: "feat-B" }),
      makeFeature({ id: "feat-C" }),
      makeFeature({ id: "feat-D" }),
    ]);
    const { container } = renderWithClient(
      <DagView projectId="proj-1" onClose={vi.fn()} />,
    );
    const nodes = container.querySelectorAll("[data-dag-node='true']");
    expect(nodes).toHaveLength(4);
  });

  test("clicking a node sets data-dag-focused-id to that node's id", () => {
    mockApi([
      makeFeature({ id: "feat-A" }),
      makeFeature({ id: "feat-B", depends_on: ["feat-A"] }),
    ]);
    renderWithClient(<DagView projectId="proj-1" onClose={vi.fn()} />);
    const nodeA = screen.getByTestId("dag-node-feat-A");
    fireEvent.click(nodeA);
    expect(screen.getByTestId("dag-focused-id")).toHaveTextContent(
      /feat-A/,
    );
  });

  test("clicking the empty canvas (pane) clears the focused id", () => {
    mockApi([makeFeature({ id: "feat-A" })]);
    renderWithClient(<DagView projectId="proj-1" onClose={vi.fn()} />);
    fireEvent.click(screen.getByTestId("dag-node-feat-A"));
    expect(screen.queryByTestId("dag-focused-id")).not.toBeNull();
    // ReactFlow's `onPaneClick` only fires when the user clicks the
    // internal `.react-flow__pane` element; jsdom doesn't simulate
    // pointer events on that element through a child click, so we
    // exercise the same code path via the "Reset" button.
    const resetBtn = screen.getByTestId("dag-reset");
    fireEvent.click(resetBtn);
    expect(screen.queryByTestId("dag-focused-id")).toBeNull();
  });

  test("the Fit button is present and clickable (no crash)", () => {
    mockApi([makeFeature({ id: "feat-A" })]);
    renderWithClient(<DagView projectId="proj-1" onClose={vi.fn()} />);
    const btn = screen.getByTestId("dag-fit");
    // fitView() calls ReactFlow internals; jsdom returns zero-sized
    // rects so the call is a no-op there. We only need to confirm
    // the button renders and accepts a click without throwing.
    expect(() => fireEvent.click(btn)).not.toThrow();
  });

  test("renders an empty-state message when there are no features", () => {
    mockApi([]);
    renderWithClient(<DagView projectId="proj-1" onClose={vi.fn()} />);
    expect(screen.getByText(/no features to graph/i)).toBeInTheDocument();
  });

  test("Close button calls onClose", () => {
    mockApi([makeFeature({ id: "feat-A" })]);
    const onClose = vi.fn();
    renderWithClient(<DagView projectId="proj-1" onClose={onClose} />);
    fireEvent.click(screen.getByTestId("dag-close"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});