// SPDX-License-Identifier: Apache-2.0
/**
 * Polished project switcher dropdown — feat-034.
 *
 * Header control that lets the user pick which registered project the
 * kanban / dialog are currently showing. Replaces feat-032's raw
 * `<select>` skeleton with a custom dropdown so we can:
 *
 *   - Style it consistently with the rest of the header
 *   - Show `last_accessed_at` desc ordering (most recent first)
 *   - Render a real empty state (the `<PickProjectFolderButton />`)
 *     instead of a useless empty `<select>`
 *   - Surface a Retry button on project-list load failures
 *   - Add menu ARIA semantics and ESC-to-close behaviour
 *
 * Dropdown close semantics: clicking a row closes the panel;
 * clicking outside closes the panel; pressing Escape closes the panel.
 * The trigger button shows the active project's name (or "Select a
 * project" when none is active).
 */

import { useEffect, useMemo, useRef, useState } from "react";

import {
  useProjects,
} from "../lib/api/projects.ts";
import { useUiStore } from "../lib/state/ui-store.ts";
import { PickProjectFolderButton } from "./PickProjectFolderButton.tsx";
import { useQueryClient } from "@tanstack/react-query";

export function ProjectSwitcher(): React.ReactElement {
  const projects = useProjects();
  const activeProjectId = useUiStore((s) => s.activeProjectId);
  const setActiveProjectId = useUiStore((s) => s.setActiveProjectId);
  const queryClient = useQueryClient();

  const [open, setOpen] = useState<boolean>(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  // Close on outside-click + Escape.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent): void => {
      const target = e.target as Node | null;
      if (
        target &&
        !panelRef.current?.contains(target) &&
        !triggerRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Sort: most-recently-accessed first, name as deterministic tiebreaker.
  const sorted = useMemo(() => {
    const list = projects.data ?? [];
    return [...list].sort((a, b) => {
      if (a.last_accessed_at !== b.last_accessed_at) {
        return a.last_accessed_at < b.last_accessed_at ? 1 : -1;
      }
      return a.name.localeCompare(b.name);
    });
  }, [projects.data]);

  if (projects.isPending) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <span className="text-zinc-400">Project:</span>
        <span className="text-zinc-500">(loading…)</span>
      </div>
    );
  }

  if (projects.isError) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <span className="text-zinc-400">Project:</span>
        <span className="text-red-400" role="alert">
          (error: {projects.error.message})
        </span>
        <button
          type="button"
          onClick={() => {
            void queryClient.invalidateQueries({ queryKey: ["projects"] });
          }}
          className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
        >
          Retry
        </button>
      </div>
    );
  }

  // Empty state — no projects registered yet.
  if (sorted.length === 0) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <span className="text-zinc-400">No projects yet.</span>
        <PickProjectFolderButton />
      </div>
    );
  }

  const active = activeProjectId
    ? sorted.find((p) => p.id === activeProjectId) ?? null
    : null;
  const label = active ? active.name : "Select a project";

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Active project: ${label}`}
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 rounded border border-zinc-700 bg-zinc-900 px-3 py-1 text-sm text-zinc-100 hover:border-zinc-600 focus:outline-none focus:ring-2 focus:ring-blue-500"
      >
        <span>{label}</span>
        <span aria-hidden="true" className="text-zinc-500">
          ▾
        </span>
      </button>

      {open && (
        <div
          ref={panelRef}
          role="menu"
          aria-label="Registered projects"
          className="absolute right-0 z-10 mt-1 min-w-[16rem] rounded border border-zinc-700 bg-zinc-900 py-1 shadow-lg"
        >
          {sorted.map((p) => {
            const isActive = p.id === activeProjectId;
            return (
              <button
                key={p.id}
                type="button"
                role="menuitem"
                aria-current={isActive ? "true" : undefined}
                onClick={() => {
                  setActiveProjectId(p.id);
                  setOpen(false);
                }}
                className={`flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-sm ${
                  isActive
                    ? "border-l-2 border-blue-500 bg-zinc-800 text-zinc-100"
                    : "border-l-2 border-transparent text-zinc-200 hover:bg-zinc-800"
                }`}
              >
                <span className="truncate">{p.name}</span>
                <span className="text-xs text-zinc-500" aria-hidden="true">
                  {p.path}
                </span>
              </button>
            );
          })}
          <div className="border-t border-zinc-800 px-3 py-2">
            <PickProjectFolderButton />
          </div>
        </div>
      )}
    </div>
  );
}
