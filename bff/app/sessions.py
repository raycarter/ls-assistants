"""Small single-writer journal. Full bounded snapshots per event are the projection.

Local POSIX filesystem only. A process lock excludes other BFF workers. No network
filesystem/power-loss guarantees; fsync commits precede observable publication.
"""

import codecs
import fcntl
import hashlib
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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

SESSION_ID = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[a-z0-9]{4}$")
ARTIFACT_FILE = re.compile(r"^[a-f0-9]{32}\.txt$")
SETTLEMENT_MARGIN = 5000
# Nine execution events at most, plus Stop, archive settlement and crash recovery.
OPERATION_EVENTS = 12


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def session_id() -> str:
    suffix = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ-") + suffix


@dataclass(frozen=True)
class StoreLimits:
    sessions: int = 100
    events: int = 500
    record_bytes: int = 512_000
    disk_bytes: int = 64_000_000


class ArtifactReceipt(Strict):
    artifact_id: ArtifactId
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)
    total_chars: int = Field(ge=0)


class TextDocument(Protocol):
    @property
    def text_length(self) -> int: ...

    def iter_text(self) -> Iterator[str]: ...


class SessionStore:
    def __init__(
        self,
        root: Path | None = None,
        *,
        clock: Callable[[], str] = utc_now,
        id_factory: Callable[[], str] = session_id,
        limits: StoreLimits | None = None,
    ):
        self.root = root or Path(__file__).absolute().parents[1] / "sessions"
        self.clock, self.id_factory, self.limits = clock, id_factory, limits or StoreLimits()
        self.lock = threading.RLock()
        self.fd: int | None = None
        self.lease: int | None = None
        self.poisoned = False
        self.used = 0
        # Existing journal budgets remain journal budgets. Evidence has no body quota.
        self.artifact_bytes = 0
        self.artifacts: dict[str, dict[str, ArtifactReceipt]] = {}
        self.records: dict[str, list[JournalRecord]] = {}
        self.creations: dict[str, tuple[str, str]] = {}
        self.reservations: dict[str, tuple[str, int]] = {}

    @property
    def recovery_disk_limit(self) -> int:
        return self.limits.disk_bytes + self.limits.sessions * (
            self.limits.record_bytes + SETTLEMENT_MARGIN
        )

    def reserve(self, snapshot: AgentSession, operation_id: str, *, executing: bool):
        """Conservatively reserve the whole finite slice, including stop/recovery.

        Normal mutations cannot consume these slots/bytes. Planning failure keeps
        the previous snapshot; execution can add at most three bounded results.
        """
        with self.lock:
            self._opened()
            # Three 24,000-character results at six JSON bytes/character,
            # plus bounded result metadata; never assume ASCII source text.
            growth = SETTLEMENT_MARGIN + (450_000 if executing else 0)
            if len(snapshot.model_dump_json().encode()) + growth > self.limits.record_bytes:
                raise AgentFault(
                    "storage_limit", "Insufficient bounded settlement record space.", 507
                )
            events = sum(n for sid, n in self.reservations.values() if sid == snapshot.id)
            reserved_bytes = (
                sum(n for _, n in self.reservations.values()) * self.limits.record_bytes
            )
            if (
                len(self.records[snapshot.id]) + events + OPERATION_EVENTS > self.limits.events
                or self.used + reserved_bytes + OPERATION_EVENTS * self.limits.record_bytes
                > self.limits.disk_bytes
            ):
                raise AgentFault(
                    "storage_limit", "Insufficient capacity for a complete Agent operation.", 507
                )
            self.reservations[operation_id] = (snapshot.id, OPERATION_EVENTS)

    def release(self, operation_id: str):
        with self.lock:
            self.reservations.pop(operation_id, None)

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
            parent = open_directory(self.root.absolute().parent)
            try:
                try:
                    os.mkdir(self.root.name, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
                self.fd = os.open(
                    self.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
                os.fchmod(self.fd, 0o700)
                self.lease = os.open(
                    ".lock",
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=self.fd,
                )
                if not stat.S_ISREG(os.fstat(self.lease).st_mode):
                    raise ValueError("unsafe lock file")
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._recover()
            except BaseException:
                self.poisoned = True
                self.close()
                raise AgentFault(
                    "storage_unavailable", "Agent storage could not be opened safely.", 503
                ) from None
            finally:
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
            return os.open(sid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)

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
                        count = os.write(fd, remaining)
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
            info = self._regular(fd)
            if info.st_size != receipt.bytes:
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
            ):
                raise ValueError("evidence changed")
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

    def _recover(self):
        root_fd = self._opened()
        entries = os.listdir(root_fd)
        if len(entries) > self.limits.sessions * 2 + 1:
            raise ValueError("session directory limit")
        for sid in sorted(entries):
            if sid == ".lock":
                continue
            if sid.startswith(".orphan-"):
                if not SESSION_ID.fullmatch(sid.removeprefix(".orphan-")):
                    raise ValueError("invalid orphan directory")
                orphan = os.open(sid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                try:
                    for name in os.listdir(orphan):
                        info = os.stat(name, dir_fd=orphan, follow_symlinks=False)
                        if name == "artifacts" and stat.S_ISDIR(info.st_mode):
                            self._recover_artifact_files(orphan)
                        elif name != "events.jsonl" or not stat.S_ISREG(info.st_mode):
                            raise ValueError("unsafe orphan contents")
                        else:
                            self.used += info.st_size
                finally:
                    os.close(orphan)
                continue
            directory = self._directory(sid)
            try:
                names = os.listdir(directory)
                if any(name not in {"events.jsonl", "artifacts"} for name in names):
                    raise ValueError("unexpected storage file")
                if "artifacts" in names:
                    self._recover_artifact_files(directory)
                try:
                    journal = os.open(
                        "events.jsonl", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
                except FileNotFoundError:
                    journal = None
                records = []
                artifact_index = {}
                if journal is not None:
                    try:
                        info = os.fstat(journal)
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_size > self.recovery_disk_limit
                        ):
                            raise ValueError("journal limit")
                        raw = bytearray()
                        while block := os.read(journal, 65536):
                            raw.extend(block)
                            if self.used + len(raw) > self.recovery_disk_limit:
                                raise ValueError("journal limit")
                        # Only an unterminated final record is discarded. Complete bad JSON
                        # (including a bad middle record) poisons startup, never skipped.
                        end = raw.rfind(b"\n") + 1
                        if end != len(raw):
                            os.ftruncate(journal, end)
                            os.fsync(journal)
                        self.used += end
                        for line in raw[:end].splitlines():
                            record = JournalRecord.model_validate_json(line)
                            event = record.event
                            emergency = (
                                event.type == "run.interrupted"
                                and event.request_id == "recovery"
                                and record.audit.get("capacity_recovery") is True
                            )
                            if len(line) > self.limits.record_bytes + (
                                SETTLEMENT_MARGIN if emergency else 0
                            ):
                                raise ValueError("record limit")
                            if (
                                event.sequence != len(records) + 1
                                or event.session_id != sid
                                or event.session.id != sid
                                or event.session.sequence != event.sequence
                                or len(records) >= self.limits.events + int(emergency)
                            ):
                                raise ValueError("invalid journal sequence")
                            artifact_index = self._index_artifacts(record, artifact_index)
                            records.append(record)
                    finally:
                        os.close(journal)
                if not records:
                    os.rename(sid, ".orphan-" + sid, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                    os.fsync(root_fd)
                    continue
                first = records[0]
                if first.event.type != "session.created" or first.claim is None:
                    raise ValueError("missing creation claim")
                if first.claim.request_id in self.creations:
                    raise ValueError("duplicate creation")
                self.creations[first.claim.request_id] = (first.claim.digest, sid)
                self.records[sid] = records
                self.artifacts[sid] = artifact_index
            finally:
                os.close(directory)
        if len(self.records) > self.limits.sessions or self.used > self.recovery_disk_limit:
            raise ValueError("retained limit")
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
                    recovery=True,
                )

    def get(self, sid: str) -> AgentSession:
        with self.lock:
            self._healthy()
            if not SESSION_ID.fullmatch(sid):
                raise AgentFault("invalid_session_id", "Invalid Agent session ID.", 422)
            if sid not in self.records:
                raise AgentFault("session_not_found", "Agent session not found.", 404)
            snapshot = self.records[sid][-1].event.session
            self._verify_artifacts(sid, self.artifacts.get(sid, {}), snapshot)
            return snapshot.model_copy(deep=True)

    def claim(self, sid: str, request_id: str, digest: str) -> bool:
        with self.lock:
            for record in self.records[sid]:
                if record.claim is not None and record.claim.request_id == request_id:
                    if record.claim.digest != digest:
                        raise AgentFault(
                            "request_conflict",
                            "Request ID was already used with different content.",
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
            if len(self.records) >= self.limits.sessions:
                raise AgentFault("storage_limit", "Agent session limit reached.", 507)
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
                raw = (record.model_dump_json() + "\n").encode()
                # Check before mkdir: capacity rejection must leave neither a
                # directory nor a visible empty cache entry behind.
                self._capacity(record, raw)
                try:
                    os.mkdir(sid, mode=0o700, dir_fd=root_fd)
                    os.fsync(root_fd)
                    break
                except FileExistsError:
                    continue
            else:
                raise AgentFault(
                    "storage_unavailable", "Could not allocate a session identity.", 503
                )
            # A write/fsync failure leaves the folder for startup replay or
            # quarantine. Never delete a possibly committed creation record.
            self._write(record, raw)
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
        reservation: str | None = None,
        recovery: bool = False,
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
            raw = (record.model_dump_json() + "\n").encode()
            try:
                self._capacity(record, raw, reservation)
            except AgentFault:
                if not recovery:
                    raise
                # Compatibility escape hatch for an already-full legacy journal:
                # one bounded recovery-only control record beyond normal caps.
                record.audit["capacity_recovery"] = True
                raw = (record.model_dump_json() + "\n").encode()
                if (
                    record.event.sequence > self.limits.events + 1
                    or len(raw) > self.limits.record_bytes + SETTLEMENT_MARGIN
                    or self.used + len(raw) > self.recovery_disk_limit
                ):
                    raise
            self._write(record, raw)
            if reservation is not None:
                sid, remaining = self.reservations[reservation]
                self.reservations[reservation] = (sid, remaining - 1)
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

    def _capacity(self, record, raw: bytes, reservation: str | None = None):
        sid = record.event.session_id
        reserved = self.reservations.get(reservation)
        if reservation is not None and (reserved is None or reserved[0] != sid or reserved[1] < 1):
            raise AgentFault("storage_limit", "Agent operation reservation is exhausted.", 507)
        others = [value for key, value in self.reservations.items() if key != reservation]
        held_events = sum(n for session, n in others if session == sid)
        held_bytes = sum(n for _, n in others) * self.limits.record_bytes
        if (
            len(raw) > self.limits.record_bytes
            or record.event.sequence + held_events > self.limits.events
            or self.used + len(raw) + held_bytes > self.limits.disk_bytes
        ):
            raise AgentFault("storage_limit", "Agent recording limit reached.", 507)

    def _write(self, record: JournalRecord, raw: bytes):
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
                fd = os.open(
                    "events.jsonl",
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=directory,
                )
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("not regular")
                os.fchmod(fd, 0o600)
                remaining = memoryview(raw)
                while remaining:
                    count = os.write(fd, remaining)
                    if count <= 0:
                        raise OSError("short write")
                    remaining = remaining[count:]
                os.fsync(fd)
                os.fsync(directory)
            except OSError, ValueError:
                self.poisoned = True
                raise AgentFault(
                    "storage_unavailable",
                    "Agent recording failed; no further work is allowed.",
                    503,
                ) from None
            finally:
                if fd is not None:
                    os.close(fd)
                if directory is not None:
                    os.close(directory)
            self.used += len(raw)
            self.records.setdefault(snapshot.id, []).append(record)
            self.artifacts[snapshot.id] = artifact_index

    def events(self, sid: str, after: int, limit: int = 50) -> list[AgentEvent]:
        with self.lock:
            self.get(sid)
            return [r.event.model_copy(deep=True) for r in self.records[sid][after : after + limit]]

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
