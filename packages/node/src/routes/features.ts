// SPDX-License-Identifier: Apache-2.0
/**
 * REST routes for ``/api/projects/:id/features*`` — feat-028 + feat-031.
 *
 * Endpoints:
 *
 *   GET  /api/projects/:id/features                    → list features
 *   POST /api/projects/:id/features/:fid/transition    → drive the
 *                                                        5-state machine
 *                                                        (retry | abandon |
 *                                                        mark-done)
 *   POST /api/projects/:id/features/:fid/start         → start the
 *                                                        feature (mark
 *                                                        in_progress +
 *                                                        resolve LLM via
 *                                                        feat-031)
 *   POST /api/projects/:id/features/:fid/retry         → same as start
 *                                                        but explicitly
 *                                                        tagged ``retry``
 *                                                        for the
 *                                                        supervisor UI
 *
 * Shares the supervisor-forwarding helper with projects.ts; see that
 * file's header comment for the HTTP status mapping. The
 * ``forwardOrFail`` helper is re-exported from projects.js so tests
 * can import it from a single place.
 */

import type { FastifyPluginAsync } from "fastify";
import { Type } from "@sinclair/typebox";

import type { Feature } from "@heddle/shared";

import type { DaemonSupervisor } from "../supervisor.js";
import {
  forwardOrFail,
} from "./projects.js";
import type { ApiErr, ApiOk } from "../types.js";

// ---------- TypeBox schemas ----------

// One feature row, per feature_list_io's on-disk shape. We mirror the
// full row so the browser can render kanban / DAG without a second
// round-trip; new optional fields added in feat-010 are all listed so
// the schema is forward-compatible with the daemon's wire shape.
const FeatureRowSchema = Type.Object({
  id: Type.String(),
  category: Type.String(),
  description: Type.String(),
  steps: Type.Array(Type.String()),
  status: Type.String(),
  priority: Type.String(),
  depends_on: Type.Array(Type.String()),
  attempts: Type.Array(Type.Unknown()),
  kind: Type.String(),
  fixes: Type.Union([Type.String(), Type.Null()]),
  enhances: Type.Union([Type.String(), Type.Null()]),
  superseded_by: Type.Union([Type.String(), Type.Null()]),
  implementation_model: Type.Union([Type.String(), Type.Null()]),
});

const FeatureListResponseSchema = Type.Object({
  ok: Type.Literal(true),
  data: Type.Object({
    project_id: Type.String(),
    features: Type.Array(FeatureRowSchema),
  }),
});

const TransitionBodySchema = Type.Object({
  action: Type.Union([
    Type.Literal("retry"),
    Type.Literal("abandon"),
    Type.Literal("mark-done"),
  ]),
});

const TransitionResponseSchema = Type.Object({
  ok: Type.Literal(true),
  data: Type.Object({
    project_id: Type.String(),
    feature_id: Type.String(),
    action: Type.String(),
    feature: FeatureRowSchema,
  }),
});

const FeatureParamsSchema = Type.Object({
  id: Type.String({ minLength: 1 }),
  fid: Type.String({ minLength: 1 }),
});

// feat-031: body for the new /start and /retry endpoints. The body
// is optional (clients that don't know about feat-031 can POST `{}`
// and the daemon falls back to the feature's stored
// ``implementation_model`` from ``feature_list.json``). When present
// the value may be a string (explicit config name) or null (force
// the daemon's default-fallback path, ignoring whatever the feature
// row says).
const StartOrRetryBodySchema = Type.Object({
  implementation_model: Type.Optional(
    Type.Union([Type.String({ minLength: 1 }), Type.Null()]),
  ),
});

// ---------- plugin ----------

export interface FeatureRoutesOptions {
  supervisor?: DaemonSupervisor | undefined;
}

export const registerFeatureRoutes: FastifyPluginAsync<
  FeatureRoutesOptions
> = async (app, opts) => {
  const { supervisor } = opts;
  // Cast: see projects.ts for the Fastify v4 type-plumbing rationale.
  const typed: any = app;

  // GET /api/projects/:id/features
  typed.get(
    "/api/projects/:id/features",
    {
      schema: {
        params: Type.Object({ id: Type.String({ minLength: 1 }) }),
        response: { 200: FeatureListResponseSchema },
      },
    },
    async (req: any, reply: any): Promise<
      ApiOk<{ project_id: string; features: Feature[] }> | ApiErr
    > => {
      const out = await forwardOrFail<{
        project_id: string;
        features: Feature[];
      }>(supervisor, "feature_list", { project_id: req.params.id }, reply);
      if (!out.ok) {
        reply.code(out.httpStatus);
        return out.errBody;
      }
      return out.okBody;
    },
  );

  // POST /api/projects/:id/features/:fid/transition
  typed.post(
    "/api/projects/:id/features/:fid/transition",
    {
      schema: {
        params: FeatureParamsSchema,
        body: TransitionBodySchema,
        response: { 200: TransitionResponseSchema },
      },
    },
    async (req: any, reply: any): Promise<
      | ApiOk<{
          project_id: string;
          feature_id: string;
          action: string;
          feature: Feature;
        }>
      | ApiErr
    > => {
      const out = await forwardOrFail<{
        project_id: string;
        feature_id: string;
        action: string;
        feature: Feature;
      }>(
        supervisor,
        "feature_transition",
        {
          project_id: req.params.id,
          feature_id: req.params.fid,
          action: req.body.action,
        },
        reply,
      );
      if (!out.ok) {
        reply.code(out.httpStatus);
        return out.errBody;
      }
      return out.okBody;
    },
  );

  // feat-031: shared handler factory for /start and /retry. Both
  // endpoints forward to the daemon's per-feature command handlers
  // with the body's `implementation_model` field (when present). The
  // daemon resolves the model against its LLM-config registry and
  // emits `feature_attempt_started` + `llm_resolved` events back to
  // the browser over the WS stream.
  const forwardExecution = async (
    req: any,
    reply: any,
    daemonCommand: "start_feature" | "retry_feature",
  ): Promise<ApiOk<unknown> | ApiErr> => {
    const body = req.body ?? {};
    const payload: Record<string, unknown> = {
      project_id: req.params.id,
      feature_id: req.params.fid,
    };
    // Only forward the field when the client explicitly set it. The
    // daemon reads `feature_list.json` directly when the field is
    // missing, so forwarding `undefined` would be a no-op anyway —
    // skipping the key keeps the wire narrow for legacy clients.
    if (Object.prototype.hasOwnProperty.call(body, "implementation_model")) {
      payload.implementation_model = body.implementation_model;
    }
    const out = await forwardOrFail<unknown>(supervisor, daemonCommand, payload, reply);
    if (!out.ok) {
      reply.code(out.httpStatus);
      return out.errBody;
    }
    return out.okBody;
  };

  typed.post(
    "/api/projects/:id/features/:fid/start",
    {
      schema: {
        params: FeatureParamsSchema,
        body: StartOrRetryBodySchema,
      },
    },
    async (req: any, reply: any) => forwardExecution(req, reply, "start_feature"),
  );

  typed.post(
    "/api/projects/:id/features/:fid/retry",
    {
      schema: {
        params: FeatureParamsSchema,
        body: StartOrRetryBodySchema,
      },
    },
    async (req: any, reply: any) => forwardExecution(req, reply, "retry_feature"),
  );
};