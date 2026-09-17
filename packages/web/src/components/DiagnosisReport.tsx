// SPDX-License-Identifier: Apache-2.0
/**
 * Structured diagnosis card — feat-042 (D-035).
 *
 * Renders a `DiagnoseResponse` ({ cause, suggestion, diff? }) as a
 * three-section card. When `diff` is present, the diff section shows
 * a before/after side-by-side and an "Apply" button that, in a later
 * feature (feat-043), triggers retry-with-hint. Today the Apply
 * button is wired to the supplied `onApply` callback so callers can
 * stub it for v0.1 testing.
 *
 * The card is a presentational component — it owns no network state
 * and no global state. The parent (`Dialog`) decides when to mount
 * it (when the daemon returns `kind: "diagnose"`) and provides the
 * `onApply` callback (which defaults to a no-op so the component is
 * safe to drop in anywhere).
 *
 * Out of scope for this feature:
 *   - Streaming the diff token-by-token (feat-048 will plumb a
 *     structured event log; today the full diff is delivered in one
 *     POST response).
 *   - Real "Apply" wiring — feat-043 will replace the no-op callback
 *     with `useRetryWithHint(featId)` once that hook exists.
 */

import type { DiagnoseResponse } from "@heddle/shared";

export interface DiagnosisReportProps {
  /** The structured diagnosis payload returned by the daemon. */
  diagnosis: DiagnoseResponse;
  /**
   * Optional id of the feature this diagnosis is about — surfaces as
   * the card subtitle so users can tell which @feat-XXX the report
   * belongs to. When omitted (e.g. the daemon returned a generic
   * diagnosis), the subtitle is hidden.
   */
  featureId?: string;
  /** Called when the user clicks "Apply". Defaults to a no-op so the
   *  component is safe to render before feat-043 lands. */
  onApply?: (diagnosis: DiagnoseResponse) => void;
}

export function DiagnosisReport({
  diagnosis,
  featureId,
  onApply,
}: DiagnosisReportProps): React.ReactElement {
  const hasDiff = typeof diagnosis.diff === "string" && diagnosis.diff.length > 0;
  const handleApply = (): void => {
    if (onApply) onApply(diagnosis);
  };
  return (
    <article
      aria-label="Diagnosis report"
      data-testid="diagnosis-report"
      className="flex flex-col gap-3 rounded-md border border-purple-300 bg-surface-card p-3 text-sm"
    >
      <header className="flex items-baseline justify-between">
        <h3 className="font-semibold text-purple-900" data-testid="diagnosis-title">
          Diagnosis
        </h3>
        {featureId ? (
          <span
            className="font-code text-xs text-purple-700"
            data-testid="diagnosis-feature-id"
          >
            {featureId}
          </span>
        ) : null}
      </header>

      <DiagnosisSection
        label="Cause"
        testId="diagnosis-cause"
        tone="rose"
        text={diagnosis.cause}
      />

      <DiagnosisSection
        label="Suggestion"
        testId="diagnosis-suggestion"
        tone="amber"
        text={diagnosis.suggestion}
      />

      {hasDiff ? (
        <section
          aria-label="Proposed diff"
          data-testid="diagnosis-diff-section"
          className="flex flex-col gap-2 rounded-md border border-hairline bg-canvas p-2"
        >
          <header className="flex items-center justify-between">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-purple-700">
              Proposed diff
            </h4>
            <button
              type="button"
              data-testid="diagnosis-apply"
              aria-label="Apply proposed diff"
              onClick={handleApply}
              className="rounded-full border border-primary bg-primary px-3 py-0.5 text-xs font-semibold text-on-primary hover:bg-primary-deep"
            >
              Apply
            </button>
          </header>
          <DiffView diff={diagnosis.diff ?? ""} />
        </section>
      ) : null}
    </article>
  );
}

interface DiagnosisSectionProps {
  label: string;
  testId: string;
  tone: "rose" | "amber";
  text: string;
}

function DiagnosisSection({
  label,
  testId,
  tone,
  text,
}: DiagnosisSectionProps): React.ReactElement {
  const toneClasses =
    tone === "rose"
      ? "border-red-700 bg-red-50 text-red-900"
      : "border-amber-700 bg-amber-50 text-amber-900";
  return (
    <section
      aria-label={label}
      data-testid={testId}
      className={`flex flex-col gap-1 rounded border p-2 ${toneClasses}`}
    >
      <h4 className="text-xs font-semibold uppercase tracking-wide">{label}</h4>
      <p className="whitespace-pre-wrap text-sm">{text}</p>
    </section>
  );
}

interface DiffViewProps {
  diff: string;
}

/**
 * Tiny diff renderer. We don't pull in a heavy `diff` library for v0.1
 * — the daemon's diff strings are expected to be small hunks the user
 * can eyeball, so we split on `@@` markers (if any) and render each
 * hunk as a pre-formatted block. Lines starting with `-` render red,
 * `+` green, everything else muted.
 */
function DiffView({ diff }: DiffViewProps): React.ReactElement {
  const lines = diff.split("\n");
  return (
    <pre
      data-testid="diagnosis-diff"
      className="overflow-x-auto rounded bg-canvas p-2 font-code text-xs leading-relaxed"
    >
      {lines.map((line, idx) => {
        const trimmed = line;
        const first = trimmed.charAt(0);
        const cls =
          first === "-"
            ? "text-red-700"
            : first === "+"
              ? "text-badge-success"
              : "text-charcoal";
        return (
          <span key={idx} className={`block ${cls}`}>
            {line}
          </span>
        );
      })}
    </pre>
  );
}
