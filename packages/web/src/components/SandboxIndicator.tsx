// SPDX-License-Identifier: Apache-2.0
/**
 * SandboxIndicator — feat-055 / D-053.
 *
 * Header badge that surfaces the active project's current sandbox
 * level (read-only / edit-with-confirm / full). The badge doubles as
 * a dropdown trigger so the user can flip the level in place; the
 * mutation goes through `useUpdateProjectSandbox` which PATCHes
 * `/api/projects/:id` and writes to `.heddle/config.yaml`.
 *
 * In v0.1 we also expose a "Test destructive action" button that
 * synthesises a tool-call confirmation so the user can verify the
 * `edit-with-confirm` flow without driving the full agent loop. The
 * button is gated on the active level being `edit-with-confirm` so
 * the demo only fires in the most interesting state.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import type { SandboxLevel } from "@heddle/shared";

import { useProjects, useUpdateProjectSandbox } from "../lib/api/projects.ts";

const SANDBOX_LEVELS: SandboxLevel[] = ["read-only", "edit-with-confirm", "full"];

interface SandboxIndicatorProps {
  /** Active project id (drives which row we render). */
  projectId: string | null;
  /**
   * Fired when the user clicks the "test destructive action" button
   * while the level is `edit-with-confirm`. The host (App.tsx) wires
   * this to the ConfirmationDialog so the demo reflects the real
   * confirm/cancel flow that real tool calls will use.
   */
  onTestDestructive?: () => void;
}

export function SandboxIndicator({
  projectId,
  onTestDestructive,
}: SandboxIndicatorProps): JSX.Element | null {
  const projects = useProjects();
  const mutation = useUpdateProjectSandbox();
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  const project = useMemo(
    () => projects.data?.find((p) => p.id === projectId) ?? null,
    [projects.data, projectId],
  );

  const level: SandboxLevel = project?.sandbox_level ?? "full";

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

  if (!projectId) return null;

  const badgeClass = badgeColorClass(level);
  const label = labelFor(level);

  async function pickLevel(next: SandboxLevel): Promise<void> {
    setOpen(false);
    if (next === level) return;
    try {
      await mutation.mutateAsync({ projectId: projectId!, sandbox_level: next });
    } catch (err) {
      // Surface the error inline; do not crash the header.
      // eslint-disable-next-line no-console
      console.error("Failed to update sandbox level:", err);
    }
  }

  return (
    <div className="flex items-center gap-2" data-testid="sandbox-indicator">
      <div className="relative">
        <button
          ref={triggerRef}
          type="button"
          aria-haspopup="menu"
          aria-expanded={open}
          aria-label={`Sandbox level: ${label}`}
          onClick={() => setOpen((o) => !o)}
          disabled={mutation.isPending}
          className={`flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium ${badgeClass} disabled:opacity-60`}
          data-testid="sandbox-badge"
          data-level={level}
        >
          <span aria-hidden="true">{iconFor(level)}</span>
          <span>{label}</span>
          <span aria-hidden="true" className="opacity-70">
            ▾
          </span>
        </button>
        {open && (
          <div
            ref={panelRef}
            role="menu"
            aria-label="Sandbox level"
            className="absolute right-0 z-20 mt-1 min-w-[14rem] rounded-md border border-hairline-strong bg-surface-card py-1 shadow-lg"
          >
            {SANDBOX_LEVELS.map((opt) => (
              <button
                key={opt}
                type="button"
                role="menuitemradio"
                aria-checked={opt === level}
                onClick={() => void pickLevel(opt)}
                className={`flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-xs ${
                  opt === level
                    ? "border-l-2 border-primary bg-surface-bone text-ink"
                    : "border-l-2 border-transparent text-body hover:bg-canvas"
                }`}
                data-testid={`sandbox-option-${opt}`}
              >
                <span className="font-medium">{labelFor(opt)}</span>
                <span className="text-[10px] text-ash">{hintFor(opt)}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      {level === "edit-with-confirm" && onTestDestructive ? (
        <button
          type="button"
          onClick={onTestDestructive}
          className="rounded-full border border-amber-700 bg-amber-50 px-3 py-1 text-xs text-amber-900 hover:bg-amber-100"
          data-testid="sandbox-test-destructive"
        >
          Test destructive action
        </button>
      ) : null}
    </div>
  );
}

function labelFor(level: SandboxLevel): string {
  switch (level) {
    case "read-only":
      return "Read-only";
    case "edit-with-confirm":
      return "Edit (confirm)";
    case "full":
      return "Full";
  }
}

function hintFor(level: SandboxLevel): string {
  switch (level) {
    case "read-only":
      return "no writes";
    case "edit-with-confirm":
      return "ask first";
    case "full":
      return "no prompt";
  }
}

function iconFor(level: SandboxLevel): string {
  switch (level) {
    case "read-only":
      return "RO";
    case "edit-with-confirm":
      return "EC";
    case "full":
      return "FF";
  }
}

function badgeColorClass(level: SandboxLevel): string {
  switch (level) {
    case "read-only":
      return "border-stone bg-surface-bone text-charcoal hover:border-ash";
    case "edit-with-confirm":
      return "border-amber-700 bg-amber-50 text-amber-900 hover:border-amber-600";
    case "full":
      return "border-badge-success bg-emerald-50 text-emerald-900 hover:border-badge-success";
  }
}
