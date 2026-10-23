# Assistant UI

Angular frontend for the customized personal assistants, using standalone components,
client-side routing, CSS, and Vitest unit tests.

All frontend source, configuration, dependencies, and tooling belong in this directory.
**Run every command below from `./ui`, not the repository root.**

## Setup and development

Use Node.js and npm. Activate your default Node.js environment with nvm before installing dependencies.

```bash
source "$HOME/.nvm/nvm.sh"
nvm use default
npm ci
npm start
```

Open `http://localhost:4200/`. The development server reloads when source files change.
Run the FastAPI BFF on port 3100 using its setup instructions in `../bff/README.md`.
`proxy.conf.json` forwards `/api/**` to `http://localhost:3100` during `npm start`.
After changing `angular.json` or `proxy.conf.json`, stop and restart `npm start` to reload the proxy;
refreshing the browser or source-code hot reload alone does not apply proxy changes.
Production hosting must route `/api` to the BFF under the same origin.

## Conversations and streaming

- Create, switch, and delete sessions in the sidebar; unfinished drafts stay with their session.
- The first message requests a BFF-generated session title. Its `session_title` event updates the
  sidebar before the answer starts streaming, without ending the pending turn. The first-message
  excerpt is a provisional title and fallback if generation fails. Follow-ups and retries keep the title.
- Select a system prompt before the first message. That message locks a snapshot of the prompt for
  the session, so later catalogue edits or deletions do not change started sessions.
- Open Settings to select, add, edit, or delete system prompts.
- Send messages with the Send button or Enter; Shift + Enter inserts a new line.
- Choose a model from the BFF's model list and a Thinking effort (`low`, `medium`, `high`, `xhigh`)
  in the main top bar, aligned right. The default effort is `medium`; all four choices remain
  available for every model.
  The BFF reports unsupported combinations in the reply error UI.
- The model dropdown grows with its option names from its existing 9 rem width (11 rem from the
  `sm` breakpoint), up to 18 rem (22 rem from `sm`). Header controls wrap on narrow screens, and the
  selected model's full name is also available in the dropdown's tooltip.
- Both user and assistant bubbles render sanitized Markdown, with syntax highlighting for code.
- Message headers show timestamps in the browser's local timezone as `yyyy-MM-dd HH:mm:ss`,
  after the assistant label and before `User`. Missing or invalid historical timestamps stay undated.
- Replies grow incrementally in one bubble. Partial rendering failures silently show escaped plain text.
- Each session permits one in-flight turn. You can edit its next draft or chat in another session
  while receiving a reply. Replies never activate another session. Deleting a session aborts its turn.
- “Receiving reply…” includes a decorative spinner while waiting, including during title generation.
  It stays still when the system's reduced-motion preference is enabled.
- Stop preserves partial text. Errors preserve partial text and offer Retry on the last failed reply.
  Retry replaces that reply, generates a fresh request ID, and sends the original user message once.
  The current model/effort can be changed before retrying; the session's prompt stays locked.
- Sessions, drafts, preferences, active selection, and prompts restore on reload. Pending state is
  transient; saved streaming replies become `interrupted` and can be retried without automatically
  restarting a request. If reload happens before the reply starts, Retry is available for the trailing
  unanswered user message and reuses it without appending another user message. Sidebar visibility
  and unsaved Settings edits are also transient.

### API contract

`ChatApi` uses native `fetch` and a readable response stream, with an injectable `FETCH` token.
`GET /api/models` returns `{ "models": ["deployment-name"], "default_model": "deployment-name" }`.
`POST /api/chat` captures a turn's inputs once:

```json
{
  "session_id": "session-id",
  "request_id": "fresh-request-id",
  "model": "deployment-name",
  "reasoning_effort": "medium",
  "system_prompt": null,
  "generate_title": true,
  "messages": [{ "role": "user", "content": "Hello" }]
}
```

History is ordered visible user/assistant text. No reasoning payloads are stored or sent.
`generate_title` is optional (default `false`); the UI sets it only on the first send in an empty session.
Failed, stopped, interrupted, and streaming assistant replies are excluded from subsequent request
history; their partial text remains visible until explicitly replaced by retry.
Zero-length messages are also excluded at the outbound history boundary, while remaining in the
visible conversation and storage.

SSE events are `start`, `session_title`, `delta`, `done`, and `error`. Each carries `session_id`, `request_id`,
and server-assigned `message_id`. `start` adds reply-creation `time`; `session_title` adds `title`
(at most 60 characters) and is nonterminal. On first sends, ordering is `start` → `session_title` →
`delta`* → `done`/`error`; the BFF sends the title before opening the answer stream.
`delta` adds `text` and has no
`time`; `done` adds the same creation `time` and `status: "complete"`, with no final content; `error`
adds `code` and `message` and has no `time`. The bubble preserves the start timestamp; a terminal
error received before start uses a local creation timestamp.
SSE IDs are `<message_id>:<sequence>`. Unknown event types are ignored; malformed known events,
changed identities, mismatched start/done times, invalid order, HTTP failures, and EOF without a terminal event become
visible request errors. Identical repeated SSE sequences do not duplicate text. HTTP 422 `detail`
messages are shown for validation failures. Stop and deletion use `AbortController`.

## Local storage

`ChatState` hydrates its Angular signals synchronously at startup. The UI reads these signals and
updates them through state methods; one Angular effect persists changes to the localStorage key
`ls-assistants.chat-state`.
`ChatTurns` coordinates transient requests separately from durable state. Text updates are batched
at 32 ms; streaming persistence batches encoding, JSON stringification, and writes at 200 ms.
Session titles persist immediately while awaiting the answer. Completion, errors, and Stop flush
accumulated text and storage immediately. Ordinary state changes
continue to persist on the existing Angular effect flush.

The saved record contains:

```json
{
  "version": 1,
  "sessions": [],
  "systemPrompts": [],
  "drafts": {}
}
```

Each entry in `sessions`:

- `id`, `title`, and `type` identify the session; `selected` restores the active session.
- `items` contain `id`, `time`, `role`, `msg`, `type: "Message"`, and optional `status`.
  Assistant messages use `role: "ai"` in storage and `assistant` in the UI. Raw Markdown is stored,
  not rendered HTML. Existing timestamps/statuses are preserved. New user messages get an ISO
  timestamp when sent; assistant replies retain their server-provided creation timestamp from `start`
  (or a local creation timestamp for a failure before `start`). Only the display is localized.
- New replies also store optional `requestId`, `replyTo` (the submitted user message ID), and
  `error: { code: string, message: string }` on the message. New assistant statuses are `streaming`,
  `complete`, `error`, `stopped`, and `interrupted`; historical arbitrary statuses remain accepted.
- Sessions optionally store `model: string` and
  `reasoningEffort: "low" | "medium" | "high" | "xhigh"`. Absent fields use API model/medium defaults;
  resolved defaults are saved when a turn starts. Unavailable model preferences and histories are retained;
  choose an available model to send or retry. Unusable preference fields are ignored during hydration.
- `selectedSystemPromptId`, `systemPromptSnapshot`, and `systemPromptLocked` preserve prompt selection.
  A locked snapshot remains unchanged even if its catalogue entry is edited or deleted.
- Unsent drafts are stored separately by session ID, so they do not change the example's session format.

The reader also accepts a single example-shaped session or an array of sessions under the same key.
Saved data is validated before hydration. Missing data creates one empty session;
restored histories are not reseeded. Invalid or unsupported records are left untouched instead of
being overwritten. Blocked storage or quota errors leave the UI usable in memory; a console warning
reports the problem, with no additional UI banners.

Storage is local to this browser profile and origin, unencrypted, and not synchronized across tabs
or devices. Avoid storing secrets; multiple open tabs can overwrite each other's changes. Removing
only `ls-assistants.chat-state` from browser storage resets this application's data on the next reload.

## Agent mode: paginated v2 and neutral LLM progress

The header has a native Chat/Agent radio group (arrow-key navigation follows the browser's native
radio behavior). Both coordinators are root-scoped: switching views preserves pending replies,
selected sessions and drafts. Agent history is fetched on first entry, not on initial Chat startup.
Settings still edits the shared **Chat** prompt catalogue; Agent has no active Chat prompt-selection
context and does not consume those instructions.

Agent sessions come only from the BFF. Create/select/archive actions target the active mode.
Agent preferences are saved by PATCH; the controls reconcile to the acknowledged server settings on
success or failure. They stay locked throughout planning, clarification/review, execution, cancellation
and pending archive. A rejected or settled run releases the session when no archive is pending. Legacy browser
`type: "Agent"` records remain in the existing Chat v1 envelope, with selection normalized to a Chat
fallback. They are displayed separately as read-only local records: no upload, streaming repair,
prompt propagation, send, retry or deletion through Chat routes.

### Implemented interaction

Request → tool-free plan → structured scope review → **Approve and run** → approved `read_file`/
`web_fetch` steps → tool-free final synthesis → paginated GET-based history after reload. The default
BFF calls the Responses provider for planning, plan-feedback revisions and final synthesis through
its recorded adapters sharing a controlled dedicated HTTPX client. Provider/deployment compatibility has not
been live-validated. Implementation status, test evidence and remaining checks are tracked only in the
[root checklist](../AGENT_MODE_CHECKLIST.md).

The plan displays every step and description, unverified assumptions, workspace/path or URL, exact
arguments, model/effort, revision/digest and the complete exact recorded JSON, including access policy.
After all exact approved tool calls commit, the BFF reloads the approved plan and all ordered
completed results for one tool-free synthesis using the frozen approved model/effort. Input includes
the full original request, approved-plan snapshot and recorded result bodies with provenance and
truncation metadata; it is independent of the UI's loaded result window. Excerpts remain excerpts,
without implicit loading of further retained artifact pages. No extra tools or approval are requested.
Synthesis stays within `executing`; the existing completion event supplies the validated final answer.
Full observable output, outcome and valid answer validation commit before atomic answer/completion.
Failures, Stop/cancellation and recording/publication races retain committed results without a
fabricated answer. No public status or API shape changes are needed.
Approval, rejection and plan feedback bind the current identity. A full plan is loaded for review;
this is not a plan-payload quota. New plans omit `limits`; known legacy
`{max_steps,max_tool_calls,timeout_seconds}` material remains readable with its exact approval digest
but is nonoperative. There is no mandatory budget display.
Double clicks are guarded while a command is pending. Results and answers use the existing
sanitized Markdown and local-time display pipes; original server timestamps are preserved. Agent
history does not auto-scroll, so updates do not pull a user away from review.
File results retain the BFF's path, offset, returned-line count and truncation flag. Live and reloaded
result cards show the recorded excerpt range/count and explicitly disclose omitted content or clipped
long lines when `truncated` is true; an excerpt is not presented as evidence of the complete file.
The BFF scans regular UTF-8 text through EOF in 8 KiB chunks, retaining a preview of at most 200
lines/16,000 UTF-8 bytes and 1,000 characters per displayed line. There is no 256 KB file allowance
or 10,000-line offset ceiling. Invalid text beyond the preview still fails the read, and each read
has its own timeout. File roots still default to none; `/tmp/ls-assistants` is the sole approved
scope, with its root/platform-alias wiring pending. The v4 file policy narrowly accepts known v3
approval material only at the same access scope.

Fetch review displays the exact public URL and zero-based character-page arguments. Successful fetch
cards distinguish actual retrieved text from file excerpts, showing title, requested/final URL,
retrieval time, content type/status, and page counts. Source text and titles are escaped literal text,
not executable HTML or interpreted Markdown. Links allow credential-free HTTP(S) only.
The full extracted text is retained by the BFF. Read retained pages with the result card's paging
controls; these issue only GET requests to the evidence endpoint, never fetch the URL again or approve
another execution. Loading/errors are result-local; session changes and component destruction abort
or suppress stale results. Reload restores the execution preview and access to retained pages, not
the prior inspector position.

### Paged history and loaded views

- Session listing requests 25 v2 headers at a time, including archived sessions, with `total` and an
  opaque keyset `next_cursor`. More sessions load only on request. A restored selected session outside
  page one is preserved and resolved by direct `GET /{sid}`, including archived history.
- Run history loads one page of 10 summaries, newest first by append ordinal. **Load older runs**
  replaces the summary window using `next_before`; it does not accumulate all run bodies. The
  current nonterminal run, or latest settled run, stays pinned independently of the summary page.
- One additional historical run can be opened at a time. Each loaded body contains its complete
  request and exact plan, answer/error, total `result_count` and `completed_steps`, and a window of
  at most 20 results. Recent results are the default; each window is in ascending step order.
  **Load older results** replaces that window, and **Show recent results** returns to the tail.
  Live changes update matching cells without pulling an older window back to recent results.
- Reads have page-local loading/errors, request ownership and causal sequence checks. Matching
  pending reads are shared. Closing historical detail cancels its detail/result reads and clears
  their loading/error state; late callbacks cannot reopen it or affect newer reconciliation.
- History is not automatically fetched in full. The stream decoder retains at most 64 duplicate
  signatures; the workspace keeps a 10-summary window and at most the pinned and opened bodies,
  each with a 20-result window. Complete plans intentionally remain available for exact review.

Backend list/run/result/event pages accept sizes up to 100, but the UI uses the fixed sizes above
(and 50 for explicit event reads). Page windows and duplicate-cache size are transport/rendering
controls, not retained-history or execution allowances. There are no artificial step, call, attempt,
revision, run, session, event, journal or payload allowances. There is no overall Agent planning,
execution or transport expiry. One active run per session, exact approval, model/effort freezes and Stop remain.
Per-call defaults are planner 120 seconds, file read 30 seconds and ASGI send 30 seconds; WebFetcher
owns a 30-second timeout across one fetch/redirect chain, 10 redirects and per-request parser guards.
There is no overall stream timer. Chat deadlines are unchanged.

Stop sends the captured operation ID. **Stopping…** is nonterminal and keeps preferences locked.
Archive retains the session in reviewable history; pending archive stays locked even after a run settles,
until the server reports archival or explicitly clears `archive_pending`. A lost/failed archive
acknowledgement remains uncertain until an authoritative reload. Reload history is a safe GET/list action,
including archived sessions. No execution or
planning command is automatically reposted after a transport error or reload.
An in-flight archive command has separate local ownership: a concurrently delivered, earlier
`archive_pending: false` projection cannot release its lock before acknowledgement or confirmed recovery.

### Agent API and recovery

All routes are under `/api/agent/sessions`: list (`include_archived=true&limit=25`), create,
header GET, preference PATCH, `POST /{sid}/runs`, `/{sid}/runs/{rid}/execute`, `/revise`, `/reject`,
`/cancel`, and `/{sid}/archive`. Mutation bodies contain a fresh `request_id`. Approval sends `plan_id`, `revision`
and `digest`; Stop sends `operation_id`. The transport understands question-identified clarification
rejection, but the current BFF returns HTTP 501 for the deferred clarification flow.
HTTP JSON failures are shown with status and API detail. `503 agent_not_configured` applies to an
explicitly injected BFF service with no planner, rather than the default application composition.
Nonstreaming HTTP mutation acknowledgements return a bounded v2 header, not run history:

```text
{projection_version:2,id,title,model,reasoning_effort,created_at,updated_at,archived,
 archive_pending,sequence,run_count,latest_run_id,current_run}
```

`current_run` is null or a `RunControl` with
`id,status,operation_id,created_at,updated_at,version_sequence,plan_identity,result_count,
completed_steps,error`. `plan_identity` is null or `{id,revision,digest,model,reasoning_effort}`.
Run summaries add `request_preview`. A selected run has
`projection_version,id,request,created_at,updated_at,status,operation_id,plan,results,answer,error,
clarification,version_sequence,result_count,completed_steps,results_before`; the current BFF always
returns null `clarification`.

| GET suffix                           | Response keys / continuation                                                                                                                          |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `?include_archived=true&limit=25`    | `projection_version,sessions,next_cursor,total`; pass `cursor` for the next keyset page                                                               |
| `/{sid}/runs?limit=10`               | `projection_version,session_id,at_sequence,total,runs,next_before`; exclusive older run ordinal via `before`                                          |
| `/{sid}/runs/{rid}?limit=20`         | Selected run body; `results_before` is the exclusive ordinal for older results                                                                        |
| `/{sid}/runs/{rid}/results?limit=20` | `projection_version,session_id,run_id,at_sequence,total,completed_steps,results,next_before`; older window via `before`                               |
| `/{sid}/events?after=N&limit=50`     | `projection_version,events,next_after,through_sequence,has_more`; carry `through=through_sequence` and `after=next_after` to finish a finite catch-up |

HTTP page projections use `projection_version:2`; counts describe complete matching history.
Event reads are available through `AgentApi` but the workspace does not automatically replay the
event history. Exact backend DTOs and cursor rules are documented in [AGENT_API.md](../bff/AGENT_API.md).

`GET /{sid}/artifacts/{artifact_id}?offset=0&limit=12000` reads committed same-session evidence and
returns `{artifact_id,content,offset,returned_chars,total_chars,next_offset}`. Offsets/counts use
Unicode characters, not UTF-16 code units. Page size is at most 16000; retained totals and continuation
must agree with the execution metadata. The decoder rejects unsafe provenance and malformed or
inconsistent pages without replacing the original preview.

Agent SSE uses event `agent` and decimal committed sequence IDs, with this v2 envelope:

```text
{schema_version:2,sequence,time,session_id,request_id,run_id,operation_id,type,
 session,run,request,plan,result,answer,error,invalidates_runs}
```

`session` is the header and `run` is a control or null. `invalidates_runs` is a **required boolean**,
false for ordinary single-run progress. Request text appears once per planning/revision admission
(`run.planning`), the complete plan only at review (`run.awaiting_approval`), the changed result on
tool progress or representable terminal failure, and the answer at completion. Other body fields
are null; earlier bodies and complete history are not retransmitted.

Step 3 adds `llm.intent`, `llm.dispatched`, `llm.output`, `llm.outcome`, and
`llm.validation` as **neutral controls**. They expose no canonical input, opaque continuation,
provider response/error body, usage, or private audit fields. They cannot create a missing loaded
body, offer approval, settle a run, or advance result counts/completed steps. Recording-only
sequence/timestamp changes leave the loaded plan and step frontier intact. Intent/dispatch are
accepted only for active planning/approved/executing controls; late output/outcome/validation may
also arrive while cancelling. Approval stays locked through LLM validation and becomes available
only when the separate `run.awaiting_approval` event supplies the exact reviewed plan.

The independent decoder validates timestamps, headers/controls, loaded bodies,
session correlation and frozen approval identity. Work records must match the owning request/run/operation;
known session lifecycle records can carry other request IDs and null run/operation IDs without taking
ownership of the stream. A validated first planning/approval admission establishes the server baseline
even if metadata commits advanced it beyond the cached sequence; subsequent records must be contiguous.
Committed sequence and per-run `version_sequence` are the causal order; a title/header change can
advance the session without changing the run version. Strict timestamp syntax is checked, but UTC
wall-clock readings may step backwards and are not used as a monotonic clock.
New-run acknowledgements must match the submitted request and use a new run ID. Execution requires a
reviewed plan; completion requires a validated completed step prefix, not every result body loaded
in the current window. Archived headers have no nonterminal current run.
Identical duplicates in the 64-signature cache are ignored; gaps, conflicting or older uncached
duplicates, invalid responses and EOF during active work fail safely and trigger GET recovery.
A failed recovery leaves the last committed view
visible with an error. Reading an active operation after reload never takes ownership or resumes it;
use Stop or reload history to inspect eventual settlement.

Startup recovery retains the actual `type:"run.interrupted",request_id:"recovery",run_id:null,
operation_id:null` envelope with `invalidates_runs:true`. It can project the primary affected run's
control and `outcome_unknown` error, plus a uniquely changed failed result. Multiple affected runs
or ambiguous result changes require refresh; archive-only recovery can have null `run`, `result`
and `error`. The UI does not invent command correlation for these null IDs. Any invalidation
refreshes the header, current summary page and pinned/opened bodies through bounded GETs, keeping
review/send locked until the required views reconcile at the authoritative causal frontier.
Closing an optional historical view during recovery reconciles the remaining views; an obsolete
callback or superseded refresh cannot clear a newer invalidation. A failed required read leaves
the lock and a page-local error available for recovery.

An unmatched LLM intent produces a neutral `llm.outcome` before that null-ID run invalidation.
It keeps the attempt's original run/operation correlation with `request_id:"recovery"` and
`invalidates_runs:false`; it does not settle the loaded run. The private recovery record retains
`interrupted/outcome_unknown`, output count and known metadata from committed output references,
without replaying a send or inventing output/validation. Only the subsequent `run.interrupted`
performs run-state invalidation and bounded GET recovery.

Public `projection_version:2`/`schema_version:2` is independent of private journal
`storage_version:2` and the legacy v1 journal-event model. Cached v1 clients must reload the updated
UI. The frozen v1 reader is read-only compatibility for complete old snapshots; normal backend
HTTP/SSE no longer serves the unbounded v1 contract.

Only navigation metadata and unsent Agent drafts are saved in version 1 of
`ls-assistants.agent-ui-state`; server plans/results are not browser-authoritative copies. Invalid or
unsupported saved metadata is left untouched, with a content-free console warning and memory-only
changes. The key shares Chat's injectable storage boundary for deterministic tests.

### Current limitations and retained content

- The default BFF production planner is wired in `main.py`; run/revise commands call the provider.
  Startup, health and model listing make no provider/token calls or session reads. An explicitly
  injected service with no planner retains `503 agent_not_configured`. Provider compatibility has
  not been live-validated.
- The BFF supports approved `read_file` and `web_fetch` steps through its production planner. Web search
  (including Tavily), folder grep, and no-tool steps remain unavailable. The full four-tool release,
  autonomous model/tool loop, model-driven step results, continuation replay and revision prior
  context remain deferred. Default final output is a recorded tool-free synthesis; only explicitly
  injected `AgentService` instances with `synthesizer=None` retain a deterministic receipt for fixtures.
- The recorded backend seam explicitly composes `RecordedPlanner`, `CanonicalRequest`,
  `SingleDispatchTransport` and `AttemptRunner`; it is not an opaque fixture `Planner.plan` call
  claimed as full provider coverage. Candidate plan/revision/stage identities precede the call;
  interaction/attempt/retry lineage and the actual request's frozen model/explicit reasoning effort
  are bound to it. The legacy planner fixture path stays `logical_only`. Final synthesis uses
  `RecordedSynthesizer` with `phase:synthesis`, bound to the execution operation and approved
  plan/revision, with its own stage/interaction/attempt identity. The adapters use no Agent SDK.
- The `agent-responses-planning-v1` request builder is pure and tool-free, uses frozen model/effort,
  `store=False`, `stream=False` and configured maximum output tokens, and supplies a manually reviewed
  strict closed schema for objective/assumptions and at least one `read_file`/`web_fetch` step.
  Revision supplies feedback without old-plan history in the current interface. File roots remain
  empty by default; no-tool plans and approved-root wiring remain open.
- Recorded intent/authorization precedes dispatch durably; outcomes and applicable proposal validation
  precede advancement. Provider outcome is separate from valid/invalid proposal validation. Stop/revocation
  is normal cancellation; true recording failure poisons/revokes work while retaining cleanup ownership.
  No automatic retry policy was added: explicit attempts need durable failed/invalid predecessors;
  cancellation/timeout revoke the runner. Dedicated direct HTTPX sends exact canonical bytes with
  lower `AsyncHTTPTransport(retries=0)`, `trust_env=False`, redirects disabled and fresh requests
  using `auth=None`, no ambient cookies/default headers/client auth. It shares Chat's bearer
  credential provider; Chat SDK `max_retries=0` is unchanged. Scoped mocked transport evidence is
  in the root checklist, not an Agent SDK retry claim.
- The provider adapter requests identity encoding only and rejects other encodings while preserving
  raw bytes. HTTP stream read ownership retains failed partial bodies reversibly as UTF-8/base64;
  complete response observations precede proposal decoding, with unsafe/deep JSON falling back to
  raw evidence. Response close remains owned through repeated cancellation. The retained lifespan
  exit-stack task closes Agent → Agent HTTP → Chat SDK → credential after settlement.
- Fetch handles public static HTML/plain text only, not authenticated/internal pages or browser/JS
  rendering. The BFF requests identity encoding and rejects compressed responses; static extraction
  can report unsupported/empty pages. These per-request protocol/parser guards do not allocate
  cumulative work or retained-history budgets.
- Plan feedback is supported. Clarification handling, including replies and rejection, is deferred in
  the current BFF, which produces no clarification states. Compatibility question records are rendered
  read-only without offering unsupported commands; use a new session or await server-side resolution.
- History renders paged server projections, including archived sessions. There is no full event/audit
  timeline or automatic background polling; reload history to refresh settlement after reconnection.
- Private store readers `llm_attempts(sid, rid, limit=25, before=None)` and
  `llm_records(sid, attempt_id, after=0, limit=50)` are internal, bounded by record count, and not
  HTTP-exposed. The UI has no raw canonical/opaque-body reader or complete per-interaction audit view.
- The UI discloses retention before use: user requests, reviewed plans, approved source excerpts,
  tool results and model input/output are retained by the BFF. The recorded backend boundary retains
  exact application JSON and every supplied observable event/response/explicit error body, including
  per-observation model/status/usage/failure metadata and full opaque continuation under `store=False`.
  Credential/client/settings fields are structurally excluded, without substring redaction of permitted
  source/user content or opaque bodies. This is neither guaranteed scrubbing nor hidden-reasoning capture.
  Archive preserves records; there is no automatic expiry. Permitted content may contain sensitive
  text; there is no guarantee of secret-content scrubbing. Approved fetch URLs leave the BFF;
  future search queries would too.
  Provider usage is retained privately when supplied; the UI does not display it or promise a currency ceiling.
- Mocked approval/Stop tests verify UI command ownership only. Durable approval, tool authorization,
  request-id idempotency and cancellation settlement remain BFF responsibilities.

Paged public/UI windows do not make persistent storage fully paged or guarantee bounded total
memory/disk or physically infinite history. The BFF retains one hydrated latest baseline and
reference/index state per session, checks O(all committed references) metadata, hashes the complete
journal prefix on append, and replays prefixes for historical diagnostics/recovery. Normal public
reads select body references without full-session hydration/replay; selecting a legacy body can
parse one old full record. Compaction, garbage collection and orphan cleanup are not implemented.
Independent stream chunks are immutable audit payloads, but the runner retains one active call's
observations and the codec serializes each complete JSON record in memory. Those costs remain in
addition to the baseline/index/linear-verification costs above. Production planning/revision
does not add approved-root wiring. Implementation status and verification evidence belong only
in [AGENT_MODE_CHECKLIST.md](../AGENT_MODE_CHECKLIST.md).

## Build, test, and scaffold

```bash
npm run build
npm test -- --watch=false
npx --no-install tsc -p tsconfig.spec.json --noEmit
npx --no-install prettier --check src README.md angular.json proxy.conf.json
npx --no-install ng generate component component-name
```

Production output is written to `dist/ui/`. `npm test` without additional flags runs tests in watch mode.
Tests inject fake fetch/API implementations; they never require a running BFF or provider credentials.
The test target enables per-file isolation so import-time mocks, including Markdown highlighter
failure doubles, cannot reuse another spec's initialized module state.

Agent verification uses API/state/component doubles and the backend-generated
`src/app/agent-contract.fixture.json` v2 contract. Its recovery case comes from actually reopening a
temporary backend store with unmatched tool intent, then passes through the production v2 decoder
and bounded GET recovery paths. `src/app/agent-contract-v1.fixture.json` is frozen for read-only v1
reader tests. Coverage includes quota-free plan review, 64-signature deduplication, summary/result
window replacement, restored selections outside page one, closed pending reads/loading cleanup,
causal invalidation recovery, preference rollback, archive ownership and session-scoped errors.
Step 3 adds `agent-llm.spec.ts` and recorded-planning/unmatched-LLM-intent recovery fixture cases
for private-body exclusion, neutral controls, locked approval and unchanged step frontiers. The
frozen v1 fixture remains unchanged read-only evidence. These paths use static/mocked inputs;
current verification results and remaining release work are recorded only in the root checklist.

### Historical verification (2026-10-16)

The original 2026-10-16 review-fix counts and build details are preserved in the
[checklist's dated verification record](../AGENT_MODE_CHECKLIST.md#verification-record--2026-10-16-first-mocked-slice).
That checklist is the sole progress/evidence ledger; historical results do not certify later increments.

## Installed libraries

- **Styling:** `tailwindcss`, `@tailwindcss/postcss`, `postcss`, `flowbite`, and `tailwind-scrollbar`.
- **Icons:** locally bundled Material Icons WOFF2 in `src/assets/fonts/`, with its license and source
  information. `src/styles.css` loads the font; no external font service or Angular Material is required.
- **Content rendering:** `marked`, `highlight.js`, and `dompurify`.

Tailwind's PostCSS integration is configured in `.postcssrc.json`. Global styles in
`src/styles.css` load the Flowbite theme and register the Flowbite and scrollbar plugins.

Initialize Flowbite's JavaScript after the affected Angular views render when adding interactive
components; its CSS plugin alone does not initialize dropdowns or modals.

Markdown rendering parses with `marked`, highlights code with `highlight.js`, then sanitizes the
final HTML with DOMPurify before Angular displays it. It does not bypass Angular's HTML sanitization.
