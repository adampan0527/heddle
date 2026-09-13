// SPDX-License-Identifier: Apache-2.0
/**
 * TanStack Query hooks for `/api/projects*`. All mutations invalidate
 * the `["projects"]` query so any open project switcher refreshes
 * after add/remove.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import type {
  AddProjectBody,
  ApiErr,
  ApiOk,
  Project,
  SandboxLevel,
} from "@heddle/shared";

import { apiFetch } from "../query-client.js";
import { unwrap } from "./errors.js";

const PROJECTS_KEY = ["projects"] as const;

export function useProjects(): UseQueryResult<Project[], Error> {
  return useQuery<Project[], Error>({
    queryKey: PROJECTS_KEY,
    queryFn: async () => {
      const res = await apiFetch("/api/projects");
      const body = (await res.json()) as ApiOk<{ projects: Project[] }> | ApiErr;
      return unwrap(body, res.status).projects;
    },
  });
}

export function useCreateProject(): UseMutationResult<
  Project,
  Error,
  AddProjectBody
> {
  const qc = useQueryClient();
  return useMutation<Project, Error, AddProjectBody>({
    mutationFn: async (body) => {
      const res = await apiFetch("/api/projects", {
        method: "POST",
        body: JSON.stringify(body),
      });
      const parsed = (await res.json()) as ApiOk<{ project: Project }> | ApiErr;
      return unwrap(parsed, res.status).project;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}

export function useDeleteProject(): UseMutationResult<
  Project,
  Error,
  string
> {
  const qc = useQueryClient();
  return useMutation<Project, Error, string>({
    mutationFn: async (projectId) => {
      const res = await apiFetch(`/api/projects/${projectId}`, {
        method: "DELETE",
      });
      const parsed = (await res.json()) as ApiOk<{ project: Project }> | ApiErr;
      return unwrap(parsed, res.status).project;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}

// feat-055 / D-053: PATCH a project's sandbox level. The daemon writes
// to `<project>/.heddle/config.yaml`; we invalidate the projects query
// so the SandboxIndicator's badge updates immediately.
export function useUpdateProjectSandbox(): UseMutationResult<
  { project_id: string; sandbox_level: SandboxLevel },
  Error,
  { projectId: string; sandbox_level: SandboxLevel }
> {
  const qc = useQueryClient();
  return useMutation<
    { project_id: string; sandbox_level: SandboxLevel },
    Error,
    { projectId: string; sandbox_level: SandboxLevel }
  >({
    mutationFn: async ({ projectId, sandbox_level }) => {
      const res = await apiFetch(`/api/projects/${projectId}`, {
        method: "PATCH",
        body: JSON.stringify({ sandbox_level }),
      });
      const parsed = (await res.json()) as
        | ApiOk<{ project_id: string; sandbox_level: string }>
        | ApiErr;
      const data = unwrap(parsed, res.status);
      return {
        project_id: data.project_id,
        sandbox_level: data.sandbox_level as SandboxLevel,
      };
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}
