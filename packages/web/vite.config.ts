// SPDX-License-Identifier: Apache-2.0
/**
 * Vite config for the heddle web client — feat-032.
 *
 * - Tailwind v4 plugin (`@tailwindcss/vite`) replaces the PostCSS
 *   pipeline entirely. No `postcss.config.js` and no
 *   `tailwind.config.js` should exist alongside this file.
 * - Dev server proxies `/api/*` (HTTP) and `/ws` (WebSocket) to the
 *   Node.js Fastify backend on `localhost:5174` per TECH.md T-026.
 *   In production the backend serves the bundled `dist/` directly so
 *   no proxy is needed.
 * - Loopback only (`127.0.0.1`); heddle is a single-user local app.
 */

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://localhost:5174",
        changeOrigin: false,
      },
      "/ws": {
        target: "ws://localhost:5174",
        ws: true,
        changeOrigin: false,
      },
    },
  },
});
