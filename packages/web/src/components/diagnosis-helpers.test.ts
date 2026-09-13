// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the diagnosis response parsers — feat-042.
 *
 * Coverage:
 *   - `extractDiagnosis` recognises both the typed envelope and the
 *     `## DIAGNOSIS\n` text marker, and returns `null` for neither.
 *   - `parseMarkerBody` tolerates missing Diff sections, requires both
 *     Cause and Suggestion, and extracts the diff code fence body.
 *   - `mockDiagnoseResponse` produces a usable v0.1 fixture.
 *   - `parseDialogCommand` (feat-043 / D-033) recognises the five
 *     `@feat-XXX <command>` shapes and returns a typed parsed object;
 *     it returns `null` for chat messages, partial mentions, or
 *     unknown verbs.
 */

import { describe, expect, test } from "vitest";

import {
  extractDiagnosis,
  mockDiagnoseResponse,
  parseDialogCommand,
  parseMarkerBody,
} from "./diagnosis-helpers.ts";

describe("extractDiagnosis", () => {
  test("returns the typed envelope when kind is 'diagnose'", () => {
    const resp = {
      kind: "diagnose" as const,
      text: "",
      diagnosis: { cause: "c", suggestion: "s" },
    };
    const result = extractDiagnosis(resp);
    expect(result).not.toBeNull();
    expect(result?.source).toBe("typed");
    expect(result?.diagnosis.cause).toBe("c");
  });

  test("parses a ## DIAGNOSIS marker when kind is not 'diagnose'", () => {
    const resp = {
      kind: "chat" as const,
      text:
        "## DIAGNOSIS\nCause: bad input\nSuggestion: add validation\n",
    };
    const result = extractDiagnosis(resp);
    expect(result).not.toBeNull();
    expect(result?.source).toBe("marker");
    expect(result?.diagnosis.cause).toBe("bad input");
    expect(result?.diagnosis.suggestion).toBe("add validation");
  });

  test("returns null when neither shape is present", () => {
    const resp = { kind: "chat" as const, text: "hello there" };
    expect(extractDiagnosis(resp)).toBeNull();
  });

  test("returns null when Cause or Suggestion is missing from the marker", () => {
    const resp = {
      kind: "chat" as const,
      text: "## DIAGNOSIS\nCause: only cause\n",
    };
    expect(extractDiagnosis(resp)).toBeNull();
  });
});

describe("parseMarkerBody", () => {
  test("captures diff from the fenced code block when present", () => {
    const text =
      "## DIAGNOSIS\nCause: c\nSuggestion: s\nDiff:\n```diff\n-line\n+line2\n```\n";
    expect(parseMarkerBody(text)).toEqual({
      cause: "c",
      suggestion: "s",
      diff: "-line\n+line2",
    });
  });

  test("omits diff when the marker has no Diff section", () => {
    expect(parseMarkerBody("## DIAGNOSIS\nCause: c\nSuggestion: s\n")).toEqual({
      cause: "c",
      suggestion: "s",
    });
  });

  test("returns null when Cause or Suggestion is missing", () => {
    expect(parseMarkerBody("## DIAGNOSIS\nSuggestion: only s\n")).toBeNull();
    expect(parseMarkerBody("## DIAGNOSIS\nCause: only c\n")).toBeNull();
  });
});

describe("mockDiagnoseResponse", () => {
  test("returns a typed envelope with a usable cause/suggestion/diff", () => {
    const resp = mockDiagnoseResponse("feat-042", "fix the wiring please");
    expect(resp.kind).toBe("diagnose");
    expect(resp.diagnosis?.cause).toContain("feat-042");
    expect(resp.diagnosis?.cause).toContain("fix the wiring please");
    expect(resp.diagnosis?.suggestion.length).toBeGreaterThan(0);
    expect(resp.diagnosis?.diff?.length ?? 0).toBeGreaterThan(0);
    expect(resp.text.startsWith("## DIAGNOSIS")).toBe(true);
  });
});

describe("parseDialogCommand (feat-043 / D-033)", () => {
  test("parses `@feat-003 diagnose`", () => {
    expect(parseDialogCommand("@feat-003 diagnose")).toEqual({
      kind: "command",
      featureId: "feat-003",
      command: "diagnose",
    });
  });

  test("parses `@feat-042 retry` (case + whitespace insensitive)", () => {
    expect(parseDialogCommand("@feat-042 Retry")).toEqual({
      kind: "command",
      featureId: "feat-042",
      command: "retry",
    });
    expect(parseDialogCommand("  @feat-042   retry   ")).toEqual({
      kind: "command",
      featureId: "feat-042",
      command: "retry",
    });
  });

  test("parses `@feat-042 retry-with-hint:use OpenAI`", () => {
    expect(
      parseDialogCommand("@feat-042 retry-with-hint:use OpenAI"),
    ).toEqual({
      kind: "command",
      featureId: "feat-042",
      command: "retry-with-hint",
      hint: "use OpenAI",
    });
  });

  test("parses `@feat-042 retry-with-hint:` with empty hint", () => {
    expect(parseDialogCommand("@feat-042 retry-with-hint:")).toEqual({
      kind: "command",
      featureId: "feat-042",
      command: "retry-with-hint",
      hint: "",
    });
  });

  test("trims leading whitespace inside the hint body", () => {
    expect(
      parseDialogCommand("@feat-042 retry-with-hint:    please retry"),
    ).toEqual({
      kind: "command",
      featureId: "feat-042",
      command: "retry-with-hint",
      hint: "please retry",
    });
  });

  test("parses `@feat-042 mark-done`", () => {
    expect(parseDialogCommand("@feat-042 mark-done")).toEqual({
      kind: "command",
      featureId: "feat-042",
      command: "mark-done",
    });
  });

  test("parses `@feat-042 abandon`", () => {
    expect(parseDialogCommand("@feat-042 abandon")).toEqual({
      kind: "command",
      featureId: "feat-042",
      command: "abandon",
    });
  });

  test("returns null for chat messages (no @feat-XXX token)", () => {
    expect(parseDialogCommand("please retry feat-042")).toBeNull();
    expect(parseDialogCommand("hello world")).toBeNull();
  });

  test("returns null for partial mentions", () => {
    expect(parseDialogCommand("@feat-042")).toBeNull();
    expect(parseDialogCommand("@feat-042 ")).toBeNull();
  });

  test("returns null for unknown verbs", () => {
    expect(parseDialogCommand("@feat-042 ship-it")).toBeNull();
    expect(parseDialogCommand("@feat-042 delete")).toBeNull();
  });

  test("returns null when the message has trailing prose", () => {
    // The parser is strict — chat-after-command is treated as chat.
    expect(parseDialogCommand("@feat-042 retry now")).toBeNull();
    expect(
      parseDialogCommand("@feat-042 mark-done please"),
    ).toBeNull();
  });
});
