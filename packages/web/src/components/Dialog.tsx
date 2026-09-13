// SPDX-License-Identifier: Apache-2.0
/**
 * Persistent bottom dialog (feat-038 / D-014).
 *
 * Always-visible input strip at the bottom of the kanban board. The
 * user types a natural-language message (chat reply, feature request,
 * `@feat-XXX diagnose`, etc.) and presses Enter / clicks Send; the
 * message is POSTed to `/api/projects/:id/dialog` via `useSubmitDialog`
 * and the daemon's `{ kind, text, drafts? }` response is appended to a
 * scrollable transcript area above the textarea.
 *
 * Out of scope for this feature:
 *
 *   - `@feat-XXX` autocomplete → feat-039 (now wired into this component)
 *   - Draft cards tray + "Confirm all" → feat-040
 *   - Streaming token-by-token render → wired in a later feature
 *     (feat-029 already emits `project.dialog_response` over WS;
 *     v0.1 just awaits the full POST response, which is acceptable
 *     for the v0.1 chat stub — feat-044 will swap in classification
 *     and a real LLM call)
 *
 * The component is intentionally framework-only (no @dnd-kit, no
 * project-specific state) so it can be unit-tested in isolation with
 * a mocked `useSubmitDialog`.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { DialogResponse, Feature } from "@heddle/shared";

import { useSubmitDialog } from "../lib/api/dialog.ts";
import { useFeatures } from "../lib/api/features.ts";
import { useUiStore } from "../lib/state/ui-store.ts";
import {
  MentionAutocomplete,
  clampSelection,
  extractMention,
  filterFeatures,
  insertMention,
  MAX_CANDIDATES,
  type MentionContext,
} from "./MentionAutocomplete.tsx";

interface DialogProps {
  projectId: string | null;
}

interface TranscriptEntry {
  /** Monotonic id used as the React key. */
  id: number;
  /** Who produced the entry — "user" types into the textarea, "assistant"
   *  is whatever the daemon returned in the most recent response. */
  role: "user" | "assistant" | "error";
  text: string;
  /** When the entry was appended; used for stable ordering. */
  at: number;
}

const PLACEHOLDER =
  "Ask, request a feature, or use @feat-XXX to act on an existing one.";

/** Bound the textarea before submit; matches the server schema. */
const MAX_MESSAGE_CHARS = 8000;

function nextId(): number {
  // Monotonic id generator that survives StrictMode double-invocation;
  // a module-level counter is fine here because the transcript is
  // ephemeral component state, not persisted across remounts.
  return ++idCounter;
}
let idCounter = 0;

export function Dialog({ projectId }: DialogProps): React.ReactElement {
  const submit = useSubmitDialog(projectId);
  const featuresQuery = useFeatures(projectId);
  const setDrafts = useUiStore((s) => s.setDrafts);
  const [draft, setDraft] = useState<string>("");
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // Mention-dropdown state. `mention` is the parsed @-token at the
  // cursor (null when no @ is being typed); `mentionSelected` is the
  // currently-highlighted row index in the filtered list.
  const [mention, setMention] = useState<MentionContext | null>(null);
  const [mentionSelected, setMentionSelected] = useState<number>(0);

  // Auto-scroll the transcript to the bottom when a new entry lands.
  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [transcript]);

  function appendEntry(entry: Omit<TranscriptEntry, "id" | "at">): void {
    setTranscript((prev) => [
      ...prev,
      { id: nextId(), at: Date.now(), ...entry },
    ]);
  }

  // Derived: filtered candidate list for the current mention query.
  const allFeatures = featuresQuery.data ?? [];
  const matchedAll = useMemo<Feature[]>(
    () => (mention ? filterFeatures(allFeatures, mention.query) : []),
    [allFeatures, mention],
  );
  const candidates = useMemo<Feature[]>(
    () => matchedAll.slice(0, MAX_CANDIDATES),
    [matchedAll],
  );
  // Keep `mentionSelected` inside the new candidate range whenever the
  // candidate list changes shape (backspace / further typing).
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
    // Restore focus + cursor after the dropdown swallows focus on click.
    // requestAnimationFrame waits for React to flush the new `value`
    // before we move the caret.
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(cursor, cursor);
    });
  }

  async function handleSubmit(): Promise<void> {
    const message = draft.trim();
    if (message.length === 0) return;
    if (message.length > MAX_MESSAGE_CHARS) return;
    if (!projectId) {
      appendEntry({
        role: "error",
        text: "Select a project before sending a dialog message.",
      });
      return;
    }
    appendEntry({ role: "user", text: message });
    setDraft("");
    try {
      const resp: DialogResponse = await submit.mutateAsync({ message });
      appendEntry({ role: "assistant", text: resp.text });
      // feat-040: when the daemon's classifier returns work-mode
      // draft cards, refresh the tray. The user's previous selection
      // is dropped (a new decomposition round supersedes it); cards
      // can be re-toggled by id on the tray.
      if (resp.kind === "work" && Array.isArray(resp.drafts)) {
        setDrafts(resp.drafts);
      }
    } catch (err) {
      appendEntry({
        role: "error",
        text: err instanceof Error ? err.message : String(err),
      });
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>): void {
    // While the mention dropdown is open, intercept nav + insert keys.
    if (mention && candidates.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMentionSelected((s) =>
          clampSelection(s + 1, candidates.length),
        );
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
    // Enter submits; Shift+Enter inserts a newline (textarea convention).
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSubmit();
    }
  }

  const sending = submit.isPending;
  const canSend =
    !sending && draft.trim().length > 0 && projectId !== null;
  const showMention = mention !== null && featuresQuery.isSuccess;

  return (
    <section
      aria-label="Dialog"
      data-testid="dialog"
      className="flex shrink-0 flex-col gap-2 rounded border border-zinc-800 bg-zinc-900 p-3"
    >
      <div
        ref={transcriptRef}
        aria-label="Dialog transcript"
        data-testid="dialog-transcript"
        className="h-40 overflow-y-auto rounded border border-zinc-800 bg-zinc-950 p-2 text-sm"
      >
        {transcript.length === 0 ? (
          <p className="text-zinc-500" data-testid="dialog-empty">
            No messages yet.
          </p>
        ) : (
          transcript.map((entry) => (
            <div
              key={entry.id}
              data-testid={`dialog-entry-${entry.role}`}
              className={
                entry.role === "user"
                  ? "text-zinc-100"
                  : entry.role === "assistant"
                    ? "text-blue-300"
                    : "text-red-400"
              }
            >
              <span className="mr-2 text-xs uppercase text-zinc-500">
                {entry.role}
              </span>
              {entry.text}
            </div>
          ))
        )}
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
            maxLength={MAX_MESSAGE_CHARS}
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
            className="w-full resize-none rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-sm text-zinc-100 placeholder:text-zinc-500 focus:border-blue-500 focus:outline-none disabled:opacity-50"
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
            void handleSubmit();
          }}
          className="shrink-0 rounded border border-zinc-700 bg-blue-600 px-3 py-1 text-sm font-medium text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {sending ? "Sending…" : "Send"}
        </button>
      </div>
    </section>
  );
}