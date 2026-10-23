# Agent: quota-free execution and paginated public v2

This backend implements request → injected tool-free plan → exact-version approval
→ approved `read_file`/`web_fetch` steps → persisted results → paginated history.
Step 1 (`2fb37a5`) established the incremental private journal; this document covers
the current uncommitted Step 2 implementation, including quota removal and public
v2 projections. Tests supply a planner, temporary fixtures, and mocked network
connections. The public-web fetch implementation is real, but no live verification
has been performed. No default mock planner, production planner/search adapter,
shell, framework, or autonomous LLM tool loop is included. Chat's schemas, API and
credential selection are unchanged.

## Composition and defaults

`create_app(..., agent_service_factory=callable)` accepts a synchronous factory
receiving the existing `Settings`. It returns `AgentService`, for example:

```python
AgentService(
    SessionStore(explicit_storage_path),
    planner=my_async_planner,
    files=FilePolicy(),  # File access stays disabled until approved-root wiring lands.
    models=settings.available_models,
)
```

An injected planner implements `async plan(context: PlanningInput) -> Proposal`.
The immutable context contains request, frozen model/effort, workspace **IDs**,
optional feedback, versioned BFF instructions, attempt count, and available tool
descriptions. It receives no tool interface. The coordinator journals its logical
input/attempt before calling it, validates its structured result, journals that result, and assigns
plan/step identities and a canonical SHA-256 digest. Injected adapters must perform
one attempt per call, without hidden retries or tools. No SDK is called by this
slice. This is not yet the complete per-attempt provider audit required by the
design. Provider usage/cost is unknown, not a currency ceiling.

The default planner is absent and planning returns HTTP `503` JSON with
`agent_not_configured` before accepting a run. Allowed file roots default to none;
configuration is explicit Python composition, with no changes to identity settings
or `.env` loading. Default storage is `bff/sessions`, **lazily opened on the first
storage endpoint**, never read/created at unconfigured application startup. An
explicitly injected store is recovered during lifespan startup. Tests always inject
temporary paths. The service settles owned operations before the shared Chat
client and credential close.

`fetcher=WebFetcher(...)` is an optional injection seam; the default is the safe
public-network implementation. It opens connections only during approved execution.
`/tmp/ls-assistants` is the sole approved filesystem scope, but its root/platform-alias
wiring is still pending; default file roots remain empty.

The coordinator binds the actual `SessionStore.root` into the supplied file
policy, including custom storage directory names. That exclusion is part of the
plan's effective-policy digest. Current file policy is `read-text-v4`; its narrow
compatibility check also accepts the known `read-text-v3` rules digest at identical
workspace/root/storage scope. It does not accept arbitrary older rules or expand
access. Other policy changes require a newly reviewed plan and fresh approval.
New plans also bind the fixed registry's enabled tools and network-policy version.
A legacy file-only policy can retain its unchanged original digest for `read_file`
only; it cannot authorize a network tool. All four tool names are recognized;
only `read_file` and `web_fetch` are enabled. `web_search` and `grep_in_folder` are
explicitly rejected, not stubbed successes.

## Quota-free work and per-call guards

There are no artificial step, tool-call, planner-attempt, revision, run, session,
event, journal-byte or payload-byte allowances, and no overall Agent planning,
execution or transport expiry. `StoreLimits` and capacity reservations have been
removed. `SessionStore.used` is informational physical journal/JSON-payload
accounting, including safe orphans; fetched TXT evidence has separate diagnostic
accounting. Neither controls admission. Actual recording failures still stop work.

The one-active-run/operation-per-session guard, exact approval, frozen model/effort
and effective policy, single-consumption execution, and Stop remain operative.
Request IDs, strict schemas, path/URL policy, page sizes and parser guards remain
protocol and per-request controls. They do not allocate cumulative work budgets.

| Guard | Default | Scope |
| --- | --- | --- |
| `AgentService.planner_timeout_seconds` | 120 seconds | Each injected planner call |
| `AgentService.file_timeout_seconds` | 30 seconds | Each file read, with a fresh monotonic deadline |
| `AgentService.send_timeout_seconds` | 30 seconds | Each ASGI response send |
| `WebFetcher(timeout=..., max_redirects=...)` | 30 seconds, 10 redirects | One fetch and its redirect chain; loop/parser/protocol guards also apply |

The service timeout injections must be positive and finite. Waiting for the next
SSE frame has no overall stream timer. Timeout/Stop cleanup retains ownership until
the actual call or worker settles; an arbitrary blocked filesystem operation is
not forcibly terminated. Chat deadlines are unchanged.

## HTTP contract

All endpoints are under `/api/agent/sessions`. Mutation bodies require a nonempty
ASCII `request_id` (letters, digits, `_`, `-`, at most 128 characters). Unknown
fields/coercions are rejected. Model names must belong to `AVAILABLE_MODELS`;
effort is `low | medium | high | xhigh`.

| Method / suffix | Body / result |
| --- | --- |
| `GET /` | Session header page; `include_archived=false`, `limit=25`, optional `cursor` |
| `POST /` | `{request_id, model, reasoning_effort}` → `SessionHeader` |
| `GET /{sid}` | `SessionHeader` only, no embedded history or inference |
| `PATCH /{sid}` | `{request_id, model?, reasoning_effort?, title?}` → `SessionHeader` |
| `GET /{sid}/runs` | Run summary page; `limit=10`, optional `before` |
| `GET /{sid}/runs/{rid}` | Selected run body with full exact plan and result window; `limit=20`, optional `before` |
| `GET /{sid}/runs/{rid}/results` | Result page; `limit=20`, optional `before` |
| `POST /{sid}/runs` | `{request_id, content}` → planning SSE |
| `POST /{sid}/runs/{rid}/execute` | `{request_id, plan_id, revision, digest}` → execution SSE |
| `POST /{sid}/runs/{rid}/revise` | Plan identity plus `{kind:"feedback", content}` → new planning operation/revision SSE |
| `POST /{sid}/runs/{rid}/reject` | Plan identity plus `{kind:"plan"}` → `SessionHeader` |
| `POST /{sid}/runs/{rid}/cancel` | `{request_id, operation_id}` → `SessionHeader`; `202` while settling |
| `POST /{sid}/archive` | `{request_id}` → `SessionHeader`; `202` with `archive_pending:true` until settled |
| `GET /{sid}/events` | Finite v2 event page; `after=0`, `limit=50`, optional `through` |
| `GET /{sid}/artifacts/{artifact_id}?offset=0&limit=12000` | Retained text page; no network/tool execution |

### Headers, pages and selected bodies

Nonstreaming HTTP acknowledgements return a bounded `SessionHeader`, including
duplicate commands. Public projection DTOs use `projection_version:2`:

- `SessionHeader`: `projection_version,id,title,model,reasoning_effort,created_at,
  updated_at,archived,archive_pending,sequence,run_count,latest_run_id,current_run`.
  `current_run` is a `RunControl` for the nonterminal run, or null; `latest_run_id`
  can identify a settled run. There is no `runs` array in a header.
- `RunControl`: `id,status,operation_id,created_at,updated_at,version_sequence,
  plan_identity,result_count,completed_steps,error`. `plan_identity` is null or
  `{id,revision,digest,model,reasoning_effort}`. `completed_steps` counts the
  completed execution prefix; `result_count` includes a running/failed frontier.
- `RunSummary`: all `RunControl` fields plus `request_preview` (first 200 characters).
- Selected run: `projection_version,id,request,created_at,updated_at,status,
  operation_id,plan,results,answer,error,clarification,version_sequence,
  result_count,completed_steps,results_before`. `clarification` is always null in
  this implementation. `plan` is the full exact reviewed plan, not a summary;
  `results` is only the selected window.

Page envelopes have these exact keys:

| Page | Keys |
| --- | --- |
| Session list | `projection_version,sessions,next_cursor,total` |
| Run summaries | `projection_version,session_id,at_sequence,total,runs,next_before` |
| Results | `projection_version,session_id,run_id,at_sequence,total,completed_steps,results,next_before` |
| Events | `projection_version,events,next_after,through_sequence,has_more` |

List/run/result/event `limit` values are `1..100`; the defaults above are read
windows, not retention allowances. Session listing is newest-first by immutable
`(created_at,id)` with an opaque keyset `next_cursor` bound to the archive filter.
`total` counts all sessions matching that filter, not just the current page.
Run summaries are newest-first by stable append ordinal; pass `next_before` as
`before` for an exclusive older page. Ordering does not depend on mutable update
timestamps.

Run detail and result pages select the most recent result window by default,
returned in ascending step order. `results_before`/`next_before` is the exclusive
result ordinal for the next older window, or null. Older windows replace the
displayed window; clients need not fetch or accumulate all history. Counts describe
the complete retained run. `at_sequence` is the session frontier at the read;
`version_sequence` records the last change to that run, so unrelated metadata
events can advance `sequence` without changing it.

Event reads return sequences strictly after `after`. Omit `through` to capture
the current tail as `through_sequence`, then carry that value into subsequent
pages with `after=next_after` to finish a finite catch-up despite concurrent
appends. An explicit frontier must satisfy `after <= through <= current tail`.
These reads do not run planning/tools. The UI does not automatically drain event
or history pages.

### Plans, results and command identity

New plans contain `id,revision,digest,objective,assumptions,model,reasoning_effort,
steps,access_policy`; they omit `limits`, even if an injected proposal supplied it.
Legacy known `limits:{max_steps,max_tool_calls,timeout_seconds}` material still
parses and remains part of the exact persisted plan and canonical approval digest.
It is **nonoperative**: it imposes neither work allowances nor an overall timeout.
Removing quota enforcement does not rewrite an old approval identity.
`access_policy` contains workspace IDs and configuration/rules hashes, not absolute
server paths. Each step contains `id,title,description,tool,arguments`. Supported
tools have matching strict arguments:

- `tool:"read_file"`: `{workspace_id,path,offset,limit}`; one-based line offset,
  default `1`, and `1..200` lines per page, default `200`. There is no 10,000-line
  offset ceiling.
- `tool:"web_fetch"`: `{url,offset,limit}`; zero-based Unicode-character offset,
  default `0`, and page size `1..16000`, default `12000`.

No reasoning-only/null-tool steps are advertised yet. Planning validates URL syntax
without DNS, HTTP, or any tool invocation.

Results contain `step_id,status,content,time,error` plus
`path,offset,returned_lines,truncated,fetch`. File content is numbered UTF-8 text.
For a successful fetch, content is a page of extracted text and `fetch` contains
`artifact_id,requested_url,final_url,title,status_code,content_type,fetched_at,
redirects,total_chars,offset,returned_chars,next_offset`. `title` and `next_offset`
may be null. `fetched_at` is UTC; redirects list followed destinations, including
the final destination. File results have null/absent fetch metadata.
The final answer is a deterministic completion receipt pointing to recorded
evidence, not an LLM synthesis or a fabricated substantive answer.

Normal flow: `planning → awaiting_approval → approved → executing → completed`.
Feedback creates an immutable next revision; rejection releases the session.
Preferences are locked throughout all nonterminal run states. Approval binds the
session/run, exact revision/digest, model/effort and effective workspace policy.
Execution consumes a revision once, independently of request ID. Retrying any
consumed revision (including after restart/failure/Stop) conflicts; create a new
run and obtain fresh approval. Reused command keys with different bodies conflict.
Same-payload create/patch/reject/archive/Stop returns the current session header;
duplicate planning/revision/execution commands return `409` with instructions to fetch it.

Preflight validation/conflicts return HTTP JSON before streaming. Agent errors
have `{error:{code,message}}` with curated messages. Schema errors use FastAPI's
normal `422` format. Raw provider exceptions are never used as messages.

### Explicitly deferred wire behavior

Clarification-shaped `/revise` and `/reject` requests are strictly parsed but
return `501 clarification_unavailable`; no clarification state is produced.
Search/Tavily, grep, null-tool steps, function-loop/model continuation and LLM
synthesis are unavailable. The completion receipt is not a substantive model answer.

## Public web fetching and retained evidence

- HTTP(S) GET only, with static HTML/XHTML readable/main-text or plain-text
  extraction. No JavaScript/browser execution, authentication, attachments, or
  arbitrary binary downloads. Search is not required to fetch a supplied URL.
- URL validation rejects credentials, unsupported schemes, ambiguous hosts, and
  local/internal/metadata destinations. Every DNS answer must be public; the
  connection uses a pinned numeric address and verifies the actual peer. Host and
  verified TLS/SNI retain the original origin. Every redirect is revalidated.
- Each call owns a dedicated HTTPX/HTTPcore client with environment proxies and
  authentication disabled. Requests do not forward or accumulate cookies, bearer
  tokens, or preceding response headers. Current connection selection uses the
  first validated address, without address failover.
- Default operational guards are WebFetcher's own 30-second per-fetch timeout
  across its redirect chain, 10 redirects, and redirect-loop rejection. Parser and
  protocol guards apply per request; these are not cumulative tool/run quotas.
- Responses request `Accept-Encoding: identity`; nonidentity/compressed responses
  are rejected before body processing. Supported bodies are incrementally decoded
  and parsed in bounded workers into disk-spooled text, not buffered in full.
  Pathological markup tokens, nesting, and title metadata fail explicitly rather
  than grow parser memory without bound. Static extraction is heuristic; blocked,
  empty, unsupported, and JavaScript-only pages return safe errors.
- Full extracted text is copied incrementally to `artifacts/<uuid32>.txt` in its
  session, with an internal SHA-256/byte/character receipt. Evidence and directories
  are fsynced before the successful journal reference/public result. Only the
  selected page enters public results. No full-document-size or cumulative fetch
  allowance is imposed; physical I/O failure stops recording/dispatch.

The artifact GET returns `{artifact_id,content,offset,returned_chars,total_chars,
next_offset}`. Its page bounds match fetch arguments; an offset past the retained
text is `422`. IDs are server-owned 32-character lowercase hex. Only evidence
referenced by a committed successful result in that session is readable; unknown,
uncommitted, and cross-session IDs return `404`. Malformed IDs/pages return `422`.
Reading pages never fetches the URL again and requires no new execution approval.
Integrity is verified on reads/recovery; missing, replaced, or corrupt committed
evidence fails closed as storage unavailable.

Fetched content is untrusted source data, never instructions or approval. The UI
shows requested/final URL and retrieval time, renders fetched text literally, and
uses GET-only continuation with result-local errors/loading and stale-response
guards. A search snippet alone is never presented as a fetched page.

## SSE, cancellation and transport ownership

Every frame has `event: agent`, decimal persisted `id: <sequence>`, and JSON:

```text
{schema_version:2,sequence,time,session_id,request_id,run_id,operation_id,type,
 session,run,request,plan,result,answer,error,invalidates_runs}
```

`session` is a `SessionHeader`; `session.sequence` equals the envelope sequence.
`run` is a `RunControl` or null. `invalidates_runs` is a **required boolean** on
every envelope, including ordinary events where it is false. Only selected bodies
are sent, never the complete session history or internal audit records:

- `request` appears on `run.planning`, once per planning/revision admission.
- The full exact `plan` appears only on `run.awaiting_approval` for review.
- `result` carries the changed cell on `tool.intent`/`tool.result`, or a changed
  failed cell on terminal failure/cancellation/interruption when representable.
- `answer` appears only on `run.completed`; `error` projects the affected run's
  error. Unused body fields are null.

Small control events do not retransmit earlier requests, plans or results. One
complete plan is intentionally loaded for exact approval, with no plan-payload
quota. Types include `run.planning`,
`planner.attempt`, `planner.result`, `run.awaiting_approval`, `run.approved`,
`run.executing`, `tool.intent`, `tool.result`, and terminal `run.completed`,
`run.rejected`, `run.failed`, `run.cancelled`, `run.interrupted`, plus
`run.cancelling` and session lifecycle events. Rejection is available through
header/detail/event reads rather than its own SSE response. Streams end at their
operation's review/terminal event. Replay exposes the same safe projection.
An operation stream can include committed session lifecycle events caused by
another command, such as Stop/archive, with that command's different `request_id`.
Those events retain journal continuity. Persisted `sequence` and per-run
`version_sequence` are the causal ordering authority; UTC wall timestamps may
move backward and need not be monotonic.

Actual startup recovery preserves its original envelope:
`type:"run.interrupted",request_id:"recovery",run_id:null,operation_id:null`.
It sets `invalidates_runs:true`. The projection can include the primary affected
run's control and `outcome_unknown` error; a uniquely changed failed result in
that run is included. For multiple affected runs the primary is the newest changed
run, not necessarily `latest_run_id`; an ambiguous set of changed result cells has
no single `result`. Archive-only recovery may have null `run`, `result` and `error`
while the header confirms archival. Null command IDs are not fabricated into work
correlation. Clients must refresh bounded header/summary/loaded-run views when
`invalidates_runs` is true, including ambiguous or multi-run changes.

Public v2 is separate from private storage v2 and the legacy v1 journal event
model. Cached v1 clients must reload to the updated UI. The frozen v1 reader and
fixture provide read-only compatibility; the backend does not continue serving
unbounded v1 snapshots through its normal HTTP/SSE API.

Stop targets an immutable operation ID. Stopping an old settled planning operation
cannot cancel a newer execution. Stop intent signals its worker without waiting
for the service/store locks; `cancelling` and a successful HTTP acknowledgement
still require a durable commit. Known conflicting request keys are rejected before
signaling. A signal can precede its command acknowledgement, including when a
concurrent command/key race or recording failure prevents that acknowledgement;
this only revokes work, never authorizes new work. The operation remains owned
until the planner coroutine or actual read/parser/storage worker or async fetch
settles. Disconnect interrupts **its** operation. Retained tasks only settle work
and never continue plan progression. Planning cancels and drains its injected
async call on Stop or its per-call timeout (`planner_timeout`). File reads are
offloaded with cooperative Stop and per-read deadline checks (`file_timeout`).
Cancelling an await is not presented as killing a thread or rolling back external work.
Fetch checks Stop and recording health inside its dispatch task immediately before
starting external work. Cancellation closes and drains the owned client and parser
workers; spooled evidence is closed after its artifact worker settles. A queued
Stop/recording failure cannot start a newly scheduled fetch.

Each ASGI send has its own timeout; waiting for the next frame consumes no
transport lifetime. A stalled send ends the transport without another error/end
frame, and cleanup requests interruption. Disconnect cleanup signals Stop
immediately, waits at most 100 ms per cleanup invocation for its durable control task, and
retains that task if storage/locks remain blocked. Journal-read I/O abandoned by a
closed transport is also tracked through actual worker completion. These tasks
are drained at shutdown before storage and shared-client closure. The existing
response cleanup window remains five seconds; Stop persistence cannot extend it
indefinitely. Worker/storage settlement and nonstreaming durable acknowledgements
may outlast that window; arbitrary filesystem I/O cannot be forcibly terminated.
EOF while the last header still has an active operation means interruption/unknown
outcome, never permission to automatically re-POST. Fetch the session/events to
reconcile. No automatic resume
or tool replay is performed.

Admission (durable run/approval commit plus operation registration) runs in a
tracked native asyncio task shielded from its HTTP waiter's `Task.cancel()`.
If that waiter disappears during a blocked commit, the retained admission task
registers the committed operation and immediately requests interruption without
starting planning/tools. Shutdown also drains in-flight admissions. No accepted
run is left without a local owner merely because its admission waiter vanished.
Shutdown rejects new admission immediately and rechecks readiness after storage
startup. After operation/control settlement, it takes the service command lock as
a final barrier, waits for commands already past readiness, and drains detached
I/O workers under that barrier before closing the store. Thus an empty I/O-task
set between a readiness result and its command's subsequent write cannot allow
premature closure. Native-cancelled creation waiters still leave their actual
workers tracked until completion. Creation and every descriptor-relative storage
write/open require a live store descriptor under the store lock; an unopened or
closed store fails with `storage_unavailable`, never `dir_fd=None`/cwd fallback.

## Storage, recovery and retained costs

Single BFF writer on a local POSIX filesystem. A lifetime nonblocking `flock`
excludes additional store instances/workers. Coordinator and store locks serialize
accepted commands and writes. Session folders use exclusive mkdir with a server
UTC timestamp plus four cryptographically random lowercase/digit characters,
validated IDs, no-follow path components, `0700` directories and `0600` journals.
The suffix is not an authentication token.

`events.jsonl` is authoritative. New entries use private `storage_version: 2`:
event headers, fixed session/run/result changes, optional command claims, and
references to immutable JSON payloads. The private event/header and legacy
`JournalRecord.event` (`AgentEvent`) retain schema version 1; they are not public
v2 DTOs. Public HTTP projections use `projection_version:2`, and SSE/replay uses
`schema_version:2`. Private JSON uses UTF-8 without ASCII escaping; canonical
approval and command digest encoding is unchanged.

Session-local `payloads/<server-id>.json` files hold request text, exact plan
revisions, new/replaced step results, answers, and existing logical audit records.
References contain ID, SHA-256, and byte length. Unchanged bodies reuse references
without serialization or hashing; control/title updates do not rewrite old bodies.
Successful fetched text remains separately stored in `artifacts/<uuid>.txt`.
These storage primitives do **not** complete provider input/output instrumentation.

The store keeps one private hydrated latest-session baseline per session, its
reference projection, compact record/claim/attempt/operation/integrity indexes,
and a public header/control/body-reference index. It does not retain every
historical full snapshot or audit body. Normal public reads select the referenced
bodies directly without full-session hydration or historical prefix replay.
Legacy body selection can parse an old full record at its locator; the reader
retains at most one such decoded legacy record at a time. Historical diagnostic
iterators still reconstruct records on demand and own read descriptors, never a
store lock across yields. Early Stop's committed-claim lookup remains lock-free
and I/O-free. No on-disk `session.json` cache is introduced.

Payload files and directories are fsynced before their journal references; journal
and directory fsyncs precede verified in-memory publication/acknowledgement. A final
stable-metadata fingerprint check covers the complete journal prefix and new entry.
A short failure/publication barrier prevents publishing success after another
reader or worker has already latched recording failure; it never holds a lock
across filesystem I/O. Root/parent directory changes are fsynced too.
Creation keys/digests are rebuilt from the first committed event, so a lost
creation acknowledgement remains retryable.
No session/creation lookup entry is published until the first event is fsynced.
A possibly written creation journal after an I/O failure is retained for
replay/quarantine, never discarded.

Recovery reads legacy unversioned `{event,claim,audit}` records and new v2 entries
sequentially, including mixed journals. Legacy bytes, identities, digests, and
sequences are not rewritten, and no migration event or extra approval is added.
Replay discards and fsync-truncates only an unterminated final JSONL record.
Uncommitted empty/orphan folders are renamed `.orphan-<sid>`. Corrupt complete or
middle records fail startup closed; they are never skipped/trusted. Unfinished
operations are durably marked `interrupted/outcome_unknown`, with running results
marked failed, before commands are admitted. Waiting reviewed plans survive.
Nothing replays tools. An actual recording failure stops further dispatch and output
publication, signals cooperative stop, and retains ownership until settlement.
A failed disk cannot promise a final event; the last durable intent remains the
recovery authority. Local filesystem/fsync behavior is assumed, not universal
hardware/power-loss or network-filesystem durability.
Recovery accepts legacy journal-only sessions and expected no-follow, single-link
regular payload/artifact files. Safe uncommitted files may remain after interruption
but are not exposed or interpreted as completed work. All committed JSON references,
including superseded plans and audit-only payloads, are verified during recovery.
Live cached reads and commit boundaries check all committed payload metadata;
changed files receive SHA verification. Journal rereads are bound to committed
record fingerprints. Live reads use the pinned storage root; closed diagnostic
reads reject a replacement root. Missing/changed/unsafe evidence fails closed.

Known retained costs: one internal hydrated latest baseline plus reference/index
state per session; O(all committed references) metadata checks on cached reads
and commit boundaries; full-prefix journal hashing on append; and prefix replay
for historical diagnostics/recovery. Selected public bodies and UI windows do not
make persistent state fully paged or guarantee bounded total memory/disk or
physically infinite history. The fast path relies on coherent single-writer
filesystem metadata, not protection against a malicious operating system.
Compaction, garbage collection/orphan cleanup, and complete LLM auditing remain
future work. Archive retains history; no expiry/permanent deletion exists.

## File pages and access policy

Regular UTF-8 text is incrementally decoded through real EOF in 8 KiB chunks,
including text beyond the requested page. There is no 256 KB input-file allowance
or 10,000-line offset ceiling. Only the requested page and the current line's
display prefix are retained by the reader: at most 200 lines, 16,000 UTF-8 bytes
including line numbers/joining newlines, and 1,000 displayed characters per line.
The per-read timeout and cooperative Stop checks apply throughout the scan.
Invalid UTF-8/control data even after the displayed page is rejected; observed
size/mtime/ctime changes during the read fail closed rather than returning a mixed
page. Results report returned lines and truthful truncation for omitted trailing
lines/page overflow or clipped selected lines, not a separate total-line count.
An excerpt starting later in a file remains an excerpt even if `truncated:false`
means it reached EOF without clipping. Offsets past EOF return `422` (offset `1`
is valid for an empty file). File bytes are **not** frozen at review: approval
authorizes the exact path/page, and the
file can change before execution. Descriptor-relative no-follow opens reject
symlink traversal/replacement; special files and binary/control data are denied.
Shared policy denies `.env*`, credentials/secrets/private archives, `.git`, common
dependency/credential directories and session directories, including configured
roots and all their ancestors, using the same mandatory component rules as
relative paths (`private-archive`, `credentials-backup`, and
`dev-plans-archive-copy` are excluded in either position). On macOS only the exact
leading `/private` platform anchor is exempted; nested/private-like names are not.
The actual storage subtree is excluded regardless of its name, including workspace
roots equal to or inside it. Lexical containment is only a preflight shortcut.
The policy records the live store directory's `(st_dev, st_ino)` identity and
checks directory identities during no-follow traversal, including **every
ancestor of a configured workspace root**, the root itself, and each descendant
directory before further traversal/file open. This denies case aliases on
case-insensitive filesystems without universally case-folding filesystem paths.
Pinned store identities remain excluded if the directory is renamed; the current
directory at each configured storage spelling is also protected. Identity capture
uses the live store fd after opening or metadata-only checks in the read worker,
not unconfigured application startup. These exclusions participate in policy v4's
digest. Symlink aliases remain denied by no-follow opens.
These filename rules are not complete
secret-content detection and cannot detect hardlink aliases of permitted files.

Retained content includes user requests/feedback, logical planner inputs
and valid outputs, reviewed plans, approval/rejection, allowed file excerpts and
results/errors, complete supported fetched text, and fetch provenance. No headers,
bearer tokens, credential/settings dumps, raw provider
error bodies or hidden chain-of-thought are captured. Do not treat arbitrary
permitted user/file/web text as scrubbed of secrets. File/tool content is evidence,
never a source of execution permissions.

## Verification

From `bff`: `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .`, `uv lock --check`. Agent tests use temporary stores,
mock planners and file fixtures under the existing no-network/no-real-subprocess
fixtures. The module implements independently written application code, with no
OpenCode source/runtime reuse. Fetch tests exercise fake resolvers, connectors,
peers, TLS streams, hostile HTML, spooling, cancellation, artifact integrity, and
recovery. Backend-generated public fixtures also exercise fetched evidence and
continuation through the actual Angular decoder. The current
`ui/src/app/agent-contract.fixture.json` is v2; its startup-recovery case is generated
by actually reopening a temporary store with unmatched tool intent, preserving the
null run/operation envelope and invalidation flag. The frozen
`ui/src/app/agent-contract-v1.fixture.json` exercises read-only v1 compatibility,
not the normal backend API. Regeneration instructions for the current fixture are in
`tests/test_agent_contract.py`; format the generated fixture with the workspace
Prettier from `ui/` afterward. Semantic fixture comparison ignores JSON whitespace.
Storage tests additionally cover literal legacy/mixed journals, payload references,
exact historical replay, Unicode byte compatibility, failure/publication ordering,
root replacement, corruption, lock-free Stop, and paused-iterator shutdown. Step 2
coverage crosses former quota boundaries, selects public bodies without full
hydration/replay, exercises finite event frontiers and ordinal/keyset paging,
streams file validation through EOF, preserves exact legacy approvals, and tests
actual recovery for single/multiple affected runs and archive-only settlement.
UI tests cover the 64-signature duplicate cache, 10-summary/20-result windows,
pinned selections outside page one, closed pending reads, and causal recovery.
These are offline verification paths; final check results are recorded separately.
Broad release items for all four tools, clarification, complete provider audit,
production planning, function-loop continuation and synthesis remain incomplete.
