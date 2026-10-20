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

## Agent mode: approval-bound tool slice

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

Request → tool-free plan → structured scope review → **Approve and run** → `read_file` or
`web_fetch` result → completion receipt → GET-based history after reload. Tests drive the actual
Agent transport through HTTP/SSE doubles and backend-generated wire fixtures. These do not establish
live provider/site compatibility or replace the backend durability and access-policy tests.

The plan displays unverified assumptions, workspace/path and exact arguments, model/effort, limits,
revision/digest and any recorded search policy. Approval, rejection and plan feedback bind the current
identity. Double clicks are guarded while a command is pending. Results and answers use the existing
sanitized Markdown and local-time display pipes; original server timestamps are preserved. Agent
history does not auto-scroll, so updates do not pull a user away from review.
File results retain the BFF's path, offset, returned-line count and truncation flag. Live and reloaded
result cards show the recorded excerpt range/count and explicitly disclose omitted content or clipped
long lines when `truncated` is true; an excerpt is not presented as evidence of the complete file.

Fetch review displays the exact public URL and zero-based character-page arguments. Successful fetch
cards distinguish actual retrieved text from file excerpts, showing title, requested/final URL,
retrieval time, content type/status, and page counts. Source text and titles are escaped literal text,
not executable HTML or interpreted Markdown. Links allow credential-free HTTP(S) only.
The full extracted text is retained by the BFF. Read retained pages with the result card's paging
controls; these issue only GET requests to the evidence endpoint, never fetch the URL again or approve
another execution. Loading/errors are result-local; session changes and component destruction abort
or suppress stale results. Reload restores the execution preview and access to retained pages, not
the prior inspector position.

Stop sends the captured operation ID. **Stopping…** is nonterminal and keeps preferences locked.
Archive retains the session in reviewable history; pending archive stays locked even after a run settles,
until the server reports archival or explicitly clears `archive_pending`. A lost/failed archive
acknowledgement remains uncertain until an authoritative reload. Reload history is a safe GET/list action,
including archived sessions. No execution or
planning command is automatically reposted after a transport error or reload.
An in-flight archive command has separate local ownership: a concurrently delivered, earlier
`archive_pending: false` projection cannot release its lock before acknowledgement or confirmed recovery.

### Agent API and recovery

All routes are under `/api/agent/sessions`: list (`include_archived=true`), create, get, preference
PATCH, `/{sid}/runs`, `/{sid}/runs/{rid}/execute`, `/revise`, `/reject`, `/cancel`, and
`/{sid}/archive`. Mutation bodies contain a fresh `request_id`. Approval sends `plan_id`, `revision`
and `digest`; Stop sends `operation_id`. The transport understands question-identified clarification
rejection, but the current BFF returns HTTP 501 for the deferred clarification flow.
HTTP JSON failures are shown with status and API detail, including missing planner configuration.
`GET /{sid}/artifacts/{artifact_id}?offset=0&limit=12000` reads committed same-session evidence and
returns `{artifact_id,content,offset,returned_chars,total_chars,next_offset}`. Offsets/counts use
Unicode characters, not UTF-16 code units. Page size is at most 16000; retained totals and continuation
must agree with the execution metadata. The decoder rejects unsafe provenance and malformed or
inconsistent pages without replacing the original preview.

Agent SSE uses event `agent` and decimal committed sequence IDs, with schema version 1 and a full
session snapshot per envelope. Its independent decoder validates timestamps, snapshot shapes,
session correlation and frozen approval identity. Work records must match the owning request/run/operation;
known session lifecycle records can carry other request IDs and null run/operation IDs without taking
ownership of the stream. A validated first planning/approval admission establishes the server baseline
even if metadata commits advanced it beyond the cached sequence; subsequent records must be contiguous.
Committed sequence is the causal order. Strict timestamp syntax is checked, but UTC wall-clock readings
may step backwards and are not used as a monotonic clock.
New-run acknowledgements must match the submitted request and use a new run ID. Execution requires a
reviewed plan, completed runs require completed results, and archived sessions must contain only settled runs.
Identical duplicates are ignored; gaps, conflicting duplicates, invalid responses and EOF during
active work fail safely and trigger GET recovery. A failed recovery leaves the last committed view
visible with an error. Reading an active operation after reload never takes ownership or resumes it;
use Stop or reload history to inspect eventual settlement.

Only navigation metadata and unsent Agent drafts are saved in version 1 of
`ls-assistants.agent-ui-state`; server plans/results are not browser-authoritative copies. Invalid or
unsupported saved metadata is left untouched, with a content-free console warning and memory-only
changes. The key shares Chat's injectable storage boundary for deterministic tests.

### Current limitations and retained content

- The production UI does **not** fabricate fixture/demo plans. The BFF planner is unavailable until
  explicitly injected/configured. These frontend tests make no real provider or search calls.
- The BFF supports approved `read_file` and `web_fetch` steps through an injected planner. Web search,
  folder grep, and no-tool steps remain unavailable. The full four-tool release, full event/audit
  timeline, paged session history, and live deployment compatibility remain outside this increment.
- Fetch handles public static HTML/plain text only, not authenticated/internal pages or browser/JS
  rendering. The BFF requests identity encoding and rejects compressed responses; static extraction
  can report unsupported/empty pages. Existing prototype plan/run/history caps remain pending backend
  migration and are displayed honestly, not represented as the target product policy.
- Plan feedback is supported. Clarification handling, including replies and rejection, is deferred in
  the current BFF, which produces no clarification states. Compatibility question records are rendered
  read-only without offering unsupported commands; use a new session or await server-side resolution.
- History renders the server's run projections, including archived sessions. It does not implement
  event replay or automatic background polling; reload history to refresh settlement after reconnection.
- The UI discloses retention before use: user requests, reviewed plans, approved source excerpts,
  tool results and logical model input/output are retained by the BFF. Archive preserves records;
  there is no automatic expiry. Permitted content may contain sensitive text; there is no guarantee
  of secret-content scrubbing. Approved fetch URLs leave the BFF; future search queries would too. Resource limits are not a
  guaranteed currency ceiling; cost is displayed as unknown.
- Mocked approval/Stop tests verify UI command ownership only. Durable approval, tool authorization,
  request-id idempotency and cancellation settlement remain BFF responsibilities.

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

First-slice verification is exercised by frontend API/state/component doubles, including preference
rollback, archive settlement, correlated acknowledgements, recovery validation and session-scoped errors.
Review-fix verification (2026-10-16): 543 frontend tests pass, including the independent backend-generated
HTTP/SSE fixture contract suite,
alongside TypeScript checking, the production build and repository-wide Prettier checking.
The build's initial bundle is approximately 442 kB; Agent workspace and Markdown load lazily.
The build reports two CSS critical-inlining selector warnings; the full stylesheet remains available.

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
