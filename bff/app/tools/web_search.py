"""A small, isolated adapter for Tavily's basic search API."""

import json
from urllib.parse import unquote_plus

import httpx

# Basic search returns snippets, not full pages. One MiB allows ample room for
# 20 results while bounding memory per request; this is not a run-wide quota.
MAX_RESPONSE_BYTES = 1024 * 1024


class WebSearchError(Exception):
    """The search provider failed or returned an invalid response."""


class TavilySearch:
    def __init__(self, api_key: str, client: httpx.AsyncClient):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        self._api_key = api_key
        self._client = client

    async def search(self, query: str, max_results: int) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if type(max_results) is not int or not 1 <= max_results <= 20:
            raise ValueError("max_results must be an integer between 1 and 20")

        # Raise outside the handler so the original exception, which may contain
        # credentials or provider data, is not attached as exception context.
        error = None
        try:
            async with self._client.stream(
                "POST",
                "https://api.tavily.com/search",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept-Encoding": "identity",
                },
                json={
                    "query": query,
                    "search_depth": "basic",
                    "auto_parameters": False,
                    "max_results": max_results,
                    "include_raw_content": False,
                    "include_answer": False,
                    "include_published_date": True,
                },
                auth=None,
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise ValueError
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding != "identity":
                    raise ValueError
                length = response.headers.get("content-length")
                if length is not None:
                    if not length.isascii() or not length.isdecimal():
                        raise ValueError
                    declared_length = int(length)
                    if declared_length > MAX_RESPONSE_BYTES:
                        raise _ResponseTooLarge
                else:
                    declared_length = None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunk) > MAX_RESPONSE_BYTES - len(body):
                        raise _ResponseTooLarge
                    body.extend(chunk)
                if declared_length is not None and len(body) != declared_length:
                    raise ValueError
                data = json.loads(body.decode("utf-8"))
                results = self._parse(data, query, max_results, self._api_key)
        except _ResponseTooLarge:
            error = "Web search response exceeds size limit"
        except Exception:
            error = "Web search failed"
        if error:
            raise WebSearchError(error)
        return results

    @staticmethod
    def _parse(data: object, query: str, max_results: int, api_key: str) -> list[dict]:
        if not isinstance(data, dict) or data.get("query") != query:
            raise ValueError
        results = data.get("results")
        if not isinstance(results, list) or len(results) > max_results:
            raise ValueError
        parsed = []
        for item in results:
            if not isinstance(item, dict):
                raise ValueError
            title, url, content = item.get("title"), item.get("url"), item.get("content")
            published_date = item.get("published_date")
            if (
                not isinstance(title, str)
                or not isinstance(url, str)
                or not isinstance(content, str)
                or (published_date is not None and not isinstance(published_date, str))
            ):
                raise ValueError
            if any(api_key in value for value in (title, url, content, published_date or "")):
                raise ValueError
            # Decode at most three layers: this catches common nested URL
            # encodings while keeping work bounded by the response size limit.
            decoded_url = url
            for _ in range(3):
                decoded_url = unquote_plus(decoded_url)
                if api_key in decoded_url:
                    raise ValueError
            parsed.append(
                {"title": title, "url": url, "snippet": content, "published_date": published_date}
            )
        return parsed


class _ResponseTooLarge(Exception):
    pass
