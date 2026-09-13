// SPDX-License-Identifier: Apache-2.0
/**
 * Transcript entry model + append helper for the dialog component —
 * feat-038 + feat-042.
 *
 * Extracted from Dialog.tsx so the entry shape and the monotonic-id
 * bookkeeping can be unit-tested independently of the React tree.
 * The render loop stays in Dialog.tsx because it depends on the
 * mention-dropdown state.
 */

import type { DiagnoseResponse } from "@heddle/shared";

export interface TranscriptEntry {
  /** Monotonic id used as the React key. */
  id: number;
  /** Who produced the entry — "user" types into the textarea, "assistant"
   *  is whatever the daemon returned in the most recent response. */
  role: "user" | "assistant" | "error";
  text: string;
  /** When the entry was appended; used for stable ordering. */
  at: number;
  /** Optional structured diagnosis payload — feat-042. When set, the
   *  assistant entry renders via <DiagnosisReport> instead of plain
   *  text. */
  diagnosis?: DiagnoseResponse;
  /** Optional feature id the diagnosis is about (e.g. "feat-042"). */
  diagnosisFeatureId?: string;
  /** feat-054 / D-054: when this assistant entry carries a
   *  destructive-op confirmation request, this id matches a row in
   *  the UI store's `pendingConfirmation`. The dialog renders the
   *  confirmation card with that row's diff + Confirm/Cancel. */
  confirmationId?: string;
}

/** A new entry sans `id` and `at`; the helpers fill those in. */
export type NewTranscriptEntry = Omit<TranscriptEntry, "id" | "at">;

/** Module-level counter that survives StrictMode double-invocation;
 *  the transcript is ephemeral component state, not persisted across
 *  remounts, so a module-level singleton is fine. */
let idCounter = 0;

export function nextTranscriptId(): number {
  return ++idCounter;
}

export function makeTranscriptEntry(
  entry: NewTranscriptEntry,
): TranscriptEntry {
  return { id: nextTranscriptId(), at: Date.now(), ...entry };
}
