// SPDX-License-Identifier: Apache-2.0
/**
 * feat-054 / D-054: post-confirm feature modification routes.
 *
 * Five new mutation endpoints that operate on existing features:
 *
 *   POST   /api/projects/:id/features/:fid/split       — split into N
 *   POST   /api/projects/:id/features/merge            — merge N into 1
 *   PATCH  /api/projects/:id/features/:fid             — edit fields
 *   PATCH  /api/projects/:id/features/:fid/priority    — reprioritize
 *   POST   /api/projects/:id/features/:fid/deps        — add/remove deps
 *
 * All five forward to the daemon's per-command handlers (which are
 * thin wrappers around `feature_list_io`'s split_feature /
 * merge_features / edit_feature / set_priority / update_deps).
 * Destructive ops (split, merge, deps-remove) include a `diff` field
 * in the response so the dialog UI can show the before/after
 * confirmation card before invoking the mutation (D-054: "destructive
 * ops MUST show diff before execution").
 *
 * Wire shape note: the body schemas mirror the daemon handlers'
 * payload shape exactly so the same JSON round-trips through Node.js
 * to Python without coercion.
 */

import type { FastifyPluginAsync } from "fastify";
import { Type } from "@sinclair/typebox";

import type { DaemonSupervisor } from "../supervisor.js";
import { forwardOrFail } from "./projects.js";
import type { ApiErr, ApiOk } from "../types.js";

// ---------- shared schemas ----------

const FeatureParamsSchema = Type.Object({
  id: Type.String({ minLength: 1 }),
  fid: Type.String({ minLength: 1 }),
});

const ProjectParamsSchema = Type.Object({
  id: Type.String({ minLength: 1 }),
});

// Each split child is a partial feature row. Required: title. The
// daemon defaults category / priority / kind from the source row.
const SplitChildSchema = Type.Object({
  id: Type.Optional(Type.String({ minLength: 1 })),
  title: Type.String({ minLength: 1, maxLength: 500 }),
  description: Type.Optional(Type.String({ maxLength: 4000 })),
  steps: Type.Optional(Type.Array(Type.String({ maxLength: 500 }))),
  depends_on: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
  category: Type.Optional(Type.String({ minLength: 1 })),
  priority: Type.Optional(
    Type.Union([
      Type.Literal("high"),
      Type.Literal("medium"),
      Type.Literal("low"),
    ]),
  ),
  kind: Type.Optional(
    Type.Union([
      Type.Literal("feature"),
      Type.Literal("bugfix"),
      Type.Literal("enhancement"),
    ]),
  ),
});

const SplitBodySchema = Type.Object({
  new_features: Type.Array(SplitChildSchema, { minItems: 1, maxItems: 20 }),
});

const MergeBodySchema = Type.Object({
  source_ids: Type.Array(Type.String({ minLength: 1 }), {
    minItems: 1,
    maxItems: 20,
  }),
  target: Type.Object({
    id: Type.String({ minLength: 1, maxLength: 200 }),
    title: Type.String({ minLength: 1, maxLength: 500 }),
    description: Type.Optional(Type.String({ maxLength: 4000 })),
    steps: Type.Optional(Type.Array(Type.String({ maxLength: 500 }))),
    depends_on: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
    category: Type.Optional(Type.String({ minLength: 1 })),
    priority: Type.Optional(
      Type.Union([
        Type.Literal("high"),
        Type.Literal("medium"),
        Type.Literal("low"),
      ]),
    ),
    kind: Type.Optional(
      Type.Union([
        Type.Literal("feature"),
        Type.Literal("bugfix"),
        Type.Literal("enhancement"),
      ]),
    ),
  }),
});

const VALID_CATEGORIES = [
  "functional",
  "ui",
  "error-handling",
  "accessibility",
  "performance",
] as const;

const EditBodySchema = Type.Object({
  title: Type.Optional(Type.String({ minLength: 1, maxLength: 500 })),
  description: Type.Optional(Type.String({ minLength: 1, maxLength: 4000 })),
  steps: Type.Optional(Type.Array(Type.String({ maxLength: 500 }))),
  category: Type.Optional(Type.Union(VALID_CATEGORIES.map((c) => Type.Literal(c)))),
});

const PriorityBodySchema = Type.Object({
  priority: Type.Union([
    Type.Literal("high"),
    Type.Literal("medium"),
    Type.Literal("low"),
  ]),
});

const DepsBodySchema = Type.Object({
  add: Type.Optional(
    Type.Array(Type.String({ minLength: 1 }), { maxItems: 50 }),
  ),
  remove: Type.Optional(
    Type.Array(Type.String({ minLength: 1 }), { maxItems: 50 }),
  ),
});

// ---------- plugin ----------

export interface ModifyFeatureRoutesOptions {
  supervisor?: DaemonSupervisor | undefined;
}

export const registerModifyFeatureRoutes: FastifyPluginAsync<
  ModifyFeatureRoutesOptions
> = async (app, opts) => {
  const { supervisor } = opts;
  const typed: any = app;

  // POST /api/projects/:id/features/:fid/split
  typed.post(
    "/api/projects/:id/features/:fid/split",
    {
      schema: {
        params: FeatureParamsSchema,
        body: SplitBodySchema,
      },
    },
    async (req: any, reply: any): Promise<ApiOk<unknown> | ApiErr> => {
      const out = await forwardOrFail<unknown>(
        supervisor,
        "feature_split",
        {
          project_id: req.params.id,
          feature_id: req.params.fid,
          new_features: req.body.new_features,
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

  // POST /api/projects/:id/features/merge
  typed.post(
    "/api/projects/:id/features/merge",
    {
      schema: {
        params: ProjectParamsSchema,
        body: MergeBodySchema,
      },
    },
    async (req: any, reply: any): Promise<ApiOk<unknown> | ApiErr> => {
      const out = await forwardOrFail<unknown>(
        supervisor,
        "feature_merge",
        {
          project_id: req.params.id,
          source_ids: req.body.source_ids,
          target: req.body.target,
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

  // PATCH /api/projects/:id/features/:fid (edit)
  typed.patch(
    "/api/projects/:id/features/:fid",
    {
      schema: {
        params: FeatureParamsSchema,
        body: EditBodySchema,
      },
    },
    async (req: any, reply: any): Promise<ApiOk<unknown> | ApiErr> => {
      const body = req.body ?? {};
      const out = await forwardOrFail<unknown>(
        supervisor,
        "feature_edit",
        {
          project_id: req.params.id,
          feature_id: req.params.fid,
          ...body,
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

  // PATCH /api/projects/:id/features/:fid/priority
  typed.patch(
    "/api/projects/:id/features/:fid/priority",
    {
      schema: {
        params: FeatureParamsSchema,
        body: PriorityBodySchema,
      },
    },
    async (req: any, reply: any): Promise<ApiOk<unknown> | ApiErr> => {
      const out = await forwardOrFail<unknown>(
        supervisor,
        "feature_reprioritize",
        {
          project_id: req.params.id,
          feature_id: req.params.fid,
          priority: req.body.priority,
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

  // POST /api/projects/:id/features/:fid/deps
  typed.post(
    "/api/projects/:id/features/:fid/deps",
    {
      schema: {
        params: FeatureParamsSchema,
        body: DepsBodySchema,
      },
    },
    async (req: any, reply: any): Promise<ApiOk<unknown> | ApiErr> => {
      const body = req.body ?? {};
      const out = await forwardOrFail<unknown>(
        supervisor,
        "feature_update_deps",
        {
          project_id: req.params.id,
          feature_id: req.params.fid,
          add: body.add,
          remove: body.remove,
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