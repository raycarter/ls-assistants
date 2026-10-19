"""Descriptor-relative, no-follow, bounded text reads; no shell or provider access."""

import hashlib
import json
import os
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.agent_schemas import ReadArguments


class AgentFault(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def open_directory(path: Path, *, check: Callable[[int], None] | None = None) -> int:
    """Reject symlinks in *every* ancestor, including configured roots."""
    path = path.absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        if check is not None:
            check(fd)
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if check is not None:
                check(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


class FilePolicy:
    VERSION = "read-text-v3"
    DENIED = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "sessions",
        "dev-plans-archive",
        ".aws",
        ".azure",
        ".ssh",
        ".config",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_rsa",
        "id_ed25519",
        "site-packages",
        ".tox",
        "credentials",
        "secrets",
    }

    def __init__(self, roots: dict[str, Path] | None = None):
        self.roots = {key: Path(os.path.abspath(path)) for key, path in (roots or {}).items()}
        self.storage_roots: set[Path] = set()
        self.storage_identities: dict[Path, set[tuple[int, int]]] = {}
        self.storage_lock = threading.Lock()
        if any(
            self.excluded(part)
            for root in self.roots.values()
            for index, part in enumerate(root.parts[1:], 1)
            # macOS's /private is a platform anchor, not an allowed arbitrary
            # "private" component elsewhere. No symlink resolution is needed.
            if not (sys.platform == "darwin" and index == 1 and part == "private")
        ):
            raise AgentFault(
                "scope_unavailable", "Configured workspace contains an excluded directory.", 422
            )

    @classmethod
    def excluded(cls, part: str) -> bool:
        name = part.casefold()
        return (
            part in ("", ".", "..")
            or "\\" in part
            or "\x00" in part
            or name in cls.DENIED
            or name.startswith(".env")
            or "credential" in name
            or "secret" in name
            or name.endswith((".pem", ".key"))
            or "private" in name
            or "dev-plans-archive" in name
        )

    def bind_storage(self, root: Path, *, identity: tuple[int, int] | None = None):
        root = Path(os.path.abspath(root))
        with self.storage_lock:
            self.storage_roots.add(root)
            if identity is not None:
                self.storage_identities.setdefault(root, set()).add(identity)

    def protected_identities(self) -> set[tuple[int, int]]:
        """Called in the read worker, not at unconfigured application startup.

        Keep identities pinned from the live store fd, even if its directory is
        renamed. Also protect the current directory at each configured spelling.
        """
        with self.storage_lock:
            roots = tuple(self.storage_roots)
            identities = {root: set(ids) for root, ids in self.storage_identities.items()}
        for root in roots:
            try:
                directory = open_directory(root)
            except FileNotFoundError:
                if not identities.get(root):
                    raise AgentFault(
                        "scope_unavailable", "The storage boundary is unavailable."
                    ) from None
                continue
            try:
                info = os.fstat(directory)
                identity = (info.st_dev, info.st_ino)
                identities.setdefault(root, set()).add(identity)
                self.bind_storage(root, identity=identity)
            finally:
                os.close(directory)
        return {identity for ids in identities.values() for identity in ids}

    def projection(self) -> dict[str, str]:
        # Fingerprint binds actual configured paths without exposing absolute server paths.
        with self.storage_lock:
            storage = {
                str(root): sorted(self.storage_identities.get(root, set()))
                for root in self.storage_roots
            }
        return {
            "version": self.VERSION,
            "roots_digest": canonical_digest({k: str(v) for k, v in self.roots.items()}),
            "workspaces": ",".join(sorted(self.roots)),
            "storage_digest": canonical_digest(storage),
            "rules_digest": canonical_digest(
                {
                    "denied_components": sorted(self.DENIED),
                    "path_rules": "shared-exclusions-storage-identity-ancestors-v3",
                    "file_bytes": 256000,
                    "page_bytes": 16000,
                    "line_chars": 1000,
                    "page_lines": 200,
                }
            ),
        }

    def validate(self, args: ReadArguments) -> list[str]:
        parts = args.path.split("/")
        if args.workspace_id not in self.roots:
            raise AgentFault("scope_unavailable", "The requested workspace is not configured.", 422)
        for part in parts:
            if self.excluded(part):
                raise AgentFault("path_denied", "This path is excluded from Agent access.", 422)
        target = self.roots[args.workspace_id].joinpath(*parts)
        with self.storage_lock:
            protected = any(target.is_relative_to(root) for root in self.storage_roots)
        if protected:
            raise AgentFault("path_denied", "Agent storage is excluded from file access.", 422)
        return parts

    def read(self, args: ReadArguments, stop: threading.Event, deadline: float) -> dict:
        parts = self.validate(args)

        def check():
            if stop.is_set() or time.monotonic() >= deadline:
                raise AgentFault("operation_stopped", "The read operation stopped.")

        check()
        fd = None
        directory = None
        try:
            identities = self.protected_identities()

            def exclude_storage(directory_fd: int):
                check()
                info = os.fstat(directory_fd)
                if (info.st_dev, info.st_ino) in identities:
                    raise AgentFault(
                        "path_denied", "Agent storage is excluded from file access.", 422
                    )

            # Check every ancestor as well as the root itself. Lexical spelling
            # is only a preflight shortcut, not the storage authorization check.
            directory = open_directory(self.roots[args.workspace_id], check=exclude_storage)
            for part in parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
                exclude_storage(directory)
            exclude_storage(directory)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 256_000:
                raise AgentFault("file_unsupported", "Read requires a text file of at most 256 KB.")
            chunks = []
            size = 0
            while True:
                check()
                block = os.read(fd, min(8192, 256_001 - size))
                if not block:
                    break
                chunks.append(block)
                size += len(block)
                if size > 256_000:
                    raise AgentFault("file_unsupported", "The file exceeds the read size limit.")
            raw = b"".join(chunks)
            if b"\x00" in raw:
                raise ValueError("binary")
            text = raw.decode("utf-8")
            if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
                raise ValueError("binary")
            lines = text.splitlines()
            if args.offset > max(1, len(lines)):
                raise AgentFault("invalid_offset", "Offset is past the end of the file.", 422)
            selected = []
            used = 0
            clipped = False
            for number, line in enumerate(
                lines[args.offset - 1 : args.offset - 1 + args.limit], args.offset
            ):
                check()
                clipped |= len(line) > 1000
                rendered = f"{number}: {line[:1000]}"
                if used + len(rendered.encode()) > 16000:
                    break
                selected.append(rendered)
                used += len(rendered.encode())
            return {
                "content": "\n".join(selected),
                "path": args.path,
                "offset": args.offset,
                "returned_lines": len(selected),
                "truncated": clipped or args.offset - 1 + len(selected) < len(lines),
            }
        except (OSError, ValueError) as exc:
            raise AgentFault(
                "file_unavailable", "The file is unavailable or is not UTF-8 text."
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                os.close(directory)
