import os
import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from app import agent_read_chunks as chunks
from app.agent_files import AgentFault, FilePolicy
from app.agent_schemas import ReadArguments


@pytest.fixture
def source(tmp_path):
    # Use a synthetic root with a no-symlink spelling (macOS /var is a symlink).
    root = tmp_path.resolve()
    path = root / "input.txt"
    policy = FilePolicy({"test": root})
    args = ReadArguments(workspace_id="test", path="input.txt", offset=99, limit=1)
    return root, path, policy, args


def advance(reader, stop=None):
    return reader.next_chunk(stop or threading.Event(), time.monotonic() + 10)


def fault(code, call):
    with pytest.raises(AgentFault) as caught:
        call()
    assert caught.value.code == code


def test_lazy_construction_and_close(source, monkeypatch):
    _, _, policy, args = source

    def forbidden(*args, **kwargs):
        raise AssertionError("construction must do no I/O or validation")

    monkeypatch.setattr(policy, "validate", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(os, "fstat", forbidden)
    reader = chunks.FileChunkReader(policy, args)
    reader.close()
    reader.close()
    fault("file_closed", lambda: advance(reader))


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"x" * (chunks.CHUNK_BYTES * 4 + 7),
        ("a" * (chunks.CHUNK_BYTES - 1) + "\U0001f642\r\n" + "é\r\n" * 9000).encode(),
        b"a" * (chunks.CHUNK_BYTES - 1) + b"\r\nend\r\n",
        b"z" * chunks.CHUNK_BYTES,
    ],
)
def test_exact_content_offsets_eof_and_immutable(source, payload, monkeypatch):
    _, path, policy, args = source
    path.write_bytes(payload)
    reader = chunks.FileChunkReader(policy, args)
    pages = []
    while True:
        page = advance(reader)
        assert page.start_byte == (pages[-1].end_byte if pages else 0)
        assert page.end_byte - page.start_byte == len(page.content.encode())
        assert len(page.content.encode()) <= chunks.CHUNK_BYTES
        assert page.total_bytes == len(payload)
        pages.append(page)
        if page.eof:
            break
    assert "".join(page.content for page in pages).encode() == payload
    assert len({page.file_version for page in pages}) == 1
    with pytest.raises(FrozenInstanceError):
        page.content = "changed"

    def forbidden(*args):
        raise AssertionError("must not read after EOF")

    monkeypatch.setattr(os, "read", forbidden)
    again = advance(reader)
    assert again.content == "" and again.eof
    assert again.start_byte == again.end_byte == len(payload)
    reader.check_version()
    reader.close()


@pytest.mark.parametrize("operation", ["next", "check", "close"])
def test_mutation_closes_owned_fd(source, operation):
    _, path, policy, args = source
    path.write_bytes(b"a" * (chunks.CHUNK_BYTES + 1))
    reader = chunks.FileChunkReader(policy, args)
    advance(reader)
    fd = reader._fd
    path.write_bytes(b"changed")
    call = {"next": lambda: advance(reader), "check": reader.check_version, "close": reader.close}[
        operation
    ]
    fault("file_changed", call)
    with pytest.raises(OSError):
        os.fstat(fd)
    reader.close()


def test_eof_mutation_and_path_replacement(source):
    root, _, policy, args = source
    directory = root / "original"
    directory.mkdir()
    path = directory / "input.txt"
    path.write_bytes(b"a" * (chunks.CHUNK_BYTES + 10))
    reader = chunks.FileChunkReader(policy, args.model_copy(update={"path": "original/input.txt"}))
    first = advance(reader)
    # Replace the containing directory, leaving the pinned file's stat unchanged.
    directory.rename(root / "held")
    directory.mkdir()
    try:
        path.write_bytes(b"replacement")
        last = advance(reader)
        assert first.content + last.content == "a" * (chunks.CHUNK_BYTES + 10)
        assert last.eof
        (root / "held" / "input.txt").write_bytes(b"mutated after EOF")
        fault("file_changed", reader.check_version)
    finally:
        reader.close()


@pytest.mark.parametrize("payload", [b"bad\xff", b"bad\x00", b"bad\x1b", b"bad\xf0\x9f"])
def test_invalid_text_at_encounter_only(source, payload):
    _, path, policy, args = source
    path.write_bytes(b"a" * chunks.CHUNK_BYTES + payload)
    reader = chunks.FileChunkReader(policy, args)
    assert advance(reader).content == "a" * chunks.CHUNK_BYTES
    fd = reader._fd
    fault("file_unavailable", lambda: advance(reader))
    with pytest.raises(OSError):
        os.fstat(fd)
    # Explicit early stop never validates the unseen suffix.
    reader = chunks.FileChunkReader(policy, args)
    advance(reader)
    reader.close()


@pytest.mark.parametrize("kind", ["final", "child", "root", "ancestor"])
def test_symlinks_rejected_everywhere(source, kind):
    root, path, policy, args = source
    directory = root / "plain"
    directory.mkdir()
    (directory / "text.txt").write_text("safe")
    alias = root / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    if kind == "final":
        path.symlink_to(directory / "text.txt")
    elif kind == "child":
        args = args.model_copy(update={"path": "alias/text.txt"})
    else:
        nested = directory / "nested"
        nested.mkdir()
        policy = FilePolicy({"test": alias if kind == "root" else alias / "nested"})
    reader = chunks.FileChunkReader(policy, args)
    fault("file_unavailable", lambda: advance(reader))
    assert reader._fd is None


def test_storage_lexical_and_renamed_identity(source):
    root, _, policy, args = source
    storage = root / "data"
    storage.mkdir()
    (storage / "text.txt").write_text("protected")
    info = storage.stat()
    policy.bind_storage(storage, identity=(info.st_dev, info.st_ino))
    reader = chunks.FileChunkReader(policy, args.model_copy(update={"path": "data/text.txt"}))
    fault("path_denied", lambda: advance(reader))
    storage.rename(root / "moved")
    for configured_root, relative in [(root, "moved/text.txt"), (root / "moved", "text.txt")]:
        policy.roots["test"] = configured_root
        reader = chunks.FileChunkReader(policy, args.model_copy(update={"path": relative}))
        fault("path_denied", lambda reader=reader: advance(reader))


@pytest.mark.parametrize("relative", [".env", "../input.txt", "a/../input.txt", "secrets/x"])
def test_unchanged_policy_exclusions(source, relative):
    _, _, policy, args = source
    reader = chunks.FileChunkReader(policy, args.model_copy(update={"path": relative}))
    fault("path_denied", lambda: advance(reader))


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo"])
def test_hard_errors_and_regular_only(source, kind):
    _, path, policy, args = source
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    reader = chunks.FileChunkReader(policy, args)
    fault("file_unavailable" if kind == "missing" else "file_unsupported", lambda: advance(reader))
    assert reader._fd is None


@pytest.mark.parametrize("reason", ["stop", "deadline"])
@pytest.mark.parametrize("opened", [False, True])
def test_cancel_deadline_and_cleanup(source, reason, opened):
    _, path, policy, args = source
    path.write_bytes(b"x" * (chunks.CHUNK_BYTES + 1))
    reader = chunks.FileChunkReader(policy, args)
    if opened:
        advance(reader)
    fd = reader._fd
    stop = threading.Event()
    if reason == "stop":
        stop.set()
    deadline = time.monotonic() + (10 if reason == "stop" else -1)
    fault(
        "operation_stopped" if reason == "stop" else "file_timeout",
        lambda: reader.next_chunk(stop, deadline),
    )
    assert reader._fd is None
    if fd is not None:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("action", ["mutate", "cancel", "deadline", "ioerror", "short", "grow"])
def test_during_read_checks_and_short_reads(source, monkeypatch, action):
    _, path, policy, args = source
    path.write_bytes(b"a" * 20)
    reader = chunks.FileChunkReader(policy, args)
    real_read = os.read
    stop = threading.Event()
    clock = [100.0]
    monkeypatch.setattr(chunks.time, "monotonic", lambda: clock[0])
    fired = False

    def read(fd, count):
        nonlocal fired
        if action == "ioerror":
            raise OSError("synthetic read failure")
        data = real_read(fd, min(count, 3) if action == "short" else count)
        if not fired:
            fired = True
            if action == "mutate":
                path.write_bytes(b"b" * 20)
            elif action == "grow":
                with path.open("ab") as output:
                    output.write(b"b")
            elif action == "cancel":
                stop.set()
            elif action == "deadline":
                clock[0] += 20
        return data

    monkeypatch.setattr(os, "read", read)
    if action == "short":
        assert advance(reader).content == "a" * 20
        reader.close()
    else:
        code = {
            "cancel": "operation_stopped",
            "deadline": "file_timeout",
            "ioerror": "file_unavailable",
        }.get(action, "file_changed")
        fault(code, lambda: advance(reader, stop))
        assert reader._fd is None


def test_fresh_deadlines_between_calls(source, monkeypatch):
    _, path, policy, args = source
    path.write_bytes(b"a" * (chunks.CHUNK_BYTES + 1))
    clock = [100.0]
    monkeypatch.setattr(chunks.time, "monotonic", lambda: clock[0])
    reader = chunks.FileChunkReader(policy, args)
    assert not reader.next_chunk(threading.Event(), 101).eof
    clock[0] = 1000
    assert reader.next_chunk(threading.Event(), 1001).eof
    reader.close()


def test_successful_close_releases_fd(source):
    _, path, policy, args = source
    path.write_text("text")
    reader = chunks.FileChunkReader(policy, args)
    assert advance(reader).eof
    fd = reader._fd
    reader.close()
    reader.close()
    with pytest.raises(OSError):
        os.fstat(fd)
    fault("file_closed", reader.check_version)


def test_storage_identity_checked_in_configured_root_ancestors(source):
    root, _, policy, args = source
    storage = root / "data"
    nested = storage / "nested"
    nested.mkdir(parents=True)
    (nested / "input.txt").write_text("protected")
    info = storage.stat()
    policy.bind_storage(storage, identity=(info.st_dev, info.st_ino))
    storage.rename(root / "relocated")
    policy.roots["test"] = root / "relocated" / "nested"
    reader = chunks.FileChunkReader(policy, args)
    fault("path_denied", lambda: advance(reader))
