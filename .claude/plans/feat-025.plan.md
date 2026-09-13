# Plan: feat-025 — Daemon-side logging with rotation

**Source PRD**: `feature_list.json` (feat-025)
**Selected Milestone**: feat-025 — Daemon-side logging with rotation
**Complexity**: Small (extends feat-015)

## Summary

Add the LLM-call audit log sidecar (`~/.heddle/logs/<project_id>/llm-audit.jsonl`) to the daemon. feat-015 already shipped `<project_id>.daemon.log` rotation; this PR adds the audit log that records every LLM call's `feature_id`, `model`, `prompt_tokens`, `completion_tokens`, `latency_ms`, `outcome`. Also ensures the per-project logs directory exists on daemon startup. The audit log uses its own simple JSON-line appender (no rotation in v0.1).

## Patterns to Mirror
| Category | Source | Pattern |
|---|---|---|
| Naming | `packages/common/heddle_common/log_rotation.py` (feat-015) | `RotatingFileSink` class; we mirror with non-rotating `JsonLineAppender` |
| Naming | `packages/common/heddle_common/logging.py:76-104` | `emit()` writes structured JSON line + redact filter |
| Naming | `packages/daemon/heddle_daemon/server.py:540-577` | `Daemon.start()` brings up per-project log sink after checkpoint setup |
| Errors | `packages/daemon/heddle_daemon/server.py:122-208` | `__post_init__` validation chokepoint |
| Tests | `packages/daemon/tests/test_log_rotation_wiring.py` (feat-015) | pytest file-per-module; uses `tmp_path` |
| File layout | `packages/daemon/heddle_daemon/` | One responsibility per file; new `llm_audit.py` |

## Files to Change
| File | Action | Why |
|---|---|---|
| `packages/daemon/heddle_daemon/llm_audit.py` | CREATE | `JsonLineAppender(path)` class + `LlmAuditLogger(logs_dir, project_id)` class. `record_call(feature_id, model, prompt_tokens, completion_tokens, latency_ms, outcome)` writes structured record to `<logs_dir>/<project_id>/llm-audit.jsonl`. Constants: `DEFAULT_LOGS_DIR = "~/.heddle/logs"`, env var `HEDDLE_LOGS_DIR` |
| `packages/daemon/heddle_daemon/server.py` | UPDATE | Add `logs_dir: Optional[Path]` to `DaemonConfig`. In `Daemon.start()`: ensure `<logs_dir>/<project_id>/` exists, construct `LlmAuditLogger`, store on `self._llm_audit` |
| `packages/daemon/heddle_daemon/agent_runtime.py` | UPDATE | Add `llm_audit: Optional[LlmAuditLogger]` to `AgentRuntime.__init__`. After each LLM call, append audit record with timing |
| `packages/daemon/tests/test_llm_audit.py` | CREATE | pytest tests: appender mechanics, audit record shape, redaction, dir auto-create |
| `packages/daemon/tests/test_llm_audit_wiring.py` | CREATE | Daemon-side wiring tests |
| `feature_list.json` | UPDATE via script | mark-in-progress → mark-passing |

## Tasks
1. **JsonLineAppender + LlmAuditLogger**: classes for audit logging
2. **Wire into Daemon.start() + DaemonConfig**: ensure logs dir, construct logger
3. **Wire into AgentRuntime**: record after each LLM call with `time.monotonic()` timing
4. **Validate + commit**

## Validation
```bash
cd packages/daemon && HEDDLE_FAKE_LLM=1 python -m pytest tests/test_llm_audit.py tests/test_llm_audit_wiring.py -v
cd packages/daemon && HEDDLE_FAKE_LLM=1 python -m pytest -q
python HARNESS/tools/handoff_check.py
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Audit log grows unbounded | Low | v0.2 concern; documented |
| Concurrent writes | Low | v0.1 single-active-project |
| Token counts absent | Low | Defensive: write null |
| Path differs from feat-015 | Low | Both go to `~/.heddle/logs/<project_id>/`; different filenames |

## Acceptance
- [ ] `llm_audit.py` exists with `JsonLineAppender`, `LlmAuditLogger`
- [ ] Daemon ensures logs dir exists on start
- [ ] agent_step writes JSON line with `{ts, feature_id, model, prompt_tokens, completion_tokens, latency_ms, outcome}`
- [ ] Redaction applied
- [ ] All tests pass; handoff_check 10/10
- [ ] Commit recorded