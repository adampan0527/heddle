# CODE_STYLE.md

> Code style and conventions for this project.
>
> **Two kinds of rules live here:**
> 1. **Hard rules** — universal values. Apply to every project regardless of
>    tech stack. These are non-negotiable. Do not modify.
> 2. **Fill-in rules** — project-specific choices (formatter, linter, etc.).
>    Replace every `[fill-in]` with the project's chosen convention before
>    any coding agent starts work.
>
> Every agent (Initializer or Coding Agent) must read this file before writing
> or modifying source code. See [`_AGENT.md`](../_AGENT.md).

---

## Part 1 — Hard rules (universal values)

These exist to keep code **clean**, **directory layout clear**, **files small**,
and to **avoid "屎山"** (dumpster-fire codebases — large files, tangled
dependencies, hidden side effects, magic values, god components).

### Clean code

- **One responsibility per file.** If you have to use "and" to describe a
  file's purpose, split it. A file should be answerable to "what does this
  file do?" with one short sentence.
- **Short functions.** Aim for ≤ 40 lines per function. If a function grows
  past ~60 lines, extract sub-functions or move logic to a helper module.
- **Short files.** Aim for ≤ 200 lines per file excluding comments and
  blank lines. Above that, the file is doing too much — split by
  responsibility.
- **Names express intent.** Prefer `validateEmailFormat` over `check`. Prefer
  `UserRepository` over `Manager`. No abbreviations unless they are domain
  standard.
- **No magic numbers.** Use named constants. `MAX_RETRIES = 3`, not `3`.
- **No hidden side effects.** A function called `getUser` should not also
  send an email. If two things must happen, call two functions.
- **Comments explain *why*, not *what*.** The code shows what. Comments
  justify non-obvious decisions.
- **No dead code.** Delete unused branches, commented-out blocks, unreferenced
  exports. Git keeps history; the working tree does not.

### Clear directory layout

- **Top-level directories describe layers or domains**, not features. Prefer
  `routes/`, `components/`, `services/`, `data/`, `config/` over
  `stuff/`, `utils/`, `misc/`.
- **Domain logic is colocated.** Code that changes together lives together.
  Do not scatter one feature across five top-level folders.
- **Public vs internal is obvious.** Use a public entry point file
  (`index.ts`, `mod.rs`, `__init__.py`) per directory. Internal modules
  start with `_` or live in a clearly-marked subfolder.
- **Tests mirror source layout.** `src/foo/bar.ts` → `tests/foo/bar.test.ts`
  (or `src/foo/bar.test.ts` co-located — choose per project, document the
  choice).

### Many small files, not few large ones

- **Split by responsibility, not by size target.** Do not artificially chop
  a 100-line file into two 50-liners. Split when a file actually has two
  distinct concerns.
- **One component / one module per file.** A React component + its tests +
  its styles go in three files, not one mega `UserPage.tsx`.
- **Configuration files belong in `config/`, not at the root mixed with
  source.** `config/database.ts`, not `db.ts` at the repo root.
- **No "utils" dumping ground.** If a file accumulates unrelated helpers,
  rename it by what it actually does, or split by topic
  (`time.ts`, `validation.ts`).

### Avoid 屎山

- **No circular dependencies.** `A → B → A` is a refactor trigger, not a
  fact of life.
- **No god objects / god components.** A class or component > ~300 lines,
  or one that "knows everything", must be decomposed.
- **No global mutable state.** Singletons and module-level mutable variables
  are red flags. Inject dependencies instead.
- **No copy-paste.** If two files share logic, extract a shared module.
  Two near-identical blocks are a refactor trigger.
- **No premature abstraction.** Do not introduce a base class, factory,
  or generic for a single use case. Wait for the second occurrence.
- **No silent failures.** No empty `catch`, no `return null` without a
  documented reason. Errors propagate or get logged with context.

### Data integrity via scripts

Some data files are easy to corrupt by hand-editing — JSON configs, schema
files, the harness's `feature_list.json`, etc. These MUST go through a
script:

- **Every fragile data file has a dedicated script in `tools/`** that
  enforces invariants (id uniqueness, type checks, schema validation).
- **Direct edits to those data files are forbidden** — even one-line
  changes. If a script subcommand is missing, add it to the script.
- A direct edit is a code-review blocker, the same way merging without
  a passing CI is.
- Examples in this harness: `HARNESS/tools/feature_list.py` for `feature_list.json`.
- `feature_list.json` features carry a `status` field with a 5-state
  machine: `pending` | `in_progress` | `blocked` | `deferred` | `passing`.
  The legacy boolean `passes` is **deprecated and no longer tolerated**
  — the script strips it on read and refuses to migrate it. All status
  transitions go through `HARNESS/tools/feature_list.py` subcommands
  (`mark-in-progress`, `mark-passing`, `mark-blocked`, `mark-deferred`,
  `next-feature`). `failing` is defined as
  `pending + in_progress + blocked + deferred` — every status that is
  not `passing`.

This is not bureaucracy — the script catches things humans forget (the
`metadata` counter, the `last_updated` stamp, the `category` whitelist).
Forcing edits through the script is what makes those invariants survive
across dozens of sessions.

### Refactor triggers (when to stop adding, start splitting)

| Trigger | Action |
|---------|--------|
| File > 200 lines (excluding comments) | Split by responsibility |
| Function > 60 lines | Extract helper |
| File needs `and` in its purpose statement | Split into two files |
| Module imports from sibling too deeply | Move shared logic up one layer |
| A "utils" / "helpers" file passes 20 entries | Rename or split by topic |
| You copy-pasted a 5-line block twice | Extract shared function |
| A test file is bigger than the code it tests | Tests are doing too much |

---

## Part 2 — Fill-in rules (project-specific)

Complete every `[fill-in]` below before any coding agent starts work.

### Language and toolchain

- **Primary language:** `[fill-in]` (e.g. TypeScript, Python, Go, Rust)
- **Formatter:** `[fill-in]` (e.g. prettier, black, gofmt, rustfmt)
- **Linter:** `[fill-in]` (e.g. eslint, ruff, golangci-lint, clippy)
- **Type checker:** `[fill-in]` (e.g. tsc, mypy, go build, cargo check)
- **Package manager:** `[fill-in]` (e.g. npm, pnpm, poetry, cargo)

### Directory layout (project-specific)

- **Source root:** `[fill-in]` (e.g. `src/`, `app/`, `lib/`)
- **Entry point:** `[fill-in]` (e.g. `src/index.ts`, `app/main.py`)
- **Top-level directories and their meaning:**
  - `[fill-in: e.g. routes/ — HTTP handlers]`
  - `[fill-in: e.g. services/ — business logic]`
  - `[fill-in: e.g. data/ — persistence and models]`
  - `[fill-in: add more as needed]`
- **Test location:** `[fill-in: e.g. co-located *.test.ts OR tests/ mirror]`

### Naming conventions

- **Files:** `[fill-in]` (e.g. `kebab-case.ts`, `snake_case.py`, `PascalCase.tsx`)
- **Classes / components:** `[fill-in]` (e.g. `PascalCase`)
- **Functions / methods:** `[fill-in]` (e.g. `camelCase`, `snake_case`)
- **Constants:** `[fill-in]` (e.g. `SCREAMING_SNAKE_CASE`)
- **Test files:** `[fill-in]` (e.g. `*.test.ts`, `test_*.py`)

### Testing conventions

- **Framework:** `[fill-in]` (e.g. vitest, jest, pytest, go test)
- **Coverage threshold (line):** `[fill-in]` (e.g. 80%)
- **Test file naming:** `[fill-in]`
- **What *must* be tested:** `[fill-in]` (e.g. all public APIs, all error paths)

### Commit conventions

- **Format:** `[fill-in]` (e.g. Conventional Commits, free-form sentence)
- **Branch naming:** `[fill-in]` (e.g. `feat/...`, `fix/...`)
- **Max commit size:** `[fill-in]` (e.g. one feature per commit)

### Project-specific hard rules

Add any additional non-negotiable rules for THIS project (in addition to Part 1):

- `[fill-in: e.g. "no `any` in TypeScript", "all exports must be explicitly typed"]`

---

## How to enforce

- **Formatter + linter + type checker must pass before commit.** Pick a hook
  (`pre-commit`, `husky`, `lefthook`) that runs all three. If the project
  cannot run all three yet, defer commits until it can.
- **CI must run the same checks.** A failing CI is the only ground truth
  for "code is clean enough".
- **Refactor triggers (Part 1) are review-time checks.** A reviewer who
  sees a 300-line file says "split first", not "ship it".

---

Once Part 2 is filled in, every agent must read **both parts** before writing
or modifying source code. Part 1 never changes; Part 2 changes with the
project. See [`_AGENT.md`](../_AGENT.md).