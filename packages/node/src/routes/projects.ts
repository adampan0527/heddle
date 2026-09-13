// SPDX-License-Identifier: Apache-2.0
/**
 * REST routes for the ``/api/projects*`` family — feat-028.
 *
 * Thin proxies over the daemon's WS message loop. Each handler
 * validates its input via TypeBox, calls
 * ``supervisor.request("<command>", payload)``, and translates the
 * daemon's response envelope into the HTTP status + body the
 * browser consumes. Mapping rules (see ``daemonCodeToHttpStatus``):
 *
 *   daemon `ok: true`                          → 200 / 201 + `{ok, data}`
 *   daemon `ok: false, code=invalid_input`     → 400 + `{ok, error}`
 *   daemon `ok: false, code=not_found`         → 404 + `{ok, error}`
 *   daemon `ok: false, code=conflict`          → 409 + `{ok, error}`
 *   daemon `ok: false, code=invalid_state`     → 409 + `{ok, error}`
 *   daemon `ok: false, code=schema_too_new`    → 400 + `{ok, error}`
 *   daemon `ok: false, anything else`          → 502 + `{ok, error}`
 *
 *   DaemonUnavailableError                     → 503 + `{ok, error}`
 *   DaemonRequestTimeoutError                  → 504 + `{ok, error}`
 *   Fastify TypeBox validation                  → 400 + `{ok, error}`
 *
 * Why proxies only:
 *   All mutations on ``projects.json`` go through the daemon. A
 *   Node-side mirror would risk two writers fighting for the same
 *   file; feat-029 / feat-030 inherit this discipline so the browser
 *   never sees a parallel source of truth. The same contract is
 * *   documented at the top of ``heddle_common.feature_list_io``.
 */

import type { FastifyInstance } from "fastify";
import type { FastifyPluginAsync } from "fastify";
import { Type } from "@sinclair/typebox";

import type { Project, SandboxLevel } from "@heddle/shared";

import type { DaemonSupervisor } from "../supervisor.js";
import {
  DaemonRequestTimeoutError,
  DaemonUnavailableError,
  type ApiErr,
  type ApiOk,
  type DaemonError,
  type ErrorCode,
} from "../types.js";

// ---------- TypeBox schemas ----------

const ProjectSchema = Type.Object({
  id: Type.String(),
  name: Type.String(),
  path: Type.String(),
  added_at: Type.String(),
  last_accessed_at: Type.String(),
  sandbox_level: Type.Optional(
    Type.Union([
      Type.Literal("read-only"),
      Type.Literal("edit-with-confirm"),
      Type.Literal("full"),
      Type.Null(),
    ]),
  ),
});

const ProjectListResponseSchema = Type.Object({
  ok: Type.Literal(true),
  data: Type.Object({
    projects: Type.Array(ProjectSchema),
  }),
});

const AddProjectBodySchema = Type.Object({
  path: Type.String({ minLength: 1 }),
  name: Type.Optional(Type.String()),
});

const ProjectResponseSchema = Type.Object({
  ok: Type.Literal(true),
  data: Type.Object({
    project: ProjectSchema,
  }),
});

const ParamsWithIdSchema = Type.Object({
  id: Type.String({ minLength: 1 }),
});

// feat-055: PATCH /api/projects/:id accepts a sandbox_level change.
// We only validate the level enum here; the daemon re-validates so a
// stray or future variant still surfaces a clean error envelope.
const PatchProjectBodySchema = Type.Object({
  sandbox_level: Type.Union([
    Type.Literal("read-only"),
    Type.Literal("edit-with-confirm"),
    Type.Literal("full"),
  ]),
});

// ---------- plugin options ----------

export interface ProjectRoutesOptions {
  supervisor?: DaemonSupervisor | undefined;
}

export const registerProjectRoutes: FastifyPluginAsync<
  ProjectRoutesOptions
> = async (app, opts) => {
  const { supervisor } = opts;
  // Fastify v4's plugin type plumbing drops the TypeBox provider
  // even though ``FastifyPluginAsyncTypebox`` declares it on the
  // outer signature. Re-bind here so route-level schema inference
  // works. The cast is the documented escape hatch — see the
  // @fastify/type-provider-typebox README §"Using the Type Provider".
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const typed: any = app;

  // GET /api/projects — list every registered project.
  typed.get(
    "/api/projects",
    { schema: { response: { 200: ProjectListResponseSchema } } },
    async (_req: any, reply: any): Promise<ApiOk<{ projects: Project[] }> | ApiErr> => {
      const out = await forwardOrFail<{ projects: Project[] }>(
        supervisor,
        "project_list",
        {},
        reply,
      );
      if (!out.ok) {
        reply.code(out.httpStatus);
        return out.errBody;
      }
      return out.okBody;
    },
  );

  // POST /api/projects — register a new project.
  typed.post(
    "/api/projects",
    {
      schema: {
        body: AddProjectBodySchema,
        response: { 201: ProjectResponseSchema },
      },
    },
    async (req: any, reply: any): Promise<ApiOk<{ project: Project }> | ApiErr> => {
      const out = await forwardOrFail<{ project: Project }>(
        supervisor,
        "project_add",
        { path: req.body.path, name: req.body.name ?? null },
        reply,
      );
      if (!out.ok) {
        reply.code(out.httpStatus);
        return out.errBody;
      }
      reply.code(201);
      return out.okBody;
    },
  );

  // PATCH /api/projects/:id — feat-055 / D-053: update per-project
  // sandbox level. Writes through to ``.heddle/config.yaml`` via the
  // daemon's ``project_sandbox_set`` handler.
  typed.patch(
    "/api/projects/:id",
    {
      schema: {
        params: ParamsWithIdSchema,
        body: PatchProjectBodySchema,
        response: { 200: ProjectResponseSchema },
      },
    },
    async (req: any, reply: any): Promise<ApiOk<{ project: Project }> | ApiErr> => {
      // The daemon returns ``{project_id, sandbox_level}``; we
      // forward that and let TanStack Query's invalidation refresh
      // the full project row. Forward as a synthesized ``project``
      // so the response shape matches the schema.
      const out = await forwardOrFail<{ project_id: string; sandbox_level: string }>(
        supervisor,
        "project_sandbox_set",
        {
          project_id: req.params.id,
          sandbox_level: req.body.sandbox_level as SandboxLevel,
        },
        reply,
      );
      if (!out.ok) {
        reply.code(out.httpStatus);
        return out.errBody;
      }
      const project: Project = {
        id: out.okBody.data.project_id,
        name: "",
        path: "",
        added_at: "",
        last_accessed_at: "",
        sandbox_level: out.okBody.data.sandbox_level as SandboxLevel,
      };
      return { ok: true, data: { project } };
    },
  );

  // DELETE /api/projects/:id — remove + cascade.
  typed.delete(
    "/api/projects/:id",
    {
      schema: {
        params: ParamsWithIdSchema,
        response: { 200: ProjectResponseSchema },
      },
    },
    async (req: any, reply: any): Promise<ApiOk<{ project: Project }> | ApiErr> => {
      const out = await forwardOrFail<{ project: Project }>(
        supervisor,
        "project_remove",
        { project_id: req.params.id },
        reply,
      );
      if (!out.ok) {
        reply.code(out.httpStatus);
        return out.errBody;
      }
      return out.okBody;
    },
  );
};

// ---------- shared forwarding helper ----------

/**
 * Translate a daemon error code into the HTTP status we should send.
 * Pure function — features.ts and dialog.ts use the same mapping so
 * the v0.1 wire shape stays consistent across the three route plugins.
 */
export function daemonCodeToHttpStatus(code: string): number {
  switch (code) {
    case "invalid_input":
      return 400;
    case "not_found":
      return 404;
    case "conflict":
    case "invalid_state":
      return 409;
    case "schema_too_new":
    case "envelope_malformed":
    case "envelope_version_too_new":
      return 400;
    case "daemon_unavailable":
      return 503;
    case "request_timeout":
      return 504;
    case "handler_error":
    case "internal_error":
    default:
      return 502;
  }
}

function makeErr(code: string, message: string): ApiErr {
  // ``code`` is a string at runtime (any of the wire codes the daemon
  // emits, or the route-level ones we synthesise). The ApiErr
  // declaration narrows to ``ErrorCode``; since the daemon and HTTP
  // layer both pass values that ARE ErrorCodes, the cast is safe.
  return { ok: false, error: { code: code as ErrorCode, message } };
}

export type ForwardResult<TData> =
  | { ok: true; okBody: ApiOk<TData>; httpStatus: number }
  | { ok: false; errBody: ApiErr; httpStatus: number };

/**
 * The single chokepoint that:
 *   - calls supervisor.request()
 *   - maps DaemonUnavailableError / DaemonRequestTimeoutError to
 *     their HTTP status + body
 *   - maps daemon-side ok:false envelopes (which the supervisor
 *     unwraps into an Error with `.code` attached) to the right
 *     status via ``daemonCodeToHttpStatus``.
 *
 * Returns a discriminated union rather than throwing — the caller is
 * responsible for calling ``reply.code(httpStatus)`` and returning
 * the right body. We can't throw because Fastify's exception handler
 * always renders 500; the union keeps the contract visible.
 */
export async function forwardOrFail<TData>(
  supervisor: DaemonSupervisor | undefined,
  envelopeType: string,
  payload: Record<string, unknown>,
  reply: { code: (n: number) => unknown },
): Promise<ForwardResult<TData>> {
  if (!supervisor) {
    return {
      ok: false,
      errBody: makeErr("daemon_unavailable", "supervisor not attached"),
      httpStatus: 503,
    };
  }
  try {
    const data = (await supervisor.request(envelopeType, payload)) as TData;
    return { ok: true, okBody: { ok: true, data }, httpStatus: 200 };
  } catch (e) {
    if (e instanceof DaemonUnavailableError) {
      return {
        ok: false,
        errBody: makeErr("daemon_unavailable", e.message),
        httpStatus: 503,
      };
    }
    if (e instanceof DaemonRequestTimeoutError) {
      return {
        ok: false,
        errBody: makeErr("request_timeout", e.message),
        httpStatus: 504,
      };
    }
    const code =
      (e as unknown as { code?: string }).code ?? "internal_error";
    const message = e instanceof Error ? e.message : String(e);
    return {
      ok: false,
      errBody: makeErr(code, message),
      httpStatus: daemonCodeToHttpStatus(code),
    };
  }
  // Quiet unused-import warnings for FastifyInstance (re-exported
  // symbol users might import transitively).
  void ({} as FastifyInstance);
  void reply;
}

/**
 * Public helper for the test suite: get the daemon-error shape that
 * a particular route would produce for a particular code. Lets tests
 * assert on both `ok: false` and the matching HTTP status without
 * re-implementing the mapping.
 */
export function daemonErrorForCode(code: string, message: string): DaemonError {
  return { code: code as ErrorCode, message };
}