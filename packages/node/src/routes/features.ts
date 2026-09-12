// SPDX-License-Identifier: Apache-2.0
/**
 * REST routes for ``/api/projects/:id/features*`` — feat-028.
 *
 * Two endpoints:
 *
 *   GET  /api/projects/:id/features                → list features
 *   POST /api/projects/:id/features/:fid/transition → drive the
 *                                                    5-state machine
 *                                                    (retry / abandon /
 *                                                    mark-done)
 *
 * Shares the supervisor-forwarding helper with projects.ts; see that
 * file's header comment for the HTTP status mapping. The
 * ``forwardOrFail`` helper is re-exported from projects.js so tests
 * can import it from a single place.
 */

import type { FastifyPluginAsync } from "fastify";
import { Type } from "@sinclair/typebox";

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
      ApiOk<{ project_id: string; features: unknown[] }> | ApiErr
    > => {
      const out = await forwardOrFail<{
        project_id: string;
        features: unknown[];
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
          feature: unknown;
        }>
      | ApiErr
    > => {
      const out = await forwardOrFail<{
        project_id: string;
        feature_id: string;
        action: string;
        feature: unknown;
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
};