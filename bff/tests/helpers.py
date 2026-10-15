from types import SimpleNamespace
from unittest.mock import AsyncMock


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
