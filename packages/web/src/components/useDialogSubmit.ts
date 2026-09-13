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
import {
  useEditFeature,
  useMergeFeatures,
  useReprioritizeFeature,
  useRetryFeature,
  useSplitFeature,
  useTransitionFeature,
  useUpdateDeps,
} from "../lib/api/features.ts";
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
  const splitMutation = useSplitFeature(projectId);
  const mergeMutation = useMergeFeatures(projectId);
  const editMutation = useEditFeature(projectId);
  const prioritizeMutation = useReprioritizeFeature(projectId);
  const depsMutation = useUpdateDeps(projectId);

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
        splitMutation,
        mergeMutation,
        editMutation,
        prioritizeMutation,
        depsMutation,
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
    transitionMutation.isPending ||
    splitMutation.isPending ||
    mergeMutation.isPending ||
    editMutation.isPending ||
    prioritizeMutation.isPending ||
    depsMutation.isPending;
  return { submit, sending };
}

/**
 * Internal: dispatch one of the eleven dialog commands (5 from
 * feat-043 / D-033 plus 6 from feat-054 / D-054) and append a
 * transcript entry describing the result. Destructive ops
 * (split / merge / remove-dep) per D-054 surface a confirmation
 * card with the diff before the mutation runs — the parser marks
 * them via `isDestructiveCommand`, and we render the diff via
 * `PendingConfirmation` (a UI-store entry the dialog reads).
 */
interface RouteCommandDeps {
  appendEntry: (entry: NewTranscriptEntry) => void;
  retryMutation: ReturnType<typeof useRetryFeature>;
  transitionMutation: ReturnType<typeof useTransitionFeature>;
  splitMutation: ReturnType<typeof useSplitFeature>;
  mergeMutation: ReturnType<typeof useMergeFeatures>;
  editMutation: ReturnType<typeof useEditFeature>;
  prioritizeMutation: ReturnType<typeof useReprioritizeFeature>;
  depsMutation: ReturnType<typeof useUpdateDeps>;
}

async function routeCommand(
  cmd: DialogCommand,
  deps: RouteCommandDeps,
): Promise<void> {
  const {
    appendEntry,
    retryMutation,
    transitionMutation,
    splitMutation,
    mergeMutation,
    editMutation,
    prioritizeMutation,
    depsMutation,
  } = deps;
  try {
    switch (cmd.command) {
      case "diagnose":
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
      // ---- feat-054 / D-054: post-confirm modification ----
      case "split": {
        // Destructive — surface a confirmation card with the diff
        // before invoking the mutation.
        const nParts = Number.parseInt(cmd.payload ?? "0", 10);
        if (!Number.isFinite(nParts) || nParts < 1 || nParts > 20) {
          throw new Error(
            `split requires between 1 and 20 parts (got ${cmd.payload ?? "?"})`,
          );
        }
        const diff = buildSplitDiff(cmd.featureId, nParts);
        const confirmId = queueConfirmation({
          featureId: cmd.featureId,
          command: cmd.command,
          diff,
          apply: () => executeSplit(splitMutation, cmd, nParts, appendEntry),
          cancel: () =>
            appendEntry({
              role: "assistant",
              text: `Split of ${cmd.featureId} cancelled.`,
              diagnosisFeatureId: cmd.featureId,
            }),
        });
        appendEntry({
          role: "assistant",
          text: `Split ${cmd.featureId} into ${nParts} parts? See confirmation card.`,
          confirmationId: confirmId,
          diagnosisFeatureId: cmd.featureId,
        });
        return;
      }
      case "merge": {
        const mentions = (cmd.mentions ?? []).filter(
          (m) => m.toLowerCase() !== cmd.featureId.toLowerCase(),
        );
        if (mentions.length === 0) {
          throw new Error(
            `merge requires at least one sibling @feat-XXX mention`,
          );
        }
        const diff = buildMergeDiff(cmd.featureId, mentions);
        const confirmId = queueConfirmation({
          featureId: cmd.featureId,
          command: cmd.command,
          diff,
          apply: () => executeMerge(mergeMutation, cmd, mentions, appendEntry),
          cancel: () =>
            appendEntry({
              role: "assistant",
              text: `Merge of ${cmd.featureId} cancelled.`,
              diagnosisFeatureId: cmd.featureId,
            }),
        });
        appendEntry({
          role: "assistant",
          text: `Merge ${cmd.featureId} with ${mentions.join(", ")}? See confirmation card.`,
          confirmationId: confirmId,
          diagnosisFeatureId: cmd.featureId,
        });
        return;
      }
      case "rename": {
        const newTitle = cmd.payload ?? "";
        if (newTitle.length === 0) {
          throw new Error("rename requires a non-empty title");
        }
        await editMutation.mutateAsync({
          featureId: cmd.featureId,
          title: newTitle,
        });
        appendEntry(
          commandOk(
            cmd,
            `${cmd.featureId} renamed to "${newTitle}".`,
          ),
        );
        return;
      }
      case "set-priority": {
        const prio = cmd.payload ?? "medium";
        await prioritizeMutation.mutateAsync({
          featureId: cmd.featureId,
          priority: prio as "high" | "medium" | "low",
        });
        appendEntry(
          commandOk(cmd, `${cmd.featureId} priority set to ${prio}.`),
        );
        return;
      }
      case "add-dep": {
        const mentions = cmd.mentions ?? [];
        if (mentions.length === 0) {
          throw new Error("depend on requires an @feat-XXX mention");
        }
        await depsMutation.mutateAsync({
          featureId: cmd.featureId,
          add: mentions,
        });
        appendEntry(
          commandOk(
            cmd,
            `${cmd.featureId} now depends on ${mentions.join(", ")}.`,
          ),
        );
        return;
      }
      case "remove-dep": {
        // Destructive — drop relationship requires confirm.
        const mentions = cmd.mentions ?? [];
        if (mentions.length === 0) {
          throw new Error("remove dep requires an @feat-XXX mention");
        }
        const diff = buildRemoveDepDiff(cmd.featureId, mentions);
        const confirmId = queueConfirmation({
          featureId: cmd.featureId,
          command: cmd.command,
          diff,
          apply: () => executeRemoveDep(depsMutation, cmd, mentions, appendEntry),
          cancel: () =>
            appendEntry({
              role: "assistant",
              text: `Remove dep from ${cmd.featureId} cancelled.`,
              diagnosisFeatureId: cmd.featureId,
            }),
        });
        appendEntry({
          role: "assistant",
          text: `Remove ${mentions.join(", ")} from ${cmd.featureId}'s deps? See confirmation card.`,
          confirmationId: confirmId,
          diagnosisFeatureId: cmd.featureId,
        });
        return;
      }
      default: {
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

/**
 * Push a confirmation request into the UI store. The dialog UI
 * subscribes to the store and renders a card with the diff +
 * Confirm / Cancel buttons; clicking Confirm runs `apply`,
 * clicking Cancel runs `cancel`. The `confirmId` is stamped on
 * the assistant transcript entry so the card knows which request
 * to render.
 */
function queueConfirmation(req: {
  featureId: string;
  command: string;
  diff: unknown;
  apply: () => Promise<void>;
  cancel: () => void;
}): string {
  const setConfirmation = useUiStore.getState().setPendingConfirmation;
  const id = `conf-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  setConfirmation({
    id,
    featureId: req.featureId,
    command: req.command,
    diff: req.diff,
    apply: req.apply,
    cancel: req.cancel,
  });
  return id;
}

function buildSplitDiff(sourceId: string, n: number): unknown {
  return {
    operation: "split",
    source: { id: sourceId },
    created: Array.from({ length: n }, (_, i) => ({
      id: `${sourceId}-${i + 1}`,
      title: `<child ${i + 1}>`,
    })),
    changes: [
      `archive ${sourceId} (superseded_by=${sourceId}-1)`,
      ...Array.from({ length: n }, (_, i) => `add ${sourceId}-${i + 1}`),
    ],
  };
}

function buildMergeDiff(sourceId: string, mentions: string[]): unknown {
  const target = mentions[mentions.length - 1] ?? sourceId;
  return {
    operation: "merge",
    sources: [{ id: sourceId }, ...mentions.slice(0, -1).map((m) => ({ id: m }))],
    successor: { id: target },
    changes: [
      `archive ${sourceId} (superseded_by=${target})`,
      ...mentions
        .slice(0, -1)
        .map((m) => `archive ${m} (superseded_by=${target})`),
      `add ${target}`,
    ],
  };
}

function buildRemoveDepDiff(sourceId: string, mentions: string[]): unknown {
  return {
    operation: "deps",
    before: mentions,
    after: [],
    changes: mentions.map((m) => `remove ${m} from ${sourceId}.depends_on`),
  };
}

async function executeSplit(
  mutation: ReturnType<typeof useSplitFeature>,
  cmd: DialogCommand,
  nParts: number,
  appendEntry: (entry: NewTranscriptEntry) => void,
): Promise<void> {
  const newFeatures = Array.from({ length: nParts }, (_, i) => ({
    id: `${cmd.featureId}-${i + 1}`,
    title: `Part ${i + 1} of ${cmd.featureId}`,
    description: `Part ${i + 1} of ${cmd.featureId}`,
    steps: [],
  }));
  const result = await mutation.mutateAsync({
    featureId: cmd.featureId,
    new_features: newFeatures,
  });
  appendEntry({
    role: "assistant",
    text: `${cmd.featureId} split into ${result.created.length} parts.`,
    diagnosisFeatureId: cmd.featureId,
  });
}

async function executeMerge(
  mutation: ReturnType<typeof useMergeFeatures>,
  cmd: DialogCommand,
  mentions: string[],
  appendEntry: (entry: NewTranscriptEntry) => void,
): Promise<void> {
  const targetId = mentions[mentions.length - 1] ?? cmd.featureId;
  const sourceIds = [cmd.featureId, ...mentions.slice(0, -1)];
  const result = await mutation.mutateAsync({
    source_ids: sourceIds,
    target: {
      id: targetId,
      title: `Merged: ${sourceIds.join(" + ")}`,
      description: `Merged: ${sourceIds.join(" + ")}`,
    },
  });
  appendEntry({
    role: "assistant",
    text: `Merged ${sourceIds.join(", ")} into ${result.created.id}.`,
    diagnosisFeatureId: cmd.featureId,
  });
}

async function executeRemoveDep(
  mutation: ReturnType<typeof useUpdateDeps>,
  cmd: DialogCommand,
  mentions: string[],
  appendEntry: (entry: NewTranscriptEntry) => void,
): Promise<void> {
  await mutation.mutateAsync({
    featureId: cmd.featureId,
    remove: mentions,
  });
  appendEntry({
    role: "assistant",
    text: `Removed ${mentions.join(", ")} from ${cmd.featureId}'s deps.`,
    diagnosisFeatureId: cmd.featureId,
  });
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
