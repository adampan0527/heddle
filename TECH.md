# TECH.md — Engineering Decisions

This document is the **single source of truth for engineering decisions** in
the heddle project. It complements [`DESIGN.md`](./DESIGN.md):

- **`DESIGN.md` answers *what* we are building and *why*.**
- **`TECH.md` answers *how* we are building it** — language, framework,
  library, package layout, interface contract, data model, build & ship.
- **[`VISUAL_DESIGN.md`](./VISUAL_DESIGN.md) is the visual design system**
  (colors, typography, components, do/don't) — any UI work references it
  before reaching for ad-hoc styling.

When a decision is made, append a new entry under **Decisions**. When a
question is raised but not yet resolved, append it under **Open
Questions**. Past decisions are append-only — superseded entries get a
new id and a "Superseded by T-NNN" cross-link; old entries are not
edited.

> **Provenance.** Each decision is dated and labeled "Confirmed" once
> approved. Decisions prefixed with `T-` are locked; questions prefixed
> with `Q-` are open. Decisions and questions share a single number
> space per type, so a `T-NNN` decision may supersede an earlier `Q-NNN`.

---

## 1. Decisions (confirmed)

Each entry: `T-NNN — title (YYYY-MM-DD)`. Append at the bottom of the
table; never edit a past entry — supersede it instead.

| ID | Decision | Confirmed |
|----|----------|-----------|
| T-001 | **Three-process local architecture: browser (frontend) / Node.js (HTTP+WS gateway + process supervisor) / Python (LangGraph daemon).** Mirrors DESIGN.md D-037. The browser never speaks to Python directly; the Node.js layer is the single public-facing surface. Rationale: lets us use the best runtime for each layer (browser rendering, TS-native web stack, Python's LLM ecosystem) and keeps the daemon's lifecycle inside a supervisor that can restart it | pending user confirm |
| T-002 | **v0.1 ships PyPI-only; npm packaging is post-v0.1.** Single distribution channel for v0.1: PyPI (`pip install heddle`). The `heddle` console script is a Python entry point. The user must have **Node.js ≥20 on `PATH`** themselves — runtime bundling is explicitly out of v0.1 per the user's "user installs their own environment" policy (resolves Q-006). Rationale: PyPI has a more universal install story than npm-global for developer tools (no `PATH`/permission surprises), Python is the harder-to-replace runtime (LangGraph dependency chain is large), and adding npm as a second packaging pipeline is meaningful maintenance tax for a channel that primarily targets Node.js developers — they can `pip install` too. If npm demand materializes post-v0.1, both packages build from the same monorepo via `hatchling` + a thin `bin/heddle.js` shim | pending user confirm |
| T-003 | **`heddle start` is implemented in Python (`heddle/cli/`) and is the canonical launch sequence.** Node.js is launched as a child process by the Python CLI; the Node.js backend then spawns and supervises the Python daemon (see T-011). One `pip install heddle && heddle start` is the entire developer experience. The npm entry point (`bin/heddle.js`) is a thin shim that delegates to the same Python CLI for parity. Why Python is the entry point rather than Node.js: the Python daemon is the longer-lived / harder-to-replace component, and `pip install` has a more universal "developer tool" install story than `npm install -g` (no `PATH`/permission surprises on macOS/Linux) | pending user confirm |
| T-004 | **WebSocket transport binds to loopback only** (`127.0.0.1`). Default port `8765` for Node.js↔Daemon; default port `5173` for Browser↔Node.js (HTTP + WS share the Fastify server). Ports configurable via `HEDDLE_PORT` and `HEDDLE_DAEMON_PORT` env vars. The Node.js HTTP server **refuses to bind a non-loopback address** — a hard `assert` at startup. See DESIGN.md D-040, D-042. LAN / shared-secret access is explicitly out of v0.1 scope | pending user confirm |
| T-005 | **Single git repository (monorepo).** Layout: `packages/web/` (browser frontend), `packages/daemon/` (Python daemon), `packages/cli/` (Python + thin Node.js shim). The root `HARNESS/` directory is **preserved** as the reference implementation of the underlying agent loop (DESIGN.md §1, D-053 final paragraph) — it is **not** part of the npm/PyPI build artifact; it lives alongside the product for human reference. npm workspaces handle JS inter-package deps; the Python side is one package (the daemon), reused by both `pip install heddle` and `npm install` | pending user confirm |
| T-006 | **Browser frontend stack: React 18 + Vite + Tailwind v4 + TypeScript (strict mode).** State: **Zustand** for ephemeral UI state (drag state, dialog draft state, panel open/close). Server state: **TanStack Query** for the cached feature list, project metadata, and attempt history. Routing: **none** in v0.1 — single-page app, project switcher is the only navigation. Rationale: React + Vite + Tailwind is the 2026 default; TanStack Query gives us free retry/stale/refetch for the WebSocket-merged-with-HTTP data flow; Zustand beats Redux for an app this size without losing TS-friendliness | pending user confirm |
| T-007 | **Drag-and-drop library: `@dnd-kit/core`** for the kanban (DESIGN.md D-009, D-011, D-016, D-019, D-047). Rationale: most flexible for multi-column reorder + cross-column drag, accessible by default (keyboard + screen-reader sortable), works with React 18 strict mode without the React-17-only warnings that `react-beautiful-dnd` carries. **DAG view uses `reactflow` (a.k.a. `@xyflow/react`)** for node-edge rendering, pan/zoom out of the box, and bidirectional selection callbacks (DESIGN.md D-058). Avoids rolling our own SVG/Canvas layer. **v0.1 scope:** full kanban drag-and-drop + basic DAG view (right slide-in panel + pan/zoom + ego-network highlight + bidirectional kanban↔DAG selection sync). **Post-v0.1:** DAG view filter (only show `in_progress` / `blocked` / etc.), search, minimap, keyboard navigation between nodes | pending user confirm |
| T-008 | **Node.js backend stack: Fastify + `@fastify/websocket` + `@sinclair/typebox` for schema validation. TypeScript strict.** Fastify chosen over Express for: (a) 2-3× throughput on WS-heavy workloads, (b) built-in JSON-schema-based input validation, (c) native async/await plugin model that mirrors our supervisor use case. The `@fastify/websocket` plugin provides per-route WS upgrade handling without us hand-rolling `ws` server plumbing. SQLite access via `better-sqlite3` (synchronous, fast, perfect for the low-concurrency metadata workload) | pending user confirm |
| T-009 | **Python daemon stack: Python 3.11+ / asyncio / LangGraph Python SDK / LangChain provider integrations / `websockets` (asyncio) for the WS server / `sqlite3` (stdlib) for the LangGraph checkpointer.** Self-written agent runtime (DESIGN.md D-053) lives in `heddle/daemon/agent_runtime.py`; self-written six tools (Read/Write/Edit/Bash/Glob/Grep, D-053) live in `heddle/daemon/tools/`. **We deliberately do not use LangChain's prebuilt agent executor** — its prompt template and tool-calling conventions would force an opinionated loop shape that conflicts with D-053's "self-written loop" requirement. We *do* use LangChain's chat-model integrations because they abstract the four v0.1 providers (Anthropic / OpenAI / Bedrock / Ollama, per D-053) into one `BaseChatModel` interface | pending user confirm |
| T-010 | **Inter-process message contract: versioned JSON envelopes, schema-tagged.** Every WS message and HTTP request/response carries `{"v": 1, "type": "...", ...}`. The `v` field is an integer protocol version; clients and servers reject messages with an unknown `v` at the boundary. This lets us evolve the protocol without breaking deployed pairs during rolling upgrades. The full schema lives in `packages/shared/protocol/` and is imported by both Node.js and Python (Python side uses `dataclasses` + a hand-written JSON encoder — we deliberately do not pull `pydantic` into the daemon's hot path to keep startup latency low) | pending user confirm |
| T-011 | **Process supervision model.** The Node.js backend (parent) spawns the Python daemon as a child process via Node's `child_process.spawn`. On daemon WS disconnect, Node.js waits `HEDDLE_DAEMON_RESTART_DELAY_MS` (default 500 ms) then re-spawns. The daemon restores all in-flight threads from its SQLite checkpoint store (DESIGN.md D-039, D-051). A per-feature **restart budget of 3 within any 10-minute sliding window** caps silent recovery (D-051); on budget exhaustion, the feature transitions to `blocked` (D-051 second paragraph) and the user must `@feat-XXX retry` to clear it. The counter is persisted in the project's `feature_list.json` (in the feature's `attempts[]`) so it survives restarts. Loopback WS health check: 5-second ping interval; 3 missed pings → daemon considered dead → respawn triggered | pending user confirm |
| T-012 | **Self-written tools (D-053) live in `heddle/daemon/tools/`. Each tool is a `BaseTool` subclass with `name` + `description` + `args_schema` + `_run()`.** The sandbox policy (D-053's `read-only` / `edit-with-confirm` / `full` levels) is enforced by a **tool-dispatch middleware** that runs **before** every tool call and either (a) rejects + returns synthetic error result for `read-only`, (b) emits a WS `confirm_request` event for `edit-with-confirm` and blocks until the user responds in the dialog, or (c) passes through for `full`. The middleware is the single chokepoint — every tool is sandboxed identically without per-tool bespoke logic. The six v0.1 tools' implementations use Python stdlib (`pathlib`, `subprocess`, `re`) plus `watchfiles` for change detection in `Glob`/`Grep` performance paths | pending user confirm |
| T-013 | **Data model — `feature_list.json` schema extensions** (additive, backward-compatible). New optional fields per feature (see DESIGN.md D-018, D-023, D-024, D-054, D-055, D-060): `kind` (`"feature" | "bugfix" | "enhancement"`, default `"feature"`); `fixes: string | null` (bugfix target); `enhances: string | null` (enhancement target); `depends_on: string[]` (already in HARNESS schema, retained); `superseded_by: string[] | null` (replacement pointer per D-054); `implementation_model: string | null` (locked config name from `~/.heddle/configs.yaml`); `blocked_reason`, `deferred_until` (already in HARNESS). **Validation:** the existing `HARNESS/tools/feature_list.py` script continues to be the canonical writer; the web daemon **never** edits `feature_list.json` directly — it always shells out to the same script (or, post-T-014, a thin Python library wrapper around the same logic). See Q-007 for migration strategy | pending user confirm |
| T-014 | **The web daemon uses an embedded Python library** (`heddle.feature_list_io`) that wraps the **same logic** as `HARNESS/tools/feature_list.py` — both share a `heddle_common/` module. The CLI script remains the human/agent-facing entry point; the daemon uses the library directly for in-process mutations. **No two implementations of the mutation rules.** This is the single-source-of-truth contract the project enforces: every `feature_list.json` write goes through one module, regardless of caller | pending user confirm |
| T-015 | **Security — API keys.** `~/.heddle/configs.yaml` stores `api_key_env: <ENV_VAR_NAME>` (DESIGN.md D-053, D-055). The daemon resolves the env var at LLM-call time and **never** logs the resolved value, **never** writes it to disk, **never** includes it in error reports. The resolver is a single function `resolve_api_key(name: str) -> str | None` that returns `None` if unset (the daemon surfaces a clear "missing API key" error to the user). The configs file is **gitignored by default** if it ever lives inside a project directory (it lives at `~/.heddle/`, not inside the project, so this is moot — but the .gitignore template ships it for safety). See Q-009 | pending user confirm |
| T-016 | **Failure handling wiring.** Daemon-internal LLM/tool retries: exponential backoff 1s→2s→4s, 3 attempts (DESIGN.md D-041). On 3rd failure: feature transitions to the **red `in_progress` sub-state** (D-025); auto-mode does not auto-promote (D-025); the user must invoke a dialog command (D-033): `diagnose` / `retry` / `retry-with-hint` / `mark-done` / `abandon`. **Recursion-limit guardrail** (D-052): LangGraph `recursion_limit` set to **200** at daemon startup; configurable via `HEDDLE_RECURSION_LIMIT` env var. On `GraphRecursionError`: same red sub-state + structured `cause: "Recursion limit reached"` diagnosis. **Crash recovery budget** (D-051): 3 restarts / 10 min sliding window, persisted in `feature_list.json`'s feature attempts | pending user confirm |
| T-017 | **Observability — structured logging.** All components emit JSON lines to **stderr** (one event per line, no nested objects). Schema: `{"ts": ISO8601, "level": "debug|info|warn|error", "component": "web|node|daemon|langgraph", "project_id": str?, "feature_id": str?, "event": str, "msg": str, ...}`. Per-project log files: `~/.heddle/logs/<project_id>.log` (one file per registered project); rotation policy in T-029. LLM-call audit: every `agent_step` invocation logs `{feature_id, model, prompt_tokens, completion_tokens, latency_ms, outcome}` to `~/.heddle/logs/<project_id>/llm-audit.jsonl` for cost / debugging analysis. **No PII or API keys** in any log line — verified by an output filter on `api_key`, `secret`, `token` keys in the JSON encoder | confirmed |
| T-028 | **Project removal deletes associated log files.** When the user removes a project via the UI (per D-057), the Node.js layer deletes `~/.heddle/logs/<project_id>.log` and the entire `~/.heddle/logs/<project_id>/` directory. **No "retain for audit" option in v0.1.** Matches user expectation ("I removed the project, get rid of its stuff"); avoids stale data accumulating under `~/.heddle/`. Audit-trail risk (lost logs after removal) is acceptable because logs are also written to stderr per T-017 — if the user redirected stdout, they have another copy. Resolves Q-008 | confirmed |
| T-029 | **Log rotation: 50 MB per file, 5 generations retained, configurable via env.** T-017's per-project log files are wrapped with a rotation policy: `~/.heddle/logs/<project_id>.log` rolls over when it hits 50 MB; rolled files become `<project_id>.log.1` through `<project_id>.log.4`; the oldest (`.log.5`) is deleted on each new rotation. Defaults: `HEDDLE_LOG_MAX_BYTES=50_000_000` (50 MB), `HEDDLE_LOG_BACKUP_COUNT=5`. Daemon side uses Python stdlib `logging.handlers.RotatingFileHandler`; Node.js side uses a hand-rolled equivalent (`winston` transport or ~30 lines of `fs` rotation). Resolves Q-010 | confirmed |
| T-030 | **Optional `~/.heddle/.env` fallback for env-var resolution.** T-015 says API keys come from environment variables. As a convenience for headless setups (systemd / Docker / macOS LaunchAgent / CI runners), heddle also reads `~/.heddle/.env` on startup and loads `KEY=VALUE` lines into the process environment. **Resolution order:** existing process env vars win; `~/.heddle/.env` is fallback only. **Safety:** the file is added to a `.gitignore` template; heddle **never** writes to it; if it has loose permissions (`chmod 644` readable by other users), heddle warns and refuses to start. Format is plain dotenv (one `KEY=VALUE` per line; `#` for comments; no quotes required). Resolves Q-009 | confirmed |
| T-031 | **CI provider: GitHub Actions.** Single workflow at `.github/workflows/ci.yml`. **Triggers:** every PR + push to `main`. **Jobs:** install deps (`pnpm install` + `pip install -e .`) → build (`pnpm --filter web build`) → unit tests (Vitest + pytest under `HEDDLE_FAKE_LLM=1`) → integration tests (Playwright browser E2E + pytest-asyncio daemon round-trip). **Live-LLM E2E tests** (one per major feature, gated behind the `LIVE_LLM_TESTS` repo secret) run on a separate `nightly` schedule, not on every PR, to keep PR runs < 5 minutes and avoid burning API credits. **Caching:** pnpm cache + pip cache keyed on lockfile hashes. Resolves Q-011 | confirmed |
| T-032 | **CI matrix: Linux + macOS + Windows (full coverage).** GitHub Actions matrix runs the same T-031 job on `ubuntu-latest`, `macos-latest`, `windows-latest`. Cost: ~3× per-PR runtime vs single-OS; v0.1 is small enough that GitHub Actions free tier (2000 min/month private, unlimited public) absorbs the multiplier. **Fallback:** if cost becomes an issue post-launch, drop `windows-latest` to a `nightly` schedule only (most regressions are cross-Unix, not Unix-vs-Windows). Resolves Q-012 | confirmed |
| T-018 | **Testing strategy — three layers.** (a) **Unit tests:** Vitest (TS/JS) for packages/web + packages/cli; pytest for packages/daemon. (b) **Integration tests:** Playwright for the browser E2E (drag a card, send a dialog message, verify state propagation); Python `pytest-asyncio` for daemon+WS round-trip tests using a fake Node.js gateway. (c) **LangGraph-specific:** the daemon ships a `tests/fixtures/` directory with pre-recorded SQLite checkpoints so each test can "resume from a known mid-flight state" and assert on the next 1-2 agent steps deterministically (no live LLM calls in CI by default — `HEDDLE_FAKE_LLM=1` env var short-circuits all LLM calls with scripted responses). See Q-011 for CI strategy | pending user confirm |
| T-019 | **Local dev workflow.** `heddle dev` starts the full stack with hot reload: Vite dev server for the frontend (HMR), Node.js backend with `tsx --watch`, Python daemon with `watchfiles` reloader. Browser auto-opens to `http://localhost:5173`. `heddle test` runs the full test pyramid (unit → integration → E2E). `heddle lint` runs ESLint (JS) + Ruff (Python) + TypeScript `--noEmit`. `heddle format` runs Prettier (JS) + Ruff format (Python). All four subcommands are implemented in `packages/cli/heddle/cli/`. The same commands work in CI | pending user confirm |
| T-020 | **Browser / runtime support matrix.** **Browser:** evergreen Chrome / Firefox / Safari (last 2 versions). The `< 480px` mobile banner (DESIGN.md D-056) means we test desktop primarily but ensure the layout does not crash on a phone — deep mobile optimization is post-v0.1. **Node.js:** ≥ 20.x (LTS). **Python:** ≥ 3.11 (LangGraph + asyncio + structural pattern matching all required). **OS:** macOS 12+, Ubuntu 22.04+, Windows 11 (Windows path handling tested but not primary dev environment). See Q-012 | pending user confirm |
| T-021 | **License: Apache 2.0.** Apache License 2.0 covers the entire monorepo — root `LICENSE` file plus per-file SPDX headers. Apache chosen for: (a) explicit patent grant (protects contributors), (b) industry-standard for serious infrastructure/agent frameworks (vs MIT's "just trust me"), (c) compatible with all intended dependencies (no AGPL-style incompatibility). No CLA required for v0.1. Resolves Q-005 | confirmed |
| T-022 | **`feature_list.json` carries a top-level `schema_version: <int>` field (initial value: 1).** The shared mutation library in `heddle_common/feature_list_io.py` (T-014) reads and writes this field: read returns the int, defaulting to 0 if absent; write always sets the current version. New daemon refuses to operate on files with `schema_version > current_max` with a clear "please upgrade heddle" error. Old daemon (and `HARNESS/tools/feature_list.py`) ignores the field per the HARNESS invariant that unknown root fields are dropped silently. All v0.1 schema changes are additive (T-013), so no migration script is needed for v0.1; future breaking changes ship a `heddle migrate <from> <to>` subcommand. Cost: ~15-30 lines in `feature_list_io.py` + 1 root field. Resolves Q-007 | confirmed |
| T-023 | **v0.1 ships `~/.heddle/configs.yaml` pre-populated with four LLM provider templates.** Each template has `name` + `provider` + `model` + `base_url` + `api_key_env` filled in; `api_key` is left blank (user supplies via env var per T-015). The four templates: `anthropic-claude-sonnet` (Anthropic provider, default model `claude-sonnet-4-5`), `openai-gpt-4` (OpenAI provider), `bedrock-claude` (AWS Bedrock, claude model), `ollama-llama` (local Ollama, `llama3`). User picks one, sets the env var, done. Users can edit / clone / delete templates. First-run UX: a CLI prompt lists the four templates with their pre-filled values. **Supersedes** D-055's original "user adds entries themselves" framing for the v0.1 scope; the rest of D-055 (dialog/implementation model split, lock-on-drag, retry-inherits-locked-model) stands | confirmed |
| T-024 | **Daemon WebSocket port conflict: hard fail with message.** Single bind attempt on `HEDDLE_DAEMON_PORT` (default 8765). On `OSError: [Errno 98] EADDRINUSE`, exit with a clear error and instructions for the user to pick a different port via env var. **No auto-select, no interactive prompt.** Rationale: self-hosted loopback tool, port collision = misconfiguration (two heddle instances, or another service holding the port). Auto-select adds a new IPC channel (daemon writes port to a file, Node.js polls); interactive prompt adds UX cost for an uncommon case. Resolves Q-001 | confirmed |
| T-025 | **Multi-project concurrency (post-v0.1): one event loop, N LangGraph threads.** A single daemon process runs an asyncio event loop; all projects' LangGraph threads execute concurrently via `asyncio.gather`. Per-thread error isolation via try/except wrappers (one thread's exception does not propagate to the loop or to other threads). **N subprocesses explicitly rejected.** Rationale: LangGraph Python SDK supports concurrent threads natively; one interpreter is ~10× lighter than N; supervision is one child process instead of a process pool. Applies once multi-project lands (D-057 is post-v0.1); v0.1 has one project so this is effectively a no-op today but locks the architecture to avoid early code going the wrong way. Resolves Q-002 | confirmed |
| T-026 | **Frontend bundling: dev = two processes, prod = one process.** `heddle dev` runs Vite dev server on 5173 + Fastify on 5174; Vite proxies `/api` and `/ws` to Fastify (proxy config in `vite.config.ts`, ~10 lines). `heddle start` (no `dev`) runs only Fastify on 5173; Fastify serves the built `dist/` via `@fastify/static`. Rationale: Vite's HMR + dev tools are worth a second process during development, but production wants single-port serving so the loopback-only security model (D-042 / T-004) is trivially enforceable. The Fastify build step (`pnpm --filter web build` in `heddle start` startup) ensures assets are up-to-date. Resolves Q-003 | confirmed |
| T-027 | **Browser WebSocket client: native `WebSocket` + custom reconnect wrapper.** No external library. The wrapper (~30 lines, lives in `packages/web/src/lib/ws-client.ts`) handles: connection state machine (`connecting` / `open` / `reconnecting` / `closed`), exponential-backoff reconnect (1s → 2s → 4s → 8s, cap 30s), event buffering during disconnect (capped at 1000 events or 30s, then drop oldest), JSON envelope parsing (T-010 protocol). **Upgrade path:** if reconnect logic grows past ~100 lines (e.g., server-pushed backlog replay becomes a real requirement), swap in `reconnecting-websocket` (~2 KB, drop-in replacement for the wrapper). Resolves Q-004 | confirmed |

---

## 2. Open Questions

Each entry: `Q-NNN — title`. Status values: `unresolved` (active question,
no answer yet) / `parked-by-user` (user explicitly deferred this) /
`resolved → T-NNN` (answered, see decision).

### Architecture & deployment

| ID | Question | Status |
|----|----------|--------|
| Q-001 | **Daemon WebSocket port conflict handling.** Resolved: hard fail with clear error message. User picks a different port via `HEDDLE_DAEMON_PORT` env var. See T-024 | resolved → T-024 |
| Q-002 | **Single-instance multi-project concurrency.** Resolved: one event loop, N LangGraph threads via `asyncio.gather`. Per-thread error isolation via try/except wrappers. See T-025 | resolved → T-025 |
| Q-003 | **Frontend bundling.** Resolved: dev = two processes (Vite on 5173, Fastify on 5174, Vite proxies API/WS), prod = one process (Fastify serves built `dist/` via `@fastify/static`). See T-026 | resolved → T-026 |

### Distribution & packaging

| ID | Question | Status |
|----|----------|--------|
| Q-006 | **Bundle Node.js into PyPI package / bundle Python into npm package?** Resolved: do not bundle. User installs their own runtime (Node.js ≥20 + Python ≥3.11 on `PATH`). PyPI is the only distribution channel for v0.1; npm is post-v0.1. See T-002 (revised) | resolved → T-002 (revised) |
| Q-007 | **`feature_list.json` schema migration strategy.** Resolved: add a top-level `schema_version` field (initial value 1), plus ensure all new per-feature fields are optional with defaults. See T-022 | resolved → T-022 |
| Q-008 | **Project removal cascade.** Resolved: project removal deletes its log files too (per user confirm). No "retain for audit" option in v0.1. See T-028 | resolved → T-028 |

### Security

| ID | Question | Status |
|----|----------|--------|
| Q-009 | **`api_key_env` resolution in non-interactive contexts.** Resolved: heddle also reads `~/.heddle/.env` as a fallback (process env vars win). The file is gitignored and heddle never writes to it; loose permissions trigger a warning + startup refusal. See T-030 | resolved → T-030 |
| Q-010 | **Log rotation policy.** Resolved: 50 MB per file, 5 generations retained, configurable via `HEDDLE_LOG_MAX_BYTES` and `HEDDLE_LOG_BACKUP_COUNT` env vars. See T-029 | resolved → T-029 |

### Testing & CI

| ID | Question | Status |
|----|----------|--------|
| Q-011 | **CI provider and test budget.** Resolved: GitHub Actions. PR runs use `HEDDLE_FAKE_LLM=1` and stay < 5 minutes; live-LLM E2E tests run on a nightly schedule behind a repo secret. See T-031 | resolved → T-031 |
| Q-012 | **Cross-platform CI coverage.** Resolved: full matrix — linux + macOS + Windows on every PR. See T-032 | resolved → T-032 |

### Deferred (out of v0.1, but flagged for follow-up)

| ID | Question | Status |
|----|----------|--------|
| Q-004 | **Browser-target WebSocket library on the frontend.** Resolved: native `WebSocket` + custom reconnect wrapper (~30 lines in `packages/web/src/lib/ws-client.ts`). See T-027 | resolved → T-027 |
| Q-005 | **License.** Resolved: Apache 2.0. Chosen for: (a) explicit patent grant protects contributors, (b) industry-standard for serious infrastructure/agent frameworks, (c) compatible with all intended dependencies. See T-021 | resolved → T-021 |
| Q-013 | **Telemetry / opt-in crash reporting.** Strictly opt-in, off by default. Even when on, no feature descriptions or feature IDs are sent — only stack traces + library versions. Decide based on user feedback after v0.1 | parked-by-user |
| Q-014 | **Auto-update mechanism.** `heddle update` CLI subcommand vs OS-package-manager-only (Homebrew, apt). Proposal: ship `heddle update` for users installed via pip/npm directly; Homebrew/apt users get updates through their normal channel. Decide post-v0.1 | parked-by-user |

---

## 3. What this document is NOT

- Not a UI mockup (those live in [`VISUAL_DESIGN.md`](./VISUAL_DESIGN.md) and the actual frontend code).
- Not a code style guide (that's `HARNESS/CODE_STYLE.md` once filled in).
- Not a roadmap with timeline estimates.
- Not a deployment runbook (that's `docs/operations.md`, to be written
  after v0.1 ships).

## 4. Cross-references

- **DESIGN.md decisions that constrain this document:** D-037 (three-layer
  arch), D-038 (daemon bound to UI launch), D-039 (SQLite checkpoints),
  D-040 (WS loopback), D-041 (daemon retry policy), D-042 (loopback
  fail-closed), D-043 (launcher UX), D-050 (auto-mode UI), D-051
  (crash recovery budget), D-052 (recursion limit), D-053 (LLM runtime
  + sandbox), D-054 (post-confirm modification), D-055 (named configs +
  per-project selection), D-056 (kanban layout), D-057 (multi-project),
  D-058 (DAG view), D-059 (anomaly 3-layer defense), D-060 (overlap
  detection + `enhancement` kind).
- **HARNESS references that survive into the web product:** the schema
  in `HARNESS/feature_list.json` and the mutation logic in
  `HARNESS/tools/feature_list.py` (per DESIGN.md §2 "Notes on existing
  HARNESS alignment" and D-053 final paragraph). The web daemon **must
  not** diverge from HARNESS's read/write semantics — T-014 is the
  enforcement.

---

## 5. Maintenance

- New decisions append to the table in §1 with a fresh `T-NNN` id.
- New questions append to the appropriate subsection in §2 with a fresh
  `Q-NNN` id.
- Superseded decisions stay in place; the new decision cross-links them.
- When a question resolves, change its `Status` to `resolved → T-NNN`
  and link the decision. Do not delete the question — it is part of the
  audit trail of *why* the decision exists.