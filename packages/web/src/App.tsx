// SPDX-License-Identifier: Apache-2.0
/**
 * Top-level app shell — feat-032.
 *
 * Renders the header with `<ProjectSwitcher />` and `<ConnectionStatus />`,
 * plus the kanban (feat-035) and the persistent bottom dialog
 * (feat-038). Tailwind v4 utility classes only.
 *
 * feat-033 wires the WebSocket singleton: opened on mount via
 * `getDefaultWsClient().connect()`, and torn down via `close()` on
 * unmount. Strict-mode-safe (the connect/close pair is idempotent).
 */

import { useEffect } from "react";

import { ConnectionStatus } from "./components/ConnectionStatus.tsx";
import { Dialog } from "./components/Dialog.tsx";
import { DraftTray } from "./components/DraftTray.tsx";
import { Kanban } from "./components/Kanban.tsx";
import { ProjectSwitcher } from "./components/ProjectSwitcher.tsx";
import { getDefaultWsClient } from "./lib/ws-client.ts";
import { useUiStore } from "./lib/state/ui-store.ts";

export default function App(): React.ReactElement {
  const activeProjectId = useUiStore((s) => s.activeProjectId);
  useEffect(() => {
    const ws = getDefaultWsClient();
    ws.connect();
    return () => {
      ws.close();
    };
  }, []);

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center justify-between border-b border-zinc-800 px-6 py-3">
        <h1 className="text-lg font-semibold text-zinc-100">heddle</h1>
        <div className="flex items-center gap-4">
          <ProjectSwitcher />
          <ConnectionStatus />
        </div>
      </header>

      <main className="flex flex-1 flex-col gap-4 p-6">
        <DraftTray projectId={activeProjectId} />
        <Kanban projectId={activeProjectId} />
        <Dialog projectId={activeProjectId} />
      </main>
    </div>
  );
}
