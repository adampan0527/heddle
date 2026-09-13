// SPDX-License-Identifier: Apache-2.0
/**
 * `useDialogSubmit` — feat-038 + feat-042.
 *
 * Encapsulates the dialog-submit lifecycle: validate the message,
 * append a user entry, route through the v0.1 diagnose shortcut or
 * the real daemon, then append the assistant (or error) entry.
 *
 * Extracted from Dialog.tsx so the rules (regex patterns, draft
 * clearing, draft-tray refresh) live in one place and can be unit
 * tested by injecting mock hooks.
 */

import type { DialogResponse } from "@heddle/shared";

import { useSubmitDialog } from "../lib/api/dialog.ts";
import { useUiStore } from "../lib/state/ui-store.ts";
import {
  extractDiagnosis,
  extractMentionedFeature,
  isDiagnoseShortcut,
  mockDiagnoseResponse,
} from "./diagnosis-helpers.ts";
import type {
  NewTranscriptEntry,
  TranscriptEntry,
} from "./transcript-model.ts";

const MAX_MESSAGE_CHARS = 8000;

export interface UseDialogSubmitArgs {
  projectId: string | null;
  appendEntry: (entry: NewTranscriptEntry) => void;
  clearDraft: () => void;
}

export interface DialogSubmit {
  submit: (message: string) => Promise<void>;
  sending: boolean;
}

export function useDialogSubmit({
  projectId,
  appendEntry,
  clearDraft,
}: UseDialogSubmitArgs): DialogSubmit {
  const submitMutation = useSubmitDialog(projectId);
  const setDrafts = useUiStore((s) => s.setDrafts);

  async function submit(message: string): Promise<void> {
    const trimmed = message.trim();
    if (trimmed.length === 0) return;
    if (trimmed.length > MAX_MESSAGE_CHARS) return;
    if (!projectId) {
      appendEntry({
        role: "error",
        text: "Select a project before sending a dialog message.",
      });
      return;
    }
    appendEntry({ role: "user", text: trimmed });
    clearDraft();
    try {
      // v0.1 mock for "@feat-XXX diagnose" (feat-043 will replace
      // this with a real daemon-side handler). The daemon does not
      // yet implement classify_intent(), so we short-circuit here
      // and synthesise a typed diagnose response so the
      // DiagnosisReport card is testable end-to-end today.
      if (isDiagnoseShortcut(trimmed)) {
        const featureId = extractMentionedFeature(trimmed);
        const mock = mockDiagnoseResponse(featureId, trimmed);
        // mockDiagnoseResponse always produces a diagnosis, so the
        // branch below stays simple. (If we ever loosen mockDiagnose
        // to omit the diagnosis, conditionally set the field instead
        // of passing undefined — exactOptionalPropertyTypes forbids it.)
        if (mock.diagnosis) {
          const entry: NewTranscriptEntry = {
            role: "assistant",
            text: mock.text,
            diagnosis: mock.diagnosis,
          };
          if (featureId) entry.diagnosisFeatureId = featureId;
          appendEntry(entry);
        }
        return;
      }
      const resp: DialogResponse = await submitMutation.mutateAsync({
        message: trimmed,
      });
      const extracted = extractDiagnosis(resp);
      if (extracted) {
        const entry: NewTranscriptEntry = {
          role: "assistant",
          text: resp.text,
          diagnosis: extracted.diagnosis,
        };
        appendEntry(entry);
        return;
      }
      appendEntry({ role: "assistant", text: resp.text });
      // feat-040: when the daemon's classifier returns work-mode
      // draft cards, refresh the tray.
      if (resp.kind === "work" && Array.isArray(resp.drafts)) {
        setDrafts(resp.drafts);
      }
    } catch (err) {
      appendEntry({
        role: "error",
        text: err instanceof Error ? err.message : String(err),
      });
    }
  }

  return { submit, sending: submitMutation.isPending };
}

/** Re-export so the dialog component can keep its constant local. */
export const DIALOG_MAX_MESSAGE_CHARS = MAX_MESSAGE_CHARS;

/** Marker used by tests to confirm the submit hook ran. */
export function _transcriptEntriesAreCompatible(
  _e: TranscriptEntry,
): boolean {
  return true;
}
