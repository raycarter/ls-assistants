# Agent: approval-bound tool slice

This backend implements request → injected tool-free plan → exact-version approval
→ one to three approved `read_file`/`web_fetch` steps → persisted results →
reload/history. Tests supply a planner, temporary fixtures, and mocked network
connections. The public-web fetch implementation is real, but no live verification
has been performed. No default mock planner, production planner/search adapter,
shell, framework, or autonomous LLM tool loop is included. Chat's schemas, API and
credential selection are unchanged. The prototype caps below remain implementation
debt, not the target no-quota product policy.

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
descriptions. It receives no tool interface. The coordinator journals its bounded logical input/attempt before
calling it, validates its structured result, journals that result, and assigns
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
`/tmp/ls-assistants` is the approved filesystem scope, but its sole-root/platform-alias
wiring is still pending; default file roots remain empty. Do not infer that this
fetch increment enables filesystem access.

The coordinator binds the actual `SessionStore.root` into the supplied file
policy, including custom storage directory names. That exclusion is part of the
plan's effective-policy digest. A plan made with an older/different policy cannot
execute unchanged: obtain a newly reviewed revision/run and fresh approval rather
than silently retaining access. New plans also bind the fixed registry's enabled
tools and network-policy version. A legacy read-only approval can retain its
unchanged original digest only when its filesystem policy still matches; it cannot
authorize a new network tool. All four tool names are recognized; only `read_file`
and `web_fetch` are enabled. Search/grep are explicitly rejected, not stubbed successes.

## HTTP contract

All endpoints are under `/api/agent/sessions`. Mutation bodies require a nonempty
ASCII `request_id` (letters, digits, `_`, `-`, at most 128 characters). Unknown
fields/coercions are rejected. Model names must belong to `AVAILABLE_MODELS`;
effort is `low | medium | high | xhigh`.

| Method / suffix | Body / result |
| --- | --- |
| `GET /` | `{sessions: AgentSession[]}`; optional `include_archived=true` |
| `POST /` | `{request_id, model, reasoning_effort}` → session |
| `GET /{sid}` | Session snapshot, no inference |
| `PATCH /{sid}` | `{request_id, model?, reasoning_effort?, title?}` → session |
| `POST /{sid}/runs` | `{request_id, content}` → planning SSE |
| `POST /{sid}/runs/{rid}/execute` | `{request_id, plan_id, revision, digest}` → execution SSE |
| `POST /{sid}/runs/{rid}/revise` | Plan identity plus `{kind:"feedback", content}` → new planning operation/revision SSE |
| `POST /{sid}/runs/{rid}/reject` | Plan identity plus `{kind:"plan"}` → rejected session |
| `POST /{sid}/runs/{rid}/cancel` | `{request_id, operation_id}` → session; `202` while settling |
| `POST /{sid}/archive` | `{request_id}` → session; `202` with `archive_pending:true` until settled |
| `GET /{sid}/events?after=N` | `{events: AgentEvent[], has_more:boolean}`, at most 50 committed public events |
| `GET /{sid}/artifacts/{artifact_id}?offset=0&limit=12000` | Retained text page; no network/tool execution |

`AgentSession` has `id,title,model,reasoning_effort,created_at,updated_at,archived,
sequence,runs`, plus `archive_pending`. Runs have the shared
`id,request,created_at,updated_at,status,operation_id,plan,results,answer,error,
clarification` fields. `clarification` is always null in this slice.

Plans contain `id,revision,digest,objective,assumptions,model,reasoning_effort,
limits,steps` plus `access_policy` (workspace IDs and configuration/rules hashes,
not absolute server paths). Each step contains `id,title,description,tool,
arguments`. Supported tools have matching strict arguments:

- `tool:"read_file"`: `{workspace_id,path,offset,limit}`; one-based line offset.
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
Same-payload create/patch/reject/archive/Stop returns the current session; duplicate
planning/revision/execution commands return `409` with instructions to fetch it.

Preflight validation/conflicts return HTTP JSON before streaming. Agent errors
have `{error:{code,message}}` with curated messages. Schema errors use FastAPI's
normal `422` format. Raw provider exceptions are never used as messages.

### Explicitly deferred wire behavior

Clarification-shaped `/revise` and `/reject` requests are strictly parsed but
return `501 clarification_unavailable`; no clarification state is produced.
Search, grep, null-tool steps, real model continuation and autonomous execution
are unavailable. Session listing is bounded by the 100-session store cap rather
than paginated; event replay is paginated. Other shared required fields and URLs
are preserved.

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
- Default operational guards are a 30-second fetch-request timeout, 10 redirects,
  and redirect-loop rejection. These are not cumulative tool/run quotas. The
  separate legacy overall Agent deadlines below still need migration.
- Responses request `Accept-Encoding: identity`; nonidentity/compressed responses
  are rejected before body processing. Supported bodies are incrementally decoded
  and parsed in bounded workers into disk-spooled text, not buffered in full.
  Pathological markup tokens, nesting, and title metadata fail explicitly rather
  than grow parser memory without bound. Static extraction is heuristic; blocked,
  empty, unsupported, and JavaScript-only pages return safe errors.
- Full extracted text is copied incrementally to `artifacts/<uuid32>.txt` in its
  session, with an internal SHA-256/byte/character receipt. Evidence and directories
  are fsynced before the successful journal reference/public result. Only the
  selected page enters snapshots. No new full-document-size or cumulative fetch
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
{schema_version:1,sequence,time,session_id,request_id,run_id,operation_id,type,session}
```

`session.sequence` equals the envelope sequence. Public committed full snapshots
are sent, never internal audit records. Types include `run.planning`,
`planner.attempt`, `planner.result`, `run.awaiting_approval`, `run.approved`,
`run.executing`, `tool.intent`, `tool.result`, and terminal `run.completed`,
`run.rejected`, `run.failed`, `run.cancelled`, `run.interrupted`, plus
`run.cancelling` and session lifecycle events. Rejection is available through
snapshot/replay rather than its own SSE response. Streams end at their operation's
review/terminal event. Replay exposes the same safe projection.
An operation stream can include committed session lifecycle events caused by
another command, such as Stop/archive, with that command's different `request_id`.
Those events retain journal continuity. Sequence is the ordering authority; UTC
wall timestamps may move backward and need not be monotonic.

Stop targets an immutable operation ID. Stopping an old settled planning operation
cannot cancel a newer execution. Stop intent signals its worker without waiting
for the service/store locks; `cancelling` and a successful HTTP acknowledgement
still require a durable commit. Known conflicting request keys are rejected before
signaling. A signal can precede its command acknowledgement, including when a
concurrent command/key race or exhausted unreserved capacity prevents that
acknowledgement; this only revokes work, never authorizes new work. The operation
remains owned and capacity remains occupied until the planner coroutine or actual
read/parser/storage worker or async fetch settles. Disconnect interrupts **its** operation. An independent
deadline task signals Stop even if the worker or journal is blocked; timeout
settles as `failed/timeout` once the worker returns. Retained tasks only settle
work and never continue plan progression. Planning cancels and drains its injected
async call. File reads are offloaded with cooperative stop/deadline checks. Cancelling
an await is not presented as killing a thread or rolling back external work.
Fetch checks Stop and recording health inside its dispatch task immediately before
starting external work. Cancellation closes and drains the owned client and parser
workers; spooled evidence is closed after its artifact worker settles. A queued
Stop/recording failure cannot start a newly scheduled fetch.

The entire ASGI transport, including waiting for the next SSE frame, is bounded by
the operation's absolute deadline. Disconnect cleanup signals Stop immediately,
waits at most 100 ms per cleanup invocation for its durable control task, and
retains that task if storage/locks remain blocked. Journal-read I/O abandoned by a
closed transport is also tracked through actual worker completion. These tasks
are drained at shutdown before storage and shared-client closure. The existing
response cleanup window remains five seconds; Stop persistence cannot extend it
indefinitely. Worker/storage settlement and nonstreaming durable acknowledgements
may outlast that window; arbitrary filesystem I/O cannot be forcibly terminated.
EOF while a snapshot still has an active operation means interruption/unknown
outcome, never permission to
automatically re-POST. Fetch the session/events to reconcile. No automatic resume
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

## Storage and recovery limits

Single BFF writer on a local POSIX filesystem. A lifetime nonblocking `flock`
excludes additional store instances/workers. Coordinator and store locks serialize
accepted commands and writes. Session folders use exclusive mkdir with a server
UTC timestamp plus four cryptographically random lowercase/digit characters,
validated IDs, no-follow path components, `0700` directories and `0600` journals.
The suffix is not an authentication token.

`events.jsonl` is authoritative. New entries use private `storage_version: 2`:
event headers, fixed session/run/result changes, optional command claims, and
references to immutable JSON payloads. This is not a public protocol change:
HTTP/SSE still returns full `AgentEvent` snapshots with `schema_version: 1`.
Private JSON uses UTF-8 without ASCII escaping; canonical approval and command
digest encoding is unchanged.

Session-local `payloads/<server-id>.json` files hold request text, exact plan
revisions, new/replaced step results, answers, and existing logical audit records.
References contain ID, SHA-256, and byte length. Unchanged bodies reuse references
without serialization or hashing; control/title updates do not rewrite old bodies.
Successful fetched text remains separately stored in `artifacts/<uuid>.txt`.
These storage primitives do **not** complete provider input/output instrumentation.

The store keeps one private hydrated latest-session baseline, its reference
projection, and compact record/claim/attempt/operation/integrity indexes. It does
not retain every historical full snapshot or audit body. Historical records and
50-event replay pages are reconstructed on demand; paused iterators own read
descriptors, never the store lock. Early Stop's committed-claim lookup remains
lock-free and I/O-free. No on-disk `session.json` cache is introduced.

Payload files and directories are fsynced before their journal references; journal
and directory fsyncs precede verified in-memory publication/acknowledgement. A final
stable-metadata fingerprint check covers the complete journal prefix and new entry.
A short failure/publication barrier prevents publishing success after another
reader or worker has already latched recording failure; it never holds a lock
across filesystem I/O. Root/parent directory changes are fsynced too.
Creation keys/digests are rebuilt from the
first committed event, so a lost creation acknowledgement remains retryable.
Creation capacity is checked before mkdir, and no session/creation lookup entry
is published until the first event is fsynced. Capacity rejection therefore leaves
no empty visible session or growing retry cache. A possibly written creation
journal after an I/O failure is retained for replay/quarantine, never discarded.

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

This step trades repeated historical snapshot storage for reconstruction work:
historical reads replay their prefix, every append verifies the full journal
prefix, and cached operations check O(committed-reference-count) metadata. The
fast path relies on coherent local single-writer filesystem metadata, not protection
against a malicious operating system. Compaction, orphan cleanup, paged public
session projections, and complete LLM recording remain future work.

**Legacy prototype guards, pending coordinated migration:** 100 retained sessions,
500 events/session, 512 KB per new entry plus its new JSON payload bytes, 64 MB total
journal/JSON-payload bytes (including safe orphans), 8 runs/session, 8 planner
attempts/run, 4 active operations globally,
1 active operation/session, 3 steps/calls/plan, 30-second planning deadline, and
approved execution deadline at most 60 seconds. Request/feedback/objective/step
description strings are capped at 8,000 characters. Session/event/disk caps stop
new work without deleting history. Store limits, clock and session ID generator
are injectable. Archive retains history; no expiry/permanent deletion exists.
Fetched TXT evidence retains its separate accounting. The storage migration does
not remove quotas, reservations, schema limits, or overall operation deadlines.

Before accepting any planning/revision/execution operation, the store reserves
**12 event slots and `12 × record_bytes` bytes** for its bounded slice and
settlement/recovery. Existing snapshot size must also leave 5 KB of terminal
record margin; execution additionally requires 450 KB for up to three bounded
results. These deliberately conservative admission checks may return `507` well
before physical storage is full. Unrelated mutations, including title edits and
command claims, cannot spend reserved capacity. Each committed operation/control
event consumes one reserved slot; unused reservations are released only after
local settlement. Quota rejection of unrelated work does not poison storage or
stop admitted operations. A Stop/archive command claim can still return `507`
if unreserved space is exhausted; already-signaled Stop and its reserved terminal
settlement remain effective.

For an already-full legacy journal with unfinished work, startup may append one
**recovery-only** `run.interrupted` control record beyond the normal event cap.
That record is bounded by `record_bytes + 5,000` bytes. Total replay/recovery bytes
are bounded by `disk_bytes + sessions × (record_bytes + 5,000)`; normal writes and
admission still obey the original normal caps. This emergency record is explicitly
identified internally, replayable, and not repeated on every restart. Thus a full
journal can remain readable and durably interrupted instead of poisoning every
startup. A session/global disk budget already above its normal cap admits no new
normal work there until capacity is raised; history is not deleted. Arbitrarily
lowering configured limits below existing retained history, corrupt complete
records, or a filesystem that cannot write the recovery record can still fail
startup closed. This is bounded local recovery headroom, not disk preallocation
or protection against unrelated processes filling the physical filesystem.

Read bounds: regular UTF-8 text only, 256 KB input/file, 200 lines/page, 16 KB
rendered page, 1,000 characters/line, one-based offset at most 10,000. Results
report returned lines and truncation, not a separate total-line count. File bytes
are **not** frozen at review: approval authorizes the exact path/page, and the
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
not unconfigured application startup. These exclusions participate in policy v3's
digest. Symlink aliases remain denied by no-follow opens.
These filename rules are not complete
secret-content detection and cannot detect hardlink aliases of permitted files.

Retained content includes bounded user requests/feedback, logical planner inputs
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
continuation through the actual Angular decoder. Regeneration instructions are in
`tests/test_agent_contract.py`; format the generated fixture with the workspace
Prettier from `ui/` afterward. Semantic fixture comparison ignores JSON whitespace.
Storage tests additionally cover literal legacy/mixed journals, payload references,
exact historical replay, Unicode byte compatibility, failure/publication ordering,
root replacement, corruption, lock-free Stop, and paused-iterator shutdown.
This storage increment requires no public fixture regeneration or frontend changes.
Broad release checklist items for all four tools,
clarification, provider audit/continuation, synthesis and full UI integration
remain incomplete.
