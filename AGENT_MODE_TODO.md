# Agent mode: research and design plan

Research date: 2026-10-15. Implementation update: 2026-10-16. Status: the first mocked vertical slice is implemented and independently reviewed; the full Agent release is not complete.

Implementation progress is tracked only in [AGENT_MODE_CHECKLIST.md](AGENT_MODE_CHECKLIST.md). This document owns the design and contract decisions, not a second completion ledger.

## 1. Goal and first-release scope

Add an Agent workflow beside the completed Chat workflow:

1. A mutually exclusive **Chat / Agent** control in the main header.
2. Independent session histories, selected sessions, drafts, and per-session model/thinking preferences.
3. No session-level system-prompt controls in Agent mode. Settings continues to manage the existing Chat prompt catalogue.
4. Three backend tools: `web_search`, `read_file`, and `grep_in_folder`.
5. A structured plan that the user reviews before explicitly choosing **Approve and run**.
6. Sequential execution with visible step results and cancellation.
7. A durable, reviewable record under `bff/sessions/<timestamp>-<4-random-chars>/`, excluded from Git.

**Recommendation:** build a small, backend-owned coordinator using the existing Responses SDK, not a new agent framework or an embedded OpenCode process. Keep `/api/chat` and Chat persistence behavior unchanged.

Out of scope initially: file writes, shell execution, arbitrary URL fetching, autonomous subagents, parallel steps, MCP/plugin loading, unattended execution, automatic restart/resume, and a generic workflow language.

### Current increment — 2026-10-16

- Implemented Chat/Agent navigation, independent histories/drafts/preferences, read-only legacy Agent preservation, and Agent-mode Settings catalogue management without Chat prompt selection.
- Implemented durable BFF sessions, injected tool-free planning, immutable revision-bound approval/rejection/feedback, committed-sequence progress events, targeted Stop/settlement, and one to three approved bounded `read_file` steps.
- Verified the mocked request → review → approve → fixture read → result → reload path, including rejection, duplicate/stale approval, concurrent control events, and interruption recovery. Shared backend-generated wire fixtures are consumed by the real UI decoder; no live services are involved.
- Current storage keeps bounded full snapshots and internal logical audit data inside `events.jsonl`; separate snapshot/artifact files in the target layout below are not implemented. Existing append-only records preserve prior plan revisions. The current read/execution limits are deliberately smaller than the target proposals; see the actual contract.
- The default planner is intentionally **not configured** and returns `503 agent_not_configured`. There is no production Responses planner, autonomous LLM/function-call loop, LLM synthesis, web-search provider, or folder-grep tool yet. Clarification commands return explicit `501`; history UI uses snapshots/manual reload rather than full event replay/polling. File roots are enabled only through explicit service composition and otherwise remain empty.
- Actual first-slice contracts and bounds are documented in [bff/AGENT_API.md](bff/AGENT_API.md). The remaining sections describe the target design, not a claim that every capability already exists. User policy decisions and walkthroughs remain open in the checklist.
- Verification: 197 backend tests and 543 frontend tests pass, including all 141/398 baseline tests. Independent review findings were fixed and rechecked. No real Azure/search calls, OpenCode source/runtime reuse, or Git commits were performed.

## 2. Research findings: what to borrow from OpenCode

Upstream researched: `anomalyco/opencode`, `dev`, commit [`4f31eb987dadde52ed986ab6434a00397d3b51a6`](https://github.com/anomalyco/opencode/commit/4f31eb987dadde52ed986ab6434a00397d3b51a6), dated 2026-10-15. These findings concern the inspected V2 implementation; older documentation can describe different defaults and architecture.

| Pattern | Evidence and application here |
| --- | --- |
| Planning and execution use different policies | OpenCode's Plan profile constrains editing and instructs the model to ask the user to switch agents. That is **not** a version-bound approval of a structured plan. Implement our approval gate in backend code. [Plan source][oc-plan] |
| Tool registration, validation, and authorization are separate | Register a fixed set of three tools; validate every argument; authorize every invocation. Hiding a tool from an LLM is not authorization. [Registry][oc-tool], [runtime][oc-runtime] |
| File boundary checks precede file access | Use a shared filesystem policy for both reading and searching. Resolve allowed roots and prevent symlink escapes; a grep result can disclose the same secrets as a read. [File access][oc-files], [permissions][oc-permissions] |
| A model iteration is not a reviewed plan step | OpenCode's runner iterates model/tool interactions and can execute calls concurrently. Our coordinator must additionally own step order and allow only the current step's capabilities. [Runner][oc-runner] |
| Live updates and durable history are different | OpenCode distinguishes volatile bus subscriptions from durable events/projections. Its permission prompts are not themselves a complete durable approval ledger. Persist our approval and execution events explicitly before publishing them. [Bus][oc-bus], [permission events][oc-permission-events] |
| Tools need bounded, structured results | Borrow line numbering, pagination, truncation metadata, search citations, and cancellation patterns. Do not reproduce image/PDF readers, code execution, or provider failover initially. [Built-in tools][oc-builtins], [web-search providers][oc-websearch] |

**Firm implementation constraint:** OpenCode is a reference for architectural and behavioral ideas only. Do not copy, translate, vendor, or adapt its implementation code, and do not embed its runtime. Implement our coordinator, tools, permissions, and persistence independently in this application's Python/Angular architecture using the existing libraries and official provider SDKs. The upstream source links document research findings; they are not implementation templates.

**Important distinction:** OpenCode Plan mode can still permit research tools; this application's first release deliberately runs **no tools before approval**, including read-only tools. The planner receives tool descriptions and configured scope metadata, not permission to execute them.

## 3. Current application: reuse and integration points

### Frontend

- `ui/src/app/app.html` currently has a static mode label, one session list, session prompt controls, and the conversation/composer. Replace the label with the mode control and branch the workspace by mode.
- `ui/src/app/chat.models.ts` already recognizes `mode: 'chat' | 'agent'` and stored `type: 'Chat' | 'Agent'`. This is a discriminator, **not** an implemented agent workflow.
- `chat-state.ts` owns Chat sessions, drafts, prompt snapshots, and persistence. New sessions currently default to Chat, and selection/deletion logic is not mode-aware.
- `chat-storage.ts` uses the established `ls-assistants.chat-state` v1 format. Preserve it; do not insert plan/tool events into its text-message items.
- `ChatTurns` and `ChatEventDecoder` implement stateless Chat streaming with one reply identity. Do not route agent lifecycle events through them.
- Reuse `SseDecoder`, safe Markdown rendering, the timestamp pipe, accessible control patterns, and existing stale-callback/cancellation tests.
- Settings currently combines catalogue CRUD with selection for the active chat. In Agent mode, retain catalogue editing but provide no Agent prompt-selection context.

### Backend

- `bff/app/main.py` already owns the Azure credential and shared `AsyncOpenAI` client. Compose new agent services into its lifespan; preserve current credential selection and cleanup.
- `settings.py` supplies model/runtime configuration. `responses.py` supplies useful provider-error, deadline, retry, and stream-close helpers.
- `ChatService` currently sends no tools, retains no tool-call continuation, and treats tool-only completion as empty output. A separate tool-aware adapter is necessary.
- The locked OpenAI SDK is 2.54.0 and exposes Responses function tools, function-call outputs, and structured output. SDK support does **not** prove a particular Azure deployment supports every combination.
- [Microsoft's Responses documentation][azure-responses] documents structured outputs, function calling, and manual output-item chaining. Keep `store=False` and carry canonical output/tool items forward explicitly; do not add `previous_response_id` to Chat.
- No new framework is required: Pydantic, AnyIO, HTTPX, and the OpenAI SDK already cover the initial design.
- `bff/.gitignore` does not yet exclude sessions. Adding `/sessions/` is an implementation TODO, not a change made by this document.

## 4. Recommended architecture and ownership

```text
Angular shell
  ├── Chat workspace → existing ChatTurns / ChatApi → existing /api/chat
  └── Agent workspace → AgentState / AgentApi → /api/agent/...
                                               │
                                         AgentCoordinator
                                           ├── SessionStore
                                           ├── AgentResponses
                                           └── ToolRegistry + ToolPolicy
                                                 ├── web_search
                                                 ├── read_file
                                                 └── grep_in_folder
```

### Sources of truth and trust boundaries

- **UI:** selected mode/session, unsent drafts, rendering, and explicit user actions. An enabled button is not an execution permission.
- **BFF coordinator:** run state, current plan revision, approval, current step, tool authorization, budgets, and cancellation.
- **Session journal:** durable source of truth for Agent history. Snapshots and the UI are projections.
- **LLM:** proposes plans, tool arguments, and summaries. It cannot approve its own plan or expand its own permissions.
- **Files/search results:** untrusted evidence, never instructions that override the reviewed plan or grant approval. Do not automatically load instructions found in arbitrary files.

Suggested backend modules: `agent_schemas.py`, `agent.py`, `agent_responses.py`, `agent_routes.py`, `sessions.py`, and `tools/` with a fixed registry, shared filesystem policy, and three implementations. Keep functions/components small; use composition rather than subclassing `ChatService`.

Planner/executor instructions belong to the BFF and are versioned separately from the editable Chat system-prompt catalogue. Agent mode neither consumes that catalogue nor adds prompt-editing controls to Settings.

### Sessions, runs, and plan revisions

- One Agent **session** corresponds to one durable folder and can contain multiple user requests over time.
- Each user request creates a **run** with its own plan and execution results; allow only one active run per session.
- A revision is an immutable structured plan. Feedback during review produces a new revision, not an in-place modification of an approved plan.
- Every planning, revision, and execution operation has its own immutable server-assigned `operation_id`. Cancellation targets that operation, not whichever operation happens to be active later in the run.
- A follow-up request creates a new run and requires new approval. It never inherits approval from an earlier run.

```text
idle → planning → awaiting_approval → approved → executing → completed
          ↑              │
          └── revise ────┘
          │
          └── awaiting_clarification ── answer ──→ planning

Either waiting state may end as rejected; no tools run while waiting.
Active work may fail, or enter cancelling before cancelled/interrupted.
Execution needing new scope/information ends failed with a typed stop reason.
```

Only planning may enter `awaiting_clarification` initially. Persist a question set with a unique `clarification_id`; the planning operation then ends without an upstream stream. A reply uses `/revise` with that ID and starts a new planning operation in the same run. Plan feedback instead identifies the current plan ID/revision/digest. Use a discriminated request schema and reject stale answers/feedback. `/reject` accepts the corresponding waiting-state identity, including a clarification ID when no plan exists, and releases the session for a new run.

`cancelling` is nonterminal and visible as **Stopping…**. A Stop response acknowledges cancellation intent, not immediate completion. Keep operation ownership/capacity until local work settles; reject new run execution while it is stopping. A deadline or transport teardown cannot guarantee hard interruption of filesystem I/O or that an external provider stopped processing/billing.

### Plan and approval contract

A validated plan contains the objective, assumptions, ordered steps, expected results, available tool names, and explicit resource limits with cost estimates where available. Each step identifies its allowed tools and path/search scope; a reasoning-only step has no tools. Because planning uses no tools, label workspace/web assumptions as unverified; plan discovery within approved folder scopes rather than inventing filenames or facts.

The server assigns `plan_id`, `revision`, stable step IDs, and a digest over the canonical validated plan plus relevant execution configuration. Approval binds the session/run, exact revision/digest, model/thinking choice, and effective tool policy. Approval and its timestamp are durably recorded before execution begins.

The plan describes **what will be done and why**, not private chain-of-thought. The LLM may choose arguments within a reviewed scope; it may not invent a broader scope. The proposed conservative search policy binds exact query strings and result limits; this remains a product decision, not a requirement implied by version-bound approval. An alternative is permission for model-selected queries under explicit call/result limits, with correspondingly broader outbound-data permission. Do not claim a natural-language topic boundary is mechanically enforceable.

Under the conservative proposal, additional queries, wider folders, new tools, reordered substantive steps, or changed model/policy require renewed review. Before execution, use a new plan revision. During execution, stop with partial results and `failed` / `scope_change_required` (or `clarification_required`); require a **new run and fresh approval**, not an undefined same-run resume. The new planner may use identified, previously recorded evidence as untrusted context, never as inherited approval or proof that old operations should be replayed.

**Approve and run** is one idempotent backend command: validate the expected revision, persist approval, then execute. Duplicate submission must not execute twice; stale approval returns a conflict. Rejection/revision never starts execution.

Revise/reject are serialized transitions from the matching waiting state; execute is allowed only from `awaiting_approval` for the matching plan. Approval claims a revision for execution once, independently of the command's idempotency key. After failure, cancellation, or interruption, another execution requires a new run and fresh approval; a new request ID alone cannot reuse approval.

### Execution policy

1. The coordinator selects the next approved step and exposes only its allowed tool schemas.
2. The LLM produces text and/or function calls. Collect complete arguments and validate the terminal provider response before dispatch; never execute partial streamed arguments.
3. Validate the tool name, arguments, current-step scope, session/run state, and remaining budget. Persist the call intent and policy decision before invoking it.
4. Execute tools sequentially; preserve canonical response items and matching `call_id` function outputs for subsequent LLM rounds. Tool-only responses are valid intermediate results.
5. Persist tool outcomes and step results before publishing them or advancing. An LLM completion without the expected result is not automatically a successful step.
6. Produce a final synthesis using recorded evidence. It may not invent successful tool results or run extra tools outside the plan.

Use `parallel_tool_calls=False` where supported, but enforce serial execution in the coordinator regardless. Bound steps, rounds, calls, context/output size, and active operations globally as well as per session. Starting defaults to validate: 10 steps, 4 LLM rounds per step, 20 tool calls per run, and a 600-second execution dispatch deadline; human waiting is outside active-operation deadlines, but cancellation settlement can exceed them.

Run allowances must count planning, clarification replies, revisions, execution, synthesis, retries, and already-submitted cancelled requests—not just successful execution steps. Set total LLM-attempt/search-call allowances and per-call input/output caps in Phase 0; reserve before dispatch, retain unknown usage after interruption, and stop when the allowance is exhausted. These are enforceable resource bounds, **not a guaranteed currency ceiling**. Display estimated/unknown cost honestly instead of adding a billing subsystem.

Build shared AgentResponses audit/retry instrumentation **before the first planner call**. Disable SDK-managed retries for Agent requests using Agent-only client options; the coordinator owns and journals permitted attempts for planning, revisions, and execution alike. For reasoning deployments requiring encrypted stateless continuation, request `include=['reasoning.encrypted_content']` where supported, retain the required canonical output items, and replay them with the corresponding function-call outputs. Keep opaque continuation data separate from sanitized display projections; fail clearly for unsupported combinations.

Do not silently replan, repeat ambiguous operations, or compact away audit evidence. Stop with a useful result/error when a budget is exhausted or new scope is required.

## 5. Tool contracts and safety policy

| Tool | Initial contract | Bounds and result shape |
| --- | --- | --- |
| `read_file` | `workspace_id`, relative `path`, one-based `offset`, `limit` | Text-only regular files; numbered lines, total/truncation metadata, safe errors. Start with at most 2,000 lines / 50 KiB per page; cap individual line length. |
| `grep_in_folder` | `workspace_id`, relative `folder`, literal `pattern`, optional file glob, `max_matches` | Literal search initially; bounded file count, bytes scanned, time, line length, and 100 returned matches. Return relative path/line/excerpt and explicit truncation metadata. |
| `web_search` | Approved `query`, bounded `max_results` | One configured provider; start with at most 5 results containing title, URL, bounded snippet, and available date/source metadata. Record the provider and exact query. |

Filesystem policy is shared by read and grep:

- Allowed workspace roots are explicit server configuration, never arbitrary client-supplied absolute directories. Start fail-closed until configured.
- Canonical containment is necessary but insufficient. Reject symlink traversal and use no-follow, descriptor-based access where supported; test path replacement races as well as `..` and absolute-path escapes.
- Deny secret/environment/credential files, `.git`, dependency/virtual-environment directories, private archives such as `dev-plans-archive`, and `bff/sessions`. Apply exclusions during traversal **and** final open. Ignore files may narrow access, never widen it.
- Reject special files, binary files, oversized reads, unsupported encodings, and invalid offsets/globs. Filename rules are not complete secret-content detection; record that limitation.
- Prefer a bounded literal scanner over adding a required system binary for the first release. No unbounded Python regex. If regex/ripgrep is added later, use explicit arguments without a shell, shared exclusions, and subprocess timeout/cleanup tests.
- Run blocking filesystem work off the event loop with cooperative stop/deadline checks between bounded reads. Cancelling an await does not kill a worker thread: do not report settled cancellation or start replacement work while the underlying worker still runs. Hard interruption of arbitrary filesystem I/O is not promised.

Web-search policy:

- **Recommended initial option:** one external search adapter implemented with existing HTTPX; Tavily is a candidate pending confirmation of credentials, cost, and data handling. Keep the provider behind a small injected interface.
- Do not assume Azure-hosted web search works because SDK types exist. If chosen instead, verify deployment support and preserve the same approval/audit controls.
- No provider credentials in Angular, logs, or tool results; no silent provider fallback. Missing configuration produces a capability-validation error before approval, not a pre-approval tool invocation.
- Search queries leave the BFF. Show the effective query policy and, under the conservative proposal, exact reviewed queries; do not automatically send file contents or secrets as search terms. Search results are untrusted text. Opening returned URLs is not one of the three initial tools.
- Initial web evidence consists of search-provided snippets, not verified full-page content. Final answers must distinguish those snippets from files actually read and must not imply linked pages were opened. Validate a realistic mixed workspace/search task with this limitation before declaring the workflow useful.

## 6. Durable session recording and recovery

Example layout:

```text
bff/sessions/
  20261015T153000123456Z-a7q2/
    events.jsonl          # authoritative ordered journal
    session.json          # atomically replaced, rebuildable projection
    plans/
      <run-id>-r1.json     # immutable validated plan
    artifacts/
      <artifact-id>.json   # bounded LLM/tool payloads referenced by journal
```

Generate names on the server using a UTC timestamp and four cryptographically random lowercase alphanumeric characters. Create directories exclusively and retry collisions. A short suffix is not a security token. Never use a client title/path as a directory name; validate session IDs before resolving existing folders. Use restrictive file/directory permissions and reject symlinked storage/artifact paths.

Each event has `schema_version`, monotonic per-session `sequence`, server UTC `time`, `session_id`, `run_id` where applicable, `operation_id` where applicable, `request_id`, `type`, and typed data. Add `plan_id/revision`, `step_id`, `llm_call_id`, and `tool_call_id` as applicable. SSE event IDs use the persisted sequence, not a second in-memory counter.

Record all observable lifecycle and work:

- User requests, clarification/feedback, plans/revisions, approval/rejection and scope.
- Every LLM attempt: safe request payload, instructions/version, model/options, observable response content/items, final status, usage if available, retry/backoff, and errors. Adjacent text deltas may be coalesced without losing ordered text; raw network/token-frame capture is not required for a complete logical interaction record.
- Every tool request, validated arguments, permission decision, start, bounded result/error, truncation, and cancellation outcome.
- Step transitions/results, final answer, stop/disconnect, deadline, crash-recovery findings, and archive actions.

Persist before publishing: append durable intent before external work, store result artifacts before their journal references, and commit outcomes before advancing. Serialize writes per session. Flush/fsync required files before acknowledging approval or publishing committed results; fsync parent-directory changes for new/atomically replaced files where supported. Snapshots are rebuildable and never the approval source of truth. Target abrupt process-restart recovery on a supported local filesystem; document storage/platform limits rather than promise immunity to every hardware/power failure.

For idempotent session creation, the first committed `session.created` record contains the creation key and canonical request digest. Rebuild the creation-key lookup from those records at startup and serialize creation under a process-wide lock. An index may cache this mapping but is not another authoritative store. Quarantine uncommitted orphan folders; never acknowledge their creation or execute from them. Process-local concurrency capacity resets on restart; committed command claims and retained disk usage do not. Recovery marks unfinished work interrupted before admitting new commands.

On journal/artifact failure, prohibit further dispatch, advancement, and publication of uncommitted output. Signal cancellation and retain operation ownership/capacity until already-started work settles; do not report settled cancellation prematurely. If the terminal outcome cannot be persisted, recovery treats the unmatched durable intent as interrupted with outcome unknown. A failed disk cannot guarantee a final log entry.

Record bounded application/provider data with explicit credential-field exclusions: **not** HTTP authorization headers, bearer tokens, credential objects, settings dumps, or raw exception bodies. This is not guaranteed secret-content removal from permitted user/source text. Retain approved user content, permitted source excerpts, reviewed query data, logical LLM requests/results, and tool results; make that retention visible before use. Do not request or expose hidden chain-of-thought. If stateless reasoning continuation requires an encrypted provider item, preserve it as opaque internal data, not a human-readable reasoning log; do not mutate required continuation items into invalid protocol data while preparing safe UI projections.

On restart, rebuild projections from the journal, tolerate/quarantine an incomplete final JSONL record, and mark unfinished operations interrupted with any external outcome recorded as unknown. Do not infer success or automatically replay work. Existing plan revisions remain reviewable, but executing again requires a new run and fresh approval.

Use configurable disk/session/artifact limits and fail before new work when limits are reached. Preserve bounded results and truncation metadata rather than retaining unbounded provider payloads; if required continuation cannot fit, stop rather than continue with a silently truncated protocol record. Do not silently delete history. Proposed initial retention is archive-only with no automatic expiry: the history delete action **archives** an Agent session only after local work settles, and archived folders remain reviewable. If stopping is still pending, return `202` with pending status; do not claim archival completed. Retention/content scope needs user confirmation; permanent deletion/expiry automation remains a separate policy rather than an implicit promise.

## 7. Agent API and streaming contract

Keep existing `/api/chat`, `/api/models`, and `/health` contracts unchanged. Proposed separate endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/agent/sessions` | List journal-backed summaries, with an archive filter and bounded pagination. |
| `POST /api/agent/sessions` | Create an empty session; idempotent, no LLM call. |
| `GET /api/agent/sessions/{sid}` | Return snapshot plus last committed sequence; no inference. |
| `PATCH /api/agent/sessions/{sid}` | Persist title/next-run preferences; reject model/thinking changes while a run is planning, awaiting clarification/approval, executing, or cancelling. |
| `POST /api/agent/sessions/{sid}/runs` | Record a user request and stream planning; finish at review/clarification, without tools. |
| `POST /api/agent/sessions/{sid}/runs/{rid}/revise` | Accept a clarification answer bound to its question ID or plan feedback bound to its revision; start a new planning operation. |
| `POST /api/agent/sessions/{sid}/runs/{rid}/reject` | Reject the matching clarification/plan waiting state without execution; no nonexistent plan digest is required for clarification. |
| `POST /api/agent/sessions/{sid}/runs/{rid}/execute` | Validate submitted plan ID/revision/digest, durably approve, then stream sequential execution. |
| `POST /api/agent/sessions/{sid}/runs/{rid}/cancel` | Request Stop for the supplied `operation_id`; return `202` while cancelling or its known settled outcome, never target a newer operation. |
| `GET /api/agent/sessions/{sid}/events?after={sequence}` | Paginated durable event replay for recovery and history review. |
| `POST /api/agent/sessions/{sid}/archive` | Record archive intent, request targeted cancellation if needed, and archive after settlement; `202` while pending. |

Mutation requests use a client request ID/idempotency key bound to the request payload; reusing a key with different content is a conflict. Persist accepted commands and outcomes. Use a per-session operation lock and an explicit single-BFF-worker constraint initially; multiple workers require inter-process coordination, not just an asyncio lock.

Agent SSE events have their own strict discriminated schema: session/run/plan state, text deltas, step state/results, tool state/results, and terminal outcome. Every durable event carries correlation IDs, sequence, and server timestamp. Whitelist exposed fields; raw internal artifacts are not an unaudited static directory.

Planning and execution are finite, **request-owned** operations initially. Disconnect requests cancellation of its owning operation, eventually settling as interrupted; explicit Stop eventually settles the targeted operation as cancelled. The root BFF coordinator retains task ownership solely to settle already-started local work after transport teardown—not to keep executing the plan. A bounded transport-cleanup window is not a hard worker-termination guarantee. Stop for an already-settled operation returns its recorded outcome and cannot cancel a later revision/execution. Human waiting holds no upstream stream or background loop. Switching modes must not abort an operation—the root-scoped UI coordinator remains alive—but reload/network loss may interrupt it. Recovery fetches snapshot/events; it never re-POSTs execution automatically or takes ownership of existing work. A duplicate in-flight command returns its known operation/conflict, not another execution.

Preserve absolute deadlines over provider acquisition/retries/reads and transport sends, plus bounded shielded cleanup. Agent history/replay requests have no execution side effects. Reserve background job infrastructure for a later requirement to continue after browser disconnection.

## 8. Frontend behavior and persistence

- Use native radio inputs styled as a segmented control with accessible Chat/Agent labels. Exactly one mode is selected; preserve keyboard navigation and focus on mobile/desktop.
- Treat switching as navigation, not converting a Chat session into an Agent session. Remember the last selected session independently for each mode.
- Chat history shows only Chat sessions. Agent history loads its own BFF session summaries and count; BFF failures must not fall back to displaying Chat sessions as Agent sessions.
- Preserve Chat's v1 storage and existing draft/model/thinking behavior. Add a separately versioned `ls-assistants.agent-ui-state` key for selected mode, selected Agent session, and unsent Agent drafts. Agent plans/results come from BFF, not authoritative browser copies.
- Legacy browser `Agent` records may exist. Preserve them in the old envelope and offer a read-only legacy section in Agent history; do not silently drop, convert, upload, or execute them. Exclude them from Chat streaming-status repairs, prompt-catalogue propagation, and Chat mutation entry points. Apart from selection normalization, preserve their content and supported stored metadata. Explicit import can be a later feature.
- Keep `ChatState`'s active selection pointing to a Chat session during normal navigation; normalize a legacy selected Agent to a Chat fallback without losing that legacy record. Guard Chat send/retry/prompt-selection entry points against Agent sessions.
- New session targets the selected mode; Agent creation uses BFF-generated identity. Selecting/deleting a session never changes the other mode's draft or selection.
- Hide the entire session prompt section and Chat-specific empty-state wording in Agent mode. Keep Settings visible and preserve its prompt CRUD/editor. Disable/omit only the active-chat selection context when none exists; editing the catalogue must not assign an Agent prompt or mutate locked Chat snapshots.
- Keep the existing model/thinking controls but bind them to the active mode/session. Persist Agent preferences in BFF. Freeze choices when planning starts and disable Agent model/thinking changes through clarification/review/execution/cancellation; reject a waiting run or await settlement before choosing different settings for a new run. Chat's existing preference behavior remains unchanged.
- Render Agent messages plus a structured plan card: objective/unverified assumptions, numbered steps, allowed tools/folders, effective search policy and exact queries when required, model/thinking settings, resource limits, and **Approve and run / Request changes / Reject**. The displayed revision/digest must be the submitted approval identity. Do not implement approval by parsing Markdown checkboxes or model text.
- Render pending/running/completed/failed step states, expandable bounded tool arguments/results, search citations, final output, and an audit timeline. Use the existing safe Markdown/timestamp rendering; never execute returned HTML or URLs.
- Provide Stop with a visible nonterminal Stopping state, clarification answer/reject controls, recoverable errors/partial results, and read-only history. Reconcile sequence duplicates/gaps and stale session/run/revision callbacks. Avoid auto-scroll pulling the user away from a plan they are reviewing.

## 9. Delivery strategy and task ownership

[AGENT_MODE_CHECKLIST.md](AGENT_MODE_CHECKLIST.md) is the **only completion/evidence ledger**. Its phase/task IDs organize capabilities, not a requirement to finish every backend layer before showing a usable interface. Update this design only for contract/architecture changes; record implementation status in the checklist.

### First milestone: a thin mocked vertical slice

After contracts and minimum durable storage/approval primitives, bring forward the necessary subset of tools, API, navigation, and review UI:

**Chat/Agent switch → request → mocked tool-free plan → displayed approval scope → one approved fixture-backed `read_file` step → recorded result → reload/history.**

Include rejection, duplicate approval, and Stop checks. Durable approval, bounded access, idempotency, and Chat regression protection are not deferred. Use injected provider/search doubles, not real paid calls or a second throwaway implementation. A broad task remains unchecked until all of its associated work/tests are complete.

Then complete the remaining tools, multi-step execution, error/recovery paths, and full history UI. This validates the user's interaction early without relaxing the release gates.

| Phase | Capability and required completion evidence |
| --- | --- |
| 0 | Explicit product/contracts decisions and a clean Chat baseline. |
| 1 | Journal-backed storage, creation idempotency, limits, and crash recovery. |
| 2 | Three bounded tools sharing enforced access policy. |
| 3 | Tool-free planning, shared LLM audit/retries, clarification, and exact-version approval. |
| 4 | Sequential current-step execution with complete logical interaction records. |
| 5 | Idempotent Agent APIs, replay, and truthful cancellation/settlement status. |
| 6 | Isolated mode navigation, histories, drafts, and Settings behavior. |
| 7 | Reviewable scope, step progress, clarification/Stop, and archived history. |
| 8 | Mocked integration, realistic-task evaluation, full regressions, and documentation. |

Real Azure/search smoke testing remains optional and requires explicit authorization/configuration. Do not confuse passing mocks with verified live deployment compatibility.

## 10. Verification and definition of done

Frontend, from `ui/`, activating the installed default Node first:

```bash
source "$HOME/.nvm/nvm.sh" && nvm use default
npm test -- --watch=false
npx --no-install tsc -p tsconfig.spec.json --noEmit
npm run build
npx --no-install prettier --check .
```

Backend, from `bff/`:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv lock --check
```

Repository checks: `git diff --check`, `git status --short`, and `git check-ignore bff/sessions/example/events.jsonl` after the ignore rule is implemented. Use temporary directories and mocked provider/search clients in tests; do not inspect real `.env` files, private archives, or user session logs.

Done means: modes are mutually exclusive and histories independent; Agent hides session prompts while Settings still edits Chat prompts; no tool runs before exact-plan approval; clarification, scope changes, and stopping have complete observable paths; execution is sequential and resource-bounded; every logical LLM/tool interaction and step is durably reviewable; reload never silently repeats work; all existing Chat tests and the new Agent tests pass. Cancellation means no further dispatch and eventual local settlement, not a hard external rollback guarantee.

## 11. Decisions needed from the user before implementation

1. **Web search:** choose the first external provider (recommend one simple HTTP adapter; Tavily is a candidate) or explicitly choose Azure-hosted search after a capability check. Credentials and costs must be confirmed separately.
2. **Filesystem scope:** choose which directories the Agent may read/search. Recommendation: explicitly configured workspace roots with secrets, private archives, and session logs always excluded—not unrestricted access to the machine.
3. **Search autonomy and allowances:** retain exact-query approval or permit model-selected queries with bounded calls/results? The latter is more useful for discovery but grants broader outbound-query permission. Confirm total LLM/search allowances across planning, revisions, execution, and synthesis; resource caps are not a hard price cap.
4. **Retained content/history:** confirm bounded logical input/output recording, including approved source/user content and required opaque continuation, and the proposed archive-only/no-auto-expiry behavior. Credential-field exclusion is not guaranteed content-secret scrubbing. Raw token/network-frame capture is not needed for the requested reviewable interaction history.

Unless changed before implementation, this plan proposes literal grep, read-only tools, no pre-approval research, conservative exact-query approval, a single BFF worker, request-owned execution with retained cancellation settlement, and archive-not-erase history. These remain documented proposals until Phase 0 decisions are confirmed.

[oc-plan]: https://github.com/anomalyco/opencode/blob/4f31eb987dadde52ed986ab6434a00397d3b51a6/packages/core/src/plugin/plan.ts#L14-L42
[oc-tool]: https://github.com/anomalyco/opencode/blob/4f31eb987dadde52ed986ab6434a00397d3b51a6/packages/core/src/tool.ts
[oc-runtime]: https://github.com/anomalyco/opencode/blob/4f31eb987dadde52ed986ab6434a00397d3b51a6/packages/core/src/tool/runtime.ts
[oc-files]: https://github.com/anomalyco/opencode/blob/4f31eb987dadde52ed986ab6434a00397d3b51a6/packages/core/src/file-access.ts
[oc-permissions]: https://github.com/anomalyco/opencode/blob/4f31eb987dadde52ed986ab6434a00397d3b51a6/packages/core/src/permission.ts
[oc-runner]: https://github.com/anomalyco/opencode/blob/4f31eb987dadde52ed986ab6434a00397d3b51a6/packages/core/src/session/runner/step.ts
[oc-bus]: https://github.com/anomalyco/opencode/blob/4f31eb987dadde52ed986ab6434a00397d3b51a6/packages/core/src/bus.ts
[oc-permission-events]: https://github.com/anomalyco/opencode/blob/4f31eb987dadde52ed986ab6434a00397d3b51a6/packages/schema/src/permission.ts
[oc-builtins]: https://github.com/anomalyco/opencode/tree/4f31eb987dadde52ed986ab6434a00397d3b51a6/packages/core/src/tool/plugin
[oc-websearch]: https://opencode.ai/v2/docs/websearch/
[azure-responses]: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses?tabs=python
