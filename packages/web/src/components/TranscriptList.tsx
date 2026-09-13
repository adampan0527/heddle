// SPDX-License-Identifier: Apache-2.0
/**
 * Transcript render loop for the dialog — feat-038 + feat-042.
 *
 * Extracted from Dialog.tsx so the entry mapping can be reasoned about
 * (and visually tested) without dragging in the textarea + mention
 * state. Pure presentational: takes entries, returns React nodes.
 */

import { DiagnosisReport } from "./DiagnosisReport.tsx";
import type { TranscriptEntry } from "./transcript-model.ts";

export function TranscriptList({
  entries,
}: {
  entries: readonly TranscriptEntry[];
}): React.ReactElement {
  if (entries.length === 0) {
    return (
      <p className="text-zinc-500" data-testid="dialog-empty">
        No messages yet.
      </p>
    );
  }
  return (
    <>
      {entries.map((entry) => (
        <TranscriptRow key={entry.id} entry={entry} />
      ))}
    </>
  );
}

function TranscriptRow({ entry }: { entry: TranscriptEntry }): React.ReactElement {
  const roleClass =
    entry.role === "user"
      ? "text-zinc-100"
      : entry.role === "assistant"
        ? "text-blue-300"
        : "text-red-400";
  return (
    <div
      data-testid={`dialog-entry-${entry.role}`}
      className={roleClass}
    >
      <span className="mr-2 text-xs uppercase text-zinc-500">{entry.role}</span>
      {entry.diagnosis ? (
        <DiagnosisReport
          diagnosis={entry.diagnosis}
          {...(entry.diagnosisFeatureId !== undefined
            ? { featureId: entry.diagnosisFeatureId }
            : {})}
        />
      ) : (
        entry.text
      )}
    </div>
  );
}
