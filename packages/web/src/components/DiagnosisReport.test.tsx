// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the structured diagnosis card — feat-042.
 *
 * Coverage:
 *
 *   1. **Render**: the card renders all three sections (cause,
 *      suggestion, diff) when `diff` is present.
 *   2. **Conditional diff**: when `diff` is omitted, the diff section
 *      is not rendered at all.
 *   3. **Feature id subtitle**: the optional `featureId` shows up as
 *      a subtitle, and is hidden when omitted.
 *   4. **Apply button**: clicking Apply calls the supplied
 *      `onApply` callback with the diagnosis; when omitted, the
 *      button still renders (defaults to no-op).
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { DiagnosisReport } from "./DiagnosisReport.tsx";

function makeDiagnosis(overrides: Partial<{ cause: string; suggestion: string; diff?: string }> = {}) {
  return {
    cause: "The handler was missing a guard for empty input.",
    suggestion: "Add a non-empty check before invoking the LLM.",
    diff: [
      "--- a/foo.ts",
      "+++ b/foo.ts",
      "@@",
      "-if (msg) call(msg);",
      "+if (msg && msg.trim()) call(msg);",
    ].join("\n"),
    ...overrides,
  };
}

describe("<DiagnosisReport /> rendering", () => {
  test("renders the card with cause and suggestion sections", () => {
    render(<DiagnosisReport diagnosis={makeDiagnosis()} />);
    expect(screen.getByTestId("diagnosis-report")).toBeInTheDocument();
    expect(screen.getByTestId("diagnosis-title")).toHaveTextContent(/diagnosis/i);
    expect(screen.getByTestId("diagnosis-cause")).toHaveTextContent(
      /missing a guard for empty input/i,
    );
    expect(screen.getByTestId("diagnosis-suggestion")).toHaveTextContent(
      /non-empty check before invoking/i,
    );
  });

  test("renders the diff section when diagnosis.diff is present", () => {
    render(<DiagnosisReport diagnosis={makeDiagnosis()} />);
    expect(screen.getByTestId("diagnosis-diff-section")).toBeInTheDocument();
    expect(screen.getByTestId("diagnosis-diff")).toBeInTheDocument();
    expect(screen.getByTestId("diagnosis-apply")).toBeInTheDocument();
  });

  test("renders diff lines with +/- highlighting classes", () => {
    render(<DiagnosisReport diagnosis={makeDiagnosis()} />);
    const diff = screen.getByTestId("diagnosis-diff");
    // The added line starts with `+` and the removed line with `-`.
    expect(diff.textContent).toContain("+if (msg && msg.trim()) call(msg);");
    expect(diff.textContent).toContain("-if (msg) call(msg);");
  });

  test("hides the diff section when diagnosis.diff is omitted", () => {
    const diag = {
      cause: "The handler was missing a guard for empty input.",
      suggestion: "Add a non-empty check before invoking the LLM.",
    };
    render(<DiagnosisReport diagnosis={diag} />);
    expect(screen.queryByTestId("diagnosis-diff-section")).toBeNull();
    expect(screen.queryByTestId("diagnosis-apply")).toBeNull();
    // The cause and suggestion sections still render.
    expect(screen.getByTestId("diagnosis-cause")).toBeInTheDocument();
    expect(screen.getByTestId("diagnosis-suggestion")).toBeInTheDocument();
  });
});

describe("<DiagnosisReport /> feature id subtitle", () => {
  test("shows the feature id when supplied", () => {
    render(<DiagnosisReport diagnosis={makeDiagnosis()} featureId="feat-042" />);
    expect(screen.getByTestId("diagnosis-feature-id")).toHaveTextContent("feat-042");
  });

  test("hides the feature id subtitle when omitted", () => {
    render(<DiagnosisReport diagnosis={makeDiagnosis()} />);
    expect(screen.queryByTestId("diagnosis-feature-id")).toBeNull();
  });
});

describe("<DiagnosisReport /> apply button", () => {
  test("invokes onApply with the diagnosis when Apply is clicked", () => {
    const onApply = vi.fn();
    const diag = makeDiagnosis();
    render(<DiagnosisReport diagnosis={diag} onApply={onApply} />);
    fireEvent.click(screen.getByTestId("diagnosis-apply"));
    expect(onApply).toHaveBeenCalledTimes(1);
    expect(onApply).toHaveBeenCalledWith(diag);
  });

  test("renders Apply with no-op handler when onApply is omitted", () => {
    render(<DiagnosisReport diagnosis={makeDiagnosis()} />);
    const btn = screen.getByTestId("diagnosis-apply");
    expect(btn).toBeInTheDocument();
    // Should not throw when clicked.
    fireEvent.click(btn);
  });
});
