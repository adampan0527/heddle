// SPDX-License-Identifier: Apache-2.0
/**
 * Draft cards tray — feat-040 (D-013 / D-044 / D-045).
 *
 * Sits above the kanban board whenever the daemon returned
 * `kind: "work"` with one or more draft cards. Each draft is a
 * preview of a future `feat-XXX`; the user can keep (checked) or
 * discard (unchecked) each card. Clicking "Confirm all (N)" POSTs
 * the kept drafts to `/api/projects/:id/drafts/confirm` so the
 * daemon can persist them as new `feat-XXX` rows.
 *
 * Out of scope for this feature:
 *
 *   - Streaming re-decomposition while the user types more — feat-045
 *     will replace the dialog's stub with a real LLM classifier that
 *     re-emits `kind: "work"` whenever the user submits another
 *     message; until then the tray's setDrafts() call wipes and
 *     re-fills the tray on every successful submit.
 *   - Editing a draft card's title/steps before confirm — feat-054
 *     will allow inline editing in the tray; today the cards are
 *     read-only previews.
 *
 * Component layout: one row per draft (checkbox + temp id + title +
 * truncated description + a kind badge). Below the list: a single
 * "Confirm all (N)" button (disabled when N === 0) and a "Cancel"
 * button that drops the tray without persisting anything.
 *
 * Test handles (`data-testid`) are stable so the Vitest suite can
 * drive selection / confirm / cancel without depending on Tailwind
 * class names.
 */

import type { DraftCard } from "@heddle/shared";

import { useConfirmDrafts } from "../lib/api/features.ts";
import { useUiStore } from "../lib/state/ui-store.ts";

interface DraftTrayProps {
  projectId: string | null;
}

const KIND_LABEL: Record<string, string> = {
  feature: "feature",
  bugfix: "bugfix",
  enhancement: "enhancement",
};

export function DraftTray({ projectId }: DraftTrayProps): React.ReactElement | null {
  const drafts = useUiStore((s) => s.drafts);
  const selectedIds = useUiStore((s) => s.selectedIds);
  const toggleDraft = useUiStore((s) => s.toggleDraft);
  const clearDrafts = useUiStore((s) => s.clearDrafts);
  const confirmDrafts = useConfirmDrafts(projectId);

  const list = Object.values(drafts);
  if (list.length === 0) return null;

  const isConfirming = confirmDrafts.isPending;

  function handleConfirm(): void {
    const kept = list.filter((d) => selectedIds.includes(d.id));
    if (kept.length === 0) return;
    if (!projectId) return;
    confirmDrafts.mutate(
      { drafts: kept },
      {
        onSuccess: () => {
          clearDrafts();
        },
      },
    );
  }

  function handleCancel(): void {
    clearDrafts();
  }

  return (
    <section
      aria-label="Draft tray"
      data-testid="draft-tray"
      className="flex flex-col gap-2 rounded border border-amber-700 bg-amber-950/40 p-3"
    >
      <header className="flex items-center justify-between">
        <h2
          className="text-sm font-semibold text-amber-200"
          data-testid="draft-tray-title"
        >
          Draft cards ({list.length})
        </h2>
        <span className="text-xs text-amber-300/70" data-testid="draft-tray-count">
          {selectedIds.length} kept
        </span>
      </header>
      <ul className="flex flex-col gap-2" data-testid="draft-list">
        {list.map((draft) => (
          <DraftRow
            key={draft.id}
            draft={draft}
            kept={selectedIds.includes(draft.id)}
            onToggle={() => toggleDraft(draft.id)}
            disabled={isConfirming}
          />
        ))}
      </ul>
      <footer className="flex items-center justify-end gap-2 pt-1">
        <button
          type="button"
          data-testid="draft-tray-cancel"
          aria-label="Cancel draft tray"
          disabled={isConfirming}
          onClick={handleCancel}
          className="rounded border border-zinc-700 bg-zinc-900 px-3 py-1 text-sm text-zinc-200 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          type="button"
          data-testid="draft-tray-confirm"
          aria-label={`Confirm all (${selectedIds.length})`}
          disabled={selectedIds.length === 0 || isConfirming || !projectId}
          onClick={handleConfirm}
          className="rounded border border-blue-600 bg-blue-600 px-3 py-1 text-sm font-medium text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isConfirming ? "Confirming…" : `Confirm all (${selectedIds.length})`}
        </button>
      </footer>
    </section>
  );
}

interface DraftRowProps {
  draft: DraftCard;
  kept: boolean;
  onToggle: () => void;
  disabled: boolean;
}

function DraftRow({ draft, kept, onToggle, disabled }: DraftRowProps): React.ReactElement {
  const descriptionPreview = draft.description.slice(0, 140);
  return (
    <li
      data-testid="draft-row"
      data-draft-id={draft.id}
      className="flex items-start gap-2 rounded border border-zinc-700 bg-zinc-900 p-2"
    >
      <input
        type="checkbox"
        data-testid="draft-checkbox"
        aria-label={`Keep draft ${draft.id}`}
        checked={kept}
        disabled={disabled}
        onChange={onToggle}
        className="mt-1 h-4 w-4 shrink-0 accent-blue-500"
      />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-center gap-2">
          <span
            data-testid="draft-temp-id"
            className="rounded bg-zinc-800 px-1.5 py-0.5 text-xs text-zinc-300"
          >
            {draft.id}
          </span>
          <span
            className="rounded bg-zinc-800 px-1.5 py-0.5 text-xs uppercase text-zinc-400"
            data-testid="draft-kind"
          >
            {KIND_LABEL[draft.kind] ?? draft.kind}
          </span>
          <h3
            data-testid="draft-title"
            className="truncate text-sm font-medium text-zinc-100"
          >
            {draft.title}
          </h3>
        </div>
        <p
          data-testid="draft-description"
          className="line-clamp-2 text-xs text-zinc-400"
        >
          {descriptionPreview}
          {draft.description.length > 140 ? "…" : ""}
        </p>
      </div>
    </li>
  );
}