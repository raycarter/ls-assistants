# Repository Overview

This repository is for developing customized personal assistants using LLM-based AI technologies.

## Architecture

- **Frontend:** An Angular application in `./ui` for interacting with the assistants.
- **Backend:** A FastAPI service in `./bff` exposing APIs and coordinating assistant functionality.

Keep frontend presentation separate from backend assistant logic, and prefer simple, maintainable solutions.

## Frontend Environment

- Keep all frontend code, configuration, dependencies, and tooling under `./ui`, including `package.json`, `package-lock.json`, `node_modules/`, and Angular, Tailwind, and PostCSS configuration.
- Run all frontend commands and package installations from `./ui`, not the repository root.
- activating the current installed Node.js with `nvm use default` before running frontend commands. Use the workspace Angular CLI through npm scripts or `npx ng` for development.

## Backend Environment

- Keep all Python-related code, configuration, and tooling under `./bff`, including `.env`, virtual environments (`.venv/` or `venv/`), `pyproject.toml`, and `uv.lock`.
- Use `uv` to manage backend dependencies and the Python environment. Run backend `uv` commands from `./bff`.
