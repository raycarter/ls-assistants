"""Literal folder search with descriptor authority and disk-backed JSONL staging.

Lines follow str.splitlines semantics for accepted text: LF, CR, CRLF, NEL,
U+2028 and U+2029. Terminators are omitted; empty lines count, but a terminal
separator does not invent an extra line. Other controls (except TAB) are binary.
Glob patterns select workspace-relative paths using fnmatchcase (so '*' can
cross '/'); a slash-free glob selects basenames at any depth.
"""

import codecs
import fnmatch
import json
import os
import stat
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterator
from contextlib import ExitStack
from typing import TextIO

from app.agent_files import AgentFault, FilePolicy
from app.agent_schemas import GrepArguments

BLOCK = 8192
SEPARATORS = "\n\r\x85\u2028\u2029"


def validate_grep(policy: FilePolicy, args: GrepArguments) -> list[str]:
    """Pure pre-approval validation; no filesystem probes or glob expansion."""
    if args.workspace_id not in policy.roots:
        raise AgentFault("scope_unavailable", "The requested workspace is not configured.", 422)
    parts = [] if args.path == "." else args.path.split("/")
    if any(policy.excluded(part) for part in parts):
        raise AgentFault("path_denied", "This path is excluded from Agent access.", 422)
    if not args.pattern or any(
        char in SEPARATORS or unicodedata.category(char) in {"Cc", "Cs"} for char in args.pattern
    ):
        raise AgentFault("invalid_pattern", "The pattern must be a single-line literal.", 422)
    if args.file_glob is not None and any(
        policy.excluded(part) for part in args.file_glob.split("/")
    ):
        raise AgentFault("path_denied", "The file glob contains an excluded component.", 422)
    target = policy.roots[args.workspace_id].joinpath(*parts)
    with policy.storage_lock:
        if any(target.is_relative_to(root) for root in policy.storage_roots):
            raise AgentFault("path_denied", "Agent storage is excluded from file access.", 422)
    return parts


class _Units:
    """Each incremental operation gets a fresh timeout, never a search deadline."""

    def __init__(self, stop: threading.Event, seconds: float):
        self.stop, self.seconds = stop, seconds

    def check(self):
        if self.stop.is_set():
            raise AgentFault("operation_stopped", "The grep operation stopped.")

    def run(self, operation, *args, **kwargs):
        self.check()
        start = time.monotonic()
        result = operation(*args, **kwargs)
        # Resource-producing calls are checked by their caller after ownership
        # has been registered, so cancellation cannot strand a returned fd.
        return result, start

    def done(self, start):
        self.check()
        if time.monotonic() - start >= self.seconds:
            raise AgentFault("file_timeout", "A grep operation exceeded its unit timeout.")

    def call(self, operation, *args, **kwargs):
        result, start = self.run(operation, *args, **kwargs)
        self.done(start)
        return result


class GrepDocument:
    """Caller-owned source: close after the artifact persistence worker finishes."""

    def __init__(self, source: TextIO, units: _Units):
        self._source, self._units = source, units
        self._text_length = 0
        self.total_matches = self.files_searched = 0
        self.skipped_binary = self.skipped_unreadable = 0

    @property
    def text_length(self) -> int:
        return self._text_length

    @property
    def complete(self) -> bool:
        return self.skipped_binary == self.skipped_unreadable == 0

    def iter_text(self) -> Iterator[str]:
        self._units.call(self._source.seek, 0)
        while chunk := self._units.call(self._source.read, BLOCK):
            yield chunk

    def close(self):
        self._source.close()


class _Binary(Exception):
    pass


class _Unreadable(Exception):
    pass


def _version(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _spool(units, resources):
    source, start = units.run(tempfile.TemporaryFile, mode="w+t", encoding="utf-8", newline="")
    resources.callback(source.close)
    units.done(start)
    return source


def _copy(source, target, units):
    units.call(source.seek, 0)
    length = 0
    while chunk := units.call(source.read, BLOCK):
        units.call(target.write, chunk)
        length += len(chunk)
    return length


def _scan_file(fd, path, pattern, staged, units):
    decoder = codecs.getincrementaldecoder("utf-8")()
    number, matches = 1, 0
    tail = ""
    matched = present = skip_lf = False
    # The current line contains already JSON-escaped fragments, not a giant str.
    with ExitStack() as resources:
        line = _spool(units, resources)

        def finish():
            nonlocal number, matches, tail, matched, present
            if matched:
                units.call(
                    staged.write,
                    '{"path":' + json.dumps(path) + ',"line":' + str(number) + ',"text":"',
                )
                _copy(line, staged, units)
                units.call(staged.write, '"}\n')
                matches += 1
            units.call(line.seek, 0)
            units.call(line.truncate, 0)
            number += 1
            tail, matched, present = "", False, False

        while True:
            try:
                block = units.call(os.read, fd, BLOCK)
            except OSError as exc:
                raise _Unreadable from exc
            try:
                text = decoder.decode(block, final=not block)
            except UnicodeError as exc:
                raise _Binary from exc
            if any(unicodedata.category(c) == "Cc" and c not in "\t" + SEPARATORS for c in text):
                raise _Binary
            if text:
                if skip_lf and text.startswith("\n"):
                    text = text[1:]
                skip_lf = text.endswith("\r")
            for fragment in text.splitlines(keepends=True):
                units.check()
                ended = fragment[-1] in SEPARATORS
                if ended:
                    fragment = fragment[:-2] if fragment.endswith("\r\n") else fragment[:-1]
                present |= bool(fragment)
                if not matched:
                    window = tail + fragment
                    matched = pattern in window
                    tail = window[-(len(pattern) - 1) :] if len(pattern) > 1 else ""
                units.call(line.write, json.dumps(fragment, ensure_ascii=True)[1:-1])
                if ended:
                    finish()
            if not block:
                break
        if present:
            finish()
    return matches


def scan_folder(
    policy: FilePolicy, args: GrepArguments, stop: threading.Event, timeout_seconds: float
) -> GrepDocument:
    """Scan the entire selected folder; offsets and limits belong to artifact GET.

    File read errors/mutation and multiply-linked regular files are counted as
    unreadable; directory enumeration,
    selected-root errors and observed directory mutation fail the operation.
    Expected exclusions, protected storage, symlinks and special files are pruned.
    Timeouts are cooperative: a blocking syscall is checked when it returns.
    """
    parts = validate_grep(policy, args)
    units = _Units(stop, timeout_seconds)
    units.check()
    document = None
    try:
        source, start = units.run(tempfile.TemporaryFile, mode="w+t", encoding="utf-8", newline="")
        document = GrepDocument(source, units)
        units.done(start)
        identities = units.call(policy.protected_identities)

        def storage_path(path):
            with policy.storage_lock:
                return any(path.is_relative_to(root) for root in policy.storage_roots)

        def check_directory(fd):
            units.check()
            identities.update(units.call(policy.protected_identities))
            info = units.call(os.fstat, fd)
            if (info.st_dev, info.st_ino) in identities:
                raise AgentFault("path_denied", "Agent storage is excluded from file access.", 422)

        with ExitStack() as stack:

            def opened(name, flags, parent=None):
                fd, start = units.run(os.open, name, flags, dir_fd=parent)
                stack.callback(os.close, fd)
                units.done(start)
                return fd

            directory = opened("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            check_directory(directory)
            for part in policy.roots[args.workspace_id].parts[1:]:
                directory = opened(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, directory)
                check_directory(directory)
            for part in parts:
                directory = opened(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, directory)
                check_directory(directory)

            # One live scandir and descriptor per depth; never materialize a tree.
            seen = set()
            frames = []

            def push(fd, relative, resources):
                info = units.call(os.fstat, fd)
                seen.add((info.st_dev, info.st_ino))
                entries, start = units.run(os.scandir, fd)
                resources.callback(entries.close)
                units.done(start)
                frames.append((fd, relative, entries, info, resources))

            push(directory, parts, stack.enter_context(ExitStack()))
            try:
                while frames:
                    units.check()
                    fd, relative, entries, before, resources = frames[-1]
                    entry = units.call(next, entries, None)
                    if entry is None:
                        if _version(before) != _version(units.call(os.fstat, fd)):
                            raise OSError("Directory changed during grep")
                        frames.pop()
                        resources.close()
                        continue
                    if policy.excluded(entry.name):
                        continue
                    child_parts = [*relative, entry.name]
                    absolute = policy.roots[args.workspace_id].joinpath(*child_parts)
                    if storage_path(absolute):
                        continue
                    info = units.call(os.stat, entry.name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child_resources = ExitStack()
                        try:
                            child, start = units.run(
                                os.open,
                                entry.name,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=fd,
                            )
                            child_resources.callback(os.close, child)
                            units.done(start)
                            try:
                                check_directory(child)
                            except AgentFault as exc:
                                if exc.code != "path_denied":
                                    raise
                                continue
                            after = units.call(os.fstat, child)
                            if _version(info) != _version(after):
                                raise OSError("Directory changed during open")
                            if (after.st_dev, after.st_ino) in seen:
                                continue
                            push(child, child_parts, child_resources)
                            child_resources = None
                        finally:
                            if child_resources is not None:
                                child_resources.close()
                    elif stat.S_ISREG(info.st_mode):
                        path = "/".join(child_parts)
                        glob = args.file_glob
                        if glob is not None and not fnmatch.fnmatchcase(
                            path if "/" in glob else entry.name, glob
                        ):
                            continue
                        # An allowed spelling must not reveal a hard-linked
                        # storage or excluded file through the same inode.
                        if info.st_nlink > 1:
                            document.skipped_unreadable += 1
                            continue
                        with ExitStack() as file_resources:
                            try:
                                try:
                                    file_fd, start = units.run(
                                        os.open,
                                        entry.name,
                                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                        dir_fd=fd,
                                    )
                                    file_resources.callback(os.close, file_fd)
                                    units.done(start)
                                    initial = units.call(os.fstat, file_fd)
                                except OSError as exc:
                                    raise _Unreadable from exc
                                if (
                                    not stat.S_ISREG(initial.st_mode)
                                    or initial.st_nlink > 1
                                    or _version(info) != _version(initial)
                                ):
                                    raise _Unreadable
                                staged = _spool(units, file_resources)
                                matches = _scan_file(file_fd, path, args.pattern, staged, units)
                                try:
                                    final = units.call(os.fstat, file_fd)
                                    named = units.call(
                                        os.stat, entry.name, dir_fd=fd, follow_symlinks=False
                                    )
                                except OSError as exc:
                                    raise _Unreadable from exc
                                if (
                                    final.st_nlink > 1
                                    or named.st_nlink > 1
                                    or _version(initial) != _version(final)
                                    or _version(final) != _version(named)
                                ):
                                    raise _Unreadable
                                document._text_length += _copy(staged, source, units)
                                document.total_matches += matches
                                document.files_searched += 1
                            except _Binary:
                                document.skipped_binary += 1
                            except _Unreadable:
                                document.skipped_unreadable += 1
            finally:
                for *_, resources in reversed(frames):
                    resources.close()
        units.check()
        return document
    except BaseException as exc:
        if document is not None:
            document.close()
        if isinstance(exc, (OSError, ValueError)):
            raise AgentFault(
                "file_unavailable", "The grep folder is unavailable or changed."
            ) from exc
        raise
