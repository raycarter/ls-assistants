"""Offline synthesis boundary tests: no settings, runtime or persistent storage."""

import asyncio
import copy
import json
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest

from app.agent_files import AgentFault
from app.agent_llm import AttemptRunner
from app.agent_synthesis import (
    INSTRUCTIONS,
    INSTRUCTIONS_VERSION,
    SynthesisInput,
    build_recorded_synthesizer,
    build_request,
)
from app.llm_schemas import LLMIdentity, LLMRecord


def context():
    return SynthesisInput(
        request="总结 原文🌱",
        model="frozen-model",
        reasoning_effort="high",
        plan_json=json.dumps({"id": "plan", "steps": [{"id": "step_1"}]}),
        results_json=json.dumps(
            [
                {
                    "step_id": "step_1",
                    "content": "原文🌱",
                    "path": "文档.txt",
                    "offset": 4,
                    "line_count": 7,
                    "truncated": True,
                    "url": "https://example.invalid/source",
                    "unknown_provenance": {"v": 1},
                }
            ],
            ensure_ascii=False,
        ),
    )


def body(text="结论🌱 [step_1, 文档.txt:4]。来源已截断。"):
    return {
        "object": "response",
        "status": "completed",
        "model": "reported-model",
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "output": [
            {"type": "reasoning", "encrypted_content": "private opaque🌱", "summary": []},
            {
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        ],
        "error": None,
        "incomplete_details": None,
    }


def boundary(*, record_hook=None, timeout=2):
    records, failures = [], []

    async def record(kind, audit):
        LLMRecord.model_validate(audit)
        if record_hook:
            await record_hook(kind, audit)
        records.append(audit)

    runner = AttemptRunner(
        record=record,
        dispatch_check=lambda: None,
        timeout_seconds=timeout,
        recording_failed=lambda: failures.append(True),
    )
    identity = LLMIdentity(
        session_id="session",
        run_id="run",
        operation_id="operation",
        plan_id="plan",
        revision=1,
        phase="synthesis",
        stage_id="stage",
        step_id=None,
        interaction_id="interaction",
        attempt_id="attempt",
        attempt=1,
        retry_of=None,
        backoff_seconds=0.0,
    )
    return runner, identity, records, failures


async def bearer():
    return "PRIVATE_TOKEN"


def test_frozen_snapshots_and_tool_free_versioned_request():
    ctx = context()
    with pytest.raises(FrozenInstanceError):
        ctx.plan_json = "{}"
    with pytest.raises(TypeError):
        replace(ctx, results_json=[])
    request = build_request(ctx, 2048)
    wire = request.body
    assert request.instructions_version == INSTRUCTIONS_VERSION
    assert wire["instructions"] == INSTRUCTIONS
    assert wire["store"] is wire["stream"] is False
    assert wire["text"] == {"format": {"type": "text"}}
    assert wire["model"] == ctx.model
    assert wire["reasoning"] == {"effort": ctx.reasoning_effort}
    assert wire["max_output_tokens"] == 2048
    assert not {"tools", "tool_choice", "previous_response_id"} & wire.keys()
    data = json.loads(wire["input"][0]["content"])
    assert data == {
        "request": ctx.request,
        "approved_plan": json.loads(ctx.plan_json),
        "results": json.loads(ctx.results_json),
    }
    data["results"].clear()
    assert json.loads(build_request(ctx, 2048).body["input"][0]["content"])["results"]


@pytest.mark.parametrize("field", ["plan_json", "results_json"])
@pytest.mark.parametrize("text", ['{"x":1,"x":2}', "NaN", '{"x":1e999}', "{"])
def test_invalid_snapshot_json_rejected(field, text):
    with pytest.raises(ValueError):
        build_request(replace(context(), **{field: text}), 2048)


@pytest.mark.parametrize("tokens", [True, 0, -1, 1.5])
def test_invalid_token_budget(tokens):
    with pytest.raises(ValueError):
        build_request(context(), tokens)


async def test_exact_bytes_one_send_full_private_output_metadata_and_validation_order():
    runner, identity, records, failures = boundary()
    answer = "  结论🌱\n" + "full evidence 文档🌱" * 10000 + "\n "
    full = body(answer)
    full["private_extra"] = {"source": "unabridged🌱" * 10000}
    calls = []

    def handler(request):
        calls.append(request)
        assert request.content == build_request(context(), 2048).body_bytes
        assert request.method == "POST"
        assert str(request.url) == "https://provider.invalid/v1/responses"
        assert request.headers["authorization"] == "Bearer PRIVATE_TOKEN"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, json=full, headers={"private": "HEADER_SECRET"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synth = build_recorded_synthesizer(client, "https://provider.invalid/v1/", bearer, 2048)
        decode = synth.decode

        def checked_decode(result):
            assert records[-1]["kind"] == "outcome"
            assert records[-1]["data"]["status"] == "completed"
            return decode(result)

        synth.decode = checked_decode
        result = await synth.synthesize_recorded(context(), runner=runner, identity=identity)
        assert result == answer
        assert records[-1]["kind"] == "validation"
        assert records[-1]["data"] == {"status": "valid", "error_code": None}
    assert len(calls) == 1 and failures == []
    assert [r["kind"] for r in records] == [
        "intent",
        "dispatched",
        "output",
        "outcome",
        "validation",
    ]
    output = records[2]["data"]
    assert output["body"] == full
    for key in ("reported_model", "provider_status", "usage"):
        assert output[key] == records[3]["data"][key]
    assert output["reported_model"] == "reported-model"
    assert output["provider_status"] == "completed"
    assert output["usage"] == full["usage"]
    assert all(r["identity"] == identity.model_dump() for r in records)
    assert "PRIVATE_TOKEN" not in json.dumps(records)
    assert "HEADER_SECRET" not in json.dumps(records)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
        lambda b: b["output"][1].update(
            content=[{"type": "refusal", "refusal": "private refusal"}]
        ),
        lambda b: b["output"].append({"type": "function_call", "name": "read_file"}),
        lambda b: b["output"].append(copy.deepcopy(b["output"][1])),
        lambda b: b["output"][1]["content"].append({"type": "output_text", "text": "extra"}),
        lambda b: b["output"][1].update(status="in_progress"),
    ],
)
async def test_refusal_incomplete_tool_call_and_ambiguous_outputs(mutation):
    runner, identity, records, _ = boundary()
    full, calls = body(), []
    mutation(full)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=full)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synth = build_recorded_synthesizer(client, "https://provider.invalid/", bearer, 2048)
        with pytest.raises(AgentFault) as exc:
            await synth.synthesize_recorded(context(), runner=runner, identity=identity)
    assert exc.value.code == "synthesis_unavailable"
    assert len(calls) == 1
    assert records[2]["data"]["body"] == full
    assert records[-1]["data"]["status"] == "failed"
    assert not any(r["kind"] == "validation" for r in records)


@pytest.mark.parametrize("answer", ["", " \n\t "])
async def test_empty_answer_is_durably_invalid_before_safe_fault(answer):
    runner, identity, records, _ = boundary()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body(answer))),
    ) as client:
        synth = build_recorded_synthesizer(client, "https://provider.invalid/", bearer, 2048)
        with pytest.raises(AgentFault) as exc:
            await synth.synthesize_recorded(context(), runner=runner, identity=identity)
    assert exc.value.code == "invalid_answer"
    assert records[-2]["data"]["status"] == "completed"
    assert records[-1]["kind"] == "validation"
    assert records[-1]["data"]["status"] == "invalid"


@pytest.mark.parametrize("field,value", [("model", "changed"), ("reasoning_effort", "low")])
async def test_model_selection_checked_before_intent(field, value):
    runner, identity, records, _ = boundary()
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r)),
    ) as client:
        synth = build_recorded_synthesizer(client, "https://provider.invalid/", bearer, 2048)
        original = synth.build_request
        synth.build_request = lambda ctx: original(replace(ctx, **{field: value}))
        with pytest.raises(AgentFault) as exc:
            await synth.synthesize_recorded(context(), runner=runner, identity=identity)
    assert exc.value.code == "synthesis_request_mismatch"
    assert calls == records == []


@pytest.mark.parametrize("kind", ["intent", "output", "outcome", "validation"])
async def test_recording_failure_never_publishes_answer(kind):
    async def fail(record_kind, audit):
        if record_kind == "llm." + kind:
            raise OSError("PRIVATE_STORAGE_ERROR")

    runner, identity, records, failures = boundary(record_hook=fail)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synth = build_recorded_synthesizer(client, "https://provider.invalid/", bearer, 2048)
        with pytest.raises(AgentFault) as exc:
            await synth.synthesize_recorded(context(), runner=runner, identity=identity)
    assert exc.value.code == "storage_unavailable"
    assert failures == [True]
    assert len(calls) == (0 if kind == "intent" else 1)
    assert not any(r["kind"] == "validation" for r in records)


async def test_cancellation_during_validation_waits_for_durable_record_without_publishing():
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_validation(kind, audit):
        if kind == "llm.validation":
            entered.set()
            await release.wait()

    runner, identity, records, failures = boundary(record_hook=hold_validation)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body())),
    ) as client:
        synth = build_recorded_synthesizer(client, "https://provider.invalid/", bearer, 2048)
        task = asyncio.create_task(
            synth.synthesize_recorded(context(), runner=runner, identity=identity),
        )
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert records[-1]["kind"] == "validation"
    assert records[-1]["data"]["status"] == "valid"
    assert failures == []


@pytest.mark.parametrize("decoded", [None, {}, 42, " \n"])
async def test_injected_decoder_cannot_publish_non_answer(decoded):
    runner, identity, records, _ = boundary()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body())),
    ) as client:
        synth = build_recorded_synthesizer(client, "https://provider.invalid/", bearer, 2048)
        synth.decode = lambda result: decoded
        with pytest.raises(AgentFault) as exc:
            await synth.synthesize_recorded(context(), runner=runner, identity=identity)
    assert exc.value.code == "invalid_answer"
    assert records[-1]["data"]["status"] == "invalid"


@pytest.mark.parametrize("status", [429, 500, 307])
async def test_http_failure_records_evidence_without_retry(status):
    runner, identity, records, _ = boundary()
    calls, full = [], body()

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json=full,
            headers={"location": "https://other.invalid/"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        synth = build_recorded_synthesizer(client, "https://provider.invalid/", bearer, 2048)
        with pytest.raises(AgentFault) as exc:
            await synth.synthesize_recorded(context(), runner=runner, identity=identity)
    assert exc.value.code == "synthesis_unavailable"
    assert len(calls) == 1
    assert records[2]["data"]["body"] == full
    assert records[-1]["data"]["status"] == "failed"
    assert "PRIVATE_STORAGE_ERROR" not in json.dumps(records)


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_runner_owns_cancel_timeout_partial_evidence_and_close(stop):
    entered, closed = asyncio.Event(), asyncio.Event()
    runner, identity, records, _ = boundary(timeout=0.05 if stop == "timeout" else 2)
    calls = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial private evidence"
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=Stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synth = build_recorded_synthesizer(client, "https://provider.invalid/", bearer, 2048)
        task = asyncio.create_task(
            synth.synthesize_recorded(context(), runner=runner, identity=identity),
        )
        await entered.wait()
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(AgentFault) as exc:
                await task
            assert exc.value.code == "synthesis_timeout"
    assert closed.is_set() and len(calls) == 1
    assert records[-2]["data"]["body"]["data"] == "partial private evidence"
    assert records[-1]["data"]["status"] == ("cancelled" if stop == "cancel" else "timed_out")
    assert not any(r["kind"] == "validation" for r in records)
