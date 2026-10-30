import asyncio
import json
from urllib.parse import quote

import httpx
import pytest

from app.tools.web_search import MAX_RESPONSE_BYTES, TavilySearch, WebSearchError


async def test_request_and_results():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "query": "hello",
                "results": [
                    {"title": "Title", "url": "https://example.com", "content": "Text"},
                    {
                        "title": "Other",
                        "url": "https://example.org",
                        "content": "More",
                        "published_date": "2024-01-01",
                        "extra": 1,
                    },
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        auth=("client-user", "client-password"),
    ) as client:
        result = await TavilySearch("secret", client).search("hello", 2)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.tavily.com/search"
    assert request.headers["authorization"] == "Bearer secret"
    assert request.headers["accept-encoding"] == "identity"
    assert json.loads(request.content) == {
        "query": "hello",
        "search_depth": "basic",
        "auto_parameters": False,
        "max_results": 2,
        "include_raw_content": False,
        "include_answer": False,
        "include_published_date": True,
    }
    assert result == [
        {"title": "Title", "url": "https://example.com", "snippet": "Text", "published_date": None},
        {
            "title": "Other",
            "url": "https://example.org",
            "snippet": "More",
            "published_date": "2024-01-01",
        },
    ]


@pytest.mark.parametrize("value", [0, 21, True, 1.0, "1", None])
async def test_invalid_max_results(value):
    transport = httpx.MockTransport(lambda request: pytest.fail())
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="max_results"):
            await TavilySearch("key", client).search("query", value)


@pytest.mark.parametrize("query", ["", "  ", None, 123])
async def test_invalid_query(query):
    transport = httpx.MockTransport(lambda request: pytest.fail())
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="query"):
            await TavilySearch("key", client).search(query, 1)


@pytest.mark.parametrize("value", [1, 20])
async def test_allowed_boundaries(value):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"query": "q", "results": []})
        )
    ) as client:
        assert await TavilySearch("key", client).search("q", value) == []


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"query": "different", "results": []},
        {"query": "q", "results": {}},
        {"query": "q", "results": [None]},
        {"query": "q", "results": [{"title": "t", "url": "u"}]},
        {"query": "q", "results": [{"title": 1, "url": "u", "content": "c"}]},
        {"query": "q", "results": [{"title": "t", "url": None, "content": "c"}]},
        {
            "query": "q",
            "results": [{"title": "t", "url": "u", "content": "c", "published_date": 1}],
        },
        {"query": "q", "results": [{"title": "t", "url": "u", "content": "c"}] * 2},
    ],
)
async def test_malformed_response(data):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    ) as client:
        with pytest.raises(WebSearchError, match="^Web search failed$") as exc:
            await TavilySearch("key", client).search("q", 1)
        assert exc.value.__context__ is None


async def test_redirect_is_not_followed_and_error_is_sanitized():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            307, headers={"Location": "https://evil.example/secret"}, text="provider secret"
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(WebSearchError, match="^Web search failed$") as exc:
            await TavilySearch("secret", client).search("q", 1)
    assert len(seen) == 1
    assert exc.value.__context__ is None


@pytest.mark.parametrize("kind", ["http", "json", "transport"])
async def test_failures_are_sanitized(kind):
    def handler(request):
        if kind == "transport":
            raise httpx.ConnectError("secret provider output", request=request)
        if kind == "json":
            return httpx.Response(200, text="secret provider output")
        return httpx.Response(400, text="secret provider output")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WebSearchError, match="^Web search failed$") as exc:
            await TavilySearch("secret", client).search("q", 1)
    assert exc.value.__context__ is None
    assert exc.value.__cause__ is None


async def test_cancellation_propagates_and_transport_is_cleaned_up():
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def handler(request):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(TavilySearch("key", client).search("q", 1))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()


@pytest.mark.parametrize("field", ["title", "url", "content", "published_date"])
async def test_reflected_credentials_are_rejected_in_every_result_field(field):
    item = {"title": "t", "url": "u", "content": "c", "published_date": "date"}
    item[field] = "prefix-super-secret-suffix"
    data = {
        "query": "q",
        "results": [
            {"title": "safe", "url": "u", "content": "c"},
            item,
        ],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    persisted = []
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(WebSearchError, match="^Web search failed$") as exc:
            persisted.extend(await TavilySearch("super-secret", client).search("q", 2))
    assert persisted == []
    assert exc.value.__context__ is None


async def test_query_may_contain_the_key_when_results_do_not():
    data = {
        "query": "search for secret",
        "results": [
            {"title": "safe", "url": "u", "content": "c"},
        ],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    async with httpx.AsyncClient(transport=transport) as client:
        assert len(await TavilySearch("secret", client).search("search for secret", 1)) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path/%73ecret",
        "https://example.com/?key=%73ecret",
        "https://example.com/#%73ecret",
        "https://example.com/%2573ecret",
        "https://example.com/%252573ecret",
        "https://example.com/?key=secret+value",
    ],
)
async def test_encoded_credentials_in_result_url_are_rejected(url):
    key = "secret value" if "+" in url else "secret"
    data = {"query": "q", "results": [{"title": "t", "url": url, "content": "c"}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(WebSearchError, match="^Web search failed$") as exc:
            await TavilySearch(key, client).search("q", 1)
    assert exc.value.__context__ is None


async def test_url_with_unrelated_encoded_data_is_accepted():
    data = {
        "query": "q",
        "results": [
            {
                "title": "t",
                "url": "https://example.com/" + quote("a path"),
                "content": "c",
            }
        ],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    async with httpx.AsyncClient(transport=transport) as client:
        assert len(await TavilySearch("secret", client).search("q", 1)) == 1


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.read_count = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


async def test_oversized_stream_fails_without_returning_partial_results_and_closes():
    stream = ChunkStream([b"x" * MAX_RESPONSE_BYTES, b"x", b"never read"])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    persisted = []
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(WebSearchError, match="^Web search response exceeds size limit$") as exc:
            persisted.extend(await TavilySearch("secret", client).search("q", 1))
    assert persisted == []
    assert stream.closed
    assert stream.read_count == 2
    assert exc.value.__context__ is None


@pytest.mark.parametrize(
    "headers",
    [
        {"content-encoding": "gzip"},
        {"content-length": "invalid"},
        {"content-length": "1"},
        {"content-length": "999999999"},
    ],
)
async def test_encoding_and_content_length_are_enforced(headers):
    stream = ChunkStream([b"{}"])
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers=headers, stream=stream)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(WebSearchError):
            await TavilySearch("key", client).search("q", 1)
    assert stream.closed
    if headers.get("content-encoding") or headers.get("content-length") != "1":
        assert stream.read_count == 0


async def test_cancellation_closes_response_stream():
    started = asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            self.closed = True

    stream = BlockingStream()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    async with httpx.AsyncClient(transport=transport) as client:
        task = asyncio.create_task(TavilySearch("key", client).search("q", 1))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
