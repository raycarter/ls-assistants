"""File-scoped text streaming; callers serialize calls and await their owned I/O.

Only encountered bytes are validated. Stopping early does not validate the suffix.
The descriptor stays pinned through EOF so a caller can check again after an LLM
call. Deadlines apply to individual next_chunk calls, never to the reader lifetime.
"""

import codecs
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.agent_files import (
    TEXT_CONTROLS,
    AgentFault,
    FilePolicy,
    canonical_digest,
    open_directory,
)
from app.agent_schemas import ReadArguments

CHUNK_BYTES = 16000


@dataclass(frozen=True)
class ReadChunk:
    content: str
    start_byte: int
    end_byte: int
    total_bytes: int
    eof: bool
    file_version: str


def _version(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class FileChunkReader:
    def __init__(self, policy: FilePolicy, args: ReadArguments):
        # No validation, stat, open, or other filesystem work at construction.
        self._policy = policy
        self._args = args.model_copy()
        self._fd: int | None = None
        self._initial: tuple[int, ...] | None = None
        self._file_version = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._read_bytes = self._end_byte = 0
        self._eof = self._closed = False

    def _release(self) -> None:
        fd, self._fd = self._fd, None
        self._closed = True
        if fd is not None:
            os.close(fd)

    def _assert_version(self) -> None:
        if self._fd is not None and _version(os.fstat(self._fd)) != self._initial:
            raise AgentFault("file_changed", "The file changed during the read.")

    def check_version(self) -> None:
        """Recheck the pinned file, including after EOF; errors close the reader."""
        try:
            if self._closed:
                raise AgentFault("file_closed", "The file reader is closed.")
            self._assert_version()
        except OSError as exc:
            self._release()
            raise AgentFault("file_unavailable", "The file is unavailable.") from exc
        except BaseException:
            self._release()
            raise

    def close(self) -> None:
        """Verify once more and release the owned descriptor; idempotent."""
        if self._closed:
            return
        try:
            self.check_version()
        finally:
            self._release()

    def _open(self, check: Callable[[], None]) -> None:
        parts = self._policy.validate(self._args)
        check()
        identities = self._policy.protected_identities()

        def exclude_storage(fd: int) -> None:
            check()
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) in identities:
                raise AgentFault("path_denied", "Agent storage is excluded from file access.", 422)

        directory = open_directory(
            self._policy.roots[self._args.workspace_id], check=exclude_storage
        )
        try:
            for part in parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
                exclude_storage(directory)
            check()
            self._fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            info = os.fstat(self._fd)
            if not stat.S_ISREG(info.st_mode):
                raise AgentFault("file_unsupported", "Read requires a regular text file.")
            self._initial = _version(info)
            self._file_version = canonical_digest(self._initial)
            check()
        finally:
            os.close(directory)

    def next_chunk(self, stop: threading.Event, deadline: float) -> ReadChunk:
        """Return contiguous decoded source bytes, starting at zero regardless of paging args.

        At EOF subsequent calls return an empty EOF chunk without advancing or
        reading. UTF-8's incomplete trailing codepoint is carried to the next call;
        each returned content encodes to at most CHUNK_BYTES source bytes.
        """

        def check() -> None:
            if stop.is_set():
                raise AgentFault("operation_stopped", "The read operation stopped.")
            if time.monotonic() >= deadline:
                raise AgentFault("file_timeout", "The file read exceeded its request timeout.")

        try:
            check()
            if self._closed:
                raise AgentFault("file_closed", "The file reader is closed.")
            if self._fd is None:
                self._open(check)
            self._assert_version()
            assert self._initial is not None and self._fd is not None
            total = self._initial[2]
            start = self._end_byte
            text = ""
            if not self._eof:
                remaining = CHUNK_BYTES - len(self._decoder.getstate()[0])
                fragments = []
                while remaining and self._read_bytes < total:
                    check()
                    block = os.read(self._fd, min(remaining, total - self._read_bytes))
                    check()
                    if not block:
                        raise AgentFault("file_changed", "The file ended before its initial size.")
                    self._read_bytes += len(block)
                    remaining -= len(block)
                    fragments.append(self._decoder.decode(block))
                if self._read_bytes == total:
                    check()
                    if os.read(self._fd, 1):
                        raise AgentFault("file_changed", "The file grew during the read.")
                    check()
                    fragments.append(self._decoder.decode(b"", final=True))
                    self._eof = True
                text = "".join(fragments)
                if TEXT_CONTROLS.search(text):
                    raise ValueError("binary text")
                self._end_byte = self._read_bytes - len(self._decoder.getstate()[0])
            self._assert_version()
            check()
            return ReadChunk(text, start, self._end_byte, total, self._eof, self._file_version)
        except (OSError, ValueError) as exc:
            self._release()
            raise AgentFault(
                "file_unavailable", "The file is unavailable or is not UTF-8 text."
            ) from exc
        except BaseException:
            self._release()
            raise
