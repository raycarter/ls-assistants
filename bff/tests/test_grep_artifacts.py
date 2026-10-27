"""Synthetic grep evidence through the existing text artifact journal and reader."""

import hashlib
import json
from dataclasses import dataclass

import pytest

from app.agent_files import AgentFault
from app.agent_schemas import (
    AgentPlan,
    AgentRun,
    CreateSession,
    FetchMetadata,
    GrepArguments,
    GrepMetadata,
    PlanStep,
    ReadArguments,
    StepResult,
)
from app.sessions import SessionStore
from tests.test_agent import BASE, CREATE
from tests.test_agent import agent_setup as agent_setup  # explicit fixture re-export

NOW = "2026-10-27T12:00:00Z"
RECORDS = [
    {"path": "nested/文.txt", "line": 7, "text": "match 🧪é文" * 14000},
    {"path": "second.txt", "line": 2, "text": "another match"},
]
TEXT = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in RECORDS)


@dataclass
class Document:
    text: str

    @property
    def text_length(self):
        return len(self.text)

    def iter_text(self):
        for start in range(0, len(self.text), 997):
            yield self.text[start : start + 997]


@pytest.fixture
def opened(tmp_path):
    store = SessionStore(tmp_path / "journal", clock=lambda: NOW)
    store.start()
    snapshot = store.create(CreateSession(**CREATE))
    try:
        yield store, snapshot
    finally:
        store.close()


def evidence(store, snapshot, text=TEXT, *, offset=1003, limit=137, complete=True):
    args = GrepArguments(
        workspace_id="synthetic",
        path="nested",
        pattern="match",
        file_glob="*.txt",
        offset=offset,
        limit=limit,
    )
    page, receipt = store.write_artifact(snapshot.id, Document(text), offset, limit)
    metadata = GrepMetadata(
        **page.model_dump(exclude={"content"}),
        workspace_id=args.workspace_id,
        path=args.path,
        pattern=args.pattern,
        file_glob=args.file_glob,
        total_matches=len(RECORDS) if text else 0,
        files_searched=3,
        skipped_binary=0,
        skipped_unreadable=0 if complete else 1,
        complete=complete,
    )
    run = AgentRun(
        id="grep-run",
        request="Search synthetic files",
        created_at=NOW,
        updated_at=NOW,
        status="completed",
        plan=AgentPlan(
            id="grep-plan",
            revision=1,
            digest="a" * 64,
            objective="Search",
            assumptions=[],
            model="deployment-a",
            reasoning_effort="high",
            access_policy={"synthetic": "fixture-only"},
            steps=[
                PlanStep(
                    id="grep-step",
                    title="Grep",
                    description="Search fixture text",
                    tool="grep_in_folder",
                    arguments=args,
                )
            ],
        ),
        results=[
            StepResult(
                step_id="grep-step",
                status="completed",
                content=page.content,
                time=NOW,
                grep=metadata,
                truncated=page.next_offset is not None or not complete,
            )
        ],
    )
    snapshot.runs.append(run)
    return receipt


def commit(store, snapshot, receipt):
    return store.append(
        snapshot,
        "tool.result",
        "synthetic-result",
        run_id="grep-run",
        audit={"step_id": "grep-step", "artifact": receipt},
    ).session


@pytest.mark.parametrize("complete", [True, False])
def test_full_jsonl_unicode_evidence_and_bounded_character_pages(opened, complete):
    store, snapshot = opened
    receipt = evidence(store, snapshot, complete=complete)
    snapshot = commit(store, snapshot, receipt)
    result = snapshot.runs[0].results[0]
    assert result.fetch is None
    assert result.content == TEXT[1003:1140]
    assert len(result.content) == 137
    assert result.grep.total_matches == 2 and result.grep.complete == complete
    assert store.artifacts[snapshot.id][receipt["artifact_id"]].model_dump() == receipt
    assert set(receipt) == {"artifact_id", "sha256", "bytes", "total_chars"}
    assert receipt["sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert receipt["bytes"] > receipt["total_chars"] == len(TEXT)
    parts, offset = [], 0
    while True:
        page = store.read_artifact(snapshot.id, receipt["artifact_id"], offset, 16000)
        assert page.returned_chars == len(page.content) <= 16000
        assert page.content == TEXT[offset : offset + 16000]
        parts.append(page.content)
        if page.next_offset is None:
            break
        assert page.next_offset == offset + page.returned_chars
        offset = page.next_offset
    assert "".join(parts) == TEXT
    assert [json.loads(line) for line in "".join(parts).splitlines()] == RECORDS


@pytest.mark.parametrize(
    "damage",
    [
        "no_proof",
        "wrong_event",
        "wrong_run",
        "wrong_step",
        "wrong_id",
        "duplicate",
        "failed",
        "wrong_tool",
        "no_plan",
        "missing_step",
        "duplicate_step",
        "workspace_id",
        "path",
        "pattern",
        "file_glob",
        "offset",
        "limit",
        "total_chars",
        "content",
        "cursor",
    ],
)
def test_invalid_commits_are_not_published(opened, damage):
    store, snapshot = opened
    receipt = evidence(store, snapshot)
    run = snapshot.runs[0]
    result = run.results[0]
    audit = {"step_id": result.step_id, "artifact": receipt}
    kind, rid = "tool.result", run.id
    if damage == "no_proof":
        audit.pop("artifact")
    elif damage == "wrong_event":
        kind = "run.completed"
    elif damage == "wrong_run":
        other = run.model_copy(deep=True)
        other.id, other.results = "other-run", []
        snapshot.runs.append(other)
        rid = other.id
    elif damage == "wrong_step":
        audit["step_id"] = "other-step"
    elif damage == "wrong_id":
        audit["artifact"] = {**receipt, "artifact_id": "f" * 32}
    elif damage == "duplicate":
        run.results.append(result.model_copy(deep=True))
    elif damage == "failed":
        result.status = "failed"
    elif damage == "wrong_tool":
        run.plan.steps[0].tool = "read_file"
        run.plan.steps[0].arguments = ReadArguments(workspace_id="synthetic", path="nested")
    elif damage == "no_plan":
        run.plan = None
    elif damage == "missing_step":
        run.plan.steps[0].id = "other-step"
    elif damage == "duplicate_step":
        run.plan.steps.append(run.plan.steps[0].model_copy(deep=True))
    elif damage in {"workspace_id", "path", "pattern", "file_glob"}:
        setattr(result.grep, damage, "different")
    elif damage in {"offset", "limit"}:
        setattr(run.plan.steps[0].arguments, damage, 1)
    elif damage == "total_chars":
        result.grep.total_chars += 1
    elif damage == "content":
        result.content = "X" + result.content[1:]
    elif damage == "cursor":
        result.grep.next_offset += 1
    journal = store.root / snapshot.id / "events.jsonl"
    before = journal.read_bytes()
    with pytest.raises(AgentFault):
        store.append(snapshot, kind, "invalid", run_id=rid, audit=audit)
    assert journal.read_bytes() == before
    assert store.artifacts[snapshot.id] == {}


@pytest.mark.parametrize("damage", [None, "no_proof", "wrong_tool", "wrong_args", "duplicate"])
def test_full_snapshot_jsonl_recovery_verifies_grep_binding(opened, damage):
    store, snapshot = opened
    receipt = evidence(store, snapshot)
    snapshot = commit(store, snapshot, receipt)
    records = [record.model_dump(mode="json") for record in store.records[snapshot.id]]
    final = records[-1]
    run = final["event"]["session"]["runs"][0]
    if damage == "no_proof":
        final["audit"].pop("artifact")
    elif damage == "wrong_tool":
        run["plan"]["steps"][0].update(
            tool="read_file", arguments={"workspace_id": "synthetic", "path": "nested"}
        )
    elif damage == "wrong_args":
        run["plan"]["steps"][0]["arguments"]["pattern"] = "unapproved"
    elif damage == "duplicate":
        run["results"].append(run["results"][0])
    store.close()
    journal = store.root / snapshot.id / "events.jsonl"
    raw = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()
    journal.write_bytes(raw)
    recovered = SessionStore(store.root, clock=lambda: NOW)
    try:
        if damage is None:
            recovered.start()
            assert recovered.get(snapshot.id) == snapshot
            page = recovered.read_artifact(snapshot.id, receipt["artifact_id"], 1003, 137)
            assert page.content == TEXT[1003:1140]
            assert journal.read_bytes() == raw
        else:
            with pytest.raises(AgentFault) as failure:
                recovered.start()
            assert failure.value.status == 503
            assert recovered.fd is None and not recovered.artifacts
    finally:
        recovered.close()


def test_legacy_fetch_without_plan_or_new_receipt_fields_still_recovers(opened):
    store, snapshot = opened
    receipt = evidence(store, snapshot)
    run = snapshot.runs[0]
    result = run.results[0]
    page = result.grep
    result.grep = None
    result.fetch = FetchMetadata(
        artifact_id=page.artifact_id,
        requested_url="https://example.test/evidence",
        final_url="https://example.test/evidence",
        title=None,
        status_code=200,
        content_type="text/plain",
        fetched_at=NOW,
        redirects=[],
        total_chars=page.total_chars,
        offset=page.offset,
        returned_chars=page.returned_chars,
        next_offset=page.next_offset,
    )
    run.plan = None
    snapshot = commit(store, snapshot, receipt)
    records = [record.model_dump(mode="json") for record in store.records[snapshot.id]]
    # Historical fetch records predate grep and have no grep field at all.
    records[-1]["event"]["session"]["runs"][0]["results"][0].pop("grep", None)
    store.close()
    journal = store.root / snapshot.id / "events.jsonl"
    raw = "".join(json.dumps(record) + "\n" for record in records).encode()
    journal.write_bytes(raw)
    recovered = SessionStore(store.root, clock=lambda: NOW)
    try:
        recovered.start()
        assert recovered.get(snapshot.id) == snapshot
        assert recovered.artifacts[snapshot.id][receipt["artifact_id"]].model_dump() == receipt
        assert journal.read_bytes() == raw
    finally:
        recovered.close()


def test_receipt_cannot_be_committed_twice(opened):
    store, snapshot = opened
    receipt = evidence(store, snapshot)
    snapshot = commit(store, snapshot, receipt)
    with pytest.raises(AgentFault):
        commit(store, snapshot, receipt)
    assert len(store.artifacts[snapshot.id]) == 1


@pytest.mark.parametrize("replay", [False, True], ids=["append", "restart"])
@pytest.mark.parametrize(
    "change",
    [
        None,
        "run",
        "run_scope",
        "step",
        "step_scope",
        "workspace_id",
        "path",
        "pattern",
        "file_glob",
        "limit",
    ],
)
def test_indexed_artifact_cannot_be_rebound_without_new_proof(opened, replay, change):
    store, snapshot = opened
    receipt = evidence(store, snapshot, "match 🧪é文\n", offset=0)
    snapshot = commit(store, snapshot, receipt)
    binding = store.artifacts[snapshot.id][receipt["artifact_id"]]
    assert (binding.run_id, binding.step_id) == ("grep-run", "grep-step")
    assert binding.grep_arguments == ("synthetic", "nested", "match", "*.txt", 0, 137)
    assert binding.model_dump() == receipt
    records = list(store.records[snapshot.id])
    run = snapshot.runs[0]
    if change in {"run", "run_scope"}:
        run = run.model_copy(deep=True)
        run.id = "second-run"
        snapshot.runs.append(run)
        if change == "run_scope":
            run.plan.steps[0].arguments.pattern = run.results[0].grep.pattern = "different"
    elif change in {"step", "step_scope"}:
        step = run.plan.steps[0].model_copy(deep=True)
        result = run.results[0].model_copy(deep=True)
        step.id = result.step_id = "second-step"
        if change == "step_scope":
            step.arguments.pattern = result.grep.pattern = "different"
        run.plan.steps.append(step)
        run.results.append(result)
    elif change == "limit":
        # Both limits return the same complete page, but are distinct approved arguments.
        run.plan.steps[0].arguments.limit += 1
    elif change is not None:
        setattr(run.plan.steps[0].arguments, change, "different")
        setattr(run.results[0].grep, change, "different")
    journal = store.root / snapshot.id / "events.jsonl"
    before = journal.read_bytes()
    if not replay:
        if change is None:
            event = store.append(snapshot, "tool.result", "unchanged", run_id=run.id)
            assert store.get(snapshot.id) == event.session
        else:
            with pytest.raises(AgentFault) as failure:
                store.append(snapshot, "tool.result", "reuse", run_id=run.id)
            assert failure.value.status == 503
            assert journal.read_bytes() == before
        assert store.artifacts[snapshot.id][receipt["artifact_id"]] == binding
        return
    # Replay a complete later snapshot with no new artifact proof. The original
    # proof must rebuild provenance before this later reference is considered.
    candidate = store._record(snapshot, "tool.result", "reuse", run_id=run.id)
    assert "artifact" not in candidate.audit
    records.append(candidate)
    store.close()
    raw = "".join(record.model_dump_json() + "\n" for record in records).encode()
    journal.write_bytes(raw)
    recovered = SessionStore(store.root, clock=lambda: NOW)
    try:
        if change is None:
            recovered.start()
            assert recovered.get(snapshot.id) == candidate.event.session
            assert recovered.artifacts[snapshot.id][receipt["artifact_id"]] == binding
            assert journal.read_bytes() == raw
        else:
            with pytest.raises(AgentFault) as failure:
                recovered.start()
            assert failure.value.status == 503
            assert recovered.fd is None and not recovered.artifacts
    finally:
        recovered.close()


@pytest.mark.parametrize("empty", [False, True])
def test_restart_preserves_hashes_incremental_storage_and_zero_matches(opened, empty):
    store, snapshot = opened
    text = "" if empty else TEXT
    receipt = evidence(store, snapshot, text, offset=0)
    snapshot = commit(store, snapshot, receipt)
    orphan, _ = store.write_artifact(snapshot.id, Document("uncommitted synthetic text"))
    files = {
        path: path.read_bytes() for path in (store.root / snapshot.id).rglob("*") if path.is_file()
    }
    used, artifact_bytes = store.used, store.artifact_bytes
    store.close()
    recovered = SessionStore(store.root, clock=lambda: NOW)
    try:
        recovered.start()
        assert recovered.get(snapshot.id) == snapshot
        assert recovered.used == used and recovered.artifact_bytes == artifact_bytes
        assert all(path.read_bytes() == raw for path, raw in files.items())
        page = recovered.read_artifact(snapshot.id, receipt["artifact_id"], len(text), 1)
        assert page.content == "" and page.next_offset is None
        if empty:
            result = recovered.get(snapshot.id).runs[0].results[0]
            assert result.grep.total_matches == 0 and result.grep.total_chars == 0
            assert not result.truncated
            assert receipt["sha256"] == hashlib.sha256(b"").hexdigest()
        with pytest.raises(AgentFault) as failure:
            recovered.read_artifact(snapshot.id, orphan.artifact_id)
        assert failure.value.status == 404
    finally:
        recovered.close()


@pytest.mark.parametrize("damage", ["same_length", "missing", "invalid_utf8"])
def test_tampering_fails_live_and_restart_hash_verification(opened, damage):
    store, snapshot = opened
    receipt = evidence(store, snapshot)
    commit(store, snapshot, receipt)
    path = store.root / snapshot.id / "artifacts" / (receipt["artifact_id"] + ".txt")
    raw = path.read_bytes()
    if damage == "missing":
        path.unlink()
    else:
        # Outside the selected result page: full evidence must still be verified.
        path.write_bytes(raw[:-1] + (b"X" if damage == "same_length" else b"\xff"))
    with pytest.raises(AgentFault) as failure:
        store.read_artifact(snapshot.id, receipt["artifact_id"])
    assert failure.value.status == 503 and store.poisoned
    store.close()
    recovered = SessionStore(store.root)
    with pytest.raises(AgentFault) as failure:
        recovered.start()
    assert failure.value.status == 503 and recovered.fd is None


async def test_existing_http_pages_hide_uncommitted_orphan_and_cross_session_evidence(agent_setup):
    async with agent_setup.opened() as (client, owner, _):
        snapshot = owner.store.create(CreateSession(**CREATE))
        receipt = evidence(owner.store, snapshot)
        orphan, _ = owner.store.write_artifact(snapshot.id, Document("orphan"))
        url = f"{BASE}/{snapshot.id}/artifacts/"
        assert (await client.get(url + receipt["artifact_id"])).status_code == 404
        commit(owner.store, snapshot, receipt)
        response = await client.get(url + receipt["artifact_id"] + "?offset=1003&limit=137")
        assert response.status_code == 200
        assert response.json()["content"] == TEXT[1003:1140]
        other = owner.store.create(CreateSession(**{**CREATE, "request_id": "other"}))
        cross_url = f"{BASE}/{other.id}/artifacts/{receipt['artifact_id']}"
        assert (await client.get(cross_url)).status_code == 404
        assert (await client.get(url + orphan.artifact_id)).status_code == 404
        for invalid in ("events.jsonl", "arbitrary.txt", "A" * 32):
            assert (await client.get(url + invalid)).status_code == 422
    async with agent_setup.opened() as (client, _, _):
        assert (await client.get(url + orphan.artifact_id)).status_code == 404
        assert (await client.get(url + receipt["artifact_id"])).status_code == 200
