# CODE_STYLE.md

> Code style and conventions for this project.
>
> **Two kinds of rules live here:**
> 1. **Hard rules** — universal values. Apply to every project regardless of
>    tech stack. These are non-negotiable. Do not modify.
> 2. **Project-specific rules** — chosen conventions for this repo
>    (formatter, linter, naming, etc.). Edit when the project changes;
>    never leave the template placeholders unfilled.
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

## Part 2 — Project-specific rules (filled-in)

Filled in on 2026-09-12 (session 2) per `handoff_check` `code_style_fillins`
contract. Source of truth for every convention listed here is either an
existing repo file (e.g. `pyproject.toml`, `tsconfig.json`) or TECH.md
(T-008, T-009, T-019, T-031). When the toolchain evolves, update this
section in the same PR that introduces the new tool.

### Language and toolchain

- **Primary language:** TypeScript (browser + Node.js) and Python 3.11+
  (CLI + daemon). Both are first-class — the project is a bilingual
  monorepo (TECH.md T-008, T-009, T-019).
- **Formatter:**
  - TypeScript / JSON / Markdown / YAML: **Prettier** (default config;
    `heddle format` runs `prettier --write .` per T-019)
  - Python: **Ruff format** (`heddle format` runs `ruff format .` per
    T-019)
- **Linter:**
  - TypeScript: **ESLint** (`heddle lint` runs `eslint .` per T-019 +
    feat-053)
  - Python: **Ruff** (`heddle lint` runs `ruff check .` per T-019 +
    feat-053)
- **Type checker:**
  - TypeScript: **`tsc --noEmit`** (the `packages/web` `typecheck`
    npm script and `build` script both run `tsc --noEmit`; `heddle lint`
    shells out to the same per feat-053). `tsconfig.json` enables
    `strict`, `noUnusedLocals`, `noUnusedParameters`,
    `noFallthroughCasesInSwitch` — treat every error as a blocker.
  - Python: **mypy** (`heddle lint` runs `mypy packages/`, per feat-053).
    Code under `heddle_common/` already uses `from __future__ import
    annotations` and explicit `Final[...]` typing; new modules must do
    the same.
- **Package manager:**
  - JavaScript / TypeScript: **pnpm** with `pnpm-workspace.yaml`
    declaring `packages/*` (root + every package has `package.json`).
    Workspace-wide commands: `pnpm install`, `pnpm --filter web dev`.
  - Python: **pip** with PEP 660 editable installs. Root
    `pyproject.toml` aggregates the three Python packages
    (`heddle-cli`, `heddle-daemon`, `heddle-common`); each package has
    its own `pyproject.toml`. Dev install: `pip install -e .` from
    repo root.

### Directory layout (project-specific)

- **Source root:** `packages/<name>/` — every package (browser, CLI,
  daemon, shared lib) lives under `packages/`. There is no top-level
  `src/` or `app/` directory.
- **Entry point:**
  - Python CLI: `packages/cli/heddle_cli/__main__.py` (the
    `heddle` console-script entry is registered in root
    `pyproject.toml`)
  - Python daemon: `packages/daemon/heddle_daemon/__main__.py` (the
    daemon process entry — wired by feat-017)
  - TypeScript browser: `packages/web/src/main.tsx` (Vite root,
    `index.html` references it)
  - TypeScript Node.js backend: `packages/node/src/main.ts` (created
    by feat-026, not yet on disk in v0.1 stub)
- **Top-level directories and their meaning:**
  - `packages/web/` — browser frontend (React 18 + Vite 5 + Tailwind
    v4 + TypeScript strict; Zustand + @dnd-kit per feat-032 / feat-035)
  - `packages/cli/` — Python CLI (`heddle start | dev | test | lint |
    format | migrate …` per feat-050..feat-053, feat-016 migration
    subcommand)
  - `packages/daemon/` — Python asyncio daemon (LangGraph workflow
    engine + six self-written tools per D-053; feat-017..feat-025)
  - `packages/common/` — Python library shared between CLI and daemon
    (`heddle_common.feature_list_io`, `heddle_common.logging`,
    `heddle_common.atomic_io`)
  - `HARNESS/` — long-running-agent harness machinery that the project
    ships as a vendored tool (templates, scripts, sample
    `feature_list.json`). Not source code for the heddle app itself.
  - `scripts/` — repo-root helper scripts that do not belong in any
    package (e.g. `check_spdx_headers.py`)
  - `docs/` — design / tech / progress docs
- **Test location:** co-located under each package in a `tests/`
  subpackage (Python) or `*.test.ts` sibling (TypeScript). Python
  example: `packages/common/heddle_common/tests/test_feature_list_io.py`.
  TypeScript example: `packages/web/src/lib/logger.test.ts` (sibling
  of `logger.ts`). Tests for `HARNESS/tools/` live alongside the tool
  in `HARNESS/tests/` (mirror layout, not co-located — the HARNESS
  side predates this convention and is grandfathered).

### Naming conventions

- **Files:**
  - Python: `snake_case.py` (e.g. `feature_list_io.py`,
    `atomic_io.py`). Test files: `test_*.py`.
  - TypeScript: `camelCase.ts` for plain modules (e.g. `logger.ts`),
    `PascalCase.tsx` for React components (e.g. `App.tsx`). Test files:
    `*.test.ts` / `*.test.tsx` co-located with the module.
  - Markdown: `UPPER_SNAKE_CASE.md` for top-level docs (`README.md`,
    `DESIGN.md`, `TECH.md`, `HARNESS.md`); `snake_case.md` for nested
    docs (`docs/templates/progress_session_block.md`).
- **Classes / components:** **PascalCase** in both languages
  (`SchemaVersionError`, `App`). Python exception classes end with
  `Error` (e.g. `SchemaVersionError`).
- **Functions / methods:**
  - Python: **snake_case** (`load`, `save`, `mark_passing`,
    `_resolve_path` — leading underscore marks internal helpers).
  - TypeScript: **camelCase** (`emit`, `redact`, `normalizeKey`).
- **Constants:** **SCREAMING_SNAKE_CASE** in both languages
  (`SCHEMA_VERSION`, `STATUSES`, `REDACT_PATTERNS`,
  `BLOCK_REASON_MIN_CHARS`). `Final[...]` annotation is mandatory on
  every Python module-level constant.
- **Test files:** `test_*.py` (Python, mirror layout under each
  package's `tests/`), `*.test.ts` / `*.test.tsx` (TypeScript,
  co-located sibling of the source file).

### Testing conventions

- **Framework:**
  - Python: **pytest** (TECH.md T-018, T-031). Tests are written as
    `unittest.TestCase` subclasses where assertion-heavy (e.g.
    `test_feature_list_io.py`); pure pytest-style `def test_*` is
    also fine. Run via `python -m unittest discover` from the
    package root, or `pytest` once `pytest.ini` is added (feat-052).
  - TypeScript: **Vitest** (TECH.md T-018, T-031). The current
    `packages/web/src/lib/logger.test.ts` uses `node:test` as a
    pre-Vitest shim — when feat-052 lands, migrate to Vitest's
    `test()` / `expect()` API.
- **Coverage threshold (line):** **80%** for `packages/common/` and
  `packages/daemon/` (pure logic, easy to cover). Browser
  (`packages/web/`) and the Node.js backend use component / integration
  coverage instead of line coverage — 70% line threshold is the
  aspirational target there until the E2E suite matures (feat-049).
- **Test file naming:** `test_*.py` (Python), `*.test.ts(x)`
  (TypeScript, sibling of source file). One test class per source
  module; one test method per behavior, named `test_<scenario>` or
  `test_<scenario>_<expected_outcome>`.
- **What *must* be tested:**
  - Every public function exported from a library module
    (`heddle_common.feature_list_io`, `heddle_common.logging`,
    `heddle_common.atomic_io`).
  - Every mutation path on `feature_list.json` (`add`, `mark_*`,
    `next_feature`, `remove`, `recompute_metadata`) — round-trip +
    error path.
  - Every error / refusal branch that the daemon or CLI relies on
    for safety (e.g. `SchemaVersionError` on too-new files,
    `block_reason` length validation, placeholder-step rejection).
  - The HARNESS end-of-session ritual (`session_end.py`) — covered by
    its sibling unit tests under `HARNESS/tests/`.

### Commit conventions

- **Format:** **free-form sentence**, capitalized, imperative mood,
  prefix with the action verb (`Implement:`, `Fix:`, `Update:`, `Refactor:`,
  `Document:`, `Backfill:`). Always include the `feat-XXX` id when the
  commit closes or advances a feature — e.g. `Implement: feat-009
  schema_version field on feature_list.json (T-022)`. WIP commits
  (mandatory per HARNESS.md § Checkpoint Protocol) use the exact
  prefix `WIP: session N - <feature_id> partial` so `git log --grep
  "WIP:"` can locate them.
- **Branch naming:** **none** — work happens on `main` directly in
  v0.1 (no `develop`, no PRs yet; feat-006 will introduce GitHub
  Actions CI but does not yet mandate branch protection). When a
  feature warrants isolation, create a feature branch with the
  pattern `feat/<id>-<slug>` (e.g. `feat/009-schema-version`) and
  rebase-merge back to `main`; never leave a branch dangling past
  one session.
- **Max commit size:** **one feature per commit** (matches the
  HARNESS "one feature at a time" rule). Multi-step features may
  land as a `WIP:` partial followed by a clean `Implement:` — never
  squash the WIP away. Mechanical bookkeeping commits (e.g.
  `Backfill attempts[].session=N`) are allowed standalone.

### Project-specific hard rules

Add any additional non-negotiable rules for THIS project (in addition to Part 1):

- **No `any` in TypeScript.** Use `unknown` + a narrowing guard or a
  proper `interface` / `type` alias. The single permitted use of `any`
  is in interop glue for libraries without types, and it must be
  scoped to one expression with an `// eslint-disable-next-line` and
  a comment justifying it. (`tsconfig.json` strict + `noImplicitAny`
  already enforces this mechanically.)
- **Every Python module starts with `# SPDX-License-Identifier:
  Apache-2.0`** as the first line (or second line, after a shebang).
  `scripts/check_spdx_headers.py` enforces coverage on
  `packages/{cli,daemon,web}/`; `packages/common/` is expected to
  follow the same rule by convention.
- **`feature_list.json` is mutated only via the
  `heddle_common.feature_list_io` library** (T-014). The HARNESS CLI
  (`HARNESS/tools/feature_list.py`) and the project's `session_end.py`
  are the two allowed callers; hand-editing the JSON — even to fix
  one character — is a CODE_STYLE.md Part 1 violation (see "Data
  integrity via scripts").
- **Daemon ↔ Node.js ↔ Browser message envelopes carry a `schema_version`
  int** (T-010, T-022). Unknown versions are refused at the boundary,
  never silently dropped. The same rule applies to `feature_list.json`
  at the file level — see feat-009 / `SchemaVersionError`.
- **No API keys in source.** Per-feature LLM config references an env
  var (`api_key_env`) and the daemon reads it at runtime; never inline
  keys. The structured logger redacts `api_key` / `secret` / `token`
  keys automatically (T-015, T-017, T-030) but defense-in-depth says
  do not write them in the first place.
- **All public Python functions in `heddle_common/` carry type
  annotations** and a docstring. Internal helpers (`_*`) may omit the
  docstring if the function name + signature is self-explanatory, but
  the annotation is still mandatory.

---

## How to enforce

- **Formatter + linter + type checker must pass before commit.** The
  `heddle lint` and `heddle format` subcommands (feat-053) are the
  canonical entry point — they shell out to Prettier + ESLint + Ruff
  + mypy + `tsc --noEmit` in one go. Today (2026-09-12) those
  commands are not yet implemented; until they are, manually run
  `pnpm --filter web typecheck`, `ruff check .`, `ruff format .`,
  and `prettier --check .` before each commit. CI (feat-006) will
  fail any PR that skips them.
- **CI must run the same checks.** A failing CI is the only ground truth
  for "code is clean enough".
- **Refactor triggers (Part 1) are review-time checks.** A reviewer who
  sees a 300-line file says "split first", not "ship it".

---

Both parts of this document are now filled in; every agent must read
both parts before writing or modifying source code. Part 1 never
changes; Part 2 changes with the project (update in the same PR that
introduces a new tool). See [`_AGENT.md`](../_AGENT.md).