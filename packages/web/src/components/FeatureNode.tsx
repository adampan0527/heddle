// SPDX-License-Identifier: Apache-2.0
/**
 * Custom ReactFlow node for a single feature — feat-041.
 *
 * Renders the feature id and status badge inside a rounded box
 * colored by status. The `style.opacity` is driven by the parent
 * `<DagView />`'s ego-network state: when a node is "selected"
 * (clicked), all other nodes are dimmed to 30% so the user can
 * trace just that feature's neighborhood.
 *
 * Keeping this in its own file means ReactFlow's `nodeTypes` map
 * stays a one-line import and we don't ship the whole canvas
 * styles down a deeply-nested render path.
 */

import { memo } from "react";
import { Handle, Position, type NodeProps } from "reactflow";

import {
  NODE_HEIGHT,
  NODE_WIDTH,
  STATUS_BORDER,
  STATUS_COLOR,
} from "./dag-view-helpers.ts";

export interface FeatureNodeData {
  id: string;
  status: string;
  description: string;
  /** Set by <DagView />: 1.0 = normal, 0.3 = dimmed. */
  opacity?: number;
  /** Set by <DagView />: the id of the focused node, or null. */
  focusedId?: string | null;
}

function FeatureNodeImpl({
  data,
}: NodeProps<FeatureNodeData>): React.ReactElement {
  const focused = data.focusedId != null && data.focusedId !== data.id;
  const opacity = focused ? 0.3 : (data.opacity ?? 1);
  const fill = STATUS_COLOR[data.status as keyof typeof STATUS_COLOR] ?? "#e7e3d8";
  const border =
    STATUS_BORDER[data.status as keyof typeof STATUS_BORDER] ?? "#a8a195";
  return (
    <div
      data-feature-id={data.id}
      data-dag-node="true"
      data-testid={`dag-node-${data.id}`}
      style={{
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        background: fill,
        border: `2px solid ${border}`,
        borderRadius: 10,
        opacity,
        color: "#202020",
        fontSize: 12,
        padding: 8,
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        boxSizing: "border-box",
      }}
    >
      <Handle type="target" position={Position.Left} />
      <span style={{ fontFamily: "JetBrains Mono, monospace", fontSize: 11 }}>
        {data.id}
      </span>
      <span
        style={{
          fontSize: 10,
          textTransform: "uppercase",
          letterSpacing: 0.5,
          opacity: 0.7,
        }}
      >
        {data.status.replace("_", " ")}
      </span>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

export const FeatureNode = memo(FeatureNodeImpl);