import builtins
import io
import os
import threading
import time
from pathlib import Path

import pytest

from app import agent_workspace
from app.agent_files import AgentFault, FilePolicy, canonical_digest
from app.agent_schemas import ReadArguments


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # Canonicalize only the pytest fixture, before any guards: on Darwin its
    # ancestors can include /var -> /private/var. Production never resolves.
    root = tmp_path.resolve() / "workspace"
    root.mkdir()
    monkeypatch.setattr(agent_workspace, "LOGICAL_ROOT", root)

    # Preserve the real generic policy, substituting only the approved spelling
    # at the factory boundary (including its hardcoded Darwin spelling).
    def fixture_policy(roots):
        assert list(roots) == ["workspace"]
        return FilePolicy({"workspace": root})

    monkeypatch.setattr(agent_workspace, "FilePolicy", fixture_policy)
    return root


def read(policy, path="note.txt", workspace_id="workspace"):
    return policy.read(
        ReadArguments(workspace_id=workspace_id, path=path),
        threading.Event(),
        time.monotonic() + 10,
    )


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_fixed_mapping_and_projection_without_io(tmp_path, monkeypatch, platform):
    # Materialize the existing project fixture before guarding global APIs;
    # restore guards before pytest reporting/fixture cleanup needs filesystem IO.
    assert tmp_path.is_dir()
    expected = Path("/private/tmp/ls-assistants" if platform == "darwin" else "/tmp/ls-assistants")

    def forbidden(*args, **kwargs):
        raise AssertionError("Workspace construction/projection must not perform filesystem IO")

    with monkeypatch.context() as guard:
        guard.setattr(agent_workspace.sys, "platform", platform)
        guard.setenv("WORKSPACE_ROOT", str(tmp_path))
        guard.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path))
        for name in (
            "resolve",
            "absolute",
            "stat",
            "lstat",
            "exists",
            "is_dir",
            "is_file",
            "is_symlink",
            "open",
            "read_text",
            "read_bytes",
            "mkdir",
            "iterdir",
            "glob",
            "rglob",
            "readlink",
        ):
            guard.setattr(Path, name, forbidden)
        for name in ("open", "stat", "lstat", "fstat", "mkdir", "listdir", "scandir", "readlink"):
            guard.setattr(os, name, forbidden)
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(io, "open", forbidden)
        policy = agent_workspace.build_file_policy()
        projection = policy.projection()
        assert type(policy) is FilePolicy
        assert agent_workspace.WORKSPACE_ID == "workspace"
        assert agent_workspace.LOGICAL_ROOT == Path("/tmp/ls-assistants")
        assert policy.roots == {"workspace": expected}
        assert projection == FilePolicy({"workspace": expected}).projection()
        assert projection["roots_digest"] == canonical_digest({"workspace": str(expected)})
        assert projection["workspaces"] == "workspace"
        assert str(expected) not in str(projection)


def test_generic_default_stays_empty():
    policy = FilePolicy()
    assert policy.roots == {}
    with pytest.raises(AgentFault) as failure:
        read(policy)
    assert failure.value.code == "scope_unavailable"


def test_factory_reads_only_configured_workspace(workspace):
    (workspace / "note.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    policy = agent_workspace.build_file_policy()
    assert policy.roots == {"workspace": workspace}
    assert read(policy)["content"] == "1: alpha\n2: beta"
    with pytest.raises(AgentFault) as failure:
        read(policy, workspace_id="other")
    assert failure.value.code == "scope_unavailable"


@pytest.mark.parametrize("kind", ["root", "ancestor", "directory", "file"])
def test_factory_keeps_no_follow_at_every_level(workspace, monkeypatch, kind):
    (workspace / "note.txt").write_text("fixture", encoding="utf-8")
    target = "note.txt"
    if kind == "root":
        moved = workspace.with_name("moved")
        workspace.rename(moved)
        workspace.symlink_to(moved, target_is_directory=True)
    elif kind == "ancestor":
        parent = workspace / "outer"
        root = parent / "child"
        root.mkdir(parents=True)
        (root / "note.txt").write_text("fixture", encoding="utf-8")
        moved = workspace / "moved"
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        monkeypatch.setattr(
            agent_workspace, "FilePolicy", lambda roots: FilePolicy({"workspace": root})
        )
    elif kind == "directory":
        (workspace / "alias").symlink_to(workspace, target_is_directory=True)
        target = "alias/note.txt"
    else:
        (workspace / "alias.txt").symlink_to(workspace / "note.txt")
        target = "alias.txt"
    with pytest.raises(AgentFault) as failure:
        read(agent_workspace.build_file_policy(), target)
    assert failure.value.code == "file_unavailable"


def test_missing_root_does_not_create_or_fall_back(workspace, monkeypatch):
    workspace.rmdir()
    (workspace.parent / "note.txt").write_text("cwd decoy", encoding="utf-8")
    monkeypatch.chdir(workspace.parent)
    policy = agent_workspace.build_file_policy()
    with pytest.raises(AgentFault) as failure:
        read(policy)
    assert failure.value.code == "file_unavailable"
    assert policy.roots == {"workspace": workspace}
    assert not workspace.exists()


@pytest.mark.parametrize(
    "path",
    [
        "../note.txt",
        "/note.txt",
        "a\\note.txt",
        ".env",
        "sessions/note.txt",
        "dev-plans-archive/note.txt",
    ],
)
def test_factory_keeps_escape_and_exclusion_rules(workspace, monkeypatch, path):
    def forbidden(*args, **kwargs):
        raise AssertionError("Denied paths must not reach filesystem access")

    policy = agent_workspace.build_file_policy()
    with monkeypatch.context() as guard:
        guard.setattr(os, "open", forbidden)
        with pytest.raises(AgentFault) as failure:
            read(policy, path)
        assert failure.value.code == "path_denied"


@pytest.mark.parametrize("renamed", [False, True])
def test_factory_keeps_bound_storage_excluded(workspace, monkeypatch, renamed):
    storage = workspace / "journal"
    storage.mkdir()
    policy = agent_workspace.build_file_policy()
    info = storage.stat()
    policy.bind_storage(storage, identity=(info.st_dev, info.st_ino))
    if renamed:
        moved = workspace / "moved"
        storage.rename(moved)
        storage = moved
    original_open = os.open

    def guarded(path, *args, **kwargs):
        assert path != "note.txt", "Storage contents must never be opened"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    with pytest.raises(AgentFault) as failure:
        read(policy, f"{storage.name}/note.txt")
    assert failure.value.code == "path_denied"


def test_changed_mapping_invalidates_projection(workspace, monkeypatch):
    before = agent_workspace.build_file_policy()
    projection = before.projection()
    other = workspace.parent / "other"
    monkeypatch.setattr(
        agent_workspace, "FilePolicy", lambda roots: FilePolicy({"workspace": other})
    )
    after = agent_workspace.build_file_policy()
    assert after.projection()["roots_digest"] != projection["roots_digest"]
    assert not after.matches(projection)
    assert before.matches(projection)


def test_unchanged_mapping_digest_ignores_root_availability(workspace):
    before = agent_workspace.build_file_policy().projection()
    workspace.rmdir()
    missing = agent_workspace.build_file_policy()
    assert missing.projection() == before
    assert missing.matches(before)
    workspace.mkdir()
    (workspace / "note.txt").write_text("new fixture", encoding="utf-8")
    assert agent_workspace.build_file_policy().projection() == before
