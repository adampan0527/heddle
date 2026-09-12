// SPDX-License-Identifier: Apache-2.0
/**
 * Fastify skeleton — feat-026.
 *
 * Bare-bones HTTP + WebSocket server bound to loopback only. Registers
 * three 501 placeholder routes (`/api/projects`, `/api/features`,
 * `/api/dialog`) so the Node.js ↔ Browser wiring (feat-029 / feat-032)
 * can start pointing at real endpoints in subsequent features.
 *
 * Loopback-only is enforced at config-time in `main.ts`; this module
 * just trusts the host it was given.
 *
 * Note on TypeBox type provider (feat-026 step 2): the typebox
 * package alone does not expose the Fastify type provider — that lives
 * in the separate ``fastify-type-provider-typebox`` adapter. We
 * deliberately defer registration to feat-028, which will be the
 * first feature to attach request / response schemas. The
 * ``@sinclair/typebox`` dependency is already declared in
 * package.json so feat-028 has no install step to add.
 */

import Fastify, { FastifyInstance } from "fastify";
import websocket from "@fastify/websocket";
import fastifyStatic from "@fastify/static";
import { logger } from "./lib/logger.js";

export const DEFAULT_HOST = "127.0.0.1";

/**
 * Default port. 5173 is reserved by packages/web Vite (vite.config.ts),
 * so the Node.js process binds 5174 by default — keeps the dev loop
 * "run both with no extra config" working out of the box.
 */
export const DEFAULT_PORT = 5174;

/** Three reserved routes that return 501 until feat-028 ships. */
export const PLACEHOLDER_ROUTES: ReadonlyArray<string> = [
  "/api/projects",
  "/api/features",
  "/api/dialog",
];

/**
 * Returns true for loopback addresses. Mirrors the Python daemon's
 * `_is_loopback` (packages/daemon/heddle_daemon/server.py) plus a
 * TypeScript-friendly regex for the 127.0.0.0/8 block. Does NOT
 * resolve DNS — hostname inputs are rejected outright so a future
 * config-file loader cannot sneak a public host past the check.
 */
export function isLoopback(host: string): boolean {
  if (host === "localhost") return true;
  if (host === "::1") return true;
  if (host.startsWith("127.")) return true;
  return false;
}

/**
 * Build (but do not listen on) a configured Fastify instance.
 *
 * Splitting build from listen lets tests inject / close the server
 * via fastify's in-process `.inject()` API without subprocess dance.
 */
export async function buildServer(): Promise<FastifyInstance> {
  const app = Fastify({ logger: false });
  await app.register(websocket);
  // Serve packages/web/dist if it exists (post-feat-032 build). The
  // route is silently no-op'd by @fastify/static when the root path
  // is absent, so the skeleton works in dev mode without a web build.
  const webDist = new URL("../../web/dist", import.meta.url).pathname;
  await app.register(fastifyStatic, {
    root: webDist,
    prefix: "/",
    decorateReply: false,
  });

  for (const path of PLACEHOLDER_ROUTES) {
    app.route({
      method: ["GET", "POST", "PUT", "DELETE"],
      url: path,
      handler: async (_req, reply) => {
        logger.info(
          "node",
          "api_not_implemented",
          `501 for ${path}; feat-028 will replace this stub`,
          { path },
        );
        await reply
          .code(501)
          .send({ error: "not_implemented", path });
      },
    });
  }
  return app;
}