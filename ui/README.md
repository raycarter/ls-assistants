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
- Select a system prompt before the first message. That message locks a snapshot of the prompt for
  the session, so later catalogue edits or deletions do not change started sessions.
- Open Settings to select, add, edit, or delete system prompts.
- Send messages with the Send button or Enter; Shift + Enter inserts a new line.
- Choose a model from the BFF's model list and a Thinking effort (`low`, `medium`, `high`, `xhigh`)
  above Send. The default effort is `medium`; all four choices remain available for every model.
  The BFF reports unsupported combinations in the reply error UI.
- Both user and assistant bubbles render sanitized Markdown, with syntax highlighting for code.
- Replies grow incrementally in one bubble. Partial rendering failures silently show escaped plain text.
- Each session permits one in-flight turn. You can edit its next draft or chat in another session
  while receiving a reply. Replies never activate another session. Deleting a session aborts its turn.
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
  "messages": [{ "role": "user", "content": "Hello" }]
}
```

History is ordered visible user/assistant text. No reasoning payloads are stored or sent.
Failed, stopped, interrupted, and streaming assistant replies are excluded from subsequent request
history; their partial text remains visible until explicitly replaced by retry.
Zero-length messages are also excluded at the outbound history boundary, while remaining in the
visible conversation and storage.

SSE events are `start`, `delta`, `done`, and `error`. Each carries `session_id`, `request_id`,
and server-assigned `message_id`. `start` adds reply-creation `time`; `delta` adds `text` and has no
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
Completion, errors, and Stop flush accumulated text and storage immediately. Ordinary state changes
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
  not rendered HTML. Existing timestamps/statuses are preserved, and new user messages get ISO timestamps.
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

## Installed libraries

- **Styling:** `tailwindcss`, `@tailwindcss/postcss`, `postcss`, `flowbite`, and `tailwind-scrollbar`.
- **Content rendering:** `marked`, `highlight.js`, and `dompurify`.

Tailwind's PostCSS integration is configured in `.postcssrc.json`. Global styles in
`src/styles.css` load the Flowbite theme and register the Flowbite and scrollbar plugins.

Initialize Flowbite's JavaScript after the affected Angular views render when adding interactive
components; its CSS plugin alone does not initialize dropdowns or modals.

Markdown rendering parses with `marked`, highlights code with `highlight.js`, then sanitizes the
final HTML with DOMPurify before Angular displays it. It does not bypass Angular's HTML sanitization.
