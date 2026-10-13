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

When implementing Markdown rendering, sanitize the generated HTML with DOMPurify before displaying it.
Do not treat `marked` output as trusted HTML.
