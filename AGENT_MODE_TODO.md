# Agent mode: research and design plan

Research date: 2026-10-15. Implementation update: 2026-10-20. Policy clarification: 2026-10-19. Status: the injected-planner slice supports approved file reads and public-page fetching; the full Agent release is not complete.

Implementation progress is tracked only in [AGENT_MODE_CHECKLIST.md](AGENT_MODE_CHECKLIST.md). This document owns the design and contract decisions, not a second completion ledger.

## 1. Goal and first-release scope

Add an Agent workflow beside the completed Chat workflow:

1. A mutually exclusive **Chat / Agent** control in the main header.
2. Independent session histories, selected sessions, drafts, and per-session model/thinking preferences.
3. No session-level system-prompt controls in Agent mode. Settings continues to manage the existing Chat prompt catalogue.
4. Four backend tools: `web_search`, `web_fetch`, `read_file`, and `grep_in_folder`.
5. A structured plan that the user reviews before explicitly choosing **Approve and run**.
6. Sequential execution with visible step results and cancellation.
7. A durable, reviewable record under `bff/sessions/<timestamp>-<4-random-chars>/`, excluded from Git.

**Recommendation:** build a small, backend-owned coordinator using the existing Responses SDK, not a new agent framework or an embedded OpenCode process. Keep `/api/chat` and Chat persistence behavior unchanged.

Out of scope initially: file writes, shell execution, browser/JavaScript automation, private/internal-network fetching, non-HTTP(S) URLs, arbitrary file downloads, autonomous subagents, parallel steps, MCP/plugin loading, unattended execution, automatic restart/resume, and a generic workflow language.

### Confirmed operating rules — 2026-10-19

1. **Approve each plan once before execution.** Steps, tool calls, search queries, and retries within that plan do not require separate approval. A new or revised executable plan requires approval again.
2. **No product-level quotas.** Do not cap plan steps, LLM interactions, tool/search calls, total run duration, or the number of retained plans/sessions. Web search is task-dependent, not a mandatory phase. Actual provider/model constraints, per-request failure handling, and paged I/O are operational facts, not cumulative task allowances.
3. **Record every LLM interaction** inside its session folder under `bff/sessions`, with plan/step identity, timestamp, LLM/model name, actual prompt/input, and observable response/output. Include planning, revisions, execution, synthesis, retries, and unsuccessful attempts. Authentication credentials are not part of the prompt and must not be logged.

These rules replace the earlier exact-query approval and run-budget proposals. The current prototype still contains quotas and abbreviated recording; removing those restrictions and completing the audit trail are implementation tasks, not changes already made by editing this plan.

**MVP scope addition (2026-10-19):** `web_fetch` is required alongside search. Search finds candidate sources; fetch reads an actual public page, including a URL supplied directly by the user without a preceding search. Both are task-dependent and covered by the plan's single approval. Completion and verification evidence belong in the checklist.

### Historical first increment — 2026-10-16

This snapshot records the first slice, not current capabilities. Later increments supersede it; see the checklist's latest verification record and the current backend API document.

- Implemented Chat/Agent navigation, independent histories/drafts/preferences, read-only legacy Agent preservation, and Agent-mode Settings catalogue management without Chat prompt selection.
- Implemented durable BFF sessions, injected tool-free planning, immutable revision-bound approval/rejection/feedback, committed-sequence progress events, targeted Stop/settlement, and one to three approved bounded `read_file` steps.
- Verified the mocked request → review → approve → fixture read → result → reload path, including rejection, duplicate/stale approval, concurrent control events, and interruption recovery. Shared backend-generated wire fixtures are consumed by the real UI decoder; no live services are involved.
- Current storage keeps bounded full snapshots and internal logical audit data inside `events.jsonl`; separate snapshot/artifact files in the target layout below are not implemented. Existing append-only records preserve prior plan revisions. Prototype step/call/session/journal caps and overall planning/execution deadlines are not the clarified product policy and must be migrated away; see the actual current contract.
- The default planner is intentionally **not configured** and returns `503 agent_not_configured`. There is no production Responses planner, autonomous LLM/function-call loop, LLM synthesis, web-search provider, or folder-grep tool yet. Clarification commands return explicit `501`; history UI uses snapshots/manual reload rather than full event replay/polling. File roots are enabled only through explicit service composition and otherwise remain empty.
- Actual first-slice contracts and bounds are documented in [bff/AGENT_API.md](bff/AGENT_API.md). The remaining sections describe the target design, not a claim that every capability already exists. User policy decisions and walkthroughs remain open in the checklist.
- Verification: 197 backend tests and 543 frontend tests pass, including all 141/398 baseline tests. Independent review findings were fixed and rechecked. No real Azure/search calls, OpenCode source/runtime reuse, or Git commits were performed.

## 2. Research findings: what to borrow from OpenCode

Upstream researched: `anomalyco/opencode`, `dev`, commit [`4f31eb987dadde52ed986ab6434a00397d3b51a6`](https://github.com/anomalyco/opencode/commit/4f31eb987dadde52ed986ab6434a00397d3b51a6), dated 2026-10-15. These findings concern the inspected V2 implementation; older documentation can describe different defaults and architecture.

| Pattern | Evidence and application here |
| --- | --- |
| Planning and execution use different policies | OpenCode's Plan profile constrains editing and instructs the model to ask the user to switch agents. That is **not** a version-bound approval of a structured plan. Implement our approval gate in backend code. [Plan source][oc-plan] |
| Tool registration, validation, and authorization are separate | Register the four MVP tools; validate every argument; authorize every invocation. Hiding a tool from an LLM is not authorization. [Registry][oc-tool], [runtime][oc-runtime] |
| File boundary checks precede file access | Use a shared filesystem policy for both reading and searching. Resolve allowed roots and prevent symlink escapes; a grep result can disclose the same secrets as a read. [File access][oc-files], [permissions][oc-permissions] |
| A model iteration is not a reviewed plan step | OpenCode's runner iterates model/tool interactions and can execute calls concurrently. Our coordinator must additionally own step order and allow only the current step's capabilities. [Runner][oc-runner] |
| Live updates and durable history are different | OpenCode distinguishes volatile bus subscriptions from durable events/projections. Its permission prompts are not themselves a complete durable approval ledger. Persist our approval and execution events explicitly before publishing them. [Bus][oc-bus], [permission events][oc-permission-events] |
| Tools need bounded, structured results | Borrow line numbering, pagination, truncation metadata, search citations, and cancellation patterns. Do not reproduce image/PDF readers, code execution, or provider failover initially. [Built-in tools][oc-builtins], [web-search providers][oc-websearch] |

**Firm implementation constraint:** OpenCode is a reference for architectural and behavioral ideas only. Do not copy, translate, vendor, or adapt its implementation code, and do not embed its runtime. Implement our coordinator, tools, permissions, and persistence independently in this application's Python/Angular architecture using the existing libraries and official provider SDKs. The upstream source links document research findings; they are not implementation templates.

Provider selection verified on 2026-10-19: the inspected V2 implementation supports Exa, Firecrawl, Parallel, Tavily, and TinyFish. It uses configured/remembered selection or asks the user; there is no single hardwired provider. Its consent flow also offers random provider selection. The user selected Tavily for our first adapter on the same date; that is our independent choice, not a claim about OpenCode's default. See [web-search documentation][oc-websearch] and the [selection implementation][oc-websearch-selection].

**Important distinction:** OpenCode Plan mode can still permit research tools; this application's first release deliberately runs **no tools before approval**, including read-only tools. The planner receives tool descriptions and configured scope metadata, not permission to execute them.

## 3. Research baseline: reuse and integration points

These notes describe the pre-Agent application inspected on 2026-10-15. They are historical integration context, not a current feature or completion list.

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
                                                 ├── web_fetch
                                                 ├── read_file
                                                 └── grep_in_folder
```

### Sources of truth and trust boundaries

- **UI:** selected mode/session, unsent drafts, rendering, and explicit user actions. An enabled button is not an execution permission.
- **BFF coordinator:** run state, current plan revision, approval, current step, tool authorization, interaction recording, and cancellation.
- **Session journal:** durable source of truth for Agent history. Snapshots and the UI are projections.
- **LLM:** proposes plans, tool arguments, and summaries. It cannot approve its own plan or expand its own permissions.
- **Files/search results/fetched pages:** untrusted evidence, never instructions that override the reviewed plan or grant approval. Do not automatically load instructions found in arbitrary files or web pages.

Suggested backend modules: `agent_schemas.py`, `agent.py`, `agent_responses.py`, `agent_routes.py`, `sessions.py`, and `tools/` with a fixed registry, shared filesystem/network policies, and four implementations. Keep functions/components small; use composition rather than subclassing `ChatService`.

Planner/executor instructions belong to the BFF and are versioned separately from the editable Chat system-prompt catalogue. Agent mode neither consumes that catalogue nor adds prompt-editing controls to Settings.

### Sessions, runs, and plan revisions

- One Agent **session** corresponds to one durable folder and can contain multiple user requests over time.
- Each user request creates a **run** with its own plan and execution results; allow only one active run per session.
- A revision is an immutable structured plan. Feedback during review produces a new revision, not an in-place modification of an approved plan.
- Every planning, revision, and execution operation has its own immutable server-assigned `operation_id`. Cancellation targets that operation, not whichever operation happens to be active later in the run.
- A follow-up that produces a new executable plan requires approval of that plan; it does not inherit approval from an earlier plan. Clarification or ordinary calls within the existing approved plan do not add an approval prompt.

```text
idle → planning → awaiting_approval → approved → executing → completed
          ↑              │
          └── revise ────┘
          │
          └── awaiting_clarification ── answer ──→ planning

Either waiting state may end as rejected; no tools run while waiting.
Active work may fail, or enter cancelling before cancelled/interrupted.
Execution needing a genuinely new plan pauses for that plan's approval.
```

Only planning may enter `awaiting_clarification` initially. Persist a question set with a unique `clarification_id`; the planning operation then ends without an upstream stream. A reply uses `/revise` with that ID and starts a new planning operation in the same run. Plan feedback instead identifies the current plan ID/revision/digest. Use a discriminated request schema and reject stale answers/feedback. `/reject` accepts the corresponding waiting-state identity, including a clarification ID when no plan exists, and releases the session for a new run.

`cancelling` is nonterminal and visible as **Stopping…**. A Stop response acknowledges cancellation intent, not immediate completion. Keep operation ownership/capacity until local work settles; reject new run execution while it is stopping. A deadline or transport teardown cannot guarantee hard interruption of filesystem I/O or that an external provider stopped processing/billing.

### Plan and approval contract

A validated plan contains the objective, assumptions, ordered steps, expected results, and intended tools. Tool usage depends on the task: a reasoning-only plan needs no tools, and a valid plan may contain no web search. Because planning uses no tools, label workspace/web assumptions as unverified; plan discovery within the configured filesystem scope rather than inventing filenames or facts. Do not require task-budget fields or a fixed list of search queries.

The server assigns `plan_id`, `revision`, stable step IDs, and a digest over the canonical validated plan plus relevant execution configuration. Approval binds the session/run, exact revision/digest, model/thinking choice, and effective tool policy. Approval and its timestamp are durably recorded before execution begins.

The plan describes **what will be done and why**, not private chain-of-thought. Approval covers execution of that plan, including task-dependent tool choices, arguments, search queries, and ordinary retries. Do not interrupt the user for individual calls or query refinements. The coordinator still enforces configured tool availability and the `/tmp/ls-assistants` filesystem boundary; these checks are not additional approval prompts.

If the Agent creates a new or revised executable plan, retain the existing results, present that plan, and obtain approval before executing it. Finishing a plan does not authorize the next one. A tool retry or different query alone does not create a new plan. Previously recorded evidence may inform later planning, but is not itself approval. Errors/cancellation must not silently manufacture a new plan or replay completed work; automatic execution resume remains out of scope.

**Approve and run** is one idempotent backend command: validate the expected revision, persist approval, then execute. Duplicate submission must not execute twice; stale approval returns a conflict. Rejection/revision never starts execution.

Revise/reject are serialized transitions from the matching waiting state; execute is allowed only after approval of the matching plan. Claim each execution once independently of the command's idempotency key so duplicate submissions cannot repeat work. Retries during that execution do not need approval again; a new plan does. Failure/interruption is recorded honestly, not treated as permission to auto-replay an execution.

### Execution policy

1. The coordinator selects the next approved step and exposes the available tools appropriate to it; a step need not use any tool.
2. The LLM produces text and/or function calls. Collect complete arguments and validate the terminal provider response before dispatch; never execute partial streamed arguments.
3. Validate the tool name, arguments, applicable filesystem/network boundary, and session/run state. Persist the call intent before invoking it; no additional user approval is required for a call within the plan.
4. Execute tools sequentially; preserve canonical response items and matching `call_id` function outputs for subsequent LLM rounds. Tool-only responses are valid intermediate results.
5. Persist tool outcomes and step results before publishing them or advancing. An LLM completion without the expected result is not automatically a successful step.
6. Produce a final synthesis using recorded evidence. It may not invent successful tool results or run extra tools outside the plan.

Use `parallel_tool_calls=False` where supported, but enforce serial execution in the coordinator regardless. Do not impose cumulative step, round, LLM/tool/search-call, or total-run time quotas. Execution follows the approved plan until completion, user Stop, a new-plan requirement, or an operational failure.

Use per-provider/request timeouts, paged file/search results, and supported model context/output controls where technically necessary. These must not become hidden run quotas or discard the retained LLM input/output. Report provider constraints or actual failures explicitly. Usage/cost metadata is informational, not an execution allowance or guaranteed currency ceiling.

Build shared AgentResponses audit/retry instrumentation **before the first planner call**. Disable SDK-managed retries for Agent requests using Agent-only client options; the coordinator owns and journals permitted attempts for planning, revisions, and execution alike. For reasoning deployments requiring encrypted stateless continuation, request `include=['reasoning.encrypted_content']` where supported, retain the required canonical output items, and replay them with the corresponding function-call outputs. Keep opaque continuation data separate from sanitized display projections; fail clearly for unsupported combinations.

Do not silently execute a new plan, repeat ambiguous operations, or compact away audit evidence. A hung/failed request or unavailable storage is an operational error, not a fabricated task-budget-exhaustion result.

## 5. Tool contracts and safety policy

| Tool | Initial contract | Paging and result shape |
| --- | --- | --- |
| `read_file` | `workspace_id`, relative `path`, one-based `offset`, page size | Text-only regular files; numbered lines, continuation/truncation metadata, safe errors. Further pages remain available within the same approved plan; a page size is not a task-wide read allowance. |
| `grep_in_folder` | `workspace_id`, relative `folder`, literal `pattern`, optional file glob, page size | Literal search initially; incremental traversal and paged path/line/excerpt results. Report partial pages honestly and permit continuation rather than imposing a cumulative match/scan quota. |
| `web_search` | Task-dependent `query`, provider-supported result-page size | Tavily results include title, URL, snippet, and available date/source metadata. Record actual queries. There is no required search step, minimum number of searches, or per-run search-call cap. |
| `web_fetch` | Public HTTP(S) `url`, optional content-page selection | GET the page and return extracted HTML main text or plain text, requested/final URL, title when available, HTTP status/content type, and retrieval timestamp. Preserve fetched evidence and expose continuation for long text; no browser/JavaScript execution or implicit file download. |

Filesystem policy is shared by read and grep:

- **Confirmed implementation-phase scope (2026-10-19): `/tmp/ls-assistants` is the sole approved filesystem-tool root.** `read_file` and `grep_in_folder` may access only that directory and permitted descendants; neither the client nor the model may add roots. Keep access disabled until this scope is wired into server configuration.
- Account for the platform's `/tmp` alias during trusted root configuration without granting access to `/tmp` or `/private/tmp` generally. Reject a symlinked workspace root or descendant that escapes the approved boundary; do not solve platform aliasing by enabling arbitrary symlink traversal. Missing/unusable roots fail closed.
- Canonical containment is necessary but insufficient. Reject symlink traversal and use no-follow, descriptor-based access where supported; test path replacement races as well as `..` and absolute-path escapes.
- Deny secret/environment/credential files, `.git`, dependency/virtual-environment directories, private archives such as `dev-plans-archive`, and `bff/sessions`. Apply exclusions during traversal **and** final open. Ignore files may narrow access, never widen it.
- Reject special/binary files, unsupported encodings, and invalid offsets/globs. Read large supported text files through pages rather than rejecting them solely because of an application-defined file-size quota. Filename rules are not complete secret-content detection; record that limitation.
- Prefer a bounded literal scanner over adding a required system binary for the first release. No unbounded Python regex. If regex/ripgrep is added later, use explicit arguments without a shell, shared exclusions, and subprocess timeout/cleanup tests.
- Run blocking filesystem work off the event loop with cooperative stop/deadline checks between bounded reads. Cancelling an await does not kill a worker thread: do not report settled cancellation or start replacement work while the underlying worker still runs. Hard interruption of arbitrary filesystem I/O is not promised.

Web-search policy:

- **Selected provider (2026-10-19): Tavily Search**, implemented with existing HTTPX behind a small injected interface. Basic search with snippets is the initial integration; provider result-page sizes are request parameters, not total search quotas. Use search only if the task needs it. Configure credentials locally; provider selection does not authorize this assistant to make paid verification calls.
- Do not assume Azure-hosted web search works because SDK types exist. If chosen instead, verify deployment support and preserve the same approval/audit controls.
- No provider credentials in Angular, logs, or tool results; no silent provider fallback. Missing configuration produces a capability-validation error before approval, not a pre-approval tool invocation.
- Search queries leave the BFF and are recorded, not individually approved. Do not automatically upload entire files or credentials as search input. Search does not silently fetch returned links: the coordinator invokes `web_fetch` explicitly when page content is needed.
- Distinguish search-provided snippets, successfully fetched page text, and files actually read. Only claim a page was fetched when its successful tool result is recorded; a search citation alone is not evidence that the page was read.

Web-fetch policy:

- Use the existing HTTPX stack for public HTTP(S) GET and static HTML/plain-text extraction. Do not add a browser runtime, execute page scripts, or use Tavily search as a substitute for the page body. Report blocked/unsupported/JavaScript-only pages honestly rather than fabricate extracted content. Request identity encoding and reject unsupported compressed responses before body processing; stream supported text into disk-spooled extraction and durable evidence rather than materialize the entire body. Explicit parser/protocol guards must not become total document-size or cumulative task quotas.
- Prevent SSRF: reject unsupported schemes, URL credentials, localhost, private/loopback/link-local/reserved destinations, and metadata services. Apply the policy to parsed URLs, DNS results, the actual connection target, and every redirect; protect against DNS rebinding rather than relying on a one-time hostname check. Keep TLS verification on and fail closed when the target cannot be verified.
- Do not forward browser cookies, provider bearer tokens, authorization headers, or ambient credentials. Fetched HTML is untrusted data and must not grant instructions or permissions. Validate/render extracted content safely.
- Record requested/final URL, redirect/provenance metadata, timestamp, status, and extracted result/error against the current plan/step before feeding it to the LLM. Complete subsequent LLM prompts/responses remain covered by the session audit requirement.
- Fetch is optional and needs no per-URL approval inside the approved plan. No cumulative fetch-call/run quota is introduced. Per-request timeouts, redirect-loop handling, and streamed/paged content are operational behavior. This network tool does not expand filesystem access beyond `/tmp/ls-assistants`.

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

Each event has `schema_version`, monotonic per-session `sequence`, server UTC `time`, `session_id`, `run_id` where applicable, `operation_id` where applicable, `request_id`, `type`, and typed data. Every LLM interaction must additionally identify its plan/revision, step or planning/synthesis stage, and unique interaction/attempt. Allocate candidate plan and stage identities **before** the first planning call; assign the validated plan digest after its result is validated. SSE event IDs use persisted sequence, not a second in-memory counter.

Each LLM interaction record must retain: `session_id`, `plan_id`, `plan_revision`, `step_id`, `phase`, `interaction_id`, attempt identity, start/completion UTC timestamps, requested model name, provider-reported model when available, actual `prompt`/API input, observable `response`/API output, and status/error. The prompt includes instructions, messages, and other model input, not merely a summary. Planning/revision/synthesis use explicit stage IDs when there is no execution step. Records and any referenced payload files live inside that session's folder.

Record all observable lifecycle and work:

- User requests, clarification/feedback, plans/revisions, approval/rejection and scope.
- Every LLM attempt: the full observable prompt/input and response/output, required identities/timestamps/model names, instructions/version, options, status, usage when available, retry/backoff, and errors. Include invalid/refused/partial/failed/cancelled attempts, not only valid successful outputs. Adjacent text deltas may be coalesced losslessly; unavailable responses must be marked unavailable rather than invented.
- Every tool request, validated arguments, access-policy decision, start, result/error or paged-result reference, and cancellation outcome.
- Step transitions/results, final answer, stop/disconnect, per-request timeout, crash-recovery findings, and archive actions.

Persist before publishing: append durable intent before external work, store result artifacts before their journal references, and commit outcomes before advancing. Serialize writes per session. Flush/fsync required files before acknowledging approval or publishing committed results; fsync parent-directory changes for new/atomically replaced files where supported. Snapshots are rebuildable and never the approval source of truth. Target abrupt process-restart recovery on a supported local filesystem; document storage/platform limits rather than promise immunity to every hardware/power failure.

For idempotent session creation, the first committed `session.created` record contains the creation key and canonical request digest. Rebuild the creation-key lookup from those records at startup and serialize creation under a process-wide lock. An index may cache this mapping but is not another authoritative store. Quarantine uncommitted orphan folders; never acknowledge their creation or execute from them. Process-local concurrency capacity resets on restart; committed command claims and retained disk usage do not. Recovery marks unfinished work interrupted before admitting new commands.

On journal/artifact failure, prohibit further dispatch, advancement, and publication of uncommitted output. Signal cancellation and retain operation ownership/capacity until already-started work settles; do not report settled cancellation prematurely. If the terminal outcome cannot be persisted, recovery treats the unmatched durable intent as interrupted with outcome unknown. A failed disk cannot guarantee a final log entry.

Do not record HTTP authorization headers, bearer tokens, credential objects, or settings dumps. Credential-field exclusion is not guaranteed secret-content removal from permitted user/source text. Retain actual observable LLM requests/results, user content, permitted tool excerpts, and query data; make that retention visible. Do not request or expose hidden chain-of-thought. Required encrypted continuation is opaque protocol data, not human-readable reasoning. UI previews may be shortened, but the retained prompt/response must not be silently truncated; stream large payloads into session-local artifacts instead of holding or repeating them all in memory.

On restart, rebuild projections from the journal, tolerate/quarantine an incomplete final JSONL record, and mark unfinished operations interrupted with any external outcome recorded as unknown. Do not infer success or automatically replay work. Existing plans remain reviewable; recovery alone does not require another approval of an unchanged plan. If a new executable plan is created, obtain its approval before execution. Automatic resume remains out of scope.

Do not impose artificial retained-session, journal-event, or interaction-payload quotas that prevent this complete recording. Use incremental journals, session-local payload files, and paged projections instead of embedding all prior history in every event. Physical disk/I/O failure stops further dispatch because unrecorded execution is unacceptable; it is not a configured product allowance. Do not silently delete history. Existing archive behavior preserves records without automatic expiry; pending archival returns `202` until local work settles. No new retention policy decision is required to implement the user's recording requirement.

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
| `GET /api/agent/sessions/{sid}/artifacts/{artifact_id}?offset=0&limit=12000` | Read a character page of committed, same-session extracted-text evidence without fetching again or executing tools. |
| `POST /api/agent/sessions/{sid}/archive` | Record archive intent, request targeted cancellation if needed, and archive after settlement; `202` while pending. |

Mutation requests use a client request ID/idempotency key bound to the request payload; reusing a key with different content is a conflict. Persist accepted commands and outcomes. Use a per-session operation lock and an explicit single-BFF-worker constraint initially; multiple workers require inter-process coordination, not just an asyncio lock.

Agent SSE events have their own strict discriminated schema: session/run/plan state, text deltas, step state/results, tool state/results, and terminal outcome. Every durable event carries correlation IDs, sequence, and server timestamp. Whitelist exposed fields; raw internal artifacts are not an unaudited static directory.

Planning and execution are finite, **request-owned** operations initially. Disconnect requests cancellation of its owning operation, eventually settling as interrupted; explicit Stop eventually settles the targeted operation as cancelled. The root BFF coordinator retains task ownership solely to settle already-started local work after transport teardown—not to keep executing the plan. A bounded transport-cleanup window is not a hard worker-termination guarantee. Stop for an already-settled operation returns its recorded outcome and cannot cancel a later revision/execution. Human waiting holds no upstream stream or background loop. Switching modes must not abort an operation—the root-scoped UI coordinator remains alive—but reload/network loss may interrupt it. Recovery fetches snapshot/events; it never re-POSTs execution automatically or takes ownership of existing work. A duplicate in-flight command returns its known operation/conflict, not another execution.

Apply timeouts to individual provider/transport operations and bounded shielded cleanup, not an overall Agent-run expiry. Agent history/replay requests have no execution side effects. Reserve background job infrastructure for a later requirement to continue after browser disconnection.

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
- Render Agent messages plus a structured plan card: objective/unverified assumptions, numbered steps, intended tools, filesystem scope, model/thinking settings, and **Approve and run / Request changes / Reject**. The displayed revision/digest must be the submitted approval identity. Do not ask for approval of individual queries/calls or display mandatory task budgets. Do not implement approval by parsing Markdown checkboxes or model text.
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
| 1 | Complete session-local recording, creation idempotency, paged history, and crash recovery. |
| 2 | Four task-dependent tools with enforced filesystem/network policy and paged I/O. |
| 3 | Tool-free planning, shared LLM audit/retries, clarification, and exact-version approval. |
| 4 | Sequential current-step execution with complete logical interaction records. |
| 5 | Idempotent Agent APIs, replay, and truthful cancellation/settlement status. |
| 6 | Isolated mode navigation, histories, drafts, and Settings behavior. |
| 7 | Reviewable scope, step progress, clarification/Stop, and archived history. |
| 8 | Mocked integration, optional-search scenarios, realistic-task evaluation, full regressions, and documentation. |

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

Done means: modes are mutually exclusive and histories independent; Agent hides session prompts while Settings still edits Chat prompts; each plan is approved once before execution and each new plan is approved again; execution is sequential without product-level run/call quotas; web search occurs only when needed; every LLM interaction has its plan/step/timestamp/model/prompt/response recorded in its session folder; clarification and Stop remain usable; reload never silently repeats work; all existing Chat tests and new Agent tests pass. Cancellation means no further dispatch and eventual local settlement, not a hard external rollback guarantee.

## 11. Confirmed decisions and remaining setup

1. **Web-search provider:** Tavily is the first provider. Set credentials locally in BFF configuration, never in chat or Angular. Real-provider verification by this assistant still requires authorization; normal Agent searches are covered by approval of a plan that needs them.
2. **Filesystem scope — confirmed 2026-10-19:** only `/tmp/ls-assistants` and permitted descendants for filesystem tools, with the existing mandatory exclusions. This decision is recorded; runtime configuration/enforcement and its tests remain implementation work. Durable BFF session recording stays under `bff/sessions` and is not exposed as an Agent-readable workspace.
3. **Approval and execution:** one approval per plan; a new/revised plan needs approval again. No per-step/tool/query approval, no product-level call/step/run quotas, and no mandatory web-search phase.
4. **Recording:** every LLM interaction, including its plan/step identity, timestamp, model name, actual prompt and observable response, is retained in its session folder under `bff/sessions`. Failed/partial attempts are included. No additional recording-policy question blocks implementation.

Implementation details remain literal grep, read-only tools, no pre-approval tool execution, a single BFF worker initially, request-owned execution with retained cancellation settlement, and archive-not-erase history. They must not reintroduce per-call approvals or cumulative run quotas. Prototype cap removal, complete interaction recording, and remaining real-provider/tool work are tracked in the checklist.

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
[oc-websearch-selection]: https://github.com/anomalyco/opencode/blob/4f31eb987dadde52ed986ab6434a00397d3b51a6/packages/core/src/tool/plugin/websearch.ts
[azure-responses]: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses?tabs=python
