from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.sessions import SessionStore


def settled_session_records(store, sid):
    """Read while leased; reopening must not synthesize a shutdown outcome."""
    if store.lease is not None:
        return list(store.records[sid])
    journal = store.root / sid / "events.jsonl"
    before = journal.read_bytes()
    reopened = SessionStore(store.root)
    reopened.start()
    try:
        assert journal.read_bytes() == before, "Shutdown left recovery work in the journal"
        return list(reopened.records[sid])
    finally:
        reopened.close()


def delta(text="Hello"):
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def terminal(status="completed", *, error=None, reason=None, output=None):
    return SimpleNamespace(
        type=f"response.{status}",
        response=SimpleNamespace(
            status=status,
            error=error,
            incomplete_details=SimpleNamespace(reason=reason),
            output=output or [],
        ),
    )


def title_response(text="A concise title", *, status="completed", output=None):
    return SimpleNamespace(status=status, output_text=text, output=output or [])


class FakeStream:
    def __init__(self, *events):
        self.events = iter(events)
        self.reads = 0
        self.close = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.reads += 1
        try:
            event = next(self.events)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(event, BaseException):
            raise event
        return event
