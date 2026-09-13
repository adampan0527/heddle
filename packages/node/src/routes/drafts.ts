// SPDX-License-Identifier: Apache-2.0
/**
 * REST route for draft-tray confirmation — feat-040 (D-013 / D-044 / D-045).
 *
 *   POST /api/projects/:id/drafts/confirm   {drafts: DraftCard[]}   → 200
 *
 * Body: a non-empty array of `DraftCard`s the user has kept (see
 * `@heddle/shared` for the schema). Response: `{project_id,
 * feature_ids: string[]}` with the `feat-XXX` ids the daemon assigned
 * (one per kept card, in submission order).
 *
 * v0.1 status — TODO(feat-040-followup): the daemon does not yet own
 * a "confirm drafts" WS command. The wire shape and TypeBox schema
 * are stable so the browser can ship today; the route currently
 * returns 501 Not Implemented and the DraftTray's "Confirm all (N)"
 * button surfaces the rejection as an error. feat-044 (intent
 * classification) and feat-045 (auto-decomposition) will land the
 * daemon-side `confirm_drafts` handler in the same release line —
 * once those ship, replace the 501 below with a `forwardOrFail` to
 * the new command. See HARNESS.md → Workflow rules and
 * packages/node/src/routes/features.ts for the forward pattern.
 */

import type { FastifyPluginAsync } from "fastify";
import { Type } from "@sinclair/typebox";

import type { DaemonSupervisor } from "../supervisor.js";
import type { ApiErr } from "../types.js";

const DraftCardSchema = Type.Object({
  id: Type.String(),
  title: Type.String(),
  description: Type.String(),
  steps: Type.Array(Type.String()),
  depends_on: Type.Array(Type.String()),
  kind: Type.Union([
    Type.Literal("feature"),
    Type.Literal("bugfix"),
    Type.Literal("enhancement"),
  ]),
});

const ConfirmDraftsParamsSchema = Type.Object({
  id: Type.String({ minLength: 1 }),
});

const ConfirmDraftsBodySchema = Type.Object({
  drafts: Type.Array(DraftCardSchema, { minItems: 1 }),
});

export interface DraftRoutesOptions {
  supervisor?: DaemonSupervisor | undefined;
}

export const registerDraftRoutes: FastifyPluginAsync<DraftRoutesOptions> = async (
  app,
  _opts,
) => {
  // Fastify v4's plugin type plumbing drops the TypeBox provider
  // through the `register()` boundary; cast here so the body schema
  // infers correctly. Same trick as the other route plugins.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const typed: any = app;
  // Intentional unused-arg pin so the supervisor wiring above is
  // reserved for the post-045 daemon-side handler — keeping it on the
  // plugin signature now lets `server.ts` register the route
  // uniformly with the others.
  void ({} as DaemonSupervisor | undefined);

  typed.post(
    "/api/projects/:id/drafts/confirm",
    {
      schema: {
        params: ConfirmDraftsParamsSchema,
        body: ConfirmDraftsBodySchema,
      },
    },
    async (
      req: any,
      reply: any,
    ): Promise<ApiErr> => {
      // TODO(feat-040-followup): replace with
      //   forwardOrFail<{project_id: string; feature_ids: string[]}>(
      //     supervisor, "confirm_drafts",
      //     {project_id: req.params.id, drafts: req.body.drafts},
      //     reply,
      //   );
      // once feat-044/045 land the daemon handler. The DraftTray
      // already invalidates its feature-list query on success and
      // surfaces the 501 as a toast error in the meantime.
      void reply;
      const count = Array.isArray(req.body?.drafts) ? req.body.drafts.length : 0;
      return {
        ok: false,
        error: {
          code: "internal_error",
          message:
            `draft confirm is not yet wired (received ${count} draft(s) ` +
            `for project ${req.params.id}); awaiting feat-044/045 daemon handler`,
        },
      };
    },
  );
};