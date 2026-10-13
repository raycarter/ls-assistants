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
- Open the clearly labelled Markdown demo session to preview assistant bubbles and formatting.

This is a frontend-only preview: sending adds a user message but does not request or simulate an AI
reply. Sessions, drafts, and prompt changes are held in memory and reset when the page reloads.

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
