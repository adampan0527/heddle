# Progress Session Block Template

The authoritative definitions live in `tools/_constants.py`; this file only
shows the rendered result.

This file is the **single source of truth** for the "every session must append a
SESSION block to `current_progress.txt`" rule used by the long-running agent
harness.

`HARNESS.md` and `CODING_AGENT_PROMPT.md` MUST reference this template rather
than maintaining their own copy. If this template changes, update those two
files' reference blocks and notify any running agents to restart their session.

## When to use

At the **end of every coding session**, append **one** SESSION block to
`current_progress.txt`. Do not rewrite earlier SESSION blocks — the file is an
append-only history. (This file used to be split into a separate Initializer seed file and the active append target; they have been merged — SESSION 0 lives at the top of `current_progress.txt`.)

## Template (copy exactly)

```
### SESSION <N> — <ISO date>

**Worked on:** <feature_id> @ <commit_sha> — <one-line declarative summary of what this session did>

**Status:** <one rendered label listed under Status values below>

**Features now passing:** <comma-separated list of feature_ids> (<X> / <Y> total)

**Notes:** <free text, max 500 chars; write "(none)" if there is nothing to record>

---
```

## Status values

The following five Status values correspond to `SESSION_STATUSES` /
`SESSION_STATUS_LABELS` in `tools/_constants.py`:

- (`implemented_and_tested`) → "Implemented and tested"
- (`implemented_not_tested`) → "Implemented, not yet tested"
- (`in_progress_blocked`) → "In progress, blocked"
- (`deferred`) → "Deferred"
- (`no_op`) → "No-op"

## Field rules

- `### SESSION <N> — <ISO date>` — N is the 1-indexed session counter for
  Coding Agent sessions (Initializer session is `SESSION 0`). ISO date format
  is `YYYY-MM-DD`. The em-dash between N and date is mandatory.
- `**Worked on:**` — single line. Include the `feature_id` (e.g. `feat-007`)
  and the commit SHA that ended the session, then a short declarative
  description. Example: `feat-007 @ a1b2c3d — implement user profile page with avatar upload`.
- `**Status:**` — pick exactly one from the allowed set. Do not invent new
  values. `Implemented, not yet tested` is a temporary state that must be
  resolved in a later session.
- `**Features now passing:**` — comma-separated feature_ids that flipped to
  `status: passing` **during this session** (not cumulative). The trailing
  `(X / Y total)` MUST be copied from `feature_list.json` `metadata` after
  running `python HARNESS/tools/feature_list.py update-metadata` if needed. Format
  example: `feat-007, feat-008 (8 / 12 total)`. A feature that moved to
  `in_progress` but did not yet reach `passing` does **not** belong in
  this list — record it under `**Notes:**` instead.
- `**Notes:**` — free-form context the next session needs (blockers, design
  decisions, follow-up TODOs). Hard cap 500 characters; write `(none)` if
  there is nothing to record.

## Example (filled-in)

```
### SESSION 3 — 2026-07-23

**Worked on:** feat-007 @ a1b2c3d — implement user profile page with avatar upload and email verification

**Status:** Implemented and tested

**Features now passing:** feat-007 (8 / 12 total)

**Notes:** Avatar upload uses presigned S3 URLs (5MB max, jpg/png). Email verification sends a 24h-valid token. Next session: feat-009 (password reset) unblocked now that email infra is in place.

---
```

## Maintenance

If this template changes:

1. Update this file (`docs/templates/progress_session_block.md`).
2. Update the reference blocks in `HARNESS.md` and `CODING_AGENT_PROMPT.md`
   so they still point here and continue to match the new field set.
3. Notify any in-flight coding agents to restart their session so they pick
   up the new template.