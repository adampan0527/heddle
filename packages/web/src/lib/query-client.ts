// SPDX-License-Identifier: Apache-2.0
/**
 * TanStack Query client + fetch helper for the heddle web client.
 *
 * Single source of truth for the QueryClient singleton — instantiated
 * once in `main.tsx` and passed to `<QueryClientProvider>`. Components
 * import the hook factories from `./api/*.ts` rather than building
 * queries inline.
 *
 * `apiFetch` is the only place that touches `fetch`. It relies on the
 * Vite dev-server proxy (see `vite.config.ts`) to forward `/api/*` to
 * the Node.js backend on `localhost:5174`. In production the backend
 * serves the bundled `dist/` directly, so requests are same-origin.
 */

import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

export class ApiFetchError extends Error {
  public readonly status: number;
  public readonly bodyText: string;
  constructor(status: number, bodyText: string) {
    super(`api fetch failed (status=${status}): ${bodyText}`);
    this.name = "ApiFetchError";
    this.status = status;
    this.bodyText = bodyText;
  }
}

/**
 * Thin wrapper around `fetch` that:
 *   - defaults `Content-Type` to `application/json` for non-GET bodies
 *   - returns the raw `Response` so callers can inspect status
 *   - throws `ApiFetchError` on non-2xx so TanStack Query marks the
 *     query as failed (instead of returning an opaque Response)
 */
export async function apiFetch(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    throw new ApiFetchError(res.status, await res.text());
  }
  return res;
}
