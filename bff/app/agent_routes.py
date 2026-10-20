import asyncio
import time

import anyio
from fastapi import APIRouter, Query, Request, Response

from app.agent import AgentService, Operation
from app.agent_files import AgentFault
from app.agent_schemas import (
    ArtifactPage,
    Cancel,
    ClarificationReply,
    Command,
    CreateSession,
    PatchSession,
    PlanIdentity,
    Reject,
    Revise,
    StartRun,
)
from app.routes import ClosingStreamingResponse

router = APIRouter(prefix="/api/agent/sessions")


def service(request: Request) -> AgentService:
    return request.app.state.agent_service


class AgentStreamingResponse(ClosingStreamingResponse):
    def __init__(self, owner: AgentService, op: Operation):
        self.owner, self.op = owner, op
        super().__init__(
            owner.frames(op),
            deadline=asyncio.get_running_loop().time() + max(0, op.deadline - time.monotonic()),
        )

    async def __call__(self, scope, receive, send):
        try:
            # Bound the whole transport, including waits for the next journal
            # frame. The operation's independent timer still owns worker Stop.
            with anyio.move_on_after(max(0, self.op.deadline - time.monotonic())):
                await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.owner.disconnect(self.op)


@router.get("")
async def listing(request: Request, include_archived: bool = False):
    return {"sessions": await service(request).listing(include_archived)}


@router.post("")
async def create(body: CreateSession, request: Request):
    return await service(request).create(body)


@router.get("/{sid}")
async def get(sid: str, request: Request):
    return await service(request).get(sid)


@router.patch("/{sid}")
async def patch(sid: str, body: PatchSession, request: Request):
    return await service(request).patch(sid, body)


@router.get("/{sid}/events")
async def events(sid: str, request: Request, after: int = Query(default=0, ge=0)):
    return await service(request).history(sid, after)


@router.get("/{sid}/artifacts/{artifact_id}", response_model=ArtifactPage)
async def artifact(
    sid: str,
    artifact_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=12000, ge=1, le=16000),
):
    return await service(request).artifact(sid, artifact_id, offset, limit)


@router.post("/{sid}/runs")
async def start(sid: str, body: StartRun, request: Request):
    owner = service(request)
    return AgentStreamingResponse(owner, await owner.prepare(sid, body))


@router.post("/{sid}/runs/{rid}/execute")
async def execute(sid: str, rid: str, body: PlanIdentity, request: Request):
    owner = service(request)
    return AgentStreamingResponse(owner, await owner.prepare(sid, body, rid=rid, action="execute"))


@router.post("/{sid}/runs/{rid}/revise")
async def revise(sid: str, rid: str, body: Revise, request: Request):
    if isinstance(body, ClarificationReply):
        raise AgentFault(
            "clarification_unavailable", "Clarification is unavailable in this slice.", 501
        )
    owner = service(request)
    return AgentStreamingResponse(owner, await owner.prepare(sid, body, rid=rid, action="revise"))


@router.post("/{sid}/runs/{rid}/reject")
async def reject(sid: str, rid: str, body: Reject, request: Request):
    return await service(request).reject(sid, rid, body)


@router.post("/{sid}/runs/{rid}/cancel")
async def cancel(sid: str, rid: str, body: Cancel, request: Request, response: Response):
    snapshot, pending = await service(request).cancel(sid, rid, body)
    response.status_code = 202 if pending else 200
    return snapshot


@router.post("/{sid}/archive")
async def archive(sid: str, body: Command, request: Request, response: Response):
    snapshot, pending = await service(request).archive(sid, body)
    response.status_code = 202 if pending else 200
    return snapshot
