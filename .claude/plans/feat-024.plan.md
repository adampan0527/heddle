# Plan: feat-024 — Per-feature restart budget

**Source PRD**: `feature_list.json` (feat-024)
**Selected Milestone**: feat-024 — Per-feature restart budget (D-051)
**Complexity**: Medium

## Summary

Add a per-feature restart budget: when the daemon crashes and the supervisor (feat-027) respawns it, increment a restart counter persisted in `feature_list.json`'s `attempts[]`. After 3 restarts within any 10-minute sliding window for the same feature, transition that feature to `blocked` with a clear `blocked_reason`. Also detect checkpoint loss (missing/corrupt `checkpoints.db`) on respawn and transition the affected feature to blocked. The pure decision function `should_block(restarts: list[datetime])` is the load-bearing test target; the daemon wires it into the post-restart hook.

## Patterns to Mirror
| Category | Source | Pattern |
|---|---|---|
| Naming | `packages/daemon/heddle_daemon/agent_runtime.py:74-79` | `RECURSION_LIMIT_ENV_VAR` env-var constant + `get_max_steps_from_env` resolver — we mirror for restart budget |
| Naming | `packages/common/heddle_common/feature_list_io.py` (existing) | `mark-blocked --reason "..."` and `mark-regressed --reason "..."` subcommands — we wire the daemon to call `mark-blocked` on budget exhaustion |
| Errors | `packages/daemon/heddle_daemon/agent_runtime.py:167-176` | `RecursionLimitError` with structured `cause` field — we mirror with `RestartBudgetExceededError(cause="restart_budget", window_count=3)` |
| Errors | `packages/daemon/heddle_daemon/server.py:122-208` | `DaemonConfig.__post_init__` validates env vars eagerly — we mirror with `RestartBudgetConfig.__post_init__` |
| Tests | `packages/daemon/heddle_daemon/tests/test_checkpointing.py` | pytest file-per-module; uses `tmp_path` for isolated project dirs — we mirror |
| File layout | `packages/daemon/heddle_daemon/` | One responsibility per file; new `restart_budget.py` |
| Sliding window | `collections.deque` (stdlib) | Most natural structure: append on each restart, pop entries older than `now - 10 min`. O(1) amortized for the steady-state path |

## Files to Change
| File | Action | Why |
|---|---|---|
| `packages/daemon/heddle_daemon/restart_budget.py` | CREATE | Pure decision function `should_block(restart_times: list[datetime], window_minutes=10, max_restarts=3) -> bool`; `RestartBudgetCounter` class (deque-based sliding window) with `record_restart(now)` and `is_exhausted(now)` methods; `RestartBudgetConfig` dataclass with env-var resolution (HEDDLE_RESTART_WINDOW_MINUTES default 10, HEDDLE_RESTART_MAX_COUNT default 3); `RestartBudgetExceededError` exception with `cause="restart_budget"` |
| `packages/daemon/heddle_daemon/checkpointing.py` | UPDATE | Add `is_checkpoint_db_healthy(project_path: Path) -> bool` function — returns False if `checkpoints.db` is missing OR fails SQLite `PRAGMA integrity_check` |
| `packages/daemon/heddle_daemon/server.py` | UPDATE | New `Daemon._on_respawn(features_in_flight: list[str])` hook that fires on respawn: for each in-flight feature, increment counter, append attempts[outcome="regressed"]; if exhausted, mark blocked with reason. Also call `is_checkpoint_db_healthy` — if False, mark all in-flight blocked with "checkpoint loss" |
| `packages/daemon/tests/test_restart_budget.py` | CREATE | pytest tests: pure-function table for `should_block`, deque mechanics, env-var resolution, error shape, explicit spec tests (3 in 10min → blocked; 2 + 1 at 11min → NOT blocked) |
| `packages/daemon/tests/test_checkpoint_health.py` | CREATE | pytest tests for `is_checkpoint_db_healthy`: missing / corrupted / healthy DBs |
| `packages/daemon/tests/test_restart_recovery_wiring.py` | CREATE | Integration tests: 3 restarts → blocked; missing DB → all blocked; healthy → no change |
| `feature_list.json` | UPDATE via script | After tests pass: `mark-in-progress feat-024` → `mark-passing feat-024` |

## Tasks
### Task 1: Pure `should_block` decision function
- **Action**: `should_block(restart_times: list[datetime], *, window_minutes=10, max_restarts=3) -> bool`. Filter times to those within `now - window_minutes`. Return `len(filtered) >= max_restarts`.
- **Mirror**: `agent_runtime.py:74-79` (env-var pattern).
- **Validate**: pytest table-driven tests.

### Task 2: `RestartBudgetCounter` sliding window class
- **Action**: Wraps `collections.deque` of `datetime`s. Methods: `record_restart(now)`, `is_exhausted(now) -> bool`, `recent_restarts(now) -> list[datetime]`. Defaults from `RestartBudgetConfig` (window=10 min, max=3). Internal deque cap at 100 to bound memory.
- **Mirror**: `DaemonConfig.__post_init__` (server.py:157-208, validation).
- **Validate**: pytest deque mechanics + sliding-window behavior tests.

### Task 3: `RestartBudgetConfig` env-var resolver + error type
- **Action**: `@dataclass(frozen=True) RestartBudgetConfig` with `window_minutes: int = 10`, `max_restarts: int = 3`. `__post_init__` raises `ValueError` on bad values. `from_env()` reads `HEDDLE_RESTART_WINDOW_MINUTES` / `HEDDLE_RESTART_MAX_COUNT`. `class RestartBudgetExceededError(Exception)` with `cause="restart_budget"`, `window_count: int`, `window_minutes: int`.
- **Mirror**: `agent_runtime.py:74-79` + `RecursionLimitError` (agent_runtime.py:167-176).
- **Validate**: pytest config + error tests.

### Task 4: `is_checkpoint_db_healthy` in `checkpointing.py`
- **Action**: Function `is_checkpoint_db_healthy(project_path: Path) -> bool`. Returns False if `<project>/.heddle/checkpoints.db` is missing. Returns False if SQLite `PRAGMA integrity_check` returns anything other than `ok`. Returns True otherwise.
- **Mirror**: existing `project_checkpoint_path` helper (checkpointing.py:77+).
- **Validate**: pytest tests with `tmp_path` fixtures for missing / corrupted / healthy DBs.

### Task 5: `RestartRecoveryHook` in `server.py`
- **Action**: New method `Daemon._on_respawn(features_in_flight: list[str])`:
  1. Get current time `now = datetime.now(timezone.utc)`.
  2. Check `is_checkpoint_db_healthy(self._config.project_path)`. If False, mark every feature in `features_in_flight` blocked with `blocked_reason="checkpoint loss detected on daemon respawn"` and emit `restart_recovery_checkpoint_loss` warn event. Return early.
  3. Else, for each feature: increment restart counter, append `attempts[]` entry with `outcome="regressed"`, `note="daemon respawn"`, `at=now.isoformat()`. If `is_exhausted(now)`, mark that feature blocked with `blocked_reason="restart budget exhausted (3 in 10min)"` and emit `restart_recovery_blocked` warn event.
- **Mirror**: `Daemon._on_project_removed` (server.py:630-698) — same async hook shape, structured logging, fail-soft on close errors.
- **Validate**: pytest wiring tests with mocked `feature_list_io`.

### Task 6: End-to-end + handoff_check + commit
- **Action**:
  1. `python HARNESS/tools/handoff_check.py` → 10/10 PASS.
  2. `python HARNESS/tools/feature_list.py mark-in-progress feat-024` → `mark-passing feat-024`.
  3. Atomic commit.
  4. Append SESSION block.
- **Validate**: `git log --oneline -5` shows the new commit.

## Validation
```bash
cd packages/daemon && HEDDLE_FAKE_LLM=1 python -m pytest tests/test_restart_budget.py tests/test_checkpoint_health.py tests/test_restart_recovery_wiring.py -v
cd packages/common && HEDDLE_FAKE_LLM=1 python -m pytest -q
cd packages/daemon && HEDDLE_FAKE_LLM=1 python -m pytest -q
python HARNESS/tools/handoff_check.py
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Sliding window deque grows unbounded | Low | `RestartBudgetCounter` caps at 100 entries |
| `datetime.now()` UTC vs local inconsistently | Low | All `now` calls use `datetime.now(timezone.utc)`; tests inject `now` as parameter |
| Corrupt SQLite DB causes `PRAGMA integrity_check` to hang | Low | Wrap in `try/except sqlite3.DatabaseError`; fail-closed: corrupt → blocked |
| Three restarts in 10min block forever | Medium | Documented behavior; user can `mark-deferred` then `mark-passing` |
| `mark_regressed` race with agent_runtime writing attempts | Medium | Both writers use `feature_list_io.atomic_write` with `fcntl.flock` |
| `RestartRecoveryHook` reads `feature_list.json` from inside daemon | Low | Internal to `server.py`'s `start()` method; matches v0.1 single-active-project scope |
| Missing DB raises `FileNotFoundError` on `PRAGMA` | Low | Check `path.exists()` first |

## Acceptance
- [ ] `should_block(3 in 10min) == True`; `should_block(2 in 10min + 1 at 11min) == False`
- [ ] `is_checkpoint_db_healthy` correctly handles missing / corrupted / healthy DBs
- [ ] Daemon `_on_respawn` increments counter, appends attempts[], marks blocked on exhaustion
- [ ] On checkpoint loss, ALL in-flight features get blocked with reason
- [ ] All new pytest tests pass; no regressions
- [ ] `python HARNESS/tools/handoff_check.py` reports 10/10 PASS
- [ ] `python HARNESS/tools/feature_list.py mark-passing feat-024` succeeds; commit recorded