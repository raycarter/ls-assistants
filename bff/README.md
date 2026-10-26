# Streaming Responses backend

FastAPI backend for the Azure OpenAI **v1 Responses API**. The caller owns the
conversation history and sends it explicitly on every turn. The backend keeps no
Chat conversation state and calls the provider with `store=False`. The separate
Agent API uses **production planning/revision and tool-free final synthesis over the recorded LLM boundary** with quota-free work and paginated
public v2, described in [AGENT_API.md](AGENT_API.md): journal-backed sessions,
provider-generated tool-free planning, exact-plan approval, streaming UTF-8 file pages,
public-page fetching, durable paged web evidence, final answers, progress, rejection,
Stop and restart recovery. Default run/revise commands and final synthesis call the
Responses provider through a shared controlled dedicated HTTPX client; no Agent SDK is used. An explicitly injected service with
no planner still returns `503 agent_not_configured`. Provider/deployment
compatibility has not been live-validated; this is not a full Agent release.

Agent HTTP acknowledgements return v2 session headers; list, run summaries, results
and events are paginated. Selected runs load one full exact plan and a recent result
window; v2 SSE sends headers/controls and changed bodies rather than session history.
There are no artificial work/history/payload allowances or overall Agent operation
or stream expiry. Per-call defaults remain: planner 120 seconds, file read 30 seconds,
ASGI send 30 seconds; WebFetcher owns a 30-second fetch timeout and 10-redirect guard.
Exact approval, one active run per session, preference freezes and Stop still apply.
New plans omit `limits`; known legacy limits remain exact digest material but are
nonoperative. These Agent changes do not alter Chat deadlines.

Agent storage uses private journal version 2 and immutable JSON payloads, preserving
legacy journal bytes and approvals. This is separate from public projection/schema
version 2; cached v1 clients must reload the updated UI. Normal public reads select
references directly, but one internal hydrated baseline/index per session, reference
metadata checks and append-prefix hashing still grow with retained history. There is
no compaction/GC. Typed LLM audit covers production planning/revision and final synthesis through the
recorded boundary; full audit-history UI is pending. Default `main.py` passes
`files=agent_workspace.build_file_policy()` for the sole approved workspace described
below. Bare `FilePolicy()` still has no roots, and custom `agent_service_factory`
injection is unchanged. See the Agent API document for exact DTOs, paging, recovery and policy rules.

### Agent filesystem workspace

The fixed ID `workspace` maps logical `/tmp/ls-assistants` to physical
`/private/tmp/ls-assistants` on Darwin, and to the logical path on other platforms.
The helper is pure mapping: no `resolve`, probing, mkdir, discovery, or configuration
override. There is no fallback or widening to repository, home, `/`, `/tmp`, or
`/private/tmp`. Strict descriptor-based no-follow checks still cover every physical
ancestor, the root and descendants.

**Create and populate `/tmp/ls-assistants` yourself with nonsecret UTF-8 files.**
Then request **“Read note.txt in workspace and summarize”**. Paths are relative to
the fixed workspace; requests do not select roots. A missing directory leaves startup
and plan preflight usable; an approved read fails with generic `file_unavailable`
and no synthesis.

Existing case-folded `.env`/secret/private/archive/dependency/session exclusions and
the actual storage directory's inode exclusion remain enforced. They are not
secret-content detection or hardlink isolation. Approval binds the path mapping and
policy, not the workspace root inode or file contents. Changed scope rejects old
approval and requires a fresh plan and approval without rewriting journals.

### Agent LLM boundary

`RecordedPlanner` composes a request builder, `SingleDispatchTransport.send_once`,
decoder, and coordinator-owned proposal validation. `CanonicalRequest` enforces
`store=False` and a reviewed application-JSON allowlist. Intent retains the exact JSON
body, and the same immutable serialized UTF-8 bytes enter the transport. The model
and explicit `reasoning.effort` must match the frozen run selection. Candidate plan
ID/revision and stage are allocated before the call; interaction/attempt IDs and retry lineage
correlate every typed record. The old injected `Planner.plan` fixture path remains
`logical_only`, not evidence of actual provider input/output or hidden retries.

Through its store callback, `AttemptRunner` fsyncs intent/authorization before
dispatch and retains every supplied observable event/response/explicit error body
with per-observation model/status/usage/
failure metadata, and commits outcome plus applicable proposal validation before
advancement. Provider completion and valid-plan validation are separate facts.
Independent stream chunks have immutable session-local audit payloads; full actual
content and opaque continuation are retained. Credential/client/settings/transport
fields are structurally excluded, without substring redaction or guaranteed content
scrubbing. Opaque continuation is protocol evidence, not hidden chain-of-thought.

Stop/revocation is normal cancellation; actual recording failure poisons/revokes
dispatch and advancement. Both retain owned recording/transport cleanup until it
settles. Unmatched intents recover as `interrupted/outcome_unknown` with committed
output counts and known metadata from exact output references, without replay.
There is no automatic retry policy: explicit attempts require durable failed/invalid
predecessors, while cancellation/timeout revoke the runner. One-send mocks do not
establish live compatibility; direct HTTPX retry/configuration evidence is recorded
only in the root checklist. No Agent SDK retry claim is involved.

Internal store readers `llm_attempts(sid, rid, limit=25, before=None)` and
`llm_records(sid, attempt_id, after=0, limit=50)` page committed audit records; they
are not HTTP endpoints. Raw canonical/opaque bodies stay private. Public v2 `llm.*`
events are neutral controls, never approval or step advancement; v1 compatibility
remains read-only. The runner retains one active call's observations, and JSON
serialization still materializes each complete record, in addition to the existing
baseline/index/linear-verification costs. No constant-memory guarantee is implied.

`agent_responses.py` supplies `agent-responses-planning-v2`: pure request construction
with `stream=False`, configured `max_output_tokens`, and a manually reviewed strict
closed schema for objective/assumptions and at least one `read_file`/`web_fetch` step.
The schema and instructions distinguish one-based file line offsets and 1–200-line
excerpts from zero-based web character offsets and 1–16000-character pages. File
paths must be workspace-relative. Invalid arguments are rejected, not repaired.
Revision input includes feedback but no old-plan history in the current interface.
The adapter sends exact canonical bytes via fresh HTTPX requests with `auth=None`,
no ambient cookies/default headers/client auth, `trust_env=False`, redirects disabled,
and lower `AsyncHTTPTransport(retries=0)`. Bearer acquisition shares Chat's credential
provider; the Chat SDK retains `max_retries=0`.

Requests accept identity encoding only; other encodings are rejected with raw bytes
retained. Owned HTTP streaming preserves failed partial reads as reversible raw
UTF-8/base64 evidence. Complete response observations precede proposal decoding;
unsafe/deep JSON falls back to raw evidence. Response close remains owned through
repeated cancellation. `main.py` retains one exit-stack cleanup task closing
Agent → Agent HTTP → Chat SDK → credential after settlement.

Execution runs the exact approved tool calls sequentially. After all results commit,
`AgentService` reloads the approved plan and all ordered completed results, then makes
one audited tool-free `agent-responses-synthesis-v1` request with the full original
request, approved-plan snapshot and recorded result bodies, including provenance and
truncation metadata. Result excerpts are not silently expanded into unseen artifact
pages. Synthesis uses the approved frozen model/effort, `store=False`, `stream=False`
and configured output tokens, with no extra tools or approval.

Its `phase:synthesis` identity binds the execution operation and approved plan/revision.
Full observable output, provider outcome and valid answer validation commit before
the atomic answer plus `run.completed` transition. Failures, invalid/empty/refused or
incomplete output, Stop/cancellation and recording/publication races fail closed;
committed results remain inspectable without a fabricated answer. Only explicitly
injected services with `synthesizer=None` keep the deterministic receipt for fixture
compatibility. Default `main.py` supplies both planner and synthesizer; no new status
or public API shape is introduced.

No-tool plans, clarification, autonomous model/tool execution, continuation replay,
model-driven step results, revision prior context, Tavily, grep
and full audit UI remain pending.
Implementation status, all test counts/evidence and remaining checks are
recorded only in [AGENT_MODE_CHECKLIST.md](../AGENT_MODE_CHECKLIST.md).

## Setup

Requires Python **3.14 or newer** and `uv`. Run all commands from `bff`:

```bash
uv sync
cp .env.example .env
# Set the resource endpoint, deployment names and credential configuration.
uv run uvicorn app.main:app --port 3100
```

`uv run python main.py` is an equivalent entry point. For development, add
`--reload` to the Uvicorn command. Dependencies and exact versions are recorded in
`pyproject.toml` and `uv.lock`; use `uv sync --locked` for reproducible installs.
OpenAI SDK upgrades are bounded to version 2.x, whose async bearer-provider and
Responses streaming interfaces are covered by mocked HTTP integration tests.

## Configuration

Pydantic settings load `bff/.env`, independently of the process working directory.
**Environment variables override `.env` values.** Invalid configuration fails
startup. `.env.example` contains placeholders only.

| Variable                   | Default               | Meaning                                                                                                                                                                    |
| -------------------------- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AZURE_OPENAI_ENDPOINT`    | Required              | HTTPS Azure resource URL, e.g. `https://your-resource.openai.azure.com`. A URL already ending in `/openai/v1/` is also accepted and normalized.                            |
| `AVAILABLE_MODELS`         | Required              | CSV of Azure **deployment names**, in display order; trimmed, deduplicated, empty entries removed. At least one nonempty entry is required.                                |
| `DEFAULT_MODEL`            | First available model | Optional; must belong to `AVAILABLE_MODELS`. Blank selects the first model.                                                                                                |
| `AZURE_TENANT_ID`          | Unset                 | Service-principal tenant ID.                                                                                                                                               |
| `AZURE_CLIENT_ID`          | Unset                 | Service-principal client ID.                                                                                                                                               |
| `AZURE_CLIENT_SECRET`      | Unset                 | Service-principal secret, represented internally as `SecretStr`.                                                                                                           |
| `REQUEST_TIMEOUT_SECONDS`  | `120`                 | One fixed deadline for title generation, token acquisition waits, opening attempts, retry delays, provider stream reads and ASGI response sends. Positive, at most `3600`. |
| `PROVIDER_TIMEOUT_SECONDS` | `60`                  | OpenAI HTTP timeout per transport operation, including stream-read inactivity. Positive, at most `3600`; the whole-operation deadline still applies.                       |
| `TITLE_TIMEOUT_SECONDS`    | `10`                  | Best-effort title call timeout, bounded by the remaining absolute request deadline. Positive, finite, at most `3600`. No title retries.                                    |
| `MAX_OUTPUT_TOKENS`        | `4096`                | Positive Responses output-token limit, including reasoning tokens where applicable.                                                                                        |
| `MAX_RETRIES`              | `2`                   | Additional stream-opening attempts (`0`–`5`). The default permits three attempts total.                                                                                    |
| `RETRY_DELAY_SECONDS`      | `0.5`                 | Initial retry delay (`0`–`10` seconds), doubled per retry and capped at five seconds.                                                                                      |

Credential selection is explicit:

- All three SPN values present: async `ClientSecretCredential`.
- All three absent or blank: async `AzureCliCredential`, using an existing CLI session.
- Any partial configuration: startup configuration error.

The selected credential never changes after failure. One shared Azure Identity
async bearer-token policy caches and refreshes tokens; a lock serializes concurrent
refreshes. The scope is **`https://ai.azure.com/.default`**, as specified by the
[Microsoft v1 endpoint documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/switching-endpoints).
`AsyncOpenAI` uses `https://<resource>/openai/v1/` and the deployment name as `model`.
No API-version query parameter or API key is required.

Credentials, the Chat OpenAI client and the dedicated Agent HTTP client are created
in FastAPI lifespan and closed on shutdown. Token acquisition and default Agent
storage opening are lazy; startup, health and model listing perform no provider/token
calls or session reads. Agent uses the configured provider transport timeout and
output-token setting; Chat retry settings do not add Agent retries.

The CLI credential is composed behind an async public-interface adapter. Its
public `get_token` call runs in a shielded, tracked task with an explicit SDK
`process_timeout=10` seconds. Request timeout or cancellation stops the caller's
wait immediately, while an already-started CLI acquisition finishes normally or
reaches the SDK timeout/kill path. Subsequent acquisitions wait for that attempt
to settle before starting another SDK call; the bearer policy still owns caching
and refresh serialization. Completed task exceptions are always retrieved, and
lifespan close drains the outstanding acquisition before closing the credential.

This cleanup can continue after the HTTP request has ended, for the remainder of
the SDK's ten-second process-communication timeout. The timeout begins after
subprocess creation, not before it. Azure Identity 1.26 kills on its own timeout
without explicitly awaiting process exit; asyncio's subprocess transport owns
exit notification and reaping. The public credential API exposes no process
handle, so the adapter drains the SDK coroutine, not a separate process `wait`.
Process creation and OS exit-notification latency are not covered by that SDK
timeout. No SDK internals are overridden.

## HTTP API

### `GET /health`

```json
{ "status": "ok" }
```

This is process health, not a provider-connectivity or inference probe.

### `GET /api/models`

```json
{ "models": ["deployment-a", "deployment-b"], "default_model": "deployment-a" }
```

Models come exclusively from configuration. No provider discovery call is made.

### `POST /api/chat`

Send `Content-Type: application/json`. All fields below except `generate_title`
are required, including `system_prompt`, which may be `null`. Unknown fields and
camelCase names are rejected. IDs and model must be nonempty strings; `messages`
must contain at least one message with a nonempty string `content`.

```json
{
  "session_id": "session-1",
  "request_id": "request-1",
  "model": "deployment-a",
  "reasoning_effort": "xhigh",
  "system_prompt": "Be concise.",
  "generate_title": false,
  "messages": [
    { "role": "user", "content": "Earlier question" },
    { "role": "assistant", "content": "Earlier answer" },
    { "role": "user", "content": "New question" }
  ]
}
```

- `model` must be exactly one configured deployment name.
- `reasoning_effort` is exactly `low`, `medium`, `high` or `xhigh`. All four are
  forwarded unchanged for every deployment; provider rejection becomes an SSE error.
- Message roles are exactly `user` or `assistant`; ordering and content are preserved.
- A nonnull `system_prompt` becomes `instructions`, including an empty string.
  A null prompt omits `instructions`.
- History becomes Responses `input`. The answer call uses `stream=True`, `store=False`,
  the configured `max_output_tokens`, and `reasoning={"effort": reasoning_effort}`.
  No provider conversation or previous-response ID is used.
- `generate_title` is an optional **strict boolean**, default `false` (`null`, numbers
  and strings are rejected). Set it to `true` only for the first new send, not
  followups or retries. Title generation additionally requires **exactly one
  message whose role is `user`**; otherwise the flag is ignored. The service is
  stateless and does not track whether a session has already been titled.

### First-turn titles

For a qualifying request, the backend sends `start` immediately, generates and
sends one `session_title`, **then opens the answer stream**. The title uses the
same `AsyncOpenAI` Responses client and selected deployment, with fixed title-only
instructions independent of `system_prompt`, only the first user message as
`input`, `stream=False`, and `store=False`. It uses `reasoning={"effort":"low"}`
and a fixed `max_output_tokens=1024`, including reasoning tokens, rather than the
user's answer effort or output limit. Low effort keeps this small task inexpensive;
deployments that reject it use the fallback without another title attempt.

Whitespace is collapsed, matching surrounding quotes are removed, and titles are
truncated to at most 60 characters with trailing whitespace removed. Any title
exception, refusal, incomplete/noncompleted response, unusable output or title
timeout produces a deterministic, identically normalized first-message excerpt
instead (`New chat` if the excerpt is empty). Title failures expose no provider
details and do not emit an `error` of their own. The answer proceeds normally with
the remaining request budget; if the absolute deadline has expired, its existing
timeout behavior applies. Cancellation stops the request without emitting a
fallback or starting an answer. Clients can keep a local excerpt as the immediate
fallback while waiting for the title event.

Schema or allowlist errors return a normal **HTTP 422 JSON** response before any
stream starts. A valid request returns **HTTP 200 `text/event-stream`**; subsequent
runtime failures are terminal SSE `error` events.

## SSE contract

Read the POST response incrementally using a streaming HTTP client. Each frame has
an `id`, an `event` name, and one JSON `data` payload, followed by a blank line:

```text
id: <message_id>:0
event: start
data: {"session_id":"session-1","request_id":"request-1","message_id":"<message_id>","time":"2026-10-13T12:00:00Z"}

```

The `data` fields are exactly:

| Event           | JSON payload                                                     |
| --------------- | ---------------------------------------------------------------- |
| `start`         | `{session_id, request_id, message_id, time}`                     |
| `session_title` | `{session_id, request_id, message_id, title}`                    |
| `delta`         | `{session_id, request_id, message_id, text}`                     |
| `done`          | `{session_id, request_id, message_id, time, status: "complete"}` |
| `error`         | `{session_id, request_id, message_id, code, message}`            |

`session_id` and `request_id` echo the request. `message_id` is a backend-generated
UUID, stable across all events for that reply. `time` is the reply-creation time in
UTC RFC 3339 format, identical on `start` and `done`. Event IDs are
`<message_id>:<sequence>`, starting at zero and increasing for **every** frame.
They identify frames; the stateless API does not implement replay/resume or
idempotent request deduplication. A fresh POST creates a fresh reply identity.

The stream starts with one `start`, followed by one `session_title` only for a
qualifying title request, then forwards each nonempty `response.output_text.delta`
immediately. The title participates in the same correlation identity and sequence
(normally `start :0`, `session_title :1`, first `delta :2`). Requests without the
flag retain `start` → `delta`* → `done`/`error`; title requests use
`start` → `session_title` → `delta`* → `done`/`error`.
Exactly one `done` or `error` terminates
a stream whose transport remains writable within its deadline. **`done` contains
no final content**: append only `delta.text` to avoid duplicate text. Final text
snapshots, reasoning text/summaries and tool
events are not forwarded. No complete answer response is buffered by the service.

Provider `response.completed` succeeds only after text has been emitted. Failed
or incomplete responses, refusal, completion without text, and EOF without a
terminal provider event become errors. Preserve any already received text when
an error arrives. Disconnect or cancellation stops provider work and closes the
upstream stream; no terminal delivery is attempted to a disconnected client.
Transport cleanup is shielded from cancellation and bounded to five seconds.
The response sets `Cache-Control: no-cache, no-transform` and
`X-Accel-Buffering: no`.

### Errors, retries and deadlines

Messages are curated from provider error categories, never raw provider exception
text, credentials or tracebacks. Error codes include:

- `unsupported_reasoning_effort`, `invalid_provider_request`, `context_length_exceeded`
- `model_unavailable`, `rate_limited`, `provider_authentication_error`, `provider_permission_denied`
- `refusal`, `content_filtered`, `output_limit`, `incomplete_response`, `no_output`
- `stream_interrupted`, `timeout`, `provider_timeout`, `provider_unavailable`
- `provider_error`, `internal_error`

Only connection failures, HTTP 429 and HTTP 5xx may retry **while opening** the
answer provider stream. Title calls never retry. OpenAI SDK retries are disabled;
the service owns the explicit
budget. Once a stream opens there are no retries, even before its first delta,
so an emitted answer is never duplicated. Provider authentication or validation
failures do not retry. The fixed request deadline is never reset by a token or
delta and also bounds token refresh waits and backoff.

The same absolute deadline applies to ASGI sends, including a send blocked while
the provider iterator is suspended after a yield. On send timeout, the blocked
send is cancelled and the iterator/upstream stream is unwound using the shielded,
five-second cleanup budget. No further error or end-frame send is attempted on
that stalled transport. The ASGI server ends the unfinished response; the caller
may observe EOF without a terminal event and should preserve partial text. An
immediately writable timeout error may still be delivered, but an expired send
deadline never gains a new delivery budget.

## Test/check commands

All verification results and evidence are maintained only in the
[root checklist](../AGENT_MODE_CHECKLIST.md).

Run from `bff`:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv lock --check
```

Tests supply settings with `.env` loading disabled, except for a committed,
secret-free configuration fixture. Credentials, provider streams and HTTP
transports are mocked; real credential acquisition, socket connections and DNS
lookups are blocked. Coverage includes config
precedence/validation, credential selection/failure, token caching/refresh,
request mapping, exact JSON/SSE contracts, immediate start/title/delta delivery,
first-turn title guards, title normalization/fallback/timeout/cancellation, terminal errors,
refusal, retry budgets, whole-operation deadlines, cleanup, cancellation and ASGI
disconnects. Mock HTTP tests exercise the installed OpenAI SDK's actual request
serialization and event parsing. No live Azure inference is needed for this suite.
CLI cancellation regressions also execute Azure Identity's public token callback
with a mocked subprocess, checking timeout kill, process-completion notification,
acquisition serialization and lifespan draining. No real subprocess is started.

## Module layout

- `app/main.py`: application factory and lifespan resource ownership; injectable
  credential/client factories for tests.
- `app/settings.py`: environment/`.env` settings and startup validation.
- `app/auth.py`: credential selection and shared refreshing async bearer provider.
- `app/schemas.py`: strict request schema, typed SSE payloads and frame encoding.
- `app/routes.py`: HTTP endpoints and disconnect-aware streaming response.
- `app/responses.py`: injectable `ChatService`, Responses mapping and stream lifecycle.
- `app/agent.py`, `app/agent_schemas.py`, `app/agent_routes.py`: isolated Agent
  coordinator, strict commands/durable models, planner injection and HTTP/SSE endpoints.
- `app/agent_views.py`: public v2 headers, run controls, selected bodies and paged events.
- `app/agent_llm.py`: canonical request, injected recorded planner, one-send transport
  contract, and attempt/recording/cleanup ownership.
- `app/agent_responses.py`: production direct HTTPX planning/revision adapter,
  versioned strict proposal contract, exact wire bytes and raw response ownership.
- `app/agent_synthesis.py`: tool-free final answer request/decoding and recorded
  validation over frozen approved-plan/result snapshots, sharing the controlled HTTPX client.
- `app/llm_schemas.py`, `app/llm_journal.py`: strict private audit lifecycle,
  committed-reference indexes/readers and unmatched-intent recovery.
- `app/sessions.py`: lazy, single-writer, fsynced Agent journal and recovery.
- `app/agent_files.py`: fail-closed v4 workspace policy and no-follow UTF-8 EOF scans
  with bounded page previews; narrow same-access v3 approval compatibility.
- `app/agent_workspace.py`: pure fixed logical/physical workspace mapping for default composition.
- `main.py`: thin compatibility entry point.
