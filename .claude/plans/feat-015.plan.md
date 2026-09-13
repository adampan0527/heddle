# Plan: feat-015 — Log file rotation

**Source PRD**: `feature_list.json` (feat-015)
**Selected Milestone**: feat-015 — Log file rotation
**Complexity**: Medium

## Summary

Add size-based log file rotation across both the Python daemon and the Node.js supervisor. Default to 50 MB per file with 5 generations retained; both knobs overridable via `HEDDLE_LOG_MAX_BYTES` and `HEDDLE_LOG_BACKUP_COUNT` env vars. The Python side wires `logging.handlers.RotatingFileHandler` into the existing `heddle_common.logging` module's per-project file sink (the foundation laid by feat-005 / feat-014). The Node.js side implements an equivalent "hand-rolled" rotation: a tiny write-stream wrapper that tracks bytes-written, rotates on threshold, and prunes the oldest generations. Both sides expose the same env-var contract so a single deployment script controls rotation policy across the whole supervisor + daemon pair.

**Important invariant — file ownership**: the Python daemon writes to `<project_id>.daemon.log`, the Node.js supervisor writes to `<project_id>.node.log`. They NEVER share a path. This avoids Windows sharing violations and makes the two streams independently debuggable. feat-025 (which depends on feat-015) inherits this convention.

## Patterns to Mirror
| Category | Source | Pattern |
|---|---|---|
| Naming | `packages/common/heddle_common/logging.py:76-104` | `emit(level, component, event, msg, *, project_id, feature_id, **fields)` — structured one-line JSON; rotation must NOT change the on-stdderr contract, only the optional file sink |
| Naming | `packages/common/heddle_common/env_loader.py` | `get_env(name, default)`-style helper returning typed values; we mirror with `get_log_max_bytes()` / `get_log_backup_count()` returning `int`, raising `ValueError` on bad input (mirror `DaemonConfig.__post_init__` at `server.py:157-208`) |
| Naming | `packages/node/src/lib/logger.ts:74-93` | `emit(level, component, event, msg, fields)` writes to stderr; we add an optional `RotatingFileSink` companion that callers can attach without changing the stderr path |
| Errors | `packages/common/heddle_common/logging.py:61-73, 102` | `redact(obj)` is run BEFORE serialization (line 102: `payload = redact(payload)`); the file sink MUST receive the **already-redacted** payload, not raw — backups must contain the same redaction guarantees as stderr |
| Errors | `packages/daemon/heddle_daemon/server.py:122-208` | `__post_init__` raises `ValueError` on bad config; we mirror this for `max_bytes <= 0`, `backup_count <= 0`, or `max_bytes` exceeding a sane upper bound (1 GB) |
| Tests | `packages/common/heddle_common/tests/test_logging.py` | pytest file-per-module; asserts redaction + emit contract; rotation tests follow the same shape, in `tests/test_log_rotation.py` |
| Tests | `packages/node/tests/server.test.ts` | vitest file-per-module; existing convention uses `packages/node/tests/`, NOT sibling-to-source — we follow the established convention for Node (deviation from CODE_STYLE.md:209 noted; existing tests are grandfathered). New file: `packages/node/tests/rotating-file-sink.test.ts` |
| Logging | `packages/common/heddle_common/logging.py:76-104` | Stdout/stderr emit MUST stay synchronous + flushed; the file sink is opt-in and off the hot emit path until a feature explicitly enables it |
| Env-var precedence | `packages/daemon/heddle_daemon/server.py:901-914` | CLI flag > env var > defaults. **v0.1 ships env-var-only** for rotation knobs; CLI flags `--log-max-bytes` / `--log-backup-count` are deferred to a follow-up so the parser doesn't grow without need |
| File layout | `packages/daemon/heddle_daemon/` | One responsibility per file; `log_rotation.py` lives alongside `logging.py` in `heddle_common` (cross-language contract). Rotation logic in a single class (~80 lines) — split into a helper only if the rotate() method itself exceeds 40 lines |

## Files to Change
| File | Action | Why |
|---|---|---|
| `packages/common/heddle_common/log_rotation.py` | CREATE | `RotatingFileSink` class wrapping `logging.handlers.RotatingFileHandler`; reads `HEDDLE_LOG_MAX_BYTES` (default 50 MB) + `HEDDLE_LOG_BACKUP_COUNT` (default 5) via `get_log_max_bytes()` / `get_log_backup_count()`; exposes `emit(record_dict: dict)` and `close()`. Validates config in `__init__` (raises `ValueError`); pure stdlib, no new deps |
| `packages/common/heddle_common/logging.py` | UPDATE | Add module-level `_file_sink: RotatingFileSink | None` + `attach_file_sink(path, *, max_bytes=None, backup_count=None)` and `detach_file_sink()`. The `emit()` function gains ONE new line at the end (after `redact()` is applied): `if _file_sink is not None: _file_sink.emit(redacted)`. attach/detach are idempotent (second attach closes the previous sink; detach with no sink is a no-op). Stdout/stderr behavior is unchanged when no sink is attached |
| `packages/common/heddle_common/tests/test_log_rotation.py` | CREATE | pytest tests: (a) rotation triggers at byte boundary (≤1 line overrun allowed), (b) generations numbered `.1`/`.2`/.../`.N`, (c) oldest pruned at `backupCount + 1`, (d) env-var override path, (e) `redact` runs before write — `{api_key: 'sk-real-secret'}` MUST NOT appear in any backup file, (f) idempotent attach closes the first sink, (g) detach with no sink is no-op, (h) `max_bytes <= 0` / `backup_count <= 0` / `max_bytes > 1 GB` raise `ValueError`, (i) sink fixture `tmp_path` + `monkeypatch` resets module state between tests |
| `packages/daemon/heddle_daemon/server.py` | UPDATE | In `Daemon.start()`, after checkpoint store setup (line ~556), if `config.project_path` is set, resolve project_id via `projects_io.list_projects()` matching the path. If a match exists, attach a `RotatingFileSink` at `~/.heddle/logs/<project_id>.daemon.log`. If NO match: log a structured `project_log_sink_skipped` warn event with the path and continue (daemon still serves — fallback is graceful). Store the sink on `self._log_sink`. In `Daemon.stop()`, call `detach_file_sink()` and `self._log_sink.close()`. In `_on_project_removed` (line 630), BEFORE step 1 (signal stop_events), call `self._log_sink.close()` and `detach_file_sink()` so the sink does not leak after the project is removed. **For v0.1, no re-attach after a new project is added** — the daemon must be restarted for a different project; this matches the v0.1 single-project-at-a-time scope in DESIGN.md |
| `packages/daemon/tests/test_log_rotation_wiring.py` | CREATE | Daemon-side wiring tests: (a) sink attached on start with project_path, (b) NO attach when project_path is None (skeleton-only mode), (c) NO attach + warn log when project_id resolution fails, (d) env-var override honored, (e) close on stop, (f) close on `_on_project_removed` hook |
| `packages/node/src/lib/rotating-file-sink.ts` | CREATE | Hand-rolled `RotatingFileSink` class (~80 lines incl. comments). State: `path`, `maxBytes`, `backupCount`, `bytesWritten`, `fd: number | null`. `write(line: string)`: write to fd, update `bytesWritten`; when `bytesWritten >= maxBytes`, call `rotate()`. `rotate()`: `fs.closeSync(fd)`, then `for i = backupCount down to 1: rename('<path>.<i-1>', '<path>.<i>')` (use `<path>` when `i === 1`); if `<path>.<backupCount>` already exists, `unlink` it before the rename chain. Reopen fd at `<path>`, reset `bytesWritten = 0`. `close()` flushes + closes; idempotent (close after close is no-op). Reads env vars `HEDDLE_LOG_MAX_BYTES` / `HEDDLE_LOG_BACKUP_COUNT` with same defaults. Validates config in constructor (throws `Error` on bad input) |
| `packages/node/src/lib/logger.ts` | UPDATE | Add module-level `_fileSink: RotatingFileSink | null` + `attachFileSink(sink)` / `detachFileSink()`. `emit()` adds ONE line after `process.stderr.write`: `if (_fileSink) _fileSink.write(JSON.stringify(redacted) + "\n")`. attach/detach are idempotent |
| `packages/node/tests/rotating-file-sink.test.ts` | CREATE | vitest tests: (a) rotation boundary (≤1 line overrun), (b) generations numbered `.1`/`.2`/.../`.N`, (c) oldest pruned at `backupCount + 1`, (d) close-before-rename on Windows (mock `fs.renameSync` to confirm `fs.closeSync` is called first), (e) close is idempotent, (f) `maxBytes <= 0` / `backupCount <= 0` throws |
| `packages/node/tests/logger.test.ts` | CREATE | New file — Node.js logger has no test today. Tests: (a) stderr emit unchanged when no sink attached (existing tests pass byte-for-byte), (b) attach → emit writes to sink + stderr, (c) sink receives the **redacted** payload (assert `{api_key: 'sk-real'}` does not appear in the sink output), (d) double attach closes the first sink, (e) detach with no sink is no-op, (f) detach closes the previous sink |
| `packages/node/src/main.ts` | UPDATE | On supervisor startup, AFTER `supervisor.start()` succeeds (so daemon port is confirmed live), attach a single `RotatingFileSink` at `~/.heddle/logs/<active_project_id>.node.log` for the currently-active project. v0.1 single-project scope means one sink per supervisor lifetime; project-switch (feat-034) closes the sink and re-attaches. On `SIGTERM`/`SIGINT`, the existing `shutdown` async function (line 299) closes the sink via `detachFileSink()` |
| `packages/node/tests/main-log-rotation.test.ts` | CREATE | vitest integration test: stub `process.env.HEDDLE_LOG_MAX_BYTES`, invoke the supervisor startup hook with a fake `projects_io.listProjects()` (or equivalent JS reader — see "JS-side projects registry" callout below), assert the sink is attached at the expected path with the expected `maxBytes` |
| `HARNESS/CODE_STYLE.md` | NO-OP | Rotation is a logging extension; existing patterns cover it. No rule changes needed |
| `feature_list.json` | UPDATE via script | After tests pass, run `python HARNESS/tools/feature_list.py mark-in-progress feat-015` then `mark-passing feat-015` — script handles metadata |

**JS-side projects registry callout**: there is no Node.js equivalent of `heddle_common.projects_io.listProjects()` today (it's a Python module). For feat-015 we add the smallest possible bridge: read the same `~/.heddle/projects.json` file (Python writes it; feat-012 documents the schema) via `fs.readFileSync` in `packages/node/src/lib/projects-registry.ts`. This is a ~30-line reader that returns a `[{id, name, path, ...}]` array. It is NOT a sync of the full Python module — it only reads the JSON file, which is the only thing the supervisor needs to find the active project. This unblocks feat-015 and is the foundation for feat-050/051 (which need the same data).

## Tasks
### Task 1: Python `RotatingFileSink` (`heddle_common.log_rotation`)
- **Action**: Pure-stdlib class wrapping `RotatingFileHandler`. Constructor takes `(path, max_bytes=DEFAULT, backup_count=DEFAULT)`. Reads env vars `HEDDLE_LOG_MAX_BYTES` / `HEDDLE_LOG_BACKUP_COUNT` via `get_log_max_bytes()` / `get_log_backup_count()` helpers. Validates config in `__init__`: `max_bytes > 0`, `max_bytes <= 1 GB`, `backup_count > 0`, `backup_count <= 100`; raises `ValueError` with a clear message. `emit(record)` re-emits through the handler's `emit()` after applying the existing `redact()` filter, so the JSON shape + redaction guarantees are identical to the stderr stream. `close()` flushes + closes. Constants `DEFAULT_LOG_MAX_BYTES = 50 * 1024 * 1024` and `DEFAULT_LOG_BACKUP_COUNT = 5` exported from the module.
- **Mirror**: `logging.py:76-104` (redact-before-write) + `env_loader.py` (typed env-var readers) + `DaemonConfig.__post_init__` (`server.py:157-208`, validation pattern).
- **Validate**: `cd packages/common && HEDDLE_FAKE_LLM=1 python -m pytest tests/test_log_rotation.py -v` — all green.

### Task 2: Wire sink into `heddle_common.logging`
- **Action**: Add module-level `_file_sink: RotatingFileSink | None` + `attach_file_sink(path, *, max_bytes=None, backup_count=None)` and `detach_file_sink()`. The `emit()` function gains ONE new line at the end (after `redact()`): `if _file_sink is not None: _file_sink.emit(redacted)`. The sink receives the **already-redacted** payload — do NOT re-redact. Stdout behavior is unchanged when no sink is attached. attach/detach are idempotent (second attach closes the previous sink; detach with no sink is a no-op).
- **Mirror**: `logging.py:76-104` (the emit function shape stays byte-identical for existing tests when no sink is attached).
- **Validate**: Re-run `pytest packages/common/tests/test_logging.py` — no regression. Extend `tests/test_logging.py` with three new tests: (a) attach + emit writes to sink + stderr, (b) sink receives redacted payload, (c) detach with no sink is no-op.

### Task 3: Daemon-side wiring in `server.py`
- **Action**: In `Daemon.start()`, after checkpoint store setup (line ~556), if `config.project_path` is set, resolve project_id via `projects_io.list_projects()` matching the path. If a match exists, attach a `RotatingFileSink` at `~/.heddle/logs/<project_id>.daemon.log`. If NO match: log a structured `project_log_sink_skipped` warn event with the path and continue (daemon still serves — fallback is graceful, NO silent failure). Store the sink on `self._log_sink`. In `Daemon.stop()`, call `detach_file_sink()` and `self._log_sink.close()`. In `_on_project_removed` (line 630), BEFORE step 1 (signal stop_events), close the sink via `self._log_sink.close()` and `detach_file_sink()` so the sink does not leak after the project is removed. **v0.1: no re-attach after a new project is added** — daemon must be restarted for a different project (matches DESIGN.md single-project-at-a-time scope).
- **Mirror**: `server.py:540-577` (start checkpoint store + emit `checkpoint_store_ready`) + `server.py:579-615` (stop teardown) + `server.py:630-698` (`_on_project_removed` cascade).
- **Validate**: New `test_log_rotation_wiring.py` covers: (a) attach on start with project_path + matching project, (b) NO attach when project_path is None, (c) NO attach + warn log when project_id resolution fails, (d) env-var override honored, (e) close on stop, (f) close on `_on_project_removed` hook.

### Task 4: JS-side projects registry reader (`heddle_node.lib.projects_registry`)
- **Action**: `packages/node/src/lib/projects-registry.ts` exports `listProjects(): Project[]` reading `~/.heddle/projects.json` via `fs.readFileSync` (sync because the supervisor startup is sequential). Returns an empty array if the file is missing (warn-logged but not raised — a fresh install has no projects). `Project` interface: `{id: string, name: string, path: string, added_at: string, last_accessed_at: string}`. Schema mirrors `heddle_common.projects_io.Project`. Validates each entry's required fields; entries with missing fields are dropped with a warn log.
- **Mirror**: `heddle_common.projects_io.list_projects` — return shape matches.
- **Validate**: vitest test `packages/node/tests/projects-registry.test.ts`: writes a fake `projects.json`, calls `listProjects()`, asserts the array shape; tests missing-file → empty array; tests malformed entries are dropped.

### Task 5: Node.js `RotatingFileSink` (hand-rolled equivalent)
- **Action**: `packages/node/src/lib/rotating-file-sink.ts` exports `RotatingFileSink` class (~80 lines incl. comments). State: `path`, `maxBytes`, `backupCount`, `bytesWritten`, `fd: number | null`. `write(line: string)`: write to fd, update `bytesWritten`; when `bytesWritten >= maxBytes`, call `rotate()`. `rotate()`: `fs.closeSync(fd)`, then `for i = backupCount down to 1: rename('<path>.<i-1>', '<path>.<i>')` (use `<path>` when `i === 1`); if `<path>.<backupCount>` already exists, `unlink` it before the rename chain. Reopen fd at `<path>`, reset `bytesWritten = 0`. `close()` flushes + closes; idempotent (close after close is no-op). Reads env vars `HEDDLE_LOG_MAX_BYTES` / `HEDDLE_LOG_BACKUP_COUNT` with same defaults. Validates config in constructor (throws `Error` on bad input: `maxBytes <= 0`, `backupCount <= 0`, `maxBytes > 1 GB`).
- **Mirror**: `logger.ts:74-93` (signature `write(line: string)` mirrors `process.stderr.write`). No external deps — pure Node `fs`/`path`.
- **Validate**: `pnpm --filter node test rotating-file-sink.test.ts` — covers (a) boundary ≤1 line overrun, (b) generations count, (c) prune oldest, (d) atomic rename order with `fs.closeSync` BEFORE `fs.renameSync` (mock assertion), (e) close idempotent, (f) env override, (g) config validation throws.

### Task 6: Wire sink into Node.js logger
- **Action**: `logger.ts` gains a module-level `_fileSink: RotatingFileSink | null` + `attachFileSink(sink)` / `detachFileSink()`. `emit()` adds ONE line after `process.stderr.write`: `if (_fileSink) _fileSink.write(JSON.stringify(redacted) + "\n")`. The sink receives the redacted payload (the existing `redact()` at line 91 is run before this new line). attach/detach are idempotent (second attach closes the first sink; detach with no sink is no-op).
- **Mirror**: `logger.ts:74-93` (unchanged stderr behavior).
- **Validate**: New `packages/node/tests/logger.test.ts` covers (a) stderr emit unchanged when no sink attached, (b) attach + emit writes to sink + stderr, (c) sink receives redacted payload (assert `{api_key: 'sk-real'}` does not appear in sink output), (d) double attach closes the first sink, (e) detach with no sink is no-op, (f) detach closes the previous sink.

### Task 7: Node.js supervisor wiring in `main.ts`
- **Action**: After `supervisor.start()` succeeds, look up the active project via the new `projects_registry.listProjects()` (Task 4). For the first project (v0.1 single-project scope), attach a `RotatingFileSink` at `~/.heddle/logs/<project_id>.node.log`. Store the sink on the supervisor so it can be closed on shutdown. The active-project lookup uses the same `last_accessed_at` ordering as `projects_io.list_projects()`. Env vars read once at startup. On `SIGTERM`/`SIGINT`, the existing `shutdown` async function (line 299) closes the sink via `detachFileSink()`. **v0.1: project-switch (feat-034) closes the sink and re-attaches** — this is wired by feat-034 itself; feat-015 only ships the single-active-project path.
- **Mirror**: `main.ts` current pattern for supervisor + Fastify startup (analogous to daemon `start()`); `main.ts:299-318` shutdown function.
- **Validate**: vitest integration test in `packages/node/tests/main-log-rotation.test.ts`: stub `process.env.HEDDLE_LOG_MAX_BYTES`, stub `projects_registry.listProjects()` to return a fake project, invoke the supervisor startup hook, assert the sink is attached at the expected path with the expected `maxBytes`. Cover shutdown: invoke the shutdown function, assert the sink is closed.

### Task 8: End-to-end test + handoff_check + commit
- **Action**:
  1. Run `python HARNESS/tools/handoff_check.py` — must report 10/10 PASS before declaring done.
  2. Mark feat-015 in-progress via `mark-in-progress feat-015`; after all tests green, `mark-passing feat-015`.
  3. Atomic commit: `git add -- feature_list.json current_progress.txt packages/{common,daemon,node} HARNESS && git commit -m "Implement: feat-015 log file rotation (T-017)"`.
  4. Append SESSION block to `current_progress.txt` per the session-end protocol.
- **Mirror**: `HARNESS/HARNESS.md` "Checkpoint Protocol" + `tools/session_end.py` flow.
- **Validate**: `git log --oneline -5` shows the new commit; `python HARNESS/tools/feature_list.py status` shows `passing: 31` (was 30).

## Validation
```bash
# typecheck (Node)
cd packages/node && pnpm typecheck

# Python: rotation module + logging + daemon wiring tests
cd packages/common && HEDDLE_FAKE_LLM=1 python -m pytest tests/test_log_rotation.py tests/test_logging.py -v
cd packages/daemon && HEDDLE_FAKE_LLM=1 python -m pytest tests/test_log_rotation_wiring.py -v

# Node: rotation + logger + supervisor wiring
cd packages/node && pnpm --filter node test rotating-file-sink.test.ts logger.test.ts main-log-rotation.test.ts projects-registry.test.ts

# Full suites (no regression)
cd packages/common && HEDDLE_FAKE_LLM=1 python -m pytest -q
cd packages/daemon && HEDDLE_FAKE_LLM=1 python -m pytest -q
cd packages/node && pnpm test

# handoff_check 10/10 PASS gate
python HARNESS/tools/handoff_check.py

# Manual smoke (in a separate shell):
#   1. start heddle-node (supervisor + daemon)
#   2. tail -F ~/.heddle/logs/<project_id>.daemon.log ~/.heddle/logs/<project_id>.node.log
#   3. trigger 60 MB of logs (e.g. issue ~100k daemon `info` events)
#   4. assert files <project_id>.daemon.log.1 .. .5 AND <project_id>.node.log.1 .. .5 exist,
#      each <= 50 MB, .6 absent for both
#   5. assert all backup files contain redacted redaction patterns (no api_key leaked)
#   6. assert the two filenames are DIFFERENT (no shared-file ownership)
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Rotation on a slow disk blocks the emit path | Medium | Both sinks are non-blocking on the steady-state path (write returns immediately, rotate is in-line but O(1) renames). The Python `RotatingFileHandler` does an in-process rename + reopen — fast on local FS. Python handler caches `bytesWritten` in memory, so `shouldRollover()` does NOT call `os.stat` per emit; overhead is one int comparison + occasionally one `os.fstat` after write |
| Rename races on Windows (open file handle) | Medium | The Node.js sink holds the fd in append mode + closes BEFORE rename via `fs.closeSync(fd)` then `fs.renameSync`. Verified by mock-assertion test `renames_after_close`. Python `RotatingFileHandler` already handles this internally on Windows (the stdlib handler is platform-aware) |
| Existing tests that assert the EXACT log payload break when a sink is attached | Low | `attach_file_sink` is opt-in and not called in any existing test path. The stderr contract is unchanged. We add new tests for the file-sink behavior; old tests stay green. Module-level sink is reset via `detach_file_sink()` fixture so tests start clean |
| Config drift between Python env-var name and Node.js env-var name | Low | Same two names on both sides by design; documented in module headers. `test_log_rotation.py::test_env_var_names_match_node_doc` reads the docstring of `rotating-file-sink.ts` (via `fs.readFileSync`) and asserts the names match |
| `RotatingFileSink` held by stale references after project remove | Medium | The daemon's `_on_project_removed` hook (feat-014 cascade) calls `detach_file_sink()` AND closes the sink BEFORE signaling stop_events. The Node.js side closes the sink in the project-switch handler (feat-034). Verified by `test_log_rotation_wiring.py::test_project_removal_closes_sink` |
| Concurrent writes from daemon AND Node.js supervisor to the SAME log file (Windows sharing violation) | **Resolved by design** | Python daemon writes to `<project_id>.daemon.log`, Node.js writes to `<project_id>.node.log`. Different filenames, no shared fd. Documented in this plan's "Important invariant" header |
| A test writes 60 MB to /tmp and flakes the suite | Medium | Default `max_bytes` in tests is 1024 bytes (NOT 50 MB) — tests use a tiny `max_bytes` override. The smoke step is the ONLY place that exercises 50 MB |
| Adding `attach_file_sink` mutates the import-time `logging.py` module state, breaking parallel test runs | Low | pytest runs tests sequentially in one process; the module-level sink is reset via a `monkeypatch` + `detach_file_sink()` fixture so each test starts with no sink attached. The fixture is co-located in `test_log_rotation.py::sink_isolation` |
| `pnpm test -- file.test.ts` syntax error (Vitest doesn't accept `--`) | **Resolved by design** | Plan uses `pnpm --filter node test file.test.ts` (single positional, no `--`) |
| Rotation boundary is "≤1 line overrun", not "exact maxBytes" — a test that asserts `bytes_written === max_bytes` will flake | Medium | Tests assert `bytes_written <= max_bytes + MAX_LINE_SIZE` where `MAX_LINE_SIZE = 4096` (any single log line is much smaller than this). Acceptance criterion item 5 softened from "exact" to "≤1 line over" |
| Node.js has no `projects_io` equivalent — feat-015 needs project_id resolution on the JS side | **Resolved by design** | New `packages/node/src/lib/projects-registry.ts` reads `~/.heddle/projects.json` directly. ~30 lines, schema-mirrors `heddle_common.projects_io.Project` |

## Acceptance
- [ ] `packages/common/heddle_common/log_rotation.py` exists, exports `RotatingFileSink`, `DEFAULT_LOG_MAX_BYTES=50*1024*1024`, `DEFAULT_LOG_BACKUP_COUNT=5`, `get_log_max_bytes()`, `get_log_backup_count()`
- [ ] `get_log_max_bytes()` / `get_log_backup_count()` raise `ValueError` on bad input (`max_bytes <= 0`, `max_bytes > 1 GB`, `backup_count <= 0`, `backup_count > 100`)
- [ ] `heddle_common.logging.emit()` writes to stderr identically when no sink is attached (existing tests pass byte-for-byte)
- [ ] When a sink is attached, every emitted line is ALSO written to the sink with redaction applied (verified by `{api_key: 'sk-real-secret'}` assertion in test)
- [ ] The daemon attaches a sink at `~/.heddle/logs/<project_id>.daemon.log` on `start()` (when project_path is set AND a registered project matches) and detaches on `stop()`
- [ ] When project_id resolution fails, the daemon emits a `project_log_sink_skipped` warn event and continues to serve (no silent failure)
- [ ] The daemon's `_on_project_removed` hook closes the sink before signaling stop_events
- [ ] `packages/node/src/lib/projects-registry.ts` exists; reads `~/.heddle/projects.json`; returns `[]` on missing file
- [ ] `packages/node/src/lib/rotating-file-sink.ts` exists; hand-rolled (no new npm deps); rotation triggers at `bytesWritten >= maxBytes` (≤1 line overrun); oldest backup pruned at `backupCount + 1`; `fs.closeSync` called BEFORE `fs.renameSync`
- [ ] `packages/node/src/lib/logger.ts` accepts an optional `RotatingFileSink` via `attachFileSink` / `detachFileSink`; stderr behavior unchanged
- [ ] New `packages/node/tests/logger.test.ts` exists and covers: stderr-only when no sink, attach + sink + stderr, sink receives redacted payload, double-attach closes first, detach no-op, detach closes previous
- [ ] Node.js supervisor attaches a sink at `~/.heddle/logs/<project_id>.node.log` on startup; closes on `SIGTERM`/`SIGINT`
- [ ] Env vars `HEDDLE_LOG_MAX_BYTES` and `HEDDLE_LOG_BACKUP_COUNT` are honored on both sides; bad values raise `ValueError` (Python) / `Error` (Node)
- [ ] All new pytest + vitest tests pass; no regressions in `test_logging.py`, `test_server.py`, `routes.test.ts`, `supervisor.test.ts`
- [ ] `python HARNESS/tools/handoff_check.py` reports 10/10 PASS
- [ ] `pnpm --filter node typecheck` clean; `python -m pytest` in `packages/common` and `packages/daemon` clean
- [ ] Manual smoke: writing >50 MB produces `.1`..`.5` backups (≤1 line overrun), each ≤50 MB + 4 KB, no `.6` file; daemon and node log files have DIFFERENT filenames; no api_key appears in any backup
- [ ] `python HARNESS/tools/feature_list.py mark-passing feat-015` succeeds; `metadata.passing` increments by 1; SESSION block appended to `current_progress.txt`; atomic commit `Implement: feat-015 log file rotation (T-017)` exists in `git log --oneline -5`