"""Offline wire and recorded-boundary tests; no runtime/settings/storage access."""

import asyncio
import base64
import copy
import gzip
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.agent_files import AgentFault
from app.agent_llm import AttemptRunner
from app.agent_responses import (
    INSTRUCTIONS_VERSION,
    ResponsesTransport,
    _check_shape,
    build_recorded_planner,
    build_request,
    create_http_client,
    proposal_schema,
)
from app.agent_schemas import FetchArguments, ReadArguments
from app.llm_schemas import LLMIdentity, LLMRecord
from app.tools.registry import ToolRegistry


def context():
    return SimpleNamespace(
        request="读取 原文🌱",
        model="frozen-deployment",
        reasoning_effort="high",
        workspaces=("workspace_a",),
        feedback="修改 🌱",
        instructions="Backend rules",
        available_tools=tuple(ToolRegistry.descriptions[name] for name in ToolRegistry.enabled),
    )


def proposal():
    return {
        "objective": "读取🌱",
        "assumptions": [],
        "steps": [
            {
                "title": "Read",
                "description": "Read the document",
                "tool": "read_file",
                "arguments": {
                    "workspace_id": "workspace_a",
                    "path": "文档.txt",
                    "offset": 1,
                    "limit": 200,
                },
            }
        ],
    }


def body():
    return {
        "object": "response",
        "id": "provider_id",
        "status": "completed",
        "model": "reported-deployment",
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "output": [
            {"type": "reasoning", "encrypted_content": "opaque🌱", "summary": []},
            {
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(proposal(), ensure_ascii=False),
                        "annotations": [],
                    },
                ],
            },
        ],
        "error": None,
        "incomplete_details": None,
    }


def boundary():
    records = []

    async def record(kind, audit):
        LLMRecord.model_validate(audit)
        records.append(audit)

    runner = AttemptRunner(
        record=record,
        dispatch_check=lambda: None,
        timeout_seconds=2,
        recording_failed=lambda: pytest.fail("recording failed"),
    )
    identity = LLMIdentity(
        session_id="session",
        run_id="run",
        operation_id="operation",
        plan_id="plan",
        revision=1,
        phase="planning",
        stage_id="stage",
        step_id=None,
        interaction_id="interaction",
        attempt_id="attempt",
        attempt=1,
        retry_of=None,
        backoff_seconds=0.0,
    )
    return runner, identity, records


async def bearer():
    return "PRIVATE_TOKEN"


async def run_planner(handler, *, provider=bearer, validate=lambda p: p):
    runner, identity, records = boundary()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        planner = build_recorded_planner(client, "https://provider.invalid/v1/", provider, 2048)
        try:
            result = await planner.plan_recorded(
                context(), runner=runner, identity=identity, validate=validate
            )
        except AgentFault as exc:
            result = exc
    return result, records


def test_request_and_manually_reviewed_schema():
    request = build_request(context(), 2048)
    wire = request.body
    assert request.instructions_version == INSTRUCTIONS_VERSION == "agent-responses-planning-v3"
    assert wire["text"]["format"]["name"] == "agent_plan_v3"
    assert wire["model"] == "frozen-deployment"
    assert wire["reasoning"] == {"effort": "high"}
    assert wire["store"] is wire["stream"] is False
    assert wire["max_output_tokens"] == 2048
    assert INSTRUCTIONS_VERSION in wire["instructions"]
    assert "Backend rules" in wire["instructions"]
    assert not {"tools", "tool_choice", "previous_response_id"} & wire.keys()
    data = json.loads(wire["input"][0]["content"])
    assert data == {
        "request": context().request,
        "workspace_ids": ["workspace_a"],
        "available_tool_descriptions": list(context().available_tools),
        "revision_feedback": "修改 🌱",
    }

    def review(node):
        if isinstance(node, dict):
            assert not {"oneOf", "discriminator", "default", "limits", "id"} & node.keys()
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                review(value)
        elif isinstance(node, list):
            for value in node:
                review(value)

    schema = wire["text"]["format"]["schema"]
    review(schema)
    assert schema == proposal_schema()
    assert schema["properties"]["steps"]["minItems"] == 1
    assert len(schema["properties"]["steps"]["items"]["anyOf"]) == 2


def test_request_explains_relative_paths_and_distinct_paging_units_and_defaults():
    wire = build_request(context(), 2048).body
    instructions = wire["instructions"]
    for text in (
        "exact supplied workspace ID",
        "workspace-relative path",
        "note.txt",
        "never an absolute path or a workspace-ID prefix",
        "one-based line",
        "start at 1",
        "1..200 (normally 200)",
        "zero-based character",
        "start at 0",
        "1..16000 (normally 12000)",
    ):
        assert text in instructions
    descriptions = json.loads(wire["input"][0]["content"])["available_tool_descriptions"]
    read, fetch = descriptions
    for text in (
        "exact supplied workspace ID",
        "workspace-relative path",
        "note.txt",
        "never an absolute path",
        "one-based line number (normally 1)",
        "1..200 lines (normally 200)",
    ):
        assert text in read
    for text in (
        "zero-based character position (normally 0)",
        "1..16000 characters (normally 12000)",
    ):
        assert text in fetch


@pytest.mark.parametrize(
    "tool,model,minimum,maximum,unit,offset_default,limit_default",
    [
        ("read_file", ReadArguments, 1, 200, "line", 1, 200),
        ("web_fetch", FetchArguments, 0, 16000, "character", 0, 12000),
    ],
)
def test_provider_paging_schema_matches_argument_models(
    tool, model, minimum, maximum, unit, offset_default, limit_default
):
    variants = proposal_schema()["properties"]["steps"]["items"]["anyOf"]
    variant = next(v for v in variants if v["properties"]["tool"]["enum"] == [tool])
    fields = variant["properties"]["arguments"]["properties"]
    domain = model.model_json_schema()["properties"]
    for name, lower, upper, default in (
        ("offset", minimum, None, offset_default),
        ("limit", 1, maximum, limit_default),
    ):
        assert fields[name]["type"] == domain[name]["type"] == "integer"
        assert fields[name]["minimum"] == domain[name]["minimum"] == lower
        assert fields[name].get("maximum") == domain[name].get("maximum") == upper
        assert unit in fields[name]["description"]
        assert f"normally {default}" in fields[name]["description"]
        assert model.model_fields[name].default == default
    if tool == "read_file":
        assert "Workspace-relative" in fields["path"]["description"]
        assert "not an absolute path" in fields["path"]["description"]
        assert "exact supplied workspace ID" in fields["workspace_id"]["description"]


@pytest.mark.parametrize(
    "tool,offset,limit,valid",
    [
        ("read_file", 0, 200, False),
        ("read_file", -1, 200, False),
        ("read_file", 1, 0, False),
        ("read_file", 1, 1, True),
        ("read_file", 1, 200, True),
        ("read_file", 1, 201, False),
        ("read_file", 1, 12000, False),
        ("read_file", 0, 12000, False),
        ("read_file", 10**30, 200, True),
        ("web_fetch", 0, 12000, True),
        ("web_fetch", 0, 16000, True),
        ("web_fetch", 0, 1, True),
        ("web_fetch", -1, 12000, False),
        ("web_fetch", 0, 0, False),
        ("web_fetch", 0, 16001, False),
        ("web_fetch", 10**30, 16000, True),
    ],
)
async def test_paging_bounds_enforced_locally_and_at_recorded_boundary(tool, offset, limit, valid):
    plan = proposal()
    arguments = (
        {"workspace_id": "workspace_a", "path": "note.txt"}
        if tool == "read_file"
        else {"url": "https://example.invalid/note.txt"}
    ) | {"offset": offset, "limit": limit}
    plan["steps"][0].update(tool=tool, arguments=arguments)
    unchanged = copy.deepcopy(plan)
    model = ReadArguments if tool == "read_file" else FetchArguments
    if valid:
        _check_shape(plan, proposal_schema())
        assert model.model_validate(arguments).model_dump() == arguments
    else:
        with pytest.raises(ValueError):
            _check_shape(plan, proposal_schema())
        with pytest.raises(ValueError):
            model.model_validate(arguments)
    full = body()
    full["output"][1]["content"][0]["text"] = json.dumps(plan)
    calls, validated = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=full)

    result, records = await run_planner(handler, validate=lambda p: validated.append(p) or p)
    assert len(calls) == 1
    assert plan == unchanged
    assert next(r["data"]["body"] for r in records if r["kind"] == "output") == full
    assert records[0]["data"]["instructions_version"] == "agent-responses-planning-v3"
    assert records[-2]["data"]["status"] == "completed"
    if valid:
        assert result.steps[0].arguments.model_dump() == arguments
        assert validated == [result]
        assert records[-1]["data"]["status"] == "valid"
    else:
        assert result.code == "invalid_plan"
        assert validated == []
        assert records[-1]["data"]["status"] == "invalid"


async def test_exact_wire_unicode_full_body_metadata_and_coordinator_validation():
    calls = []
    full = body()
    full["opaque_extra"] = "未经裁剪🌱" * 10000

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://provider.invalid/v1/responses"
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer PRIVATE_TOKEN"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept-encoding"] == "identity"
        assert request.content == build_request(context(), 2048).body_bytes
        assert "原文🌱".encode() in request.content
        return httpx.Response(200, json=full, headers={"private": "HEADER_SECRET"})

    validated = []
    result, records = await run_planner(handler, validate=lambda p: validated.append(p) or p)
    assert result.objective == "读取🌱"
    assert validated == [result]
    assert len(calls) == 1
    output = next(r["data"] for r in records if r["kind"] == "output")
    assert output["body"] == full
    assert output["reported_model"] == full["model"]
    assert output["provider_status"] == "completed"
    assert output["usage"] == full["usage"]
    assert records[-1]["data"]["status"] == "valid"
    assert "PRIVATE_TOKEN" not in json.dumps(records)
    assert "HEADER_SECRET" not in json.dumps(records)
    assert records[0]["data"]["request"] == build_request(context(), 2048).body


@pytest.mark.parametrize("status", [429, 500, 503, 301, 302, 307, 308])
async def test_http_failure_one_send_retains_body(status):
    calls = []
    full = body()

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=full, headers={"location": "https://other.invalid/"})

    result, records = await run_planner(handler)
    assert result.code == "planner_unavailable"
    assert len(calls) == 1
    output = next(r["data"] for r in records if r["kind"] == "output")
    assert output["body"] == full and output["failed"]
    assert output["kind"] == "error_body"
    assert records[-1]["data"]["usage"] == full["usage"]


@pytest.mark.parametrize(
    "error,code",
    [
        (httpx.ConnectError, "planner_unavailable"),
        (httpx.ReadTimeout, "planner_timeout"),
    ],
)
async def test_network_error_one_send_sanitized(error, code):
    calls = []

    def handler(request):
        calls.append(request)
        raise error("PRIVATE_EXCEPTION Authorization: Bearer secret")

    result, records = await run_planner(handler)
    assert result.code == code
    assert len(calls) == 1
    assert "PRIVATE_EXCEPTION" not in json.dumps(records)
    assert not any(r["kind"] == "output" for r in records)


async def test_token_error_no_send_sanitized():
    calls = []

    async def failing_token():
        raise RuntimeError("PRIVATE_TOKEN_EXCEPTION")

    result, records = await run_planner(lambda r: calls.append(r), provider=failing_token)
    assert result.code == "planner_unavailable"
    assert calls == []
    assert "PRIVATE_TOKEN_EXCEPTION" not in json.dumps(records)


@pytest.mark.parametrize(
    "raw",
    [
        "not JSON 🌱",
        '{"truncated":',
        '{"x":1,"x":2}',
        "NaN",
        '{"usage":1e999}',
    ],
)
async def test_malformed_body_retained_as_raw_text(raw):
    result, records = await run_planner(lambda r: httpx.Response(200, text=raw))
    assert result.code == "planner_unavailable"
    assert next(r["data"]["body"] for r in records if r["kind"] == "output") == raw


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
        lambda b: b.update(status="failed", error={"code": "server_error"}),
        lambda b: b.update(status="queued"),
        lambda b: b.update(status="in_progress"),
        lambda b: b.update(error={"code": "error_even_on_200"}),
        lambda b: b["output"][1].update(status="in_progress"),
        lambda b: b["output"][1].update(content=[{"type": "refusal", "refusal": "No"}]),
        lambda b: b["output"].append({"type": "function_call", "name": "read_file"}),
        lambda b: b["output"].append(copy.deepcopy(b["output"][1])),
        lambda b: b["output"][1]["content"].append({"type": "output_text", "text": "{}"}),
        lambda b: b.pop("output"),
        lambda b: b.pop("status"),
    ],
)
async def test_nonterminal_refusal_toolcall_and_ambiguous_outputs_fail_with_evidence(mutation):
    full = body()
    mutation(full)
    result, records = await run_planner(lambda r: httpx.Response(200, json=full))
    assert result.code == "planner_unavailable"
    output = next(r["data"] for r in records if r["kind"] == "output")
    assert output["body"] == full and output["failed"]
    assert output["usage"] == full["usage"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(steps=[]),
        lambda p: p.update(limits={"max_steps": 3}),
        lambda p: p["steps"][0].update(id="server_id"),
        lambda p: p["steps"][0]["arguments"].pop("offset"),
        lambda p: p["steps"][0]["arguments"].update(offset=True),
        lambda p: p["steps"][0]["arguments"].update(limit=201),
        lambda p: p["steps"][0].update(tool="web_fetch"),
        lambda p: p.update(objective=""),
    ],
)
async def test_invalid_proposal_retained_then_validation_failed(mutation):
    plan = proposal()
    mutation(plan)
    full = body()
    full["output"][1]["content"][0]["text"] = json.dumps(plan)
    result, records = await run_planner(lambda r: httpx.Response(200, json=full))
    assert result.code == "invalid_plan"
    assert next(r["data"]["body"] for r in records if r["kind"] == "output") == full
    assert records[-2]["data"]["status"] == "completed"
    assert records[-1]["data"]["status"] == "invalid"


async def test_coordinator_policy_rejection_is_durably_invalid():
    def reject(plan):
        raise ValueError("Coordinator policy rejection")

    result, records = await run_planner(lambda r: httpx.Response(200, json=body()), validate=reject)
    assert result.code == "invalid_plan"
    assert records[-1]["data"]["status"] == "invalid"


async def test_valid_web_fetch_proposal():
    full = body()
    plan = proposal()
    plan["steps"][0].update(
        tool="web_fetch",
        arguments={"url": "https://example.invalid/文档", "offset": 0, "limit": 12000},
    )
    full["output"][1]["content"][0]["text"] = json.dumps(plan)
    result, records = await run_planner(lambda r: httpx.Response(200, json=full))
    assert result.steps[0].tool == "web_fetch"
    assert result.steps[0].arguments.url == "https://example.invalid/文档"
    assert records[-1]["data"]["status"] == "valid"


@pytest.mark.parametrize("text", ["not JSON", '{"objective":', '{"x":1,"x":2}', "NaN"])
async def test_malformed_proposal_json_is_recorded_before_invalid_validation(text):
    full = body()
    full["output"][1]["content"][0]["text"] = text
    result, records = await run_planner(lambda r: httpx.Response(200, json=full))
    assert result.code == "invalid_plan"
    assert next(r["data"]["body"] for r in records if r["kind"] == "output") == full
    assert records[-1]["data"]["status"] == "invalid"


@pytest.mark.parametrize("field,value", [("model", "changed"), ("reasoning_effort", "low")])
async def test_frozen_selection_mismatch_blocks_dispatch(field, value):
    runner, identity, records = boundary()
    calls = []
    mock_transport = httpx.MockTransport(lambda r: calls.append(r))
    async with httpx.AsyncClient(transport=mock_transport) as client:
        planner = build_recorded_planner(client, "https://provider.invalid/", bearer, 2048)
        original = planner.build_request

        def mismatched(ctx):
            changed = copy.copy(ctx)
            setattr(changed, field, value)
            return original(changed)

        planner.build_request = mismatched
        with pytest.raises(AgentFault) as exc:
            await planner.plan_recorded(
                context(), runner=runner, identity=identity, validate=lambda p: p
            )
        assert exc.value.code == "planner_request_mismatch"
    assert calls == records == []


async def test_redirect_disabled_even_if_injected_client_enables_it():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            307, text="Redirect body 🌱", headers={"location": "https://other.invalid/"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        transport = ResponsesTransport(client, "https://provider.invalid/", bearer)
        observations = []

        async def observe(item):
            observations.append(item)

        result = await transport.send_once(
            build_request(context(), 2048).body_bytes,
            observe,
        )
    assert result is None
    (observation,) = observations
    assert len(calls) == 1
    assert observation.failed
    assert observation.body == "Redirect body 🌱"


async def test_client_configuration_and_close():
    real_transport = httpx.AsyncHTTPTransport
    with patch("app.agent_responses.httpx.AsyncHTTPTransport", wraps=real_transport) as transport:
        client = create_http_client()
        transport.assert_called_once_with(retries=0, trust_env=False)
        assert client.follow_redirects is False
        assert client.trust_env is False
        await client.aclose()
        assert client.is_closed


async def test_cancellation_releases_response_stream_without_retry():
    entered = asyncio.Event()
    closed = asyncio.Event()
    calls = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            closed.set()

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=Stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ResponsesTransport(client, "https://provider.invalid/", bearer)
        task = asyncio.create_task(
            transport.send_once(build_request(context(), 2048).body_bytes, lambda o: None)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert len(calls) == 1


async def test_client_defaults_auth_and_cookies_never_enter_wire_or_resend():
    calls = []

    class ResendingAuth(httpx.Auth):
        def auth_flow(self, request):
            pytest.fail("Ambient auth must not run")
            yield request
            yield request

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer PRIVATE_TOKEN"
        assert request.headers["accept-encoding"] == "identity"
        assert "cookie" not in request.headers
        assert "x-client-secret" not in request.headers
        assert request.extensions["timeout"] == httpx.Timeout(7).as_dict()
        assert request.content == build_request(context(), 2048).body_bytes
        return httpx.Response(
            401 if len(calls) == 1 else 200,
            json=body(),
            headers={"set-cookie": "provider_secret=secret; Path=/"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        auth=ResendingAuth(),
        timeout=7,
        headers={"X-Client-Secret": "secret", "Authorization": "ambient"},
        cookies={"ambient_cookie": "secret"},
    ) as client:
        transport = ResponsesTransport(client, "https://provider.invalid/", bearer)
        observations = []

        async def observe(item):
            observations.append(item)

        for _ in range(2):
            assert (
                await transport.send_once(build_request(context(), 2048).body_bytes, observe)
                is None
            )
        assert client.cookies.get("provider_secret") == "secret"
    assert len(calls) == len(observations) == 2


@pytest.mark.parametrize(
    "raw",
    [
        b'{"usage":' + b"[" * 300 + b"0" + b"]" * 300 + b"}",
        b"[" * 2000 + b"0" + b"]" * 2000,
        b'{"model":"\\ud800","status":"completed","usage":{"input_tokens":1}}',
        b'{"usage":{"text":"\\udfff"}}',
    ],
)
async def test_unsafe_json_falls_back_to_complete_raw_text_without_poisoning_journal(raw):
    result, records = await run_planner(lambda r: httpx.Response(200, content=raw))
    assert result.code == "planner_unavailable"
    output = next(r["data"] for r in records if r["kind"] == "output")
    assert output["body"] == raw.decode("utf-8")
    assert output["failed"]
    assert output["usage"] is output["reported_model"] is output["provider_status"] is None
    json.dumps(records, ensure_ascii=False, allow_nan=False).encode("utf-8")


async def test_invalid_utf8_complete_body_reversible():
    raw = b"provider bytes\xff\xfe\x00"
    result, records = await run_planner(lambda r: httpx.Response(502, content=raw))
    assert result.code == "planner_unavailable"
    output = next(r["data"] for r in records if r["kind"] == "output")
    representation = output["body"]
    assert representation["adapter_body_representation"] is True
    assert representation["partial"] is False
    assert representation["encoding"] == "base64"
    assert base64.b64decode(representation["data"]) == raw


@pytest.mark.parametrize("failure", ["read", "timeout", "cancel"])
@pytest.mark.parametrize("raw", [b"partial \xe5\x8e\x9f", b"partial\xff\xe5"])
async def test_partial_read_is_observed_before_failure_and_close(failure, raw):
    events = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw
            if failure == "read":
                raise httpx.ReadError("PRIVATE_READ_ERROR")
            if failure == "timeout":
                raise httpx.ReadTimeout("PRIVATE_TIMEOUT")
            raise asyncio.CancelledError

        async def aclose(self):
            events.append("close")
            raise RuntimeError("PRIVATE_CLOSE_ERROR")

    def handler(request):
        events.append("send")
        return httpx.Response(200, stream=Stream())

    async def observe(item):
        LLMRecord.model_validate(
            {
                "llm_audit_version": 1,
                "kind": "output",
                "identity": boundary()[1],
                "at": "2026-10-23T00:00:00+00:00",
                "data": {
                    "index": 1,
                    "kind": item.kind,
                    "body": item.body,
                    "failed": item.failed,
                },
            }
        )
        events.append(item)

    expected = {"read": httpx.ReadError, "timeout": TimeoutError, "cancel": asyncio.CancelledError}[
        failure
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ResponsesTransport(client, "https://provider.invalid/", bearer)
        with pytest.raises(expected):
            await transport.send_once(build_request(context(), 2048).body_bytes, observe)
    assert len(events) == 3
    assert events[0] == "send" and events[-1] == "close"
    item = events[1]
    assert item.failed and item.kind == "error_body"
    assert item.body["adapter_body_representation"] is item.body["partial"] is True
    recovered = (
        base64.b64decode(item.body["data"])
        if item.body["encoding"] == "base64"
        else item.body["data"].encode("utf-8")
    )
    assert recovered == raw


async def test_complete_body_survives_close_error_without_duplicate_observation():
    full = body()
    closes = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raw = json.dumps(full, ensure_ascii=False).encode("utf-8")
            yield raw[:7]
            yield raw[7:]

        async def aclose(self):
            closes.append(True)
            raise RuntimeError("PRIVATE_CLOSE_ERROR")

    result, records = await run_planner(lambda r: httpx.Response(200, stream=Stream()))
    assert result.objective == "读取🌱"
    outputs = [r["data"] for r in records if r["kind"] == "output"]
    assert len(outputs) == 1 and outputs[0]["body"] == full
    assert closes == [True]
    assert "PRIVATE_CLOSE_ERROR" not in json.dumps(records)


@pytest.mark.parametrize(
    "failure,code,outcome",
    [
        (httpx.ReadError, "planner_unavailable", "failed"),
        (httpx.ReadTimeout, "planner_timeout", "timed_out"),
    ],
)
async def test_partial_body_retained_by_runner_before_sanitized_outcome(failure, code, outcome):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"partial":'
            raise failure("PRIVATE_READ_ERROR")

    result, records = await run_planner(lambda r: httpx.Response(200, stream=Stream()))
    assert result.code == code
    outputs = [r["data"] for r in records if r["kind"] == "output"]
    assert len(outputs) == 1
    assert outputs[0]["body"]["data"] == '{"partial":'
    assert outputs[0]["body"]["partial"] is True
    assert records[-1]["data"]["status"] == outcome
    assert "PRIVATE_READ_ERROR" not in json.dumps(records)


async def test_runner_cancellation_owns_partial_recording_and_close():
    entered = asyncio.Event()
    closed = asyncio.Event()
    runner, identity, records = boundary()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"observable partial"
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            assert any(r["kind"] == "output" for r in records)
            closed.set()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream())),
    ) as client:
        planner = build_recorded_planner(client, "https://provider.invalid/", bearer, 2048)
        task = asyncio.create_task(
            planner.plan_recorded(
                context(),
                runner=runner,
                identity=identity,
                validate=lambda p: p,
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
    assert len([r for r in records if r["kind"] == "output"]) == 1
    assert records[-1]["data"]["status"] == "cancelled"


async def test_cancellation_during_close_preserves_complete_body_and_waits_for_close():
    closing = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    observations = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps(body()).encode()

        async def aclose(self):
            closing.set()
            await release.wait()
            closed.set()

    async def observe(item):
        observations.append(item)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream())),
    ) as client:
        transport = ResponsesTransport(client, "https://provider.invalid/", bearer)
        task = asyncio.create_task(
            transport.send_once(build_request(context(), 2048).body_bytes, observe)
        )
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
    assert len(observations) == 1 and observations[0].body == body()


@pytest.mark.parametrize("variant", ["gzip", "concatenated", "missing_trailer"])
async def test_unsupported_gzip_retains_every_original_raw_byte(variant):
    full = body()
    raw = gzip.compress(json.dumps(full).encode())
    if variant == "concatenated":
        raw += gzip.compress(b"additional member must not disappear")
    elif variant == "missing_trailer":
        raw = raw[:-8]

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw[:5]
            yield raw[5:]

    result, records = await run_planner(
        lambda r: httpx.Response(
            200,
            stream=Stream(),
            headers={"content-encoding": "gzip"},
        )
    )
    assert result.code == "planner_unavailable"
    outputs = [r["data"] for r in records if r["kind"] == "output"]
    assert len(outputs) == 1
    output = outputs[0]
    assert output["kind"] == "error_body" and output["failed"]
    assert output["body"]["partial"] is False
    assert output["body"]["encoding"] == "base64"
    assert base64.b64decode(output["body"]["data"]) == raw
    assert output["reported_model"] is output["provider_status"] is output["usage"] is None
    assert records[-1]["data"]["status"] == "failed"


@pytest.mark.parametrize("encoding", [None, "", "identity", " Identity "])
async def test_identity_encoding_stream_success(encoding):
    full = body()
    raw = json.dumps(full, ensure_ascii=False).encode("utf-8")

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw[:5]
            yield raw[5:]

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            stream=Stream(),
            headers=({} if encoding is None else {"content-encoding": encoding}),
        )

    result, records = await run_planner(handler)
    assert result.objective == "读取🌱"
    outputs = [r["data"] for r in records if r["kind"] == "output"]
    assert len(outputs) == 1
    assert outputs[0]["body"] == full and not outputs[0]["failed"]


@pytest.mark.parametrize("encoding", ["br", "deflate", "gzip, identity", "unknown"])
async def test_any_unsupported_encoding_rejected_even_for_valid_json_bytes(encoding):
    raw = json.dumps(body()).encode()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw

    result, records = await run_planner(
        lambda r: httpx.Response(
            200,
            stream=Stream(),
            headers={"content-encoding": encoding},
        )
    )
    assert result.code == "planner_unavailable"
    output = next(r["data"] for r in records if r["kind"] == "output")
    assert output["body"] == raw.decode("utf-8")
    assert output["failed"] and output["kind"] == "error_body"
