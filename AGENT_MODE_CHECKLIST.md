# Agent mode implementation TODO

Design and research: [AGENT_MODE_TODO.md](AGENT_MODE_TODO.md).

This is the only implementation progress/evidence ledger; use the plan for architecture, contracts, rationale, and references. Phases group capabilities, not a mandatory backend-first waterfall: follow task prerequisites and bring forward the minimum pieces for the early vertical slice below. Mark a task complete only after its implementation and associated verification pass. The first mocked vertical slice is implemented; the full Agent release and phase gates remain incomplete. Checked tasks below have first-slice verification evidence, not an implied full release.

**Firm constraint:** Borrow OpenCode's architectural/behavioral ideas only. Write the implementation independently; do not copy, translate, vendor, or adapt OpenCode implementation code or embed its runtime.

## Phase 0 — Confirm decisions and establish the baseline

- [x] **P0-01** Research the current application and OpenCode; save the design plan.
- [ ] **P0-02 — User decision:** Choose the web-search provider and confirm credentials, cost, and data handling.
- [ ] **P0-03 — User decision:** Choose allowed filesystem roots and exclusions; keep access disabled until configured.
- [ ] **P0-04** Finalize schemas/transitions, including question-identified clarification replies/rejection, nonterminal cancelling, and terminal scope/information stop reasons requiring a new run.
- [ ] **P0-05 — User decision:** Confirm exact-query versus bounded-query approval, allowances across all LLM/search attempts, retained content/archive behavior, literal grep, single-worker operation, and disconnect/settlement behavior.
- [x] **P0-06** Run the existing frontend/backend checks and record a clean Chat regression baseline.
- [ ] **P0-07** Specify commit/creation-idempotency recovery, process-local versus retained limits, input/output caps, and estimated/unknown cost reporting; do not promise a hard currency ceiling.

**Gate:** Required decisions and contracts are explicit; the existing application passes its baseline checks. Provider credentials are not needed for mocked development.

## Early milestone — Validate a thin mocked vertical slice

After contracts and minimum Phase 1 primitives, bring forward a subset of P2-03, Phases 3–5, and the Phase 6–7 shell/review components. Do not wait for every tool or history presentation feature. Use the planned architecture with injected test doubles, not a second implementation. Keep broad tasks unchecked until their full requirements pass.

- [x] **M1-01** Demonstrate Chat/Agent switch → request → mocked tool-free plan → displayed approval scope → one fixture-backed approved `read_file` step → persisted result → reload/history.
- [x] **M1-02** Verify the slice rejects pre-approval work, rejection/duplicate approval execution, stale Stop, and Chat regressions; enforce durable approval, access bounds, and idempotency from the start.
- [ ] **M1-03 — User review:** Walk through the plan/review/Stop interaction before expanding all tools and audit presentation; record usability issues and contract changes.

## Phase 1 — Build durable Agent session storage

Depends on Phase 0 contracts.

- [ ] **P1-01** Add strict Agent schemas and injectable clock, ID generator, storage root, and limits.
- [x] **P1-02** Ignore `/sessions/` in `bff/.gitignore` and verify it before writing real session records.
- [x] **P1-03** Create server-owned `<UTC-timestamp>-<4-random-chars>` folders exclusively, with collision retries and safe permissions/path handling.
- [x] **P1-04** Implement serialized append-only `events.jsonl` with schema version, sequence, server timestamps, and correlation IDs.
- [ ] **P1-05** Implement immutable plans, bounded artifacts, and atomic snapshots; flush/fsync the required files/directory changes before acknowledgements/publication under the documented crash-recovery contract.
- [ ] **P1-06** Persist command claims and creation keys/digests; rebuild the creation lookup from committed `session.created` records, serialize creation, and add session ownership/locking plus process-local concurrency limits.
- [ ] **P1-07** Implement replay, incomplete-final-record/orphan recovery, and interrupted/unknown outcomes before admitting new commands, without automatic execution replay.
- [ ] **P1-08** Implement archive/list filtering and disk/session/artifact limits; stop new dispatch on recording failure.
- [ ] **P1-09** Test collisions, unsafe paths, concurrent writes, partial/orphan records, commit ordering, disk failures, creation retry after a lost acknowledgement, restart idempotency, and pending/archive preservation.

**Gate:** Session history can be rebuilt from its journal; failures never silently restart work or publish uncommitted success.

## Phase 2 — Implement the three tools and shared policy

Depends on Phase 1 schemas and recording primitives.

- [ ] **P2-01** Add a fixed registry for `web_search`, `read_file`, and `grep_in_folder`, with strict argument/result validation.
- [ ] **P2-02** Implement shared filesystem allowlists, exclusions, containment, and no-follow access checks for both read and grep.
- [x] **P2-03** Implement bounded text `read_file`: relative paths, numbered lines, pagination, safe errors, and truncation metadata.
- [ ] **P2-04** Implement bounded literal `grep_in_folder`: filtered traversal, optional file glob, relative path/line matches, and scan/result limits.
- [ ] **P2-05** Move blocking filesystem work off the event loop; add cooperative cancellation and retain ownership until workers settle.
- [ ] **P2-06** Implement one injected web-search provider with HTTPX, the confirmed query/call/result policy, snippet citations, timeouts, and no silent fallback or implicit page fetching.
- [ ] **P2-07** Add safe configuration examples in `bff/.env.example`; keep credentials out of UI, logs, and tool results.
- [ ] **P2-08** Test path escapes, symlink races, secret exclusions through both tools, binary/special files, oversized data, cancellation, search failures, and missing configuration.

**Gate:** Every tool is bounded, independently testable with fixtures/mocks, and unable to bypass the shared access policy.

## Phase 3 — Generate plans and enforce explicit approval

Depends on Phase 1 primitives and Phase 2 registry/policy schemas; full implementations of all tools need not block the slice. No tool implementation is exposed during planning. P3-08 is a prerequisite to the first planner call, not executor-only hardening.

- [x] **P3-01** Add BFF-owned, versioned Agent instructions independent of the editable Chat prompt catalogue.
- [ ] **P3-02** Implement tool-free structured planning with objective, explicitly unverified assumptions, ordered discovery steps, tools/scopes, expected results, and limits.
- [ ] **P3-03** Validate capabilities; persist clarification question IDs and accept answers through discriminated `/revise` requests, with new planning operation IDs and stale-answer rejection.
- [x] **P3-04** Persist immutable plan revisions, stable step IDs, canonical digests, and frozen model/thinking/policy settings.
- [ ] **P3-05** Implement serialized waiting-state transitions: question-identified answer/rejection or plan-identified feedback/rejection; only the exact awaiting-approval plan can execute.
- [x] **P3-06** Claim each revision for execution once; require a new run and fresh approval after failure, cancellation, or interruption.
- [ ] **P3-07** Test zero tools before approval, spoofed approval, malformed plans, changed settings, stale/duplicate commands, clarification answer/rejection without a plan, and waiting-state recovery.
- [ ] **P3-08** Before planning, implement shared AgentResponses request/result/attempt recording, disabled SDK retries, coordinator retry/budget accounting, and failure handling; test planning/revision audit completeness.

**Gate:** Only an explicit valid user command can authorize the reviewed revision; no model message, tool output, or reused approval can do so.

## Phase 4 — Execute approved steps sequentially

Depends on storage/approval primitives and the tool(s) used by the slice; complete the full Phase 2–3 gates before release.

- [ ] **P4-01** Implement coordinator-owned step selection and a bounded Responses/function-call loop.
- [ ] **P4-02** Collect complete arguments and validate the terminal provider response before dispatch; accept tool-only intermediate responses.
- [ ] **P4-03** Preserve canonical response items, matching `call_id` outputs, and required opaque reasoning continuation; retain `store=False`.
- [ ] **P4-04** Reuse Phase 3's shared audited retry/budget path for execution/synthesis; enforce sequential tool execution regardless of model/provider parallel-call settings.
- [ ] **P4-05** Check current-step tool/resource permissions, state, and budgets on every call; persist intent before invocation and outcomes before advancement.
- [ ] **P4-06** Enforce all attempt/context/output/time limits; preserve partial results on `scope_change_required` or `clarification_required`, terminate execution, and require a new run/fresh approval rather than same-run resume.
- [ ] **P4-07** Implement targeted nonterminal cancelling, deadlines, and cleanup; retain ownership after transport teardown until workers settle, and stop new dispatch on audit failure.
- [ ] **P4-08** Produce step results and a final evidence-based synthesis without extra unapproved tool work.
- [ ] **P4-09** Test partial/invalid calls, multiple calls, out-of-step access, prompt injection, refusals, retries, budget exhaustion, unknown outcomes, and recording failures.

**Gate:** Execution cannot outrun approval, step order, access policy, budgets, or durable recording.

## Phase 5 — Expose Agent APIs and progress events

Use Phase 0 contracts to bring forward the slice's routes alongside their minimal storage/planning/execution dependencies; full completion requires Phases 1–4. Keep existing Chat endpoints/contracts unchanged.

- [x] **P5-01** Compose Agent services into BFF lifespan while preserving existing Azure credential/client ownership and cleanup.
- [ ] **P5-02** Add session list/create/get/preferences/archive endpoints, lock preferences for all nonterminal run states, and return pending archive status until local work settles.
- [ ] **P5-03** Add planning, clarification-answer/plan-feedback revision, waiting-state rejection, approval-execution, and targeted cancellation endpoints with strict state validation.
- [x] **P5-04** Add a separate typed Agent SSE contract and paginated journal replay using committed event sequences.
- [ ] **P5-05** Apply durable idempotency and payload binding; reject stale/conflicting commands without duplicate execution.
- [x] **P5-06** Bind Stop/disconnect to the owning operation; distinguish `202` cancelling from known terminal outcomes and keep settlement ownership without continuing plan execution.
- [ ] **P5-07** Enforce absolute deadlines, backpressure handling, bounded cleanup, safe errors, and controlled artifact exposure.
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
- [ ] **P7-02** Show unverified assumptions, steps, allowed folders/tools, effective search policy/exact queries when required, model/thinking, and resource limits; submit the displayed revision/digest on Approve and run.
- [ ] **P7-03** Decode Agent events independently of Chat; reconcile committed sequences and reject stale session/run/operation/revision callbacks.
- [ ] **P7-04** Render bounded step/tool results, citations, and final output safely; label search snippets separately from files actually read and never imply linked pages were fetched.
- [ ] **P7-05** Add targeted Stop, visible Stopping/pending-archive status, partial results and typed scope/information errors; prevent double-click execution or automatic resume after reload.
- [ ] **P7-06** Add read-only timeline/history replay and archived-session review from BFF records.
- [x] **P7-07** Preserve review focus and useful scrolling; show interruptions without silently resuming work.
- [ ] **P7-08** Test displayed-versus-approved scope, clarification/revision races, duplicate/gapped events, stopping status, mode switches, safe rendering/links, and accessibility.
- [x] **P7-09** Disclose retained user/source/tool/LLM content and external-query behavior before use; do not describe credential-field exclusion as complete secret-content scrubbing.

**Gate:** The user can review, approve, observe, stop, and later inspect a run without relying on model prose for control decisions.

## Phase 8 — Integration, regression checks, and documentation

Depends on Phases 0–7.

- [ ] **P8-01** Run a mocked end-to-end flow: request → plan → review → approve → all three tools → final result → reload/history.
- [ ] **P8-02** Verify rejection, stale approval, duplicate commands, delayed Stop, disconnect, and restart never trigger unauthorized or repeated execution.
- [ ] **P8-03** Verify complete logical LLM/tool records across planning/revisions/execution, credential-field exclusion, opaque continuation, truncation, disk failure, retained-content disclosure, and archive review.
- [x] **P8-04** Run frontend tests, TypeScript checks, production build, and formatting checks using the commands in plan §10 from `ui/` with the default Node activated.
- [x] **P8-05** Run backend pytest, Ruff lint/format checks, and lock validation using the commands in plan §10 from `bff/` with `uv`.
- [x] **P8-06** Verify session files are Git-ignored, inspect `git status`/diff, and run `git diff --check`; never stage session data or secrets.
- [ ] **P8-07** Update frontend/backend documentation with actual contracts, configuration, limits, recovery behavior, and verification results.
- [ ] **P8-08** Reconcile implementation deviations with the design plan and record completed-task evidence here.
- [ ] **P8-09 — User walkthrough:** Evaluate a realistic workspace grep/read plus web-search task, including unverified planning assumptions, snippet-only evidence, query-policy usefulness, and scope-change behavior; passing scripted mocks alone is not proof of usefulness.
- [x] **P8-10** Review code provenance and the final diff against the independent-implementation constraint: no copied/translated/vendored OpenCode implementation source or embedded OpenCode runtime.

**Release gate:** Required tasks, early-slice/user walkthroughs, and mocked checks pass; modes/histories are independent, approval is enforced in BFF, waiting/stopping/scope-change paths are complete, tools are bounded, logical records are durable, and Chat remains regression-free. Live provider compatibility remains separately unverified until the optional checks below.

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
- Current backend contract/limits: [bff/AGENT_API.md](bff/AGENT_API.md). Journals contain bounded full snapshots/audit records rather than separate artifact/snapshot files. `read_file` is the only implemented tool; the default planner returns `503 agent_not_configured`, clarification returns `501`, and final output is a completion receipt rather than LLM synthesis. No actual provider calls or automatic execution resume occur.
- Still needed: production Responses planning and tool loop, grep/search and provider selection, clarification, complete history/replay UI, confirmed filesystem/search/retention policies, and the user walkthrough. No native browser visual test, live Azure/search check, Git staging, or commit was performed.
