// SPDX-License-Identifier: Apache-2.0
/**
 * Pure helpers for the DAG view — feat-041.
 *
 * The ReactFlow canvas needs three things from the feature list:
 *   1. One node per feature, positioned in a tidy left-to-right
 *      column per "generation" so the DAG reads top-down by status
 *      layer (pending → in_progress → passing) and left-to-right
 *      by dependency wave.
 *   2. One edge per `depends_on` entry, pointing from the
 *      dependency to the dependent.
 *   3. A status -> color map used for the node body.
 *
 * Extracted as pure functions so they can be tested in isolation
 * without rendering ReactFlow (which has known SSR / jsdom gotchas).
 */

import type { Edge, Node } from "reactflow";
import type { Feature, FeatureStatus } from "@heddle/shared";

/**
 * Status → hex color maps for DAG nodes.
 *
 * ReactFlow renders each node via inline `style={{ background, border,
 * color }}` (see `FeatureNode.tsx`), so these values must be raw hex
 * — CSS variables don't reach into inline styles reliably across all
 * browsers. The hex values come from `packages/web/src/index.css`
 * (`--color-status-*`) so they stay in sync with the rest of the
 * design system; see VISUAL_DESIGN.md at the repo root.
 *
 * Status palette (cream theme):
 *   pending    — warm gray tint, neutral default
 *   in_progress — orange-50 tint (primary brand signal in motion)
 *   blocked    — amber tint, dependency friction
 *   deferred   — bone tint, paused
 *   passing    — emerald tint, completion
 */
export const STATUS_COLOR: Record<FeatureStatus, string> = {
  pending: "#e7e3d8",
  in_progress: "#fff1eb",
  blocked: "#fbe7d8",
  deferred: "#ece9df",
  passing: "#e1f0e8",
};

/** Border color per status; slightly brighter than the fill. */
export const STATUS_BORDER: Record<FeatureStatus, string> = {
  pending: "#a8a195",
  in_progress: "#ea2804",
  blocked: "#b45309",
  deferred: "#a8a195",
  passing: "#2b9a66",
};

/** Fixed node width / height so the layout math is deterministic. */
export const NODE_WIDTH = 160;
export const NODE_HEIGHT = 60;

/** Horizontal spacing between dependency waves. */
const COL_GAP = 60;
/** Vertical spacing between sibling nodes in the same wave. */
const ROW_GAP = 24;

/**
 * Topological-wave layout: each feature gets a "column" equal to the
 * length of the longest dependency chain ending at that feature. The
 * longest-path DP runs once and the result is a list of waves.
 *
 * Returns an array indexed by feature id -> {x, y}. Features with no
 * deps land in wave 0; missing deps are ignored (the DAG might
 * reference rows that aren't loaded yet).
 */
export function layoutWaves(
  features: readonly Feature[],
): Record<string, { x: number; y: number }> {
  const idSet = new Set(features.map((f) => f.id));
  const depth = new Map<string, number>();
  for (const f of features) depth.set(f.id, 0);
  // Iterate until stable; O(N * max-iter) is fine for project sizes.
  let changed = true;
  let iter = 0;
  while (changed && iter < features.length + 1) {
    changed = false;
    iter += 1;
    for (const f of features) {
      let best = 0;
      for (const dep of f.depends_on) {
        if (!idSet.has(dep)) continue;
        const d = depth.get(dep) ?? 0;
        if (d + 1 > best) best = d + 1;
      }
      if (depth.get(f.id) !== best) {
        depth.set(f.id, best);
        changed = true;
      }
    }
  }
  // Group by wave -> vertical position.
  const waves = new Map<number, Feature[]>();
  for (const f of features) {
    const w = depth.get(f.id) ?? 0;
    const list = waves.get(w) ?? [];
    list.push(f);
    waves.set(w, list);
  }
  const positions: Record<string, { x: number; y: number }> = {};
  for (const [w, list] of waves) {
    list.forEach((f, i) => {
      positions[f.id] = {
        x: w * (NODE_WIDTH + COL_GAP),
        y: i * (NODE_HEIGHT + ROW_GAP),
      };
    });
  }
  return positions;
}

/**
 * Convert a feature list into the ReactFlow node array. One node per
 * feature, with `position` set by `layoutWaves` and `data` carrying
 * the fields the custom renderer needs (id, status, description).
 */
export function featuresToNodes(features: readonly Feature[]): Node[] {
  const positions = layoutWaves(features);
  return features.map((f) => ({
    id: f.id,
    type: "feature",
    position: positions[f.id] ?? { x: 0, y: 0 },
    data: {
      id: f.id,
      status: f.status,
      description: f.description,
    },
  }));
}

/**
 * Convert `depends_on` arrays into ReactFlow edges. One edge per
 * dependency, directed dependency -> dependent.
 *
 * Edges whose source is not in the feature list are dropped (the
 * daemon could reference a not-yet-loaded feature; we don't render a
 * dangling arrow).
 */
export function featuresToEdges(features: readonly Feature[]): Edge[] {
  const idSet = new Set(features.map((f) => f.id));
  const edges: Edge[] = [];
  for (const f of features) {
    for (const dep of f.depends_on) {
      if (!idSet.has(dep)) continue;
      edges.push({
        id: `${dep}->${f.id}`,
        source: dep,
        target: f.id,
        type: "smoothstep",
      });
    }
  }
  return edges;
}