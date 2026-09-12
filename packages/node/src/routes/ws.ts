// SPDX-License-Identifier: Apache-2.0
/**
 * Fastify plugin: browser WebSocket endpoint at /ws — feat-029.
 *
 * Accepts a single WebSocket upgrade (no path parameters) and hands
 * the resulting socket to the `BrowserWsBridge`. The bridge owns
 * message parsing, command forwarding, and event fan-out; this
 * plugin is intentionally tiny — its only job is the upgrade.
 *
 * Loopback-only: feat-026 binds 127.0.0.1 so any socket reaching us
 * is from a local browser. No auth check.
 *
 * Error policy:
 *   - The plugin does not log per-connection events; the bridge does,
 *     so log noise stays proportional to actual activity.
 *   - Fastify v4's TypeBox plumbing fight means we keep the `as any`
 *     cast on `app` (same as the other route plugins).
 */

import type { FastifyPluginAsync } from "fastify";

import { BrowserWsBridge, BROWSER_WS_PATH } from "../browser-ws.js";

export interface BrowserWsRoutesOptions {
  bridge: BrowserWsBridge;
}

export const registerBrowserWs: FastifyPluginAsync<
  BrowserWsRoutesOptions
> = async (app, opts) => {
  const { bridge } = opts;
  // TypeBox provider workaround — see server.ts:71-73.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const typed: any = app;

  typed.get(
    BROWSER_WS_PATH,
    { websocket: true },
    // @fastify/websocket's connection handler signature:
    //   (socket, request) — socket is the underlying `ws` WebSocket.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (connection: any, _req: any): void => {
      // `connection` is a SocketStream in @fastify/websocket v10+.
      // It exposes `.socket` (the underlying ws WebSocket) plus a
      // `.end()` shortcut. Bridge only needs the socket.
      const socket = connection.socket ?? connection;
      bridge.add(socket);
    },
  );
};