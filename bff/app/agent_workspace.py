"""Fixed approved workspace mapping; construction never probes the filesystem."""

import sys
from pathlib import Path

from app.agent_files import FilePolicy

WORKSPACE_ID = "workspace"
LOGICAL_ROOT = Path("/tmp/ls-assistants")


def build_file_policy() -> FilePolicy:
    # Darwin's trusted platform spelling only, not general symlink resolution.
    root = Path("/private/tmp/ls-assistants") if sys.platform == "darwin" else LOGICAL_ROOT
    return FilePolicy({WORKSPACE_ID: root})
