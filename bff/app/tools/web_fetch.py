"""Unauthenticated, static public-web retrieval. Page text is untrusted data.

No application runtime, artifact storage, browser, or registry dependencies.
Identity-encoded bodies are decoded and parsed incrementally into bounded-memory
spools, without a document-size quota or silent truncation. Successful documents
own their spool: consume iter_text() in a storage worker, then close() in finally.
"""

import asyncio
import codecs
import math
import re
import socket
import ssl
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import urljoin

import certifi  # Required trust-root dependency of HTTPcore.
import httpcore
import httpx

from app.agent_files import AgentFault
from app.tools.network_policy import (
    POLICY_VERSION,
    PublicNetworkBackend,
    Resolver,
    check_url_characters,
    resolve_public_host,
    settled_close,
    validate_url,
)

_CHUNK_BYTES = 8192
_SPOOL_MEMORY_BYTES = 65_536
_MAX_MARKUP_CHARS = 16_384
_MAX_DEPTH = 512
_MAX_TITLE_CHARS = 4096
_BINARY_SIGNATURES = (b"%PDF-", b"GIF87a", b"GIF89a", b"\x89PNG", b"\xff\xd8\xff", b"PK\x03\x04")
_WHITESPACE = re.compile(r"\s+|\S+")
_LINE_BREAK = re.compile(r"[\r\n\f\x85\u2028\u2029]")


async def _blocking[T](operation: Callable[..., T], *args) -> T:
    """Settle each bounded parser/disk operation before propagating cancellation.

    These managed workers do no network work. In particular, a cancelled write
    must finish before its spool is closed; no worker is abandoned on timeout.
    """
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    try:
        return task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


class TextContent:
    """Owned UTF-8 text spool; no implicit conversion to a full string.

    iter_text() is repeatable and yields at most 8192 Unicode characters per
    chunk. Iteration and closing do synchronous disk I/O: async consumers should
    copy and close it in their storage worker. close() is idempotent and invalidates readers;
    text_length remains available after closing. Only extraction writes to it.
    """

    def __init__(self):
        self._file = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_BYTES, mode="w+b")
        self._lock = threading.RLock()
        self._length = 0
        self._bytes = 0
        self._on_disk = False

    @property
    def text_length(self) -> int:
        return self._length

    @property
    def closed(self) -> bool:
        return self._file.closed

    @property
    def spooled_to_disk(self) -> bool:
        return self._on_disk

    def _append(self, text: str) -> None:
        if not text:
            return
        raw = text.encode("utf-8")
        with self._lock:
            if not self._on_disk and self._bytes + len(raw) > _SPOOL_MEMORY_BYTES:
                self._file.rollover()
                self._on_disk = True
            self._file.write(raw)
            self._bytes += len(raw)
            self._length += len(text)

    def _trim_end(self, byte_length: int, text_length: int) -> None:
        with self._lock:
            self._file.truncate(byte_length)
            self._bytes, self._length = byte_length, text_length

    def iter_text(self) -> Iterator[str]:
        decoder = codecs.getincrementaldecoder("utf-8")()
        offset = 0
        while True:
            with self._lock:
                if self.closed:
                    raise ValueError("Text content is closed.")
                self._file.seek(offset)
                raw = self._file.read(_CHUNK_BYTES)
            offset += len(raw)
            text = decoder.decode(raw, final=not raw)
            if text:
                yield text
            if not raw:
                break

    def close(self) -> None:
        with self._lock:
            self._file.close()


@dataclass(frozen=True, slots=True)
class FetchDocument:
    requested_url: str
    final_url: str
    title: str | None
    status_code: int
    content_type: str
    fetched_at: str
    redirects: tuple[str, ...]
    text: str | TextContent

    @property
    def text_length(self) -> int:
        return len(self.text) if isinstance(self.text, str) else self.text.text_length

    def iter_text(self) -> Iterator[str]:
        if isinstance(self.text, str):
            for start in range(0, len(self.text), _CHUNK_BYTES):
                yield self.text[start : start + _CHUNK_BYTES]
        else:
            yield from self.text.iter_text()

    def close(self) -> None:
        if isinstance(self.text, TextContent):
            self.text.close()


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, response: httpcore.Response):
        self.response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.stream:
            yield chunk

    async def aclose(self) -> None:
        await settled_close(self.response)


class _PublicTransport(httpx.AsyncBaseTransport):
    """Public API adapter: HTTPX models to HTTPcore's protocol implementation."""

    def __init__(self, network: PublicNetworkBackend):
        self.network = network
        # Explicit trust roots, without environment CA overrides.
        context = ssl.create_default_context(cafile=certifi.where())
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            network_backend=network,
            max_connections=1,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=_ResponseStream(response),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        try:
            await settled_close(self.pool)
        finally:
            await settled_close(self.network)


_IGNORED = {
    "script",
    "style",
    "noscript",
    "template",
    "nav",
    "footer",
    "header",
    "aside",
    "form",
    "button",
    "select",
    "textarea",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "audio",
    "video",
}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "wbr"}
_BLOCK = {
    "address",
    "article",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}


def _unsupported() -> AgentFault:
    return AgentFault("web_unsupported_content", "The response is not supported readable text.")


class _ReadableSink:
    """Normalize incrementally, retaining only one pending whitespace separator."""

    def __init__(self, content: TextContent):
        self.content = content
        self.pending = ""

    def append(self, data: str) -> None:
        parts: list[str] = []  # Bounded by one decoded input chunk/token.
        started = self.content.text_length > 0
        for match in _WHITESPACE.finditer(data):
            value = match.group()
            if value.isspace():
                if started:
                    if _LINE_BREAK.search(value):
                        self.pending = "\n"
                    elif not self.pending:
                        self.pending = " "
            else:
                parts.extend((self.pending, value))
                self.pending = ""
                started = True
        self.content._append("".join(parts))


_HEAD_ELEMENTS = {
    "base",
    "basefont",
    "bgsound",
    "link",
    "meta",
    "title",
    "noscript",
    "noframes",
    "style",
    "script",
    "template",
}
_ELEMENT_SCOPE = ("applet", "caption", "html", "table", "td", "th", "marquee", "object", "template")
_TABLE_SCOPE = ("html", "table", "template", "tbody", "thead", "tfoot")
_IMPLICIT_END_SCOPES = {
    "li": (("li",), _ELEMENT_SCOPE + ("ul", "ol", "menu")),
    "dt": (("dt", "dd"), _ELEMENT_SCOPE + ("dl",)),
    "dd": (("dt", "dd"), _ELEMENT_SCOPE + ("dl",)),
    "tr": (("tr",), _TABLE_SCOPE),
    "td": (("td", "th"), _TABLE_SCOPE + ("tr",)),
    "th": (("td", "th"), _TABLE_SCOPE + ("tr",)),
    "option": (("option",), ("select", "datalist", "optgroup", "template")),
}


class _ReadableHTML(HTMLParser):
    """Linear stack accounting and disk-backed candidates, not a browser DOM.

    Optional head/body/p/list/table end tags are recovered explicitly. Unmatched
    end tags take O(1); each pushed frame is popped at most once. Text callbacks
    inspect indexed active tags, never scan the growing ancestor stack.
    """

    def __init__(self, body: TextContent, main: TextContent, article: TextContent):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.positions: dict[str, list[int]] = {}
        self.body, self.main, self.article = map(_ReadableSink, (body, main, article))
        self.title: list[str] = []
        self.title_length = 0
        self.has_script = False

    def _pop(self, tag: str) -> None:
        positions = self.positions.get(tag)
        if not positions:
            return
        index = positions[-1]
        while len(self.stack) > index:
            name, _ = self.stack.pop()
            entries = self.positions[name]
            entries.pop()
            if not entries:
                del self.positions[name]

    def _pop_in_scope(self, tags: tuple[str, ...], boundaries: tuple[str, ...]) -> None:
        # Both tag sets have fixed size. Indexed lookups preserve O(1) recovery
        # decisions; frames removed by _pop are still popped at most once.
        boundary = max(
            (self.positions[name][-1] for name in boundaries if name in self.positions),
            default=-1,
        )
        index, tag = max(
            ((self.positions[name][-1], name) for name in tags if name in self.positions),
            default=(-1, ""),
        )
        if index > boundary:
            self._pop(tag)

    def _append(self, data: str) -> None:
        if self.stack and self.stack[-1][1]:
            return
        if "title" in self.positions:
            self.title_length += len(data)
            if self.title_length > _MAX_TITLE_CHARS:
                raise AgentFault(
                    "web_unsupported_markup", "The HTML title exceeds supported metadata length."
                )
            self.title.append(data)
        elif "head" not in self.positions:
            self.body.append(data)
            if "main" in self.positions:
                self.main.append(data)
            if "article" in self.positions:
                self.article.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self.get_starttag_text()) > _MAX_MARKUP_CHARS:
            raise AgentFault(
                "web_unsupported_markup", "An individual HTML markup token is too long."
            )
        if "head" in self.positions and tag not in _HEAD_ELEMENTS | {"html", "head"}:
            self._pop("head")
        if tag in _BLOCK:
            self._pop("p")
        if scope := _IMPLICIT_END_SCOPES.get(tag):
            self._pop_in_scope(*scope)
        attributes = dict(attrs)
        if tag == "script":
            self.has_script = True
        if tag in _BLOCK:
            self._append("\n")
        ignored = (
            bool(self.stack and self.stack[-1][1])
            or tag in _IGNORED
            or "hidden" in attributes
            or (attributes.get("aria-hidden") or "").lower() == "true"
            or bool(
                re.search(
                    r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
                    attributes.get("style") or "",
                    re.I,
                )
            )
        )
        if tag not in _VOID:
            if len(self.stack) >= _MAX_DEPTH:
                raise AgentFault("web_unsupported_markup", "The HTML nesting is too deep.")
            self.positions.setdefault(tag, []).append(len(self.stack))
            self.stack.append((tag, ignored))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK:
            self._append("\n")
        self._pop(tag)

    def handle_data(self, data: str) -> None:
        if (
            "head" in self.positions
            and "title" not in self.positions
            and data.strip()
            and not (self.stack and self.stack[-1][1])
        ):
            self._pop("head")
        self._append(data)

    def feed_bounded(self, text: str) -> None:
        self.feed(text)
        # HTMLParser buffers incomplete markup/entities in rawdata. In raw-text
        # script/style mode it also retains an entire unterminated script. Drop
        # ignored raw text immediately, retaining only a possible end-tag prefix.
        # These two HTMLParser state attributes are guarded here for Python 3.14.
        if self.cdata_elem and self.rawdata:
            start = self.rawdata.rfind("<")
            tail = self.rawdata[start:] if start >= 0 else ""
            possible = tail == "<"
            if tail.startswith("</"):
                name = tail[2:].lstrip().lower()
                possible = self.cdata_elem.startswith(name) or (
                    name.startswith(self.cdata_elem) and name[len(self.cdata_elem) :].isspace()
                )
            self.rawdata = tail if possible else ""
        if len(self.rawdata) > _MAX_MARKUP_CHARS:
            raise AgentFault(
                "web_unsupported_markup", "An individual HTML markup token is too long."
            )

    def result(self) -> tuple[str | None, TextContent]:
        content = next(
            (
                sink.content
                for sink in (self.main, self.article, self.body)
                if sink.content.text_length
            ),
            None,
        )
        if content is None:
            if self.has_script:
                raise AgentFault(
                    "web_js_only", "The page has no readable static text; JavaScript is not run."
                )
            raise AgentFault("web_empty", "The response contains no readable text.")
        return " ".join("".join(self.title).split()) or None, content


class _Extraction:
    """Own all candidate spools until a fully settled extraction is transferred."""

    def __init__(self, content_type: str):
        message = Message()
        message["content-type"] = content_type
        charset = message.get_content_charset() or "utf-8-sig"
        try:
            self.decoder = codecs.getincrementaldecoder(charset)(errors="strict")
            if not isinstance(self.decoder.decode(b""), str):
                raise ValueError
        except UnicodeError, LookupError, ValueError, TypeError:
            raise _unsupported() from None
        self.contents: list[TextContent] = []
        self.parser = None
        try:
            for _ in range(1 if message.get_content_type() == "text/plain" else 3):
                self.contents.append(TextContent())
            if len(self.contents) == 3:
                self.parser = _ReadableHTML(*self.contents)
        except BaseException:
            self.close()
            raise
        self.prefix = b""
        self.checked_prefix = False
        self.plain_end = (0, 0)

    def _decode(self, raw: bytes, *, final: bool = False) -> None:
        try:
            text = self.decoder.decode(raw, final=final)
            # Some advertised escape/stateful codecs otherwise retain unlimited
            # unfinished input even though wire chunks themselves are bounded.
            if len(self.decoder.getstate()[0]) > _MAX_MARKUP_CHARS:
                raise _unsupported()
        except UnicodeError, LookupError, ValueError, TypeError:
            raise _unsupported() from None
        if any(ord(c) < 32 and c not in "\t\r\n\f" for c in text):
            raise _unsupported()
        if self.parser is not None:
            try:
                self.parser.feed_bounded(text)
            except ValueError, AssertionError:
                raise _unsupported() from None
        else:
            content = self.contents[0]
            if not content.text_length:
                text = text.lstrip()
            trimmed = text.rstrip()
            if trimmed:
                self.plain_end = (
                    content._bytes + len(trimmed.encode("utf-8")),
                    content.text_length + len(trimmed),
                )
            content._append(text)

    def feed(self, raw: bytes) -> None:
        if not self.checked_prefix:
            self.prefix += raw
            if len(self.prefix) < 8:
                return
            raw, self.prefix = self.prefix, b""
            self.checked_prefix = True
            if raw.startswith(_BINARY_SIGNATURES):
                raise _unsupported()
        self._decode(raw)

    def finish(self) -> tuple[str | None, TextContent]:
        if not self.checked_prefix:
            # Check short binary signatures without requiring an eight-byte body.
            raw, self.prefix = self.prefix, b""
            if raw.startswith(_BINARY_SIGNATURES):
                raise _unsupported()
            self._decode(raw)
        self._decode(b"", final=True)
        if self.parser is not None:
            try:
                self.parser.close()
            except ValueError, AssertionError:
                raise _unsupported() from None
            return self.parser.result()
        content = self.contents[0]
        content._trim_end(*self.plain_end)
        if not content.text_length:
            raise AgentFault("web_empty", "The response contains no readable text.")
        return None, content

    def close(self, keep: TextContent | None = None) -> None:
        error = None
        for content in self.contents:
            if content is not keep:
                try:
                    content.close()
                except Exception as exception:
                    if error is None:
                        error = exception
        if error is not None:
            raise error


async def _extract(response: httpx.Response, content_type: str) -> tuple[str | None, TextContent]:
    extraction = _Extraction(content_type)
    try:
        async for raw in response.aiter_raw(chunk_size=_CHUNK_BYTES):
            await _blocking(extraction.feed, raw)
        title, content = await _blocking(extraction.finish)
        await _blocking(extraction.close, content)
        return title, content
    except BaseException:
        await _blocking(extraction.close)
        raise


class WebFetcher:
    """Fetch one public static document, with a single redirect-chain deadline.

    ``validate_url`` returns a canonical URL and performs no I/O. ``redirects``
    records canonical destinations followed, including the final destination.
    Each fetch owns a new unauthenticated client/pool; caller cancellation is
    propagated only after owned connection cleanup settles.
    """

    policy_version = POLICY_VERSION

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
        timeout: float = 30.0,
        max_redirects: int = 10,
    ):
        if not math.isfinite(timeout) or timeout <= 0 or max_redirects < 0:
            raise ValueError("Invalid fetch timeout or redirect limit.")
        self.resolver = resolver if resolver is not None else resolve_public_host
        self.backend = backend if backend is not None else httpcore.AnyIOBackend()
        self.timeout = timeout
        self.max_redirects = max_redirects

    def validate_url(self, url: str) -> str:
        return validate_url(url)

    async def fetch(self, url: str) -> FetchDocument:
        current = self.validate_url(url)
        try:
            return await self._fetch_owned(url, current)
        except TimeoutError, httpcore.TimeoutException, httpx.TimeoutException:
            raise AgentFault("web_timeout", "The web request timed out.") from None
        except socket.gaierror:
            raise AgentFault("web_dns_failed", "The public host could not be resolved.") from None
        except httpx.InvalidURL:
            # HTTPX parses Location even with automatic following disabled.
            raise AgentFault("web_redirect_invalid", "The web redirect is invalid.") from None
        except AgentFault:
            raise
        except Exception:
            # Includes malformed protocol/decoding/cleanup errors. Never surface
            # exception text, remote bodies, certificate details, or DNS answers.
            raise AgentFault("web_fetch_failed", "The public web request failed.") from None

    async def _fetch_owned(self, requested: str, current: str) -> FetchDocument:
        network = PublicNetworkBackend(self.resolver, self.backend)
        client = httpx.AsyncClient(
            transport=_PublicTransport(network),
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout,
            auth=None,
        )
        document = None
        transferred = False
        try:
            try:
                async with asyncio.timeout(self.timeout) as deadline:
                    document = await self._fetch(client, requested, current)
                    if asyncio.get_running_loop().time() >= deadline.when():
                        raise TimeoutError
            finally:
                await settled_close(client)
            transferred = True
            return document
        finally:
            if document is not None and not transferred:
                await _blocking(document.close)

    async def _fetch(
        self, client: httpx.AsyncClient, requested: str, current: str
    ) -> FetchDocument:
        visited: set[str] = set()
        redirects: list[str] = []
        while True:
            if current in visited:
                raise AgentFault(
                    "web_redirect_loop", "The web request encountered a redirect loop."
                )
            visited.add(current)
            # Construct directly, bypassing the client's cookie jar/default auth.
            # Never copy headers from a preceding request or response.
            request = httpx.Request(
                "GET",
                current,
                headers={
                    "Accept": "text/html, application/xhtml+xml, text/plain",
                    "Accept-Encoding": "identity",
                    "User-Agent": "ls-assistants-web-fetch/1",
                },
                extensions={"timeout": httpx.Timeout(self.timeout).as_dict()},
            )
            response = await client.send(request, stream=True, auth=None)
            text = None
            document = None
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    locations = response.headers.get_list("location")
                    if len(locations) != 1 or not locations[0]:
                        raise AgentFault("web_redirect_invalid", "The web redirect is invalid.")
                    if len(redirects) >= self.max_redirects:
                        raise AgentFault(
                            "web_redirect_limit", "The web redirect limit was reached."
                        )
                    check_url_characters(locations[0])
                    try:
                        destination = urljoin(current, locations[0])
                    except ValueError:
                        raise AgentFault(
                            "web_redirect_invalid", "The web redirect is invalid."
                        ) from None
                    current = self.validate_url(destination)
                    redirects.append(current)
                    continue
                if not 200 <= response.status_code < 300:
                    raise AgentFault("web_http_error", "The public server returned an HTTP error.")
                types = response.headers.get_list("content-type")
                content_type = types[0] if len(types) == 1 else ""
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type not in {
                    "text/html",
                    "application/xhtml+xml",
                    "text/plain",
                } or response.headers.get("content-disposition", "").lower().startswith(
                    "attachment"
                ):
                    raise AgentFault(
                        "web_unsupported_type", "Only static HTML and plain text are supported."
                    )
                encodings = response.headers.get("content-encoding", "identity").lower().split(",")
                if any(value.strip() != "identity" for value in encodings):
                    raise AgentFault(
                        "web_unsupported_encoding", "Only identity-encoded responses are supported."
                    )
                title, text = await _extract(response, content_type)
                document = FetchDocument(
                    requested_url=requested,
                    final_url=current,
                    title=title,
                    status_code=response.status_code,
                    content_type=media_type,
                    fetched_at=datetime.now(UTC).isoformat(),
                    redirects=tuple(redirects),
                    text=text,
                )
            finally:
                try:
                    await settled_close(response)
                except BaseException:
                    if text is not None:
                        await _blocking(text.close)
                    raise
                if text is not None and document is None:
                    await _blocking(text.close)
            return document
