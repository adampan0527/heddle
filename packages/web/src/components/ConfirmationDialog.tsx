// SPDX-License-Identifier: Apache-2.0
/**
 * ConfirmationDialog — feat-055 / D-053.
 *
 * Modal-style confirm dialog that intercepts destructive tool calls
 * when the active project's sandbox level is `edit-with-confirm`. The
 * dialog is mounted at the app root and is shown only when a pending
 * confirmation request is parked in the ui-store.
 *
 * This component is intentionally separate from `ConfirmationCard`
 * (feat-054), which renders inside the dialog transcript and is
 * scoped to per-feature mutations (split / merge / remove-dep). The
 * modal here is for whole-tool-call confirmations that the sandbox
 * middleware needs the user to approve before the call runs.
 *
 * Cancel vs. deny semantics:
 *   - Cancel drops the request without invoking the apply callback.
 *   - Deny does the same but also records the user's "no" so the
 *     agent can fall back gracefully (the apply callback's reject
 *     resolves to a synthetic "user denied" tool result).
 *
 * Approve invokes apply; on success the dialog auto-dismisses; on
 * failure the error is rendered inline and the user can retry.
 */

import { useState } from "react";

interface ConfirmationDialogProps {
  /** True when a request is parked in the ui-store. */
  open: boolean;
  /** Tool name the agent wants to invoke (e.g. "write"). */
  toolName: string;
  /** Free-form args the agent wants to pass (rendered in <pre>). */
  args: unknown;
  /** User clicked Approve — run the operation. */
  onApprove: () => Promise<void>;
  /** User clicked Deny — drop the request. */
  onDeny: () => void;
  /** User clicked Cancel (Esc / backdrop). Same as Deny today. */
  onCancel: () => void;
}

export function ConfirmationDialog({
  open,
  toolName,
  args,
  onApprove,
  onDeny,
  onCancel,
}: ConfirmationDialogProps): JSX.Element | null {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  async function handleApprove(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await onApprove();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="sandbox-confirm-title"
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/60 p-4"
      data-testid="sandbox-confirm-dialog"
    >
      <div className="w-full max-w-md rounded-md border border-hairline-strong bg-surface-card p-4 shadow-2xl">
        <h2
          id="sandbox-confirm-title"
          className="text-sm font-semibold text-amber-900"
        >
          Sandbox asks: allow {toolName}?
        </h2>
        <p className="mt-2 text-xs text-charcoal">
          The current project is set to{" "}
          <code className="rounded bg-surface-bone px-1 text-amber-900 font-code">
            edit-with-confirm
          </code>
          . Destructive tool calls require your explicit approval before
          they run.
        </p>
        <pre
          className="mt-3 max-h-40 overflow-auto rounded bg-canvas p-2 text-[11px] text-charcoal font-code"
          data-testid="sandbox-confirm-args"
        >
          {prettyArgs(args)}
        </pre>
        {error && (
          <div
            className="mt-2 rounded border border-red-700 bg-red-50 p-2 text-xs text-red-700"
            data-testid="sandbox-confirm-error"
          >
            {error}
          </div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="rounded-full border border-hairline-strong bg-surface-card px-3 py-1 text-xs text-charcoal hover:bg-canvas disabled:opacity-50"
            data-testid="sandbox-confirm-cancel"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onDeny}
            className="rounded-full border border-red-700 bg-red-50 px-3 py-1 text-xs text-red-700 hover:bg-red-100 disabled:opacity-50"
            data-testid="sandbox-confirm-deny"
          >
            Deny
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void handleApprove()}
            className="rounded-full bg-badge-success px-4 py-1 text-xs font-semibold text-on-primary hover:opacity-90 disabled:opacity-50"
            data-testid="sandbox-confirm-approve"
          >
            {busy ? "Working…" : "Approve"}
          </button>
        </div>
      </div>
    </div>
  );
}

function prettyArgs(args: unknown): string {
  try {
    return JSON.stringify(args ?? {}, null, 2);
  } catch {
    return String(args);
  }
}
