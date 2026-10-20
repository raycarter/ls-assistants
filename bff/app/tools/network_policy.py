"""Public-network URL, DNS, and connected-peer enforcement.

DNS is an advisory input only: every answer is checked, then a numeric address
is passed to the connector. HTTPcore retains the original HTTP/TLS origin.
Injected resolvers/backends are trusted test seams, not user configuration.
"""

import asyncio
import ipaddress
import re
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable, Sequence
from urllib.parse import urlsplit

import httpcore
import httpx

from app.agent_files import AgentFault

POLICY_VERSION = "public-web-v1"
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_LOCAL_SUFFIXES = ("localhost", "local", "internal", "lan", "home", "home.arpa")
_METADATA_HOSTS = ("metadata.goog", "metadata.tencentyun.com")
_V6_UNICAST = ipaddress.ip_network("2000::/3")
_V6_TRANSITION = (ipaddress.ip_network("2001::/23"), ipaddress.ip_network("2002::/16"))


def _denied() -> AgentFault:
    return AgentFault("web_url_denied", "Only public HTTP(S) URLs are allowed.", 422)


def public_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Reject nonpublic and transition addresses, including mapped/embedded IPv4."""
    try:
        if "%" in value:
            raise ValueError
        address = ipaddress.ip_address(value)
    except ValueError, TypeError:
        raise _denied() from None
    if (
        not address.is_global
        or address.is_private
        or address.is_reserved
        or address.is_multicast
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        # Azure platform virtual IP: globally numbered, not a public web origin.
        or address == ipaddress.ip_address("168.63.129.16")
    ):
        raise _denied()
    if isinstance(address, ipaddress.IPv6Address) and (
        address not in _V6_UNICAST
        or any(address in network for network in _V6_TRANSITION)
        or (int(address) >> 32) & 0xFFFFFFFF in (0x00005EFE, 0x02005EFE)  # ISATAP
    ):
        raise _denied()
    return address


def check_url_characters(value: str) -> None:
    # urlsplit/urljoin otherwise silently strip some control characters.
    if not isinstance(value, str) or any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value
    ):
        raise _denied()


def validate_url(value: str) -> str:
    """Synchronous lexical policy only; never performs DNS or HTTP.

    Return a canonical URL without a fragment. Explicit DNS names must have at
    least two labels; legacy numeric IPv4 spellings and scoped IPv6 are denied.
    """
    check_url_characters(value)
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.netloc
            or "@" in parts.netloc
            or not parts.hostname
            or "%" in parts.netloc
            or parts.netloc.endswith(":")
            or (parts.port is not None and not 1 <= parts.port <= 65535)
        ):
            raise _denied()
        parsed = httpx.URL(value)
        host = parsed.raw_host.decode("ascii").lower().removesuffix(".")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if (
                len(host) > 253
                or len(labels) < 2
                or any(not _LABEL.fullmatch(label) for label in labels)
                or labels[-1].isdigit()
                or re.fullmatch(r"0x[0-9a-f]+", labels[-1])
                or any(
                    host == suffix or host.endswith("." + suffix)
                    for suffix in (*_LOCAL_SUFFIXES, *_METADATA_HOSTS)
                )
            ):
                raise _denied() from None
        else:
            public_ip(host)
        # Only the host spelling/fragment changes; HTTPX handles IDNA and escaping.
        return str(parsed.copy_with(host=host, fragment=None))
    except ValueError, UnicodeError, httpx.InvalidURL:
        raise _denied() from None


async def resolve_public_host(host: str, port: int) -> Sequence[str]:
    """Use the event loop's cancellable resolver, never an unowned worker thread.

    Cancelling getaddrinfo cancels our await; the OS resolver itself may finish
    in asyncio's managed executor. No later connection is started from it.
    """
    answers = await asyncio.get_running_loop().getaddrinfo(
        host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return [answer[4][0] for answer in answers]


async def settled_close(resource) -> None:
    """Finish owned async cleanup even if cancellation is requested again."""
    task = asyncio.create_task(resource.aclose())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


class _PinnedStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream, address: str, port: int):
        self.stream = stream
        self.address = address
        self.port = port
        self.closed = False

    def check_peer(self) -> None:
        try:
            peer = self.stream.get_extra_info("server_addr")
            if (
                not isinstance(peer, tuple)
                or len(peer) < 2
                or public_ip(peer[0]) != public_ip(self.address)
                or peer[1] != self.port
            ):
                raise ValueError
        except Exception:
            raise AgentFault(
                "web_peer_denied", "The connected peer could not be verified as public."
            ) from None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self.check_peer()
        await self.stream.write(buffer, timeout)

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 -- HTTPcore interface
    ) -> httpcore.AsyncNetworkStream:
        try:
            self.check_peer()
            self.stream = await self.stream.start_tls(ssl_context, server_hostname, timeout)
            self.check_peer()
            return self
        except BaseException:
            await settled_close(self)
            raise

    def get_extra_info(self, info: str):
        return self.stream.get_extra_info(info)

    async def aclose(self) -> None:
        if not self.closed:
            await self.stream.aclose()
            self.closed = True


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Own and verify streams, including ones HTTPcore loses during failed TLS."""

    def __init__(self, resolver: Resolver, backend: httpcore.AsyncNetworkBackend):
        self.resolver = resolver
        self.backend = backend
        self.streams: list[_PinnedStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 -- HTTPcore interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            answers = await self.resolver(host, port)
        else:
            answers = [str(literal)]
        # Check ALL answers before making ANY connection. No happy-eyeballs race
        # may reach an unchecked answer, and the connector never sees a hostname.
        addresses = tuple(dict.fromkeys(str(public_ip(answer)) for answer in answers))
        if not addresses:
            raise AgentFault("web_dns_failed", "The public host could not be resolved.")
        stream = await self.backend.connect_tcp(
            addresses[0], port, timeout, local_address, socket_options
        )
        pinned = _PinnedStream(stream, addresses[0], port)
        self.streams.append(pinned)
        try:
            pinned.check_peer()
        except BaseException:
            await settled_close(pinned)
            raise
        return pinned

    async def connect_unix_socket(self, *args, **kwargs):
        raise _denied()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def aclose(self) -> None:
        # All streams remain owned even if pool/TLS setup was interrupted.
        results = await asyncio.gather(
            *(settled_close(stream) for stream in self.streams), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
