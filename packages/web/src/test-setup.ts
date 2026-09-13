// SPDX-License-Identifier: Apache-2.0
import "@testing-library/jest-dom/vitest";

// ReactFlow (feat-041) uses ResizeObserver + DOMRect.fromRect on mount.
// jsdom doesn't provide either, so we stub them out — the component
// only relies on the constructor + disconnect, never on actual size
// observation (the panel never tries to fitView to a real rect in
// tests; we only assert the chrome and click handlers).
if (typeof globalThis.ResizeObserver === "undefined") {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}
if (
  typeof DOMRect !== "undefined" &&
  typeof DOMRect.fromRect !== "function"
) {
  DOMRect.fromRect = (rect?: { width?: number; height?: number }) =>
    new DOMRect(0, 0, rect?.width ?? 0, rect?.height ?? 0);
}
