// SPDX-License-Identifier: Apache-2.0
/**
 * heddle-node entry point — feat-026 (Fastify skeleton) + feat-027
 * (process supervision).
 *
 * Boot order:
 *   1. Validate loopback host and parse port from env.
 *   2. Build the Fastify app (HTTP + WS for the browser).
 *   3. Bring up the DaemonSupervisor, which spawns `python -m
 *      heddle_daemon` and opens a loopback WS to it.
 *   4. Bind Fastify to the loopback address.
 *   5. Wait for SIGINT / SIGTERM; on signal, close Fastify and stop the
 *      supervisor (which SIGTERMs the daemon and waits up to
 *      SHUTDOWN_GRACE_MS before SIGKILL).
 *
 * The supervisor's structured-event stream is wired into the project
 * logger so daemon-spawn / daemon-exit / restart-scheduled events appear
 * in the same JSON line format as the rest of the node layer. feat-030
 * will subscribe to the supervisor's WS to consume the actual daemon
 * message protocol; feat-029 will fan the supervisor's events out to the
 * browser.
 */

import { buildServer, isLoopback, DEFAULT_HOST, DEFAULT_PORT } from "./server.js";
import { DaemonSupervisor } from "./supervisor.js";
import { BrowserWsBridge } from "./browser-ws.js";
import { logger } from "./lib/logger.js";
import {
  attachFileSink,
  detachFileSink,
} from "./lib/logger.js";
import { RotatingFileSink } from "./lib/rotating-file-sink.js";
import { listProjects } from "./lib/projects-registry.js";
import type { DaemonEventRecord } from "./protocol.js";
import { assertNeverDaemonEvent } from "./protocol.js";

const host = process.env.HEDDLE_NODE_HOST ?? DEFAULT_HOST;
const portRaw = process.env.HEDDLE_NODE_PORT ?? String(DEFAULT_PORT);
const port = Number.parseInt(portRaw, 10);

// feat-015: per-project log directory + Node.js supervisor log
// filename suffix. The Python daemon writes ``<project_id>.daemon.log``;
// the Node.js supervisor writes ``<project_id>.node.log``. Different
// filenames avoid Windows sharing violations and let the two streams
// be diffed independently during debugging.
const DEFAULT_LOGS_DIR = joinFromHome(".heddle", "logs");
const NODE_LOG_SUFFIX = ".node.log";

function joinFromHome(...segments: string[]): string {
  const home = process.env.HOME ?? process.env.USERPROFILE ?? ".";
  return segments.reduce((acc, seg) => joinPath(acc, seg), home);
}

function joinPath(a: string, b: string): string {
  // Cross-platform join that doesn't depend on `node:path` at module
  // load time (so tests can stub HOME before the import).
  return a === "." ? b : `${a}/${b}`;
}

/**
 * feat-015: attach a per-project rotating log sink for the
 * supervisor. Returns the attached sink (or null when no project is
 * registered — graceful fallback). Exported so the integration test
 * can drive it without going through the full process bootstrap.
 *
 * v0.1 single-project scope: the supervisor picks the first project
 * (registry insertion order). feat-034 / feat-050 will turn this into
 * a real active-project picker that closes the sink on project-switch.
 */
export function attachProjectLogSinkForActiveProject(
  projectsReader: () => ProjectLike[] = listProjects,
  logsDir: string = DEFAULT_LOGS_DIR,
  suffix: string = NODE_LOG_SUFFIX,
): RotatingFileSink | null {
  let projects: ProjectLike[];
  try {
    projects = projectsReader();
  } catch (err) {
    logger.warn(
      "node",
      "project_log_sink_skipped",
      `projects registry read failed: ${String(err)}`,
      { reason: "projects_read_failed" },
    );
    return null;
  }
  if (projects.length === 0) {
    logger.warn(
      "node",
      "project_log_sink_skipped",
      "no registered projects; supervisor serves without a rotating log file",
      { reason: "no_projects_registered" },
    );
    return null;
  }
  const active = projects[0];
  const logPath = `${logsDir}/${active.id}${suffix}`;
  let sink: RotatingFileSink;
  try {
    sink = new RotatingFileSink(logPath);
  } catch (err) {
    logger.warn(
      "node",
      "project_log_sink_skipped",
      `cannot attach rotating log sink at ${logPath}: ${String(err)}`,
      {
        reason: "sink_attach_failed",
        log_path: logPath,
        project_id: active.id,
      },
    );
    return null;
  }
  attachFileSink(sink);
  logger.info(
    "node",
    "project_log_sink_attached",
    `rotating log sink attached at ${logPath} (max_bytes=${sink.maxBytes}, backup_count=${sink.backupCount})`,
    {
      log_path: logPath,
      project_id: active.id,
      max_bytes: sink.maxBytes,
      backup_count: sink.backupCount,
    },
  );
  return sink;
}

interface ProjectLike {
  id: string;
  name: string;
  path: string;
  added_at: string;
  last_accessed_at: string;
}

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

// feat-029: bridge between the daemon event stream and browser WS
// clients. Constructed AFTER the supervisor (we subscribe to its
// "daemon-event" EventEmitter) and BEFORE supervisor.start() so
// the subscription is attached before any daemon events can arrive.
const supervisor = new DaemonSupervisor();
const bridge = new BrowserWsBridge(supervisor);
const app = await buildServer({ supervisor, bridge });

// Wire supervisor events into the structured logger. feat-030 will also
// subscribe to `ws-handshake` to attach the daemon message protocol.
supervisor.on("daemon-spawned", (e) =>
  logger.info("node", "daemon_spawned", `daemon pid=${e.pid}`, { pid: e.pid }),
);
supervisor.on("daemon-exit", (e) =>
  logger.warn(
    "node",
    "daemon_exit",
    `daemon pid=${e.pid} exited code=${e.code} signal=${String(e.signal)} lifetime=${e.lifetimeMs}ms`,
    { pid: e.pid, code: e.code, signal: e.signal, lifetime_ms: e.lifetimeMs },
  ),
);
supervisor.on("ws-connected", () =>
  logger.info("node", "ws_connected", "supervisor WS connected to daemon"),
);
supervisor.on("ws-disconnected", (e) =>
  logger.warn(
    "node",
    "ws_disconnected",
    `supervisor WS disconnected code=${e.code} reason=${e.reason}`,
    { code: e.code, reason: e.reason },
  ),
);
supervisor.on("ws-ping-missed", (e) =>
  logger.warn(
    "node",
    "ws_ping_missed",
    `missed ${e.consecutive} consecutive pongs`,
    { consecutive: e.consecutive },
  ),
);
supervisor.on("restart-scheduled", (e) =>
  logger.warn(
    "node",
    "restart_scheduled",
    `restart in ${e.delayMs}ms (cause=${e.cause})`,
    { delay_ms: e.delayMs, cause: e.cause },
  ),
);
supervisor.on("restart-budget-warning", (e) =>
  logger.warn(
    "node",
    "restart_budget_warning",
    `restart-budget-warning (cause=${e.cause}); feat-047 will track per-feature counter`,
    { cause: e.cause },
  ),
);

// feat-030: forward every daemon-pushed event to the structured
// logger so the JSON-line stream (T-017) carries the full picture of
// feature progress / completion / failure / streamed dialog tokens.
// Switch is exhaustive — `assertNeverDaemonEvent` catches a future
// event added to `protocol.ts` without a matching arm here.
//
// `DaemonEventRecord.payload` is `Record<string, unknown>` on the
// wire layer; each arm re-casts to the variant's typed payload so
// the discriminant narrowing works inside the switch.
supervisor.on("daemon-event", (e: DaemonEventRecord) => {
  const fid = e.feature_id ?? undefined; // null → undefined for LogFields
  switch (e.event) {
    case "feature_attempt_started": {
      const p = e.payload as { attempt_id: string };
      logger.info(
        "node",
        "daemon_feature_attempt_started",
        `feature ${e.feature_id ?? "?"} attempt ${p.attempt_id} started`,
        {
          project_id: e.project_id,
          feature_id: fid,
          attempt_id: p.attempt_id,
        },
      );
      break;
    }
    case "feature_progress": {
      const p = e.payload as {
        attempt_id: string;
        step: number;
        message: string;
      };
      logger.info(
        "node",
        "daemon_feature_progress",
        `feature ${e.feature_id ?? "?"} step ${p.step}: ${p.message}`,
        {
          project_id: e.project_id,
          feature_id: fid,
          attempt_id: p.attempt_id,
          step: p.step,
        },
      );
      break;
    }
    case "feature_done": {
      const p = e.payload as { attempt_id: string };
      logger.info(
        "node",
        "daemon_feature_done",
        `feature ${e.feature_id ?? "?"} attempt ${p.attempt_id} done`,
        {
          project_id: e.project_id,
          feature_id: fid,
          attempt_id: p.attempt_id,
        },
      );
      break;
    }
    case "feature_failed": {
      const p = e.payload as {
        attempt_id: string;
        error_code: string;
        error_message: string;
      };
      logger.warn(
        "node",
        "daemon_feature_failed",
        `feature ${e.feature_id ?? "?"} attempt ${p.attempt_id} failed: ${p.error_code} ${p.error_message}`,
        {
          project_id: e.project_id,
          feature_id: fid,
          attempt_id: p.attempt_id,
          error_code: p.error_code,
        },
      );
      break;
    }
    case "feature_stopped": {
      const p = e.payload as { attempt_id: string };
      logger.info(
        "node",
        "daemon_feature_stopped",
        `feature ${e.feature_id ?? "?"} attempt ${p.attempt_id} stopped by user`,
        {
          project_id: e.project_id,
          feature_id: fid,
          attempt_id: p.attempt_id,
        },
      );
      break;
    }
    case "log_line": {
      // Daemon-side structured log forwarded verbatim. The daemon
      // already redacted sensitive fields before emitting; we just
      // route to the matching logger level. Unknown levels fall back
      // to `info`.
      const p = e.payload as {
        level: "debug" | "info" | "warn" | "error";
        event: string;
        msg: string;
      };
      const fn = logger[p.level] ?? logger.info;
      fn("node", p.event, p.msg, {
        project_id: e.project_id,
        feature_id: fid,
        forwarded_from: "daemon",
      });
      break;
    }
    case "dialog_token": {
      const p = e.payload as { token: string };
      // Streamed LLM token. Don't dump full text to stderr (noisy);
      // just record arrival so we can correlate with dialog_done.
      logger.debug(
        "node",
        "daemon_dialog_token",
        `dialog token (${p.token.length} chars)`,
        {
          project_id: e.project_id,
          chars: p.token.length,
        },
      );
      break;
    }
    case "dialog_done": {
      const p = e.payload as { full_text: string };
      logger.info(
        "node",
        "daemon_dialog_done",
        `dialog stream complete (${p.full_text.length} chars)`,
        {
          project_id: e.project_id,
          chars: p.full_text.length,
        },
      );
      break;
    }
    default:
      // The switch above enumerates every DaemonEventName; an
      // unhandled value at runtime means the daemon emitted a new
      // event we don't know about. Log it at warn so the operator
      // sees it without crashing the supervisor.
      logger.warn(
        "node",
        "daemon_event_unhandled",
        `daemon emitted unknown event ${String((e as { event: unknown }).event)}`,
        {
          project_id: e.project_id,
          feature_id: fid,
          event: e.event,
        },
      );
      // assertNeverDaemonEvent is the compile-time exhaustiveness
      // check; we deliberately don't call it at runtime because
      // daemon-side evolution must not crash the supervisor.
      void assertNeverDaemonEvent;
      break;
  }
});

try {
  await supervisor.start();
  logger.info(
    "node",
    "supervisor_started",
    `DaemonSupervisor entered RUNNING; daemon pid=${supervisor.pid ?? "?"}`,
  );
  // feat-015: now that the daemon port is confirmed live, attach the
  // per-project rotating log sink. We do this AFTER supervisor.start()
  // so a failing sink never blocks the daemon from running.
  attachProjectLogSinkForActiveProject();
} catch (err) {
  logger.error("node", "supervisor_start_failed", String(err));
  process.exit(1);
}

try {
  await app.listen({ host, port });
  logger.info(
    "node",
    "listening",
    `Fastify bound ${host}:${port} (feat-026 skeleton + feat-027 supervisor)`,
    { host, port },
  );
} catch (err) {
  logger.error("node", "bind_failed", String(err), { host, port });
  await supervisor.stop();
  process.exit(1);
}

const shutdown = async (signal: NodeJS.Signals): Promise<void> => {
  logger.info("node", "shutdown", `received ${signal}; closing Fastify + supervisor`);
  try {
    await app.close();
  } catch (err) {
    logger.warn("node", "shutdown_error", String(err));
  }
  // feat-015: detach + close the rotating log sink before stopping
  // the supervisor so the trailing log events land on disk.
  try {
    detachFileSink();
  } catch (err) {
    logger.warn("node", "log_sink_detach_failed", String(err));
  }
  try {
    await supervisor.stop();
  } catch (err) {
    logger.warn("node", "supervisor_stop_failed", String(err));
  }
  process.exit(0);
};

for (const sig of ["SIGINT", "SIGTERM"] as const) {
  process.on(sig, () => {
    void shutdown(sig);
  });
}