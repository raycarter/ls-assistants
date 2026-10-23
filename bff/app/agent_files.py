"""Descriptor-relative, no-follow, bounded text reads; no shell or provider access."""

import codecs
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.agent_schemas import ReadArguments

READ_CHUNK_BYTES = 8192
PAGE_BYTES = 16000
LINE_CHARS = 1000
PAGE_LINES = 200
TEXT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


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


def _text_page(fd: int, args: ReadArguments, check: Callable[[], None]) -> dict:
    """Validate through real EOF; retain only a page and the current line's display prefix."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    selected = []
    number, used = 1, 0
    prefix = ""
    line_present = line_clipped = clipped = page_full = skip_lf = False

    def finish_line():
        nonlocal number, used, prefix, line_present, line_clipped, clipped, page_full
        if args.offset <= number < args.offset + args.limit and not page_full:
            rendered = f"{number}: {prefix}"
            # Count the joining newline too, so the complete content fits the byte bound.
            size = len(rendered.encode("utf-8")) + bool(selected)
            if used + size > PAGE_BYTES:
                page_full = True
            else:
                selected.append(rendered)
                used += size
                clipped |= line_clipped
        number += 1
        prefix = ""
        line_present = line_clipped = False

    while True:
        check()
        block = os.read(fd, READ_CHUNK_BYTES)
        check()
        text = decoder.decode(block, final=not block)
        if TEXT_CONTROLS.search(text):
            raise ValueError("binary")
        # splitlines preserves the old Unicode separator semantics. Only CRLF can
        # straddle decoded chunks; finish CR immediately and swallow its following LF.
        if text:
            if skip_lf and text.startswith("\n"):
                text = text[1:]
            skip_lf = text.endswith("\r")
        for fragment in text.splitlines(keepends=True):
            ended = fragment[-1] in "\n\r\x85\u2028\u2029"
            if ended:
                fragment = fragment[:-2] if fragment.endswith("\r\n") else fragment[:-1]
            line_present |= bool(fragment)
            if args.offset <= number < args.offset + args.limit and not page_full:
                remaining = LINE_CHARS - len(prefix)
                line_clipped |= len(fragment) > remaining
                prefix += fragment[:remaining]
            if ended:
                finish_line()
        check()
        if not block:
            break
    if line_present:
        finish_line()
    total_lines = number - 1
    if args.offset > max(1, total_lines):
        raise AgentFault("invalid_offset", "Offset is past the end of the file.", 422)
    return {
        "content": "\n".join(selected),
        "path": args.path,
        "offset": args.offset,
        "returned_lines": len(selected),
        "truncated": clipped or args.offset - 1 + len(selected) < total_lines,
    }


class FilePolicy:
    VERSION = "read-text-v4"
    # Exact released v3 rules, never recomputed from potentially changed current rules.
    LEGACY_VERSION = "read-text-v3"
    LEGACY_RULES_DIGEST = "060b1c1ed619a9dd292d85d30250c3e6387a0e9c67684652209cdb9fa298a20a"
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
                    "text_rules": "streaming-utf8-controls-splitlines-eof-v1",
                    "page_bytes": PAGE_BYTES,
                    "line_chars": LINE_CHARS,
                    "page_lines": PAGE_LINES,
                }
            ),
        }

    def matches(self, projection: dict[str, str]) -> bool:
        """Accept current policy or only its known size-quota predecessor at identical scope."""
        current = self.projection()
        return projection == current or projection == {
            **current,
            "version": self.LEGACY_VERSION,
            "rules_digest": self.LEGACY_RULES_DIGEST,
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
        """deadline is a fresh per-request monotonic deadline supplied by the caller."""
        parts = self.validate(args)

        def check():
            if stop.is_set():
                raise AgentFault("operation_stopped", "The read operation stopped.")
            if time.monotonic() >= deadline:
                raise AgentFault("file_timeout", "The file read exceeded its request timeout.")

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
            if not stat.S_ISREG(info.st_mode):
                raise AgentFault("file_unsupported", "Read requires a regular text file.")
            result = _text_page(fd, args, check)
            after = os.fstat(fd)
            check()
            # The fd pins the opened file across path replacement. Fail closed on
            # observed mutation rather than returning a mixed or falsely complete page.
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("file changed during read")
            return result
        except (OSError, ValueError) as exc:
            raise AgentFault(
                "file_unavailable", "The file is unavailable or is not UTF-8 text."
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                os.close(directory)
