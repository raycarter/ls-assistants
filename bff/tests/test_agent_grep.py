"""All search targets and storage boundaries are synthetic temporary folders."""

import errno
import json
import os
import threading
import tracemalloc
from types import SimpleNamespace

import pytest

from app import agent_grep
from app.agent_files import AgentFault, FilePolicy
from app.agent_grep import BLOCK, scan_folder, validate_grep
from app.agent_schemas import GrepArguments


@pytest.fixture
def folder(tmp_path):
    root = tmp_path.resolve() / "workspace"
    root.mkdir()
    return root, FilePolicy({"workspace": root})


def arguments(**kwargs):
    return GrepArguments(workspace_id="workspace", pattern="needle", **kwargs)


def search(policy, args=None, stop=None, timeout=10):
    return scan_folder(policy, args or arguments(), stop or threading.Event(), timeout)


def records(document):
    chunks = list(document.iter_text())
    assert all(len(chunk) <= BLOCK for chunk in chunks)
    text = "".join(chunks)
    assert len(text) == document.text_length
    return [json.loads(line) for line in text.splitlines()]


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "./"),
        ("path", "a/../b"),
        ("path", "/a"),
        ("path", "a//b"),
        ("path", "a\\b"),
        ("path", "a\0b"),
        ("path", "a/.env"),
        ("file_glob", ""),
        ("file_glob", "../*.py"),
        ("file_glob", "/*.py"),
        ("file_glob", "a\\*"),
        ("file_glob", "a/\0"),
        ("file_glob", "sessions/*"),
        ("file_glob", "a/./*"),
        ("file_glob", "."),
        ("pattern", "a\nb"),
        ("pattern", "a\rb"),
        ("pattern", "a\u2028b"),
        ("pattern", "a\u2029b"),
        ("pattern", "a\x85b"),
        ("pattern", "\0"),
        ("pattern", "a\tb"),
        ("pattern", "\x7f"),
        ("pattern", "\ud800"),
    ],
)
def test_pure_rejection(folder, monkeypatch, field, value):
    _, policy = folder
    args = arguments().model_copy(update={field: value})

    def forbidden(*a, **kw):
        raise AssertionError("validation must be pure")

    with monkeypatch.context() as guard:
        for name in ("open", "stat", "fstat", "scandir"):
            guard.setattr(os, name, forbidden)
        with pytest.raises(AgentFault):
            validate_grep(policy, args)


def test_pure_valid_root_and_storage(folder, monkeypatch):
    root, policy = folder
    policy.bind_storage(root / "journal")

    def forbidden(*a, **kw):
        raise AssertionError("validation must be pure")

    with monkeypatch.context() as guard:
        guard.setattr(os, "open", forbidden)
        assert validate_grep(policy, arguments()) == []
        assert validate_grep(policy, arguments(path="src", file_glob="**/*.py")) == ["src"]
        with pytest.raises(AgentFault):
            validate_grep(policy, arguments(path="journal"))
        with pytest.raises(AgentFault):
            validate_grep(policy, arguments().model_copy(update={"workspace_id": "unknown"}))


def test_literal_unicode_json_and_case(folder):
    root, policy = folder
    literal = '[a-z]+.*(é雪)"<&'
    (root / 'a"雪.txt').write_text(f"prefix {literal}\t\\ suffix\n{literal.upper()}\n")
    document = search(policy, GrepArguments(workspace_id="workspace", pattern=literal))
    try:
        assert records(document) == [
            {"path": 'a"雪.txt', "line": 1, "text": f"prefix {literal}\t\\ suffix"}
        ]
        assert document.total_matches == document.files_searched == 1
        assert document.complete
        assert records(document) == records(document)  # Rewindable source.
    finally:
        document.close()


def test_boundaries_long_lines_and_full_artifact(folder):
    root, policy = folder
    line = "x" * (BLOCK - 3) + "needle" + "雪" * (BLOCK * 3) + '"\\end'
    (root / "large.txt").write_text(line + "\n" + "needle\n" * 2000)
    document = search(policy, arguments(offset=999999, limit=1))
    try:
        found = records(document)
        assert found[0] == {"path": "large.txt", "line": 1, "text": line}
        assert len(found) == document.total_matches == 2001
        assert document.text_length > 16000
    finally:
        document.close()


def test_file_beyond_former_byte_quota(folder):
    root, policy = folder
    with (root / "large.txt").open("wb") as stream:
        for _ in range(2049):
            stream.write(b"x" * (BLOCK - 1) + b"\n")
        stream.write(b"needle at EOF")
    document = search(policy)
    try:
        assert records(document) == [{"path": "large.txt", "line": 2050, "text": "needle at EOF"}]
        assert document.complete
    finally:
        document.close()


def test_newlines_and_crlf_at_block_boundary(folder):
    root, policy = folder
    content = "x" * (BLOCK - 7) + "needle\r\nneedle\rneedle\n\r\nneedle\x85needle\u2028needle\u2029"
    (root / "lines.txt").write_bytes(content.encode())
    document = search(policy)
    try:
        found = records(document)
        assert [r["line"] for r in found] == [1, 2, 3, 5, 6, 7]
        assert found[0]["text"] == "x" * (BLOCK - 7) + "needle"
        assert all(r["text"] == "needle" for r in found[1:])
    finally:
        document.close()


@pytest.mark.parametrize(
    "path,glob,expected,files",
    [
        (".", None, ["a.py", "src/b.py", "src/c.txt"], 4),
        ("src", "*.py", ["src/b.py"], 1),
        (".", "src/*.py", ["src/b.py"], 1),
        (".", "*.missing", [], 0),
        ("empty", None, [], 0),
    ],
)
def test_selection_and_empty_results(folder, path, glob, expected, files):
    root, policy = folder
    (root / "src").mkdir()
    (root / "empty").mkdir()
    for name in ("a.py", "src/b.py", "src/c.txt"):
        (root / name).write_text("needle")
    (root / "no.txt").write_text("NEEDLE")
    document = search(policy, arguments(path=path, file_glob=glob))
    try:
        assert sorted(r["path"] for r in records(document)) == expected
        assert document.files_searched == files
        assert document.complete
    finally:
        document.close()


@pytest.mark.parametrize("ending", [b"\0", b"\xff", b"\xe2", b"\x01", b"\x0b"])
def test_late_binary_discards_all_staged_records(folder, ending):
    root, policy = folder
    (root / "bad.txt").write_bytes(b"needle\n" + b"x" * (BLOCK * 3) + ending)
    (root / "good.txt").write_text("needle good")
    document = search(policy)
    try:
        assert records(document) == [{"path": "good.txt", "line": 1, "text": "needle good"}]
        assert document.skipped_binary == 1
        assert document.files_searched == document.total_matches == 1
        assert not document.complete
    finally:
        document.close()


def test_exclusions_symlinks_special_and_storage_aliases(folder, monkeypatch):
    root, policy = folder
    for name in FilePolicy.DENIED | {".env.local", "my-secret", "a.pem", "a.key"}:
        (root / name).mkdir()
        (root / name / "hidden.txt").write_text("needle")
    storage = root / "journal"
    storage.mkdir()
    (storage / "hidden.txt").write_text("needle")
    info = storage.stat()
    policy.bind_storage(storage, identity=(info.st_dev, info.st_ino))
    storage.rename(root / "renamed")
    (root / "link-dir").symlink_to(root / "renamed", target_is_directory=True)
    (root / "link-file").symlink_to(root / "renamed" / "hidden.txt")
    os.mkfifo(root / "pipe")
    (root / "ok.txt").write_text("needle")
    original = os.open

    def guarded(name, *a, **kw):
        assert name != "hidden.txt"
        if isinstance(name, str) and "/" not in name:
            assert name == "private" or not policy.excluded(name)
            assert name not in {"link-dir", "link-file", "pipe"}
        return original(name, *a, **kw)

    monkeypatch.setattr(os, "open", guarded)
    document = search(policy)
    try:
        assert records(document) == [{"path": "ok.txt", "line": 1, "text": "needle"}]
        assert document.complete
    finally:
        document.close()
    with pytest.raises(AgentFault, match="storage"):
        search(policy, arguments(path="renamed"))


@pytest.mark.parametrize("kind", ["root", "ancestor", "selected", "missing", "file"])
def test_invalid_selected_directory_fails(folder, kind):
    root, policy = folder
    path = "."
    if kind in {"root", "ancestor"}:
        target = root if kind == "root" else root.parent / "outer"
        if kind == "ancestor":
            target.mkdir()
            (target / "inner").mkdir()
            policy = FilePolicy({"workspace": target / "inner"})
        moved = target.with_name("moved")
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=True)
    elif kind == "selected":
        (root / "alias").symlink_to(root, target_is_directory=True)
        path = "alias"
    elif kind == "missing":
        path = "missing"
    else:
        (root / "file").write_text("needle")
        path = "file"
    with pytest.raises(AgentFault):
        search(policy, arguments(path=path))


def test_storage_ancestor_identity_rejected(folder):
    root, _ = folder
    (root / "child").mkdir()
    policy = FilePolicy({"workspace": root / "child"})
    info = root.stat()
    policy.bind_storage(root.with_name("old"), identity=(info.st_dev, info.st_ino))
    with pytest.raises(AgentFault, match="storage"):
        search(policy)


@pytest.mark.parametrize("mode", ["read", "mutation", "replacement"])
def test_file_failure_discards_staging(folder, monkeypatch, mode):
    root, policy = folder
    target = root / "file.txt"
    target.write_bytes(b"needle\n" + b"x" * BLOCK * 2)
    original = os.read
    calls = 0

    def changed(fd, size):
        nonlocal calls
        result = original(fd, size)
        calls += 1
        if calls == 2:
            if mode == "read":
                raise PermissionError("synthetic read failure")
            if mode == "mutation":
                with target.open("ab") as output:
                    output.write(b"changed")
            else:
                target.rename(root / "detached")
                target.write_text("replacement")
        return result

    monkeypatch.setattr(os, "read", changed)
    if mode == "replacement":
        # Namespace mutation is a traversal fault, never a zero-match success.
        with pytest.raises(AgentFault):
            search(policy)
    else:
        document = search(policy)
        try:
            assert records(document) == []
            assert document.skipped_unreadable == 1
            assert not document.complete
        finally:
            document.close()


def test_enumeration_error_fails_and_closes_fds(folder, monkeypatch):
    root, policy = folder
    (root / "child").mkdir()
    original_open, original_close, original_scan = os.open, os.close, os.scandir
    live = set()

    def opened(*a, **kw):
        fd = original_open(*a, **kw)
        live.add(fd)
        return fd

    def closed(fd):
        live.discard(fd)
        return original_close(fd)

    calls = 0

    def scan(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("synthetic enumeration failure")
        return original_scan(fd)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", closed)
    monkeypatch.setattr(os, "scandir", scan)
    with pytest.raises(AgentFault):
        search(policy)
    for fd in live:  # TemporaryFile closes its fd in C, bypassing os.close.
        with pytest.raises(OSError) as failure:
            os.fstat(fd)
        assert failure.value.errno == errno.EBADF


@pytest.mark.parametrize("phase", ["open", "read", "write"])
def test_cancellation_cleanup(folder, monkeypatch, phase):
    root, policy = folder
    (root / "file.txt").write_text("needle\n" * 3000)
    stop = threading.Event()
    original_open, original_close, original_read = os.open, os.close, os.read
    original_temp = agent_grep.tempfile.TemporaryFile
    live, spools = set(), []

    def opened(*a, **kw):
        fd = original_open(*a, **kw)
        live.add(fd)
        if phase == "open":
            stop.set()
        return fd

    def closed(fd):
        live.discard(fd)
        return original_close(fd)

    def read(*a):
        result = original_read(*a)
        if phase == "read":
            stop.set()
        return result

    def temporary(*a, **kw):
        spool = original_temp(*a, **kw)
        spools.append(spool)
        if phase == "write":
            write = spool.write

            def cancelling(text):
                result = write(text)
                stop.set()
                return result

            spool.write = cancelling
        return spool

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", closed)
    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(agent_grep.tempfile, "TemporaryFile", temporary)
    with pytest.raises(AgentFault) as failure:
        search(policy, stop=stop)
    assert failure.value.code == "operation_stopped"
    for fd in live:
        with pytest.raises(OSError) as failure:
            os.fstat(fd)
        assert failure.value.errno == errno.EBADF
    assert spools and all(spool.closed for spool in spools)


def test_fresh_unit_timeouts_allow_long_search(folder, monkeypatch):
    root, policy = folder
    (root / "file.txt").write_text("needle\n" * 5000)
    clock = 0

    def monotonic():
        nonlocal clock
        clock += 0.001
        return clock

    monkeypatch.setattr(agent_grep.time, "monotonic", monotonic)
    document = search(policy, timeout=0.01)
    try:
        assert document.total_matches == 5000
        assert clock > 1
    finally:
        document.close()


def test_slow_unit_times_out(folder, monkeypatch):
    root, policy = folder
    (root / "file.txt").write_text("needle")
    clock = 0
    original = os.read

    def read(*args):
        nonlocal clock
        clock += 2
        return original(*args)

    monkeypatch.setattr(agent_grep.time, "monotonic", lambda: clock)
    monkeypatch.setattr(os, "read", read)
    with pytest.raises(AgentFault) as failure:
        search(policy, timeout=1)
    assert failure.value.code == "file_timeout"


@pytest.mark.parametrize("literal", ["needle", "雪é", "[.*]", "e"])
def test_literal_and_utf8_decoder_boundaries(folder, literal):
    root, policy = folder
    content = "x" * (BLOCK - 2) + literal + "suffix"
    (root / "file.txt").write_text(content)
    document = search(policy, GrepArguments(workspace_id="workspace", pattern=literal))
    try:
        assert records(document) == [{"path": "file.txt", "line": 1, "text": content}]
    finally:
        document.close()


def test_long_line_memory_is_bounded(folder):
    root, policy = folder
    size = 4 * 1024 * 1024
    with (root / "file.txt").open("wb") as target:
        for _ in range(size // BLOCK):
            target.write(b"x" * BLOCK)
        target.write(b"needle")
    tracemalloc.start()
    document = None
    try:
        document = search(policy)
        assert document.total_matches == 1
        assert document.text_length > size
        assert sum(len(chunk) for chunk in document.iter_text()) == document.text_length
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 1024 * 1024
    finally:
        tracemalloc.stop()
        if document is not None:
            document.close()


def test_seen_directory_identity_is_not_scanned_twice(folder, monkeypatch):
    root, policy = folder
    child = root / "child"
    child.mkdir()
    (child / "file.txt").write_text("needle")
    original = os.scandir
    root_identity = (root.stat().st_dev, root.stat().st_ino)

    class Repeated:
        def __init__(self, entries):
            self.entries = entries
            self.saved = None
            self.repeated = False

        def __next__(self):
            if self.saved is None:
                self.saved = next(self.entries)
                return self.saved
            if not self.repeated:
                self.repeated = True
                return self.saved
            return next(self.entries)

        def close(self):
            self.entries.close()

    def scan(fd):
        info = os.fstat(fd)
        entries = original(fd)
        return Repeated(entries) if (info.st_dev, info.st_ino) == root_identity else entries

    monkeypatch.setattr(os, "scandir", scan)
    document = search(policy)
    try:
        assert document.total_matches == document.files_searched == 1
        assert records(document)[0]["path"] == "child/file.txt"
    finally:
        document.close()


def test_directory_replaced_between_stat_and_open_fails(folder, monkeypatch):
    root, policy = folder
    (root / "child").mkdir()
    (root / "child" / "file.txt").write_text("needle")
    original = os.open

    def changed(name, *args, **kwargs):
        if name == "child":
            (root / "child").rename(root / "moved")
            (root / "child").symlink_to(root / "moved", target_is_directory=True)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", changed)
    with pytest.raises(AgentFault):
        search(policy)


def test_unreadable_file_open_is_reported(folder, monkeypatch):
    root, policy = folder
    (root / "file.txt").write_text("needle")
    original = os.open

    def denied(name, *args, **kwargs):
        if name == "file.txt":
            raise PermissionError("synthetic denied file")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    document = search(policy)
    try:
        assert records(document) == []
        assert document.skipped_unreadable == 1
        assert not document.complete
    finally:
        document.close()


def test_successful_source_is_caller_owned(folder, monkeypatch):
    root, policy = folder
    (root / "file.txt").write_text("needle")
    spools = []
    original = agent_grep.tempfile.TemporaryFile

    def temporary(*args, **kwargs):
        spool = original(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(agent_grep.tempfile, "TemporaryFile", temporary)
    document = search(policy)
    try:
        assert not spools[0].closed
        assert all(spool.closed for spool in spools[1:])
        assert records(document)[0]["text"] == "needle"
    finally:
        document.close()
    assert all(spool.closed for spool in spools)


@pytest.mark.parametrize("phase", ["entry", "opened", "final"])
def test_protected_storage_hardlink_cannot_produce_evidence(folder, monkeypatch, phase):
    root, policy = folder
    journal = root / "journal"
    journal.mkdir()
    info = journal.stat()
    policy.bind_storage(journal, identity=(info.st_dev, info.st_ino))
    alias = root / "alias.txt"
    record = journal / "record.txt"
    if phase == "entry":
        record.write_text("needle from protected storage")
        os.link(record, alias)
    else:
        alias.write_text("needle from protected storage")
    identity = (alias.stat().st_dev, alias.stat().st_ino)
    original_open, original_read = os.open, os.read
    linked = phase == "entry"
    reads = 0

    def opened(name, *args, **kwargs):
        nonlocal linked
        if name == "alias.txt":
            assert phase != "entry", "Entry link count must reject before opening"
            if phase == "opened":
                os.link(alias, record)
                linked = True
        return original_open(name, *args, **kwargs)

    def read(fd, size):
        nonlocal linked, reads
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            assert phase == "final", "Multiply-linked file must never reach os.read"
            reads += 1
            result = original_read(fd, size)
            if not linked:
                os.link(alias, record)
                linked = True
            return result
        return original_read(fd, size)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "read", read)
    document = search(policy)
    try:
        assert records(document) == []
        assert document.text_length == document.total_matches == document.files_searched == 0
        assert document.skipped_unreadable == 1
        assert document.skipped_binary == 0
        assert not document.complete
        assert linked
        assert (reads > 0) == (phase == "final")
    finally:
        document.close()


def test_link_count_alone_changes_file_version(folder):
    root, _ = folder
    target = root / "file.txt"
    target.write_text("needle")
    info = target.stat()
    changed = SimpleNamespace(
        **{
            name: getattr(info, name)
            for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        },
        st_nlink=info.st_nlink + 1,
    )
    assert agent_grep._version(info) != agent_grep._version(changed)


@pytest.mark.parametrize("spool_number", [1, 2, 3], ids=["document", "file", "line"])
@pytest.mark.parametrize("failure_mode", ["timeout", "cancel"])
def test_spool_creation_failure_closes_every_returned_resource(
    folder, monkeypatch, spool_number, failure_mode
):
    root, policy = folder
    (root / "file.txt").write_text("needle")
    stop = threading.Event()
    clock = 0
    spools, live = [], set()
    original_temp = agent_grep.tempfile.TemporaryFile
    original_open, original_close = os.open, os.close

    def opened(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        live.add(fd)
        return fd

    def closed(fd):
        live.discard(fd)
        return original_close(fd)

    def temporary(*args, **kwargs):
        nonlocal clock
        spool = original_temp(*args, **kwargs)
        spools.append(spool)
        if len(spools) == spool_number:
            if failure_mode == "timeout":
                clock += 2
            else:
                stop.set()
        return spool

    monkeypatch.setattr(agent_grep.time, "monotonic", lambda: clock)
    monkeypatch.setattr(agent_grep.tempfile, "TemporaryFile", temporary)
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", closed)
    with pytest.raises(AgentFault) as failure:
        search(policy, stop=stop, timeout=1)
    assert failure.value.code == (
        "file_timeout" if failure_mode == "timeout" else "operation_stopped"
    )
    assert len(spools) == spool_number
    assert all(spool.closed for spool in spools)
    for fd in live:  # TemporaryFile closes its descriptor without os.close.
        with pytest.raises(OSError) as unavailable:
            os.fstat(fd)
        assert unavailable.value.errno == errno.EBADF
