// SPDX-License-Identifier: Apache-2.0
/**
 * Minimal toast helper — feat-035.
 *
 * Inline error/info toasts so the kanban can surface drag rejections
 * (single-active violation, blocked-feature lock) without pulling in a
 * 50KB toast library. v0.1 has no need for stacking, animations, or
 * undo buttons; the full toast system is post-v0.1.
 *
 * Rendering: appends a fixed-position `<div>` to `document.body`,
 * removes it after `ttlMs`. A single toast at a time is enough for
 * the kanban's drag rules — overlapping messages would confuse more
 * than they help.
 */

const TOAST_TTL_MS = 3_000;

let currentToast: HTMLDivElement | null = null;
let currentTimer: ReturnType<typeof setTimeout> | null = null;

function clearCurrent(): void {
  if (currentTimer !== null) {
    clearTimeout(currentTimer);
    currentTimer = null;
  }
  if (currentToast !== null && currentToast.parentNode !== null) {
    currentToast.parentNode.removeChild(currentToast);
    currentToast = null;
  }
}

function showToast(message: string, tone: "error" | "info"): void {
  if (typeof document === "undefined") return; // SSR / vitest node env
  clearCurrent();
  const el = document.createElement("div");
  el.setAttribute("role", tone === "error" ? "alert" : "status");
  el.textContent = message;
  const bg = tone === "error" ? "bg-red-50 text-red-700" : "bg-surface-card text-ink";
  el.className =
    `fixed bottom-20 left-1/2 z-50 -translate-x-1/2 rounded-md border ` +
    `border-hairline-strong ${bg} px-4 py-2 text-sm shadow-lg`;
  document.body.appendChild(el);
  currentToast = el;
  currentTimer = setTimeout(clearCurrent, TOAST_TTL_MS);
}

export function toastError(message: string): void {
  showToast(message, "error");
}

export function toastInfo(message: string): void {
  showToast(message, "info");
}