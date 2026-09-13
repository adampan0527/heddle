// SPDX-License-Identifier: Apache-2.0
/**
 * Diagnosis helpers for the dialog transcript — feat-042.
 *
 * Two responsibilities:
 *
 *   1. `extractDiagnosis` — recognise a `DialogResponse` as carrying
 *      a structured diagnosis, whether via the typed
 *      `kind: "diagnose"` envelope or via a `## DIAGNOSIS\n` text
 *      marker. Pure function so it can be unit-tested in isolation.
 *
 *   2. `mockDiagnoseResponse` — synthesise a v0.1 typed diagnosis
 *      envelope for the `@feat-XXX diagnose` shortcut, until
 *      feat-043 lands a real daemon-side handler.
 *
 * The diff / marker parser lives here (not in `DiagnosisReport.tsx`)
 * because the parsing rules are independent of the rendering —
 * `DiagnosisReport.tsx` only knows how to lay out the three sections.
 */

import type { DialogResponse, DiagnoseResponse } from "@heddle/shared";

const DIAGNOSIS_HEADING = "## DIAGNOSIS";

/**
 * Extract a structured diagnosis from either:
 *
 *   1. A typed `DialogResponse` whose `kind === "diagnose"` and
 *      `diagnosis` is set.
 *   2. The same response whose `text` starts with the
 *      `## DIAGNOSIS\n` marker and whose body parses as a `cause` /
 *      `suggestion` / `diff?` block.
 *
 * Returns `null` when the response carries neither shape.
 */
export function extractDiagnosis(
  resp: Pick<DialogResponse, "kind" | "text" | "diagnosis">,
): { diagnosis: DiagnoseResponse; source: "typed" | "marker" } | null {
  if (resp.kind === "diagnose" && resp.diagnosis) {
    return { diagnosis: resp.diagnosis, source: "typed" };
  }
  if (resp.text.startsWith(DIAGNOSIS_HEADING)) {
    const parsed = parseMarkerBody(resp.text);
    if (parsed) return { diagnosis: parsed, source: "marker" };
  }
  return null;
}

/** True when a message is the v0.1 `@feat-XXX diagnose` shortcut. */
export function isDiagnoseShortcut(message: string): boolean {
  return /@feat-\d+\s+diagnose\b/i.test(message);
}

/** Extract the first `@feat-XXX` token from a message (or null). */
export function extractMentionedFeature(message: string): string | null {
  const m = /@(feat-\d+)/i.exec(message);
  return m ? (m[1] ?? null) : null;
}

/**
 * Parse a `## DIAGNOSIS\n...` block. Expected body:
 *
 *     ## DIAGNOSIS
 *     Cause: ...
 *     Suggestion: ...
 *     Diff:
 *     ```diff
 *     ...
 *     ```
 *
 * `Cause:` and `Suggestion:` are required; `Diff:` is optional.
 */
export function parseMarkerBody(text: string): DiagnoseResponse | null {
  const body = text.slice(DIAGNOSIS_HEADING.length).replace(/^\n+/, "");
  const lines = body.split("\n");
  const causeLine = lines.find((l) => l.startsWith("Cause:"));
  const suggestionLine = lines.find((l) => l.startsWith("Suggestion:"));
  if (!causeLine || !suggestionLine) return null;
  const cause = causeLine.slice("Cause:".length).trim();
  const suggestion = suggestionLine.slice("Suggestion:".length).trim();
  if (cause.length === 0 || suggestion.length === 0) return null;

  const diffStart = lines.findIndex((l) => l.startsWith("Diff:"));
  let diff: string | undefined;
  if (diffStart >= 0) {
    const tail = lines.slice(diffStart + 1);
    const codeFenceOpen = tail.findIndex((l) => l.startsWith("```"));
    if (codeFenceOpen >= 0) {
      const codeStart = diffStart + 1 + codeFenceOpen + 1;
      const codeEnd = lines.findIndex(
        (l, idx) => idx > codeStart && l.startsWith("```"),
      );
      const stopAt = codeEnd >= 0 ? codeEnd : lines.length;
      diff = lines.slice(codeStart, stopAt).join("\n");
    }
  }
  return diff !== undefined
    ? { cause, suggestion, diff }
    : { cause, suggestion };
}

/**
 * Build a v0.1 mock diagnose response. Used by the dialog component
 * (and tests) when the user types `@feat-XXX diagnose` but the daemon
 * hasn't shipped the real handler yet (feat-043 is still pending).
 */
export function mockDiagnoseResponse(
  featureId: string | null,
  message: string,
): DialogResponse {
  const cause =
    message.length > 0
      ? `Mock cause for ${featureId ?? "feature"}: the LLM guessed wrong about "${truncate(message, 60)}".`
      : `Mock cause for ${featureId ?? "feature"}: no context was provided.`;
  const suggestion =
    "Add the missing dependency on the dialog handler module before retrying.";
  const diff = [
    "--- a/packages/web/src/components/Dialog.tsx",
    "+++ b/packages/web/src/components/Dialog.tsx",
    "@@",
    "-import { useSubmitDialog } from '../lib/api/dialog.ts';",
    "+import { useSubmitDialog, useDiagnose } from '../lib/api/dialog.ts';",
  ].join("\n");
  return {
    project_id: "mock",
    kind: "diagnose",
    text: `${DIAGNOSIS_HEADING}\nCause: ${cause}\nSuggestion: ${suggestion}\nDiff:\n\`\`\`diff\n${diff}\n\`\`\``,
    diagnosis: { cause, suggestion, diff },
  };
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1) + "…";
}
