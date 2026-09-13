// SPDX-License-Identifier: Apache-2.0
/**
 * TanStack Query mutation hook for `/api/projects/:id/dialog` — feat-038.
 *
 * Submits a natural-language message from the persistent bottom dialog
 * to the daemon and returns its `{ kind, text, drafts? }` envelope.
 * The dialog component uses this hook to power submit; feat-039 will
 * layer `@feat-XXX` autocomplete on top of the textarea and feat-040
 * will render the returned draft cards above the kanban.
 *
 * Streaming: v0.1 awaits the full response. feat-048's structured
 * event log + feat-029's WS layer carry the stream; once those are
 * fronted by a typed hook we can swap `mutateAsync` for a streaming
 * reader here without changing the call site.
 */

import {
  useMutation,
  type UseMutationResult,
} from "@tanstack/react-query";
import type {
  ApiErr,
  ApiOk,
  DialogResponse,
} from "@heddle/shared";

import { apiFetch } from "../query-client.js";
import { unwrap } from "./errors.js";

export interface SubmitDialogBody {
  message: string;
}

/**
 * POST /api/projects/:id/dialog
 *
 * No `onSettled` invalidations: the dialog response is ephemeral from
 * TanStack Query's point of view — the chat history lives in component
 * state and the feature list is unaffected by a chat reply. The hook
 * does invalidate the features list when the response carries draft
 * cards (feat-040 will use that signal to refresh the kanban), but
 * for v0.1 the dialog component owns the draft tray lifecycle itself.
 */
export function useSubmitDialog(
  projectId: string | null,
): UseMutationResult<DialogResponse, Error, SubmitDialogBody> {
  return useMutation<DialogResponse, Error, SubmitDialogBody>({
    mutationFn: async ({ message }) => {
      if (!projectId) throw new Error("no active project");
      const res = await apiFetch(`/api/projects/${projectId}/dialog`, {
        method: "POST",
        body: JSON.stringify({ message }),
      });
      const parsed = (await res.json()) as
        | ApiOk<DialogResponse>
        | ApiErr;
      return unwrap(parsed, res.status);
    },
  });
}