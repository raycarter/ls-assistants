# Streaming Responses backend

FastAPI backend for the Azure OpenAI **v1 Responses API**. The caller owns the
conversation history and sends it explicitly on every turn. The backend keeps no
Chat conversation state and calls the provider with `store=False`. The separate
Agent API has a **first mocked vertical slice**, described in [AGENT_API.md](AGENT_API.md):
journal-backed sessions, injected tool-free planning, exact-plan approval, bounded
fixture-tested file reads, progress, rejection, cancellation and restart recovery.
Its production planner is unavailable by default; this is not a full Agent release.

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

| Variable | Default | Meaning |
| --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | Required | HTTPS Azure resource URL, e.g. `https://your-resource.openai.azure.com`. A URL already ending in `/openai/v1/` is also accepted and normalized. |
| `AVAILABLE_MODELS` | Required | CSV of Azure **deployment names**, in display order; trimmed, deduplicated, empty entries removed. At least one nonempty entry is required. |
| `DEFAULT_MODEL` | First available model | Optional; must belong to `AVAILABLE_MODELS`. Blank selects the first model. |
| `AZURE_TENANT_ID` | Unset | Service-principal tenant ID. |
| `AZURE_CLIENT_ID` | Unset | Service-principal client ID. |
| `AZURE_CLIENT_SECRET` | Unset | Service-principal secret, represented internally as `SecretStr`. |
| `REQUEST_TIMEOUT_SECONDS` | `120` | One fixed deadline for title generation, token acquisition waits, opening attempts, retry delays, provider stream reads and ASGI response sends. Positive, at most `3600`. |
| `PROVIDER_TIMEOUT_SECONDS` | `60` | OpenAI HTTP timeout per transport operation, including stream-read inactivity. Positive, at most `3600`; the whole-operation deadline still applies. |
| `TITLE_TIMEOUT_SECONDS` | `10` | Best-effort title call timeout, bounded by the remaining absolute request deadline. Positive, finite, at most `3600`. No title retries. |
| `MAX_OUTPUT_TOKENS` | `4096` | Positive Responses output-token limit, including reasoning tokens where applicable. |
| `MAX_RETRIES` | `2` | Additional stream-opening attempts (`0`–`5`). The default permits three attempts total. |
| `RETRY_DELAY_SECONDS` | `0.5` | Initial retry delay (`0`–`10` seconds), doubled per retry and capped at five seconds. |

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

Credentials and the OpenAI client are created in FastAPI lifespan and closed on
shutdown. Token acquisition is lazy; startup, health and model listing do not
perform inference or request a token.

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
{"status":"ok"}
```

This is process health, not a provider-connectivity or inference probe.

### `GET /api/models`

```json
{"models":["deployment-a","deployment-b"],"default_model":"deployment-a"}
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
    {"role": "user", "content": "Earlier question"},
    {"role": "assistant", "content": "Earlier answer"},
    {"role": "user", "content": "New question"}
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

| Event | JSON payload |
| --- | --- |
| `start` | `{session_id, request_id, message_id, time}` |
| `session_title` | `{session_id, request_id, message_id, title}` |
| `delta` | `{session_id, request_id, message_id, text}` |
| `done` | `{session_id, request_id, message_id, time, status: "complete"}` |
| `error` | `{session_id, request_id, message_id, code, message}` |

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

## Tests and checks

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
  coordinator, strict contracts, planner injection and HTTP/SSE endpoints.
- `app/sessions.py`: lazy, single-writer, fsynced Agent journal and recovery.
- `app/agent_files.py`: fail-closed workspace policy and bounded no-follow text reads.
- `main.py`: thin compatibility entry point.
