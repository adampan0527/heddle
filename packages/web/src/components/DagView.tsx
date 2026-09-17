// SPDX-License-Identifier: Apache-2.0
/**
 * DAG view panel — feat-041 (D-058).
 *
 * Slide-in panel from the right edge of the viewport showing the
 * project's feature dependency graph. Rendered with `<ReactFlow>`
 * (one node per feature, one edge per `depends_on` entry).
 *
 * Behavior:
 *   - Pan + zoom are enabled by default; a "Fit" button calls
 *     `useReactFlow().fitView()` so the whole graph snaps into view.
 *   - Clicking a node focuses the ego-network: the node and its
 *     one-hop neighbors stay at full opacity, everything else dims
 *     to 30%. Clicking the empty canvas (or pressing the
 *     "Reset" button) restores full opacity.
 *   - On viewports < 768px (Tailwind v4 `md` breakpoint) the panel
 *     goes fullscreen so the graph is usable on a phone.
 *
 * ReactFlow has known SSR issues (its CSS imports a global
 * stylesheet and reads `window.matchMedia`). We therefore wrap
 * `<ReactFlow>` in a `<ReactFlowProvider>` from the same package
 * and the panel imports `reactflow/dist/style.css` at the top.
 */

import { useCallback, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeMouseHandler,
} from "reactflow";
import "reactflow/dist/style.css";

import type { Feature } from "@heddle/shared";

import { useFeatures } from "../lib/api/features.ts";
import {
  featuresToEdges,
  featuresToNodes,
} from "./dag-view-helpers.ts";
import { FeatureNode, type FeatureNodeData } from "./FeatureNode.tsx";

interface DagViewProps {
  projectId: string | null;
  /** Called when the user clicks the close (×) button. */
  onClose: () => void;
}

/** Fixed custom node type map — one entry per node type. */
const NODE_TYPES = { feature: FeatureNode };

/**
 * Compute the dim mask for the ego-network highlight. Returns a map
 * id -> opacity (0.3 for everything outside the focus node's
 * 1-hop neighborhood, 1.0 for the focus node and its dependencies
 * and dependents).
 *
 * Pure function so it can be unit-tested in isolation.
 */
export function computeOpacity(
  features: readonly Feature[],
  focusedId: string | null,
): Record<string, number> {
  if (focusedId === null) {
    const full: Record<string, number> = {};
    for (const f of features) full[f.id] = 1;
    return full;
  }
  const byId = new Map(features.map((f) => [f.id, f]));
  const focus = byId.get(focusedId);
  if (!focus) {
    const fallback: Record<string, number> = {};
    for (const f of features) fallback[f.id] = 1;
    return fallback;
  }
  const neighbors = new Set<string>([focusedId]);
  for (const dep of focus.depends_on) {
    if (byId.has(dep)) neighbors.add(dep);
  }
  // Reverse edges: features that depend on the focus.
  for (const f of features) {
    if (f.depends_on.includes(focusedId)) neighbors.add(f.id);
  }
  const out: Record<string, number> = {};
  for (const f of features) {
    out[f.id] = neighbors.has(f.id) ? 1 : 0.3;
  }
  return out;
}

/** Outer shell — provides ReactFlow context so the inner panel can
 *  call `useReactFlow().fitView()` from the Fit button. */
export function DagView(props: DagViewProps): React.ReactElement {
  return (
    <ReactFlowProvider>
      <DagViewInner {...props} />
    </ReactFlowProvider>
  );
}

function DagViewInner({ projectId, onClose }: DagViewProps): React.ReactElement {
  const features = useFeatures(projectId);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const { fitView } = useReactFlow();

  const list = features.data ?? [];

  const baseNodes = useMemo(() => featuresToNodes(list), [list]);
  const baseEdges = useMemo(() => featuresToEdges(list), [list]);
  const opacityById = useMemo(
    () => computeOpacity(list, focusedId),
    [list, focusedId],
  );

  const nodes: Node<FeatureNodeData>[] = useMemo(
    () =>
      baseNodes.map((n) => ({
        ...n,
        data: {
          ...(n.data as FeatureNodeData),
          opacity: opacityById[n.id] ?? 1,
          focusedId,
        },
      })),
    [baseNodes, opacityById, focusedId],
  );

  const edges: Edge[] = useMemo(
    () =>
      baseEdges.map((e) => {
        const srcDim = (opacityById[e.source] ?? 1) < 1;
        const tgtDim = (opacityById[e.target] ?? 1) < 1;
        const dim = srcDim && tgtDim;
        return {
          ...e,
          style: {
            ...(e.style ?? {}),
            opacity: dim ? 0.3 : 1,
          },
        };
      }),
    [baseEdges, opacityById],
  );

  const onNodeClick: NodeMouseHandler = useCallback((_event, n) => {
    setFocusedId(n.id);
  }, []);

  const onPaneClick = useCallback(() => {
    setFocusedId(null);
  }, []);

  const onFit = useCallback(() => {
    fitView({ padding: 0.2, duration: 300 });
  }, [fitView]);

  return (
    <aside
      aria-label="DAG view"
      data-dag-open="true"
      className="fixed right-0 top-0 z-40 flex h-screen w-full flex-col border-l border-hairline-strong bg-surface-card text-ink md:w-2/3"
    >
      <header className="flex items-center justify-between border-b border-hairline px-4 py-2">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide">
            DAG view
          </h2>
          {focusedId != null ? (
            <span
              data-testid="dag-focused-id"
              data-dag-focused-id={focusedId}
              className="rounded-full bg-surface-bone px-2 py-0.5 text-xs text-charcoal"
            >
              focused: {focusedId}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            data-testid="dag-fit"
            data-dag-fit
            aria-label="Fit graph to screen"
            onClick={onFit}
            className="rounded-full border border-hairline-strong px-3 py-1 text-xs text-charcoal hover:bg-canvas"
          >
            Fit
          </button>
          {focusedId != null ? (
            <button
              type="button"
              data-testid="dag-reset"
              data-dag-reset
              aria-label="Reset highlight"
              onClick={() => setFocusedId(null)}
              className="rounded-full border border-hairline-strong px-3 py-1 text-xs text-charcoal hover:bg-canvas"
            >
              Reset
            </button>
          ) : null}
          <button
            type="button"
            data-testid="dag-close"
            data-dag-close
            aria-label="Close DAG view"
            onClick={onClose}
            className="rounded-full border border-hairline-strong px-3 py-1 text-xs text-charcoal hover:bg-canvas"
          >
            Close
          </button>
        </div>
      </header>
      <div className="relative flex-1" data-testid="dag-canvas" data-dag-canvas>
        {list.length === 0 ? (
          <div className="flex h-full items-center justify-center text-ash">
            No features to graph.
          </div>
        ) : (
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            minZoom={0.1}
            maxZoom={2}
            proOptions={{ hideAttribution: true }}
          >
            <Background color="#e7e3d8" gap={16} />
            <Controls showInteractive={false} />
          </ReactFlow>
        )}
      </div>
    </aside>
  );
}