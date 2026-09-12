# Plan: feat-029 — Browser↔Node.js WebSocket Event Stream

**Source PRD**: `feature_list.json` (feat-029)
**Selected Milestone**: feat-029 — Browser↔Node.js WebSocket event stream
**Complexity**: Medium

## Summary
Add a `/ws` endpoint on the Fastify server (already registered via `@fastify/websocket`) that bridges the daemon event stream from the supervisor to connected browser clients. The browser connects once and receives `feature_attempt_started`, `feature_progress`, `feature_done`, `feature_failed`, `feature_stopped`, `log_line`, `dialog_token`, `dialog_done` events tagged by `project_id`. Outbound, the browser sends `dialog_turn` and `start_feature`/`stop_feature`/`retry_feature` envelopes that the server forwards to the daemon via `supervisor.request()` or `supervisor.sendCommand()`. Multi-client fan-out is filtered by `project_id` so a client viewing project A does not see project B events.

## Patterns to Mirror
| Category | Source | Pattern |
|---|---|---|
| Naming | `packages/node/src/protocol.ts:55-110` | Discriminated-union payload types per `DaemonEvent` variant; one interface per event with `event` as the discriminant literal |
| Naming | `packages/node/src/supervisor.ts:134-147` | Type-safe `EventEmitter` with `on<E>` overloads for typed payloads; pattern reused verbatim for the new `BrowserWsBridge` |
| Errors | `packages/node/src/routes/dialog.ts:66-103` | `forwardOrFail(supervisor, type, payload, reply)` chokepoint; route handlers return `ApiOk<T> | ApiErr` envelopes |
| Errors | `packages/node/src/server.ts:71-73` | `as any` cast on the Fastify instance to install `TypeBoxValidatorCompiler` without forcing the whole framework type to carry the provider |
| Tests | `packages/node/src/supervisor-request.test.ts` | vitest file-per-class; mock the supervisor + WS via a fake `WebSocket`-shaped object |
| Tests | `packages/node/src/routes.test.ts` | Use `buildServer({ supervisor: fakeSupervisor })` then `app.inject()` / `app.server` for WS handshake |
| Logging | `packages/node/src/main.ts:115-265` | Exhaustive switch on `DaemonEventRecord.event` routed into `logger.{info,warn,debug}`; `assertNeverDaemonEvent` enforces exhaustiveness at compile time |
| Plugin registration | `packages/node/src/server.ts:82-103` | `await app.register(registerXxxRoutes as any, { supervisor })` — same shape for the new `registerBrowserWs` plugin |

## Files to Change
| File | Action | Why |
|---|---|---|
| `packages/node/src/browser-ws.ts` | CREATE | `BrowserWsBridge` class: tracks connected clients, subscribes to `supervisor.on("daemon-event", …)`, fans events out filtered by `project_id`; handles inbound `dialog_turn` / `start_feature` / `stop_feature` / `retry_feature` envelopes |
| `packages/node/src/routes/ws.ts` | CREATE | Fastify plugin that upgrades `/ws` to a WebSocket via `@fastify/websocket` and registers the connection with `BrowserWsBridge` |
| `packages/node/src/server.ts` | UPDATE | Register the new WS plugin in `buildServer()` |
| `packages/node/src/protocol.ts` | UPDATE | Add `BrowserCommand` discriminated-union type (`dialog_turn`, `start_feature`, `stop_feature`, `retry_feature`) and the wire envelope helpers for browser → daemon forwarding |
| `packages/node/src/main.ts` | UPDATE | Construct `BrowserWsBridge` after supervisor + Fastify are wired; pass to the WS plugin |
| `packages/node/src/browser-ws.test.ts` | CREATE | vitest tests: multi-client fan-out, project_id filtering, outbound command forwarding, malformed inbound frames, project_id echo on events |
| `packages/node/src/routes/ws.test.ts` | CREATE | vitest tests: `/ws` upgrade via `app.server`, malformed envelopes rejected, bridge teardown on client disconnect |

## Tasks
### Task 1: Extend `protocol.ts` with browser-command envelope types
- **Action**: Add `BrowserCommandType` union ("dialog_turn" | "start_feature" | "stop_feature" | "retry_feature"), `BrowserCommandEnvelope` interface (`{v:1, type, project_id, ...payload}`), and `parseBrowserCommand(json): BrowserCommandEnvelope | null` validator. Re-export `DaemonEvent` shapes as-is — no new daemon events needed for feat-029.
- **Mirror**: `protocol.ts:55-110` discriminated-union style; per-shape `parse*` returns `null` on malformed input rather than throwing.
- **Validate**: `pnpm --filter node typecheck` clean.

### Task 2: Implement `BrowserWsBridge`
- **Action**: Class in `packages/node/src/browser-ws.ts` that:
  - Holds a `Set<BrowserClient>` keyed implicitly by socket reference; each client has a `project_id: string | null` set from the first inbound envelope (a client without a project_id receives no events until it sends one).
  - On `supervisor.on("daemon-event", record)`: filter by `record.project_id === client.project_id` and `ws.send(JSON.stringify(envelope))` to each match.
  - On inbound WS `message`: parse with `parseBrowserCommand`; if `dialog_turn` call `supervisor.request("dialog_turn", {project_id, message})` and write the response envelope back; if `start_feature`/`stop_feature`/`retry_feature` call `supervisor.sendCommand(type, payload)` (fire-and-forget per feat-030 contract).
  - On `ws.close`: remove the client from the set; clear any pending `dialog_turn` request timeout.
- **Mirror**: `supervisor.ts:284-342` (`request<T>`) + `supervisor.ts:362-380` (`sendCommand`) — same req_id / timeout semantics; per-client pending request map.
- **Validate**: vitest unit tests with a fake `DaemonSupervisor` mock — assert fan-out, project_id filtering, outbound command forwarding, malformed-input rejection, and disconnect cleanup.

### Task 3: Fastify WS route at `/ws`
- **Action**: `packages/node/src/routes/ws.ts` exports `registerBrowserWs: FastifyPluginAsync<{ bridge: BrowserWsBridge }>`. Inside, `app.get("/ws", { websocket: true }, (connection, req) => { bridge.add(connection.socket); … })`. The route does not validate headers beyond the WS upgrade itself — auth is loopback-only per feat-026.
- **Mirror**: `routes/dialog.ts:61-103` plugin shape; `as any` cast on `app` per the TypeBox provider workaround.
- **Validate**: vitest integration test using `app.server` + a real `ws` client to drive the upgrade.

### Task 4: Wire bridge into `buildServer` + `main.ts`
- **Action**: `buildServer({ supervisor, bridge })` accepts an optional bridge (so existing tests can pass `undefined`). `main.ts` constructs `new BrowserWsBridge(supervisor)` after `await buildServer()` but before `supervisor.start()` so subscriptions are attached before any daemon events arrive. Pass the bridge into a re-built server via a `setBridge()` shim, OR refactor `buildServer` to take both — prefer the latter (small change, removes the shim).
- **Mirror**: `main.ts:55` (supervisor construction) + `server.ts:82-103` (plugin registration).
- **Validate**: `pnpm --filter node build` clean; manual `pnpm --filter node dev` shows `/ws` upgrades succeed.

### Task 5: Tests + handoff_check
- **Action**: Cover happy path (one client, one project, event round-trip), multi-client (two clients on the same project both receive the event), project filtering (event for project A reaches client viewing A only), outbound command (browser sends `start_feature`, bridge calls `supervisor.sendCommand("start_feature", …)` with matching payload), malformed input (invalid JSON → drop, missing `type` → drop, unknown `type` → drop), and disconnect cleanup. Run `python HARNESS/tools/handoff_check.py` to confirm no invariant drift.
- **Mirror**: `routes.test.ts` style (use `app.inject` for HTTP, `app.server` for WS).
- **Validate**: `pnpm --filter node test` green; handoff_check 10/10.

## Validation
```bash
# typecheck
cd packages/node && pnpm typecheck

# unit tests for the new bridge + WS route
cd packages/node && pnpm test -- browser-ws.test.ts routes/ws.test.ts

# full Node.js test suite (no regressions)
cd packages/node && pnpm test

# handoff_check invariant gate
python HARNESS/tools/handoff_check.py

# full Python test suite (no regressions in daemon routes that the new task depends on)
cd packages/daemon && HEDDLE_FAKE_LLM=1 python -m pytest -q

# feat-029 spec: full end-to-end smoke (manual, in a separate shell):
#   1. start heddle-node (supervisor + daemon + WS bridge)
#   2. open a `wscat -c ws://127.0.0.1:5174/ws` session
#   3. send {"v":1,"type":"dialog_turn","project_id":"<id>","message":"hi"}
#   4. assert the daemon's dialog_token/dialog_done events arrive < 100ms later,
#      tagged with the same project_id
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Memory leak from per-client pending request Map on disconnect | Medium | Mirror `supervisor._rejectAllPendingRequests`: any pending dialog_turn promise is rejected on `ws.close` so the Map entry is dropped deterministically |
| Fan-out to many slow clients blocks the supervisor emitter | Medium | Use synchronous `ws.send` (small frames) but wrap each send in try/catch; a broken client throws and is dropped on the next event tick |
| Project_id spoofing (browser claims a project_id it shouldn't see) | Low in v0.1 (single-user, loopback) | Accept all project_ids at the WS layer; loopback-only is enforced at the bind in feat-026. Document the assumption in `browser-ws.ts` header comment. |
| Existing routes tests break after the `buildServer` signature change | Low | All existing call sites pass `{ supervisor }` only; make `bridge` optional with a default no-op `BrowserWsBridge` that drops messages |
| TypeBox plugin plumbing fights the WS handler | Low | WS frames are validated by the bespoke `parseBrowserCommand` (not TypeBox); route handler stays a single `app.get("/ws", { websocket: true }, …)` with no schema |

## Acceptance
- [ ] Browser opens a single WS to `ws://127.0.0.1:5174/ws` and stays open across the full session
- [ ] Daemon-emitted events (`feature_attempt_started`, `feature_progress`, `feature_done`, `feature_failed`, `feature_stopped`, `log_line`, `dialog_token`, `dialog_done`) reach the browser within 100ms
- [ ] Two clients viewing different projects each receive only their project's events
- [ ] Browser-sent `dialog_turn` envelopes reach the daemon via `supervisor.request("dialog_turn", …)` and the daemon's reply is forwarded back to the same client
- [ ] Browser-sent `start_feature` / `stop_feature` / `retry_feature` envelopes reach the daemon via `supervisor.sendCommand(...)` (no response awaited)
- [ ] Malformed inbound frames (invalid JSON, missing `type`, unknown `type`) are dropped without crashing the bridge
- [ ] On `ws.close`, all per-client pending requests are rejected and the client is removed from the fan-out set
- [ ] All new vitest tests pass; no regressions in `supervisor.test.ts`, `supervisor-request.test.ts`, `server.test.ts`, or `routes.test.ts`
- [ ] `python HARNESS/tools/handoff_check.py` reports 10/10 PASS
- [ ] `pnpm --filter node typecheck` clean; manual smoke test confirms a `wscat` session sees daemon events in < 100ms