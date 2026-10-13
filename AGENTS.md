# Repository Overview

This repository is for developing customized personal assistants using LLM-based AI technologies.

## Architecture

- **Frontend:** An Angular application in `./ui` for interacting with the assistants.
- **Backend:** A FastAPI service in `./bff` exposing APIs and coordinating assistant functionality.

Keep frontend presentation separate from backend assistant logic, and prefer simple, maintainable solutions.

## Backend Environment

- Keep all Python-related code, configuration, and tooling under `./bff`, including `.env`, virtual environments (`.venv/` or `venv/`), `pyproject.toml`, and `uv.lock`.
- Use `uv` to manage backend dependencies and the Python environment. Run backend `uv` commands from `./bff`.
