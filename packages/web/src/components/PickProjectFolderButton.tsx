// SPDX-License-Identifier: Apache-2.0
/**
 * `<PickProjectFolderButton />` — feat-034.
 *
 * Empty-state CTA in the project switcher. Clicking it adds a new
 * project. v0.1 strategy for capturing the absolute path:
 *
 *   1. Feature-detect `window.showDirectoryPicker()`. If available
 *      (Chrome/Edge/Opera), call it so the user gets a real native
 *      folder picker. The browser does NOT return an absolute path
 *      (security decision), so we fall through to step 2 regardless.
 *   2. Show an inline prompt asking the user to type/paste the
 *      absolute path. Validation is minimal — the daemon's
 *      `project_add` will reject invalid paths with a clear
 *      error code that the component surfaces inline.
 *
 * On success: refresh the project list (mutation invalidates the
 * TanStack Query key) and call `setActiveProjectId(newProject.id)`
 * so the user immediately sees the new project.
 */

import { useState } from "react";

import { useCreateProject } from "../lib/api/projects.ts";
import { useUiStore } from "../lib/state/ui-store.ts";
import { ApiCallError } from "../lib/api/errors.ts";

type Status =
  | { kind: "idle" }
  | { kind: "picking" }
  | { kind: "path_prompt" }
  | { kind: "submitting" }
  | { kind: "error"; message: string };

interface DirectoryPickerHandle {
  /** Read-only access is enough; we never write through this handle. */
  queryPermission?: (options: { mode: "read" }) => Promise<"granted" | "prompt" | "denied">;
  requestPermission?: (options: { mode: "read" }) => Promise<"granted" | "prompt" | "denied">;
}

declare global {
  interface Window {
    showDirectoryPicker?: (options?: {
      mode?: "read" | "readwrite";
    }) => Promise<DirectoryPickerHandle>;
  }
}

/**
 * Probe for the browser-native directory picker. Kept as a separate
 * export so tests can stub it without monkey-patching `window`.
 */
export function hasNativeDirectoryPicker(): boolean {
  return typeof window !== "undefined" && typeof window.showDirectoryPicker === "function";
}

export function PickProjectFolderButton(): React.ReactElement {
  const createProject = useCreateProject();
  const setActiveProjectId = useUiStore((s) => s.setActiveProjectId);
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [pathDraft, setPathDraft] = useState<string>("");

  const onClick = async (): Promise<void> => {
    setStatus({ kind: "picking" });
    // Best-effort: try the native picker first so the user gets the
    // visual affordance. The browser doesn't expose the absolute path
    // either way, so we always fall through to the path prompt.
    if (hasNativeDirectoryPicker() && window.showDirectoryPicker) {
      try {
        await window.showDirectoryPicker({ mode: "read" });
      } catch {
        // User cancelled or browser refused — fall through to prompt.
      }
    }
    setStatus({ kind: "path_prompt" });
  };

  const onSubmitPath = async (e: React.FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault();
    const trimmed = pathDraft.trim();
    if (!trimmed) {
      setStatus({ kind: "error", message: "Path cannot be empty" });
      return;
    }
    setStatus({ kind: "submitting" });
    try {
      const project = await createProject.mutateAsync({ path: trimmed });
      setActiveProjectId(project.id);
      setStatus({ kind: "idle" });
      setPathDraft("");
    } catch (err) {
      const message =
        err instanceof ApiCallError
          ? `${err.message} (${err.code})`
          : err instanceof Error
            ? err.message
            : String(err);
      setStatus({ kind: "error", message });
    }
  };

  const onCancel = (): void => {
    setStatus({ kind: "idle" });
    setPathDraft("");
  };

  if (status.kind === "path_prompt" || status.kind === "submitting") {
    const submitting = status.kind === "submitting";
    return (
      <form
        onSubmit={onSubmitPath}
        className="flex items-center gap-2"
        aria-label="Add project folder"
      >
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-zinc-400">Absolute path to project folder:</span>
          <input
            type="text"
            value={pathDraft}
            onChange={(e) => setPathDraft(e.target.value)}
            disabled={submitting}
            placeholder="/Users/me/code/my-project"
            autoFocus
            className="w-80 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
        </label>
        <button
          type="submit"
          disabled={submitting}
          className="rounded bg-blue-600 px-3 py-1 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {submitting ? "Adding…" : "Add"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className="rounded border border-zinc-700 px-3 py-1 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
        >
          Cancel
        </button>
      </form>
    );
  }

  if (status.kind === "error") {
    return (
      <div className="flex items-center gap-2 text-sm">
        <span className="text-red-400" role="alert">
          {status.message}
        </span>
        <button
          type="button"
          onClick={onClick}
          className="rounded border border-zinc-700 px-3 py-1 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          Retry
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded border border-zinc-700 px-3 py-1 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          Cancel
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded bg-blue-600 px-3 py-1 text-sm font-medium text-white hover:bg-blue-500"
    >
      Pick a project folder
    </button>
  );
}
