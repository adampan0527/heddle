// SPDX-License-Identifier: Apache-2.0
/**
 * `useDialogSubmit` — feat-038 + feat-042 + feat-043.
 *
 * Encapsulates the dialog-submit lifecycle: validate the message,
 * append a user entry, parse any `@feat-XXX <command>` shortcut
 * (D-033) and route to the right handler, otherwise fall through
 * to the v0.1 diagnose shortcut or the real daemon, then append
 * the assistant (or error) entry.
 *
 * Extracted from Dialog.tsx so the rules (regex patterns, draft
 * clearing, draft-tray refresh) live in one place and can be unit
 * tested by injecting mock hooks.
 */

import type { DialogResponse } from "@heddle/shared";

import { useSubmitDialog } from "../lib/api/dialog.ts";
import { useRetryFeature, useTransitionFeature } from "../lib/api/features.ts";
import { useUiStore } from "../lib/state/ui-store.ts";
import {
  extractDiagnosis,
  extractMentionedFeature,
  isDiagnoseShortcut,
  mockDiagnoseResponse,
  parseDialogCommand,
  type DialogCommand,
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
  const retryMutation = useRetryFeature(projectId);
  const transitionMutation = useTransitionFeature(projectId);

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
    // feat-043 (D-033): short-circuit on `@feat-XXX <command>` shapes
    // before falling through to the diagnose mock / daemon. The
    // parser is strict (whole message must match one of the five
    // verbs), so anything else falls through to chat.
    const command = parseDialogCommand(trimmed);
    if (command) {
      await routeCommand(command, {
        appendEntry,
        retryMutation,
        transitionMutation,
      });
      return;
    }
    try {
      // v0.1 mock for "@feat-XXX diagnose" (the daemon does not yet
      // implement a diagnose handler; once it does this short-circuit
      // can move into the daemon). We keep synthesising a typed
      // diagnose response so the DiagnosisReport card is testable
      // end-to-end today.
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

  const sending =
    submitMutation.isPending ||
    retryMutation.isPending ||
    transitionMutation.isPending;
  return { submit, sending };
}

/**
 * Internal: dispatch one of the five feat-043 / D-033 commands and
 * append a transcript entry describing the result. Kept as a free
 * function so the success/error branches stay readable and each path
 * can be reasoned about in isolation.
 */
interface RouteCommandDeps {
  appendEntry: (entry: NewTranscriptEntry) => void;
  retryMutation: ReturnType<typeof useRetryFeature>;
  transitionMutation: ReturnType<typeof useTransitionFeature>;
}

async function routeCommand(
  cmd: DialogCommand,
  deps: RouteCommandDeps,
): Promise<void> {
  const { appendEntry, retryMutation, transitionMutation } = deps;
  try {
    switch (cmd.command) {
      case "diagnose":
        // Reuse the same mock the diagnose shortcut path emits.
        // The daemon-side diagnose handler will replace this once
        // it ships.
        appendDiagnoseMock(cmd.featureId, appendEntry);
        return;
      case "retry":
        await retryMutation.mutateAsync({ featureId: cmd.featureId });
        appendEntry(commandOk(cmd, `Retry queued for ${cmd.featureId}.`));
        return;
      case "retry-with-hint":
        await retryMutation.mutateAsync({
          featureId: cmd.featureId,
          hint: cmd.hint ?? "",
        });
        appendEntry(
          commandOk(
            cmd,
            `Retry-with-hint queued for ${cmd.featureId}.`,
          ),
        );
        return;
      case "mark-done":
        await transitionMutation.mutateAsync({
          featureId: cmd.featureId,
          action: "mark-done",
        });
        appendEntry(
          commandOk(
            cmd,
            `${cmd.featureId} marked done (no work performed).`,
          ),
        );
        return;
      case "abandon":
        await transitionMutation.mutateAsync({
          featureId: cmd.featureId,
          action: "abandon",
        });
        appendEntry(
          commandOk(cmd, `${cmd.featureId} abandoned.`),
        );
        return;
      default: {
        // Exhaustiveness check — adding a new DialogCommandKind forces
        // a compile error here until its branch is implemented.
        const _exhaustive: never = cmd.command;
        void _exhaustive;
        appendEntry({
          role: "error",
          text: `Unknown command for ${cmd.featureId}.`,
        });
        return;
      }
    }
  } catch (err) {
    appendEntry({
      role: "error",
      text: `Command "${cmd.command}" failed: ${
        err instanceof Error ? err.message : String(err)
      }`,
    });
  }
}

function commandOk(
  cmd: DialogCommand,
  text: string,
): NewTranscriptEntry {
  return { role: "assistant", text, diagnosisFeatureId: cmd.featureId };
}

function appendDiagnoseMock(
  featureId: string,
  appendEntry: (entry: NewTranscriptEntry) => void,
): void {
  // Build the mock against an empty message so the cause line reads
  // predictably; the user typed the command verbatim.
  const mock = mockDiagnoseResponse(featureId, "");
  if (!mock.diagnosis) return;
  const entry: NewTranscriptEntry = {
    role: "assistant",
    text: mock.text,
    diagnosis: mock.diagnosis,
    diagnosisFeatureId: featureId,
  };
  appendEntry(entry);
}

/** Re-export so the dialog component can keep its constant local. */
export const DIALOG_MAX_MESSAGE_CHARS = MAX_MESSAGE_CHARS;

/** Marker used by tests to confirm the submit hook ran. */
export function _transcriptEntriesAreCompatible(
  _e: TranscriptEntry,
): boolean {
  return true;
}
