"""Offline search approval, evidence validation and cancellation integration."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from app.agent import SEARCH_PROVENANCE_NOTE
from app.agent_responses import _check_shape, proposal_schema
from app.agent_schemas import Cancel, PlanIdentity, Proposal, SearchArguments, StepResult
from app.main import create_app
from tests.test_agent import BASE, CREATE, approval, consume, decode, run_detail
from tests.test_agent_fetch import fetch_setup as fetch_setup

HIT = {
    "title": "Example",
    "url": "https://example.org/",
    "snippet": "Evidence",
    "published_date": None,
}


async def test_main_injects_isolated_search_client_without_startup_request(
    monkeypatch, settings_factory, mock_client
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "query": "exact query",
                "results": [{"title": HIT["title"], "url": HIT["url"], "content": HIT["snippet"]}],
            },
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    llm_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr("app.main.create_http_client", lambda **kwargs: search_client)
    settings = settings_factory(tavily_api_key="synthetic-key")
    assert "synthetic-key" not in repr(settings)
    app = create_app(
        settings,
        credential_factory=Mock(return_value=SimpleNamespace(close=AsyncMock())),
        client_factory=Mock(return_value=mock_client),
        agent_http_client_factory=lambda **kwargs: llm_client,
    )
    async with app.router.lifespan_context(app):
        assert requests == []
        result = await app.state.agent_service.searcher.search("exact query", 2)
        assert result == [HIT]
        assert len(requests) == 1
        body = json.loads(requests[0].content)
        assert body["query"] == "exact query" and body["max_results"] == 2
        assert body["search_depth"] == "basic"
        assert body["include_raw_content"] is False
    assert search_client.is_closed and llm_client.is_closed


def search_proposal():
    return {
        "objective": "Search",
        "assumptions": [],
        "steps": [
            {
                "title": "Search",
                "description": "Find sources",
                "tool": "web_search",
                "arguments": {"query": "exact query", "max_results": 2},
            }
        ],
    }


async def plan(client):
    sid = (await client.post(BASE, json=CREATE)).json()["id"]
    frames = decode(
        await client.post(f"{BASE}/{sid}/runs", json={"request_id": "plan", "content": "Search"})
    )
    return sid, await run_detail(client, sid, frames[-1]["run_id"])


def test_search_schema_and_result_exclusivity():
    _check_shape(search_proposal(), proposal_schema())
    Proposal.model_validate(search_proposal())
    for bad in (
        {"query": " ", "max_results": 1},
        {"query": "x", "max_results": True},
        {"query": "x", "max_results": 21},
        {"query": "x", "max_results": 0},
    ):
        with pytest.raises(ValidationError):
            SearchArguments.model_validate(bad)
    base = {
        "step_id": "a",
        "status": "completed",
        "time": "now",
        "content": "",
        "search": {"query": "x", "max_results": 1, "results": [HIT]},
    }
    assert StepResult.model_validate(base).search.results[0].url == HIT["url"]
    for extra in ({"content": "text"}, {"path": "a"}, {"truncated": True}, {"status": "running"}):
        with pytest.raises(ValidationError):
            StepResult.model_validate(base | extra)


async def test_approval_records_exact_search_and_never_fetches(fetch_setup):
    fetch_setup.planner.plan.return_value = Proposal.model_validate(search_proposal())
    searcher = SimpleNamespace(search=AsyncMock(return_value=[HIT]))
    async with fetch_setup.opened() as (client, owner, _):
        owner.searcher = searcher
        sid, run = await plan(client)
        assert run["status"] == "awaiting_approval"
        searcher.search.assert_not_awaited()
        path = f"{BASE}/{sid}/runs/{run['id']}/execute"
        assert (
            await client.post(path, json={**approval(run), "digest": "0" * 64})
        ).status_code == 409
        searcher.search.assert_not_awaited()
        response = await client.post(path, json=approval(run))
        assert decode(response)[-1]["type"] == "run.completed"
        completed = await run_detail(client, sid, run["id"])
        assert completed["answer"].startswith(SEARCH_PROVENANCE_NOTE + "\n\n")
        result = completed["results"][0]
        assert result["content"] == "" and result["search"] == {
            "query": "exact query",
            "max_results": 2,
            "results": [HIT],
        }
        searcher.search.assert_awaited_once_with("exact query", 2)
        fetch_setup.fetcher.fetch.assert_not_awaited()
        assert (await client.post(path, json=approval(run, "again"))).status_code == 409


async def test_unconfigured_preflight_and_revoked_policy(fetch_setup):
    fetch_setup.planner.plan.return_value = Proposal.model_validate(search_proposal())
    async with fetch_setup.opened() as (client, owner, _):
        sid, run = await plan(client)
        assert run["status"] == "failed" and run["error"]["code"] == "unsupported_capability"
        assert run["plan"] is None

        owner.searcher = SimpleNamespace(search=AsyncMock(return_value=[HIT]))
        sid = (await client.post(BASE, json={**CREATE, "request_id": "create-2"})).json()["id"]
        frames = decode(
            await client.post(
                f"{BASE}/{sid}/runs", json={"request_id": "plan", "content": "Search"}
            )
        )
        run = await run_detail(client, sid, frames[-1]["run_id"])
        searcher = owner.searcher
        owner.searcher = None
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert response.status_code == 409 and response.json()["error"]["code"] == "policy_changed"
        searcher.search.assert_not_awaited()


@pytest.mark.parametrize(
    "response", [[HIT, HIT, HIT], [{**HIT, "snippet": 7}], RuntimeError("secret-canary")]
)
async def test_invalid_or_failed_search_does_not_publish_or_leak(fetch_setup, response):
    fetch_setup.planner.plan.return_value = Proposal.model_validate(search_proposal())
    search = (
        AsyncMock(side_effect=response)
        if isinstance(response, Exception)
        else AsyncMock(return_value=response)
    )
    async with fetch_setup.opened() as (client, owner, _):
        owner.searcher = SimpleNamespace(search=search)
        sid, run = await plan(client)
        result = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert decode(result)[-1]["error"]["code"] == "web_search_failed"
        assert "secret-canary" not in result.text
        assert not any(r.event.type == "tool.result" for r in owner.store.records[sid])


async def test_cancel_drains_search_without_publishing(fetch_setup):
    fetch_setup.planner.plan.return_value = Proposal.model_validate(search_proposal())
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def search(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    async with fetch_setup.opened() as (client, owner, _):
        owner.searcher = SimpleNamespace(search=AsyncMock(side_effect=search))
        sid, run = await plan(client)
        op = await owner.prepare(
            sid, PlanIdentity(**approval(run)), rid=run["id"], action="execute"
        )
        task = asyncio.create_task(consume(owner, op))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await owner.cancel(sid, run["id"], Cancel(request_id="stop", operation_id=op.id))
            await asyncio.wait_for(cleaning.wait(), 3)
            assert op.id in owner.active
        finally:
            release.set()
            await task
        assert not any(r.event.type == "tool.result" for r in owner.store.records[sid])
