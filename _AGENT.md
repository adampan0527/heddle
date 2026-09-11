# _AGENT.md — Document Index

This is the **document index** for the project. Every agent that enters this
project — regardless of which agent it is — should read this file first to
discover which documents govern its work.

## How to use this index

1. Identify **what you are about to do**.
2. Read the matching document from the table below.
3. Follow the rules in that document for the duration of your task.
4. When you switch tasks, return to this index and re-select.

This indirection means agent entry points (`CLAUDE.md`, `AGENT.md`) stay tiny:
they only point here. New rule documents can be added without touching any
agent entry point — just add a row below.

## Documents

| You are about to... | Read this document |
|---------------------|--------------------|
| Work on features incrementally (Initializer or Coding Agent session) | [`HARNESS.md`](./HARNESS/HARNESS.md) |
| Write or modify source code | [`CODE_STYLE.md`](./HARNESS/CODE_STYLE.md) |
| Inspect or modify feature status | [`tools/feature_list.py`](./HARNESS/tools/feature_list.py) |
| Run pre-flight checks before starting a Coding Agent session | [`tools/handoff_check.py`](./HARNESS/tools/handoff_check.py) |
| Write or update session progress blocks | [`docs/templates/progress_session_block.md`](./HARNESS/docs/templates/progress_session_block.md) |
| End a session with an atomic commit / recover WIP | [`tools/session_end.py`](./HARNESS/tools/session_end.py) + [`HARNESS.md` § Checkpoint Protocol](./HARNESS/HARNESS.md) |
| Compute the next session number from `current_progress.txt` | [`tools/next_session_number.py`](./HARNESS/tools/next_session_number.py) |
| Debug a failing feature | [`HARNESS.md` § Common failure modes](./HARNESS/HARNESS.md) |
| Initialize a new project from this template | [`INITIALIZER_PROMPT.md`](./HARNESS/INITIALIZER_PROMPT.md) |
| Understand safety boundaries (forbidden ops / untrusted input) | [`HARNESS.md` § Forbidden operations + § Trust boundaries](./HARNESS/HARNESS.md) |
| Understand product scope / intent of any change | [`DESIGN.md`](./DESIGN.md) |
| Make engineering decisions (tech stack, data model, contracts, packaging) | [`TECH.md`](./TECH.md) |

Add more rows here as the project grows — e.g. testing, deployment, security.

## Maintenance

When you add a new rule document to this project:

1. Create the new `.md` file at the project root.
2. Add a row to the **Documents** table above describing when to read it.
3. Do NOT modify `CLAUDE.md` or `AGENT.md` — they already point here.

When you remove a rule document:

1. Remove the file.
2. Remove the matching row from the **Documents** table above.
3. Update any cross-references in the remaining documents.