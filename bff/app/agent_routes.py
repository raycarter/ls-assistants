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
            send_timeout_seconds=owner.send_timeout_seconds,
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.owner.disconnect(self.op)


@router.get("")
async def listing(
    request: Request,
    include_archived: bool = False,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
):
    return await service(request).public_list(include_archived, limit, cursor)


@router.post("")
async def create(body: CreateSession, request: Request):
    owner = service(request)
    snapshot = await owner.create(body)
    return await owner.public_session(snapshot.id)


@router.get("/{sid}")
async def get(sid: str, request: Request):
    return await service(request).public_session(sid)


@router.patch("/{sid}")
async def patch(sid: str, body: PatchSession, request: Request):
    owner = service(request)
    snapshot = await owner.patch(sid, body)
    return await owner.public_session(snapshot.id)


@router.get("/{sid}/events")
async def events(
    sid: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    through: int | None = Query(default=None, ge=0),
):
    return await service(request).public_events(sid, after, limit, through)


@router.get("/{sid}/runs")
async def runs(
    sid: str,
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    before: int | None = Query(default=None, ge=0),
):
    return await service(request).public_runs(sid, limit, before)


@router.get("/{sid}/runs/{rid}")
async def run(
    sid: str,
    rid: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    before: int | None = Query(default=None, ge=0),
):
    return await service(request).public_run(sid, rid, limit, before)


@router.get("/{sid}/runs/{rid}/results")
async def results(
    sid: str,
    rid: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    before: int | None = Query(default=None, ge=0),
):
    return await service(request).public_results(sid, rid, limit, before)


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
    owner = service(request)
    snapshot = await owner.reject(sid, rid, body)
    return await owner.public_session(snapshot.id)


@router.post("/{sid}/runs/{rid}/cancel")
async def cancel(sid: str, rid: str, body: Cancel, request: Request, response: Response):
    owner = service(request)
    snapshot, pending = await owner.cancel(sid, rid, body)
    response.status_code = 202 if pending else 200
    return await owner.public_session(snapshot.id)


@router.post("/{sid}/archive")
async def archive(sid: str, body: Command, request: Request, response: Response):
    owner = service(request)
    snapshot, pending = await owner.archive(sid, body)
    response.status_code = 202 if pending else 200
    return await owner.public_session(snapshot.id)
