"""Synthetic tmp_path-only storage contracts, independently of provider/runtime policy."""

import json
import os
from copy import deepcopy
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, Mock

import pytest

import app.sessions as storage
from app.agent_files import AgentFault
from app.agent_llm import AttemptRunner, CanonicalRequest
from app.agent_schemas import AgentEvent, AgentRun, CreateSession, JournalRecord
from app.llm_schemas import LLMIdentity, LLMRecord
from app.session_journal import encode, legacy_state
from app.sessions import SessionStore
from tests.test_session_journal import run as reviewed_run

SID = "20261023T123456123456Z-ab12"
OTHER = "20261023T123456123456Z-cd34"
NOW = "2026-10-23T12:34:56Z"
IDENTITY = {
    "session_id": SID,
    "run_id": "run",
    "operation_id": "operation",
    "plan_id": "candidate",
    "revision": 1,
    "phase": "planning",
    "stage_id": "stage",
    "step_id": None,
    "interaction_id": "interaction",
    "attempt_id": "first",
    "attempt": 1,
    "retry_of": None,
    "backoff_seconds": 0.0,
}
INTENT = {
    "request": {"model": "offline", "input": "Question", "store": False},
    "instructions_version": "v1",
}
OUTPUT = {"index": 1, "kind": "response", "body": {"opaque": ["原文", None, 1]}}
OUTCOME = {
    "status": "completed",
    "dispatched": True,
    "output_count": 1,
    "reported_model": "reported",
    "provider_status": "completed",
    "usage": {"input_tokens": 10},
    "error_code": None,
}
VALID = {"status": "valid", "error_code": None}
INVALID = {"status": "invalid", "error_code": "invalid_plan"}
FAILED = {**OUTCOME, "status": "failed", "error_code": "provider_failure"}
DEFAULTS = {
    "intent": INTENT,
    "dispatched": {},
    "output": OUTPUT,
    "outcome": OUTCOME,
    "validation": VALID,
}


def audit(kind, identity=None, data=None):
    return deepcopy(
        {
            "llm_audit_version": 1,
            "kind": kind,
            "identity": identity or IDENTITY,
            "at": NOW,
            "data": DEFAULTS[kind] if data is None else data,
        }
    )


def emit(store, kind, *, identity=None, data=None):
    binding = identity or IDENTITY
    return store.append(
        store.get(SID),
        "llm." + kind,
        "plan",
        run_id=binding["run_id"],
        operation_id=binding["operation_id"],
        audit=audit(kind, identity, data),
    )


def prefix(store, records):
    for item in records:
        if isinstance(item, str):
            emit(store, item)
        else:
            emit(store, item[0], data=item[1])


@pytest.fixture
def opened(tmp_path):
    store = SessionStore(tmp_path / "journal", clock=lambda: NOW, id_factory=lambda: SID)
    store.start()
    snapshot = store.create(
        CreateSession(request_id="create", model="offline", reasoning_effort="high")
    )
    snapshot.runs.append(
        AgentRun(
            id="run",
            request="Question",
            created_at=NOW,
            updated_at=NOW,
            status="planning",
            operation_id="operation",
        )
    )
    store.append(
        snapshot,
        "run.planning",
        "plan",
        run_id="run",
        operation_id="operation",
        audit={key: IDENTITY[key] for key in ("plan_id", "revision", "phase", "stage_id")},
    )
    try:
        yield store
    finally:
        store.close()


def journal(store):
    return store.root / SID / "events.jsonl"


def payload_images(store):
    folder = store.root / SID / "payloads"
    return {path.name: path.read_bytes() for path in folder.iterdir()} if folder.exists() else {}


def legacy_image(store):
    return b"".join(record.model_dump_json().encode() + b"\n" for record in store.records[SID])


def write_replay_tail(store, entries, mode):
    """Inject synthetic durable records without passing append's validation gate."""
    baseline = store.get(SID)
    image = legacy_image(store) if mode in {"legacy", "mixed"} else journal(store).read_bytes()
    state = (
        legacy_state(baseline) if mode in {"legacy", "mixed"} else store._committed[SID].projection
    )
    store.close()
    for kind, body in entries:
        snapshot = baseline.model_copy(update={"sequence": baseline.sequence + 1}, deep=True)
        record = JournalRecord(
            event=AgentEvent(
                sequence=snapshot.sequence,
                time=NOW,
                session_id=SID,
                request_id="synthetic-replay",
                run_id="run",
                operation_id="operation",
                type="llm." + kind,
                session=snapshot,
            ),
            audit=body,
        )
        if mode == "legacy":
            raw = record.model_dump_json().encode() + b"\n"
        else:
            raw, state, pending = encode(record, state, baseline)
            folder = store.root / SID / "payloads"
            folder.mkdir(exist_ok=True)
            for ref, payload in pending:
                (folder / (ref.id + ".json")).write_bytes(payload)
        image += raw
        baseline = snapshot
    journal(store).write_bytes(image)
    return image


def restart(store):
    store.close()
    result = SessionStore(store.root, clock=lambda: NOW)
    result.start()
    return result


STARTED = ["intent", "dispatched"]
OBSERVED = [*STARTED, "output"]
COMPLETED = [*OBSERVED, "outcome"]


FAILURE_OBSERVATIONS = [
    pytest.param({"failed": True}, id="failed-flag"),
    pytest.param({"kind": "error_body"}, id="error-body"),
    pytest.param({"provider_status": "failed"}, id="provider-failed"),
    pytest.param({"provider_status": "incomplete"}, id="provider-incomplete"),
    pytest.param({"provider_status": "refused"}, id="provider-refused"),
]


@pytest.mark.parametrize("mode", ["append", "v2", "legacy", "mixed"])
@pytest.mark.parametrize("later_success", [False, True])
@pytest.mark.parametrize("failure", FAILURE_OBSERVATIONS)
def test_failed_observation_cannot_be_completed_validated_or_authorize_a_new_send(
    opened,
    monkeypatch,
    mode,
    later_success,
    failure,
):
    store = opened
    prefix(store, STARTED)
    initial = store._committed[SID].llm.attempts["first"]
    emit(store, "output", data={**OUTPUT, **failure})
    if later_success:
        emit(
            store,
            "output",
            data={
                **OUTPUT,
                "index": 2,
                "failed": False,
                "provider_status": "completed",
            },
        )
    old = store._committed[SID]
    assert not initial.failed_observation
    assert old.llm.attempts["first"].failed_observation
    count = 2 if later_success else 1
    outputs = [
        r for r in store.llm_records(SID, "first")["records"] if r["record"]["kind"] == "output"
    ]
    canary = "SYNTHETIC_REJECTED_COMPLETED_OUTCOME"
    completed = {**OUTCOME, "output_count": count, "usage": {"must_not_publish": canary}}
    if mode == "append":
        images, raw, used = payload_images(store), journal(store).read_bytes(), store.used
        writes, publication = Mock(wraps=store._write_payloads), Mock(wraps=store._publish)
        monkeypatch.setattr(store, "_write_payloads", writes)
        monkeypatch.setattr(store, "_publish", publication)
        with pytest.raises(AgentFault) as error:
            emit(store, "outcome", data=completed)
        assert error.value.code == "storage_unavailable" and store.poisoned
        writes.assert_not_called()
        publication.assert_not_called()
        assert store._committed[SID] is old and store.used == used
        with pytest.raises(AgentFault):
            emit(store, "validation", data=VALID)
        with pytest.raises(AgentFault):
            emit(
                store,
                "intent",
                identity={
                    **IDENTITY,
                    "attempt_id": "other",
                    "interaction_id": "other",
                },
            )
        assert payload_images(store) == images and journal(store).read_bytes() == raw
        assert all(canary.encode() not in raw for raw in images.values())
        assert [r.audit for r in store.records[SID] if r.event.type == "llm.output"] == [
            r["record"] for r in outputs
        ]
    else:
        other = {**IDENTITY, "attempt_id": "other", "interaction_id": "other"}
        image = write_replay_tail(
            store,
            [
                ("outcome", audit("outcome", data=completed)),
                ("validation", audit("validation")),
                ("intent", audit("intent", other)),
                ("dispatched", audit("dispatched", other)),
            ],
            mode,
        )
        images = payload_images(store)
        failed = SessionStore(store.root)
        writes = Mock(wraps=failed._write_payloads)
        monkeypatch.setattr(failed, "_write_payloads", writes)
        with pytest.raises(AgentFault):
            failed.start()
        writes.assert_not_called()
        assert failed.poisoned and failed.fd is None and not failed._committed
        assert journal(store).read_bytes() == image and payload_images(store) == images


@pytest.mark.parametrize("mode", ["append", "v2", "legacy", "mixed"])
@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
@pytest.mark.parametrize("failure", FAILURE_OBSERVATIONS)
def test_failed_observations_preserve_non_success_settlements_and_all_private_evidence(
    opened,
    mode,
    status,
    failure,
):
    store = opened
    prefix(store, STARTED)
    emit(store, "output", data={**OUTPUT, **failure})
    emit(
        store,
        "output",
        data={
            **OUTPUT,
            "index": 2,
            "failed": False,
            "provider_status": "completed",
        },
    )
    emit(
        store,
        "outcome",
        data={
            **FAILED,
            "status": status,
            "output_count": 2,
            "dispatched": None if status == "interrupted" else True,
        },
    )
    expected = store.llm_records(SID, "first")["records"]
    assert store._committed[SID].llm.attempts["first"].failed_observation
    if mode != "append":
        write_replay_tail(store, [], mode)
        recovered = restart(store)
        try:
            assert recovered.llm_records(SID, "first")["records"] == expected
            assert recovered._committed[SID].llm.attempts["first"].failed_observation
            assert not recovered._committed[SID].llm.pending
        finally:
            recovered.close()


def test_failure_shaped_opaque_body_is_not_a_failed_observation(opened):
    prefix(opened, STARTED)
    body = {"failed": True, "kind": "error_body", "provider_status": "refused"}
    emit(opened, "output", data={**OUTPUT, "body": body})
    emit(opened, "outcome")
    emit(opened, "validation")
    assert not opened._committed[SID].llm.attempts["first"].failed_observation
    other = {**IDENTITY, "attempt_id": "other", "interaction_id": "other"}
    emit(opened, "intent", identity=other)
    emit(opened, "dispatched", identity=other)
    assert opened.llm_records(SID, "first")["records"][2]["record"]["data"]["body"] == body


BAD_TRANSITIONS = [
    ([], "dispatched", None, {}),
    ([], "output", None, {}),
    ([], "outcome", None, {}),
    ([], "validation", None, {}),
    (["intent"], "intent", None, {}),
    (["intent"], "output", None, {}),
    (STARTED, "dispatched", None, {}),
    (STARTED, "output", {**OUTPUT, "index": 2}, {}),
    (OBSERVED, "output", OUTPUT, {}),
    (OBSERVED, "outcome", {**OUTCOME, "output_count": 0}, {}),
    (OBSERVED, "outcome", {**OUTCOME, "output_count": 2}, {}),
    (COMPLETED, "outcome", None, {}),
    (COMPLETED, "output", {**OUTPUT, "index": 2}, {}),
    (COMPLETED, "dispatched", None, {}),
    (["intent"], "outcome", {**OUTCOME, "output_count": 0}, {}),
    (OBSERVED, "outcome", {**OUTCOME, "dispatched": False}, {}),
    (STARTED, "outcome", {**OUTCOME, "dispatched": False, "output_count": 0}, {}),
    (OBSERVED, "outcome", {**OUTCOME, "dispatched": None}, {}),
    (OBSERVED, "outcome", {**OUTCOME, "status": "not_dispatched"}, {}),
    (OBSERVED, "validation", None, {}),
    ([*OBSERVED, ("outcome", FAILED)], "validation", None, {}),
    ([*STARTED, ("outcome", {**OUTCOME, "output_count": 0})], "validation", None, {}),
    ([*COMPLETED, "validation"], "validation", None, {}),
    (COMPLETED, "validation", {"status": "valid", "error_code": "invalid_plan"}, {}),
    ([], "intent", {**INTENT, "headers": {}}, {}),
    ([], "intent", None, {"identity": {**IDENTITY, "session_id": OTHER}}),
    ([], "intent", None, {"identity": {**IDENTITY, "run_id": "other"}}),
    ([], "intent", None, {"identity": {**IDENTITY, "operation_id": "other"}}),
    ([], "intent", None, {"identity": {**IDENTITY, "revision": 2}}),
    ([], "intent", None, {"identity": {**IDENTITY, "plan_id": "other"}}),
    ([], "intent", None, {"identity": {**IDENTITY, "stage_id": "other"}}),
    ([], "intent", None, {"identity": {**IDENTITY, "attempt": True}}),
    ([], "intent", None, {"identity": {**IDENTITY, "backoff_seconds": -0.1}}),
    ([], "intent", None, {"identity": {**IDENTITY, "backoff_seconds": 0.1}}),
    ([], "intent", None, {"identity": {**IDENTITY, "phase": "execution", "step_id": "s"}}),
    ([], "intent", None, {"llm_audit_version": True}),
    ([], "intent", None, {"llm_audit_version": 2}),
    ([], "intent", None, {"at": "2026-10-23T10:00:00+01:00"}),
    ([], "intent", {}, {"kind": "dispatched"}),
    (["intent"], "dispatched", None, {"identity": {**IDENTITY, "plan_id": "other"}}),
    (["intent"], "dispatched", None, {"identity": {**IDENTITY, "stage_id": "other"}}),
    (["intent"], "dispatched", None, {"identity": {**IDENTITY, "interaction_id": "other"}}),
    (["intent"], "dispatched", None, {"identity": {**IDENTITY, "attempt_id": "other"}}),
]


@pytest.mark.parametrize("mode", ["append", "legacy", "mixed"])
@pytest.mark.parametrize("before,kind,data,changes", BAD_TRANSITIONS)
def test_invalid_lifecycle_fails_closed_on_append_and_replay(
    opened,
    mode,
    before,
    kind,
    data,
    changes,
):
    store = opened
    prefix(store, before)
    bad = audit(kind, data=data)
    bad.update(changes)
    old = store._committed[SID]
    raw = journal(store).read_bytes()
    if mode == "append":
        with pytest.raises(AgentFault) as failure:
            store.append(
                store.get(SID),
                "llm." + kind,
                "plan",
                run_id="run",
                operation_id="operation",
                audit=bad,
            )
        assert failure.value.code == "storage_unavailable" and store.poisoned
        store.poisoned = False
        assert store.poisoned and store._committed[SID] is old
        assert journal(store).read_bytes() == raw
    else:
        record = store._record(
            store.get(SID),
            "llm." + kind,
            "plan",
            run_id="run",
            operation_id="operation",
            audit=bad,
        )
        image = legacy_image(store) if mode == "legacy" else raw
        store.close()
        image += record.model_dump_json().encode() + b"\n"
        journal(store).write_bytes(image)
        failed = SessionStore(store.root)
        with pytest.raises(AgentFault) as failure:
            failed.start()
        assert failure.value.code == "storage_unavailable" and failed.poisoned
        assert failed.fd is None and not failed.records
        assert journal(store).read_bytes() == image


@pytest.mark.parametrize("kind", ["intent", "dispatched", "output", "outcome", "validation"])
def test_every_llm_kind_requires_explicit_version(opened, kind):
    before = {
        "intent": [],
        "dispatched": ["intent"],
        "output": STARTED,
        "outcome": OBSERVED,
        "validation": COMPLETED,
    }[kind]
    prefix(opened, before)
    body = audit(kind)
    del body["llm_audit_version"]
    with pytest.raises(AgentFault):
        opened.append(
            opened.get(SID),
            "llm." + kind,
            "plan",
            run_id="run",
            operation_id="operation",
            audit=body,
        )


@pytest.mark.parametrize("mode", ["append", "legacy", "mixed"])
@pytest.mark.parametrize(
    "bad",
    [
        "headers",
        "api_key",
        "credentials",
        "settings",
        "store_true",
        "store_missing",
        "envelope",
        "data",
        "binding",
        "candidate",
        "state",
        "output_order",
        "output_metadata",
    ],
)
def test_invalid_body_envelope_or_lifecycle_cannot_write_sensitive_orphans(
    opened,
    monkeypatch,
    mode,
    bad,
):
    store = opened
    canary = "SYNTHETIC_FORBIDDEN_PAYLOAD_CANARY_原文"
    snapshot = store.get(SID)
    kind = "intent"
    body = audit(
        "intent",
        data={
            **INTENT,
            "request": {**INTENT["request"], "input": {"user_text": canary}},
        },
    )
    if bad in {"headers", "api_key", "credentials", "settings"}:
        body["data"]["request"][bad] = {"forbidden_configuration": canary}
    elif bad == "store_true":
        body["data"]["request"]["store"] = True
    elif bad == "store_missing":
        del body["data"]["request"]["store"]
    elif bad == "envelope":
        body["headers"] = {"Authorization": canary}
    elif bad == "data":
        body["data"]["headers"] = {"Authorization": canary}
    elif bad == "binding":
        body["identity"]["operation_id"] = "not-this-operation"
    elif bad == "candidate":
        body["identity"]["plan_id"] = "not-this-candidate"
    elif bad == "state":
        snapshot.runs[0].answer = canary
    else:
        prefix(store, STARTED if bad == "output_metadata" else ["intent"])
        snapshot = store.get(SID)
        kind = "output"
        body = audit("output", data={**OUTPUT, "body": canary})
        if bad == "output_metadata":
            body["data"]["failed"] = "not-a-boolean"
    old, used = store._committed[SID], store.used
    images = payload_images(store)
    raw = journal(store).read_bytes()
    if mode == "append":
        writes = Mock(wraps=store._write_payloads)
        publication = Mock(wraps=store._publish)
        monkeypatch.setattr(store, "_write_payloads", writes)
        monkeypatch.setattr(store, "_publish", publication)
        with pytest.raises(AgentFault) as failure:
            store.append(
                snapshot,
                "llm." + kind,
                "bad",
                run_id="run",
                operation_id="operation",
                audit=body,
            )
        assert failure.value.code == "storage_unavailable"
        writes.assert_not_called()
        publication.assert_not_called()
        assert store._committed[SID] is old and store.used == used
        assert journal(store).read_bytes() == raw
        store.poisoned = False
        assert store.poisoned
        with pytest.raises(AgentFault):
            store.append(snapshot, "session.updated", "after-failure")
    else:
        record = store._record(
            snapshot,
            "llm." + kind,
            "bad",
            run_id="run",
            operation_id="operation",
            audit=body,
        )
        raw = legacy_image(store) if mode == "legacy" else raw
        store.close()
        raw += record.model_dump_json().encode() + b"\n"
        journal(store).write_bytes(raw)
        failed = SessionStore(store.root)
        with pytest.raises(AgentFault) as failure:
            failed.start()
        assert failure.value.code == "storage_unavailable"
        assert not failed._committed and failed.fd is None and failed.poisoned
        assert journal(store).read_bytes() == raw
    assert payload_images(store) == images
    assert all(canary.encode() not in image for image in payload_images(store).values())


def test_preflight_validates_detached_evidence_even_if_caller_mutates_after_serialization(
    opened,
    monkeypatch,
):
    body = audit("intent")
    body["data"]["request"]["headers"] = {"Authorization": "SYNTHETIC_NEVER_WRITE"}
    encode = opened._encode
    before, images = opened._committed[SID], payload_images(opened)

    def encode_then_change_caller(record):
        result = encode(record)
        del body["data"]["request"]["headers"]
        return result

    monkeypatch.setattr(opened, "_encode", encode_then_change_caller)
    with pytest.raises(AgentFault):
        opened.append(
            opened.get(SID),
            "llm.intent",
            "plan",
            run_id="run",
            operation_id="operation",
            audit=body,
        )
    assert "headers" not in body["data"]["request"]
    assert opened._committed[SID] is before and opened.poisoned
    assert payload_images(opened) == images


@pytest.mark.parametrize("mode", ["append", "v2", "legacy", "mixed"])
def test_credential_shaped_user_content_and_schema_property_names_are_preserved(opened, mode):
    content = {
        "headers": {"Authorization": "Bearer SYNTHETIC_USER_TEXT"},
        "api_key": "SYNTHETIC_SOURCE_TEXT",
        "credentials": ["source", "原文"],
        "settings": {"store": True},
    }
    request = {
        **INTENT["request"],
        "input": [content, "headers/api_key/credentials/settings in source"],
        "instructions": "Source text mentions headers, api_key, credentials and settings.",
        "reasoning": {"effort": "high", "summary": "auto"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "credential_properties",
                "schema": {
                    "type": "object",
                    "properties": {
                        name: {"type": "string", "description": "SYNTHETIC_SOURCE_TEXT"}
                        for name in content
                    },
                },
            }
        },
    }
    emit(opened, "intent", data={**INTENT, "request": request})
    emit(opened, "dispatched")
    emit(opened, "output", data={**OUTPUT, "body": content})
    records = opened.llm_records(SID, "first")["records"]
    assert records[0]["record"]["data"]["request"] == request
    assert records[-1]["record"]["data"]["body"] == content
    assert not opened.poisoned
    if mode != "append":
        write_replay_tail(opened, [], mode)
        recovered = restart(opened)
        try:
            assert recovered.llm_records(SID, "first")["records"][:3] == records
        finally:
            recovered.close()


@pytest.mark.parametrize("mode", ["append", "v2", "legacy", "mixed"])
@pytest.mark.parametrize("container", [("reasoning",), ("text",), ("text", "format")])
@pytest.mark.parametrize("field", ["headers", "api_key", "credentials", "settings"])
def test_raw_intent_rejects_nested_option_configuration_without_forbidden_orphans(
    opened,
    monkeypatch,
    mode,
    container,
    field,
):
    canary = "SYNTHETIC_NESTED_OPTION_CONFIGURATION"
    request = {
        **INTENT["request"],
        "reasoning": {"effort": "high"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "source",
                "schema": {"type": "object", "properties": {"api_key": {"type": "string"}}},
            }
        },
    }
    target = request
    for part in container:
        target = target[part]
    target[field] = {"forbidden_configuration": canary}
    body = audit("intent", data={**INTENT, "request": request})
    if mode == "append":
        old, images, raw = (
            opened._committed[SID],
            payload_images(opened),
            journal(opened).read_bytes(),
        )
        writes, publication = Mock(wraps=opened._write_payloads), Mock(wraps=opened._publish)
        monkeypatch.setattr(opened, "_write_payloads", writes)
        monkeypatch.setattr(opened, "_publish", publication)
        # Deliberately bypass CanonicalRequest to exercise the storage boundary.
        with pytest.raises(AgentFault):
            opened.append(
                opened.get(SID),
                "llm.intent",
                "raw",
                run_id="run",
                operation_id="operation",
                audit=body,
            )
        writes.assert_not_called()
        publication.assert_not_called()
        assert opened.poisoned and opened._committed[SID] is old
        assert journal(opened).read_bytes() == raw and payload_images(opened) == images
        assert all(canary.encode() not in raw for raw in images.values())
    else:
        image = write_replay_tail(opened, [("intent", body)], mode)
        images = payload_images(opened)
        failed = SessionStore(opened.root)
        writes = Mock(wraps=failed._write_payloads)
        monkeypatch.setattr(failed, "_write_payloads", writes)
        with pytest.raises(AgentFault):
            failed.start()
        writes.assert_not_called()
        assert failed.poisoned and failed.fd is None and not failed._committed
        assert journal(opened).read_bytes() == image and payload_images(opened) == images


def retry_identity(**updates):
    return {
        **IDENTITY,
        "attempt_id": "second",
        "attempt": 2,
        "retry_of": "first",
        "backoff_seconds": 0.25,
        **updates,
    }


@pytest.mark.parametrize("mode", ["append", "legacy", "mixed"])
@pytest.mark.parametrize(
    "bad",
    [
        "pending",
        "unvalidated",
        "valid",
        "cancelled",
        "interrupted",
        "not_dispatched",
        "missing",
        "fork",
        "skip",
        "interaction",
        "candidate",
        "stage",
        "reuse",
        "backoff",
    ],
)
def test_retry_requires_settled_same_interaction_and_nonforking_lineage(opened, mode, bad):
    store = opened
    prefix(store, OBSERVED if bad != "not_dispatched" else ["intent"])
    if bad not in {"pending", "not_dispatched"}:
        status = {
            "unvalidated": "completed",
            "valid": "completed",
            "cancelled": "cancelled",
            "interrupted": "interrupted",
        }.get(bad, "failed")
        emit(store, "outcome", data={**FAILED, "status": status})
        if bad == "valid":
            emit(store, "validation")
    elif bad == "not_dispatched":
        emit(
            store,
            "outcome",
            data={
                **FAILED,
                "status": "not_dispatched",
                "dispatched": False,
                "output_count": 0,
            },
        )
    ident = retry_identity()
    ident.update(
        {
            "missing": {"retry_of": "missing"},
            "skip": {"attempt": 3},
            "interaction": {"interaction_id": "different"},
            "candidate": {"plan_id": "other"},
            "stage": {"stage_id": "other"},
            "reuse": {"attempt_id": "first"},
            "backoff": {"backoff_seconds": -1.0},
        }.get(bad, {})
    )
    if bad == "fork":
        emit(store, "intent", identity=ident)
        emit(
            store,
            "outcome",
            identity=ident,
            data={
                **FAILED,
                "status": "not_dispatched",
                "dispatched": False,
                "output_count": 0,
            },
        )
        ident = retry_identity(attempt_id="fork")
    old = store._committed[SID]
    raw = legacy_image(store) if mode == "legacy" else journal(store).read_bytes()
    if mode == "append":
        with pytest.raises(AgentFault):
            emit(store, "intent", identity=ident)
        assert store._committed[SID] is old
    else:
        record = store._record(
            store.get(SID),
            "llm.intent",
            "plan",
            run_id="run",
            operation_id="operation",
            audit=audit("intent", ident),
        )
        store.close()
        journal(store).write_bytes(raw + record.model_dump_json().encode() + b"\n")
        failed = SessionStore(store.root)
        with pytest.raises(AgentFault):
            failed.start()
        assert failed.fd is None


@pytest.mark.parametrize("what", ["candidate", "stage", "stage_reuse", "unsettled", "unvalidated"])
def test_new_interaction_cannot_change_candidate_stage_or_dispatch_past_missing_recording(
    opened,
    what,
):
    store = opened
    prefix(store, OBSERVED)
    if what != "unsettled":
        emit(store, "outcome", data=OUTCOME if what == "unvalidated" else FAILED)
    ident = {**IDENTITY, "attempt_id": "other", "interaction_id": "other-interaction"}
    ident.update(
        {
            "candidate": {"plan_id": "other"},
            "stage": {"stage_id": "other"},
            "stage_reuse": {"phase": "synthesis"},
        }.get(what, {})
    )
    if what in {"unsettled", "unvalidated"}:
        emit(store, "intent", identity=ident)
        with pytest.raises(AgentFault):
            emit(store, "dispatched", identity=ident)
    else:
        with pytest.raises(AgentFault):
            emit(store, "intent", identity=ident)


@pytest.mark.parametrize("kind", ["intent", "dispatched", "output", "outcome", "validation"])
@pytest.mark.parametrize("field", ["status", "plan", "request", "answer", "archive", "other_run"])
def test_audit_cannot_advance_or_mutate_coordinator_state(opened, kind, field):
    store = opened
    prefix(
        store,
        {
            "intent": [],
            "dispatched": ["intent"],
            "output": STARTED,
            "outcome": OBSERVED,
            "validation": COMPLETED,
        }[kind],
    )
    snapshot = store.get(SID)
    run = snapshot.runs[0]
    if field == "status":
        run.status = "awaiting_approval"
    elif field == "plan":
        run.plan = reviewed_run().plan
    elif field == "archive":
        snapshot.archived = True
    elif field == "other_run":
        snapshot.runs.append(reviewed_run("other"))
    else:
        setattr(run, field, "forged")
    with pytest.raises(AgentFault):
        store.append(
            snapshot,
            "llm." + kind,
            "plan",
            run_id="run",
            operation_id="operation",
            audit=audit(kind),
        )


@pytest.mark.parametrize("status", ["cancelling", "cancelled", "interrupted"])
def test_stop_revokes_dispatch_but_late_output_and_outcome_still_settle(opened, status):
    store = opened
    prefix(store, STARTED)
    snapshot = store.get(SID)
    snapshot.runs[0].status = status
    if status != "cancelling":
        snapshot.runs[0].operation_id = None
    store.append(snapshot, "run." + status, "stop", run_id="run", operation_id="operation")
    emit(store, "output")
    final = emit(store, "outcome", data={**OUTCOME, "status": "cancelled"})
    assert final.session.runs[0].status == status
    assert final.session.runs[0].plan is None
    ident = {**IDENTITY, "attempt_id": "other", "interaction_id": "other"}
    with pytest.raises(AgentFault):
        emit(store, "intent", identity=ident)


@pytest.mark.parametrize("marker", [False, True])
def test_not_dispatched_can_follow_authorization_marker_without_claiming_remote_execution(
    opened,
    marker,
):
    emit(opened, "intent")
    if marker:
        emit(opened, "dispatched")
    emit(
        opened,
        "outcome",
        data={
            **OUTCOME,
            "status": "not_dispatched",
            "dispatched": False,
            "output_count": 0,
            "reported_model": None,
            "provider_status": None,
            "usage": None,
            "error_code": "dispatch_blocked",
        },
    )
    assert not opened._committed[SID].llm.pending


@pytest.mark.parametrize("mode", ["v2", "legacy", "mixed"])
@pytest.mark.parametrize("stop_at", ["intent", "dispatched", "partial", "outcome", "validation"])
def test_recovery_settles_only_unmatched_intents_once_without_outputs_usage_or_validation(
    opened,
    mode,
    stop_at,
):
    store = opened
    if mode == "mixed":
        image = legacy_image(store)
        store.close()
        journal(store).write_bytes(image)
        store = restart(store)
        # The synthetic operation was interrupted by startup: admit a fresh one.
        snapshot = store.get(SID)
        snapshot.runs[0].status = "planning"
        snapshot.runs[0].operation_id = "operation"
        snapshot.runs[0].error = None
        store.append(snapshot, "run.planning", "new", run_id="run", operation_id="operation")
    emit(store, "intent")
    count = 0
    if stop_at != "intent":
        emit(store, "dispatched")
    if stop_at in {"partial", "outcome", "validation"}:
        emit(store, "output")
        emit(store, "output", data={"index": 2, "kind": "event", "body": "second chunk"})
        count = 2
    if stop_at in {"outcome", "validation"}:
        emit(store, "outcome", data={**OUTCOME, "output_count": count})
    if stop_at == "validation":
        emit(store, "validation")
    original = legacy_image(store) if mode == "legacy" else journal(store).read_bytes()
    sequences = store.get(SID).sequence
    store.close()
    journal(store).write_bytes(original)
    recovered = SessionStore(store.root, clock=lambda: NOW)
    recovered.start()
    try:
        run = recovered.get(SID).runs[0]
        assert run.status == "interrupted" and run.plan is None and run.operation_id is None
        assert run.error.code == "outcome_unknown"
        attempt = recovered.llm_attempts(SID, "run")["attempts"][0]
        assert attempt["identity"] == IDENTITY and attempt["output_count"] == count
        rows = recovered.llm_records(SID, "first")["records"]
        bodies = [row["record"] for row in rows]
        outcomes = [body for body in bodies if body["kind"] == "outcome"]
        assert len(outcomes) == 1
        if stop_at in {"intent", "dispatched", "partial"}:
            assert outcomes[0]["data"] == {
                "status": "interrupted",
                "dispatched": False if stop_at == "intent" else None,
                "output_count": count,
                "reported_model": None,
                "provider_status": None,
                "usage": None,
                "error_code": "outcome_unknown",
            }
        else:
            assert outcomes[0]["data"] == {**OUTCOME, "output_count": count}
        assert sum(body["kind"] == "output" for body in bodies) == count
        assert sum(body["kind"] == "validation" for body in bodies) == (stop_at == "validation")
        assert not recovered._committed[SID].llm.pending
        extra_outcome = stop_at not in {"outcome", "validation"}
        assert recovered.get(SID).sequence == sequences + 1 + extra_outcome
        last = recovered.records[SID][-1]
        assert last.event.type == "run.interrupted" and last.event.run_id is None
        assert last.event.operation_id is None and last.audit == {"outcome": "unknown"}
        public = recovered.public_events(SID, 0, limit=100)
        assert public["events"][-1]["schema_version"] == 2
        assert public["events"][-1]["invalidates_runs"] is True
        assert "instructions_version" not in json.dumps(public)
        committed = journal(recovered).read_bytes()
        images = {p.name: p.read_bytes() for p in (store.root / SID / "payloads").iterdir()}
    finally:
        recovered.close()
    again = SessionStore(store.root, clock=lambda: NOW)
    again.start()
    try:
        assert journal(again).read_bytes() == committed
        assert again.llm_records(SID, "first")["records"] == rows
        assert {p.name: p.read_bytes() for p in (store.root / SID / "payloads").iterdir()} == images
    finally:
        again.close()


@pytest.mark.parametrize("mode", ["v2", "legacy", "mixed"])
def test_output_only_crash_retains_exact_observations_and_latest_known_metadata(
    opened,
    monkeypatch,
    mode,
):
    store = opened
    if mode == "mixed":
        image = legacy_image(store)
        store.close()
        journal(store).write_bytes(image)
        store = restart(store)
        snapshot = store.get(SID)
        run = snapshot.runs[0]
        run.status, run.operation_id, run.error = "planning", "operation", None
        store.append(
            snapshot, "run.planning", "resume-fixture", run_id="run", operation_id="operation"
        )
    usage = {"tokens": 2, "opaque": [None, "原文"], "memo": "USAGE_METADATA_CANARY" * 1000}
    observations = [
        {"kind": "event", "body": {"model": "not-observation-metadata", "usage": {"tokens": 999}}},
        {
            "kind": "event",
            "body": "first",
            "reported_model": "model-a",
            "provider_status": "created",
            "usage": {"tokens": 1},
            "failed": False,
        },
        {
            "kind": "event",
            "body": "second",
            "reported_model": "model-b",
            "provider_status": None,
            "usage": usage,
            "failed": True,
        },
        {
            "kind": "response",
            "body": "OPAQUE_RESPONSE_BODY_CANARY" * 1000,
            "reported_model": None,
            "provider_status": "failed",
            "usage": None,
            "failed": True,
        },
        # Old version-one chunks may omit all optional metadata. A later body
        # cannot erase earlier known values or become an inferred metadata source.
        {
            "kind": "event",
            "body": {
                "reported_model": "fake",
                "provider_status": "fake",
                "usage": {"tokens": 9999},
                "failed": False,
            },
        },
    ]
    prefix(store, STARTED)
    outputs = [{"index": n, **body} for n, body in enumerate(observations, 1)]
    for body in outputs:
        emit(store, "output", data=body)
    before = store.llm_records(SID, "first")["records"]
    assert [r["record"]["data"] for r in before if r["record"]["kind"] == "output"] == outputs
    image = legacy_image(store) if mode == "legacy" else journal(store).read_bytes()
    store.close()
    journal(store).write_bytes(image)
    recovered = SessionStore(store.root, clock=lambda: NOW)
    recover = recovered._llm.recovery_record
    recoveries = []

    def selected_metadata_only(sid, aid, at):
        committed = recovered._committed[SID]
        attempt = committed.llm.attempts[aid]
        sources = dict(attempt.metadata)
        assert sources["reported_model"] == sources["usage"]
        assert sources["provider_status"] != sources["usage"]
        assert "USAGE_METADATA_CANARY" not in repr(attempt)
        assert "OPAQUE_RESPONSE_BODY_CANARY" not in repr(attempt)
        payloads = Mock(wraps=recovered._read_payload)
        records = Mock(wraps=recovered._record_bytes)
        with monkeypatch.context() as patch:
            patch.setattr(recovered, "_read_payload", payloads)
            patch.setattr(recovered, "_record_bytes", records)
            patch.setattr(
                recovered, "_history", Mock(side_effect=AssertionError("No prefix replay"))
            )
            patch.setattr(storage, "decode", Mock(side_effect=AssertionError("No prefix decode")))
            result = recover(sid, aid, at)
        unique = set(sources.values())
        if mode == "legacy":
            payloads.assert_not_called()
            assert records.call_count == 2
            assert {call.args[1] for call in records.call_args_list} == {
                committed.locations[source.sequence - 1] for source in unique
            }
        else:
            records.assert_not_called()
            assert payloads.call_count == 2
            assert {call.args[1].id for call in payloads.call_args_list} == {
                source.payload[0] for source in unique
            }
        recoveries.append(aid)
        return result

    monkeypatch.setattr(recovered._llm, "recovery_record", selected_metadata_only)
    recovered.start()
    try:
        rows = recovered.llm_records(SID, "first")["records"]
        assert rows[:-1] == before
        assert rows[-1]["record"]["data"] == {
            "status": "interrupted",
            "dispatched": None,
            "output_count": len(outputs),
            "reported_model": "model-b",
            "provider_status": "failed",
            "usage": usage,
            "error_code": "outcome_unknown",
        }
        assert recovered.get(SID).runs[0].status == "interrupted"
        assert recovered.get(SID).runs[0].plan is None
        assert recoveries == ["first"]
        committed_image, images = journal(recovered).read_bytes(), payload_images(recovered)
        assert "USAGE_METADATA_CANARY" not in repr(recovered._committed[SID].llm)
    finally:
        recovered.close()
    again = SessionStore(store.root, clock=lambda: NOW)
    monkeypatch.setattr(
        again._llm, "recovery_record", Mock(side_effect=AssertionError("Already settled"))
    )
    again.start()
    try:
        assert again.llm_records(SID, "first")["records"] == rows
        assert journal(again).read_bytes() == committed_image
        assert payload_images(again) == images
    finally:
        again.close()


@pytest.mark.parametrize("usage", [0, False, {}, []])
def test_recovery_retains_known_falsey_metadata_without_body_inference(opened, usage):
    prefix(opened, STARTED)
    emit(
        opened,
        "output",
        data={
            **OUTPUT,
            "reported_model": "",
            "provider_status": "",
            "usage": usage,
            "failed": False,
        },
    )
    emit(opened, "output", data={**OUTPUT, "index": 2, "body": {"usage": "not metadata"}})
    outcome = opened._llm.recovery_record(SID, "first", NOW)
    assert outcome.data["reported_model"] == "" and outcome.data["provider_status"] == ""
    assert outcome.data["usage"] == usage and type(outcome.data["usage"]) is type(usage)
    assert outcome.data["status"] == "interrupted"
    assert outcome.data["error_code"] == "outcome_unknown"


def test_recovery_audit_settlement_preserves_existing_approval_and_digest(opened):
    prefix(opened, OBSERVED)
    snapshot = opened.get(SID)
    run = snapshot.runs[0]
    run.status, run.operation_id, run.plan = "awaiting_approval", None, reviewed_run().plan
    opened.append(
        snapshot, "run.awaiting_approval", "review", run_id="run", operation_id="operation"
    )
    recovered = restart(opened)
    try:
        current = recovered.get(SID).runs[0]
        assert current.status == "awaiting_approval" and current.plan == run.plan
        assert current.plan.digest == run.plan.digest
        assert recovered.records[SID][-1].event.type == "llm.outcome"
        assert recovered.llm_attempts(SID, "run")["attempts"][0]["validation"] is None
    finally:
        recovered.close()


def test_legacy_opaque_audits_never_become_provider_attempts_or_change_digests(opened):
    for n, body in enumerate(
        [
            {"kind": "intent", "identity": IDENTITY, "request": "opaque"},
            {"llm_audit_version": True, "kind": "output", "data": {"index": "opaque"}},
        ]
    ):
        opened.append(
            opened.get(SID),
            "planner.attempt",
            f"logical-{n}",
            run_id="run",
            operation_id="operation",
            audit=body,
        )
    snapshot = opened.get(SID)
    snapshot.runs[0].status, snapshot.runs[0].operation_id = "awaiting_approval", None
    snapshot.runs[0].plan = reviewed_run().plan
    opened.append(snapshot, "run.awaiting_approval", "review", run_id="run")
    image = legacy_image(opened)
    expected = list(opened.records[SID])
    opened.close()
    journal(opened).write_bytes(image)
    for _ in range(2):
        recovered = restart(opened)
        try:
            assert recovered.attempt_count(SID, "run") == 2
            assert recovered.llm_attempts(SID, "run")["attempts"] == []
            assert list(recovered.records[SID]) == expected
            assert journal(recovered).read_bytes() == image
            assert recovered.get(SID).runs[0].plan.digest == snapshot.runs[0].plan.digest
        finally:
            recovered.close()


def test_bounded_readers_use_only_selected_refs_and_return_detached_full_canonical_bodies(
    opened,
    monkeypatch,
):
    store = opened
    large = "原文🌱 opaque+/== " * 40000
    request = {"model": "offline", "input": [{"encrypted_content": large}], "store": False}
    emit(store, "intent", data={**INTENT, "request": request})
    emit(store, "dispatched")
    chunks = ["first", large, {"delta": "last", "opaque": [None, 1, "終"]}]
    for n, body in enumerate(chunks, 1):
        emit(store, "output", data={"index": n, "kind": "event", "body": body})
    emit(store, "outcome", data={**FAILED, "output_count": 3, "usage": {"opaque": large}})
    second = retry_identity()
    for kind in ["intent", "dispatched", "output", "outcome"]:
        emit(store, kind, identity=second)
    emit(store, "validation", identity=second, data=INVALID)
    third = retry_identity(attempt_id="third", attempt=3, retry_of="second", backoff_seconds=0.0)
    for kind in ["intent", "dispatched", "output", "outcome", "validation"]:
        emit(store, kind, identity=third)
    old = store._committed[SID]
    read = Mock(wraps=store._read_payload)
    monkeypatch.setattr(store, "_read_payload", read)
    monkeypatch.setattr(store, "_history", Mock(side_effect=AssertionError("No history replay")))
    monkeypatch.setattr(storage, "decode", Mock(side_effect=AssertionError("No codec replay")))
    page = store.llm_attempts(SID, "run", limit=2)
    read.assert_not_called()
    assert [a["identity"]["attempt_id"] for a in page["attempts"]] == ["third", "second"]
    assert page["total"] == 3 and page["next_before"] == 1
    older = store.llm_attempts(SID, "run", before=1)
    assert older["attempts"][0]["identity"]["attempt_id"] == "first"
    assert older["next_before"] is None
    assert "opaque+/==" not in json.dumps(older)
    assert "opaque+/==" not in repr(old.llm)
    assert store.attempt_count(SID, "run") == 0  # Never counts physical provider sends.
    small = store.llm_records(SID, "first", limit=2)
    assert read.call_count == 2 and len(small["records"]) == 2 and small["has_more"]
    assert small["records"][0]["record"]["data"]["request"] == request
    first_ref = old.llm.attempts["first"].records[0].payload
    assert [call.args[1].id for call in read.call_args_list][0] == first_ref[0]
    small["records"][0]["record"]["data"]["request"]["input"].clear()
    page["attempts"][0]["identity"]["plan_id"] = "caller mutation"
    read.reset_mock()
    rest = store.llm_records(SID, "first", after=small["next_after"], limit=100)
    assert read.call_count == 4 and not rest["has_more"]
    assert [row["record"]["data"]["body"] for row in rest["records"][:-1]] == chunks
    refs = [loc.payload[0] for loc in old.llm.attempts["first"].records]
    assert len(set(refs)) == 6  # No output payload is a cumulative snapshot.
    assert all(LLMRecord.model_validate(row["record"]) for row in rest["records"])
    assert store.llm_records(SID, "first", limit=1)["records"][0]["record"]["data"] == {
        **INTENT,
        "request": request,
    }
    assert store.llm_attempts(SID, "run")["attempts"][0]["identity"]["plan_id"] == "candidate"
    assert [row["sequence"] for row in rest["records"]] == sorted(
        row["sequence"] for row in rest["records"]
    )
    with pytest.raises(TypeError):
        old.llm.attempts["injected"] = old.llm.attempts["first"]
    with pytest.raises(FrozenInstanceError):
        old.llm.attempts["first"].output_count = 100


def test_legacy_private_reader_selects_one_exact_record_not_prefix(opened, monkeypatch):
    prefix(opened, [*COMPLETED, "validation"])
    image = legacy_image(opened)
    opened.close()
    journal(opened).write_bytes(image)
    store = restart(opened)
    try:
        read = Mock(wraps=store._record_bytes)
        monkeypatch.setattr(store, "_record_bytes", read)
        monkeypatch.setattr(store, "_history", Mock(side_effect=AssertionError("No history")))
        monkeypatch.setattr(storage, "decode", Mock(side_effect=AssertionError("No prefix")))
        output_sequence = store._committed[SID].llm.attempts["first"].records[2].sequence
        page = store.llm_records(SID, "first", after=output_sequence - 1, limit=1)
        assert page["records"] == [{"sequence": output_sequence, "record": audit("output")}]
        assert read.call_count == 1
        assert read.call_args.args[1] == store._committed[SID].locations[output_sequence - 1]
    finally:
        store.close()


@pytest.mark.parametrize("status", ["failed", "timed_out", "completed"])
def test_retry_metadata_and_invalid_validation_survive_startup_replay(opened, status):
    prefix(opened, OBSERVED)
    emit(opened, "outcome", data={**OUTCOME, "status": status})
    if status == "completed":
        emit(opened, "validation", data=INVALID)
    ident = retry_identity()
    for kind in ["intent", "dispatched", "output", "outcome", "validation"]:
        emit(opened, kind, identity=ident)
    expected = opened.llm_attempts(SID, "run")["attempts"]
    expected_records = opened.llm_records(SID, "second")["records"]
    recovered = restart(opened)
    try:
        assert recovered.llm_attempts(SID, "run")["attempts"] == expected
        assert recovered.llm_records(SID, "second")["records"] == expected_records
        assert recovered.attempt_count(SID, "run") == 0
    finally:
        recovered.close()


def test_multiple_unmatched_intents_each_recover_their_own_identity_and_count(opened):
    prefix(opened, OBSERVED)
    other = {**IDENTITY, "attempt_id": "other", "interaction_id": "other-interaction"}
    emit(opened, "intent", identity=other)
    recovered = restart(opened)
    try:
        first = recovered.llm_records(SID, "first")["records"][-1]["record"]
        second = recovered.llm_records(SID, "other")["records"][-1]["record"]
        assert first["identity"] == IDENTITY and second["identity"] == other
        assert first["data"]["output_count"] == 1 and first["data"]["dispatched"] is None
        assert second["data"]["output_count"] == 0 and second["data"]["dispatched"] is False
        assert first["data"]["usage"] is None and second["data"]["usage"] is None
        image = journal(recovered).read_bytes()
    finally:
        recovered.close()
    again = restart(opened)
    try:
        assert journal(again).read_bytes() == image
    finally:
        again.close()


def test_abandoned_completed_unvalidated_attempt_does_not_block_a_new_operation(opened):
    prefix(opened, COMPLETED)
    recovered = restart(opened)
    try:
        assert recovered.get(SID).runs[0].status == "interrupted"
        old = recovered._committed[SID]
        snapshot = recovered.get(SID)
        snapshot.runs.append(
            AgentRun(
                id="new-run",
                request="New question",
                created_at=NOW,
                updated_at=NOW,
                status="planning",
                operation_id="new-operation",
            )
        )
        recovered.append(
            snapshot, "run.planning", "new-plan", run_id="new-run", operation_id="new-operation"
        )
        ident = {
            **IDENTITY,
            "run_id": "new-run",
            "operation_id": "new-operation",
            "attempt_id": "new",
            "interaction_id": "new",
            "stage_id": "new",
        }
        for kind in ["intent", "dispatched", "output", "outcome", "validation"]:
            recovered.append(
                recovered.get(SID),
                "llm." + kind,
                "new-plan",
                run_id="new-run",
                operation_id="new-operation",
                audit=audit(kind, ident),
            )
        assert old.llm.attempts["first"].validation is None
        assert recovered.llm_attempts(SID, "run")["attempts"][0]["validation"] is None
        assert recovered.llm_attempts(SID, "new-run")["attempts"][0]["validation"] == VALID
    finally:
        recovered.close()


def test_pages_and_published_lifecycle_are_detached_from_later_output_commits(opened):
    prefix(opened, OBSERVED)
    old = opened._committed[SID]
    page = opened.llm_records(SID, "first")
    metadata = opened.llm_attempts(SID, "run")
    emit(opened, "output", data={**OUTPUT, "index": 2, "body": "later"})
    assert len(page["records"]) == 3 and not page["has_more"]
    assert metadata["attempts"][0]["output_count"] == 1
    assert old.llm.attempts["first"].output_count == 1
    assert len(old.llm.attempts["first"].records) == 3
    assert opened.llm_records(SID, "first", after=page["next_after"])["records"][0]["record"] == (
        audit("output", data={**OUTPUT, "index": 2, "body": "later"})
    )


@pytest.mark.parametrize("phase", ["revision", "execution", "synthesis"])
def test_known_candidate_revision_and_execution_step_binding(opened, phase):
    snapshot = opened.get(SID)
    run = snapshot.runs[0]
    run.plan = reviewed_run().plan
    run.status = "planning" if phase == "revision" else "executing"
    run.operation_id = "new-operation"
    ident = {
        **IDENTITY,
        "plan_id": run.plan.id,
        "revision": 2 if phase == "revision" else 1,
        "phase": phase,
        "step_id": "step-0" if phase == "execution" else None,
        "operation_id": "new-operation",
    }
    opened.append(
        snapshot,
        "run.planning" if phase == "revision" else "run.approved",
        "phase",
        run_id="run",
        operation_id="new-operation",
        audit={key: ident[key] for key in ("plan_id", "revision", "phase", "stage_id")},
    )
    for kind in ["intent", "dispatched", "output", "outcome", "validation"]:
        emit(opened, kind, identity=ident)
    assert opened.llm_attempts(SID, "run")["attempts"][0]["identity"] == ident


def approve_execution(store):
    snapshot = store.get(SID)
    run = snapshot.runs[0]
    plan = reviewed_run().plan
    plan.steps.append(plan.steps[0].model_copy(update={"id": "step-1"}))
    run.plan, run.status, run.operation_id = plan, "approved", "execute-operation"
    run.results, run.answer, run.error = [], None, None
    store.append(
        snapshot,
        "run.approved",
        "approve",
        run_id="run",
        operation_id=run.operation_id,
        audit={
            "plan_id": plan.id,
            "revision": plan.revision,
            "phase": "execution",
            "stage_id": "operation-stage-is-not-a-step-stage",
        },
    )
    return plan


def execution_identity(plan, step="step-0", *, stage="step-stage-0", phase="execution"):
    return {
        **IDENTITY,
        "operation_id": "execute-operation",
        "plan_id": plan.id,
        "revision": plan.revision,
        "phase": phase,
        "step_id": step,
        "stage_id": stage,
        "interaction_id": stage,
        "attempt_id": stage,
    }


@pytest.mark.parametrize("mode", ["v2", "legacy", "mixed"])
def test_one_approved_operation_supports_distinct_known_steps_then_synthesis(opened, mode):
    store = opened
    if mode == "mixed":
        image = legacy_image(store)
        store.close()
        journal(store).write_bytes(image)
        store = restart(store)
    plan = approve_execution(store)
    identities = [
        execution_identity(plan),
        execution_identity(plan, "step-1", stage="step-stage-1"),
        execution_identity(plan, None, stage="synthesis-stage", phase="synthesis"),
    ]
    for ident in identities:
        for kind in ["intent", "dispatched", "output", "outcome", "validation"]:
            emit(store, kind, identity=ident)
    run = store.get(SID).runs[0]
    assert run.status == "approved" and run.operation_id == "execute-operation"
    assert run.plan == plan and run.results == [] and run.answer is None
    assert not any(r.event.type.startswith("tool.") for r in store.records[SID])
    expected = store.llm_attempts(SID, "run")["attempts"]
    assert [a["identity"] for a in reversed(expected)] == identities
    image = legacy_image(store) if mode == "legacy" else journal(store).read_bytes()
    store.close()
    journal(store).write_bytes(image)
    recovered = restart(store)
    try:
        assert recovered.llm_attempts(SID, "run")["attempts"] == expected
        assert recovered.get(SID).runs[0].plan == plan
        assert journal(recovered).read_bytes().startswith(image)
    finally:
        recovered.close()


@pytest.mark.parametrize("bad", ["unknown_step", "stage_reuse", "slot_change", "candidate"])
def test_execution_stages_still_enforce_known_plan_candidate_and_stable_slots(opened, bad):
    plan = approve_execution(opened)
    first = execution_identity(plan)
    for kind in ["intent", "dispatched", "output", "outcome", "validation"]:
        emit(opened, kind, identity=first)
    other = execution_identity(plan, "step-1", stage="another-stage")
    if bad == "unknown_step":
        other["step_id"] = "not-in-approved-plan"
    elif bad == "stage_reuse":
        other["stage_id"] = first["stage_id"]
    elif bad == "slot_change":
        other["step_id"] = first["step_id"]
    else:
        other["plan_id"] = "not-the-approved-candidate"
    images, before = payload_images(opened), opened._committed[SID]
    with pytest.raises(AgentFault):
        emit(opened, "intent", identity=other)
    assert opened.poisoned and opened._committed[SID] is before
    assert payload_images(opened) == images


def test_opaque_old_coordinator_controls_are_not_rejected_until_typed_opt_in(opened):
    snapshot = opened.get(SID)
    opened.append(
        snapshot,
        "run.planning",
        "opaque",
        run_id="run",
        operation_id="operation",
        audit={"plan_id": {"opaque": "historical"}, "revision": "not-provider-metadata"},
    )
    snapshot = opened.get(SID)
    snapshot.runs[0].status, snapshot.runs[0].operation_id = "cancelled", None
    opened.append(snapshot, "run.cancelled", "stop", run_id="run", operation_id="operation")
    expected = journal(opened).read_bytes()
    recovered = restart(opened)
    try:
        assert recovered.llm_attempts(SID, "run")["attempts"] == []
        assert journal(recovered).read_bytes() == expected
    finally:
        recovered.close()


def test_unknown_envelope_run_is_a_sanitized_monotonic_recording_failure(opened):
    before = journal(opened).read_bytes()
    with pytest.raises(AgentFault) as failure:
        opened.append(
            opened.get(SID),
            "llm.intent",
            "plan",
            run_id="unknown",
            operation_id="operation",
            audit=audit("intent"),
        )
    assert failure.value.code == "storage_unavailable" and opened.poisoned
    assert journal(opened).read_bytes() == before


@pytest.mark.parametrize("valid", [False, True])
async def test_empty_completed_transport_cannot_validate_nonexistent_provider_output(opened, valid):
    async def record(kind, body):
        opened.append(
            opened.get(SID), kind, "plan", run_id="run", operation_id="operation", audit=body
        )

    runner = AttemptRunner(
        record=record,
        dispatch_check=opened._healthy,
        timeout_seconds=1,
        recording_failed=Mock(),
    )
    transport = Mock(send_once=AsyncMock(return_value=None))
    result = await runner.run(
        LLMIdentity.model_validate(IDENTITY),
        CanonicalRequest(INTENT["request"], instructions_version="v1"),
        transport,
    )
    assert result.outcome.status == "failed" and result.outcome.output_count == 0
    assert result.outcome.error_code == "provider_no_output"
    committed = opened._committed[SID]
    with pytest.raises(ValueError):
        await runner.validation(result, valid=valid)
    assert not opened.poisoned and not runner.poisoned
    assert opened._committed[SID] is committed
    attempt = opened._committed[SID].llm.attempts["first"]
    assert attempt.output_count == 0 and attempt.validation is None


@pytest.mark.parametrize("valid", [False, True])
def test_storage_rejects_direct_validation_without_completed_output(opened, valid):
    prefix(opened, [*STARTED, ("outcome", {**OUTCOME, "output_count": 0})])
    before = opened._committed[SID]
    with pytest.raises(AgentFault) as failure:
        emit(opened, "validation", data=VALID if valid else INVALID)
    assert failure.value.code == "storage_unavailable" and opened.poisoned
    assert opened._committed[SID] is before
    assert before.llm.attempts["first"].validation is None


@pytest.mark.parametrize(
    "reader,kwargs",
    [
        ("attempts", {"limit": 0}),
        ("attempts", {"limit": 101}),
        ("attempts", {"limit": True}),
        ("attempts", {"before": -1}),
        ("attempts", {"before": True}),
        ("attempts", {"before": 2}),
        ("records", {"after": -1}),
        ("records", {"after": True}),
        ("records", {"after": None}),
        ("records", {"after": 1000}),
        ("records", {"limit": 101}),
        ("records", {"limit": 0}),
    ],
)
def test_private_cursor_bounds_are_strict_without_poisoning(opened, reader, kwargs):
    emit(opened, "intent")
    with pytest.raises(AgentFault) as failure:
        if reader == "attempts":
            opened.llm_attempts(SID, "run", **kwargs)
        else:
            opened.llm_records(SID, "first", **kwargs)
    assert failure.value.status == 422 and not opened.poisoned


def test_private_readers_enforce_session_boundaries_and_never_accept_paths(opened):
    emit(opened, "intent")
    opened.id_factory = lambda: OTHER
    opened.create(CreateSession(request_id="neighbor", model="offline", reasoning_effort="high"))
    for sid, key in [(OTHER, "first"), (SID, "unknown"), (SID, "../outside")]:
        with pytest.raises(AgentFault) as failure:
            opened.llm_records(sid, key)
        assert failure.value.status in {404, 422}
    for sid, rid in [(OTHER, "run"), (SID, "../outside"), ("../outside", "run")]:
        with pytest.raises(AgentFault) as failure:
            opened.llm_attempts(sid, rid)
        assert failure.value.status in {404, 422}
    for sid in [None, [], 1]:
        with pytest.raises(AgentFault) as failure:
            opened.llm_records(sid, "first")
        assert failure.value.status == 422
    assert not opened.poisoned


@pytest.mark.parametrize("reader", ["attempts", "records"])
@pytest.mark.parametrize("damage", ["missing", "same_size", "symlink", "hardlink", "fifo"])
def test_private_readers_verify_even_unselected_audit_only_refs_and_unsafe_links(
    opened,
    tmp_path,
    reader,
    damage,
):
    prefix(opened, [*COMPLETED, "validation"])
    location = opened._committed[SID].llm.attempts["first"].records[0]
    path = opened.root / SID / "payloads" / (location.payload[0] + ".json")
    raw = path.read_bytes()
    if damage == "same_size":
        path.write_bytes(b"X" + raw[1:])
    else:
        path.unlink()
        outside = tmp_path / "synthetic-outside"
        outside.write_bytes(raw)
        if damage == "symlink":
            path.symlink_to(outside)
        elif damage == "hardlink":
            os.link(outside, path)
        elif damage == "fifo":
            os.mkfifo(path)
    with pytest.raises(AgentFault) as failure:
        if reader == "attempts":
            opened.llm_attempts(SID, "run")
        else:
            opened.llm_records(SID, "first", after=location.sequence, limit=1)
    assert failure.value.code == "storage_unavailable" and opened.poisoned
    opened.close()
    failed = SessionStore(opened.root)
    with pytest.raises(AgentFault):
        failed.start()
    assert failed.fd is None


@pytest.mark.parametrize("boundary", ["payload", "journal"])
def test_uncertain_output_fsync_never_publishes_index_and_recovery_counts_committed_only(
    opened,
    monkeypatch,
    boundary,
):
    store = opened
    prefix(store, STARTED)
    old = store._committed[SID]
    real_open, real_sync = os.open, os.fsync
    names = {}

    def opened_fd(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        names[fd] = os.fspath(path)
        return fd

    def fsync(fd):
        name = names.get(fd, "")
        if (boundary == "journal" and name == "events.jsonl") or (
            boundary == "payload" and name.endswith(".json")
        ):
            raise OSError("synthetic durability failure")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened_fd)
        patch.setattr(os, "fsync", fsync)
        with pytest.raises(AgentFault):
            emit(store, "output")
    assert store.poisoned and store._committed[SID] is old
    assert old.llm.attempts["first"].output_count == 0
    assert store.claim_digest(SID, "create") is not None
    assert store.operation_known(SID, "run", "operation")
    recovered = restart(store)
    try:
        metadata = recovered.llm_attempts(SID, "run")["attempts"][0]
        assert metadata["output_count"] == (boundary == "journal")
        assert metadata["outcome"]["status"] == "interrupted"
        assert metadata["outcome"]["dispatched"] is None
    finally:
        recovered.close()
