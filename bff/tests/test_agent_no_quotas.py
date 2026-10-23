"""Former quota boundaries, legacy approvals, and independently owned request timeouts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

import app.agent as agent_module
from app.agent import AgentService
from app.agent_files import AgentFault, canonical_digest
from app.agent_routes import AgentStreamingResponse
from app.agent_schemas import (
    AgentPlan,
    AgentRun,
    AgentSession,
    CreateSession,
    LegacyPlanLimits,
    PatchSession,
    PlanIdentity,
    Proposal,
    ReadArguments,
    StartRun,
    StepResult,
)
from tests.test_agent import (
    BASE,
    CREATE,
    BlockingFiles,
    approval,
    consume,
    decode,
    planned,
    proposal,
    run_detail,
    wait_until,
)
from tests.test_agent import agent_setup as agent_setup


def many_steps(count=5):
    original = proposal().model_dump()
    original["steps"] *= count
    return Proposal.model_validate(original)


def test_uncapped_bodies_collections_and_legacy_limits_preserve_protocol_validation():
    proposed = many_steps()
    proposed.objective = "x" * 8001
    proposed.assumptions = ["assumption"] * 10
    proposed.limits = LegacyPlanLimits(max_steps=1, max_tool_calls=1, timeout_seconds=3600)
    assert Proposal.model_validate(proposed.model_dump()) == proposed
    assert LegacyPlanLimits(max_steps=1000, max_tool_calls=1000).max_steps == 1000
    assert ReadArguments(workspace_id="fixture", path="note.txt", offset=10001).offset == 10001
    result = StepResult(step_id="step", status="completed", content="x" * 24001, time="now")
    run = AgentRun(
        id="run",
        request="r" * 8001,
        created_at="now",
        updated_at="now",
        status="completed",
        results=[result] * 4,
    )
    session = AgentSession(
        id="session",
        title="title",
        model="deployment-a",
        reasoning_effort="high",
        created_at="now",
        updated_at="now",
        runs=[run] * 9,
    )
    assert AgentSession.model_validate_json(session.model_dump_json()) == session
    assert PlanIdentity(request_id="approve", plan_id="plan", revision=99, digest="0" * 64)
    assert "limits" not in proposal().model_dump()
    assert "limits" not in json.loads(proposal().model_dump_json())
    # Optional plan material is omitted locally, never by changing command hash rules.
    assert PatchSession(request_id="patch").model_dump() == {
        "request_id": "patch",
        "model": None,
        "reasoning_effort": None,
        "title": None,
    }
    for args in ({"offset": 0}, {"limit": 201}, {"workspace_id": "bad/key"}):
        with pytest.raises(ValidationError):
            ReadArguments.model_validate({"workspace_id": "fixture", "path": "note.txt", **args})


@pytest.mark.parametrize(
    "field",
    [
        "planner_timeout_seconds",
        "file_timeout_seconds",
        "send_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "30", None])
def test_request_timeouts_must_be_positive_and_finite(field, value):
    with pytest.raises(ValueError, match="positive and finite"):
        AgentService(**{field: value})


async def test_new_plan_strips_injected_legacy_limits_and_has_its_own_digest(agent_setup):
    proposed = many_steps()
    proposed.limits = LegacyPlanLimits(max_steps=1, max_tool_calls=1, timeout_seconds=1)
    proposed.assumptions *= 10
    agent_setup.planner.plan.return_value = proposed
    async with agent_setup.opened() as (client, owner, _):
        sid, run, frames = await planned(client)
        plan = run["plan"]
        assert "limits" not in plan and "limits" not in frames[-1]["plan"]
        assert len(plan["steps"]) == 5 and len(plan["assumptions"]) == 11
        material = {key: value for key, value in plan.items() if key != "digest"}
        assert plan["digest"] == canonical_digest(
            {"session": sid, "run": run["id"], "plan": material}
        )
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert decode(response)[-1]["type"] == "run.completed"
        detail = await run_detail(client, sid, run["id"])
        assert detail["result_count"] == detail["completed_steps"] == len(detail["results"]) == 5
        assert not owner.active


async def test_revisions_attempts_and_runs_survive_former_eight_boundary(agent_setup):
    async with agent_setup.opened() as (client, owner, _):
        sid, run, _ = await planned(client)
        for revision in range(2, 11):
            response = await client.post(
                f"{BASE}/{sid}/runs/{run['id']}/revise",
                json={
                    **approval(run, f"revise-{revision}"),
                    "kind": "feedback",
                    "content": "Again",
                },
            )
            assert decode(response)[-1]["type"] == "run.awaiting_approval"
            run = await run_detail(client, sid, run["id"])
            assert run["plan"]["revision"] == revision
            context = agent_setup.planner.plan.call_args.args[0]
            assert context.attempt == revision and not hasattr(context, "max_attempts")
        for index in range(10):
            response = await client.post(
                f"{BASE}/{sid}/runs/{run['id']}/reject",
                json={**approval(run, f"reject-{index}"), "kind": "plan"},
            )
            assert response.status_code == 200
            if index < 9:
                frames = decode(
                    await client.post(
                        f"{BASE}/{sid}/runs",
                        json={"request_id": f"plan-{index}", "content": "Read"},
                    )
                )
                run = await run_detail(client, sid, frames[-1]["run_id"])
        header = (await client.get(f"{BASE}/{sid}")).json()
        assert header["run_count"] == 10 and header["current_run"] is None
        assert owner.store.attempt_count(sid, run["id"]) == 1


async def test_more_than_four_active_operations_and_queued_admissions_keep_session_exclusion(
    agent_setup,
):
    async with agent_setup.opened() as (_, owner, _):
        sessions = [
            await owner.create(CreateSession(**{**CREATE, "request_id": f"create-{i}"}))
            for i in range(6)
        ]
        async with owner.lock:
            callers = [
                asyncio.create_task(
                    owner.prepare(
                        session.id,
                        StartRun(request_id="plan", content="Read"),
                    )
                )
                for session in sessions
            ]
            await wait_until(lambda: len(owner.admissions) == 6)
        ops = await asyncio.gather(*callers)
        assert len(owner.active) == 6
        with pytest.raises(AgentFault) as conflict:
            await owner.prepare(sessions[0].id, StartRun(request_id="second", content="Read"))
        assert conflict.value.code == "operation_active"
        await asyncio.gather(*(consume(owner, op) for op in ops))
        assert not owner.active


async def test_literal_legacy_approval_roundtrip_and_elapsed_operation_have_no_expiry(
    agent_setup,
    monkeypatch,
):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    deadlines = []
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]

        async def slow_clock_plan(context):
            clock.value += 120  # Simulated elapsed work, with no sleep or loop-clock mutation.
            return many_steps()

        agent_setup.planner.plan.side_effect = slow_clock_plan
        frames = decode(
            await client.post(
                f"{BASE}/{sid}/runs",
                json={"request_id": "plan", "content": "Read"},
            )
        )
        assert frames[-1]["type"] == "run.awaiting_approval"
        snapshot = await owner.get(sid)
        run = snapshot.runs[0]
        material = {
            "id": "legacy-plan",
            "revision": 1,
            "objective": "Legacy approved reads",
            "assumptions": [],
            "model": "deployment-a",
            "reasoning_effort": "high",
            "limits": {"max_steps": 3, "max_tool_calls": 3, "timeout_seconds": 1},
            "steps": [
                {
                    "id": f"step-{i}",
                    "title": "Read",
                    "description": "Exact legacy arguments",
                    "tool": "read_file",
                    "arguments": {
                        "workspace_id": "fixture",
                        "path": "note.txt",
                        "offset": 1,
                        "limit": 2,
                    },
                }
                for i in range(3)
            ],
            "access_policy": {
                **owner.registry.projection(),
                "version": "read-text-v3",
                "rules_digest": "060b1c1ed619a9dd292d85d30250c3e6387a0e9c67684652209cdb9fa298a20a",
            },
        }
        literal = {
            **material,
            "digest": canonical_digest(
                {
                    "session": sid,
                    "run": run.id,
                    "plan": material,
                }
            ),
        }
        run.plan = AgentPlan.model_validate(literal)
        assert run.plan.model_dump() == literal
        assert json.loads(run.plan.model_dump_json()) == literal
        await owner.commit(snapshot, "session.updated", "literal-legacy")
        rid = run.id
    async with agent_setup.opened() as (client, owner, _):
        detail = await run_detail(client, sid, rid)
        assert detail["plan"] == literal

        def read(args, stop, deadline):
            assert not stop.is_set()
            assert deadline == clock.value + 30
            deadlines.append(deadline)
            clock.value += 29
            return {"content": "approved evidence", "path": args.path}

        monkeypatch.setattr(owner.files, "read", read)
        response = await client.post(f"{BASE}/{sid}/runs/{rid}/execute", json=approval(detail))
        assert decode(response)[-1]["type"] == "run.completed"
        assert len(deadlines) == 3 and len(set(deadlines)) == 3
        assert clock.value == 307
        assert (await run_detail(client, sid, rid))["plan"] == literal


async def test_planner_timeout_retains_cleanup_owner_and_reports_request_timeout(agent_setup):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(context):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    agent_setup.planner.plan.side_effect = blocked
    async with agent_setup.opened(planner_timeout_seconds=0.02) as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        stream = asyncio.create_task(consume(owner, op))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(cleaning.wait(), 1)
            assert op.id in owner.active and not op.task.done()
            assert not hasattr(op, "deadline") and not hasattr(op, "timer")
        finally:
            release.set()
            frames = await stream
            await op.task
        terminal = json.loads(frames[-1].split("data: ", 1)[1])
        assert terminal["error"]["code"] == "planner_timeout"
        assert "budget" not in terminal["error"]["message"].lower()
        assert not owner.active


def transport(response, send):
    async def receive():
        await asyncio.Event().wait()

    return asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    )


@pytest.mark.parametrize("blocked_send", [False, True])
async def test_stream_wait_has_no_lifetime_but_each_blocked_send_times_out(
    agent_setup,
    monkeypatch,
    blocked_send,
):
    files = BlockingFiles({"fixture": agent_setup.workspace})
    async with agent_setup.opened(files=files, send_timeout_seconds=0.02) as (client, owner, _):
        sid, run, _ = await planned(client)
        clock = SimpleNamespace(value=100.0)
        monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=lambda: clock.value))
        # FilePolicy uses the actual monotonic clock; supply a large logical origin.
        clock.value = 10**12
        op = await owner.prepare(
            sid, PlanIdentity(**approval(run)), rid=run["id"], action="execute"
        )
        sent = []

        async def send(message):
            sent.append(message)
            if blocked_send and message["type"] == "http.response.body":
                await wait_until(files.entered.is_set)
                await asyncio.Event().wait()

        task = transport(AgentStreamingResponse(owner, op), send)
        try:
            await wait_until(files.entered.is_set)
            clock.value += 3600
            if blocked_send:
                await asyncio.wait_for(asyncio.shield(task), 1)
                assert op.stop.is_set() and op.reason == "interrupted"
                assert op.id in owner.active and not op.task.done()
            else:
                await asyncio.sleep(0.04)
                assert not task.done() and not op.stop.is_set()
        finally:
            files.release.set()
            await task
            await op.task
        assert sent[0]["status"] == 200 and not owner.active
        assert (await run_detail(client, sid, run["id"]))["status"] == (
            "interrupted" if blocked_send else "completed"
        )


async def test_public_http_pages_use_direct_views_and_preserve_exact_v2_shapes(
    agent_setup, monkeypatch
):
    agent_setup.planner.plan.return_value = many_steps()
    async with agent_setup.opened() as (client, owner, _):
        sid, run, _ = await planned(client)
        monkeypatch.setattr(
            owner.store, "events", Mock(side_effect=AssertionError("private events"))
        )
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert decode(response)[-1]["type"] == "run.completed"
        for name in ("get", "listing", "_history"):
            monkeypatch.setattr(
                owner.store, name, Mock(side_effect=AssertionError("private hydration"))
            )
        header = (await client.get(f"{BASE}/{sid}")).json()
        assert set(header) == {
            "projection_version",
            "id",
            "title",
            "model",
            "reasoning_effort",
            "created_at",
            "updated_at",
            "archived",
            "archive_pending",
            "sequence",
            "run_count",
            "latest_run_id",
            "current_run",
        }
        assert header["current_run"] is None and header["run_count"] == 1
        assert (await client.get(BASE + "?limit=1")).json()["sessions"] == [header]
        runs = (await client.get(f"{BASE}/{sid}/runs?limit=1")).json()
        assert set(runs) == {
            "projection_version",
            "session_id",
            "at_sequence",
            "total",
            "runs",
            "next_before",
        }
        assert runs["runs"][0]["result_count"] == runs["runs"][0]["completed_steps"] == 5
        assert "plan" not in runs["runs"][0] and "results" not in runs["runs"][0]
        detail = (await client.get(f"{BASE}/{sid}/runs/{run['id']}?limit=2")).json()
        assert len(detail["results"]) == 2 and detail["results_before"] == 3
        page = (await client.get(f"{BASE}/{sid}/runs/{run['id']}/results?limit=2&before=3")).json()
        assert set(page) == {
            "projection_version",
            "session_id",
            "run_id",
            "at_sequence",
            "total",
            "completed_steps",
            "results",
            "next_before",
        }
        assert len(page["results"]) == 2 and page["next_before"] == 1 and page["total"] == 5
        replay = (await client.get(f"{BASE}/{sid}/events?after=0&limit=2&through=4")).json()
        assert set(replay) == {
            "projection_version",
            "events",
            "next_after",
            "through_sequence",
            "has_more",
        }
        assert len(replay["events"]) == 2 and replay["has_more"]
        assert replay["next_after"] == 2 and replay["through_sequence"] == 4
        for url in (
            BASE + "?limit=101",
            f"{BASE}/{sid}/runs?before=-1",
            f"{BASE}/{sid}/runs/{run['id']}/results?limit=0",
        ):
            assert (await client.get(url)).status_code == 422


async def test_native_file_waiter_cancellation_retains_operation_until_worker_settles(agent_setup):
    files = BlockingFiles({"fixture": agent_setup.workspace})
    async with agent_setup.opened(files=files) as (client, owner, _):
        sid, run, _ = await planned(client)
        op = await owner.prepare(
            sid, PlanIdentity(**approval(run)), rid=run["id"], action="execute"
        )
        stream = asyncio.create_task(consume(owner, op))
        try:
            await wait_until(files.entered.is_set)
            op.task.cancel()
            await asyncio.sleep(0)
            assert op.id in owner.active and not op.task.done() and owner.io_tasks
        finally:
            files.release.set()
            await asyncio.gather(op.task, return_exceptions=True)
            await stream
        assert not owner.active and not owner.io_tasks
        assert not any(r.event.type == "tool.result" for r in owner.store.records[sid])
