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

Agent sessions come only from the BFF. Create and select actions target the active mode.
Agent preferences are saved by PATCH; the controls reconcile to the acknowledged server settings on
success or failure. They stay locked throughout planning, clarification/review, execution, cancellation
and any legacy pending archive. A rejected or settled run releases the session when no archive is pending. Legacy browser
`type: "Agent"` records remain in the existing Chat v1 envelope, with selection normalized to a Chat
fallback. They are displayed separately as read-only local records: no upload, streaming repair,
prompt propagation, send, retry or deletion through Chat routes.

### Implemented interaction

Request → tool-free plan → structured scope review → **Approve and run** → approved `read_file`/
`web_fetch`/`web_search`/`grep_in_folder` steps → tool-free final synthesis → paginated GET-based history after reload. Search is available when the BFF is configured. The default
BFF calls the Responses provider for planning, plan-feedback revisions, each read assessment and final synthesis through
its recorded adapters sharing a controlled dedicated HTTPX client. Provider/deployment compatibility,
implementation status, test evidence and remaining checks are tracked only in the
[root checklist](../AGENT_MODE_CHECKLIST.md).

For each read step, approval displays only workspace/path file identity, with neutral tool/step
identity and normal model/effort and revision/digest controls. It never displays read offset/limit,
paging/chunk explanations, whole-file notes or generated read titles/descriptions. Any plan containing
a read also hides its objective, assumptions and full-plan JSON to prevent read narrative leaks.
The original user request remains shown; fetch details and fetch-only review remain unchanged.
This is a display projection: the actual full wire plan, policy and digest remain preserved and
approval-bound. New production plans bind `agent-tools-v4` / `read_semantics:file-scoped-v1`.
Historical exact-page plans cannot gain adaptive authority through old-policy compatibility;
obtain a fresh plan and approval. Generic injected BFF fixtures without an assessor retain exact reads.
After all approved steps commit, the BFF reloads the approved plan and all ordered
completed results for one tool-free synthesis using the frozen approved model/effort. Input includes
the full original request, approved-plan snapshot and recorded result bodies with provenance and
truncation/coverage metadata; it is independent of the UI's loaded result window. Adaptive read
results contain cumulative lossy model findings plus coverage, not all raw chunks. Raw chunks remain
durable private evidence but are not reloaded into final synthesis. The synthesis stage is distinct
from read assessments and uses `agent-responses-synthesis-v3`. The backend prepends an explicit
partial-coverage warning if any read stops before EOF. No factual-accuracy guarantee is implied.
Excerpts remain excerpts,
without implicit loading of further retained artifact pages. No extra tools or approval are requested.
Synthesis stays within `executing`; the existing completion event supplies the validated final answer.
Full observable output, outcome and valid answer validation commit before atomic answer/completion.
Failures, Stop/cancellation and recording/publication races retain committed results without a
fabricated answer. Existing statuses/endpoints remain, with optional read coverage in results.
Approval, rejection and plan feedback bind the current identity. A full plan is loaded for review;
this is not a plan-payload quota. New plans omit `limits`; known legacy
`{max_steps,max_tool_calls,timeout_seconds}` material remains readable with its exact approval digest
but is nonoperative. There is no mandatory budget display.
Double clicks are guarded while a command is pending. Results and answers use the existing
sanitized Markdown and local-time display pipes; original server timestamps are preserved. Agent
history does not auto-scroll, so updates do not pull a user away from review.
Adaptive file results show cumulative findings and explicit `read` coverage:
`chunks,bytes_read,total_bytes,eof,stop_reason,file_version`. Their offset/returned_lines are null,
and `truncated = !eof`; early enough is partial coverage, not proof of a complete read. Metadata is
optional for legacy compatibility. Old exact-read result cards retain recorded line-range/count and
truncation disclosure. Read offset/limit wire fields remain legacy/internal hints, never approval UI.

The production BFF pins a no-follow file descriptor and reads sequentially from byte zero in chunks
of at most 16,000 raw bytes, preserving UTF-8 across boundaries, CRLF and long lines without suffix
clipping. Only the backend controls the same-file cursor. There is no total quota or loop deadline;
each chunk has a fresh file timeout. Observed mutation checks after assessment and at close fail
closed but do not guarantee a snapshot. An unseen suffix is not validated after early stop.
`agent-read-assessment-v1` strictly returns continue/enough and updated cumulative findings; EOF
wins over continue with no extra I/O. A stable per-step execution stage has distinct chunk
interaction/attempt identities. Full raw chunks/ranges/version commit privately as `tool.chunk`
before assessment intent/send, and every actual assessment input/output is recorded. Stop,
cancellation and recording failure retain cleanup ownership and prohibit advancement. This adds
no general autonomous tool loop, scope expansion or extra approval, and no constant-memory promise.
Default BFF composition uses `agent_workspace.build_file_policy()`:
fixed ID `workspace`, logical root `/tmp/ls-assistants`, physical root
`/private/tmp/ls-assistants` on Darwin and the logical path elsewhere. This pure mapping performs
no `resolve`, probing, mkdir, discovery or configuration override. Bare `FilePolicy()` has no roots;
custom `agent_service_factory` injection remains unchanged. There is no fallback or widening to
repository, home, `/`, `/tmp` or `/private/tmp` access.

**Create and populate `/tmp/ls-assistants` yourself with nonsecret UTF-8 files.** Then request
**“Read note.txt in workspace and summarize”**. `note.txt` is relative to the fixed workspace;
the request does not choose a root. A missing directory leaves startup and plan preflight usable;
the approved read returns generic `file_unavailable` and no synthesis. Every physical ancestor,
the root and descendants retain strict descriptor-based no-follow checks. Existing case-folded
`.env`/secret/private/archive/dependency/session exclusions and actual-storage inode exclusion
remain enforced; these are not secret-content detection. Grep additionally rejects multiply-linked
regular files, without claiming hardlink isolation for the other read tool.
Approval binds the path mapping and policy, not the workspace root inode or file contents.
Changed scope rejects old approval and needs a fresh plan and approval without rewriting journals.
The v4 filesystem compatibility rule alone cannot authorize adaptive execution of an old exact-page plan.

Fetch review displays the exact public URL and zero-based character-page arguments. Successful fetch
cards distinguish actual retrieved text from file excerpts, showing title, requested/final URL,
retrieval time, content type/status, and page counts. Source text and titles are escaped literal text,
not executable HTML or interpreted Markdown. Links allow credential-free HTTP(S) only.
The full extracted text is retained by the BFF. Read retained pages with the result card's paging
controls; these issue only GET requests to the evidence endpoint, never fetch the URL again or approve
another execution. Loading/errors are result-local; session changes and component destruction abort
or suppress stale results. Reload restores the execution preview and access to retained pages, not
the prior inspector position.

### Web search

The BFF enables Tavily Basic search when the optional `TAVILY_API_KEY` is configured locally;
Basic requests consume provider credits under Tavily's pricing. Review shows the exact
query and `max_results` (`1..20` per request), covered by the single plan approval.
There is no cumulative search quota. After approval, only that query is sent as search
content, with request parameters; files are not implicitly uploaded. Result cards show
provider-returned titles, URLs, snippets and optional dates as untrusted search results,
not fetched pages. Links are not automatically fetched; actual page text requires a
separate approved `web_fetch` step. Each search returns one bounded result set; arbitrary
result pagination is not implemented. Scoped mocked verification and reviews passed;
live provider compatibility remains unverified. See the [checklist](../AGENT_MODE_CHECKLIST.md).

The BFF requests identity encoding and rejects other encodings; a response over 1 MiB
fails rather than being truncated. This is a per-response guard, not a cumulative quota.
When a completed run has search hits, the backend deterministically prepends an answer
note explaining that search-service snippets do not establish that source pages were
fetched. Old versioned `agent-tools-v3` and `agent-tools-v3-exact` plans need a fresh
plan and approval under the current registry; historical plan bytes and digests remain intact.

### Literal folder grep

Grep review displays its exact `{workspace_id,path,pattern,file_glob,offset,limit}`
scope and arguments under the single plan approval. The folder defaults to `.`, pattern
is a nonempty case-sensitive single-line literal, optional glob defaults to null, and
offset/limit select the initial Unicode-character evidence page (`0`, `12000` normally;
limit `1..16000`). A slash-free glob selects basenames at any depth; otherwise
workspace-relative `fnmatchcase` applies, with `*` crossing `/`. Globs do not grant access.
Read steps retain their file-identity-only approval projection, including in mixed plans.

The BFF fully scans selected permitted UTF-8 files and retains one JSONL record per
matching line, `{path,line,text}`. Bounded spools/8 KiB blocks preserve cross-block UTF-8,
CRLF and full matching lines without clipping. No overall file/match/byte quota or scan
deadline applies; incremental operations have fresh timeouts and cancellation ownership.
Binary/unreadable/hardlinked/observed-mutating files discard staged matches and report
skips/`complete:false`; root/traversal/directory changes fail the operation. This is not
an atomic folder snapshot. Matched paths do not automatically trigger follow-up reads.

Grep cards show scope, total matching lines, files searched, skip counts and scan coverage
separately from the evidence excerpt. Source JSONL renders literally in `pre`, never as
Markdown. Optional `result.grep` is mutually exclusive with read/fetch metadata; exact
fields are in [AGENT_API.md](../bff/AGENT_API.md). Production tool types/validators live in
`agent-tool.models.ts`, separately from the unchanged frozen-v1 reader.

Existing artifact GET paging is coalesced into a single evidence window with local
loading/errors and stale-response guards; it adds no tool calls or page approvals.
Pages count Unicode characters and can split JSONL records, not matching-line counts.
`truncated = (next_offset != null || !complete)`; nonzero offset can omit a prefix even
when this flag is false. Synthesis v3 distinguishes scan coverage/total matches from
the model's received excerpt and deterministically warns if `!complete`, `offset > 0`
or `next_offset != null`. It does not automatically synthesize all retained pages.

The changed enabled tools/registry version require fresh review of old versioned plans;
legacy read-only fallback cannot authorize grep. Generic no-assessor services bind
`agent-tools-v4-exact`. This tool does not add a general tool loop or dependent read flow.

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
Legacy pending-archive records remain locked until the server reports archival or clears
`archive_pending`; an uncertain acknowledgement requires an authoritative reload. Reload history
is a safe GET/list action, including archived sessions. No execution or planning command is
automatically reposted after a transport error or reload.

**Permanent deletion:** The Agent delete action requires user confirmation.
`DELETE /api/agent/sessions/{sid}` returns an empty `204` after successful cleanup. For
`409 operation_active`, Stop the owning operation, wait for settlement, and explicitly retry.
For `409 session_in_use` caused by active history readers or SSE streams, wait for them to close
and retry. The UI distinguishes these conflicts and presents the corresponding guidance.
Archived legacy BFF sessions remain readable
and deletable, with no unarchive action. Cleanup fails closed on unsafe paths or errors; deletion
does not guarantee secure erase of backups, filesystem snapshots or provider copies. Legacy browser
Agent records described above are separate from BFF sessions.

### Agent API and recovery

All routes are under `/api/agent/sessions`: list (`include_archived=true&limit=25`), create,
header GET, preference PATCH, `POST /{sid}/runs`, `/{sid}/runs/{rid}/execute`, `/revise`, `/reject`,
and `/cancel`, plus `DELETE /{sid}`. The existing `/{sid}/archive` endpoint is legacy compatibility.
Existing body-bearing mutation commands contain a fresh `request_id`; DELETE has no body. Approval sends `plan_id`, `revision`
and `digest`; Stop sends `operation_id`. The transport understands question-identified clarification
rejection, but the current BFF returns HTTP 501 for the deferred clarification flow.
HTTP JSON failures are shown with status and API detail. `503 agent_not_configured` applies to an
explicitly injected BFF service with no planner, rather than the default application composition.
Non-delete nonstreaming HTTP mutation acknowledgements return a bounded v2 header, not run history:

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

The events `llm.intent`, `llm.dispatched`, `llm.output`, `llm.outcome`, and
`llm.validation` as **neutral controls**. They expose no canonical input, opaque continuation,
provider response/error body, usage, or private audit fields. They cannot create a missing loaded
body, offer approval, settle a run, or advance result counts/completed steps. Recording-only
sequence/timestamp changes leave the loaded plan and step frontier intact. Intent/dispatch are
accepted only for active planning/approved/executing controls; late output/outcome/validation may
also arrive while cancelling. Approval stays locked through LLM validation and becomes available
only when the separate `run.awaiting_approval` event supplies the exact reviewed plan.
Public v2 `tool.chunk` is likewise neutral: it exposes no private raw chunk body or ranges/version
and cannot advance results, completed steps or approval. The decoder/reducer keeps its step frontier intact.

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
  injected service with no planner retains `503 agent_not_configured`. Provider compatibility
  evidence is maintained in the root checklist.
- The BFF supports approved `read_file`, `web_fetch` and `grep_in_folder` steps, and
  `web_search` when Tavily is configured, through its production planner. No-tool steps remain unavailable. The full four-tool release,
  autonomous model/tool loop, general model-driven step results, continuation replay and revision prior
  context remain deferred. Default final output is a recorded tool-free synthesis; only explicitly
  injected `AgentService` instances with `synthesizer=None` retain a deterministic receipt for fixtures.
- The recorded backend seam explicitly composes `RecordedPlanner`, `CanonicalRequest`,
  `SingleDispatchTransport` and `AttemptRunner`; it is not an opaque fixture `Planner.plan` call
  claimed as full provider coverage. Candidate plan/revision/stage identities precede the call;
  interaction/attempt/retry lineage and the actual request's frozen model/explicit reasoning effort
  are bound to it. The legacy planner fixture path stays `logical_only`. Final synthesis uses
  `RecordedSynthesizer` with `phase:synthesis`, bound to the execution operation and approved
  plan/revision, with its own stage/interaction/attempt identity. The adapters use no Agent SDK.
- The `agent-responses-planning-v5` request builder is pure and tool-free, uses frozen model/effort,
  `store=False`, `stream=False` and configured maximum output tokens, and supplies a manually reviewed
  strict closed schema for objective/assumptions and at least one available `read_file`/`web_fetch`/`web_search`/`grep_in_folder` step.
  Revision supplies feedback without old-plan history in the current interface. Default file scope
  is the fixed `workspace` mapping above; no-tool plans remain open. Read instructions describe purpose
  without paging/chunk/whole-file narratives; legacy offset/limit fields remain validated internal hints.
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
  exit-stack task closes Agent → optional search HTTP → Agent HTTP → Chat SDK → credential after settlement.
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
  Legacy archives preserve records; there is no automatic expiry. Permitted content may contain sensitive
  text; there is no guarantee of secret-content scrubbing. Approved fetch URLs and search queries leave the BFF.
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
addition to the baseline/index/linear-verification costs above. Implementation status and verification evidence belong only
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
