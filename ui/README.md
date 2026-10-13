# Assistant UI

Angular frontend for the customized personal assistants, using standalone components,
client-side routing, CSS, and Vitest unit tests.

All frontend source, configuration, dependencies, and tooling belong in this directory.
**Run every command below from `./ui`, not the repository root.**

## Setup and development

Use Node.js and npm. Activate your default Node.js environment with nvm before installing dependencies.

```bash
nvm use default
npm ci
npm start
```

Open `http://localhost:4200/`. The development server reloads when source files change.

## First-pass interface

- Create, switch, and delete sessions in the sidebar; unfinished drafts stay with their session.
- Select a system prompt before the first message. That message locks a snapshot of the prompt for
  the session, so later catalogue edits or deletions do not change started sessions.
- Open Settings to select, add, edit, or delete system prompts.
- Send messages with the Send button or Enter; Shift + Enter inserts a new line.
- Both user and assistant bubbles render sanitized Markdown, with syntax highlighting for code.

This is a frontend-only preview: sending adds a user message but does not request or simulate an AI
reply. Sessions, drafts, the active selection, and prompt changes are saved in this browser and
restored when the page reloads. Sidebar visibility and unsaved Settings editor changes are transient.

## Local storage

`ChatState` hydrates its Angular signals synchronously at startup. The UI reads these signals and
updates them through state methods; one Angular effect persists changes to the localStorage key
`ls-assistants.chat-state`.

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
npx ng generate component component-name
```

Production output is written to `dist/ui/`. `npm test` without additional flags runs tests in watch mode.

## Installed libraries

- **Styling:** `tailwindcss`, `@tailwindcss/postcss`, `postcss`, `flowbite`, and `tailwind-scrollbar`.
- **Content rendering:** `marked`, `highlight.js`, and `dompurify`.

Tailwind's PostCSS integration is configured in `.postcssrc.json`. Global styles in
`src/styles.css` load the Flowbite theme and register the Flowbite and scrollbar plugins.

Initialize Flowbite's JavaScript after the affected Angular views render when adding interactive
components; its CSS plugin alone does not initialize dropdowns or modals.

Markdown rendering parses with `marked`, highlights code with `highlight.js`, then sanitizes the
final HTML with DOMPurify before Angular displays it. It does not bypass Angular's HTML sanitization.
