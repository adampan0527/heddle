// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the DAG view helpers — feat-041.
 *
 * The pure functions in `dag-view-helpers.ts` (wave layout, node /
 * edge projection, opacity mask) carry the bulk of the logic. We
 * exercise them here without rendering ReactFlow; the render-side
 * coverage lives in `DagView.test.tsx`.
 */

import { describe, expect, test } from "vitest";
import type { Feature } from "@heddle/shared";

import {
  computeOpacity,
} from "./DagView.tsx";
import {
  featuresToEdges,
  featuresToNodes,
  layoutWaves,
  NODE_HEIGHT,
  NODE_WIDTH,
} from "./dag-view-helpers.ts";

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

// ---------- layoutWaves ----------

describe("layoutWaves", () => {
  test("zero-dep features land in wave 0", () => {
    const a = makeFeature({ id: "feat-A" });
    const b = makeFeature({ id: "feat-B" });
    const positions = layoutWaves([a, b]);
    expect(positions["feat-A"]?.x).toBe(0);
    expect(positions["feat-B"]?.x).toBe(0);
  });

  test("dependent features land strictly to the right of their deps", () => {
    const a = makeFeature({ id: "feat-A" });
    const b = makeFeature({ id: "feat-B", depends_on: ["feat-A"] });
    const c = makeFeature({ id: "feat-C", depends_on: ["feat-B"] });
    const positions = layoutWaves([a, b, c]);
    expect(positions["feat-A"]?.x).toBe(0);
    expect(positions["feat-B"]?.x).toBeGreaterThan(positions["feat-A"]!.x);
    expect(positions["feat-C"]?.x).toBeGreaterThan(positions["feat-B"]!.x);
  });

  test("siblings within a wave are stacked vertically", () => {
    const a = makeFeature({ id: "feat-A" });
    const b = makeFeature({ id: "feat-B" });
    const positions = layoutWaves([a, b]);
    expect(positions["feat-A"]?.x).toBe(positions["feat-B"]?.x);
    expect(positions["feat-A"]?.y).not.toBe(positions["feat-B"]?.y);
  });

  test("missing deps are ignored (no NaN coords)", () => {
    const x = makeFeature({ id: "feat-X", depends_on: ["feat-MISSING"] });
    const positions = layoutWaves([x]);
    expect(positions["feat-X"]?.x).toBe(0);
    expect(positions["feat-X"]?.y).toBe(0);
  });
});

// ---------- featuresToNodes ----------

describe("featuresToNodes", () => {
  test("emits one node per feature with deterministic size", () => {
    const a = makeFeature({ id: "feat-A" });
    const b = makeFeature({ id: "feat-B" });
    const nodes = featuresToNodes([a, b]);
    expect(nodes).toHaveLength(2);
    expect(nodes[0]?.type).toBe("feature");
    expect(nodes[0]?.position.x).toBeDefined();
    expect(nodes[0]?.data.id).toBe("feat-A");
    expect(nodes[1]?.data.id).toBe("feat-B");
    // Width is propagated to the renderer via NODE_WIDTH constants
    // so the layout math matches the visual layout.
    expect(NODE_WIDTH).toBeGreaterThan(0);
    expect(NODE_HEIGHT).toBeGreaterThan(0);
  });
});

// ---------- featuresToEdges ----------

describe("featuresToEdges", () => {
  test("emits one edge per dependency, dep -> dependent", () => {
    const a = makeFeature({ id: "feat-A" });
    const b = makeFeature({ id: "feat-B", depends_on: ["feat-A"] });
    const edges = featuresToEdges([a, b]);
    expect(edges).toHaveLength(1);
    expect(edges[0]?.source).toBe("feat-A");
    expect(edges[0]?.target).toBe("feat-B");
  });

  test("drops edges to features not in the list", () => {
    const x = makeFeature({ id: "feat-X", depends_on: ["feat-MISSING"] });
    const edges = featuresToEdges([x]);
    expect(edges).toHaveLength(0);
  });

  test("emits an edge for every depends_on entry", () => {
    const a = makeFeature({ id: "feat-A" });
    const b = makeFeature({ id: "feat-B" });
    const c = makeFeature({ id: "feat-C", depends_on: ["feat-A", "feat-B"] });
    const edges = featuresToEdges([a, b, c]);
    expect(edges).toHaveLength(2);
    expect(edges.map((e) => e.target)).toEqual(["feat-C", "feat-C"]);
  });
});

// ---------- computeOpacity (ego-network) ----------

describe("computeOpacity", () => {
  test("null focus => everything at 1.0", () => {
    const a = makeFeature({ id: "feat-A" });
    const b = makeFeature({ id: "feat-B" });
    expect(computeOpacity([a, b], null)).toEqual({
      "feat-A": 1,
      "feat-B": 1,
    });
  });

  test("focus on a node dims unrelated nodes to 0.3", () => {
    const a = makeFeature({ id: "feat-A" });
    const b = makeFeature({ id: "feat-B" });
    const c = makeFeature({ id: "feat-C" });
    const out = computeOpacity([a, b, c], "feat-A");
    expect(out["feat-A"]).toBe(1);
    expect(out["feat-B"]).toBe(0.3);
    expect(out["feat-C"]).toBe(0.3);
  });

  test("focus keeps the focus's deps and dependents at 1.0", () => {
    const a = makeFeature({ id: "feat-A" });
    const b = makeFeature({ id: "feat-B", depends_on: ["feat-A"] });
    const c = makeFeature({ id: "feat-C", depends_on: ["feat-B"] });
    const out = computeOpacity([a, b, c], "feat-B");
    // focus node + its deps + features that depend on it
    expect(out["feat-B"]).toBe(1);
    expect(out["feat-A"]).toBe(1);
    expect(out["feat-C"]).toBe(1);
  });

  test("focus on a missing id falls back to full opacity", () => {
    const a = makeFeature({ id: "feat-A" });
    expect(computeOpacity([a], "feat-MISSING")).toEqual({ "feat-A": 1 });
  });
});