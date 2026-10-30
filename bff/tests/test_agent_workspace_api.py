"""Offline default composition over real, exclusively synthetic workspace files."""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from azure.core.credentials import AccessToken

import app.agent as agent
import app.main as main
from app.agent import AgentService
from app.agent_files import FilePolicy
from app.sessions import SessionStore
from tests.test_agent import BASE, approval, decode, run_detail
from tests.test_agent_responses_api import CREATE, TOKEN, audits, provider_body

ARGUMENTS = {"workspace_id": "workspace", "path": "note.txt", "offset": 2, "limit": 1}
COMMAND = {"request_id": "plan", "content": "Read line 2 of note.txt in workspace."}
ANSWER = "note.txt:2 contains synthetic evidence 🌱."


def proposal(arguments):
    body = provider_body()
    body["output"][1]["content"][0]["text"] = json.dumps(
        {
            "objective": "Read the requested relative file page",
            "assumptions": [],
            "steps": [
                {
                    "title": "Read",
                    "description": "Read exactly the requested page",
                    "tool": "read_file",
                    "arguments": arguments,
                }
            ],
        }
    )
    return body


@pytest.fixture
def workspace_api(tmp_path, monkeypatch, settings_factory, mock_client):
    # Resolve only pytest's synthetic parent: descriptor traversal rejects symlinks.
    parent = tmp_path.resolve()
    root = parent / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("first\nsynthetic evidence 🌱\nlast\n", encoding="utf-8")
    journal = parent / "journal"
    policy = FilePolicy({"workspace": root})
    policy.read = Mock(wraps=policy.read)
    fixture = SimpleNamespace(
        root=root,
        journal=journal,
        files=policy,
        calls=[],
        arguments=dict(ARGUMENTS),
        settings=settings_factory(),
    )
    fixture.build = Mock(side_effect=lambda: fixture.files)
    monkeypatch.setattr(main, "build_file_policy", fixture.build)
    # An omitted explicit store must fail before touching the production default.
    monkeypatch.setattr(agent, "SessionStore", Mock(side_effect=AssertionError("Default store")))

    def service(**kwargs):
        assert kwargs["files"] is fixture.files
        fixture.build.assert_called_once_with()
        kwargs["read_assessor"] = None
        return AgentService(store=SessionStore(journal), **kwargs)

    fixture.service = Mock(side_effect=service)
    monkeypatch.setattr(main, "AgentService", fixture.service)
    credential = SimpleNamespace(
        get_token=AsyncMock(return_value=AccessToken(TOKEN, 4102444800)), close=AsyncMock()
    )

    def dispatch(request):
        fixture.calls.append(request)
        if len(fixture.calls) == 1:
            return httpx.Response(200, json=proposal(fixture.arguments))
        body = provider_body()
        body["output"][1]["content"] = [{"type": "output_text", "text": ANSWER}]
        return httpx.Response(200, json=body)

    fixture.http = Mock(
        side_effect=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch), trust_env=False, **kwargs
        )
    )

    @asynccontextmanager
    async def opened(**overrides):
        fixture.build.reset_mock()
        app = main.create_app(
            fixture.settings,
            credential_factory=Mock(return_value=credential),
            client_factory=Mock(return_value=mock_client),
            agent_http_client_factory=fixture.http,
            **overrides,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
            ) as client:
                yield client, app.state.agent_service

    fixture.opened = opened
    fixture.credential = credential
    return fixture


async def plan(client):
    created = await client.post(BASE, json=CREATE)
    assert created.status_code == 200
    sid = created.json()["id"]
    frames = decode(await client.post(f"{BASE}/{sid}/runs", json=COMMAND))
    return sid, await run_detail(client, sid, frames[-1]["run_id"])


def execute_path(sid, run):
    return f"{BASE}/{sid}/runs/{run['id']}/execute"


async def test_startup_health_models_do_not_probe_workspace_or_open_storage(
    workspace_api,
    monkeypatch,
):
    fixture = workspace_api
    forbidden = Mock(side_effect=AssertionError("Startup filesystem access"))
    real_stat, real_lstat = os.stat, os.lstat

    def guarded_stat(original):
        def check(path, *args, **kwargs):
            # FastAPI may stat route source files for introspection. Guard the
            # workspace/storage boundaries without blocking that framework IO.
            spelling = str(path)
            if any(
                spelling.startswith(str(root))
                for root in (
                    fixture.root,
                    fixture.journal,
                    Path("/tmp/ls-assistants"),
                    Path("/private/tmp/ls-assistants"),
                )
            ):
                return forbidden(path, *args, **kwargs)
            return original(path, *args, **kwargs)

        return check

    with monkeypatch.context() as guard:
        guard.setattr(SessionStore, "start", forbidden)
        for name in ("open", "read_text", "read_bytes", "mkdir", "resolve"):
            guard.setattr(Path, name, forbidden)
        guard.setattr(os, "stat", guarded_stat(real_stat))
        guard.setattr(os, "lstat", guarded_stat(real_lstat))
        for name in ("open", "fstat", "mkdir"):
            guard.setattr(os, name, forbidden)
        async with fixture.opened() as (client, owner):
            assert (await client.get("/health")).json() == {"status": "ok"}
            assert (await client.get("/api/models")).json() == {
                "models": ["deployment-a", "deployment-b"],
                "default_model": "deployment-a",
            }
            assert owner.files is fixture.files and owner.eager is False
            fixture.build.assert_called_once_with()
            fixture.files.read.assert_not_called()
            forbidden.assert_not_called()
            fixture.credential.get_token.assert_not_awaited()
            assert fixture.calls == []
    assert not fixture.journal.exists()


async def test_relative_page_approval_real_descriptor_read_synthesis_and_restart(
    workspace_api,
    monkeypatch,
):
    fixture = workspace_api
    real_open = os.open
    opens = []

    def observed_open(path, flags, *args, **kwargs):
        if path == "note.txt":
            assert kwargs.get("dir_fd") is not None
            assert flags & os.O_NOFOLLOW
            opens.append(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", observed_open)
    async with fixture.opened() as (client, owner):
        sid, run = await plan(client)
        assert run["status"] == "awaiting_approval"
        assert run["plan"]["steps"][0]["arguments"] == ARGUMENTS
        wire = json.loads(fixture.calls[0].content)
        context = json.loads(wire["input"][0]["content"])
        assert context["workspace_ids"] == ["workspace"]
        assert context["request"] == COMMAND["content"]
        assert str(fixture.root) not in json.dumps(wire)
        assert "/tmp/ls-assistants" not in json.dumps(wire)
        fixture.files.read.assert_not_called()
        assert opens == [] and len(fixture.calls) == 1
        frames = decode(await client.post(execute_path(sid, run), json=approval(run)))
        completed = await run_detail(client, sid, run["id"])
        assert frames[-1]["type"] == "run.completed"
        assert completed["status"] == "completed" and completed["answer"] == ANSWER
        fixture.files.read.assert_called_once()
        assert fixture.files.read.call_args.args[0].model_dump() == ARGUMENTS
        assert opens == ["note.txt"] and len(fixture.calls) == 2
        evidence = json.loads(json.loads(fixture.calls[1].content)["input"][0]["content"])
        assert evidence == {
            "request": COMMAND["content"],
            "approved_plan": run["plan"],
            "results": completed["results"],
        }
        result = evidence["results"][0]
        assert result["step_id"] == run["plan"]["steps"][0]["id"]
        page_keys = ("content", "path", "offset", "returned_lines", "truncated")
        assert {key: result[key] for key in page_keys} == {
            "content": "2: synthetic evidence 🌱",
            "path": "note.txt",
            "offset": 2,
            "returned_lines": 1,
            "truncated": True,
        }
        groups = audits(owner, sid)
        assert [group[0]["identity"]["phase"] for group in groups] == ["planning", "synthesis"]
    async with fixture.opened() as (client, owner):
        assert await run_detail(client, sid, run["id"]) == completed
        assert audits(owner, sid) == groups
        duplicate = await client.post(execute_path(sid, run), json=approval(run, "restart"))
        assert duplicate.status_code == 409
    assert len(fixture.calls) == 2 and opens == ["note.txt"]


async def test_missing_root_fails_generically_without_creation_fallback_or_synthesis(
    workspace_api,
    monkeypatch,
):
    fixture = workspace_api
    (fixture.root / "note.txt").unlink()
    fixture.root.rmdir()
    (fixture.root.parent / "note.txt").write_text("cwd decoy", encoding="utf-8")
    monkeypatch.chdir(fixture.root.parent)
    async with fixture.opened() as (client, owner):
        sid, run = await plan(client)
        assert run["status"] == "awaiting_approval"
        fixture.files.read.assert_not_called()
        response = await client.post(execute_path(sid, run), json=approval(run))
        assert decode(response)[-1]["type"] == "run.failed"
        failed = await run_detail(client, sid, run["id"])
        assert failed["error"] == {
            "code": "file_unavailable",
            "message": "The file is unavailable or is not UTF-8 text.",
        }
        assert failed["answer"] is None
        assert str(fixture.root) not in response.text and "cwd decoy" not in response.text
        assert len(fixture.calls) == 1 and len(audits(owner, sid)) == 1
        fixture.files.read.assert_called_once()
    assert not fixture.root.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        {**ARGUMENTS, "path": "../note.txt"},
        {**ARGUMENTS, "path": "/note.txt"},
        {**ARGUMENTS, "path": "a\\note.txt"},
        {**ARGUMENTS, "path": ".env"},
        {**ARGUMENTS, "path": "sessions/note.txt"},
        {**ARGUMENTS, "path": "private/note.txt"},
        {**ARGUMENTS, "path": "dev-plans-archive/note.txt"},
        {**ARGUMENTS, "workspace_id": "unknown"},
    ],
)
async def test_invalid_scope_proposal_never_reads_or_synthesizes(workspace_api, arguments):
    fixture = workspace_api
    fixture.arguments = arguments
    async with fixture.opened() as (client, owner):
        sid, run = await plan(client)
        assert run["status"] == "failed" and run["plan"] is None
        assert run["error"]["code"] == "invalid_plan"
        fixture.files.read.assert_not_called()
        assert len(fixture.calls) == 1 and len(audits(owner, sid)) == 1


@pytest.mark.parametrize("offset,limit,valid", [(0, 12000, False), (1, 200, True)])
async def test_reported_read_paging_is_audited_without_repair_retry_or_execution(
    workspace_api, offset, limit, valid
):
    fixture = workspace_api
    arguments = {"workspace_id": "workspace", "path": "note.txt", "offset": offset, "limit": limit}
    fixture.arguments = dict(arguments)
    expected_body = proposal(arguments)
    async with fixture.opened() as (client, owner):
        sid, run = await plan(client)
        assert fixture.arguments == arguments
        fixture.files.read.assert_not_called()
        assert len(fixture.calls) == 1
        (records,) = audits(owner, sid)
        assert records[0]["data"]["instructions_version"] == "agent-responses-planning-v5"
        wire = records[0]["data"]["request"]
        assert wire == json.loads(fixture.calls[0].content)
        assert wire["text"]["format"]["name"] == "agent_plan_v5"
        assert json.loads(wire["input"][0]["content"])["workspace_ids"] == ["workspace"]
        assert not {"tools", "tool_choice"} & wire.keys()
        output = next(r["data"] for r in records if r["kind"] == "output")
        assert output["body"] == expected_body
        retained = json.loads(output["body"]["output"][1]["content"][0]["text"])
        assert retained["steps"][0]["arguments"] == arguments
        assert records[-2]["data"]["status"] == "completed"
        assert records[-1]["data"]["status"] == ("valid" if valid else "invalid")
        if valid:
            assert run["status"] == "awaiting_approval"
            assert run["plan"]["steps"][0]["arguments"] == arguments
        else:
            assert run["status"] == "failed" and run["plan"] is None
            assert run["error"]["code"] == "invalid_plan"
    async with fixture.opened() as (client, owner):
        assert await run_detail(client, sid, run["id"]) == run
        assert audits(owner, sid) == [records]
        fixture.files.read.assert_not_called()
        assert len(fixture.calls) == 1


@pytest.mark.parametrize("change", ["id", "root"])
async def test_old_approval_rejected_after_workspace_mapping_changes(workspace_api, change):
    fixture = workspace_api
    original = fixture.files
    async with fixture.opened() as (client, _):
        sid, run = await plan(client)
        assert run["status"] == "awaiting_approval"
    roots = (
        {"renamed": fixture.root}
        if change == "id"
        else {"workspace": fixture.root.parent / "other"}
    )
    fixture.files = FilePolicy(roots)
    fixture.files.read = Mock(wraps=fixture.files.read)
    async with fixture.opened() as (client, _):
        response = await client.post(execute_path(sid, run), json=approval(run))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "policy_changed"
        original.read.assert_not_called()
        fixture.files.read.assert_not_called()
        assert len(fixture.calls) == 1


async def test_custom_service_factory_bypasses_policy_even_with_missing_workspace(workspace_api):
    fixture = workspace_api
    (fixture.root / "note.txt").unlink()
    fixture.root.rmdir()
    custom = AgentService(store=SessionStore(fixture.journal), files=FilePolicy(), eager=False)
    factory = Mock(return_value=custom)
    fixture.build.side_effect = AssertionError("Custom factory must bypass workspace construction")
    fixture.http.side_effect = AssertionError("Custom factory must bypass HTTP construction")
    async with fixture.opened(agent_service_factory=factory) as (client, owner):
        assert owner is custom
        factory.assert_called_once_with(fixture.settings)
        fixture.build.assert_not_called()
        fixture.service.assert_not_called()
        fixture.http.assert_not_called()
        assert (await client.get("/health")).status_code == 200
    assert not fixture.root.exists() and not fixture.journal.exists()
