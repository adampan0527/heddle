// SPDX-License-Identifier: Apache-2.0
/**
 * REST route for the dialog submission — feat-028.
 *
 *   POST /api/projects/:id/dialog   {message: string}   → 200
 *
 * In v0.1 the daemon's ``dialog_turn`` handler is a chat echo stub;
 * feat-044 replaces it with intent classification + draft-card
 * generation. The wire shape here is forward-compatible: the route
 * only knows about ``{kind, text, drafts?}`` so when feat-044 starts
 * returning ``kind: "work"`` with draft cards, the browser picks
 * them up without any change on this side.
 */

import type { FastifyPluginAsync } from "fastify";
import { Type } from "@sinclair/typebox";

import type { DaemonSupervisor } from "../supervisor.js";
import { forwardOrFail } from "./projects.js";
import type { ApiErr, ApiOk } from "../types.js";

// ---------- TypeBox schemas ----------

const DraftCardSchema = Type.Object({
  id: Type.String(),           // "temp-NNN" while in draft state
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

const DialogResponseSchema = Type.Object({
  ok: Type.Literal(true),
  data: Type.Object({
    project_id: Type.String(),
    kind: Type.Union([Type.Literal("chat"), Type.Literal("work")]),
    text: Type.String(),
    drafts: Type.Optional(Type.Array(DraftCardSchema)),
  }),
});

const DialogBodySchema = Type.Object({
  message: Type.String({ minLength: 1, maxLength: 8000 }),
});

const DialogParamsSchema = Type.Object({
  id: Type.String({ minLength: 1 }),
});

// ---------- plugin ----------

export interface DialogRoutesOptions {
  supervisor?: DaemonSupervisor | undefined;
}

export const registerDialogRoutes: FastifyPluginAsync<
  DialogRoutesOptions
> = async (app, opts) => {
  const { supervisor } = opts;
  // Cast: see projects.ts for the Fastify v4 type-plumbing rationale.
  const typed: any = app;

  typed.post(
    "/api/projects/:id/dialog",
    {
      schema: {
        params: DialogParamsSchema,
        body: DialogBodySchema,
        response: { 200: DialogResponseSchema },
      },
    },
    async (req: any, reply: any): Promise<
      | ApiOk<{
          project_id: string;
          kind: "chat" | "work";
          text: string;
          drafts?: unknown[];
        }>
      | ApiErr
    > => {
      const out = await forwardOrFail<{
        project_id: string;
        kind: "chat" | "work";
        text: string;
        drafts?: unknown[];
      }>(
        supervisor,
        "dialog_turn",
        { project_id: req.params.id, message: req.body.message },
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