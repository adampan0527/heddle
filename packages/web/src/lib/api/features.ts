// SPDX-License-Identifier: Apache-2.0
/**
 * TanStack Query hooks for `/api/projects/:id/features*`.
 *
 * Mirrors the pattern in `lib/api/projects.ts`: every hook returns a
 * typed TanStack result; the mutation invalidates the matching query
 * key so any kanban/dialog subscriber refetches after a state change.
 *
 * feat-035 wires these into the kanban. The drag-to-start mutation
 * optimistically updates the feature's `status` to `in_progress` so
 * the column highlight flips immediately, then rolls back on failure
 * (the daemon enforces single-active server-side, but we want the UI
 * to feel instant on the happy path).
 *
 * feat-043 (D-033) adds `useRetryFeature` and `useTransitionFeature`
 * for the `@feat-XXX retry / retry-with-hint / mark-done / abandon`
 * dialog commands. Both mutations invalidate the features list so the
 * kanban reflects the new status without a manual refresh.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import type {
  ApiErr,
  ApiOk,
  DraftCard,
  Feature,
  TransitionAction,
} from "@heddle/shared";

import { apiFetch } from "../query-client.js";
import { unwrap } from "./errors.js";

/**
 * Query key factory — invalidations use the prefix to refetch every
 * project-scoped feature list at once (rare but possible if we ever
 * cross-reference features across projects).
 */
export const featuresKey = (projectId: string) =>
  ["features", projectId] as const;

/** GET /api/projects/:id/features */
export function useFeatures(
  projectId: string | null,
): UseQueryResult<Feature[], Error> {
  return useQuery<Feature[], Error>({
    queryKey: featuresKey(projectId ?? "__none__"),
    enabled: projectId !== null,
    queryFn: async () => {
      const res = await apiFetch(`/api/projects/${projectId}/features`);
      const body = (await res.json()) as
        | ApiOk<{ project_id: string; features: Feature[] }>
        | ApiErr;
      return unwrap(body, res.status).features;
    },
  });
}

export interface StartFeatureBody {
  featureId: string;
  /** Explicit config name; null forces default; undefined = use feature's stored value. */
  implementation_model?: string | null;
}

/**
 * POST /api/projects/:id/features/:fid/start
 *
 * Optimistic update: set the dragged feature's `status` to
 * `in_progress` immediately, then revert on failure. The daemon is the
 * source of truth (it enforces single-active server-side), so we
 * always invalidate on settle to reconcile.
 */
export function useStartFeature(
  projectId: string | null,
): UseMutationResult<unknown, Error, StartFeatureBody> {
  const qc = useQueryClient();
  return useMutation<unknown, Error, StartFeatureBody>({
    mutationFn: async ({ featureId, implementation_model }) => {
      if (!projectId) throw new Error("no active project");
      const body: Record<string, unknown> = {};
      // Mirror the node server's narrow-wire behavior (see features.ts):
      // only forward `implementation_model` when explicitly set.
      if (implementation_model !== undefined) {
        body.implementation_model = implementation_model;
      }
      const res = await apiFetch(
        `/api/projects/${projectId}/features/${featureId}/start`,
        { method: "POST", body: JSON.stringify(body) },
      );
      const parsed = (await res.json()) as ApiOk<unknown> | ApiErr;
      return unwrap(parsed, res.status);
    },
    onMutate: async ({ featureId }) => {
      if (!projectId) return { prev: [] as Feature[] };
      const key = featuresKey(projectId);
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<Feature[]>(key) ?? [];
      qc.setQueryData<Feature[]>(
        key,
        prev.map((f) =>
          f.id === featureId ? { ...f, status: "in_progress" as const } : f,
        ),
      );
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      if (!projectId) return;
      const c = ctx as { prev?: Feature[] } | undefined;
      if (c?.prev) {
        qc.setQueryData(featuresKey(projectId), c.prev);
      }
    },
    onSettled: () => {
      if (!projectId) return;
      void qc.invalidateQueries({ queryKey: featuresKey(projectId) });
    },
  });
}

/**
 * feat-040: confirm a draft tray — POST /api/projects/:id/drafts/confirm.
 *
 * The dialog's auto-decomposition returns a list of draft cards; the
 * user keeps the ones they want and the rest get dropped on the next
 * decomposition round. When they click "Confirm all (N)" the kept
 * cards are POSTed to the new confirm endpoint, the daemon persists
 * each as a `feat-XXX` row in `feature_list.json`, and we invalidate
 * the features query so the kanban refetches the new rows.
 */
export interface ConfirmDraftsBody {
  drafts: DraftCard[];
}

export interface ConfirmDraftsData {
  project_id: string;
  /** The new `feat-XXX` ids assigned by the daemon. */
  feature_ids: string[];
}

export function useConfirmDrafts(
  projectId: string | null,
): UseMutationResult<ConfirmDraftsData, Error, ConfirmDraftsBody> {
  const qc = useQueryClient();
  return useMutation<ConfirmDraftsData, Error, ConfirmDraftsBody>({
    mutationFn: async ({ drafts }) => {
      if (!projectId) throw new Error("no active project");
      const res = await apiFetch(
        `/api/projects/${projectId}/drafts/confirm`,
        { method: "POST", body: JSON.stringify({ drafts }) },
      );
      const parsed = (await res.json()) as
        | ApiOk<ConfirmDraftsData>
        | ApiErr;
      return unwrap(parsed, res.status);
    },
    onSettled: () => {
      if (!projectId) return;
      void qc.invalidateQueries({ queryKey: featuresKey(projectId) });
    },
  });
}

// ---------------------------------------------------------------------------
// feat-043 / D-033: failure-handling dialog commands.
//
// Two new mutations power the `@feat-XXX retry` and `@feat-XXX
// retry-with-hint:<text>` shortcuts (POST .../retry) and the
// `@feat-XXX mark-done` / `@feat-XXX abandon` shortcuts
// (POST .../transition). Both invalidate the features query on settle
// so the kanban/Dialog transcript reflects the new status without a
// manual refresh.
// ---------------------------------------------------------------------------

/** Body of POST /api/projects/:id/features/:fid/retry. */
export interface RetryFeatureBody {
  featureId: string;
  /** Optional LLM hint for retry-with-hint. Forwarded to the daemon
   *  verbatim and fed to the LLM on the next attempt. */
  hint?: string;
}

/** Successful response from POST .../retry. The daemon returns the
 *  refreshed feature row so the kanban can update without a refetch
 *  when the WS event stream lags. */
export interface RetryFeatureData {
  project_id: string;
  feature_id: string;
  feature: Feature;
}

/**
 * POST /api/projects/:id/features/:fid/retry
 *
 * Powers `@feat-XXX retry` and `@feat-XXX retry-with-hint:<text>`.
 * Invalidates the features list on settle so the kanban picks up the
 * status flip (typically pending/in_progress → in_progress).
 */
export function useRetryFeature(
  projectId: string | null,
): UseMutationResult<RetryFeatureData, Error, RetryFeatureBody> {
  const qc = useQueryClient();
  return useMutation<RetryFeatureData, Error, RetryFeatureBody>({
    mutationFn: async ({ featureId, hint }) => {
      if (!projectId) throw new Error("no active project");
      const body: Record<string, unknown> = {};
      // Only forward `hint` when explicitly set — matches the
      // forwardExecution helper in packages/node/src/routes/features.ts
      // which forwards the field only when present.
      if (typeof hint === "string") body.hint = hint;
      const res = await apiFetch(
        `/api/projects/${projectId}/features/${featureId}/retry`,
        { method: "POST", body: JSON.stringify(body) },
      );
      const parsed = (await res.json()) as
        | ApiOk<RetryFeatureData>
        | ApiErr;
      return unwrap(parsed, res.status);
    },
    onSettled: () => {
      if (!projectId) return;
      void qc.invalidateQueries({ queryKey: featuresKey(projectId) });
    },
  });
}

/** Body of POST /api/projects/:id/features/:fid/transition. */
export interface TransitionFeatureBody {
  featureId: string;
  action: TransitionAction;
}

/** Successful response from POST .../transition. */
export interface TransitionFeatureData {
  project_id: string;
  feature_id: string;
  action: TransitionAction;
  feature: Feature;
}

/**
 * POST /api/projects/:id/features/:fid/transition
 *
 * Powers `@feat-XXX mark-done` (action: "mark-done") and
 * `@feat-XXX abandon` (action: "abandon"). The daemon maps
 * `mark-done` → `passing` (with a manual-override note) and
 * `abandon` → `blocked`. The hook invalidates the features query
 * so the kanban re-renders the new status.
 */
export function useTransitionFeature(
  projectId: string | null,
): UseMutationResult<TransitionFeatureData, Error, TransitionFeatureBody> {
  const qc = useQueryClient();
  return useMutation<TransitionFeatureData, Error, TransitionFeatureBody>({
    mutationFn: async ({ featureId, action }) => {
      if (!projectId) throw new Error("no active project");
      const res = await apiFetch(
        `/api/projects/${projectId}/features/${featureId}/transition`,
        { method: "POST", body: JSON.stringify({ action }) },
      );
      const parsed = (await res.json()) as
        | ApiOk<TransitionFeatureData>
        | ApiErr;
      return unwrap(parsed, res.status);
    },
    onSettled: () => {
      if (!projectId) return;
      void qc.invalidateQueries({ queryKey: featuresKey(projectId) });
    },
  });
}