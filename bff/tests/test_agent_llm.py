"""Offline application-observable audit boundary, ownership and coordinator checks."""

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent import INSTRUCTIONS, AgentService, PlanningInput
from app.agent_files import AgentFault, FilePolicy, canonical_digest
from app.agent_llm import (
    AttemptRunner,
    AuthorizationRevoked,
    CanonicalRequest,
    Observation,
    ProviderBodyFailure,
    RecordedPlanner,
)
from app.agent_schemas import Cancel, CreateSession, Feedback, PlanIdentity, Proposal, StartRun
from app.llm_schemas import IntentData, LLMIdentity, LLMRecord, OutputData, RequestBody
from app.sessions import SessionStore
from tests.helpers import settled_session_records
from tests.test_agent import CREATE, proposal, wait_until

PRIVATE = "原文🌱 credential header Authorization is source text, not configuration.\n" * 8000
OPAQUE = {"type": "reasoning", "encrypted_content": "opaque+/==不可改写", "id": "continuation"}


def identity(**updates):
    return LLMIdentity.model_validate(
        {
            "session_id": "session",
            "run_id": "run",
            "operation_id": "operation",
            "plan_id": "candidate",
            "revision": 1,
            "phase": "planning",
            "stage_id": "stage",
            "step_id": None,
            "interaction_id": "interaction",
            "attempt_id": uuid4().hex,
            "attempt": 1,
            "retry_of": None,
            "backoff_seconds": 0.0,
            **updates,
        }
    )


def request(**updates):
    return CanonicalRequest(
        {"model": "deployment-a", "input": "Question", "reasoning": {"effort": "high"}, **updates},
        instructions_version="fixture-v1",
    )


class Transport:
    """Contract double: each send_once invokes exactly one injected send."""

    def __init__(self, send):
        self.send, self.calls = send, []

    async def send_once(self, raw, observe):
        self.calls.append(raw)
        return await self.send(raw, observe)


def boundary(*, timeout=1, fail=None, after_record=None):
    records = []
    health = SimpleNamespace(poisoned=False)
    failed = Mock()

    def check():
        if health.poisoned:
            raise AgentFault("storage_unavailable", "Recording is unavailable.", 503)

    async def record(kind, audit):
        if kind == fail:
            raise OSError("PRIVATE generic exception with headers and Bearer token")
        parsed = LLMRecord.model_validate(audit)
        assert kind == "llm." + parsed.kind
        records.append(json.loads(json.dumps(audit, ensure_ascii=False)))
        if after_record:
            after_record(kind)

    runner = AttemptRunner(
        record=record,
        dispatch_check=check,
        timeout_seconds=timeout,
        recording_failed=failed,
    )
    return SimpleNamespace(runner=runner, records=records, health=health, failed=failed)


def payloads(test):
    return [entry["data"] for entry in test.records if entry["kind"] == "output"]


def outcome(test):
    return next(entry["data"] for entry in reversed(test.records) if entry["kind"] == "outcome")


def test_canonical_request_is_immutable_exact_unicode_body_without_redaction():
    body = {
        "model": "deployment-a",
        "input": [OPAQUE.copy(), {"role": "user", "content": PRIVATE}],
        "instructions": "Actual instructions, not PlanningInput reconstruction",
        "reasoning": {"effort": "high"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "agent_plan",
                "schema": Proposal.model_json_schema(),
            }
        },
        "include": ["reasoning.encrypted_content"],
        "store": False,
        "stream": True,
        "previous_response_id": "opaque-response-id",
    }
    sent = CanonicalRequest(body, instructions_version="fixture-v1")
    assert sent.body == body
    assert "原文🌱".encode() in sent.body_bytes
    assert sent.body["input"][0] == OPAQUE
    original = sent.body_bytes
    body["input"].clear()
    sent.body["input"].clear()
    assert sent.body_bytes is original
    assert len(sent.body["input"]) == 2
    assert request().body["store"] is False


@pytest.mark.parametrize(
    "extra",
    ["headers", "api_key", "credential", "credentials", "client", "settings", "extra_body"],
)
def test_transport_configuration_is_structurally_excluded(extra):
    with pytest.raises(ValidationError):
        request(**{extra: {"value": "not-a-body-field"}})


@pytest.mark.parametrize("store", [True, "false", 0, None])
def test_responses_store_false_is_enforced(store):
    with pytest.raises(ValidationError):
        request(store=store)


@pytest.mark.parametrize(
    "extra",
    [
        {"headers": {"Authorization": "transport secret"}},
        {"api_key": "transport secret"},
        {"client": {}},
        {"settings": {}},
        {"store": True},
        {"store": 0},
        {"store": None},
        {"tools": [{"type": "web_search"}]},
        {"tools": [{"type": "mcp", "server_url": "https://example.invalid"}]},
        {"tools": [{"type": "function", "name": "read", "parameters": {}, "api_key": "x"}]},
        {"tools": [{"type": "function", "name": "read", "parameters": {}, "headers": {}}]},
        {"tool_choice": {"type": "mcp", "server_label": "remote"}},
        {"tool_choice": {"type": "function", "name": "read", "headers": {}}},
    ],
)
def test_durable_intent_enforces_shared_request_and_function_only_allowlists(extra):
    raw = {**request().body, **extra}
    with pytest.raises(ValidationError):
        IntentData(request=raw, instructions_version="fixture-v1")
    with pytest.raises(ValidationError):
        LLMRecord(
            llm_audit_version=1,
            kind="intent",
            identity=identity(),
            at="2026-10-23T10:00:00Z",
            data={"request": raw, "instructions_version": "fixture-v1"},
        )


def test_durable_intent_requires_explicit_store_false_without_redacting_opaque_fields():
    raw = request().body
    raw.pop("store")
    with pytest.raises(ValidationError):
        IntentData(request=raw, instructions_version="fixture-v1")
    sent = CanonicalRequest(raw, instructions_version="fixture-v1")
    assert sent.body["store"] is False
    parameters = {
        "type": "object",
        "properties": {"api_key": {"type": "string"}, "headers": {"type": "object"}},
    }
    sent = request(
        input=[OPAQUE, {"role": "user", "content": PRIVATE}],
        tools=[
            {
                "type": "function",
                "name": "fixture",
                "description": PRIVATE,
                "parameters": parameters,
                "strict": True,
            }
        ],
        tool_choice={"type": "function", "name": "fixture"},
        text={"format": {"type": "json_schema", "name": "fixture", "schema": parameters}},
    )
    intent = IntentData(request=sent.body, instructions_version="fixture-v1")
    assert intent.request == sent.body
    assert intent.request["input"][0] == OPAQUE
    assert intent.request["tools"][0]["parameters"] == parameters
    from app.agent_llm import RequestBody as ReexportedRequestBody

    assert ReexportedRequestBody is RequestBody


@pytest.mark.parametrize("key", ["headers", "api_key", "client", "settings"])
@pytest.mark.parametrize("location", ["reasoning", "text", "format"])
def test_nested_configuration_is_rejected_by_constructor_and_raw_intent(key, location):
    body = request().body
    if location == "reasoning":
        body["reasoning"][key] = {"Authorization": "SYNTHETIC"}
    elif location == "text":
        body["text"] = {key: "SYNTHETIC"}
    else:
        body["text"] = {"format": {"type": "json_object", key: "SYNTHETIC"}}
    with pytest.raises(ValidationError):
        CanonicalRequest(body, instructions_version="fixture-v1")
    with pytest.raises(ValidationError):
        IntentData(request=body, instructions_version="fixture-v1")


@pytest.mark.parametrize(
    "options",
    [
        {"reasoning": {"effort": "HIGH"}},
        {"reasoning": {"summary": "unknown"}},
        {"text": {"verbosity": "unknown"}},
        {"text": {"format": {"type": "unknown"}}},
        {"text": {"format": {"type": "json_schema", "schema": {}}}},
        {"text": {"format": {"type": "json_schema", "name": "plan", "schema_": {}}}},
        {"text": {"format": {"type": "json_schema", "name": "plan", "schema": "object"}}},
    ],
)
def test_options_and_format_wrappers_are_strict_without_silent_normalization(options):
    body = {**request().body, **options}
    with pytest.raises(ValidationError):
        CanonicalRequest(body, instructions_version="fixture-v1")
    with pytest.raises(ValidationError):
        IntentData(request=body, instructions_version="fixture-v1")


@pytest.mark.parametrize("kind", ["text", "json_object", "json_schema"])
def test_reviewed_text_formats_preserve_wire_alias_and_opaque_schema(kind):
    schema = {
        "type": "object",
        "properties": {
            "headers": {"type": "object", "description": "Authorization: SYNTHETIC"},
            "api_key": {"type": "string"},
            "client": {"type": "object"},
            "settings": {"type": "object"},
            "schema_": {"type": "string"},
        },
    }
    format_body = {"type": kind}
    if kind == "json_schema":
        format_body.update(name="agent_plan", schema=schema, strict=True, description=PRIVATE)
    body = {
        "store": False,
        "model": "deployment-a",
        "input": [OPAQUE, {"headers": {"Authorization": "SYNTHETIC"}, "source": PRIVATE}],
        "reasoning": {"effort": "high", "summary": "auto"},
        "text": {"format": format_body, "verbosity": "low"},
    }
    canonical = CanonicalRequest(body, instructions_version="fixture-v1")
    assert canonical.body == body
    assert IntentData(request=canonical.body, instructions_version="fixture-v1").request == body
    if kind == "json_schema":
        assert canonical.body["text"]["format"]["schema"] == schema
        assert "schema_" not in canonical.body["text"]["format"]


@pytest.mark.parametrize("extra", ["headers", "exception", "client", "settings", "metadata"])
def test_output_metadata_allowlist_and_legacy_defaults(extra):
    legacy = {"index": 1, "kind": "event", "body": {"opaque": OPAQUE}}
    parsed = OutputData.model_validate(legacy)
    assert parsed.reported_model is parsed.provider_status is parsed.usage is None
    assert parsed.failed is False and parsed.body == legacy["body"]
    with pytest.raises(ValidationError):
        OutputData.model_validate({**legacy, extra: {}})


@pytest.mark.parametrize(
    "change",
    [
        {"attempt": True},
        {"revision": 0},
        {"retry_of": "previous"},
        {"backoff_seconds": -1.0},
        {"backoff_seconds": float("nan")},
        {"phase": "execution"},
        {"step_id": "unexpected"},
        {"client": "not-identity"},
    ],
)
def test_identity_is_strict_and_correlated(change):
    with pytest.raises(ValidationError):
        identity(**change)
    assert identity(phase="execution", step_id="approved-step").step_id == "approved-step"


@pytest.mark.parametrize(
    "change",
    [
        {"llm_audit_version": 2},
        {"llm_audit_version": True},
        {"at": "2026-10-23T10:00:00+01:00"},
        {"at": "2026-10-23T10:00:00"},
        {"at": "not-a-time"},
        {"kind": "other"},
        {"data": {"status": "valid"}},
        {"data": {"status": "valid", "error_code": "invalid_plan"}},
        {"data": {"status": "valid", "error_code": None, "metadata": {}}},
        {"headers": {}},
    ],
)
def test_record_requires_version_utc_and_exact_per_kind_data(change):
    record = {
        "llm_audit_version": 1,
        "kind": "validation",
        "identity": identity().model_dump(),
        "at": "2026-10-23T10:00:00Z",
        "data": {"status": "valid", "error_code": None},
        **change,
    }
    with pytest.raises(ValidationError):
        LLMRecord.model_validate(record)


async def test_one_send_sees_durable_intent_and_exact_bytes_then_full_response_before_decode():
    test = boundary()
    sent = request(input=[OPAQUE, {"role": "user", "content": PRIVATE}])
    ident = identity()
    response = {"model": "reported-model", "status": "completed", "output": [OPAQUE, PRIVATE]}

    async def send(raw, observe):
        assert raw is sent.body_bytes
        assert [item["kind"] for item in test.records] == ["intent", "dispatched"]
        assert test.records[0]["identity"] == ident.model_dump()
        assert test.records[0]["data"]["request"] == json.loads(raw)
        return Observation(
            "response",
            response,
            reported_model="reported-model",
            provider_status="completed",
            usage={"input_tokens": 123, "output_tokens": 45},
        )

    transport = Transport(send)
    result = await test.runner.run(ident, sent, transport)
    assert result.identity is ident
    assert result.outputs[0].body == response
    assert payloads(test) == [
        {
            "index": 1,
            "kind": "response",
            "body": response,
            "reported_model": "reported-model",
            "provider_status": "completed",
            "usage": {"input_tokens": 123, "output_tokens": 45},
            "failed": False,
        }
    ]
    assert outcome(test) == {
        "status": "completed",
        "dispatched": True,
        "output_count": 1,
        "reported_model": "reported-model",
        "provider_status": "completed",
        "usage": {"input_tokens": 123, "output_tokens": 45},
        "error_code": None,
    }
    await test.runner.validation(result, valid=True)
    assert [entry["kind"] for entry in test.records] == [
        "intent",
        "dispatched",
        "output",
        "outcome",
        "validation",
    ]
    assert len(transport.calls) == 1
    assert all(entry["identity"] == ident.model_dump() for entry in test.records)


async def test_stream_appends_independent_chunks_durably_before_next_observation():
    test = boundary()
    events = [
        {"type": "created", "id": "provider-id"},
        {"type": "delta", "delta": PRIVATE},
        {"type": "delta", "delta": "TAIL"},
        {"type": "completed", "continuation": OPAQUE},
    ]

    async def send(raw, observe):
        for event in events:
            await observe(Observation("event", event))
            assert test.records[-1]["data"]["body"] == event
        return None

    result = await test.runner.run(identity(), request(stream=True), Transport(send))
    assert [output.body for output in result.outputs] == events
    assert [entry["index"] for entry in payloads(test)] == [1, 2, 3, 4]
    assert [entry["body"] for entry in payloads(test)] == events
    assert payloads(test)[2]["body"] == {"type": "delta", "delta": "TAIL"}
    assert outcome(test)["output_count"] == 4


@pytest.mark.parametrize("fail_outcome", [False, True])
async def test_every_event_retains_metadata_before_ack_even_without_outcome(fail_outcome):
    test = boundary(fail="llm.outcome" if fail_outcome else None)
    observations = [
        Observation("event", None, "first-model", "in_progress", {"input_tokens": 12}),
        Observation(
            "event",
            {"delta": "partial 原文"},
            "second-model",
            "failed",
            {"input_tokens": 13, "output_tokens": 2},
            True,
        ),
    ]
    expected = [
        {
            "index": 1,
            "kind": "event",
            "body": None,
            "reported_model": "first-model",
            "provider_status": "in_progress",
            "usage": {"input_tokens": 12},
            "failed": False,
        },
        {
            "index": 2,
            "kind": "event",
            "body": {"delta": "partial 原文"},
            "reported_model": "second-model",
            "provider_status": "failed",
            "usage": {"input_tokens": 13, "output_tokens": 2},
            "failed": True,
        },
    ]

    async def send(raw, observe):
        for observation, data in zip(observations, expected, strict=True):
            await observe(observation)
            assert payloads(test)[-1] == data
            observation.usage["mutated_after_ack"] = True
        return None

    transport = Transport(send)
    if fail_outcome:
        with pytest.raises(AgentFault) as failure:
            await test.runner.run(identity(), request(), transport)
        assert failure.value.code == "storage_unavailable"
        assert not any(item["kind"] == "outcome" for item in test.records)
        test.failed.assert_called_once()
    else:
        result = await test.runner.run(identity(), request(), transport)
        assert result.outcome.status == "failed"
        assert result.outcome.reported_model == "second-model"
        assert result.outcome.usage == {"input_tokens": 13, "output_tokens": 2}
    assert payloads(test) == expected and len(transport.calls) == 1


@pytest.mark.parametrize("gate", ["intent", "dispatched"])
async def test_revoked_authorization_only_records_outcome_for_durable_intent(gate):
    test = boundary()
    original = test.runner.record

    async def record(kind, audit):
        if kind == "llm." + gate:
            raise AuthorizationRevoked()
        await original(kind, audit)

    test.runner.record = record
    transport = Transport(Mock(side_effect=AssertionError("Revoked authorization must not send")))
    if gate == "intent":
        with pytest.raises(AuthorizationRevoked):
            await test.runner.run(identity(), request(), transport)
        assert test.records == [] and test.runner.previous is None
    else:
        result = await test.runner.run(identity(), request(), transport)
        assert result.outcome.status == "not_dispatched" and result.outcome.dispatched is False
        assert [item["kind"] for item in test.records] == ["intent", "outcome"]
    assert test.runner.stop_event.is_set() and not test.runner.poisoned
    assert not transport.calls
    test.failed.assert_not_called()


async def test_authorization_signal_cannot_disguise_output_recording_failure():
    test = boundary()
    original = test.runner.record

    async def record(kind, audit):
        if kind == "llm.output":
            raise AuthorizationRevoked()
        await original(kind, audit)

    test.runner.record = record

    async def send(raw, observe):
        return Observation("response", {"opaque": "must retain"})

    with pytest.raises(AgentFault) as failure:
        await test.runner.run(identity(), request(), Transport(send))
    assert failure.value.code == "storage_unavailable" and test.runner.poisoned
    test.failed.assert_called_once()


async def test_attempt_timeout_does_not_apply_to_intent_or_final_recording():
    test = boundary(timeout=0.01)
    original = test.runner.record

    async def slow_record(kind, audit):
        if kind in {"llm.intent", "llm.output", "llm.outcome"}:
            await asyncio.sleep(0.02)
        await original(kind, audit)

    test.runner.record = slow_record

    async def send(raw, observe):
        return Observation("response", {"ok": True})

    started = time.monotonic()
    result = await test.runner.run(identity(), request(), Transport(send))
    assert time.monotonic() - started >= 0.06
    assert result.outcome.status == "completed"


@pytest.mark.parametrize("kind", ["llm.intent", "llm.output"])
async def test_native_cancel_during_recording_waits_for_durability_and_retains_late_output(kind):
    test = boundary()
    recording, release = asyncio.Event(), asyncio.Event()
    original = test.runner.record

    async def blocked_record(event_kind, audit):
        if event_kind == kind and not recording.is_set():
            recording.set()
            await release.wait()
        await original(event_kind, audit)

    test.runner.record = blocked_record

    async def send(raw, observe):
        try:
            await observe(Observation("event", {"delta": "observed before cancellation"}))
        except asyncio.CancelledError:
            pass
        return Observation("response", {"late": "final after cancelled callback"})

    transport = Transport(send)
    caller = asyncio.create_task(test.runner.run(identity(), request(), transport))
    await recording.wait()
    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    if kind == "llm.intent":
        assert not transport.calls
        assert outcome(test)["status"] == "not_dispatched"
    else:
        assert [entry["body"] for entry in payloads(test)] == [
            {"delta": "observed before cancellation"},
            {"late": "final after cancelled callback"},
        ]
        assert outcome(test)["status"] == "cancelled"


@pytest.mark.parametrize("failure", ["exception", "error_body", "refused", "incomplete", "failed"])
async def test_partial_and_all_failure_observations_retained_without_generic_exception(failure):
    test = boundary()
    partial = {"type": "delta", "delta": "partial 原文"}
    final = {"error": {"code": "body_error", "message": "Explicit provider body"}}

    async def send(raw, observe):
        await observe(Observation("event", partial))
        if failure == "exception":
            raise RuntimeError("PRIVATE exception Authorization: Bearer token")
        if failure == "error_body":
            raise ProviderBodyFailure(final)
        return Observation("response", {"status": failure}, provider_status=failure)

    transport = Transport(send)
    result = await test.runner.run(identity(), request(), transport)
    assert result.outcome.status == "failed"
    assert payloads(test)[0]["body"] == partial
    assert result.outcome.output_count == (1 if failure == "exception" else 2)
    if failure == "error_body":
        assert payloads(test)[1] == {
            "index": 2,
            "kind": "error_body",
            "body": final,
            "reported_model": None,
            "provider_status": None,
            "usage": None,
            "failed": True,
        }
    assert "PRIVATE exception" not in json.dumps(test.records)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("cancellation", ["raise", "self_cancel", "self_cancel_suppressed"])
async def test_transport_cancellation_after_observation_revokes_validation_and_retry(cancellation):
    test = boundary()
    body = proposal().model_dump()

    async def send(raw, observe):
        await observe(Observation("response", body))
        if cancellation == "raise":
            raise asyncio.CancelledError("PRIVATE adapter cancellation")
        asyncio.current_task().cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            if cancellation != "self_cancel_suppressed":
                raise
        return Observation("response", {"late": body})

    transport = Transport(send)
    first = identity()
    result = await test.runner.run(first, request(), transport)
    assert result.outcome.status == "cancelled"
    assert result.outcome.error_code == "provider_cancelled"
    expected = [body, {"late": body}] if cancellation == "self_cancel_suppressed" else [body]
    assert [item["body"] for item in payloads(test)] == expected
    assert result.outcome.output_count == len(expected)
    assert test.runner.stop_event.is_set() and not test.runner.poisoned
    test.failed.assert_not_called()
    before = len(test.records)
    with pytest.raises(AgentFault):
        await test.runner.validation(result, valid=True)
    with pytest.raises(AgentFault):
        await test.runner.run(identity(attempt=2, retry_of=first.attempt_id), request(), transport)
    assert len(test.records) == before and len(transport.calls) == 1
    assert "PRIVATE adapter" not in json.dumps(test.records)


@pytest.mark.parametrize("source", ["adapter", "watchdog"])
@pytest.mark.parametrize("partial", [False, True])
async def test_adapter_and_watchdog_timeout_have_same_outcome_and_retry_barrier(source, partial):
    test = boundary(timeout=0.01 if source == "watchdog" else 1)
    body = proposal().model_dump()

    async def send(raw, observe):
        if partial:
            await observe(Observation("response", body))
        if source == "adapter":
            raise TimeoutError("PRIVATE adapter timeout with HTTP headers")
        await asyncio.Event().wait()

    transport = Transport(send)
    first = identity()
    result = await test.runner.run(first, request(), transport)
    assert result.outcome.status == "timed_out"
    assert result.outcome.error_code == "provider_timeout"
    assert result.outcome.dispatched is True
    assert result.outcome.output_count == int(partial)
    assert [item["body"] for item in payloads(test)] == ([body] if partial else [])
    assert test.runner.stop_event.is_set() and test.runner.stop_reason == "timed_out"
    assert not test.runner.poisoned
    test.failed.assert_not_called()
    before = len(test.records)
    with pytest.raises(AgentFault):
        await test.runner.validation(result, valid=True)
    with pytest.raises(AgentFault):
        await test.runner.run(identity(attempt=2, retry_of=first.attempt_id), request(), transport)
    assert len(test.records) == before and len(transport.calls) == 1
    assert "PRIVATE adapter" not in json.dumps(test.records)


async def test_no_observation_completion_fails_without_validation_or_storage_poison():
    test = boundary()

    async def send(raw, observe):
        return None

    transport = Transport(send)
    result = await test.runner.run(identity(), request(), transport)
    assert result.outcome.status == "failed"
    assert result.outcome.error_code == "provider_no_output"
    assert result.outcome.output_count == 0 and result.outputs == ()
    with pytest.raises(ValueError):
        await test.runner.validation(result, valid=True)
    assert [item["kind"] for item in test.records] == ["intent", "dispatched", "outcome"]
    assert not test.runner.poisoned and not test.runner.stop_event.is_set()
    test.failed.assert_not_called()
    assert len(transport.calls) == 1


@pytest.mark.parametrize("reason", ["timeout", "cancelled", "interrupted", "native_cancel", "race"])
async def test_revocation_retains_concurrent_and_cancellation_suppressing_final_output(reason):
    test = boundary(timeout=0.01 if reason == "timeout" else 2)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    final = {"type": "completed", "body": PRIVATE, "continuation": OPAQUE}

    async def send(raw, observe):
        await observe(Observation("event", {"type": "started"}))
        entered.set()
        if reason == "race":
            test.runner.stop()
            return Observation("response", final)
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return Observation("response", final, reported_model="late-model")

    transport = Transport(send)
    caller = asyncio.create_task(test.runner.run(identity(), request(), transport))
    await entered.wait()
    try:
        if reason == "native_cancel":
            caller.cancel()
        elif reason not in {"timeout", "race"}:
            test.runner.stop(reason)
        if reason != "race":
            await cancelled.wait()
            assert not caller.done() and test.runner.active is not None
            if reason == "native_cancel":
                caller.cancel()  # Repeated cancellation must still retain ownership.
    finally:
        release.set()
    if reason == "native_cancel":
        with pytest.raises(asyncio.CancelledError):
            await caller
    else:
        result = await caller
        expected = "timed_out" if reason == "timeout" else reason
        assert result.outcome.status == ("cancelled" if reason == "race" else expected)
    assert payloads(test)[-1]["body"] == final
    assert outcome(test)["output_count"] == 2
    assert outcome(test)["dispatched"] is True
    assert len(transport.calls) == 1 and test.runner.active is None
    with pytest.raises(AgentFault):
        await test.runner.run(identity(), request(), transport)


@pytest.mark.parametrize("kind", ["intent", "dispatched", "output", "outcome", "validation"])
async def test_record_failure_is_fail_closed_for_dispatch_decode_advancement_and_retry(kind):
    test = boundary(fail="llm." + kind)

    async def send(raw, observe):
        return Observation("response", {"output": "complete"})

    transport = Transport(send)
    with pytest.raises(AgentFault) as failure:
        result = await test.runner.run(identity(), request(), transport)
        await test.runner.validation(result, valid=True)
    assert failure.value.code == "storage_unavailable"
    assert test.runner.poisoned and test.runner.stop_event.is_set()
    assert len(transport.calls) == (0 if kind in {"intent", "dispatched"} else 1)
    test.failed.assert_called_once()
    assert "PRIVATE generic" not in json.dumps(test.records)
    before = len(transport.calls)
    with pytest.raises(AgentFault):
        await test.runner.run(identity(), request(), transport)
    assert len(transport.calls) == before


@pytest.mark.parametrize("reason", ["stop", "poisoned"])
async def test_dispatch_scheduled_owner_rechecks_queued_stop_or_store_poison(reason):
    def queued(kind):
        if kind == "llm.dispatched":

            def revoke():
                if reason == "stop":
                    test.runner.stop()
                else:
                    test.health.poisoned = True

            asyncio.get_running_loop().call_soon(revoke)

    test = boundary(after_record=queued)

    async def send(raw, observe):
        pytest.fail("Queued revocation must win before transport entry")

    transport = Transport(send)
    result = await test.runner.run(identity(), request(), transport)
    assert not transport.calls
    assert result.outcome.status == "not_dispatched"
    assert result.outcome.dispatched is False
    assert result.outcome.output_count == 0


async def test_explicit_retry_has_unique_ids_persisted_lineage_backoff_and_one_send_each():
    test = boundary()
    start = time.monotonic()

    async def send(raw, observe):
        if len(transport.calls) == 1:
            raise RuntimeError("generic failure must not become a record")
        intent = next(item for item in reversed(test.records) if item["kind"] == "intent")
        assert intent["identity"]["backoff_seconds"] == 0.01
        assert time.monotonic() - start >= 0.01
        return Observation("response", {"ok": True})

    transport = Transport(send)
    first = identity()
    failed = await test.runner.run(first, request(), transport)
    assert failed.outcome.status == "failed" and len(transport.calls) == 1
    retry = identity(attempt=2, retry_of=first.attempt_id, backoff_seconds=0.01)
    result = await test.runner.run(retry, request(), transport)
    assert result.outcome.status == "completed" and len(transport.calls) == 2
    assert result.identity.interaction_id == first.interaction_id
    intents = [item["identity"] for item in test.records if item["kind"] == "intent"]
    assert intents == [first.model_dump(), retry.model_dump()]
    assert intents[0]["attempt_id"] != intents[1]["attempt_id"]


@pytest.mark.parametrize("validation", [None, True, False])
async def test_retry_after_completed_attempt_requires_durably_invalid_validation(validation):
    test = boundary()

    async def send(raw, observe):
        return Observation("response", {"output": "observable"})

    transport = Transport(send)
    first = identity()
    result = await test.runner.run(first, request(), transport)
    if validation is not None:
        await test.runner.validation(result, valid=validation)
    assert len(transport.calls) == 1
    retry = identity(attempt=2, retry_of=first.attempt_id)
    if validation is False:
        next_result = await test.runner.run(retry, request(), transport)
        assert next_result.identity == retry and len(transport.calls) == 2
        assert test.records[-4]["identity"]["interaction_id"] == first.interaction_id
    else:
        before = len(test.records)
        with pytest.raises(ValueError):
            await test.runner.run(retry, request(), transport)
        assert len(test.records) == before and len(transport.calls) == 1


@pytest.mark.parametrize("cancel_validation", [False, True])
async def test_invalid_validation_requires_durability_and_live_waiter_for_retry(cancel_validation):
    test = boundary()
    entered, release = asyncio.Event(), asyncio.Event()
    original = test.runner.record

    async def record(kind, audit):
        if kind == "llm.validation":
            entered.set()
            await release.wait()
        await original(kind, audit)

    test.runner.record = record

    async def send(raw, observe):
        return Observation("response", "invalid plan output")

    transport = Transport(send)
    first = identity()
    result = await test.runner.run(first, request(), transport)
    writer = asyncio.create_task(test.runner.validation(result, valid=False))
    await entered.wait()
    retry = identity(attempt=2, retry_of=first.attempt_id)
    try:
        with pytest.raises(ValueError):
            await test.runner.run(retry, request(), transport)
        assert len(transport.calls) == 1
        if cancel_validation:
            writer.cancel()
    finally:
        release.set()
    if cancel_validation:
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert test.records[-1]["kind"] == "validation"
        assert not test.runner.poisoned
        with pytest.raises(AgentFault):
            await test.runner.run(retry, request(), transport)
        assert len(transport.calls) == 1
    else:
        await writer
        await test.runner.run(retry, request(), transport)
        assert len(transport.calls) == 2


@pytest.mark.parametrize("bad", ["reuse", "skip", "candidate", "interaction", "retry_of"])
async def test_incoherent_retry_is_rejected_before_record_or_send(bad):
    test = boundary()

    async def send(raw, observe):
        raise RuntimeError("failed one send")

    transport = Transport(send)
    first = identity()
    await test.runner.run(first, request(), transport)
    values = {"attempt": 2, "retry_of": first.attempt_id}
    values.update(
        {
            "reuse": {"attempt_id": first.attempt_id},
            "skip": {"attempt": 3},
            "candidate": {"plan_id": "other"},
            "interaction": {"interaction_id": "other"},
            "retry_of": {"retry_of": "other"},
        }[bad]
    )
    before = len(test.records)
    with pytest.raises(ValueError):
        await test.runner.run(identity(**values), request(), transport)
    assert len(test.records) == before and len(transport.calls) == 1


async def test_nonadjacent_attempt_id_reuse_and_validation_of_failed_send_are_rejected():
    test = boundary()

    async def send(raw, observe):
        raise RuntimeError("one failed send")

    transport = Transport(send)
    first = identity()
    failed = await test.runner.run(first, request(), transport)
    with pytest.raises(ValueError):
        await test.runner.validation(failed, valid=True)
    second = identity(attempt=2, retry_of=first.attempt_id)
    await test.runner.run(second, request(), transport)
    reused = identity(attempt=3, retry_of=second.attempt_id, attempt_id=first.attempt_id)
    with pytest.raises(ValueError):
        await test.runner.run(reused, request(), transport)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("invalid", [None, "json", "schema", "registry", "policy"])
async def test_recorded_planner_validates_with_coordinator_before_return_or_invalid_plan(
    tmp_path,
    invalid,
):
    test = boundary()
    owner = AgentService(
        SessionStore(tmp_path / "journal"), files=FilePolicy({"fixture": tmp_path})
    )
    body = proposal().model_dump()
    if invalid == "json":
        body = "unparseable plan output 原文"
    elif invalid == "schema":
        body.pop("steps")
    elif invalid == "policy":
        body["steps"][0]["arguments"]["path"] = "../outside.txt"

    async def send(raw, observe):
        return Observation("response", body)

    def decode(result):
        # Exercise the registry even for a trusted injected decoder returning a
        # model that bypassed Pydantic construction (legacy fixtures can too).
        if invalid == "registry":
            return Proposal.model_construct(
                objective="Disabled capability",
                assumptions=[],
                steps=[SimpleNamespace(tool="web_search")],
            )
        assert test.records[-1]["kind"] == "outcome"
        return Proposal.model_validate(result.outputs[-1].body)

    planner = RecordedPlanner(
        build_request=lambda context: request(), transport=Transport(send), decode=decode
    )
    context = PlanningInput("Read", "deployment-a", "high", ("fixture",))
    if invalid:
        with pytest.raises(AgentFault) as failure:
            await planner.plan_recorded(
                context, runner=test.runner, identity=identity(), validate=owner.validate_proposal
            )
        assert failure.value.code == "invalid_plan"
    else:
        approved = await planner.plan_recorded(
            context, runner=test.runner, identity=identity(), validate=owner.validate_proposal
        )
        assert isinstance(approved, Proposal)
    assert test.records[-1]["kind"] == "validation"
    assert test.records[-1]["data"] == {
        "status": "invalid" if invalid else "valid",
        "error_code": "invalid_plan" if invalid else None,
    }
    assert payloads(test)[0]["body"] == body


@pytest.mark.parametrize(
    "mismatch",
    [
        {"model": "deployment-b"},
        {"model": "PRIVATE-unlisted-model"},
        {"reasoning": {"effort": "low"}},
        {"reasoning": {"effort": None}},
        {"reasoning": None},
        {"reasoning": {}},
        {"reasoning": {"summary": "auto"}},
    ],
)
async def test_recorded_planner_selection_mismatch_rejected_before_intent(mismatch):
    test = boundary()
    transport = Transport(Mock(side_effect=AssertionError("No send on selection mismatch")))
    decode = Mock(return_value=proposal())
    planner = RecordedPlanner(
        build_request=lambda context: request(**mismatch), transport=transport, decode=decode
    )
    with pytest.raises(AgentFault) as failure:
        await planner.plan_recorded(
            PlanningInput("Read", "deployment-a", "high", ("fixture",)),
            runner=test.runner,
            identity=identity(),
            validate=lambda value: value,
        )
    assert failure.value.code == "planner_request_mismatch"
    assert "PRIVATE" not in failure.value.message
    assert not test.records and not transport.calls and not test.runner.poisoned
    decode.assert_not_called()
    test.failed.assert_not_called()


@pytest.fixture
def service_setup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("alpha\nbeta\ngamma\n")

    @asynccontextmanager
    async def opened(
        send, *, decode=None, timeout_seconds=1, build_request=None, models=("deployment-a",)
    ):
        transport = Transport(send)
        planner = RecordedPlanner(
            build_request=build_request
            or (
                lambda context: request(
                    input=[OPAQUE, {"role": "user", "content": context.request}],
                    instructions=context.instructions,
                    reasoning={"effort": context.reasoning_effort},
                )
            ),
            transport=transport,
            decode=decode or (lambda result: Proposal.model_validate(result.outputs[-1].body)),
        )
        owner = AgentService(
            SessionStore(tmp_path / "journal"),
            planner=planner,
            files=FilePolicy({"fixture": workspace}),
            models=models,
            planner_timeout_seconds=timeout_seconds,
        )
        await owner.start()
        session = await owner.create(CreateSession(**CREATE))
        try:
            yield owner, session.id, transport
        finally:
            await owner.close()

    return opened


async def drive(owner, sid, body=None, **kwargs):
    op = await owner.prepare(sid, body or StartRun(request_id="plan", content=PRIVATE), **kwargs)
    op.gate.set()
    await op.task
    return op, owner.run(await owner.get(sid), op.rid)


@pytest.mark.parametrize(
    "ending", ["raise_cancelled", "self_cancel", "self_cancel_suppressed", "timeout", "empty"]
)
async def test_service_non_success_transport_never_decodes_valid_partial_or_advances(
    service_setup,
    ending,
):
    body = proposal().model_dump()
    decode = Mock(return_value=proposal())

    async def send(raw, observe):
        if ending == "empty":
            return None
        await observe(Observation("response", body))
        if ending == "timeout":
            raise TimeoutError("PRIVATE adapter timeout")
        if ending == "raise_cancelled":
            raise asyncio.CancelledError("PRIVATE adapter cancellation")
        asyncio.current_task().cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            if ending != "self_cancel_suppressed":
                raise
        return Observation("response", {"late": body})

    async with service_setup(send, decode=decode) as (owner, sid, transport):
        op, run = await drive(owner, sid)
        assert run.status == "failed" and run.plan is None
        assert run.error.code == (
            "planner_timeout"
            if ending == "timeout"
            else ("planner_unavailable" if ending == "empty" else "operation_stopped")
        )
        decode.assert_not_called()
        assert not owner.store.poisoned and not op.stop.is_set()
        attempts = await owner.io(owner.store.llm_attempts, sid, op.rid)
        assert attempts["total"] == 1
        summary = attempts["attempts"][0]
        expected = {
            "raise_cancelled": ("cancelled", "provider_cancelled"),
            "self_cancel": ("cancelled", "provider_cancelled"),
            "self_cancel_suppressed": ("cancelled", "provider_cancelled"),
            "timeout": ("timed_out", "provider_timeout"),
            "empty": ("failed", "provider_no_output"),
        }[ending]
        assert (summary["outcome"]["status"], summary["outcome"]["error_code"]) == expected
        assert summary["validation"] is None
        stored = await owner.io(owner.store.llm_records, sid, summary["identity"]["attempt_id"])
        audits = [item["record"] for item in stored["records"]]
        observed = [] if ending == "empty" else [body]
        if ending == "self_cancel_suppressed":
            observed.append({"late": body})
        assert [item["data"]["body"] for item in audits if item["kind"] == "output"] == observed
        assert audits[-1]["kind"] == "outcome"
        assert audits[-1]["data"]["output_count"] == len(observed)
        records = owner.store.records[sid]
        assert not any(r.event.type in {"planner.result", "run.awaiting_approval"} for r in records)
        assert "PRIVATE adapter" not in json.dumps(audits)
        if ending != "empty":
            first = op.llm_runner.previous.identity
            retry = LLMIdentity.model_validate(
                {
                    **first.model_dump(),
                    "attempt_id": uuid4().hex,
                    "attempt": 2,
                    "retry_of": first.attempt_id,
                }
            )
            before = len(records)
            with pytest.raises(AgentFault):
                await op.llm_runner.run(retry, request(), transport)
            assert len(owner.store.records[sid]) == before
        assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "mismatch",
    [
        {"model": "deployment-b"},
        {"model": "PRIVATE-unlisted-model"},
        {"reasoning": {"effort": "low"}},
        {"reasoning": {"effort": None}},
        {"reasoning": None},
        {"reasoning": {}},
        {"reasoning": {"summary": "auto"}},
    ],
)
async def test_service_selection_mismatch_fails_before_any_llm_record_or_send(
    service_setup,
    mismatch,
):
    decode = Mock(return_value=proposal())
    send = Mock(side_effect=AssertionError("No send on selection mismatch"))
    async with service_setup(
        send,
        decode=decode,
        build_request=lambda context: request(**mismatch),
        models=("deployment-a", "deployment-b"),
    ) as (owner, sid, transport):
        _, run = await drive(owner, sid)
        assert run.status == "failed" and run.plan is None
        assert run.error.code == "planner_request_mismatch"
        assert not owner.store.poisoned and not transport.calls
        assert not any(r.event.type.startswith("llm.") for r in owner.store.records[sid])
        assert "PRIVATE-unlisted-model" not in run.error.message
        decode.assert_not_called()
        send.assert_not_called()


@pytest.mark.parametrize(
    "reasoning",
    [
        "omitted",
        None,
        {},
        {"summary": "auto"},
        {"effort": "high"},
        {"effort": "high", "summary": "auto"},
    ],
)
async def test_service_requires_reasoning_selection_without_rewriting_canonical_request(
    service_setup,
    reasoning,
):
    body = {"model": "deployment-a", "input": "Question"}
    if reasoning != "omitted":
        body["reasoning"] = reasoning
    sent = CanonicalRequest(body, instructions_version="fixture-v1")

    async def send(raw, observe):
        assert raw is sent.body_bytes
        return Observation("response", proposal().model_dump(), reported_model="provider-version")

    async with service_setup(send, build_request=lambda context: sent) as (owner, sid, transport):
        op, run = await drive(owner, sid)
        if not isinstance(reasoning, dict) or reasoning.get("effort") != "high":
            assert run.status == "failed" and run.error.code == "planner_request_mismatch"
            assert not transport.calls and not owner.store.poisoned
            assert not any(r.event.type.startswith("llm.") for r in owner.store.records[sid])
            return
        assert run.status == "awaiting_approval"
        assert run.plan.model == op.context.model == sent.body["model"]
        assert run.plan.reasoning_effort == op.context.reasoning_effort
        intent = next(r.audit for r in owner.store.records[sid] if r.event.type == "llm.intent")
        assert intent["data"]["request"] == sent.body
        assert len(transport.calls) == 1 and not owner.store.poisoned


async def test_service_frozen_model_must_still_be_allowed_before_recorded_planning(service_setup):
    send = Mock(side_effect=AssertionError("No send for a model outside the allowlist"))
    async with service_setup(send) as (owner, sid, transport):
        owner.models = ("deployment-b",)
        _, run = await drive(owner, sid)
        assert run.status == "failed" and run.plan is None and run.error.code == "invalid_model"
        assert not transport.calls and not owner.store.poisoned
        assert not any(r.event.type.startswith("llm.") for r in owner.store.records[sid])


async def assert_unrelated_session_usable(owner):
    other = await owner.create(CreateSession(**{**CREATE, "request_id": "unrelated"}))
    assert (await owner.get(other.id)).id == other.id
    assert not owner.store.poisoned


@pytest.mark.parametrize("gate", ["intent", "dispatched"])
async def test_committed_stop_beats_queued_authorization_with_unmodified_service(
    service_setup,
    monkeypatch,
    gate,
):
    async def send(raw, observe):
        pytest.fail("Revoked authorization must not enter transport")

    async with service_setup(send) as (owner, sid, transport):
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        loop = asyncio.get_running_loop()
        locked, intent_committed = asyncio.Event(), asyncio.Event()
        original = owner.store.append

        def watched_append(snapshot, kind, *args, **kwargs):
            result = original(snapshot, kind, *args, **kwargs)
            if kind == "llm.intent":
                loop.call_soon_threadsafe(intent_committed.set)
            return result

        monkeypatch.setattr(owner.store, "append", watched_append)

        async def hold_after_intent():
            await intent_committed.wait()
            await owner.lock.acquire()
            locked.set()

        holder = None
        if gate == "intent":
            await owner.lock.acquire()
            locked.set()
        else:
            holder = asyncio.create_task(hold_after_intent())
        op.gate.set()
        try:
            await asyncio.wait_for(locked.wait(), timeout=3)
            # The real audit callback is waiting for the coordinator lock. Use
            # the same locked Stop commit as cancel(), ahead of that callback.
            await wait_until(lambda: bool(owner.lock._waiters))
            await owner.stop_locked(op, "cancelled")
            assert owner.store.get(sid).runs[0].status == "cancelling"
        finally:
            if holder and not holder.done():
                holder.cancel()
            if holder:
                await asyncio.gather(holder, return_exceptions=True)
            if locked.is_set():
                owner.lock.release()
        await op.task
        run = owner.run(await owner.get(sid), op.rid)
        assert run.status == "cancelled" and run.plan is None and op.id not in owner.active
        audits = [r.audit for r in owner.store.records[sid] if r.event.type.startswith("llm.")]
        expected = [] if gate == "intent" else ["intent", "outcome"]
        assert [item["kind"] for item in audits] == expected
        if audits:
            assert audits[-1]["data"]["status"] == "not_dispatched"
            assert audits[-1]["data"]["dispatched"] is False
        assert not transport.calls and not op.llm_runner.poisoned
        await assert_unrelated_session_usable(owner)


@pytest.mark.parametrize("gate", ["intent", "dispatched"])
async def test_stop_during_authorization_snapshot_read_revokes_before_append(
    service_setup,
    monkeypatch,
    gate,
):
    async def send(raw, observe):
        pytest.fail("Stop during the read must win before authorization append")

    async with service_setup(send) as (owner, sid, transport):
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        entered, release = threading.Event(), threading.Event()
        original = owner.store.get

        def blocked_get(session_id):
            snapshot = original(session_id)
            attempts = owner.store._committed[sid].llm.attempts
            at_gate = not attempts if gate == "intent" else bool(attempts)
            if op.llm_runner is not None and at_gate and not entered.is_set():
                entered.set()
                assert release.wait(3), "Test did not release snapshot read"
            return snapshot

        monkeypatch.setattr(owner.store, "get", blocked_get)
        op.gate.set()
        cancel = None
        try:
            await wait_until(entered.is_set)
            cancel = asyncio.create_task(
                owner.cancel(sid, op.rid, Cancel(request_id="stop", operation_id=op.id))
            )
            await wait_until(op.stop.is_set)
        finally:
            release.set()
        await asyncio.gather(op.task, cancel)
        run = owner.run(await owner.get(sid), op.rid)
        assert run.status == "cancelled" and run.plan is None
        audits = [r.audit for r in owner.store.records[sid] if r.event.type.startswith("llm.")]
        expected = [] if gate == "intent" else ["intent", "outcome"]
        assert [item["kind"] for item in audits] == expected
        assert not transport.calls and not op.llm_runner.poisoned
        await assert_unrelated_session_usable(owner)


@pytest.mark.parametrize("gate", ["intent", "dispatched"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_stop_during_entered_authorization_write_retains_receipt_or_real_failure(
    service_setup,
    monkeypatch,
    gate,
    write_fails,
):
    async def send(raw, observe):
        pytest.fail("Stop during the writer await must prevent transport entry")

    async with service_setup(send) as (owner, sid, transport):
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        entered, release = threading.Event(), threading.Event()
        original = owner.store.append

        def blocked_append(snapshot, kind, *args, **kwargs):
            if kind == "llm." + gate:
                entered.set()
                assert release.wait(3), "Test did not release authorization write"
                if write_fails:
                    raise OSError("PRIVATE real recording failure concurrent with Stop")
            return original(snapshot, kind, *args, **kwargs)

        monkeypatch.setattr(owner.store, "append", blocked_append)
        op.gate.set()
        try:
            await wait_until(entered.is_set)
            owner.request_stop(op, "cancelled")
        finally:
            release.set()
        await op.task
        assert not transport.calls
        assert owner.store.poisoned is write_fails
        assert op.llm_runner.poisoned is write_fails
        if not write_fails:
            run = owner.run(await owner.get(sid), op.rid)
            assert run.status == "cancelled" and run.plan is None
            audits = [r.audit for r in owner.store.records[sid] if r.event.type.startswith("llm.")]
            expected = (
                ["intent", "outcome"] if gate == "intent" else ["intent", "dispatched", "outcome"]
            )
            assert [item["kind"] for item in audits] == expected
            assert audits[-1]["data"]["status"] == "not_dispatched"
            assert audits[-1]["data"]["dispatched"] is False
            await assert_unrelated_session_usable(owner)


@pytest.mark.parametrize("gate", ["intent", "dispatched"])
async def test_stop_beats_authorization_writer_queued_on_store_lock(
    service_setup,
    monkeypatch,
    gate,
):
    async def send(raw, observe):
        pytest.fail("A writer queued before Stop must not authorize transport")

    async with service_setup(send) as (owner, sid, transport):
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        original_get, original_lock = owner.store.get, owner.store.lock
        arm, hold, held, writer_waiting, release = (threading.Event() for _ in range(5))

        class ObservedLock:
            def __enter__(self):
                if arm.is_set() and not release.is_set():
                    writer_waiting.set()
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        def hold_store_lock():
            assert hold.wait(3), "Test did not reach the snapshot receipt"
            with original_lock:
                held.set()
                assert release.wait(3), "Test did not release store lock"

        def watched_get(session_id):
            snapshot = original_get(session_id)
            attempts = owner.store._committed[sid].llm.attempts
            at_gate = not attempts if gate == "intent" else bool(attempts)
            if op.llm_runner is not None and at_gate and not arm.is_set():
                hold.set()
                assert held.wait(3), "Test did not acquire store lock"
                arm.set()
            return snapshot

        monkeypatch.setattr(owner.store, "lock", ObservedLock())
        monkeypatch.setattr(owner.store, "get", watched_get)
        holder = threading.Thread(target=hold_store_lock, daemon=True)
        holder.start()
        op.gate.set()
        cancel = None
        try:
            await wait_until(writer_waiting.is_set)
            cancel = asyncio.create_task(
                owner.cancel(sid, op.rid, Cancel(request_id="stop", operation_id=op.id))
            )
            await wait_until(op.stop.is_set)
        finally:
            release.set()
            await asyncio.to_thread(holder.join, 3)
        await asyncio.gather(op.task, cancel)
        run = owner.run(await owner.get(sid), op.rid)
        assert run.status == "cancelled" and run.plan is None
        audits = [r.audit for r in owner.store.records[sid] if r.event.type.startswith("llm.")]
        expected = [] if gate == "intent" else ["intent", "outcome"]
        assert [item["kind"] for item in audits] == expected
        assert not transport.calls and not op.llm_runner.poisoned
        await assert_unrelated_session_usable(owner)


async def test_recorded_invalid_then_explicit_retry_matches_storage_lifecycle(service_setup):
    async def send(raw, observe):
        body = "invalid" if len(transport.calls) == 1 else proposal().model_dump()
        return Observation("response", body)

    async with service_setup(send) as (owner, sid, transport):
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))

        async def record(kind, audit):
            await owner.record_llm(op, kind, audit)

        runner = op.llm_runner = AttemptRunner(
            record=record,
            dispatch_check=owner.store._healthy,
            timeout_seconds=1,
            recording_failed=Mock(),
        )
        first = identity(
            session_id=sid,
            run_id=op.rid,
            operation_id=op.id,
            plan_id=op.plan_id,
            revision=op.revision,
            stage_id=op.stage_id,
        )
        invalid = await runner.run(first, request(), transport)
        await runner.validation(invalid, valid=False)
        assert len(transport.calls) == 1
        retry = LLMIdentity.model_validate(
            {
                **first.model_dump(),
                "attempt_id": uuid4().hex,
                "attempt": 2,
                "retry_of": first.attempt_id,
            }
        )
        valid = await runner.run(retry, request(), transport)
        assert Proposal.model_validate(valid.outputs[-1].body) == proposal()
        await runner.validation(valid, valid=True)
        attempts = await owner.io(owner.store.llm_attempts, sid, op.rid)
        assert attempts["total"] == 2
        statuses = [item["validation"]["status"] for item in attempts["attempts"]]
        assert statuses == ["valid", "invalid"]
        assert attempts["attempts"][0]["identity"]["retry_of"] == first.attempt_id
        assert len(transport.calls) == 2 and not owner.store.poisoned
        owner.request_stop(op, "interrupted")
        await op.task


async def test_recovery_retains_metadata_from_output_refs_when_outcome_write_failed(
    service_setup,
    monkeypatch,
):
    observations = [
        Observation("event", None, "first-model", "in_progress", {"input_tokens": 12}),
        Observation(
            "event",
            {"delta": "no metadata inside this body"},
            "last-model",
            "failed",
            {"input_tokens": 13, "output_tokens": 2},
            True,
        ),
    ]

    async def send(raw, observe):
        for observation in observations:
            await observe(observation)
        return None

    async with service_setup(send) as (owner, sid, transport):
        original = owner.store.append

        def fail_outcome(snapshot, kind, *args, **kwargs):
            if kind == "llm.outcome":
                raise OSError("PRIVATE failure before outcome append")
            return original(snapshot, kind, *args, **kwargs)

        monkeypatch.setattr(owner.store, "append", fail_outcome)
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        op.gate.set()
        await op.task
        assert owner.store.poisoned and len(transport.calls) == 1
        root = owner.store.root
    recovered = SessionStore(root)
    recovered.start()
    try:
        summary = recovered.llm_attempts(sid, op.rid)["attempts"][0]
        assert summary["outcome"]["status"] == "interrupted"
        assert summary["outcome"]["reported_model"] == "last-model"
        assert summary["outcome"]["provider_status"] == "failed"
        records = recovered.llm_records(sid, summary["identity"]["attempt_id"])["records"]
        audits = [item["record"] for item in records]
        outputs = [item["data"] for item in audits if item["kind"] == "output"]
        assert [item["reported_model"] for item in outputs] == ["first-model", "last-model"]
        assert outputs[-1]["failed"] is True
        assert audits[-1]["data"]["usage"] == {"input_tokens": 13, "output_tokens": 2}
        assert audits[-1]["data"]["output_count"] == 2
        assert not recovered.poisoned
    finally:
        recovered.close()


async def test_preallocated_durable_candidate_stage_ids_revision_digest_privacy_and_exact_approval(
    service_setup,
):
    seen = []

    async def send(raw, observe):
        records = owner.store.records[sid]
        intent = next(
            record.audit for record in reversed(records) if record.event.type == "llm.intent"
        )
        planning = next(
            record.audit for record in reversed(records) if record.event.type == "run.planning"
        )
        ident = intent["identity"]
        assert intent["data"]["request"] == json.loads(raw)
        assert ident["session_id"] == sid
        assert ident["plan_id"] == planning["plan_id"]
        assert ident["revision"] == planning["revision"]
        assert ident["stage_id"] == planning["stage_id"]
        assert ident["phase"] == planning["phase"]
        assert records[-1].event.type == "llm.dispatched"
        prior = owner.run(owner.store.get(sid), ident["run_id"]).plan
        assert prior is None or ident["revision"] == 2
        seen.append(ident)
        return Observation("response", {"proposal": proposal().model_dump(), "opaque": PRIVATE})

    async with service_setup(
        send, decode=lambda result: Proposal.model_validate(result.outputs[-1].body["proposal"])
    ) as (owner, sid, transport):
        reads = Mock(wraps=owner.files.read)
        owner.files.read = reads
        op, run = await drive(owner, sid)
        assert run.status == "awaiting_approval" and not reads.called
        assert (run.plan.id, run.plan.revision) == (op.plan_id, op.revision)
        assert seen[0]["plan_id"] == run.plan.id
        material = run.plan.model_dump(exclude={"digest"})
        assert run.plan.digest == canonical_digest(
            {"session": sid, "run": run.id, "plan": material}
        )
        assert len(transport.calls) == 1
        public = await owner.public_events(sid, 0)
        assert "encrypted_content" not in json.dumps(public)
        assert INSTRUCTIONS not in json.dumps(public)
        assert all("audit" not in event for event in public["events"])
        types = [record.event.type for record in owner.store.records[sid]]
        assert types.index("llm.validation") < types.index("run.awaiting_approval")
        approval = {
            "plan_id": run.plan.id,
            "revision": run.plan.revision,
            "digest": run.plan.digest,
        }
        revised_op, revised = await drive(
            owner,
            sid,
            Feedback(request_id="revise", kind="feedback", content="Shorter", **approval),
            rid=run.id,
            action="revise",
        )
        assert revised.plan.id == run.plan.id and revised.plan.revision == 2
        assert revised.plan.digest != run.plan.digest
        assert revised_op.stage_id != op.stage_id
        assert seen[1]["phase"] == "revision" and seen[1]["stage_id"] == revised_op.stage_id
        with pytest.raises(AgentFault) as stale:
            await owner.prepare(
                sid, PlanIdentity(request_id="stale", **approval), rid=run.id, action="execute"
            )
        assert stale.value.code == "stale_plan"
        _, finished = await drive(
            owner,
            sid,
            PlanIdentity(
                request_id="approve",
                plan_id=revised.plan.id,
                revision=2,
                digest=revised.plan.digest,
            ),
            rid=run.id,
            action="execute",
        )
        assert finished.status == "completed" and reads.call_count == 1
        assert finished.results[0].content == "1: alpha\n2: beta"
        assert len(transport.calls) == 2


@pytest.mark.parametrize("invalid", ["json", "schema", "tool", "policy", "not_proposal"])
async def test_invalid_decoder_schema_and_coordinator_tool_policy_record_validation_before_failure(
    service_setup,
    invalid,
):
    body = proposal().model_dump()
    if invalid == "schema":
        body.pop("objective")
    elif invalid == "tool":
        body["steps"][0]["tool"] = "web_search"
    elif invalid == "policy":
        body["steps"][0]["arguments"]["path"] = "../outside.txt"
    elif invalid == "json":
        body = "not valid plan JSON 原文"

    def decode(result):
        if invalid == "not_proposal":
            return result.outputs[-1].body
        return Proposal.model_validate(result.outputs[-1].body)

    async def send(raw, observe):
        return Observation("response", body)

    async with service_setup(send, decode=decode) as (owner, sid, transport):
        op, run = await drive(owner, sid)
        assert run.status == "failed" and run.plan is None and run.error.code == "invalid_plan"
        audits = [r.audit for r in owner.store.records[sid] if r.event.type.startswith("llm.")]
        assert next(item for item in audits if item["kind"] == "output")["data"]["body"] == body
        invalid_record = audits[-1]
        assert invalid_record["kind"] == "validation"
        assert invalid_record["data"] == {"status": "invalid", "error_code": "invalid_plan"}
        assert invalid_record["identity"]["plan_id"] == op.plan_id
        assert len(transport.calls) == 1
        assert not any(r.event.type == "run.awaiting_approval" for r in owner.store.records[sid])


async def test_service_shutdown_owns_late_observation_until_durable_outcome_without_publication(
    service_setup,
):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(raw, observe):
        await observe(Observation("event", {"delta": "partial"}))
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return Observation("response", proposal().model_dump())

    async with service_setup(send) as (owner, sid, transport):
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        op.gate.set()
        await asyncio.wait_for(entered.wait(), timeout=3)
        closing = asyncio.create_task(owner.close())
        try:
            await asyncio.wait_for(cancelled.wait(), timeout=3)
            assert not closing.done() and op.id in owner.active and owner.store.fd is not None
        finally:
            release.set()
            await closing
        records = settled_session_records(owner.store, sid)
        final = next(r.audit for r in reversed(records) if r.event.type == "llm.outcome")
        assert final["data"]["status"] == "interrupted" and final["data"]["output_count"] == 2
        assert records[-1].event.session.runs[0].status == "interrupted"
        assert not any(r.event.type == "run.awaiting_approval" for r in records)
        assert len(transport.calls) == 1


@pytest.mark.parametrize("prior_reason", [None, "cancelled"])
async def test_native_operation_cancel_after_validation_waits_for_full_settlement(
    service_setup,
    monkeypatch,
    prior_reason,
):
    async def send(raw, observe):
        return Observation("response", proposal().model_dump())

    async with service_setup(send) as (owner, sid, transport):
        validated, locked = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        original = owner.store.append

        def watched_append(snapshot, kind, *args, **kwargs):
            result = original(snapshot, kind, *args, **kwargs)
            if kind == "llm.validation":
                loop.call_soon_threadsafe(validated.set)
            return result

        async def hold_after_validation():
            await validated.wait()
            await owner.lock.acquire()
            locked.set()

        monkeypatch.setattr(owner.store, "append", watched_append)
        holder = asyncio.create_task(hold_after_validation())
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        op.gate.set()
        try:
            await asyncio.wait_for(locked.wait(), timeout=3)
            await wait_until(lambda: bool(owner.lock._waiters))
            if prior_reason:
                owner.signal_stop(op, prior_reason)
            op.task.cancel()
            await wait_until(op.stop.is_set)
            op.task.cancel()
            await asyncio.sleep(0)
            assert not op.task.done() and op.id in owner.active
            assert not op.settlement.done()
            assert op.reason == (prior_reason or "interrupted")
        finally:
            if not holder.done():
                holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)
            if locked.is_set():
                owner.lock.release()
        with pytest.raises(asyncio.CancelledError):
            await op.task
        run = owner.run(await owner.get(sid), op.rid)
        assert run.status == (prior_reason or "interrupted") and run.plan is None
        assert op.settlement.done() and op.id not in owner.active
        assert len(transport.calls) == 1
        assert not any(r.event.type == "run.awaiting_approval" for r in owner.store.records[sid])
        await assert_unrelated_session_usable(owner)


@pytest.mark.parametrize("published", ["awaiting_approval", "completed"])
async def test_repeated_native_cancel_during_final_cleanup_preserves_durable_publication(
    service_setup,
    monkeypatch,
    published,
):
    async def send(raw, observe):
        return Observation("response", proposal().model_dump())

    async with service_setup(send) as (owner, sid, transport):
        body = StartRun(request_id="plan", content="Read")
        kwargs = {}
        if published == "completed":
            _, reviewed = await drive(owner, sid)
            body = PlanIdentity(
                request_id="execute",
                plan_id=reviewed.plan.id,
                revision=reviewed.plan.revision,
                digest=reviewed.plan.digest,
            )
            kwargs = {"rid": reviewed.id, "action": "execute"}
        committed, entered, release = (threading.Event() for _ in range(3))
        original_append, original_get = owner.store.append, owner.store.get

        def watched_append(snapshot, kind, *args, **kw):
            result = original_append(snapshot, kind, *args, **kw)
            if kind == "run." + published:
                committed.set()
            return result

        def blocked_cleanup(session_id):
            snapshot = original_get(session_id)
            if committed.is_set() and not entered.is_set():
                entered.set()
                assert release.wait(3), "Test did not release final cleanup"
            return snapshot

        monkeypatch.setattr(owner.store, "append", watched_append)
        monkeypatch.setattr(owner.store, "get", blocked_cleanup)
        op = await owner.prepare(sid, body, **kwargs)
        op.gate.set()
        try:
            await wait_until(entered.is_set)
            op.task.cancel()
            await wait_until(op.stop.is_set)
            op.task.cancel()
            await asyncio.sleep(0)
            assert not op.task.done() and not op.settlement.done()
            assert op.id in owner.active and owner.io_tasks
            assert owner.store.fd is not None and owner.store.lease is not None
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await op.task
        run = owner.run(await owner.get(sid), op.rid)
        assert run.status == published and run.operation_id is None and run.error is None
        assert op.id not in owner.active and not owner.io_tasks
        assert len(transport.calls) == 1
        assert not any(r.event.type == "run.interrupted" for r in owner.store.records[sid])


async def test_native_cancel_before_settlement_body_starts_retains_registered_owner(
    service_setup,
    monkeypatch,
):
    async def send(raw, observe):
        pytest.fail("Cancellation before worker entry must prevent dispatch")

    async with service_setup(send) as (owner, sid, transport):
        release = asyncio.Event()
        original = owner.settle

        async def delayed(op):
            await release.wait()
            await original(op)

        monkeypatch.setattr(owner, "settle", delayed)
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        op.task.cancel()
        await wait_until(op.stop.is_set)
        op.task.cancel()
        await asyncio.sleep(0)
        assert not op.task.done() and op.id in owner.active
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await op.task
        assert owner.run(await owner.get(sid), op.rid).status == "interrupted"
        assert not owner.active and not transport.calls


async def test_repeatedly_cancelled_close_uses_one_owner_and_drains_late_transport(service_setup):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    late = {"late": proposal().model_dump(), "opaque": OPAQUE}

    async def send(raw, observe):
        await observe(Observation("event", {"delta": "partial"}))
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return Observation("response", late)

    async with service_setup(send) as (owner, sid, transport):
        closed = Mock(wraps=owner.store.close)
        owner.store.close = closed
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        op.gate.set()
        await asyncio.wait_for(entered.wait(), timeout=3)
        closing = asyncio.create_task(owner.close())
        concurrent = None
        try:
            await asyncio.wait_for(cancelled.wait(), timeout=3)
            shutdown = owner.shutdown_task
            concurrent = asyncio.create_task(owner.close())
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            assert owner.shutdown_task is shutdown and not shutdown.done()
            assert not closing.done() and not concurrent.done()
            assert op.id in owner.active and not op.task.done()
            assert owner.store.fd is not None and owner.store.lease is not None
            closed.assert_not_called()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await concurrent
        await owner.close()
        closed.assert_called_once()
        assert owner.shutdown_task is shutdown and shutdown.done()
        assert owner.store.fd is owner.store.lease is None
        assert not owner.active and not owner.io_tasks and not owner.control_tasks
        assert not owner.admissions and op.settlement.done()
        audits = [
            r.audit
            for r in settled_session_records(owner.store, sid)
            if r.event.type.startswith("llm.")
        ]
        assert audits[-1]["kind"] == "outcome"
        assert audits[-1]["data"]["status"] == "interrupted"
        assert audits[-1]["data"]["output_count"] == 2
        assert [a["data"]["body"] for a in audits if a["kind"] == "output"][-1] == late
        assert len(transport.calls) == 1


@pytest.mark.parametrize("kind", ["intent", "output", "validation"])
async def test_service_recording_failure_stops_operation_and_prevents_approval(
    service_setup,
    monkeypatch,
    kind,
):
    async def send(raw, observe):
        return Observation("response", proposal().model_dump())

    async with service_setup(send) as (owner, sid, transport):
        original = owner.store.append

        def append(snapshot, event_kind, *args, **kwargs):
            if event_kind == "llm." + kind:
                raise OSError("PRIVATE exception credentials must not persist")
            return original(snapshot, event_kind, *args, **kwargs)

        monkeypatch.setattr(owner.store, "append", append)
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        op.gate.set()
        await op.task
        assert owner.store.poisoned and op.stop.is_set() and not owner.active
        assert len(transport.calls) == (0 if kind == "intent" else 1)
        assert not any(r.event.type == "run.awaiting_approval" for r in owner.store.records[sid])
