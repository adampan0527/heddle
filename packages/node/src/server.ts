// SPDX-License-Identifier: Apache-2.0
/**
 * Fastify skeleton — feat-026 + feat-028 (HTTP REST routes).
 *
 * The three ``/api/projects*``, ``/api/projects/:id/features*``, and
 * ``/api/projects/:id/dialog`` endpoint families live in
 * ``packages/node/src/routes/{projects,features,dialog}.ts``. Each
 * one is registered as a Fastify plugin via ``app.register`` so the
 * skeleton stays small and route-level tests can mount a single
 * plugin in isolation.
 *
 * Loopback-only is enforced at config-time in ``main.ts``; this
 * module just trusts the host it was given.
 *
 * TypeBox provider note (feat-028):
 *   The bare ``@sinclair/typebox`` package does not expose Fastify's
 *   type provider — that lives in the separate
 *   ``@fastify/type-provider-typebox`` adapter. The provider is
 *   registered here (not in each route plugin) so a route can
 *   ``import { Type } from "@sinclair/typebox"`` and pass schemas
 *   straight to ``{schema: {body, params, response}}`` with full
 *   Fastify + TS inference.
 */

import Fastify, { FastifyInstance } from "fastify";
import websocket from "@fastify/websocket";
import fastifyStatic from "@fastify/static";
import {
  TypeBoxValidatorCompiler,
  TypeBoxTypeProvider,
} from "@fastify/type-provider-typebox";

import { logger } from "./lib/logger.js";
import type { DaemonSupervisor } from "./supervisor.js";

export const DEFAULT_HOST = "127.0.0.1";

export const DEFAULT_PORT = 5174;

export function isLoopback(host: string): boolean {
  if (host === "localhost") return true;
  if (host === "::1") return true;
  if (host.startsWith("127.")) return true;
  return false;
}

export interface BuildServerOptions {
  supervisor?: DaemonSupervisor | undefined;
}

/**
 * Build (but do not listen on) a configured Fastify instance.
 *
 * TypeBox wiring note: we install ``TypeBoxValidatorCompiler`` for
 * runtime schema validation, but we deliberately do NOT call
 * ``withTypeProvider<TypeBoxTypeProvider>()`` at the top level. Fastify
 * v4's plugin type plumbing drops the provider through the
 * ``register()`` boundary, and forcing it requires ``as never`` casts
 * inside every plugin. The schemas still validate at request time
 * (TypeBox is wired via ``setValidatorCompiler``), we just lose the
 * IDE-level response-type inference in route handlers. feat-029 can
 * upgrade to the full provider if/when fastify v5 ships.
 */
export async function buildServer(
  options: BuildServerOptions = {},
): Promise<FastifyInstance> {
  // ``as never`` on the Fastify instance: we want TypeBox
  // validation wired without forcing the whole framework type to
  // carry TypeBoxTypeProvider. The ``as never`` cast silences TS so
  // the rest of the file stays clean.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const app = Fastify({ logger: false }) as any;
  app.setValidatorCompiler(TypeBoxValidatorCompiler);
  await app.register(websocket);
  const webDist = new URL("../../web/dist", import.meta.url).pathname;
  await app.register(fastifyStatic, {
    root: webDist,
    prefix: "/",
    decorateReply: false,
  });

  const { registerProjectRoutes } = await import("./routes/projects.js");
  const { registerFeatureRoutes } = await import("./routes/features.js");
  const { registerDialogRoutes } = await import("./routes/dialog.js");

  // The plugin casts are needed because Fastify v4's default
  // generic route registration loses the TypeBox provider through
  // the register boundary. Runtime validation is unaffected.
  await app.register(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    registerProjectRoutes as any,
    { supervisor: options.supervisor },
  );
  await app.register(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    registerFeatureRoutes as any,
    { supervisor: options.supervisor },
  );
  await app.register(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    registerDialogRoutes as any,
    { supervisor: options.supervisor },
  );

  app.addHook("onResponse", async (req: any, reply: any) => {
    logger.info(
      "node",
      "http_request",
      `${req.method} ${req.url} -> ${reply.statusCode}`,
      { method: req.method, url: req.url, status: reply.statusCode },
    );
  });

  logger.info(
    "node",
    "build_server_ready",
    "buildServer assembled with TypeBox validation + 3 route plugins",
    { has_supervisor: options.supervisor !== undefined },
  );
  return app as FastifyInstance;
}

// Re-export so existing tests that imported the type still work.
export type { TypeBoxTypeProvider };