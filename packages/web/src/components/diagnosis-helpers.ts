// SPDX-License-Identifier: Apache-2.0
/**
 * Diagnosis + dialog-command helpers (feat-042 / feat-043).
 *
 * Three responsibilities:
 *
 *   1. `extractDiagnosis` — recognise a `DialogResponse` as carrying
 *      a structured diagnosis, whether via the typed
 *      `kind: "diagnose"` envelope or via a `## DIAGNOSIS\n` text
 *      marker. Pure function so it can be unit-tested in isolation.
 *
 *   2. `mockDiagnoseResponse` — synthesise a v0.1 typed diagnosis
 *      envelope for the `@feat-XXX diagnose` shortcut, until
 *      the daemon-side handler ships.
 *
 *   3. `parseDialogCommand` (feat-043, D-033) — recognise an
 *      `@feat-XXX <command>` shape and route it to one of
 *      `diagnose` / `retry` / `retry-with-hint:<text>` /
 *      `mark-done` / `abandon`. Returns a typed parsed object so
 *      the dialog submit hook can dispatch without re-running
 *      the regexes.
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

// ---------------------------------------------------------------------------
// feat-054 / D-054: post-confirm feature modification dialog commands.
//
// Recognised verbs (additive to feat-043 / D-033 above):
//   - split into <N> parts          → POST /features/:fid/split
//   - merge with @feat-YYY          → POST /features/merge
//   - rename to <new title>         → PATCH /features/:fid (title)
//   - set priority <high|medium|low>→ PATCH /features/:fid/priority
//   - depend on @feat-YYY           → POST /features/:fid/deps (add)
//   - remove dep @feat-YYY          → POST /features/:fid/deps (remove)
//
// Split / merge / remove-dep are classified as destructive — the
// dialog UI shows a confirmation card with the diff before the
// mutation runs. The parser only recognises the *shape*; whether
// the verb is destructive lives in `DESTRUCTIVE_COMMANDS` below so
// the dispatch helper can read it without re-parsing.
// ---------------------------------------------------------------------------

export type DialogCommandKind =
  | "diagnose"
  | "retry"
  | "retry-with-hint"
  | "mark-done"
  | "abandon"
  | "split"
  | "merge"
  | "rename"
  | "set-priority"
  | "add-dep"
  | "remove-dep";

/** Subset of `DialogCommandKind` values that are destructive (D-054). */
export const DESTRUCTIVE_COMMANDS: ReadonlySet<DialogCommandKind> =
  new Set<DialogCommandKind>([
    "split",
    "merge",
    "remove-dep",
  ]);

export interface DialogCommand {
  kind: "command";
  featureId: string;
  command: DialogCommandKind;
  /** Populated only for `retry-with-hint`. The hint text after the colon,
   *  trimmed of leading whitespace. Empty string is preserved so the UI
   *  can warn "hint is empty" if it wants to. */
  hint?: string;
  /** Populated for split / merge / rename / set-priority /
   *  add-dep / remove-dep. Each command has its own payload shape
   *  (see `DialogCommandPayloads`). Empty string is preserved so the
   *  UI can warn on missing required text. */
  payload?: string;
  /** Set of feature ids mentioned in the command (after the primary
   *  `@feat-XXX` token). For `merge` / `add-dep` / `remove-dep` this
   *  is the list of sibling ids the operation references. */
  mentions?: string[];
}

const COMMAND_REGEX =
  /^@feat-(\d+)\s+(diagnose|retry|retry-with-hint(?:\s*:.*)?|mark-done|abandon)\s*$/i;

/** Captures the @feat-XXX token, leaving the rest as `tail`. */
const MOD_REGEX = /^@feat-(\d+)\s+(.+)$/i;

/**
 * Extract every `@feat-XXX` mention from a string. Case-insensitive;
   * duplicates are deduplicated while preserving first-seen order.
 * Used by the merge / add-dep / remove-dep verbs to know which
 * sibling features the user is referencing.
 */
export function extractMentions(text: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const re = /@(feat-\d+)/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const raw = m[1];
    if (typeof raw !== "string") continue;
    const id = raw.toLowerCase();
    if (!seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  }
  return out;
}

/** Parse a split command tail: "split into <N> parts". */
const SPLIT_REGEX = /^split\s+into\s+(\d+)\s+parts?\s*$/i;

/** Parse a merge command tail: "merge with @feat-YYY[, @feat-ZZZ]*". */
const MERGE_REGEX = /^merge\s+with\s+(@?(?:feat-\d+\s*,?\s*)+)\s*$/i;

/** Parse a rename command tail: "rename to <new title>". */
const RENAME_REGEX = /^rename\s+to\s+(.+)$/i;

/** Parse a priority command tail: "set priority (high|medium|low)". */
const PRIORITY_REGEX = /^set\s+priority\s+(high|medium|low)\s*$/i;

/** Parse a "depend on @feat-YYY" command tail. */
const DEPEND_REGEX = /^depend\s+on\s+(@feat-\d+)\s*$/i;

/** Parse a "remove dep @feat-YYY" command tail. */
const REMOVE_DEP_REGEX = /^remove\s+dep\s+(@feat-\d+)\s*$/i;

/**
 * Parse a dialog message for the `@feat-XXX <command>` shape (D-033
 * + D-054). Recognises the five feat-043 verbs AND the six feat-054
 * modification verbs.
 *
 * Returns a typed `DialogCommand` when the entire message matches
 * one of the eleven recognised verbs; `null` otherwise. The intent
 * is for callers to short-circuit the regular chat submission
 * path; this is intentionally strict — partial matches fall
 * through to chat.
 */
export function parseDialogCommand(message: string): DialogCommand | null {
  const trimmed = message.trim();

  // First: the original feat-043 regex (5 verbs). Keep that path
  // exactly as-is so existing tests don't drift.
  const m = COMMAND_REGEX.exec(trimmed);
  if (m) {
    const featureIdRaw = m[1];
    const verbRaw = m[2];
    if (typeof featureIdRaw !== "string" || typeof verbRaw !== "string") {
      return null;
    }
    const featureId = `feat-${featureIdRaw}`;
    const verbLower = verbRaw.toLowerCase();
    if (verbLower.startsWith("retry-with-hint")) {
      const colonIdx = verbRaw.indexOf(":");
      if (colonIdx < 0) {
        return { kind: "command", featureId, command: "retry" };
      }
      const hint = verbRaw.slice(colonIdx + 1).trim();
      return {
        kind: "command",
        featureId,
        command: "retry-with-hint",
        hint,
      };
    }
    return {
      kind: "command",
      featureId,
      command: verbLower as DialogCommandKind,
    };
  }

  // Second: the feat-054 modification verbs. Each is a permissive
  // tail-match on `@feat-XXX <verb-specific tail>`.
  const modMatch = MOD_REGEX.exec(trimmed);
  if (!modMatch) return null;
  const featureIdRaw = modMatch[1];
  const tailRaw = modMatch[2];
  if (typeof featureIdRaw !== "string" || typeof tailRaw !== "string") {
    return null;
  }
  const featureId = `feat-${featureIdRaw.toLowerCase()}`;
  const tail = tailRaw.trim();

  const splitMatch = SPLIT_REGEX.exec(tail);
  if (splitMatch && typeof splitMatch[1] === "string") {
    return {
      kind: "command",
      featureId,
      command: "split",
      payload: splitMatch[1],
    };
  }
  const mergeMatch = MERGE_REGEX.exec(tail);
  if (mergeMatch && typeof mergeMatch[1] === "string") {
    const mentions = extractMentions(mergeMatch[1]);
    return {
      kind: "command",
      featureId,
      command: "merge",
      mentions,
    };
  }
  const renameMatch = RENAME_REGEX.exec(tail);
  if (renameMatch && typeof renameMatch[1] === "string") {
    return {
      kind: "command",
      featureId,
      command: "rename",
      payload: renameMatch[1].trim(),
    };
  }
  const prioMatch = PRIORITY_REGEX.exec(tail);
  if (prioMatch && typeof prioMatch[1] === "string") {
    return {
      kind: "command",
      featureId,
      command: "set-priority",
      payload: prioMatch[1].toLowerCase(),
    };
  }
  const dependMatch = DEPEND_REGEX.exec(tail);
  if (dependMatch && typeof dependMatch[1] === "string") {
    const mentions = extractMentions(dependMatch[1]);
    return {
      kind: "command",
      featureId,
      command: "add-dep",
      mentions,
    };
  }
  const removeMatch = REMOVE_DEP_REGEX.exec(tail);
  if (removeMatch && typeof removeMatch[1] === "string") {
    const mentions = extractMentions(removeMatch[1]);
    return {
      kind: "command",
      featureId,
      command: "remove-dep",
      mentions,
    };
  }
  return null;
}

/**
 * True when the command is destructive (split, merge, remove-dep).
 * The dialog UI uses this to render a confirmation card with the
 * diff before invoking the mutation.
 */
export function isDestructiveCommand(cmd: DialogCommandKind): boolean {
  return DESTRUCTIVE_COMMANDS.has(cmd);
}
