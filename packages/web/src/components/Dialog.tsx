// SPDX-License-Identifier: Apache-2.0
/**
 * Persistent bottom dialog (feat-038 / D-014).
 *
 * Always-visible input strip at the bottom of the kanban board.
 * `useDialogSubmit` posts the message to `/api/projects/:id/dialog`;
 * the response lands in the scrollable transcript above. feat-039
 * wires `@feat-XXX` autocomplete, feat-040 routes `kind: "work"`
 * drafts to the tray, feat-042 routes diagnose responses into
 * <DiagnosisReport>.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { Feature } from "@heddle/shared";

import { useFeatures } from "../lib/api/features.ts";
import {
  MentionAutocomplete,
  clampSelection,
  extractMention,
  filterFeatures,
  insertMention,
  MAX_CANDIDATES,
  type MentionContext,
} from "./MentionAutocomplete.tsx";
import { TranscriptList } from "./TranscriptList.tsx";
import {
  DIALOG_MAX_MESSAGE_CHARS,
  useDialogSubmit,
} from "./useDialogSubmit.ts";
import {
  makeTranscriptEntry,
  type NewTranscriptEntry,
  type TranscriptEntry,
} from "./transcript-model.ts";

interface DialogProps {
  projectId: string | null;
}

const PLACEHOLDER =
  "Ask, request a feature, or use @feat-XXX to act on an existing one.";

export function Dialog({ projectId }: DialogProps): React.ReactElement {
  const [draft, setDraft] = useState<string>("");
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [mention, setMention] = useState<MentionContext | null>(null);
  const [mentionSelected, setMentionSelected] = useState<number>(0);

  const appendEntry = (entry: NewTranscriptEntry): void => {
    setTranscript((prev) => [...prev, makeTranscriptEntry(entry)]);
  };
  const clearDraft = (): void => {
    setDraft("");
  };
  const { submit, sending } = useDialogSubmit({
    projectId,
    appendEntry,
    clearDraft,
  });

  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [transcript]);

  const featuresQuery = useFeatures(projectId);
  const allFeatures = featuresQuery.data ?? [];
  const matchedAll = useMemo<Feature[]>(
    () => (mention ? filterFeatures(allFeatures, mention.query) : []),
    [allFeatures, mention],
  );
  const candidates = useMemo<Feature[]>(
    () => matchedAll.slice(0, MAX_CANDIDATES),
    [matchedAll],
  );
  useEffect(() => {
    setMentionSelected((prev) => clampSelection(prev, candidates.length));
  }, [candidates.length]);

  function applyMention(feature: Feature): void {
    if (!mention) return;
    const el = textareaRef.current;
    if (!el) return;
    const { content, cursor } = insertMention(draft, mention, feature);
    setDraft(content);
    setMention(null);
    setMentionSelected(0);
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(cursor, cursor);
    });
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>): void {
    if (mention && candidates.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMentionSelected((s) => clampSelection(s + 1, candidates.length));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMentionSelected((s) => clampSelection(s - 1, candidates.length));
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        const picked = candidates[mentionSelected];
        if (picked) applyMention(picked);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMention(null);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit(draft);
    }
  }

  const canSend =
    !sending && draft.trim().length > 0 && projectId !== null;
  const showMention = mention !== null && featuresQuery.isSuccess;

  return (
    <section
      aria-label="Dialog"
      data-testid="dialog"
      className="flex shrink-0 flex-col gap-2 rounded-md border border-hairline-strong bg-surface-card p-3"
    >
      <div
        ref={transcriptRef}
        aria-label="Dialog transcript"
        data-testid="dialog-transcript"
        className="h-40 overflow-y-auto rounded-md border border-hairline bg-canvas p-2 text-sm"
      >
        <TranscriptList entries={transcript} />
      </div>
      <div className="flex items-end gap-2">
        <label className="sr-only" htmlFor="dialog-input">
          Dialog message
        </label>
        <div className="relative flex-1">
          <textarea
            id="dialog-input"
            ref={textareaRef}
            data-testid="dialog-textarea"
            aria-label="Dialog message"
            placeholder={projectId ? PLACEHOLDER : "Select a project to chat"}
            rows={2}
            maxLength={DIALOG_MAX_MESSAGE_CHARS}
            value={draft}
            disabled={!projectId || sending}
            onChange={(e) => {
              const value = e.target.value;
              const cursor = e.target.selectionStart ?? value.length;
              setDraft(value);
              setMention(extractMention(value, cursor));
              setMentionSelected(0);
            }}
            onKeyDown={handleKeyDown}
            className="w-full resize-none rounded-full border border-hairline-strong bg-surface-card px-4 py-2 text-sm text-ink placeholder:text-mute focus:border-primary focus:outline-none disabled:opacity-50"
          />
          {showMention ? (
            <MentionAutocomplete
              candidates={candidates}
              selected={mentionSelected}
              totalMatches={matchedAll.length}
              onHover={setMentionSelected}
              onSelect={applyMention}
            />
          ) : null}
        </div>
        <button
          type="button"
          data-testid="dialog-send"
          aria-label="Send dialog message"
          disabled={!canSend}
          onClick={() => {
            void submit(draft);
          }}
          className="shrink-0 rounded-full bg-primary px-4 py-2 text-sm font-semibold text-on-primary hover:bg-primary-deep disabled:cursor-not-allowed disabled:opacity-50"
        >
          {sending ? "Sending…" : "Send"}
        </button>
      </div>
    </section>
  );
}
