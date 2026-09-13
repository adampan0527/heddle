// SPDX-License-Identifier: Apache-2.0
/**
 * ConfirmationCard — feat-054 / D-054.
 *
 * Renders the destructive-op confirmation card the dialog mounts
 * when the user types a `@feat-XXX split / merge / remove-dep`
 * command. The card shows a short before/after diff and a pair of
 * Confirm / Cancel buttons. Confirm runs the parked `apply`
 * callback (which invokes the mutation hook + invalidates the
 * features query); Cancel runs `cancel` and clears the card.
 *
 * The component is purely presentational — all state lives in the
 * ui-store (`pendingConfirmation`). Mounting the card is the
 * Dialog's job (driven by `transcript.confirmationId`); this file
 * only knows how to lay out the diff and wire the buttons.
 */

import { useState } from "react";

import { useUiStore } from "../lib/state/ui-store.ts";

interface ConfirmationCardProps {
  confirmationId: string;
  /** The feature id this destructive op targets. */
  featureId: string;
  /** Verb in plain English for the header. */
  command: string;
  /** Free-form diff object; we render `changes` array if present. */
  diff: unknown;
}

export function ConfirmationCard({
  confirmationId,
  featureId,
  command,
  diff,
}: ConfirmationCardProps): JSX.Element {
  const setPending = useUiStore((s) => s.setPendingConfirmation);
  const pending = useUiStore((s) => s.pendingConfirmation);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Defensive: if the store has moved on (a newer confirmation
  // overwrote this one, or the user clicked Cancel), render
  // nothing rather than a stale card.
  if (!pending || pending.id !== confirmationId) return <></>;

  const changes = readChanges(diff);
  const diffView = prettyDiff(diff);

  async function onConfirm(): Promise<void> {
    if (!pending) return;
    setBusy(true);
    setError(null);
    try {
      await pending.apply();
      setPending(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function onCancel(): void {
    if (!pending) return;
    pending.cancel();
    setPending(null);
  }

  return (
    <div
      data-testid="confirmation-card"
      data-confirmation-id={confirmationId}
      className="rounded border border-amber-300 bg-amber-50 p-3 my-2"
    >
      <div className="font-semibold text-amber-900">
        Confirm destructive {command} on {featureId}?
      </div>
      {diffView && (
        <pre
          className="text-xs mt-2 bg-white/60 p-2 rounded overflow-x-auto"
          data-testid="confirmation-diff"
        >
          {diffView}
        </pre>
      )}
      {changes.length > 0 && (
        <ul className="text-sm mt-2 list-disc list-inside" data-testid="confirmation-changes">
          {changes.map((c, i) => (
            <li key={i} className="text-amber-900">
              {c}
            </li>
          ))}
        </ul>
      )}
      {error && (
        <div className="text-sm text-red-700 mt-2" data-testid="confirmation-error">
          {error}
        </div>
      )}
      <div className="flex gap-2 mt-3">
        <button
          type="button"
          disabled={busy}
          onClick={() => void onConfirm()}
          className="px-3 py-1 bg-amber-600 text-white rounded text-sm disabled:opacity-50"
          data-testid="confirmation-confirm"
        >
          {busy ? "Working…" : "Confirm"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onCancel}
          className="px-3 py-1 bg-white border border-amber-600 text-amber-700 rounded text-sm disabled:opacity-50"
          data-testid="confirmation-cancel"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function readChanges(diff: unknown): string[] {
  if (!diff || typeof diff !== "object") return [];
  const d = diff as { changes?: unknown };
  if (Array.isArray(d.changes)) {
    return d.changes.filter((c): c is string => typeof c === "string");
  }
  return [];
}

function prettyDiff(diff: unknown): string {
  if (!diff) return "";
  try {
    return JSON.stringify(diff, null, 2);
  } catch {
    return String(diff);
  }
}