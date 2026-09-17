// SPDX-License-Identifier: Apache-2.0
/**
 * Top-level app shell — feat-032.
 *
 * Renders the header with `<ProjectSwitcher />`, `<SandboxIndicator />`,
 * and `<ConnectionStatus />`, plus the kanban (feat-035) and the
 * persistent bottom dialog (feat-038). Tailwind v4 utility classes only.
 *
 * feat-033 wires the WebSocket singleton: opened on mount via
 * `getDefaultWsClient().connect()`, and torn down via `close()` on
 * unmount. Strict-mode-safe (the connect/close pair is idempotent).
 *
 * feat-055 / D-053: the SandboxIndicator lives in the header and
 * surfaces / mutates the active project's sandbox level. The
 * ConfirmationDialog is mounted at the root and reads its state from
 * the ui-store so synthetic (test) destructive actions and future
 * WS-driven sandbox requests share a single render path.
 */

import { useCallback, useEffect } from "react";

import { ConfirmationDialog } from "./components/ConfirmationDialog.tsx";
import { ConnectionStatus } from "./components/ConnectionStatus.tsx";
import { DagView } from "./components/DagView.tsx";
import { Dialog } from "./components/Dialog.tsx";
import { DraftTray } from "./components/DraftTray.tsx";
import { Kanban } from "./components/Kanban.tsx";
import { ProjectSwitcher } from "./components/ProjectSwitcher.tsx";
import { SandboxIndicator } from "./components/SandboxIndicator.tsx";
import { getDefaultWsClient } from "./lib/ws-client.ts";
import { useUiStore } from "./lib/state/ui-store.ts";

export default function App(): React.ReactElement {
  const activeProjectId = useUiStore((s) => s.activeProjectId);
  const dagViewOpen = useUiStore((s) => s.dagViewOpen);
  const setDagViewOpen = useUiStore((s) => s.setDagViewOpen);
  const pendingSandbox = useUiStore((s) => s.pendingSandboxRequest);
  const setPendingSandbox = useUiStore((s) => s.setPendingSandboxRequest);

  useEffect(() => {
    const ws = getDefaultWsClient();
    ws.connect();
    return () => {
      ws.close();
    };
  }, []);

  // feat-055: synthesise a "test destructive" sandbox request. v0.1
  // demo path — when the user clicks the SandboxIndicator's test
  // button, we park a PendingSandboxRequest so the
  // ConfirmationDialog renders. Approve/Deny/Cancel just resolve the
  // promise and clear the request; a real WS-driven destructive call
  // will replace the stub `approve` body with the actual tool-call
  // handshake.
  const onTestDestructive = useCallback((): void => {
    setPendingSandbox({
      id: `sb-${Date.now()}`,
      toolName: "write",
      args: {
        path: "src/example.ts",
        content: "// synthetic destructive action triggered by the test button",
      },
      approve: async () => {
        // No-op in v0.1: in production this would resolve a WS
        // handshake that unblocks the agent's middleware dispatch.
      },
      deny: () => undefined,
    });
  }, [setPendingSandbox]);

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center justify-between border-b border-hairline-strong px-6 py-3 bg-canvas">
        <h1 className="text-lg font-semibold text-ink">heddle</h1>
        <div className="flex items-center gap-4">
          <ProjectSwitcher />
          <SandboxIndicator
            projectId={activeProjectId}
            onTestDestructive={onTestDestructive}
          />
          <ConnectionStatus />
        </div>
      </header>

      <main className="flex flex-1 flex-col gap-4 p-6">
        <DraftTray projectId={activeProjectId} />
        <Kanban projectId={activeProjectId} />
        <Dialog projectId={activeProjectId} />
      </main>

      {/* feat-041: right-side DAG panel. Closed by default; toggled
          from the Kanban header. */}
      {dagViewOpen ? (
        <DagView
          projectId={activeProjectId}
          onClose={() => setDagViewOpen(false)}
        />
      ) : null}

      {/* feat-055: sandbox-driven confirm dialog. Mounted at the root
          so it overlays the entire UI when a destructive tool call
          needs the user's OK. */}
      {pendingSandbox ? (
        <ConfirmationDialog
          open
          toolName={pendingSandbox.toolName}
          args={pendingSandbox.args}
          onApprove={pendingSandbox.approve}
          onDeny={() => {
            pendingSandbox.deny();
            setPendingSandbox(null);
          }}
          onCancel={() => setPendingSandbox(null)}
        />
      ) : null}
    </div>
  );
}
