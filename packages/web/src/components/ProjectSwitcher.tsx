// SPDX-License-Identifier: Apache-2.0
/**
 * Header `<select>` for switching the active project.
 *
 * feat-032 ships the unpolished, raw-`<select>` skeleton; feat-034
 * replaces it with a styled dropdown. Data wiring (TanStack Query →
 * Zustand) is real so downstream features inherit the integration.
 */

import { useUiStore } from "../lib/state/ui-store.js";
import { useProjects } from "../lib/api/projects.js";

export function ProjectSwitcher(): React.ReactElement {
  const projects = useProjects();
  const activeProjectId = useUiStore((s) => s.activeProjectId);
  const setActiveProjectId = useUiStore((s) => s.setActiveProjectId);

  if (projects.isPending) {
    return (
      <label className="flex items-center gap-2 text-sm">
        <span className="text-zinc-400">Project:</span>
        <span className="text-zinc-500">(loading…)</span>
      </label>
    );
  }

  if (projects.isError) {
    return (
      <label className="flex items-center gap-2 text-sm">
        <span className="text-zinc-400">Project:</span>
        <span className="text-red-400">(error: {projects.error.message})</span>
      </label>
    );
  }

  const list = projects.data ?? [];

  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-zinc-400">Project:</span>
      <select
        aria-label="Active project"
        className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
        value={activeProjectId ?? ""}
        onChange={(e) => setActiveProjectId(e.target.value || null)}
      >
        {list.length === 0 ? (
          <option value="">(no projects yet)</option>
        ) : (
          <>
            <option value="">— select a project —</option>
            {list.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </>
        )}
      </select>
    </label>
  );
}
