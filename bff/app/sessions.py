"""Small single-writer journal. Full bounded snapshots per event are the projection.

Local POSIX filesystem only. A process lock excludes other BFF workers. No network
filesystem/power-loss guarantees; fsync commits precede observable publication.
"""

import fcntl
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.agent_files import AgentFault, canonical_digest, open_directory
from app.agent_schemas import (
    AgentError,
    AgentEvent,
    AgentSession,
    Claim,
    CreateSession,
    JournalRecord,
)

SESSION_ID = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[a-z0-9]{4}$")
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
                        if name != "events.jsonl" or not stat.S_ISREG(info.st_mode):
                            raise ValueError("unsafe orphan contents")
                        self.used += info.st_size
                finally:
                    os.close(orphan)
                continue
            directory = self._directory(sid)
            try:
                if any(name != "events.jsonl" for name in os.listdir(directory)):
                    raise ValueError("unexpected storage file")
                try:
                    journal = os.open(
                        "events.jsonl", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
                except FileNotFoundError:
                    journal = None
                records = []
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
            return self.records[sid][-1].event.session.model_copy(deep=True)

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
            except OSError:
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
