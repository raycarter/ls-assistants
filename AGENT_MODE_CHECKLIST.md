# Agent mode implementation TODO

Design and research: [AGENT_MODE_TODO.md](AGENT_MODE_TODO.md).

This is the only implementation progress/evidence ledger; use the plan for architecture, contracts, rationale, and references. Phases group capabilities, not a mandatory backend-first waterfall: follow task prerequisites and bring forward the minimum pieces for the early vertical slice below. Mark a task complete only after its implementation and associated verification pass. The mocked tool slice, incremental storage, and quota-free/paged-view increments are implemented; the full Agent release and phase gates remain incomplete. Checked tasks have scoped evidence in the verification records, not an implied full release.

**Firm constraint:** Borrow OpenCode's architectural/behavioral ideas only. Write the implementation independently; do not copy, translate, vendor, or adapt OpenCode implementation code or embed its runtime.

**Confirmed product rules (2026-10-19):** approve each plan once and obtain approval for each new/revised executable plan; no separate step/tool/query approval; no product-level step/call/run-duration or retained-history quotas; web search only when the task needs it. Record every LLM interaction in its session folder with plan, step/stage, timestamp, model, actual prompt/input, and observable response/output. Runtime quota removal is implemented; complete per-attempt LLM recording still requires implementation before real model calls.

## Phase 0 — Confirm decisions and establish the baseline

- [x] **P0-01** Research the current application and OpenCode; save the design plan.
- [ ] **P0-02 — Provider decision and setup:**
  - [x] User selected **Tavily** as the first web-search provider on 2026-10-19.
  - [ ] Configure credentials locally; real-provider verification by this assistant still requires separate authorization. No additional quota-policy decision is needed to implement the selected provider.
- [x] **P0-03 — User decision:** Approved `/tmp/ls-assistants` as the sole filesystem-tool root for this implementation phase; retain mandatory exclusions and keep access disabled until configured.
- [ ] **P0-04** Finalize schemas/transitions for one approval per plan, clarification replies/rejection, nonterminal cancelling, and renewed approval only when a new/revised executable plan is created.
- [x] **P0-05 — User decision:** Approve each plan once; approve any new/revised plan again; impose no product-level quotas; use web search only if needed; record every LLM interaction with plan/step/timestamp/model/prompt/response in its session folder.
- [x] **P0-06** Run the existing frontend/backend checks and record a clean Chat regression baseline.
- [ ] **P0-07** Specify commit/creation-idempotency recovery, complete interaction-record fields, per-request failure handling, and provider/model constraints without cumulative run allowances; usage/cost reporting is informational.
- [x] **P0-08** Migrate prototype plan/call/attempt/session/event/payload caps, fixed storage reservations, and overall planning/execution expiry out of the runtime; preserve existing journals and approval/idempotency guarantees during the change.

**Gate:** Required decisions and contracts are explicit; the existing application passes its baseline checks. Provider credentials are not needed for mocked development.

## Early milestone — Validate a thin mocked vertical slice

After contracts and minimum Phase 1 primitives, bring forward a subset of P2-03, Phases 3–5, and the Phase 6–7 shell/review components. Do not wait for every tool or history presentation feature. Use the planned architecture with injected test doubles, not a second implementation. Keep broad tasks unchecked until their full requirements pass.

- [x] **M1-01** Demonstrate Chat/Agent switch → request → mocked tool-free plan → displayed approval scope → one fixture-backed approved `read_file` step → persisted result → reload/history.
- [x] **M1-02** Verify the slice rejects pre-approval work, rejection/duplicate approval execution, stale Stop, and Chat regressions; enforce durable approval, access bounds, and idempotency from the start.
- [ ] **M1-03 — User review:** Walk through the plan/review/Stop interaction before expanding all tools and audit presentation; record usability issues and contract changes.

## Phase 1 — Build durable Agent session storage

Depends on Phase 0 contracts.

- [ ] **P1-01** Add strict Agent schemas and injectable clock, ID generator, storage root, and complete LLM interaction identities without product-quota fields.
- [x] **P1-02** Ignore `/sessions/` in `bff/.gitignore` and verify it before writing real session records.
- [x] **P1-03** Create server-owned `<UTC-timestamp>-<4-random-chars>` folders exclusively, with collision retries and safe permissions/path handling.
- [x] **P1-04** Implement serialized append-only `events.jsonl` with schema version, sequence, server timestamps, and correlation IDs.
- [ ] **P1-05** Implement immutable plans, complete session-local prompt/response artifacts, incremental journals, and atomic rebuildable projections; flush/fsync before acknowledgement/publication and do not silently truncate retained LLM interactions.
  - [x] Storage Step 1: private incremental v2 entries and immutable request/plan/result/answer/logical-audit JSON payloads; one latest baseline/reference projection, compact indexes, and lazy historical reconstruction. Read legacy/mixed journals without rewriting approvals or public events. Complete provider-attempt recording and the broader task remain open.
- [x] **P1-06** Persist command claims and creation keys/digests; rebuild the creation lookup from committed `session.created` records, serialize creation, and coordinate active session ownership without imposing cumulative work quotas.
- [x] **P1-07** Implement replay, incomplete-final-record/orphan recovery, and interrupted/unknown outcomes before admitting new commands, without automatic execution replay.
- [x] **P1-08** Implement archive/list filtering, paged history, and incremental storage without artificial session/event/payload quotas; stop new dispatch on actual recording failure, not an application-defined history allowance.
- [ ] **P1-09** Test collisions, unsafe paths, concurrent writes, partial/orphan records, commit ordering, disk failures, creation retry after a lost acknowledgement, restart idempotency, and pending/archive preservation.

**Gate:** Session history can be rebuilt from its journal; failures never silently restart work or publish uncommitted success.

## Phase 2 — Implement the four tools and shared policy

Depends on Phase 1 schemas and recording primitives.

- [ ] **P2-01** Add a fixed registry for `web_search`, `web_fetch`, `read_file`, and `grep_in_folder`, with strict argument/result validation.
  - [x] Recognize all four names; enable and strictly validate `read_file`/`web_fetch`, expose descriptions to the injected planner, and bind availability/network policy to approval. Search/grep remain explicitly unavailable, not successful stubs.
- [ ] **P2-02** Enforce `/tmp/ls-assistants` as the sole configured filesystem-tool root, with shared exclusions, containment, and no-follow checks for read and grep; handle the trusted platform `/tmp` alias without permitting workspace symlink escapes or widening access.
- [x] **P2-03** Implement bounded text `read_file`: relative paths, numbered lines, pagination, safe errors, and truncation metadata.
- [ ] **P2-04** Implement incremental literal `grep_in_folder`: filtered traversal, optional file glob, relative path/line matches, and paged/continuable results rather than cumulative scan/match quotas.
- [ ] **P2-05** Move blocking filesystem work off the event loop; add cooperative cancellation and retain ownership until workers settle.
- [ ] **P2-06** Implement task-dependent Tavily search with HTTPX, provider-supported result paging, citations, and per-request timeouts; no mandatory search step, per-run call quota, extra query approval, silent fallback, or implicit page fetching.
- [ ] **P2-07** Add safe configuration examples in `bff/.env.example`; keep credentials out of UI, logs, and tool results.
- [ ] **P2-08** Test path escapes, symlink races, secret exclusions through both tools, binary/special files, oversized data, cancellation, search failures, and missing configuration.
- [x] **P2-09** Implement `web_fetch` using HTTPX public HTTP(S) GET and static HTML/main-text/plain-text extraction; return requested/final URL, title, status/content type, retrieval timestamp, and extracted content with continuation when needed. No browser/JavaScript execution or arbitrary file downloads.
- [x] **P2-10** Test fetch SSRF controls at URL parsing, DNS, actual connection, and every redirect: private/loopback/link-local/reserved targets, metadata services, rebinding, URL credentials, and non-HTTP(S)/file schemes. Verify no ambient credential/cookie forwarding, safe extraction/rendering, operational errors/timeouts, cancellation, and recorded provenance/results.

**Gate:** All four tools are independently testable, respect filesystem/network policy, and handle paged I/O without cumulative task quotas; valid plans may use zero searches/fetches or fetch a supplied URL without searching.

## Phase 3 — Generate plans and enforce explicit approval

Depends on Phase 1 primitives and Phase 2 registry/policy schemas; full implementations of all tools need not block the slice. No tool implementation is exposed during planning. P3-08 is a prerequisite to the first planner call, not executor-only hardening.

- [x] **P3-01** Add BFF-owned, versioned Agent instructions independent of the editable Chat prompt catalogue.
- [ ] **P3-02** Implement tool-free structured planning with objective, unverified assumptions, ordered steps, intended tools, and expected results; allow no-tool/no-search plans and omit task-budget fields.
- [ ] **P3-03** Validate capabilities; persist clarification question IDs and accept answers through discriminated `/revise` requests, with new planning operation IDs and stale-answer rejection.
- [x] **P3-04** Persist immutable plan revisions, stable step IDs, canonical digests, and frozen model/thinking/policy settings.
- [ ] **P3-05** Implement serialized clarification/review transitions and one approval before each plan's execution; steps, queries, tool calls, and ordinary retries must not ask for additional approval.
- [x] **P3-06** Claim each approved plan execution once and reject duplicate/stale execution commands; do not silently replay completed or interrupted work.
- [ ] **P3-07** Test approval once per plan, fresh approval for a new/revised plan, no extra approval for calls/queries/URLs/retries, no fetch or other tools before approval, spoofed approval, clarification, stale/duplicate commands, and waiting-state recovery.
- [ ] **P3-08** Before dispatch, allocate plan and step/stage identities and record each LLM interaction's timestamps/model/actual prompt/observable response, including retries/invalid/partial/failed attempts; control hidden SDK retries and verify complete planning/revision audit data.

**Gate:** Only an explicit valid user command can authorize the reviewed revision; no model message, tool output, or reused approval can do so.

## Phase 4 — Execute approved steps sequentially

Depends on storage/approval primitives and the tool(s) used by the slice; complete the full Phase 2–3 gates before release.

- [ ] **P4-01** Implement coordinator-owned step selection and a Responses/function-call loop without cumulative step/round/call/run-duration quotas.
- [ ] **P4-02** Collect complete arguments and validate the terminal provider response before dispatch; accept tool-only intermediate responses.
- [ ] **P4-03** Preserve canonical response items, matching `call_id` outputs, and required opaque reasoning continuation; retain `store=False`.
- [ ] **P4-04** Reuse complete per-interaction recording for execution/synthesis/retries; enforce sequential tool execution regardless of provider parallel-call settings.
- [ ] **P4-05** Check available tools, filesystem/network policy, and state on every call; persist intent/results without asking the user to approve individual calls or URLs within the approved plan.
- [x] **P4-06** Remove prototype run/call/attempt quotas and overall expiry; continue the approved plan as needed, use search only when useful, and await approval if a genuinely new executable plan is created.
- [ ] **P4-07** Implement targeted nonterminal cancelling, per-request timeout/failure handling, and cleanup; retain settlement ownership and stop dispatch if recording actually fails.
- [ ] **P4-08** Produce step results and a final evidence-based synthesis without extra unapproved tool work.
- [ ] **P4-09** Test partial/invalid calls, filesystem/network escapes, prompt injection from fetched pages, refusals, retries, zero-search and fetch-only plans, work beyond former quotas, unknown outcomes, full prompt/response retention, and recording failures.

**Gate:** Execution follows the approved plan and access policy, with every LLM interaction durably recorded, no cumulative product quotas, and no extra step/tool/query approval.

## Phase 5 — Expose Agent APIs and progress events

Use Phase 0 contracts to bring forward the slice's routes alongside their minimal storage/planning/execution dependencies; full completion requires Phases 1–4. Keep existing Chat endpoints/contracts unchanged.

- [x] **P5-01** Compose Agent services into BFF lifespan while preserving existing Azure credential/client ownership and cleanup.
- [x] **P5-02** Add session list/create/get/preferences/archive endpoints, lock preferences for all nonterminal run states, and return pending archive status until local work settles.
- [ ] **P5-03** Add planning, clarification-answer/plan-feedback revision, waiting-state rejection, approval-execution, and targeted cancellation endpoints with strict state validation.
- [x] **P5-04** Add a separate typed Agent SSE contract and paginated journal replay using committed event sequences.
- [ ] **P5-05** Apply durable idempotency and payload binding; reject stale/conflicting commands without duplicate execution.
- [x] **P5-06** Bind Stop/disconnect to the owning operation; distinguish `202` cancelling from known terminal outcomes and keep settlement ownership without continuing plan execution.
- [x] **P5-07** Handle per-request provider/transport timeouts, backpressure, bounded cleanup, safe errors, and controlled artifact access without an overall Agent-run deadline.
- [ ] **P5-08** Test direct ASGI disconnects, delayed Stop, blocked-worker/pending-archive status, duplicate commands, clarification/rejection paths, replay, shutdown, and unchanged Chat SSE behavior.

**Gate:** The complete approval workflow works through mocked HTTP/SSE, including interruptions and duplicate commands.

## Phase 6 — Add mode navigation and independent histories

Depends on stable Agent contracts; build the slice's shell against an injected Agent API while BFF routes develop.

- [x] **P6-01** Add accessible Chat/Agent radio-style controls to the main header; exactly one mode is selected.
- [x] **P6-02** Add separate Agent API/state services and versioned `ls-assistants.agent-ui-state` metadata/draft storage; preserve Chat v1 storage.
- [x] **P6-03** Remember each mode's selected session; load the appropriate history/count without converting sessions between modes.
- [x] **P6-04** Make new/select/archive/draft actions mode-specific and use BFF-generated Agent identities.
- [x] **P6-05** Preserve legacy browser Agent records as read-only; exclude them from Chat status repairs, prompt propagation, and Chat mutation entry points.
- [x] **P6-06** Hide Agent session prompt controls and Chat-only empty-state text; keep Settings prompt CRUD unchanged without assigning an Agent prompt.
- [x] **P6-07** Bind model/thinking controls to the correct mode/session and freeze Agent choices through planning/clarification/review/execution/cancellation.
- [x] **P6-08** Keep coordinators alive across mode switches; switching views must not abort background Chat replies or an active Agent request.
- [ ] **P6-09** Test keyboard/mobile behavior, reload, unavailable BFF, last-session fallback, independent selections/drafts, legacy preservation, and Settings edits from Agent mode.

**Gate:** Mode navigation is isolated and persistent; existing Chat workflows and prompt snapshots remain intact.

## Phase 7 — Build plan review, execution progress, and history UI

Bring the slice's review/result UI forward with the minimal API/navigation pieces; complete full Phases 5–6 integration before release.

- [ ] **P7-01** Build clarification answer/reject and plan-feedback flows bound to their current question/plan identities; rejection must release the waiting session.
- [ ] **P7-02** Show assumptions, steps, intended tools, filesystem scope, and model/thinking for one plan-level approval; submit its displayed revision/digest without per-query prompts or mandatory task-budget controls.
  - [x] Require the complete loaded plan to match authoritative identity/model/effort before approval; show every step description and remove task-budget controls. Approved-root wiring and the broader review walkthrough remain open.
- [x] **P7-03** Decode Agent events independently of Chat; reconcile committed sequences and reject stale session/run/operation/revision callbacks.
- [ ] **P7-04** Render paged step/tool results, citations, and final output safely; distinguish search snippets, fetched page text with final URL/time, and filesystem evidence. Never claim page retrieval without a successful recorded `web_fetch` result.
- [ ] **P7-05** Add targeted Stop, visible Stopping/pending-archive status, partial results and typed scope/information errors; prevent double-click execution or automatic resume after reload.
- [ ] **P7-06** Add paged timeline/history and archived-session review with plan/step/timestamp/model/prompt/response details for every LLM interaction; shortened previews must link to complete retained data.
  - [x] Explicitly page session/run summaries and retained result windows; directly restore selected archived sessions outside page one. Complete per-interaction audit presentation remains open.
- [x] **P7-07** Preserve review focus and useful scrolling; show interruptions without silently resuming work.
- [ ] **P7-08** Test displayed-versus-approved scope, clarification/revision races, duplicate/gapped events, stopping status, mode switches, safe rendering/links, and accessibility.
- [x] **P7-09** Disclose retained user/source/tool/LLM content and external-query behavior before use; do not describe credential-field exclusion as complete secret-content scrubbing.

**Gate:** The user can review, approve, observe, stop, and later inspect a run without relying on model prose for control decisions.

## Phase 8 — Integration, regression checks, and documentation

Depends on Phases 0–7.

- [ ] **P8-01** Run mocked end-to-end scenarios covering all four tools, a reasoning-only/no-search task, a filesystem-only task, and fetch of a directly supplied URL without search; four-tool coverage is a test case, not a requirement for every plan.
- [ ] **P8-02** Verify rejection, stale approval, duplicate commands, delayed Stop, disconnect, and restart never trigger unauthorized or repeated execution.
- [ ] **P8-03** Verify every planning/revision/execution/synthesis/retry interaction retains its plan/step/timestamps/model/actual prompt/response, including failed/partial outputs; test credential exclusion, lossless artifacts, disk failure, and history review.
- [x] **P8-04** Run frontend tests, TypeScript checks, production build, and formatting checks using the commands in plan §10 from `ui/` with the default Node activated.
- [x] **P8-05** Run backend pytest, Ruff lint/format checks, and lock validation using the commands in plan §10 from `bff/` with `uv`.
- [x] **P8-06** Verify session files are Git-ignored, inspect `git status`/diff, and run `git diff --check`; never stage session data or secrets.
- [ ] **P8-07** Update frontend/backend documentation with actual contracts, configuration, provider constraints, full interaction recording, recovery behavior, and verification results; remove obsolete quota/extra-approval descriptions as runtime migration lands.
- [ ] **P8-08** Reconcile implementation deviations with the design plan and record completed-task evidence here.
- [ ] **P8-09 — User walkthrough:** Evaluate a task that needs search and one that does not; verify one approval per plan, useful results, and complete plan/step-correlated LLM history. Passing scripted mocks alone is not proof of usefulness.
- [x] **P8-10** Review code provenance and the final diff against the independent-implementation constraint: no copied/translated/vendored OpenCode implementation source or embedded OpenCode runtime.

**Release gate:** Required tasks/walkthroughs/checks pass; modes are independent, each plan is approved once, new plans need approval, tools stay within configured access, no product quotas remain, search is optional, every LLM prompt/response has its plan/step/time/model recorded, and Chat remains regression-free. Live compatibility is separately unverified until the optional checks below.

## Optional live verification — Separate authorization required

- [ ] **LIVE-01 — User authorization:** Approve real Azure/search calls and their costs, and choose a small permitted fixture folder.
- [ ] **LIVE-02** Run a controlled smoke test and record provider/deployment compatibility and results without exposing credentials or private data.

Live verification is not required for mocked implementation completion. No real provider calls are authorized by this checklist.

## Verification record — 2026-10-16, first mocked slice

- Baseline: 141 backend tests and 398 frontend tests passed, plus backend lint/format/lock and frontend type/build/format checks.
- Final combined verification: **197 backend tests** (55 Agent regressions, one generated-contract test, and all 141 baseline cases) and **543 frontend tests** (including 20 backend-generated wire-contract cases and all 398 baseline cases) passed. Ruff lint/format, Python lock validation, TypeScript, production build, Prettier, and whitespace checks passed. The two existing nonfatal critical-CSS selector warnings remain.
- Evidence: `bff/tests/test_agent.py`, `bff/tests/test_agent_contract.py`, `ui/src/app/{agent-api,agent-state,app-agent,agent-contract}.spec.ts`, and the updated Chat/Settings regressions. Backend-created static fixtures exercise actual public SSE serialization, interleaved Stop/archive records, admission sequence changes, backward UTC adjustments, and truncated UTF-8 excerpts through the UI transport/decoder.
- Separate backend/UI authors and independent reviewers iterated on quotas/recovery, admission/shutdown cancellation, filesystem/storage aliases, preference and navigation races, pending archive ownership, error routing, and cross-stack event contracts. Final targeted re-reviews reported no remaining P1/P2 findings in the reviewed corrections. This is scoped review evidence, not a guarantee that no defects exist.
- Broad unchecked tasks may have partial foundations: creation idempotency, recovery, storage limits, shared path policy, planner attempt recording, and review/results UI exist. They remain unchecked where all-tool, real-provider, clarification, artifact-layout, or broader release requirements are not met.
- As of the 2026-10-16 slice: journals contain bounded full snapshots/audit records rather than separate artifact/snapshot files. `read_file` is the only implemented tool at that point; the default planner returns `503 agent_not_configured`, clarification returns `501`, and final output is a completion receipt rather than LLM synthesis. No actual provider calls or automatic execution resume occur. Later increments supersede this historical snapshot; [bff/AGENT_API.md](bff/AGENT_API.md) describes the current runtime.
- Still needed: production Responses planning and tool loop, grep/search and provider selection, clarification, complete history/replay UI, confirmed filesystem/search/retention policies, and the user walkthrough. No native browser visual test, live Azure/search check, Git staging, or commit was performed.

## Decision record — 2026-10-19

- User approved `/tmp/ls-assistants` as the only filesystem-tool scope for this implementation phase. P0-03 records the decision; P2-02 remains open for configuration/enforcement and tests. No directory was inspected or created while recording it.
- Existing secret/private-archive/session-log exclusions remain. Application-owned journals stay in `bff/sessions`; that does not grant filesystem tools access to the journal store.
- OpenCode V2's provider selection was rechecked: Exa, Firecrawl, Parallel, Tavily, and TinyFish are supported, with configured/remembered or prompted selection rather than a single hardwired provider. Our provider is chosen independently of OpenCode.
- User subsequently selected **Tavily** as our first provider. The choice is complete; credential setup and live-use cost/data-handling confirmation remain open under P0-02. Recommended initial integration is direct HTTPX with Tavily Basic and bounded results, not MCP, provider failover, or automatic Advanced search.

### Subsequent user clarification — replaces earlier policy proposals

- One approval starts execution of a plan. Calls, queries, steps, and retries within it need no separate approval; each new/revised executable plan requires approval again.
- No product-imposed step, LLM/tool/search-call, total-run-duration, or retained-history quotas. Web search depends on the task, and plans may use none. Operational provider/model constraints, request failures, and file/result paging must not be relabeled task allowances.
- Every LLM interaction is recorded in its session folder with plan identity, step/stage identity, timestamps, requested/reported model names, actual prompt/input, and observable response/output. Include planning, revisions, synthesis, retries, invalid/refused/partial/failed/cancelled attempts; mark unavailable outputs honestly and never silently truncate the only retained record.
- P0-05 records the confirmed policy. P0-08, P1-05/P1-08, P3-08, and P4-06 remain open for migration: current BFF schemas/admission/storage/transport still enforce prototype caps and do not yet retain all required LLM data. The existing fixed event reservations/full-snapshot-per-event design must be changed coherently, not merely have numeric checks removed. No runtime code, provider calls, or filesystem-root access changed in this documentation update.

### MVP tool addition — web_fetch

- User identified page fetching as essential. The target MVP now has four tools: `web_search`, `web_fetch`, `read_file`, and `grep_in_folder`.
- `web_fetch` retrieves actual public page content, not search snippets; it can fetch a supplied URL without a preceding search. No browser/JavaScript automation or private/internal network access is included.
- P2-09/P2-10 track implementation and security/provenance tests. Existing once-per-plan approval, optional tool use, no cumulative quotas, full LLM audit, `/tmp/ls-assistants` filesystem scope, and no OpenCode source/runtime reuse remain unchanged. The runtime tool is not implemented by this documentation update.

## Verification record — 2026-10-20, approved web-fetch increment

- Completed P2-09/P2-10 through the existing injected-planner path: tool-free planning → exact-version approval → `web_fetch` → complete durable extracted-text evidence → paged UI inspection/reload. Search is not a prerequisite. The default planner is still unavailable; this is not an end-to-end production Agent release.
- Fixed registry recognizes all four names, enables `read_file` and `web_fetch`, rejects search/grep, and freezes availability/network policy in new approvals. Existing unchanged read-only approvals/journals remain compatible without digest rewriting. P2-01 remains open for all-four-tool contracts/implementations.
- HTTPX/HTTPcore fetch validates URL syntax without pre-approval DNS; at execution it validates all DNS answers, pins a numeric connection target, checks the actual peer, preserves verified TLS/SNI/Host, and rechecks every redirect. Each request has an isolated unauthenticated client with ambient proxies/cookies/credentials disabled. No JavaScript/browser, authenticated pages, binary downloads, or private/internal targets.
- Incremental raw identity-encoded reads, bounded parser workers and disk-spooled extraction avoid whole-document buffering. Unsupported compression and pathological markup/depth/title metadata fail explicitly; these operational parser/protocol guards are not document-size or cumulative work quotas. Full supported text is retained without silent truncation.
- Added session-local `artifacts/<server-uuid>.txt`, incremental SHA-256 receipts, fsync-before-reference, no-follow/regular-file checks, corruption/missing-evidence recovery checks, and committed/same-session-only GET paging. Safe uncommitted artifacts remain inaccessible. This advances P1-05/P5-07 without completing broader LLM artifacts, incremental journals, or quota migration.
- UI strictly decodes fetch arguments/provenance/pages, shows requested/final URL and retrieval time, renders fetched text literally, and reads continuation with GET only. Paging errors/loading are result-local; navigation/destroy cancels or ignores stale work. P7-04 remains open for search/grep and broader result presentation.
- Evidence: `bff/app/tools/{registry,network_policy,web_fetch}.py`, `bff/tests/{test_web_fetch,test_agent_fetch,test_agent_artifacts,test_agent_contract}.py`, and `ui/src/app/{agent-fetch,agent-workspace,agent-contract}.spec.ts`. The backend-generated fixture now includes successful fetch and durable continuation as well as all previous scenarios.
- Independent backend/network/UI review found and corrected queued-Stop/recording-failure dispatch, compressed-response memory growth, quadratic/optional-tag extraction, and nested-list/table scope issues. Targeted re-reviews reported no remaining material P1/P2 in scope; this is not a guarantee against all races or live-site incompatibility.
- Final verification: **493 backend tests** and **655 frontend tests** passed; Ruff lint/format, lock validation, app/spec TypeScript checks, production build, Prettier, fixture comparison, and whitespace checks passed. The two existing nonfatal critical-CSS selector warnings remain. `httpcore` is now an explicit dependency at its already-locked version 1.0.9; no package versions changed.
- Remaining priorities: coordinated prototype-cap/incremental-storage migration (P0-08/P1-05/P1-08), complete per-attempt LLM audit before a real planner call (P3-08), production planning/clarification/function-call loop/synthesis, approved filesystem-root wiring and grep, Tavily, and full interaction history. No actual Azure/Tavily/public-web calls, credentials/private/runtime-session reads, browser walkthrough, staging, or commits were performed.

## Verification record — 2026-10-20, incremental storage Step 1

- At the user's request, committed the preceding fetch milestone as **`811032a` — `add approved web fetch and retained evidence`**, after clean scope review and 493 backend/655 UI passing tests plus lint/type/build/format/lock checks. No push was requested or performed. The following storage work is separate and remains uncommitted.
- New writes use private `storage_version: 2` event headers and fixed session/run/result deltas in `events.jsonl`. Immutable request, plan-revision, step-result, answer, and existing logical-audit bodies are retained in session-local `payloads/<id>.json` with ID/SHA-256/byte references. Unchanged large bodies are not serialized, hashed, or rewritten on title/control changes.
- Memory retains one hydrated latest-session baseline, its reference projection, and compact record/claim/attempt/operation/integrity indexes—not all historical full snapshots/audit bodies. Exact public historical events and the read-only `records` compatibility view are reconstructed on demand. Early Stop's claim check remains lock-free and I/O-free; paused iterators own descriptors without blocking get/append/close.
- Legacy v1 and mixed v1/v2 journals are streamed and recovered without rewriting existing bytes, identities, approval digests, command digests, or sequence numbers. No migration event or renewed approval is introduced. Unfinished/unknown operations and pending archive settle once without tool replay. The existing public schema-version-1 SSE/API fixture is unchanged and was not regenerated.
- Added exclusive/no-follow/single-link JSON payload writes, payload/directory fsync-before-reference, complete committed-reference integrity coverage (including superseded plans/audit-only data), pinned-root reads, journal record fingerprints, final prefix verification, and a short latched-failure/publication barrier. Failures cannot publish uncommitted success; physically durable lost acknowledgements are resolved by recovery. Safe orphan files remain inaccessible and retained.
- Existing quotas, reservations, deadlines, and public full-session projections remain. JSON payload bytes now participate in the existing physical storage guards; TXT fetch evidence keeps separate accounting. UTF-8 private JSON avoids introducing ASCII-escape inflation; public/command canonical hashing is unchanged. No full LLM instrumentation or real planner was added.
- Evidence: `bff/app/{session_journal,sessions,agent}.py`, `bff/tests/{test_session_journal,test_session_migration}.py`, and the minimally adjusted physical-format assertion in `test_agent_artifacts.py`. Independent test authors added literal-legacy/mixed restart, exact 153-event pagination, mutation isolation, corruption, commit ordering, shutdown/Stop, and near-boundary Unicode regressions. Independent reviewers' material findings were corrected and re-reviewed; no remaining P1/P2 was reported in the reviewed scope.
- Final verification: **608 backend tests** and **655 frontend tests** passed; Ruff lint/format (37 Python files), lock validation, app/spec TypeScript checks, production build, Prettier, unchanged public fixture comparison, Git-ignore and whitespace checks passed. The two existing nonfatal critical-CSS selector warnings remain. No dependencies or frontend source/fixtures changed. No actual provider/public-web calls, credential/private/runtime-session reads, real-session migration, push, or second commit were performed.
- Known costs: append-time full journal-prefix verification, O(committed-reference-count) live metadata checks, prefix replay for historical reads, and one latest full public baseline per session. Compaction, orphan garbage collection, paged public projections, quota removal (Step 2), and complete LLM recording (Step 3) remain open. P1-05/P0-08/P1-08/P3-08 are not marked complete by this increment.

## Verification record — 2026-10-20, quota-free Step 2 and public projection v2

- At the user's request, committed storage Step 1 as **`2fb37a5` — `migrate agent sessions to incremental journals`**, after 608 backend/655 frontend tests and lint/type/build/format/lock checks passed. Step 2 is separate, uncommitted work; nothing was pushed.
- Removed artificial plan-step/tool-call/attempt/revision/run/session/event/journal/payload allowances, prototype global admission/active-operation caps, fixed event/byte reservations, and overall planning/execution/transport expiry. New plans omit `limits`; legacy limit fields remain unchanged signed historical material but no longer govern work. One nonterminal run/session, exact-version approval, model/policy validation, and idempotency remain enforced.
- Replaced lifetime expiry with individual planner-call (120 s), file-call (30 s), and ASGI-send (30 s) defaults. Fetch keeps its own per-request timeout. Waiting for a frame has no lifetime timer; Stop/disconnect/shutdown retain ownership until actual workers settle. Actual recording failures still latch storage unavailable and prohibit uncommitted success or subsequent dispatch. Byte usage remains informational, not an allowance.
- Normal public reads/SSE now use explicit **projection/schema version 2**, separate from private storage version 2 and legacy durable/public-v1 records. Session headers and small historical event/control indexes select only the requested bodies; they do not reconstruct diagnostic full-history snapshots and then slice them. Mutations return bounded headers. Session, run-summary, result, and event pages have operational page sizes, totals, and validated cursor/sequence boundaries.
- The UI requests 25 session summaries, 10 run summaries, and 20 result cells per window by default. It keeps one current body plus one opened historical body, a bounded result frontier and 64 duplicate signatures; further reads require explicit paging. Saved/archived selections are recovered by direct GET even outside page one. Complete matching plan content is required and displayed before approval; off-window completed steps are not presented as pending.
- Actual restart recovery preserves its original null command correlation while projecting a primary changed run/error/failed cell where unambiguous. Required `invalidates_runs` flags session-wide or multi-run changes, including archive-only recovery without a primary run. The UI refreshes only its bounded loaded views and cannot clear invalidation on skipped, failed, cancelled, or superseded reads; closing optional history reevaluates the remaining required views.
- Removed the absolute file-size and 10,000-line-offset ceilings. Reads validate supported UTF-8 through EOF in 8 KiB chunks, buffering only the requested excerpt. Per-page/line rendering bounds and honest truncation remain. Scope exclusions and descriptor checks are unchanged. Only the exact known v3 filesystem predecessor with matching roots/workspaces/storage identity is grandfathered under v4; old read-only approvals never authorize fetch.
- Evidence: `bff/tests/{test_agent_no_quotas,test_session_no_quotas,test_agent_views,test_agent_file_pages,test_agent_contract}.py`, existing storage/Agent/Chat regressions, and `ui/src/app/{agent-contract,agent-contract-v1,agent-review,agent-causality,agent-api,agent-state,agent-workspace}.spec.ts`. The generated v2 fixture includes actual startup recovery; the frozen v1 fixture remains read-only compatibility evidence. Boundary tests exceed former step/run/revision/session/event/payload ceilings; diagnostic usage above 64 MB is simulated explicitly, not reported as a large real-data run.
- Independent runtime, storage/projection, filesystem/policy, and frontend reviews found and corrected recovery projection omissions, progress/terminal validation, stale summary/detail and overlapping-refresh races, loading cleanup, non-latest active-run handling, and archive-only/closed-history recovery. Final targeted re-reviews reported no remaining material P1/P2 in scope; this does not establish live deployment compatibility or cover every interleaving.
- Final verification: **786 backend tests** and **725 frontend tests** passed; Ruff lint/format (42 Python files), offline lock validation, app/spec TypeScript checks, production build, Prettier, generated v2 contract comparison, frozen-v1 blob equality, Git-ignore, and whitespace checks passed. The two existing nonfatal critical-CSS selector warnings remain. No dependencies, real runtime sessions, or credentials were changed; no real Azure/Tavily/public-web calls, browser walkthrough, push, or Step 2 commit were performed.
- Remaining: full LLM-attempt identities/input/output recording (P3-08), production planning/clarification/function loop/synthesis, Tavily/grep and approved filesystem-root wiring, complete interaction history, and user/live walkthroughs. Private latest baselines/indexes still grow with retained history; metadata checks, commit-prefix hashing and diagnostic prefix replay retain their documented scaling costs. No compaction/GC, constant-memory storage, or end-to-end Agent release is claimed.
