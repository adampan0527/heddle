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
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import type { ApiErr, ApiOk, Feature } from "@heddle/shared";

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