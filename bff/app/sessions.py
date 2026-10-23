"""Single-writer incremental journal with verified, session-local JSON payloads.

Local POSIX filesystem only. A process lock excludes other BFF workers. No network
filesystem/power-loss guarantees; fsync commits precede observable publication.
"""

import codecs
import fcntl
import hashlib
import json
import operator
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

from pydantic import Field

from app.agent_files import AgentFault, canonical_digest, open_directory
from app.agent_schemas import (
    AgentError,
    AgentEvent,
    AgentSession,
    ArtifactId,
    ArtifactPage,
    Claim,
    CreateSession,
    JournalRecord,
    Strict,
)
from app.agent_views import PublicIndex, PublicViews, index_record
from app.session_journal import PayloadRef, SessionState, decode, encode

SESSION_ID = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[a-z0-9]{4}$")
ARTIFACT_FILE = re.compile(r"^[a-f0-9]{32}\.txt$")
PAYLOAD_FILE = re.compile(r"^[a-f0-9]{32}\.json$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def session_id() -> str:
    suffix = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ-") + suffix


class ArtifactReceipt(Strict):
    artifact_id: ArtifactId
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)
    total_chars: int = Field(ge=0)


class TextDocument(Protocol):
    @property
    def text_length(self) -> int: ...

    def iter_text(self) -> Iterator[str]: ...


@dataclass(frozen=True)
class RecordLocation:
    offset: int
    length: int
    sha256: str


@dataclass(frozen=True)
class FileStamp:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class VerifiedFiles:
    """Integrity metadata only; no payload bodies or historical projections."""

    journal: FileStamp
    payloads: Mapping[str, FileStamp]


@dataclass(frozen=True)
class Committed:
    projection: SessionState
    baseline: AgentSession
    locations: tuple[RecordLocation, ...]
    claims: Mapping[str, str]
    attempts: Mapping[str, int]
    operations: frozenset[tuple[str, str]]
    payloads: Mapping[str, PayloadRef]
    views: PublicIndex


class HistoricalRecords(Sequence[JournalRecord]):
    """Read-only compatibility view. Each iteration/slice replays the journal once."""

    def __init__(self, store: SessionStore, sid: str):
        self._store, self._sid = store, sid

    def __len__(self):
        return len(self._store._committed[self._sid].locations)

    def __getitem__(self, index):
        if isinstance(index, slice):
            selected = range(*index.indices(len(self)))
            if not selected:
                return []
            records = {
                i: record
                for i, record in enumerate(self._store._history(self._sid, max(selected) + 1))
                if i in selected
            }
            return [records[i] for i in selected]
        index = operator.index(index)
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError("journal record index out of range")
        return next(self._store._history(self._sid, index + 1, start=index))

    def __iter__(self):
        return self._store._history(self._sid, len(self))


class SessionStore:
    def __init__(
        self,
        root: Path | None = None,
        *,
        clock: Callable[[], str] = utc_now,
        id_factory: Callable[[], str] = session_id,
    ):
        self.root = root or Path(__file__).absolute().parents[1] / "sessions"
        self.clock, self.id_factory = clock, id_factory
        self.lock = threading.RLock()
        # Failure signalling must not wait for the I/O lock. This short barrier
        # linearizes a latched failure against publication, never against fsync.
        self._publication_lock = threading.Lock()
        self._poisoned = False
        self.fd: int | None = None
        self.lease: int | None = None
        self._root_identity: tuple[int, int] | None = None
        self.used = 0
        # Physical diagnostics only, including safe orphans; never admission quotas.
        # Fetched TXT evidence retains separate accounting.
        self.artifact_bytes = 0
        self.artifacts: dict[str, dict[str, ArtifactReceipt]] = {}
        self._artifact_stamps: dict[str, dict[str, FileStamp]] = {}
        self._committed: dict[str, Committed] = {}
        self._verified: dict[str, VerifiedFiles] = {}
        self._records: dict[str, HistoricalRecords] = {}
        self.records: Mapping[str, Sequence[JournalRecord]] = MappingProxyType(self._records)
        self.creations: dict[str, tuple[str, str]] = {}
        self._public = PublicViews(self)

    def public_session(self, sid: str) -> dict:
        return self._public.session(sid)

    def public_list(self, include_archived: bool, limit=25, cursor=None) -> dict:
        return self._public.listing(include_archived, limit, cursor)

    def public_runs(self, sid: str, limit=10, before=None) -> dict:
        return self._public.runs(sid, limit, before)

    def public_run(self, sid: str, rid: str, limit=20, before=None) -> dict:
        return self._public.run(sid, rid, limit, before)

    def public_results(self, sid: str, rid: str, limit=20, before=None) -> dict:
        return self._public.results(sid, rid, limit, before)

    def public_events(self, sid: str, after: int, limit=50, through=None) -> dict:
        return self._public.events(sid, after, limit, through)

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    @poisoned.setter
    def poisoned(self, value: bool):
        # Also covers direct failure assignments by AgentService. A failed store
        # instance cannot be revived by clearing a flag; startup uses a new store.
        with self._publication_lock:
            self._poisoned = self._poisoned or value

    def _healthy(self):
        if self.poisoned:
            raise AgentFault(
                "storage_unavailable", "Agent recording is unavailable; restart required.", 503
            )

    def _opened(self) -> int:
        """Caller holds the store lock: never let None become a cwd dir_fd."""
        self._healthy()
        if self.fd is None or self.lease is None:
            raise AgentFault("storage_unavailable", "Agent storage is not open.", 503)
        return self.fd

    def identity(self) -> tuple[int, int]:
        with self.lock:
            info = os.fstat(self._opened())
            return info.st_dev, info.st_ino

    def start(self):
        with self.lock:
            self._healthy()
            if self.fd is not None:
                return
            parent = None
            try:
                parent = open_directory(self.root.absolute().parent)
                try:
                    os.mkdir(self.root.name, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
                self.fd = os.open(
                    self.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
                os.fchmod(self.fd, 0o700)
                info = os.fstat(self.fd)
                self._root_identity = info.st_dev, info.st_ino
                self.lease = os.open(
                    ".lock",
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=self.fd,
                )
                self._regular(self.lease)
                os.fchmod(self.lease, 0o600)
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._recover()
            except BaseException:
                self.poisoned = True
                self._committed.clear()
                self._verified.clear()
                self._records.clear()
                self.creations.clear()
                self.artifacts.clear()
                self._artifact_stamps.clear()
                self.close()
                raise AgentFault(
                    "storage_unavailable", "Agent storage could not be opened safely.", 503
                ) from None
            finally:
                if parent is not None:
                    os.close(parent)

    def close(self):
        with self.lock:
            if self.lease is not None:
                os.close(self.lease)
                self.lease = None
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def _directory(self, sid: str) -> int:
        with self.lock:
            root_fd = self._opened()
            if not SESSION_ID.fullmatch(sid):
                raise AgentFault("invalid_session_id", "Invalid Agent session ID.", 422)
            fd = os.open(sid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                os.fchmod(fd, 0o700)
                return fd
            except BaseException:
                os.close(fd)
                raise

    @staticmethod
    def _artifact_id(artifact_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
            raise AgentFault("invalid_artifact_id", "Invalid Agent artifact ID.", 422)

    @staticmethod
    def _page_bounds(offset: int, limit: int):
        if (
            type(offset) is not int
            or type(limit) is not int
            or offset < 0
            or not 1 <= limit <= 16000
        ):
            raise AgentFault("invalid_artifact_page", "Invalid Agent artifact page.", 422)

    @staticmethod
    def _regular(fd: int):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("unsafe evidence file")
        return info

    @classmethod
    def _stamp(cls, fd: int) -> FileStamp:
        info = cls._regular(fd)
        return FileStamp(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @staticmethod
    def _record_bytes(fd: int, location: RecordLocation) -> bytes:
        parts = []
        length = 0
        while length < location.length:
            part = os.pread(fd, min(65536, location.length - length), location.offset + length)
            if not part:
                break
            parts.append(part)
            length += len(part)
        raw = b"".join(parts)
        if (
            len(raw) != location.length
            or not raw.endswith(b"\n")
            or hashlib.sha256(raw).hexdigest() != location.sha256
        ):
            raise ValueError("committed journal record changed")
        return raw

    def _verify_journal(
        self,
        fd: int,
        committed: Committed,
        verified: VerifiedFiles,
        *,
        diagnostic=False,
        force=False,
    ) -> FileStamp:
        stamp = self._stamp(fd)
        old = verified.journal
        end = committed.locations[-1].offset + committed.locations[-1].length
        if (stamp.device, stamp.inode) != (old.device, old.inode):
            raise ValueError("journal replaced")
        if stamp.size < end or (not diagnostic and stamp.size != end):
            raise ValueError("journal size changed")
        if force or stamp != old:
            for location in committed.locations:
                self._record_bytes(fd, location)
            if self._stamp(fd) != stamp:
                raise ValueError("journal changed during verification")
        return stamp

    def _evidence_failure(self):
        self.poisoned = True
        return AgentFault("storage_unavailable", "Agent evidence is unavailable.", 503)

    def _artifact_directory(self, directory: int, *, create=False) -> int:
        if create:
            try:
                os.mkdir("artifacts", mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
        fd = os.open("artifacts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            os.fchmod(fd, 0o700)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def write_artifact(
        self, sid: str, document: TextDocument, offset: int = 0, limit: int = 12000
    ) -> tuple[ArtifactPage, dict]:
        """Durable full UTF-8 evidence, before any journal reference is permitted.

        A failed or stopped operation may leave an unreferenced file. It is never
        exposed by the public reader and never causes recovery to replay a tool.
        The caller owns the source and closes it only after this worker settles.
        """
        with self.lock:
            self._opened()
            self.get(sid)
            self._page_bounds(offset, limit)
            text_length = document.text_length
            if offset > text_length:
                raise AgentFault("invalid_offset", "Offset is past the end of the artifact.", 422)
            directory = artifacts = fd = None
            try:
                directory = self._directory(sid)
                artifacts = self._artifact_directory(directory, create=True)
                for _ in range(10):
                    artifact_id = uuid4().hex
                    self._artifact_id(artifact_id)
                    try:
                        fd = os.open(
                            artifact_id + ".txt",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                            0o600,
                            dir_fd=artifacts,
                        )
                        break
                    except FileExistsError:
                        continue
                else:
                    raise OSError("artifact identity unavailable")
                self._regular(fd)
                os.fchmod(fd, 0o600)
                digest = hashlib.sha256()
                size = chars = 0
                selected = []
                for text in document.iter_text():
                    # Only the requested page is retained in memory, across arbitrary
                    # chunk boundaries. Encode/hash/write each bounded source chunk.
                    start, end = max(0, offset - chars), min(len(text), offset + limit - chars)
                    if start < end:
                        selected.append(text[start:end])
                    chars += len(text)
                    raw = text.encode("utf-8")
                    digest.update(raw)
                    size += len(raw)
                    remaining = memoryview(raw)
                    while remaining:
                        count = os.write(fd, remaining[:65536])
                        if count <= 0:
                            raise OSError("short evidence write")
                        self.artifact_bytes += count
                        remaining = remaining[count:]
                if chars != text_length:
                    raise ValueError("evidence character count changed")
                os.fsync(fd)
                os.fsync(artifacts)
                os.fsync(directory)
                receipt = ArtifactReceipt(
                    artifact_id=artifact_id,
                    sha256=digest.hexdigest(),
                    bytes=size,
                    total_chars=chars,
                )
                content = "".join(selected)
                return self._page(receipt, content, offset), receipt.model_dump()
            except OSError, ValueError:
                raise self._evidence_failure() from None
            finally:
                for opened in (fd, artifacts, directory):
                    if opened is not None:
                        os.close(opened)

    @staticmethod
    def _page(receipt: ArtifactReceipt, content: str, offset: int) -> ArtifactPage:
        end = offset + len(content)
        return ArtifactPage(
            artifact_id=receipt.artifact_id,
            content=content,
            offset=offset,
            returned_chars=len(content),
            total_chars=receipt.total_chars,
            next_offset=end if end < receipt.total_chars else None,
        )

    def _read_artifact(
        self, sid: str, receipt: ArtifactReceipt, offset: int, limit: int
    ) -> ArtifactPage:
        directory = artifacts = fd = None
        try:
            directory = self._directory(sid)
            artifacts = self._artifact_directory(directory)
            fd = os.open(
                receipt.artifact_id + ".txt",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=artifacts,
            )
            stamp = self._stamp(fd)
            if stamp.size != receipt.bytes:
                raise ValueError("evidence size changed")
            digest = hashlib.sha256()
            decoder = codecs.getincrementaldecoder("utf-8")()
            chars = size = 0
            selected = []
            while raw := os.read(fd, 65536):
                size += len(raw)
                digest.update(raw)
                text = decoder.decode(raw)
                start, end = max(0, offset - chars), min(len(text), offset + limit - chars)
                if start < end:
                    selected.append(text[start:end])
                chars += len(text)
            decoder.decode(b"", final=True)
            if (
                size != receipt.bytes
                or chars != receipt.total_chars
                or digest.hexdigest() != receipt.sha256
                or self._stamp(fd) != stamp
            ):
                raise ValueError("evidence changed")
            self._artifact_stamps.setdefault(sid, {})[receipt.artifact_id] = stamp
            return self._page(receipt, "".join(selected), offset)
        except OSError, ValueError:
            raise self._evidence_failure() from None
        finally:
            for opened in (fd, artifacts, directory):
                if opened is not None:
                    os.close(opened)

    @staticmethod
    def _index_artifacts(record: JournalRecord, current: dict) -> dict[str, ArtifactReceipt]:
        index = dict(current)
        proof = record.audit.get("artifact")
        if proof is not None:
            receipt = ArtifactReceipt.model_validate(proof)
            if record.event.type != "tool.result" or receipt.artifact_id in index:
                raise ValueError("invalid evidence commit")
            matches = [
                result
                for run in record.event.session.runs
                if run.id == record.event.run_id
                for result in run.results
                if result.step_id == record.audit.get("step_id")
                and result.status == "completed"
                and result.fetch is not None
                and result.fetch.artifact_id == receipt.artifact_id
            ]
            if len(matches) != 1:
                raise ValueError("unbound evidence commit")
            index[receipt.artifact_id] = receipt
        for run in record.event.session.runs:
            for result in run.results:
                if result.fetch is not None:
                    receipt = index.get(result.fetch.artifact_id)
                    if (
                        result.status != "completed"
                        or receipt is None
                        or receipt.total_chars != result.fetch.total_chars
                    ):
                        raise ValueError("uncommitted evidence reference")
        return index

    def _verify_artifacts(self, sid: str, index: dict, snapshot: AgentSession):
        unchecked = set(index)
        for run in snapshot.runs:
            for result in run.results:
                if result.fetch is None:
                    continue
                metadata = result.fetch
                receipt = index[metadata.artifact_id]
                page = self._read_artifact(
                    sid, receipt, metadata.offset, max(1, metadata.returned_chars)
                )
                if (
                    page.content != result.content
                    or page.returned_chars != metadata.returned_chars
                    or page.next_offset != metadata.next_offset
                    or metadata.offset > receipt.total_chars
                    or metadata.returned_chars > 16000
                ):
                    raise self._evidence_failure()
                unchecked.discard(metadata.artifact_id)
        for artifact_id in unchecked:
            self._read_artifact(sid, index[artifact_id], 0, 1)

    def read_artifact(
        self, sid: str, artifact_id: str, offset: int = 0, limit: int = 12000
    ) -> ArtifactPage:
        with self.lock:
            self._opened()
            self._artifact_id(artifact_id)
            self._page_bounds(offset, limit)
            self.get(sid)
            receipt = self.artifacts.get(sid, {}).get(artifact_id)
            if receipt is None:
                raise AgentFault("artifact_not_found", "Agent artifact not found.", 404)
            if offset > receipt.total_chars:
                raise AgentFault("invalid_offset", "Offset is past the end of the artifact.", 422)
            return self._read_artifact(sid, receipt, offset, limit)

    def _recover_artifact_files(self, directory: int):
        artifacts = self._artifact_directory(directory)
        try:
            for name in os.listdir(artifacts):
                if not ARTIFACT_FILE.fullmatch(name):
                    raise ValueError("unexpected artifact file")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=artifacts)
                try:
                    self.artifact_bytes += self._regular(fd).st_size
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
        finally:
            os.close(artifacts)

    @staticmethod
    def _payload_directory(directory: int, *, create=False) -> int:
        if create:
            try:
                os.mkdir("payloads", mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
        fd = os.open("payloads", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            os.fchmod(fd, 0o700)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _read_payload(
        self,
        directory: int,
        ref: PayloadRef,
        *,
        verified: dict[str, FileStamp] | None = None,
        materialize: bool = True,
    ) -> object:
        payloads = fd = None
        try:
            payloads = self._payload_directory(directory)
            fd = os.open(
                ref.id + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=payloads
            )
            stamp = self._stamp(fd)
            if stamp.size != ref.bytes:
                raise ValueError("payload size changed")
            raw = bytearray() if materialize else None
            size = 0
            digest = hashlib.sha256()
            while block := os.read(fd, min(65536, ref.bytes + 1 - size)):
                size += len(block)
                if raw is not None:
                    raw.extend(block)
                digest.update(block)
                if size > ref.bytes:
                    raise ValueError("payload grew")
            if size != ref.bytes or digest.hexdigest() != ref.sha256:
                raise ValueError("payload changed")
            value = json.loads(raw) if raw is not None else None
            if self._stamp(fd) != stamp:
                raise ValueError("payload changed during verification")
            if verified is not None:
                verified[ref.id] = stamp
            return value
        finally:
            for opened in (fd, payloads):
                if opened is not None:
                    os.close(opened)

    def _verify_payloads(
        self, directory: int, refs: Mapping[str, PayloadRef], known: Mapping[str, FileStamp]
    ) -> dict[str, FileStamp]:
        """Check every committed file, hash only new or metadata-changed bodies.

        Directory timestamps cannot detect in-place damage. Each file is opened
        no-follow and fstat'ed, including superseded plans and audit-only refs.
        Stable local single-writer metadata permits the verified latest baseline.
        """
        if not refs:
            return {}
        folder = self._payload_directory(directory)
        stamps = {}
        try:
            for key, ref in refs.items():
                fd = os.open(
                    ref.id + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder
                )
                try:
                    stamp = self._stamp(fd)
                    if stamp.size != ref.bytes:
                        raise ValueError("payload size changed")
                finally:
                    os.close(fd)
                if known.get(key) != stamp:
                    self._read_payload(directory, ref, verified=stamps, materialize=False)
                else:
                    stamps[key] = stamp
        finally:
            os.close(folder)
        return stamps

    def _verify_current(self, sid: str, directory: int) -> Committed:
        committed, verified = self._committed[sid], self._verified[sid]
        fd = os.open("events.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            journal = self._verify_journal(fd, committed, verified)
            payloads = self._verify_payloads(directory, committed.payloads, verified.payloads)
            self._verified[sid] = VerifiedFiles(journal, MappingProxyType(payloads))
            return committed
        finally:
            os.close(fd)

    def _verify_public_artifacts(self, sid: str, directory: int):
        """Keep public headers body-free for stable, already verified TXT evidence."""
        receipts = self.artifacts.get(sid, {})
        if not receipts:
            return
        folder = self._artifact_directory(directory)
        try:
            for aid, receipt in receipts.items():
                fd = os.open(
                    aid + ".txt", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder
                )
                try:
                    stamp = self._stamp(fd)
                    if stamp.size != receipt.bytes:
                        raise ValueError("evidence size changed")
                finally:
                    os.close(fd)
                if self._artifact_stamps.get(sid, {}).get(aid) != stamp:
                    self._read_artifact(sid, receipt, 0, 1)
        finally:
            os.close(folder)

    def _recover_payload_files(self, directory: int):
        payloads = self._payload_directory(directory)
        try:
            for name in os.listdir(payloads):
                if not PAYLOAD_FILE.fullmatch(name):
                    raise ValueError("unexpected payload file")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=payloads)
                try:
                    self.used += self._regular(fd).st_size
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
        finally:
            os.close(payloads)

    def _recover_files(self, directory: int):
        for name in os.listdir(directory):
            if name == "artifacts":
                self._recover_artifact_files(directory)
            elif name == "payloads":
                self._recover_payload_files(directory)
            elif name != "events.jsonl":
                raise ValueError("unexpected storage file")

    def _next_committed(
        self,
        record: JournalRecord,
        state: SessionState,
        location: RecordLocation,
        payloads: Mapping[str, PayloadRef],
    ) -> Committed:
        event = record.event
        old = self._committed.get(event.session_id)
        locations = old.locations if old else ()
        if (
            event.sequence != len(locations) + 1
            or event.session.id != event.session_id
            or event.session.sequence != event.sequence
        ):
            raise ValueError("invalid journal sequence")
        claims = dict(old.claims) if old else {}
        if record.claim is not None:
            key, digest = record.claim.request_id, record.claim.digest
            if key in claims and claims[key] != digest:
                raise ValueError("conflicting committed claim")
            claims[key] = digest
        attempts = dict(old.attempts) if old else {}
        if event.type == "planner.attempt" and event.run_id is not None:
            attempts[event.run_id] = attempts.get(event.run_id, 0) + 1
        operations = old.operations if old else frozenset()
        if event.run_id is not None and event.operation_id is not None:
            operations = operations | {(event.run_id, event.operation_id)}
        return Committed(
            state,
            record.event.session.model_copy(deep=True),
            (*locations, location),
            MappingProxyType(claims),
            MappingProxyType(attempts),
            frozenset(operations),
            MappingProxyType(dict(payloads)),
            index_record(record, state, old.views if old else None, old.baseline if old else None),
        )

    def _publish(self, sid: str, committed: Committed, artifacts: dict, verified: VerifiedFiles):
        # No I/O under this barrier. A concurrent diagnostic reader can report
        # failure promptly while a writer fsyncs; that writer must then fail here
        # without publishing an uncertain claim, projection, or trusted stamp.
        with self._publication_lock:
            self._healthy()
            self._committed[sid] = committed
            self._verified[sid] = verified
            self._records.setdefault(sid, HistoricalRecords(self, sid))
            self.artifacts[sid] = artifacts

    def _recover(self):
        root_fd = self._opened()
        self.used = self.artifact_bytes = 0
        self._committed.clear()
        self._verified.clear()
        self._records.clear()
        self.creations.clear()
        self.artifacts.clear()
        self._artifact_stamps.clear()
        entries = os.listdir(root_fd)
        for sid in sorted(entries):
            if sid == ".lock":
                continue
            if sid.startswith(".orphan-"):
                if not SESSION_ID.fullmatch(sid.removeprefix(".orphan-")):
                    raise ValueError("invalid orphan directory")
                orphan = os.open(sid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                try:
                    os.fchmod(orphan, 0o700)
                    self._recover_files(orphan)
                    try:
                        fd = os.open(
                            "events.jsonl",
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=orphan,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        try:
                            self.used += self._regular(fd).st_size
                            os.fchmod(fd, 0o600)
                        finally:
                            os.close(fd)
                finally:
                    os.close(orphan)
                continue
            directory = self._directory(sid)
            try:
                self._recover_files(directory)
                try:
                    journal = os.open(
                        "events.jsonl", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
                except FileNotFoundError:
                    journal = None
                state = None
                artifact_index = {}
                seen_refs = {}
                verified_payloads = {}
                if journal is not None:
                    try:
                        self._regular(journal)
                        os.fchmod(journal, 0o600)
                        # Locate complete records using bounded reads. Buffer just
                        # one complete record for decoding, never a partial tail or
                        # the whole journal. Complete records have no size ceiling.
                        with os.fdopen(os.dup(journal), "rb") as stream:
                            offset = 0
                            length = 0
                            digest = hashlib.sha256()
                            while part := stream.readline(65536):
                                length += len(part)
                                digest.update(part)
                                if not part.endswith(b"\n"):
                                    continue
                                location = RecordLocation(offset, length, digest.hexdigest())
                                line = self._record_bytes(journal, location)

                                def read(
                                    ref,
                                    directory=directory,
                                    seen_refs=seen_refs,
                                    verified_payloads=verified_payloads,
                                ):
                                    # Verify superseded plans and audit-only refs.
                                    # Only the latest session baseline is retained.
                                    value = self._read_payload(
                                        directory, ref, verified=verified_payloads
                                    )
                                    prior = seen_refs.get(ref.id)
                                    if prior is None:
                                        seen_refs[ref.id] = ref
                                    elif prior != ref:
                                        raise ValueError("conflicting payload identity")
                                    return value

                                record, state = decode(line, state, read)
                                event = record.event
                                if event.session_id != sid:
                                    raise ValueError("invalid journal sequence")
                                if offset == 0:
                                    if event.type != "session.created" or record.claim is None:
                                        raise ValueError("missing creation claim")
                                    if record.claim.request_id in self.creations:
                                        raise ValueError("duplicate creation")
                                    self.creations[record.claim.request_id] = (
                                        record.claim.digest,
                                        sid,
                                    )
                                artifact_index = self._index_artifacts(record, artifact_index)
                                committed = self._next_committed(
                                    record,
                                    state,
                                    location,
                                    seen_refs,
                                )
                                self._publish(
                                    sid,
                                    committed,
                                    artifact_index,
                                    VerifiedFiles(
                                        self._stamp(journal),
                                        MappingProxyType(dict(verified_payloads)),
                                    ),
                                )
                                offset += len(line)
                                self.used += len(line)
                                length = 0
                                digest = hashlib.sha256()
                            if length:
                                os.ftruncate(journal, offset)
                                os.fsync(journal)
                                os.fsync(directory)
                        if state is not None:
                            self._verified[sid] = VerifiedFiles(
                                self._stamp(journal), MappingProxyType(dict(verified_payloads))
                            )
                    finally:
                        os.close(journal)
                if state is None:
                    os.rename(sid, ".orphan-" + sid, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                    os.fsync(root_fd)
                    continue
            finally:
                os.close(directory)
        for sid in list(self.records):
            snapshot = self.get(sid)
            changed = False
            for run in snapshot.runs:
                if run.operation_id is not None or run.status in {
                    "planning",
                    "approved",
                    "executing",
                    "cancelling",
                }:
                    run.status = "interrupted"
                    run.operation_id = None
                    run.error = AgentError(
                        code="outcome_unknown",
                        message="Interrupted during restart; work was not replayed.",
                    )
                    run.updated_at = self.clock()
                    for result in run.results:
                        if result.status == "running":
                            result.status, result.error = "failed", run.error
                    changed = True
            if changed or snapshot.archive_pending:
                if snapshot.archive_pending:
                    snapshot.archived, snapshot.archive_pending = True, False
                self.append(
                    snapshot,
                    "run.interrupted",
                    "recovery",
                    audit={"outcome": "unknown"},
                )

    def get(self, sid: str) -> AgentSession:
        with self.lock:
            self._opened()
            if not SESSION_ID.fullmatch(sid):
                raise AgentFault("invalid_session_id", "Invalid Agent session ID.", 422)
            if sid not in self.records:
                raise AgentFault("session_not_found", "Agent session not found.", 404)
            directory = None
            try:
                directory = self._directory(sid)
                snapshot = self._verify_current(sid, directory).baseline.model_copy(deep=True)
            except OSError, ValueError, TypeError, KeyError:
                raise self._recording_failure() from None
            finally:
                if directory is not None:
                    os.close(directory)
            self._verify_artifacts(sid, self.artifacts.get(sid, {}), snapshot)
            return snapshot

    def _history(self, sid: str, stop: int, *, start=0) -> Iterator[JournalRecord]:
        # Capture immutable prefix metadata and independent descriptors under the
        # lock. The generator owns those fds, but NEVER a store lock across yield.
        root = directory = fd = None
        try:
            with self.lock:
                committed = self._committed[sid]
                verified = self._verified[sid]
                if not SESSION_ID.fullmatch(sid):
                    raise ValueError("invalid historical identity")
                if self.fd is not None and self.lease is not None:
                    root = os.dup(self.fd)
                else:
                    root = open_directory(self.root)
                    info = os.fstat(root)
                    if (info.st_dev, info.st_ino) != self._root_identity:
                        raise ValueError("closed storage root replaced")
                directory = os.open(sid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                fd = os.open(
                    "events.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                stamp = self._verify_journal(
                    fd, committed, verified, diagnostic=self.poisoned or self.fd is None
                )
                payloads = self._verify_payloads(directory, committed.payloads, verified.payloads)
                self._verified[sid] = VerifiedFiles(stamp, MappingProxyType(payloads))
                locations = committed.locations[:stop]
            del committed, verified  # paused readers need only the compact prefix
            state = None
            for index, location in enumerate(locations):
                raw = self._record_bytes(fd, location)
                record, state = decode(raw, state, lambda ref: self._read_payload(directory, ref))
                if (
                    record.event.session_id != sid
                    or record.event.session.id != sid
                    or record.event.sequence != index + 1
                    or record.event.session.sequence != index + 1
                ):
                    raise ValueError("historical sequence changed")
                if index >= start:
                    yield record.model_copy(deep=True)
        except OSError, ValueError, TypeError, KeyError:
            raise self._recording_failure() from None
        finally:
            for opened in (fd, directory, root):
                if opened is not None:
                    os.close(opened)

    def attempt_count(self, sid: str, rid: str) -> int:
        """Committed planner attempts; no history replay, I/O, or store lock."""
        committed = self._committed.get(sid)
        return committed.attempts.get(rid, 0) if committed else 0

    def claim_digest(self, sid: str, request_id: str) -> str | None:
        """Lock-free immutable committed view, safe for the early Stop signal.

        This is advisory. claim() rechecks authoritatively under the store lock.
        A writer never mutates a published index while holding that lock/fsyncing.
        """
        committed = self._committed.get(sid)
        return committed.claims.get(request_id) if committed else None

    def operation_known(self, sid: str, rid: str, oid: str) -> bool:
        """Whether a committed event bound this operation to this run/session."""
        committed = self._committed.get(sid)
        return committed is not None and (rid, oid) in committed.operations

    def claim(self, sid: str, request_id: str, digest: str) -> bool:
        with self.lock:
            self._opened()
            prior = self._committed[sid].claims.get(request_id)
            if prior is not None:
                if prior != digest:
                    raise AgentFault(
                        "request_conflict", "Request ID was already used with different content."
                    )
                return False
            return True

    def create(self, body: CreateSession) -> AgentSession:
        with self.lock:
            root_fd = self._opened()
            digest = canonical_digest(body.model_dump())
            if found := self.creations.get(body.request_id):
                if found[0] != digest:
                    raise AgentFault(
                        "request_conflict", "Creation request ID has different content."
                    )
                return self.get(found[1])
            now = self.clock()
            for _ in range(10):
                sid = self.id_factory()
                if not SESSION_ID.fullmatch(sid):
                    raise AgentFault("storage_unavailable", "Invalid server session identity.", 503)
                if sid in self.records:
                    continue
                snapshot = AgentSession(
                    id=sid,
                    title="New Agent session",
                    model=body.model,
                    reasoning_effort=body.reasoning_effort,
                    created_at=now,
                    updated_at=now,
                )
                record = self._record(
                    snapshot,
                    "session.created",
                    body.request_id,
                    claim=Claim(request_id=body.request_id, digest=digest),
                )
                raw, state, payloads = self._encode(record)
                try:
                    os.mkdir(sid, mode=0o700, dir_fd=root_fd)
                    os.fsync(root_fd)
                    break
                except FileExistsError:
                    continue
                except OSError:
                    raise self._recording_failure() from None
            else:
                raise AgentFault(
                    "storage_unavailable", "Could not allocate a session identity.", 503
                )
            # A write/fsync failure leaves the folder for startup replay or
            # quarantine. Never delete a possibly committed creation record.
            self._write(record, raw, state, payloads)
            self.creations[body.request_id] = (digest, sid)
            return self.get(sid)

    def append(
        self,
        snapshot: AgentSession,
        kind: str,
        request_id: str,
        *,
        run_id: str | None = None,
        operation_id: str | None = None,
        claim: Claim | None = None,
        audit: dict | None = None,
    ) -> AgentEvent:
        with self.lock:
            self._opened()
            record = self._record(
                snapshot,
                kind,
                request_id,
                run_id=run_id,
                operation_id=operation_id,
                claim=claim,
                audit=audit,
            )
            raw, state, payloads = self._encode(record)
            self._write(record, raw, state, payloads)
            return record.event.model_copy(deep=True)

    def _record(
        self, snapshot, kind, request_id, *, run_id=None, operation_id=None, claim=None, audit=None
    ) -> JournalRecord:
        snapshot = snapshot.model_copy(deep=True)
        if snapshot.sequence != len(self.records.get(snapshot.id, [])):
            raise AgentFault("session_conflict", "The session changed before recording.")
        snapshot.sequence += 1
        snapshot.updated_at = self.clock()
        if run_id:
            next(run for run in snapshot.runs if run.id == run_id).updated_at = snapshot.updated_at
        return JournalRecord(
            event=AgentEvent(
                sequence=snapshot.sequence,
                time=snapshot.updated_at,
                session_id=snapshot.id,
                request_id=request_id,
                run_id=run_id,
                operation_id=operation_id,
                type=kind,
                session=snapshot,
            ),
            claim=claim,
            audit=audit or {},
        )

    def _recording_failure(self):
        self.poisoned = True
        return AgentFault(
            "storage_unavailable", "Agent recording failed; no further work is allowed.", 503
        )

    def _encode(self, record: JournalRecord):
        previous = self._committed.get(record.event.session_id)
        try:
            return encode(
                record,
                previous.projection if previous else None,
                previous.baseline if previous else None,
            )
        except OSError, ValueError, TypeError:
            raise self._recording_failure() from None

    def _write_bytes(self, fd: int, raw: bytes):
        remaining = memoryview(raw)
        while remaining:
            count = os.write(fd, remaining[:65536])
            if count <= 0:
                raise OSError("short write")
            self.used += count
            remaining = remaining[count:]

    def _write_payloads(self, directory: int, payloads: list[tuple[PayloadRef, bytes]]):
        if not payloads:
            return
        folder = self._payload_directory(directory, create=True)
        try:
            for ref, raw in payloads:
                fd = os.open(
                    ref.id + ".json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=folder,
                )
                try:
                    self._regular(fd)
                    os.fchmod(fd, 0o600)
                    self._write_bytes(fd, raw)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.fsync(folder)
            os.fsync(directory)
        finally:
            os.close(folder)

    def _write(
        self, record: JournalRecord, raw: bytes, state: SessionState | None = None, payloads=()
    ):
        # Caller holds the store lock; only publish after all required fsyncs.
        snapshot = record.event.session
        with self.lock:
            self._opened()
            directory = None
            fd = None
            try:
                directory = self._directory(snapshot.id)
                artifact_index = self._index_artifacts(record, self.artifacts.get(snapshot.id, {}))
                self._verify_artifacts(snapshot.id, artifact_index, snapshot)
                if state is None:
                    raw, state, payloads = self._encode(record)
                previous = self._committed.get(snapshot.id)
                flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
                if previous is None:
                    flags |= os.O_CREAT | os.O_EXCL
                fd = os.open(
                    "events.jsonl",
                    flags,
                    0o600,
                    dir_fd=directory,
                )
                opened = self._stamp(fd)
                end = (
                    previous.locations[-1].offset + previous.locations[-1].length if previous else 0
                )
                if opened.size != end:
                    raise ValueError("journal size changed")
                known = self._verified.get(snapshot.id)
                if previous is not None:
                    known = VerifiedFiles(self._verify_journal(fd, previous, known), known.payloads)
                self._write_payloads(directory, payloads)
                refs = dict(previous.payloads) if previous else {}
                for ref, _ in payloads:
                    if ref.id in refs:
                        raise ValueError("payload identity reused")
                    refs[ref.id] = ref
                stamps = self._verify_payloads(directory, refs, known.payloads if known else {})
                if previous is not None:
                    self._verify_journal(fd, previous, known)
                committed = self._next_committed(
                    record,
                    state,
                    RecordLocation(end, len(raw), hashlib.sha256(raw).hexdigest()),
                    refs,
                )
                os.fchmod(fd, 0o600)
                self._write_bytes(fd, raw)
                os.fsync(fd)
                os.fsync(directory)
                self._healthy()
                # Durability I/O may also coincide with payload damage. Stable
                # files need only metadata checks; changed bodies must verify.
                stamps = self._verify_payloads(directory, refs, stamps)
                # fsync can coincide with detectable corruption of *old* bytes.
                # Never bless its resulting stat alone: bind the complete prefix
                # and new entry to their known hashes under stable metadata.
                verified = VerifiedFiles(opened, MappingProxyType(stamps))
                verified = VerifiedFiles(
                    self._verify_journal(fd, committed, verified, force=True), verified.payloads
                )
            except OSError, ValueError, TypeError, KeyError:
                raise self._recording_failure() from None
            finally:
                if fd is not None:
                    os.close(fd)
                if directory is not None:
                    os.close(directory)
            self._publish(snapshot.id, committed, artifact_index, verified)

    def events(self, sid: str, after: int, limit: int = 50) -> list[AgentEvent]:
        with self.lock:
            self.get(sid)
            stop = min(len(self.records[sid]), after + min(limit, 50))
            return [r.event for r in self._history(sid, stop, start=after)]

    def listing(self, include_archived: bool) -> list[AgentSession]:
        with self.lock:
            return sorted(
                [
                    self.get(sid)
                    for sid in self.records
                    if include_archived or not self.get(sid).archived
                ],
                key=lambda s: s.updated_at,
                reverse=True,
            )
