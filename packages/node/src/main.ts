// SPDX-License-Identifier: Apache-2.0
/**
 * heddle-node entry point — feat-026.
 *
 * Parses HEDDLE_NODE_HOST / HEDDLE_NODE_PORT env vars, refuses
 * non-loopback hosts at config time, then binds Fastify and waits
 * for SIGINT / SIGTERM. The CLI (feat-027 supervisor) eventually
 * spawns this as a subprocess, but the script is also useful for
 * local debugging (`pnpm --filter node dev`).
 */

import { buildServer, isLoopback, DEFAULT_HOST, DEFAULT_PORT } from "./server.js";
import { logger } from "./lib/logger.js";

const host = process.env.HEDDLE_NODE_HOST ?? DEFAULT_HOST;
const portRaw = process.env.HEDDLE_NODE_PORT ?? String(DEFAULT_PORT);
const port = Number.parseInt(portRaw, 10);

if (!isLoopback(host)) {
  logger.error(
    "node",
    "non_loopback_host_refused",
    `HEDDLE_NODE_HOST=${host} is not loopback; refusing to bind. ` +
      `Use 127.0.0.1, localhost, or ::1. (D-037 / feat-026)`,
    { host },
  );
  process.exit(1);
}
if (!Number.isFinite(port) || port <= 0 || port > 65535) {
  logger.error(
    "node",
    "invalid_port",
    `HEDDLE_NODE_PORT=${portRaw} is not a valid TCP port`,
    { raw: portRaw },
  );
  process.exit(1);
}

const app = await buildServer();
try {
  await app.listen({ host, port });
  logger.info(
    "node",
    "listening",
    `Fastify bound ${host}:${port} (feat-026 skeleton)`,
    { host, port },
  );
} catch (err) {
  logger.error("node", "bind_failed", String(err), { host, port });
  process.exit(1);
}

const shutdown = async (signal: NodeJS.Signals): Promise<void> => {
  logger.info("node", "shutdown", `received ${signal}; closing Fastify`);
  try {
    await app.close();
  } catch (err) {
    logger.warn("node", "shutdown_error", String(err));
  }
  process.exit(0);
};

for (const sig of ["SIGINT", "SIGTERM"] as const) {
  process.on(sig, () => {
    void shutdown(sig);
  });
}