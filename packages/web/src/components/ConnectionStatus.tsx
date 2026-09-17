// SPDX-License-Identifier: Apache-2.0
/**
 * Header `<ConnectionStatus>` pill — feat-033.
 *
 * Subscribes to the singleton `WsClient` (or a caller-provided one) and
 * renders a small colored dot + label indicating the current WS state:
 *
 *   connecting   → grey dot
 *   open         → green dot
 *   reconnecting → amber dot
 *   closed       → red dot
 *
 * The component does not own the client; the singleton in `ws-client.ts`
 * is opened from `App.tsx`'s mount effect and shared across components.
 */

import { useEffect, useState } from "react";

import { getDefaultWsClient, type WsClient, type WsState } from "../lib/ws-client.ts";

interface Props {
  /** Optional override — defaults to the module singleton. */
  client?: WsClient;
}

const STATE_LABEL: Record<WsState, string> = {
  connecting: "Connecting…",
  open: "Connected",
  reconnecting: "Reconnecting…",
  closed: "Disconnected",
};

const STATE_DOT: Record<WsState, string> = {
  connecting: "bg-ash",
  open: "bg-badge-success",
  reconnecting: "bg-amber-500",
  closed: "bg-red-700",
};

export function ConnectionStatus({ client }: Props): React.ReactElement {
  const ws = client ?? getDefaultWsClient();
  const [state, setState] = useState<WsState>(ws.getState());

  useEffect(() => {
    const handler = (next: unknown) => setState(next as WsState);
    ws.on("state", handler);
    // Sync once in case the state changed between construction and mount.
    setState(ws.getState());
    return () => ws.off("state", handler);
  }, [ws]);

  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={`WebSocket ${STATE_LABEL[state]}`}
      className="flex items-center gap-2 text-xs text-charcoal"
      data-state={state}
    >
      <span className={`h-2 w-2 rounded-full ${STATE_DOT[state]}`} aria-hidden="true" />
      <span>{STATE_LABEL[state]}</span>
    </div>
  );
}
