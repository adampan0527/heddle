# DESIGN.md — Product Design Decisions

This document tracks the product design decisions for the **long-running
harness agent with kanban UI** built on top of (and inspired by) the
HARNESS workflow already implemented in this repo.

It is the **single source of truth** for product decisions. When a
decision is made, add a new entry under "Decisions". When a question is
raised but not yet resolved, add it under "Open Questions".

This is **not** an engineering spec — it captures *what* and *why*,
not *how*. Implementation details belong elsewhere.

---

## 1. Product Positioning

A self-hostable, open-source **web-based control plane** for running
long-running, multi-session coding agents on a project, with a
DAG-aware kanban board as the primary user interface.

- Users are developers running it on **their own machines**.
- The product is the **framework**, not a hosted service.
- Existing HARNESS workflow (`HARNESS/feature_list.json`,
  `HARNESS/HARNESS.md`) is the **reference implementation** of the
  underlying agent loop. The web product wraps this workflow with a
  GUI and an intent-clarification layer.

---

## 2. Decisions (confirmed)

Each entry: `D-NNN — title (YYYY-MM-DD)`. Add new entries at the bottom
of the table; never edit a past entry — supersede it instead.

| ID | Decision | Confirmed by user |
|----|----------|-------------------|
| D-001 | Product form is **web**, not desktop | Turn 2 |
| D-002 | After user input, system first **classifies intent**, then presents a **draft for user confirmation**, then inserts | Turn 2 |
| D-003 | DAG insertion position is decided by **LLM + algorithm hybrid**: LLM proposes candidate `depends_on`, algorithm enforces topological validity | Turn 2 |
| D-004 | Feature initial status (`in_progress` / `ready` / `blocked`) is set by **algorithm from the DAG topology**, not by the LLM | Turn 2 |
| D-005 | Kanban highlight behavior is **direction A — semantic-by-status** (selecting a `blocked` feature highlights its `passing` predecessors; selecting a `ready` feature highlights its downstream). Direction B (whole connected subgraph) may be added later as a global view | Turn 2 |
| D-006 | **Product auto-decomposes** user input into multiple features during the clarification dialog | Turn 2 |
| D-007 | ~~The clarification dialog is a modal popup triggered by clicking a "+" button.~~ **Superseded by D-015.** | Turn 2 → Turn 3 |
| D-008 | Within the dialog, the system generates **one card per decomposed feature** (carried forward; popup→dialog wording change in D-015) | Turn 2 → Turn 3 |
| D-009 | The user starts development on a feature by **dragging it** from the "ready" column into the "in progress" column. Only one feature can be `in_progress` at a time (see D-011) | Turn 2 → Turn 3 |
| D-010 | Deployment model is **NOT cloud-hosted**. Users run the framework on their own infrastructure. Other deployment models (local daemon, remote cloud) are out of scope for v0.1 | Turn 2 |
| D-011 | **Single-active model.** Only one feature is `in_progress` at a time. When the user drags multiple features into `in_progress`, all but the head of the queue are **queued** (logical order: top-to-bottom in the ready column). User can reorder the queue by changing vertical position in the ready column | Turn 3 |
| D-012 | The currently `in_progress` feature is rendered with a **visually distinct style** (e.g. accent border, glow, pulsing animation) so the user can always see what is being worked on | Turn 3 |
| D-013 | Each draft feature carries a **temporary id** (e.g. `tmp-001`) before user confirmation. The user can continue the conversation in the dialog; the LLM re-runs decomposition and rewrites the set of draft cards (additions / deletions / edits) in place | Turn 3 |
| D-014 | **No "+" button and no modal popup.** The product has a **persistent dialog at the bottom of the board** as the only entry point for new feature requests | Turn 3 (supersedes D-007) |
| D-015 | **View hierarchy.** Main view is the kanban board with full drag support. The DAG graph is a **secondary view** the user manually opens (zooms into). Drag is only allowed in the kanban | Turn 3 |
| D-016 | **Blocked-column drag policy.** A feature whose current status is `blocked` **cannot be dragged out** of the blocked column. It can only be reordered vertically within the blocked column | Turn 3 |
| D-017 | **Intent classification: chat vs. work.** If the user input is judged to be chat / Q&A, **no cards are generated** — the system simply responds. If it is judged to be work, cards are generated regardless of whether it is a new feature or a bugfix | Turn 3 |
| D-018 | **Bugfix is a distinct kind.** Bugfix features are categorically distinguished from new features (separate `kind` field in schema, separate visual treatment in UI). See Q-024, Q-025 for the detailed design | Turn 4 |
| D-019 | **Queue placement: scheme A.** The queue lives entirely inside the `ready` column, ordered top-to-bottom. The topmost ready feature is the next to promote when the current active feature finishes or fails | Turn 4 |
| D-020 | **No "lock" button. Use "keep" checkboxes instead.** When the user continues the dialog and triggers a re-decomposition, draft cards the user has **checked as "keep"** are guaranteed to survive (preserved verbatim). Unchecked cards may be deleted, edited, or kept at the LLM's discretion. This avoids the ambiguity of "lock": locked card B is downstream of an unchecked card A that was modified — does B get updated? With keep-checkboxes, the contract is simple: only checked cards are preserved verbatim | Turn 4 |
| D-021 | **Per-project auto-mode toggle (default: off / manual).** Each project has an auto-mode switch. When **off** (default), an `in_progress` feature that finishes or fails simply stops — no new feature is auto-promoted; the user must drag one manually. When **on**, the system automatically promotes the topmost `ready` feature into `in_progress` once the previous one leaves that state. **Post-v0.1:** the auto-mode toggle itself (and D-047–D-050's supporting UI: header switch, on-state banner, manual-override behavior, event-driven re-evaluation) is not in v0.1. v0.1 ships manual mode only; default-off remains correct once the toggle lands | Turn 5 |
| D-022 | ~~**Auto-mode failure policy (default: stop).**~~ **Superseded by D-025.** Failure is a *visual* sub-state inside `in_progress`, not a status transition. Auto-mode does not need a "skip-and-continue" policy because the failed feature keeps occupying the active slot; the user must intervene manually to free it | Turn 5 → Turn 6 |
| D-025 | **Failure is a visual sub-state inside `in_progress`, not a status.** When the active feature's agent run fails, the feature's `status` field **stays `in_progress`** but it is rendered in red (with a failure badge). It continues to occupy the active slot — auto-mode does NOT auto-promote a replacement. Downstream features stay `blocked` because the failed one never reached `passing`. The user must explicitly intervene (retry, abandon, escalate). This eliminates the need for a new `skipped` status and aligns with the HARNESS model where `attempts[].outcome` already records failure without changing status | Turn 6 |
| D-026 | ~~**Two-phase decomposition/insertion.**~~ **Superseded by D-029.** The original two-phase design (drafts-only during decomposition, full list only at insertion) is replaced — LLM now sees the full feature-list at all times. | Turn 6 → Turn 7 |
| D-027 | **DAG satisfiability rule: any non-`passing` status blocks downstream.** A feature's dependencies are satisfied if and only if **every** feature in its `depends_on` has `status == "passing"`. Any other status — `pending`, `in_progress`, `blocked`, `deferred`, including failed-but-still-`in_progress` — counts as not-yet-passing and propagates blockage to dependents. The reason a dependency is not passing is irrelevant; only the boolean matters | Turn 6 → Turn 7 |
| D-028 | **Failed-feature recovery actions: Retry or revert-to-in_progress.** ~~Superseded by D-033.~~ The original two-button design is replaced by a dialog-driven approach (D-033); the card itself exposes only a one-click Retry shortcut | Turn 6 → Turn 7 |
| D-029 | **LLM always sees the full feature-list.** Both re-decomposition (during dialog) and insertion use the **full existing feature-list** as context for the LLM. The token cost is acceptable per user judgment (Turn 7). Q-037 and Q-038 now become LLM-handling strategies, not visibility gaps | Turn 7 |
| D-033 | **Failure handling is dialog-driven (β scheme).** All failure recovery actions are expressed as natural-language commands in the bottom dialog, referencing the failed feature via `@feat-XXX`. Supported commands: (a) **diagnose** — "分析 @feat-XXX 的失败原因"; (b) **retry** — "重试 @feat-XXX"; (c) **retry-with-hint** — "重试 @feat-XXX，加上 <hint>"; (d) **mark-done** — "把 @feat-XXX 标记为已手动完成"; (e) **abandon** — "放弃 @feat-XXX". The card itself exposes only a one-click Retry shortcut | Turn 7 |
| D-034 | **`@feat-XXX` mention syntax with autocomplete.** The dialog supports `@feat-XXX` strict-ID mentions, with a dropdown autocomplete listing matching features by id / title / description. No fuzzy `@log`-style mention in v0.1 | Turn 7 |
| D-035 | **Diagnosis output is structured.** When the user invokes diagnose (`@feat-XXX` + "分析 ..."), the LLM produces a structured report: `{ cause: <text>, suggestion: <text>, diff?: { before, after } }`. The UI renders the report as a card with three sections, plus an action button if `diff` is present | Turn 7 |
| D-036 | **Retry-with-hint shows diff before re-running.** When the user invokes retry-with-hint (`@feat-XXX` + "重试，加上 ..."), the LLM first produces the proposed change to the feature's `description` / `steps` and **displays it as a diff**. The user must explicitly confirm before the agent re-runs. No silent auto-modification | Turn 7 |
| D-023 | **Bugfix schema = `kind` + `fixes`.** Two fields: `kind: "feature" \| "bugfix"` for explicit categorization and UI filtering, and `fixes: "feat-XXX"` as a strong-typed edge pointing at the broken feature. Algorithm validates: bugfix features must have `fixes`; the target must exist and not be `passing` | Turn 5 |
| D-024 | **Bugfix UI: orange edge stripe + 🔧 icon.** Cards with `kind == "bugfix"` are rendered with an orange left-edge stripe and a 🔧 icon in the top-right corner. No background color change. Filter dropdown in the kanban header (`all / features / bugfixes`) | Turn 5 |
| D-037 | **Three-layer architecture (browser + Node.js + Python daemon).** The product runs as two cooperating local processes: a **Node.js backend** (HTTP/WebSocket for the browser frontend, process supervision) and a **Python daemon** (LangGraph workflow engine). The browser is pure frontend and cannot spawn the daemon — only the Node.js layer can. SQLite + `feature_list.json` are the shared persistence layer | Turn 8 |
| D-038 | **Daemon lifecycle is bound to Web UI launch.** A single launch command (e.g. `heddle start`) starts both the Node.js backend and the Python daemon. If the daemon crashes mid-run, the Node.js layer detects the dropped WebSocket and **automatically respawns** the daemon, which restores in-flight threads from its checkpoint store. Closing the Web UI (Ctrl-C) gracefully terminates the daemon (SIGTERM) | Turn 8 |
| D-039 | **LangGraph checkpoints persisted to local SQLite.** The Python daemon uses LangGraph's built-in checkpointer with **SQLite** as the backing store. One file (`checkpoints.db`) per project, easy to back up (`cp`), zero external service dependency | Turn 8 |
| D-040 | **Node.js ↔ Python daemon communication: WebSocket over loopback.** The Node.js backend and Python daemon communicate via **WebSocket on localhost** (default port, e.g. 8765). WebSocket carries real-time events (status changes, attempt logs, diagnosis reports). No authentication in v0.1 — loopback-only trust | Turn 8 |
| D-041 | **Daemon-internal retry: exponential backoff, 3 attempts.** When an LLM API call or tool execution hits a transient failure (rate limit, network timeout, 5xx), the daemon retries with exponential backoff: 1s → 2s → 4s. After 3 failures the error propagates to the user. Per-call retry config is out of scope for v0.1 | Turn 8 |
| D-042 | **WebSocket loopback-only, fail-closed.** The Node.js backend **refuses to start** if configured to bind a non-loopback address. Loopback-only trust is the v0.1 security model. LAN / shared-secret access is a future extension | Turn 8 |
| D-043 | **Launcher prints + auto-opens browser.** `heddle start` prints startup progress to stdout and **automatically opens the user's default browser** to `http://localhost:<port>` when ready. Standard dev-tool UX | Turn 8 |
| D-044 | **Draft cards: full-batch confirm/reject, no per-card editing.** The user refines draft cards through **continued dialog input**, not by editing individual cards. Final insertion is all-or-nothing: either all draft cards become permanent `feat-XXX` features, or none do (the user can `Discard all` to drop them). This keeps draft cards as "transient conversation snapshots" rather than first-class editable objects | Turn 9 |
| D-045 | **Confirm-all button (no per-card or drag-to-confirm).** The transition from `tmp-XXX` drafts to permanent `feat-XXX` features happens via a single `Confirm all (N)` button at the bottom of the draft tray. The button is enabled only when N > 0. There is no drag-to-confirm or per-card ✓ button | Turn 9 |
| D-046 | **No Discard-all button. Dialog is the only intent channel.** There is no explicit "reject all drafts" button. If the user is dissatisfied with the current drafts, they continue typing in the dialog; the LLM re-decomposes (potentially returning 0 cards if the user expresses abandonment, e.g. "算了不要了"). This eliminates the wasteful two-step flow of *reject buttons + retype*, and reinforces the product's core principle: **the dialog is the only place intent enters the system**. The only action button in the draft tray is `Confirm all (N)` | Turn 9 |
| D-047 | **Auto-mode allows manual override.** When auto-mode is on, the user can still drag cards manually (reorder the queue, pull a feature out of `in_progress`, force-promote a non-top ready feature). User drag always wins. After a manual reorder of the ready column, the system **immediately re-evaluates** and auto-promotes the new topmost ready feature if the active slot is empty | Turn 10 |
| D-048 | **Auto-mode is event-driven.** The system re-evaluates the queue on every status change (not by polling). Status changes from any source — agent completion, manual drag, failed attempt — trigger the check. No periodic timers | Turn 10 |
| D-049 | **New projects default to auto-mode off.** A new project starts in manual mode; the user must explicitly enable auto-mode | Turn 10 |
| D-050 | **Auto-mode toggle: header switch + on-state banner.** The auto-mode toggle lives in the header (always visible). When enabled, a banner appears below the header reminding the user "auto-mode is ON — features will start themselves" | Turn 10 |
| D-051 | **Session-crash recovery: silent checkpoint resume with per-feature restart budget.** Daemon-level crashes that interrupt a mid-flight feature **silently restore** from the LangGraph SQLite checkpoint (no status change, user is unaware). A per-feature **restart budget** caps silent recovery at **3 restarts within any 10-minute sliding window**. If the budget is exhausted, or the checkpoint is missing/corrupt, the feature's status moves to **`blocked`** (NOT the red `in_progress` sub-state from D-025 — this is a system-layer failure, not an agent-output failure). The user clears the situation via `@feat-XXX retry` (D-033), which resets the counter and reactivates the feature. The counter is persisted alongside the feature (SQLite or feature metadata — implementation detail). The "hung but not crashed" failure mode is **explicitly out of scope** and tracked separately as Q-002b | Turn 11 |
| D-052 | **Agent hang protection in v0.1: LangGraph `recursion_limit` only.** Use LangGraph's built-in `recursion_limit` as the sole hang guardrail in v0.1. The limit is configured at daemon startup (single global value, ~200 steps is the proposed default — tune in implementation). When the limit is hit, LangGraph raises `GraphRecursionError`, which the daemon treats as a feature failure → enters the red `in_progress` sub-state (D-025) and is handled via D-033 dialog commands (the user sees `cause: "Recursion limit reached"` and can retry / abandon). **Explicitly NOT in v0.1**: doom-loop detection (Cline/Praison-style), silent-hang wall-clock watchdog, cost budgets, and user-driven "still thinking?" prompts. These are tracked as Q-002c / Q-002d / Q-002e (or one consolidated future decision) once v0.1 ships | Turn 12 |
| D-053 | **LLM provider is fully user-configured per project; framework ships its own agent runtime + tool set + sandbox; LangGraph graph owns all per-feature flow.** Per-project LLM config in `.heddle/config.yaml`: `provider` + `base_url` + `api_key_env` + `model`. **v0.1 providers**: `anthropic` / `openai` / `bedrock` / `ollama` (each via its langchain integration). **API key must use environment variable reference** — no inline keys in config. **Agent runtime = daemon-internal Python code** (`heddle/daemon/agent_runtime.py`), NOT a third-party SDK, NOT a subprocess. The runtime is the LLM ↔ tool loop: call LLM → parse tool calls → execute tools → append results → loop until LLM signals done. **Tool set is self-written** — v0.1 ships exactly six tools: `Read` / `Write` / `Edit` / `Bash` / `Glob` / `Grep`. **Sandbox strictness has three levels**: `read-only` (Write/Edit/Bash disabled or read-only), `edit-with-confirm` (Write/Edit/Bash requests confirmation via D-033 dialog), `full` (no restrictions). **LangGraph graph holds all per-feature flow**: `load_feature` → `load_context` → `agent_step` (loop) → `persist_result` → `emit`. `load_context` behavior (which files, how to assemble into prompt, size limits, caching, failure handling) is **deferred** — only the node's existence and position are fixed here. **Subagent dispatch is not in v0.1** (explicitly deferred). **Protocol switch requires daemon restart**; in-flight feature finishes its current attempt first. **HARNESS CLI and the web daemon share only the `feature_list.json` schema**, not LLM constraints — HARNESS is already model-agnostic at the tool layer (its `handoff_check.py` / `session_end.py` reference no provider) and ships parallel entry points `CLAUDE.md` (for Claude Code) and `AGENT.md` (for any other agent, which is what the web daemon uses) | Turn 13 |
| D-054 | **Post-confirm feature modification via dialog commands; split/merge use a `superseded_by` metadata field, NOT a new status.** Mechanism is the same as D-033's failure handling: bottom dialog + `@feat-XXX` strict mention (D-034) + LLM with full feature-list context (D-029). No special UI buttons for split/merge/edit/reprioritize/add-dep/remove-dep — they are all instances of natural-language commands in the dialog. **Split/merge semantics** (the hard part): the original feature's `status` field is **NOT changed** (status describes work lifecycle, "is replaced" is an orthogonal dimension). Instead, an optional `superseded_by: [string]` field is added to the feature object (default `null`). Features with `superseded_by != null` are **skipped by `next-feature`** and **ignored by DAG satisfiability** (treated as if absent from the work graph). The kanban UI hides them by default (collapsed into an archive view); the DAG view also omits them by default. **Downstream dependency re-link** is part of the split/merge proposal: when split produces new features, the LLM proposes new `depends_on` edges for downstream features pointing at the replacement features, and the user confirms via the existing draft-confirm flow (D-013 / D-044 / D-045). **Audit trail**: the original feature stays in `feature_list.json` forever (consistent with HARNESS hard rule "never remove features"); `attempts[]` and all other history are preserved. **Why no new status**: `pending`/`passing`/`in_progress`/`deferred`/`blocked` all have work-lifecycle semantics; none fit "replaced by X". Borrowing `deferred` is semantic pollution; adding `superseded` is a schema enum change with cascading impact on D-027 and the kanban layout (Q-010). A nullable metadata field is cleaner: it is orthogonal to status, carries the replacement pointer (more informative than a bare state), and has no blast radius. **v0.1 command set** (in addition to D-033's failure-recovery commands): `split` (`@feat-XXX 拆成 N 个`) / `merge` (`@feat-XXX 跟 @feat-YYY 合并`) / `edit` (`@feat-XXX 改 description 为 ...`) / `reprioritize` (`@feat-XXX 设为高优先级`) / `add-dep` / `remove-dep`. Destructive operations (split, merge, abandon) show a diff before confirmation | Turn 14 |
| D-055 | **Multiple named LLM configurations; dialog model and implementation model are independently selectable.** Configs live in a global registry at `~/.heddle/configs.yaml` — user can add any number of named entries, each with `name` + `provider` + `base_url` + `api_key_env` + `model` (D-053 schema). The registry is **shared across all projects** (per-user, not per-project). **Two independent selections** persist alongside the registry: `selection.dialog_model` (drives decomposition, re-decomposition, all `@feat-XXX` dialog commands, failure-recovery commands) and `selection.implementation_model` (default for new features). **Dialog model switching**: a picker at the top of the dialog; switching is free between turns and takes effect on the next LLM call. **Implementation model switching**: a **persistent dropdown in the main UI** (visible to the user at all times so it is obvious what model will be used to implement features). When the user drags a feature to `in_progress`, the **currently-selected implementation model is locked onto that feature** and stored as `implementation_model: "config-name"` on the feature object (so `attempts[]` history records what model ran). While the feature is `in_progress`, the model **cannot be changed** — to switch models on an in-flight feature, the user must first stop/abandon it, change the dropdown, and re-drag. **`@feat-XXX retry` inherits the feature's locked model** (no model change on retry — consistent with the lock-on-start rule). Both selections default to the **first entry in the registry** when no explicit choice has been made. **v0.1 does not bundle "decomposition preset" or "implementation preset"** — each is chosen independently; preset bundles (e.g. "cheap-decomp + quality-impl") are a future extension. **Supersedes** the per-project single-config framing of D-053; the rest of D-053 (v0.1 providers, API key env-var rule, agent runtime, tools, sandbox, HARNESS schema-sharing) stands. **v0.1 supersede (per user confirm):** the registry ships pre-populated with four default templates — `anthropic-claude-sonnet` / `openai-gpt-4` / `bedrock-claude` / `ollama-llama` — each with `provider` / `model` / `base_url` / `api_key_env` filled in. User picks a template, sets the env var, done. Users can still edit / clone / delete templates. See TECH.md T-023 for implementation | Turn 15 |
| D-056 | **Kanban layout: 3 fixed main columns + 3 collapsible bottom lanes; Done default-expanded; responsive down to narrow screens.** **Main view = 3 columns side-by-side**: `in_progress` / `ready` / `blocked` (locked by D-009 / D-011 / D-016 / D-019). **Bottom area = 3 collapsible lanes**: `Done` (features with `status == "passing"`), `Someday` (`status == "deferred"`), `Archive` (`superseded_by != null`, per D-054). **Default expansion state**: `Done` expanded by default (positive feedback on recent completions), `Someday` and `Archive` collapsed. **Done card style**: title + completion timestamp (from `attempts[]` last entry) + ✓ checkmark, no description expand. **Collapsed-lane visual indicators**: each lane header **always shows** the count (`▶ Someday (3)` / `▶ Archive (5)`); hovering the count previews the top 3 titles; the ready column footer shows a `▾ 还有 N 个暂缓项` link so deferred features are not forgotten. **Deferred vs Blocked visual distinction**: blocked cards show 🔒 + the blocking dependency (`"等待 @feat-001"`); deferred cards show 🕐 + the reason and `--until` time if any. **`deferred` is sticky**: a deferred feature whose dependencies later resolve **does not auto-return to ready** — the user's `defer` decision is respected until they explicitly `@feat-XXX resume` (or similar). **Resolution precedence**: a feature with `superseded_by` set appears ONLY in Archive, never in Done (even if `status == "passing"`). **Responsive**: `>= 1280px` → 3 columns horizontal + lanes full-width below; `768–1280px` → same layout with tighter spacing; `< 768px` → 3 columns **stacked vertically** (in_progress → ready → blocked) and lanes below, main view scrolls **vertically only** (no horizontal scroll); `< 480px` → v0.1 shows a "建议桌面浏览器使用" banner and does not deeply optimize. **Drag rules unchanged**: drag only between main columns (D-016: blocked features cannot leave the blocked column; D-054: superseded features hidden from main view are not draggable). **Lane interactions**: Done/Someday/Archive are view-only — no drag in or out, no inline edit; modification goes through dialog commands (D-054) | Turn 16 |
| D-058 | **DAG view = right-side slide-in panel with bidirectional sync to kanban; zoom and pan in v0.1; auto-fullscreen on narrow viewports.** **Presentation**: a panel slides in from the **right edge** of the main UI, occupying roughly the right half of the viewport by default. The kanban on the left compresses to make room. **Width**: the divider between kanban and DAG is **draggable** — the user can pull it left/right to resize the DAG panel within a sensible range (e.g. 30%-70%). Closing the panel returns the kanban to full width. **Narrow-viewport fallback**: when the viewport is `< 1280px` wide, the side panel is **not used** — opening the DAG view instead promotes it to **fullscreen overlay** (essentially the panel content rendered into a full-window modal without the kanban behind it). This keeps the DAG readable on smaller screens without abandoning the side-panel design on desktop. **Bidirectional sync (kanban ↔ DAG)**: (a) clicking a feature card on the kanban selects the matching DAG node, highlights it together with its direct ancestors + direct descendants + the paths between them, and dims everything else; (b) clicking a DAG node selects the matching kanban card, scrolls the kanban to bring the card into view if it is not already visible, and applies the same highlight in the kanban (D-005 direction A — semantic by status). The two views stay in sync as long as the DAG panel is open; closing the panel clears the selection state. **DAG node highlight semantics**: ego-network style — selected node, its direct parents, its direct children, and the connecting edges are highlighted; everything else is dimmed (visible but de-emphasized). This is **different from the kanban's D-005 direction A** because the DAG already shows the whole graph — there is no need for the special "passing predecessors for blocked" rule; the structure is the source of truth. **In v0.1, the DAG view supports**: pan (click-and-drag empty space) + zoom (mouse wheel + pinch on touch) + a **fit-to-screen** button. **Deferred** (post-v0.1): filter (only show `in_progress` / `blocked` / etc.), search (find a node by id/title), minimap (for very large graphs), keyboard navigation between nodes. The DAG layout algorithm itself (force-directed, layered, etc.) is an implementation detail | Turn 18 |
| D-059 | **Decomposition anomaly handling: three-layer defense (autocomplete → dialog clarification → algorithm catch); no draft cards until user clarifies.** Anomalies the LLM can detect during decomposition (with full feature-list visibility from D-029) include: `fixes` target does not exist (Q-037); `fixes` target is already `passing`; future anomalies (duplicate features per Q-038, missing `depends_on` targets, cycles, etc.). **Layer 1 — autocomplete (first defense, enhanced D-034)**: as soon as the user types `@` in the dialog, a popup appears listing all features; the list filters in real time as the user types more characters (matched against id + title + description); **6 candidates visible at a time with scrolling**; ↑/↓ to navigate, Enter to confirm, Esc to dismiss. This blocks most typo'd `@feat-099` before the LLM ever sees it. **Layer 2 — LLM dialog clarification (second defense)**: when the LLM encounters an anomaly during decomposition, it does **not generate any draft cards**; it responds in the dialog explaining the issue, listing related candidates (similar feature ids, plausible interpretations), and asks the user to clarify. The user replies in the next dialog turn; the LLM re-decomposes (per D-013) based on the clarification. There is **no "explicit authorization" shortcut** — every anomaly goes through the same dialog flow, behavior is consistent. **Layer 3 — algorithm catch (third defense, complements D-023)**: if LLM hallucination or a race condition slips a bad draft through (e.g., `fixes: "feat-099"` where feat-099 was deleted after the LLM read the feature-list), the algorithm at insert time validates `fixes` against the live feature-list (per D-023) and rejects the draft, clears the tray, and prompts the user in the dialog to re-describe. This is a defense-in-depth measure for the rare failure case, not the normal path. The three layers are independent: autocomplete catches typos at input time, LLM clarification catches semantic anomalies, algorithm catch handles the residual failure case | Turn 19 |
| D-060 | **Duplicate / overlap detection during decomposition; new `enhancement` kind for partial overlaps; LLM semantic judgment; 3-way clarification; one extra confirmation before execution.** Anomaly classes (Q-038), in increasing similarity: (a) **loose similarity** — LLM does not surface, normal draft flow; (b) **partial overlap** (e.g., user asks for "GitHub OAuth", feat-007 is "Google OAuth") — LLM surfaces and **suggests treating as an enhancement of the existing feature**; (c) **strong duplicate** (e.g., user asks for "OAuth login", feat-007 is "OAuth login") — LLM surfaces, asks user to choose between **edit existing / create new anyway / cancel**. The decision between (a)/(b)/(c) is left to the **LLM's semantic judgment** — no hard-coded similarity threshold. **New `enhancement` kind**: parallel to `bugfix` (D-018); the `kind` enum is now `feature | bugfix | enhancement`. An enhancement adds new capability to an existing feature. Schema: `{ kind: "enhancement", enhances: "feat-XXX", depends_on: ["feat-XXX", ...], ... }`. The `enhances` field is the semantic pointer at the target; `depends_on` keeps the DAG consistent (the enhancement is a downstream / post-position node of the target it enhances). UI treatment parallel to bugfix (D-024): distinct icon (✨), distinct left-edge color stripe (different from bugfix's orange — green or purple), `enhancements` added to the kanban filter dropdown (`all / features / bugfixes / enhancements`). **Pre-execution confirmation**: for `enhancement` (and other destructive operations covered by D-054 — split / merge / abandon), the LLM shows a **diff preview in the dialog before execution** (e.g., "this enhancement will add steps [s1, s2] to feat-007 and create a new feature feat-NEW depending on feat-007 — confirm?"). User confirms, then the draft goes through the standard Confirm-all flow. This is one extra round of user consent beyond the initial 3-way clarification. **Historical duplicates**: when a user mentions a historical feature that is itself a duplicate of an existing one (e.g., user says "feat-014" but feat-014 is conceptually the same as feat-009), the LLM applies the same 3-way handling — edit existing / create new (with a note about the historical duplicate) / cancel. Historical duplicates do not get a separate `kind`; they go through the standard enhancement / new-feature flow with appropriate context in the dialog. **Post-v0.1:** the `enhancement` kind, the 3-way clarification, and the diff-preview flow are not in v0.1. v0.1 ships only `feature` + `bugfix` kinds. Duplicate detection still runs (LLM semantic judgment, Q-038 layer-2 defense), but it falls back to plain dialog clarification ("this looks similar to feat-XXX — edit it or skip?") without the `enhancement` suggestion | Turn 20 |
| D-057 | **One instance manages multiple projects; instance start-up directory is irrelevant; projects registered via folder picker.** A single `heddle start` instance can manage an arbitrary number of projects; the cwd at launch is **not** related to the instance state — instance state lives under `~/.heddle/`, project data lives in user-selected folders. **Project registry**: `~/.heddle/projects.json` holds the list of registered projects as `{id, name, path, added_at, last_accessed_at}`. `name` is **user-editable** (defaults to the directory's basename). **Add-project flow**: UI button → native OS folder picker → user selects an existing directory → validation → registration. The picker **does not create directories** — the directory must already exist (matching the DeepSeek Harness / Codex pattern). Validation outcomes: (a) directory has `feature_list.json` → adopt as-is; (b) directory has code but no `feature_list.json` → scaffold empty `feature_list.json` + `.heddle/config.yaml`; (c) directory is empty → scaffold a minimal HARNESS skeleton (with user confirmation); (d) directory has a foreign `.heddle/` (different `checkpoints.db`, etc.) → surface conflict and let the user confirm / pick a different folder. **Per-project data layout** (everything inside the user-selected folder): `feature_list.json` (HARNESS schema), `.heddle/config.yaml` (per-project state: `dialog_model` / `implementation_model` selection, `auto_mode`, `sandbox` strictness), `.heddle/checkpoints.db` (LangGraph SQLite checkpointer, **one file per project**). **D-055 clarification**: the `dialog_model` / `implementation_model` selection is **per-project**, not global — different projects may use different models. The global `~/.heddle/configs.yaml` registry (D-055) is the named-config pool that all projects draw from. **Cross-project execution**: each project independently obeys D-011 (at most one `in_progress`); multiple projects may each have an `in_progress` feature running concurrently. Daemon isolates LangGraph state by `project_id`. Switching projects in the UI **does not pause** the previous project's execution — daemon keeps running it in the background. **WebSocket single-connection multiplexing**: one browser WebSocket carries events for all projects, tagged with `project_id`; the frontend filters by the currently-viewed project. **Active-execution UI**: the visual indicator for `in_progress` (D-012) is **only shown in the project that owns it** — no cross-project banner or notification when another project is running. **Remove project**: removes the entry from `projects.json` and stops that project's execution. **No files in the project directory are deleted**; the user can re-register the same path later if the directory still exists. **Re-registration**: a removed project's directory can be re-added via the folder picker; the registry creates a fresh entry (potentially with a new `id`); existing `feature_list.json` and `.heddle/` data are preserved. **HARNESS CLI compatibility**: each registered project folder is also a valid HARNESS CLI project — `feature_list.json` and `tools/feature_list.py` work normally; the web daemon observes file changes via FS watch / mtime check (implementation detail deferred). **Post-v0.1:** v0.1 ships the multi-project registry + folder picker + scaffolding logic, but only **one project is active at a time** (the user can switch between registered projects, but the daemon does not run two projects' LangGraph threads concurrently). Concurrent cross-project execution, WebSocket multiplexing across projects, and "switch projects without pausing" are all post-v0.1 | Turn 17 |

### Notes on existing HARNESS alignment

The decisions above are consistent with the schema already used in
`HARNESS/feature_list.json`:

- `status: pending | in_progress | blocked | deferred | passing`
  already maps cleanly to the three kanban columns
  (in_progress / ready / blocked), with `pending` collapsing into
  `ready` and `deferred` shown as a separate "someday" lane.
- `depends_on: [feature-id]` is the DAG edge field the insertion
  algorithm will operate on.
- `attempts[]` already records session history — the kanban can
  surface this as a "failure history" view without schema change.

**Implication:** the v0.1 does **not** need a new feature-list format.
It reads and writes the same JSON the HARNESS tools already use.
This is a deliberate design choice — the product *is* HARNESS with a
GUI, not a parallel system.

---

## 3. Open Questions

Each entry: `Q-NNN — title`. Status values: `unresolved` (active question, no answer yet) / `parked-by-user` (user explicitly deferred this) / `resolved → D-NNN` (answered, see decision).

### Architecture & runtime

| ID | Question | Status |
|----|----------|--------|
| Q-001 | **Concurrency model.** ~~(parked by user, now superseded — see D-011)~~ | resolved → D-011 |
| Q-002 | **Failure recovery.** ~~Resolved~~. Silent checkpoint resume with per-feature restart budget (3 / 10 min sliding window); on budget exhaustion or checkpoint loss → `blocked`; user clears via `@feat-XXX retry`. The "hung but not crashed" failure mode is a separate concern tracked as Q-002b. See D-051 | resolved → D-051 |
| Q-002b | **Agent hang watchdog.** ~~Resolved for v0.1~~. LangGraph `recursion_limit` is the only hang guardrail in v0.1 (D-052). Doom-loop detection, silent-hang wall-clock watchdog, cost budgets, and user-driven "still thinking?" prompts are out of scope for v0.1 and to be decided in a follow-up decision (e.g. D-053+) once v0.1 ships | resolved → D-052 |
| Q-003 | **Session lifecycle.** ~~Resolved~~. Long-lived Python daemon (D-037). Each feature execution is one LangGraph thread invocation inside the daemon; the LLM ↔ tool loop is self-written Python code in the daemon, not a subprocess or third-party SDK. See D-053 | resolved → D-053 |

### Intent classification (D-002)

| ID | Question | Status |
|----|----------|--------|
| Q-004 | **Intent taxonomy.** ~~Largely resolved by D-017~~. Remaining sub-question: are bugfixes and new features visually / categorically distinct in the UI, or are they the same kind with the same workflow? See Q-018. | resolved → D-017 (Q-018 still open) |
| Q-005 | **Non-feature inputs.** ~~Resolved~~. Chat/Q&A input does not generate cards; the system responds directly. See D-017. | resolved → D-017 |
| Q-006 | **Card-level confirmation.** ~~Resolved~~. Full-batch accept/reject via continued dialog. See D-044. | resolved → D-044 |

### Feature decomposition (D-006)

| ID | Question | Status |
|----|----------|--------|
| Q-007 | **Granularity heuristic.** ~~Resolved by composition~~. Target feature size is "one agent session's work" (30-80 steps typical, 150 max). The decomposition prompt carries this anchor plus natural-boundary heuristics; user is the final judge via continued dialog before Confirm all (D-013, D-044, D-045). Sanity checks at submit-time catch degenerate cases (N==0 / N>30 / missing test steps). Post-confirm re-adjustment is via D-054 (split / merge / edit). See D-054 | resolved → D-054 |
| Q-008 | **Re-decomposition.** ~~Resolved~~. User addresses an existing feature with `@feat-XXX` in the bottom dialog and asks to split / merge / edit. The LLM produces a draft (split produces new feature cards; edit is a direct modification with diff preview). Downstream-dependency re-link is part of split/merge proposals. Original feature is marked with `superseded_by` metadata field, NOT a status change. See D-054 | resolved → D-054 |
| Q-009 | **LLM model choice.** ~~Resolved~~. Configurable via a global named-configs registry (`~/.heddle/configs.yaml`); dialog model and implementation model are independently selectable; implementation model locks per-feature at drag time and is recorded on the feature. See D-055 | resolved → D-055 |
| Q-018 | **Bugfix classification.** ~~Resolved~~. Bugfix is a distinct kind. See D-018. Open sub-questions: Q-024 (schema field), Q-025 (UI treatment). | resolved → D-018 |

### Kanban & DAG visualization (D-005)

| ID | Question | Status |
|----|----------|--------|
| Q-010 | **Column layout.** ~~Resolved~~. 3 main columns (in_progress / ready / blocked) + 3 collapsible bottom lanes (Done / Someday / Archive). Done default-expanded. Deferred is sticky. Responsive down to narrow screens. See D-056 | resolved → D-056 |
| Q-011 | **View modes.** ~~Resolved~~. Main = kanban (drag-enabled), secondary = DAG graph (manually opened). See D-015. | resolved → D-015 |
| Q-012 | **Drag target semantics.** ~~Resolved~~. Blocked features cannot be dragged out of the blocked column. See D-016. | resolved → D-016 |
| Q-019 | **Queue visualization.** ~~Resolved~~. Scheme A — all queued features live inside the `ready` column, ordered top-to-bottom. See D-019. | resolved → D-019 |
| Q-020 | **DAG secondary view presentation.** ~~Resolved~~. Right-side slide-in panel, draggable divider, bidirectional sync with kanban. Auto-fullscreen on narrow viewports. Zoom + pan in v0.1; filter/search/minimap deferred. See D-058 | resolved → D-058 |

### Drag-to-start (D-009)

| ID | Question | Status |
|----|----------|--------|
| Q-013 | **Drag source.** ~~Resolved~~. Only `ready` features can be dragged to `in_progress`. Blocked features are locked in place. See D-009 + D-016. | resolved → D-009 + D-016 |
| Q-014 | **Single-active rule.** ~~Resolved~~. Single active at a time. See D-011. | resolved → D-011 |
| Q-021 | **Queue failure behavior.** ~~Resolved~~. Manual by default; auto-promote when auto-mode toggle is on (D-021). See also Q-027–Q-031 for auto-mode edges. | resolved → D-021, D-022 |

### Draft cards & dialog flow (D-013, D-014)

| ID | Question | Status |
|----|----------|--------|
| Q-022 | **Draft→confirmed transition mechanics.** ~~Resolved~~. `Confirm all (N)` button at bottom of draft tray. See D-045. | resolved → D-045 |
| Q-023 | **Continued-conversation semantics.** ~~Largely resolved~~. New user input triggers full re-decomposition; checked-keep cards are preserved verbatim. See D-020. Open: does the LLM see the full feature-list (existing + drafts) when re-decomposing? My proposal: yes, see Q-026. | partially resolved → D-020 |

### Bugfix kind (D-018)

| ID | Question | Status |
|----|----------|--------|
| Q-024 | **Bugfix schema field.** ~~Resolved~~. `kind` + `fixes` (scheme Z). See D-023. | resolved → D-023 |
| Q-025 | **Bugfix UI treatment.** ~~Resolved~~. Orange edge + 🔧 icon. See D-024. | resolved → D-024 |

### Auto-mode (D-021, D-025)

| ID | Question | Status |
|----|----------|--------|
| Q-027 | **Manual override under auto-mode.** ~~Resolved~~. Manual drag always wins; manual reorder re-triggers auto-evaluation. See D-047. | resolved → D-047 |
| Q-028 | **Auto-mode trigger.** ~~Resolved~~. Event-driven on status changes. See D-048. | resolved → D-048 |
| Q-030 | **Global default for auto-mode.** ~~Resolved~~. Off. See D-049. | resolved → D-049 |
| Q-031 | **Auto-mode UI affordance.** ~~Resolved~~. Header toggle + on-state banner. See D-050. | resolved → D-050 |

### Failure as visual sub-state (D-025)

| ID | Question | Status |
|----|----------|--------|
| Q-032 | **Failure visual specifics.** ~~Resolved~~. Red left edge + ⚠️ icon (default option). | resolved → D-025 (default Q-032 option a) |
| Q-034 | **Failed feature in the DAG.** ~~Resolved~~. Red fill + solid border (default option). | resolved → D-025 (default Q-034 option) |
| Q-036 | **Retry vs. revert-to-in_progress semantics.** ~~Withdrawn~~. Replaced by D-033 dialog-driven approach; revert is no longer a first-class card action. | resolved → D-033 |
| Q-039 | **Failure handling entry point: dialog-led (β) vs button-led (α)?** ~~Resolved~~. Dialog-led (β). See D-033. | resolved → D-033 |
| Q-040 | **`@feature` mention syntax.** ~~Resolved~~. `@feat-XXX` strict ID + autocomplete dropdown. See D-034. | resolved → D-034 |
| Q-041 | **Diagnosis output format.** ~~Resolved~~. Structured report (`{ cause, suggestion, diff? }`). See D-035. | resolved → D-035 |
| Q-042 | **Retry-with-hint flow.** ~~Resolved~~. LLM produces diff → user confirms → re-run. See D-036. | resolved → D-036 |

### Architecture & runtime (D-037–D-040)

| ID | Question | Status |
|----|----------|--------|
| Q-043 | **Daemon startup mode.** ~~Resolved~~. Bound to Web UI launch (single `heddle start` command). See D-038. | resolved → D-038 |
| Q-044 | **Checkpoint storage.** ~~Resolved~~. SQLite. See D-039. | resolved → D-039 |
| Q-045 | **Node.js ↔ daemon transport.** ~~Resolved~~. WebSocket over loopback. See D-040. | resolved → D-040 |
| Q-046 | **Daemon crash restart supervisor.** ~~Resolved~~. Node.js detects WebSocket drop and respawns. See D-038. | resolved → D-038 |
| Q-047 | **Daemon-internal retry policy.** ~~Resolved~~. Exponential backoff, 3 attempts (1s → 2s → 4s). See D-041. | resolved → D-041 |
| Q-048 | **WebSocket authentication.** ~~Resolved~~. Refuse non-loopback binding in v0.1; future extension if needed. See D-042. | resolved → D-042 |
| Q-049 | **Launcher UX.** ~~Resolved~~. Print progress + auto-open browser. See D-043. | resolved → D-043 |

### DAG satisfiability edge cases (D-027)

| ID | Question | Status |
|----|----------|--------|
| Q-035 | **Should `pending` / `deferred` count as not-yet-passing for DAG purposes?** ~~Resolved~~. Per user: downstream cannot pass over an unfinished task regardless of *why* it is unfinished. Strict version of D-027 stands. | resolved → D-027 |

### Insertion-time conflicts (D-029)

| ID | Question | Status |
|----|----------|--------|
| Q-037 | **LLM behavior when bugfix `fixes` target is missing.** ~~Resolved~~. Three-layer defense: (1) `@feat-XXX` autocomplete at input time, (2) LLM surfaces the anomaly in dialog without generating drafts, (3) D-023 algorithm catches what slips through. See D-059 | resolved → D-059 |
| Q-038 | **LLM behavior when a draft semantically duplicates an existing feature.** ~~Resolved~~. LLM detects similarity; partial overlap suggests the new `enhancement` kind; strong duplicate prompts 3-way clarification (edit existing / create new / cancel); one extra confirmation before execution. See D-060 | resolved → D-060 |

### Re-decomposition context (D-013, D-020)

| ID | Question | Status |
|----|----------|--------|
| Q-026 | **LLM context for re-decomposition.** ~~Resolved~~. Two-phase model — see D-026. Re-decomposition sees drafts only; insertion sees full list. | resolved → D-026 |

### Out of scope but flagged

| ID | Question | Status |
|----|----------|--------|
| Q-015 | **Persistence.** ~~Resolved~~. `feature_list.json` lives at the root of each user-selected project folder (D-057). The path is per-project, not configurable globally — picking a different folder = different project | resolved → D-057 |
| Q-016 | **Multi-project.** ~~Resolved~~. One instance manages multiple projects. Startup directory is irrelevant; project registry is `~/.heddle/projects.json`; new projects added via folder picker (no directory creation). See D-057 | resolved → D-057 |
| Q-017 | **Auth / multi-user.** Parked — v0.1 is single-user (consistent with D-010 self-hosted framework and D-042 loopback-only). Multi-user collaboration is a future extension | parked-by-user |

---

## 4. What this document is NOT

- Not a tech-stack choice (e.g. React vs Vue, FastAPI vs Express).
- Not a UI mockup.
- Not a roadmap with timeline estimates.
- Not an agent prompt design (those live with the implementation).

When the time comes to make engineering decisions, they go in a
separate document (e.g. `TECH.md`) and reference decisions here.