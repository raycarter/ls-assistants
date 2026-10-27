"""Offline file paging and exact policy compatibility, using only temporary public text."""

import errno
import os
import threading
import time
import tracemalloc
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.agent_files as file_module
from app.agent import AgentService
from app.agent_files import AgentFault, FilePolicy, canonical_digest
from app.agent_schemas import (
    AgentPlan,
    AgentRun,
    CreateSession,
    FetchArguments,
    PlanIdentity,
    ProposedStep,
    ReadArguments,
)
from app.sessions import SessionStore
from app.tools.registry import ToolRegistry

V3_RULES = "060b1c1ed619a9dd292d85d30250c3e6387a0e9c67684652209cdb9fa298a20a"


@pytest.fixture
def public_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return FilePolicy({"public": workspace}), workspace / "public.txt"


def arguments(**kwargs):
    return ReadArguments(workspace_id="public", path="public.txt", **kwargs)


def read(policy, **kwargs):
    return policy.read(arguments(**kwargs), threading.Event(), time.monotonic() + 30)


def test_large_file_late_page_and_true_eof(public_files):
    policy, path = public_files
    with path.open("w", encoding="utf-8") as stream:
        for number in range(1, 15004):
            stream.write(f"public fixture line {number} 🌱\n")
    assert path.stat().st_size > 256000
    page = read(policy, offset=15001, limit=2)
    assert page == {
        "content": "15001: public fixture line 15001 🌱\n15002: public fixture line 15002 🌱",
        "path": "public.txt",
        "offset": 15001,
        "returned_lines": 2,
        "truncated": True,
    }
    last = read(policy, offset=15003)
    assert last["content"] == "15003: public fixture line 15003 🌱"
    assert last["returned_lines"] == 1 and last["truncated"] is False
    with pytest.raises(AgentFault) as failure:
        read(policy, offset=15004)
    assert failure.value.code == "invalid_offset" and failure.value.status == 422


@pytest.mark.parametrize("layout", ["long-selected", "long-skipped", "many-lines"])
def test_memory_and_read_chunks_do_not_scale_with_file_size(public_files, monkeypatch, layout):
    policy, path = public_files
    chunk = ("🌱" * 2048).encode() if layout != "many-lines" else b"public\n" * 1024
    with path.open("wb") as stream:
        for _ in range(1024):
            stream.write(chunk)
        stream.write(b"\nend\n")
    expected_bytes = path.stat().st_size
    original = os.read
    scanned = calls = 0

    def bounded(fd, count):
        nonlocal scanned, calls
        assert 0 < count <= 8192
        block = original(fd, count)
        scanned += len(block)
        calls += 1
        return block

    monkeypatch.setattr(os, "read", bounded)
    tracemalloc.start()
    try:
        page = read(policy, offset=2 if layout == "long-skipped" else 1, limit=1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert scanned == expected_bytes and calls > expected_bytes // 8192
    assert peak < 512000, f"Paging retained {peak} bytes for a {expected_bytes}-byte file"
    if layout == "long-selected":
        assert page["content"] == "1: " + "🌱" * 1000
    elif layout == "long-skipped":
        assert page["content"] == "2: end"
    else:
        assert page["content"] == "1: public"
    assert page["truncated"] is (layout != "long-skipped")


@pytest.mark.parametrize("chunk_bytes", [1, 2, 3, 7, 8192])
def test_incremental_utf8_crlf_and_unicode_splitlines(public_files, monkeypatch, chunk_bytes):
    policy, path = public_files
    text = "\talpha🌱\r\nβeta\r終\x85\n\u2028last\u2029"
    path.write_bytes(text.encode("utf-8"))
    original = os.read
    monkeypatch.setattr(os, "read", lambda fd, count: original(fd, min(count, chunk_bytes)))
    lines = text.splitlines()
    page = read(policy)
    assert page["content"] == "\n".join(f"{i}: {line}" for i, line in enumerate(lines, 1))
    assert page["returned_lines"] == len(lines) and page["truncated"] is False
    for offset in range(1, len(lines) + 1):
        page = read(policy, offset=offset, limit=1)
        assert page["content"] == f"{offset}: {lines[offset - 1]}"
        assert page["truncated"] is (offset < len(lines))


def test_multibyte_character_and_crlf_cross_actual_chunk_boundaries(public_files):
    policy, path = public_files
    path.write_bytes(b"a" * 8191 + "🌱\n".encode() + b"b" * 8188 + b"\r\nlast")
    page = read(policy, offset=3)
    assert page["content"] == "3: last"
    assert page["returned_lines"] == 1 and page["truncated"] is False


@pytest.mark.parametrize(
    "text", ["", "\n", "\n\n", "one", "one\n", "one\n\n", "one\r\n", "one\r\n\r\n", "\u2028"]
)
def test_empty_files_trailing_separators_and_eof_offsets(public_files, text):
    policy, path = public_files
    path.write_bytes(text.encode())
    lines = text.splitlines()
    page = read(policy)
    assert page == {
        "content": "\n".join(f"{i}: {line}" for i, line in enumerate(lines, 1)),
        "path": "public.txt",
        "offset": 1,
        "returned_lines": len(lines),
        "truncated": False,
    }
    if lines:
        last = read(policy, offset=len(lines))
        assert last["content"] == f"{len(lines)}: {lines[-1]}"
        assert last["truncated"] is False
    with pytest.raises(AgentFault) as failure:
        read(policy, offset=max(1, len(lines)) + 1)
    assert failure.value.code == "invalid_offset"


@pytest.mark.parametrize("length", [1000, 1001, 300000])
def test_final_line_display_clipping_is_honest(public_files, length):
    policy, path = public_files
    path.write_bytes(("終" * length).encode())
    page = read(policy)
    assert page["content"] == "1: " + "終" * 1000
    assert page["returned_lines"] == 1
    assert page["truncated"] is (length > 1000)


def test_page_byte_bound_includes_joining_newlines_and_counts_utf8(public_files):
    policy, path = public_files
    line = "🌱" * 999
    path.write_text((line + "\n") * 5, encoding="utf-8")
    page = read(policy)
    assert len(page["content"].encode()) == 15999
    assert page["returned_lines"] == 4 and page["truncated"] is True
    tail = read(policy, offset=5)
    assert tail["content"] == f"5: {line}" and tail["truncated"] is False
    # Four 4000-byte lines fit only when the joining newline is wrongly omitted.
    path.write_text(("🌱" * 999 + "a\n") * 4, encoding="utf-8")
    page = read(policy)
    assert page["returned_lines"] == 3 and page["truncated"] is True
    assert len(page["content"].encode()) <= 16000


def test_page_line_bound_and_legacy_serialized_defaults(public_files):
    policy, path = public_files
    assert arguments().model_dump() == {
        "workspace_id": "public",
        "path": "public.txt",
        "offset": 1,
        "limit": 200,
    }
    path.write_text("public\n" * 201, encoding="utf-8")
    page = read(policy)
    assert page["returned_lines"] == 200 and page["truncated"] is True
    assert read(policy, offset=201)["truncated"] is False


@pytest.mark.parametrize(
    "suffix", [b"\x00", b"\x01", b"\x0b", b"\x0c", b"\x1c", b"\xff", b"\xf0\x9f"]
)
def test_invalid_text_after_requested_page_is_still_rejected(public_files, suffix):
    policy, path = public_files
    path.write_bytes(b"visible\n" + b"public\n" * 40000 + suffix)
    with pytest.raises(AgentFault) as failure:
        read(policy, limit=1)
    assert failure.value.code == "file_unavailable"
    assert "visible" not in failure.value.message


@pytest.mark.parametrize("reason", ["timeout", "stop"])
@pytest.mark.parametrize("phase", ["before", "chunk", "eof"])
def test_cooperative_deadline_and_stop_through_eof(public_files, monkeypatch, reason, phase):
    policy, path = public_files
    path.write_bytes(b"public\n" * 10000)
    stop = threading.Event()
    clock = SimpleNamespace(now=99.0)
    monkeypatch.setattr(file_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    original = os.read
    descriptors = set()

    def expire():
        clock.now = 100.0  # The actual deadline, including equality.
        if reason == "stop":
            stop.set()  # Stop takes precedence even when the deadline also expires.

    def interrupted(fd, count):
        descriptors.add(fd)
        block = original(fd, count)
        if phase == "chunk" or (phase == "eof" and not block):
            expire()
        return block

    monkeypatch.setattr(os, "read", interrupted)
    if phase == "before":
        expire()
    with pytest.raises(AgentFault) as failure:
        policy.read(arguments(offset=9999, limit=1), stop, 100.0)
    assert failure.value.code == ("file_timeout" if reason == "timeout" else "operation_stopped")
    assert bool(descriptors) is (phase != "before")
    for fd in descriptors:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF


def test_each_read_uses_its_supplied_fresh_request_deadline(public_files, monkeypatch):
    policy, path = public_files
    path.write_text("public", encoding="utf-8")
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(file_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    for now in (100.0, 200.0):
        clock.now = now
        assert policy.read(arguments(), threading.Event(), now + 30)["content"] == "1: public"
    with pytest.raises(AgentFault) as failure:
        policy.read(arguments(), threading.Event(), 200.0)
    assert failure.value.code == "file_timeout"


@pytest.mark.parametrize("mutation", ["append", "truncate", "overwrite"])
@pytest.mark.parametrize("phase", ["chunk", "eof"])
def test_observed_mutation_never_returns_a_mixed_or_false_complete_page(
    public_files, monkeypatch, mutation, phase
):
    policy, path = public_files
    path.write_bytes(b"public\n" * 5000)
    before = path.stat()
    original = os.read
    mutated = False

    def changed(fd, count):
        nonlocal mutated
        block = original(fd, count)
        if not mutated and (phase == "chunk" or not block):
            mutated = True
            with path.open("r+b") as stream:
                if mutation == "append":
                    stream.seek(0, os.SEEK_END)
                    stream.write(b"appended public text\n")
                elif mutation == "truncate":
                    stream.truncate(0)
                else:
                    stream.write(b"edited")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
        return block

    monkeypatch.setattr(os, "read", changed)
    with pytest.raises(AgentFault) as failure:
        read(policy, limit=1)
    assert mutated and failure.value.code == "file_unavailable"


@pytest.mark.parametrize(
    "path",
    ["../public.txt", "/public.txt", "a//b", "a\\b", ".git/config", "sessions/a", "secrets/a"],
)
def test_path_scope_remains_fixed(public_files, path):
    policy, _ = public_files
    with pytest.raises(AgentFault) as failure:
        policy.read(
            ReadArguments(workspace_id="public", path=path),
            threading.Event(),
            time.monotonic() + 30,
        )
    assert failure.value.code == "path_denied"


def test_default_roots_remain_disabled(public_files):
    _, path = public_files
    path.write_text("public", encoding="utf-8")
    assert FilePolicy().roots == {}
    with pytest.raises(AgentFault) as failure:
        read(FilePolicy())
    assert failure.value.code == "scope_unavailable"


def test_symlinks_ancestors_and_nonregular_files_stay_denied(public_files, tmp_path):
    policy, path = public_files
    path.write_text("public", encoding="utf-8")
    (path.parent / "link").symlink_to(path)
    (path.parent / "dirlink").symlink_to(path.parent, target_is_directory=True)
    os.mkfifo(path.parent / "pipe")
    ancestor = tmp_path / "alias"
    ancestor.symlink_to(path.parent, target_is_directory=True)
    for files, target in (
        (policy, "link"),
        (policy, "dirlink/public.txt"),
        (policy, "pipe"),
        (FilePolicy({"public": ancestor}), "public.txt"),
    ):
        with pytest.raises(AgentFault):
            files.read(
                ReadArguments(workspace_id="public", path=target),
                threading.Event(),
                time.monotonic() + 30,
            )


def test_storage_identity_stays_denied_after_rename_and_in_root_ancestors(
    public_files, monkeypatch
):
    policy, path = public_files
    storage = path.parent / "journal"
    child = storage / "child"
    child.mkdir(parents=True)
    (child / "public.txt").write_text("public storage fixture", encoding="utf-8")
    info = storage.stat()
    policy.bind_storage(storage, identity=(info.st_dev, info.st_ino))
    moved = path.parent / "moved"
    storage.rename(moved)
    policy.roots["nested"] = moved / "child"
    original = os.open

    def guarded(target, flags, *args, **kwargs):
        assert target != "public.txt", "Storage contents must never be opened"
        return original(target, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    for workspace_id, target in (("public", "moved/child/public.txt"), ("nested", "public.txt")):
        with pytest.raises(AgentFault) as failure:
            policy.read(
                ReadArguments(workspace_id=workspace_id, path=target),
                threading.Event(),
                time.monotonic() + 30,
            )
        assert failure.value.code == "path_denied"


def steps(tool="read_file"):
    args = arguments() if tool == "read_file" else FetchArguments(url="https://example.com/public")
    return [
        ProposedStep(
            title="Public fixture", description="Read public text", tool=tool, arguments=args
        )
    ]


def predecessor(projection):
    return {**projection, "version": "read-text-v3", "rules_digest": V3_RULES}


def test_exact_policy_version_and_predecessor_contract(public_files):
    policy, _ = public_files
    current = policy.projection()
    assert current["version"] == "read-text-v4"
    assert current["rules_digest"] == canonical_digest(
        {
            "denied_components": sorted(FilePolicy.DENIED),
            "path_rules": "shared-exclusions-storage-identity-ancestors-v3",
            "text_rules": "streaming-utf8-controls-splitlines-eof-v1",
            "page_bytes": 16000,
            "line_chars": 1000,
            "page_lines": 200,
        }
    )
    assert V3_RULES == canonical_digest(
        {
            "denied_components": sorted(FilePolicy.DENIED),
            "path_rules": "shared-exclusions-storage-identity-ancestors-v3",
            "file_bytes": 256000,
            "page_bytes": 16000,
            "line_chars": 1000,
            "page_lines": 200,
        }
    )
    assert current["rules_digest"] != V3_RULES
    assert policy.matches(current) and policy.matches(predecessor(current))
    assert not policy.matches({**current, "version": "read-text-v3"})
    assert not policy.matches({**current, "rules_digest": V3_RULES})


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "changed", ["version", "rules_digest", "roots_digest", "workspaces", "storage_digest", "extra"]
)
def test_unknown_file_rules_or_changed_scope_never_match(public_files, legacy, changed):
    policy, _ = public_files
    projection = policy.projection()
    if legacy:
        projection = predecessor(projection)
    projection[changed] = "unknown"
    registry = ToolRegistry(policy, SimpleNamespace(policy_version="public-web-v1"))
    assert not policy.matches(projection)
    assert not registry.matches(projection, steps())
    full = {**registry.projection(), **projection}
    assert not registry.matches(full, steps())


@pytest.mark.parametrize("legacy", [False, True])
def test_registry_compatibility_keeps_reviewed_material_and_network_boundary(public_files, legacy):
    policy, _ = public_files
    registry = ToolRegistry(policy, SimpleNamespace(policy_version="public-web-v1"))
    assert registry.known == ("read_file", "web_fetch", "web_search", "grep_in_folder")
    assert registry.enabled == ("read_file", "web_fetch", "grep_in_folder")
    file_projection, full_projection = policy.projection(), registry.projection()
    if legacy:
        file_projection, full_projection = (
            predecessor(file_projection),
            predecessor(full_projection),
        )
    reviewed = {"access_policy": file_projection, "steps": [step.model_dump() for step in steps()]}
    reviewed_digest = canonical_digest(reviewed)
    assert registry.matches(file_projection, steps())
    assert registry.matches(full_projection, steps())
    assert registry.matches(full_projection, steps("web_fetch"))
    assert not registry.matches(file_projection, steps("web_fetch"))
    assert not registry.matches(file_projection, steps() + steps("web_fetch"))
    assert canonical_digest(reviewed) == reviewed_digest
    assert reviewed["access_policy"] == file_projection
    for key in ("tool_registry_version", "enabled_tools", "network_policy_version"):
        assert not registry.matches({**full_projection, key: "unknown"}, steps())
        partial = full_projection.copy()
        del partial[key]
        assert not registry.matches(partial, steps())
    registry.enabled = ("read_file",)
    assert not registry.matches(full_projection, steps())
    assert not registry.matches(full_projection, steps("web_fetch"))
    assert registry.matches(file_projection, steps())
    registry.enabled = ()
    assert not registry.matches(file_projection, steps())


@pytest.mark.parametrize("full_registry", [False, True])
async def test_approved_v3_read_keeps_exact_persisted_plan_and_digest(
    public_files, tmp_path, full_registry
):
    policy, path = public_files
    path.write_bytes(b"public first line\n" + b"z" * 300000)
    fetcher = SimpleNamespace(
        policy_version="public-web-v1",
        fetch=AsyncMock(side_effect=AssertionError("A legacy read must not fetch")),
    )
    owner = AgentService(
        SessionStore(tmp_path / "journal"),
        files=policy,
        fetcher=fetcher,
        planner=SimpleNamespace(plan=AsyncMock(side_effect=AssertionError("No replanning"))),
        models=("fixture-model",),
    )
    try:
        await owner.start()
        snapshot = await owner.create(
            CreateSession(request_id="create", model="fixture-model", reasoning_effort="high")
        )
        approved_step = steps()[0].model_dump()
        approved_step["arguments"]["limit"] = 1
        plan = AgentPlan.model_validate(
            {
                "id": "reviewed-v3-read",
                "revision": 1,
                "digest": "0" * 64,
                "objective": "Read the exact public path",
                "assumptions": [],
                "model": "fixture-model",
                "reasoning_effort": "high",
                "limits": {"max_steps": 3, "max_tool_calls": 3, "timeout_seconds": 30},
                "steps": [{"id": "read-step", **approved_step}],
                "access_policy": predecessor(
                    owner.registry.projection() if full_registry else policy.projection()
                ),
            }
        )
        material = {
            "session": snapshot.id,
            "run": "legacy-run",
            "plan": plan.model_dump(exclude={"digest"}),
        }
        plan.digest = canonical_digest(material)
        exact_plan = plan.model_dump()
        snapshot.runs.append(
            AgentRun(
                id="legacy-run",
                request="Read public fixture",
                status="awaiting_approval",
                created_at=owner.store.clock(),
                updated_at=owner.store.clock(),
                plan=plan,
            )
        )
        await owner.io(owner.store.append, snapshot, "session.updated", "legacy-fixture")
        operation = await owner.prepare(
            snapshot.id,
            PlanIdentity(
                request_id="approve", plan_id=plan.id, revision=plan.revision, digest=plan.digest
            ),
            rid="legacy-run",
            action="execute",
        )
        async for _ in owner.frames(operation):
            pass
        run = (await owner.get(snapshot.id)).runs[0]
        assert run.status == "completed", run.error
        assert run.plan.model_dump() == exact_plan
        assert run.plan.digest == canonical_digest(material)
        assert run.results[0].content == "1: public first line"
        assert run.results[0].truncated is True
        fetcher.fetch.assert_not_called()
        owner.planner.plan.assert_not_called()
    finally:
        await owner.close()
