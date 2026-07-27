"""Fetch images from external URLs. The feature makes the server fetch a URL of someone else's choosing, so without the measures below it is SSRF outright.

This service sits on the same Docker network as MySQL, so http://db:3306 really is reachable, and in the cloud 169.254.169.254 returns the instance's credentials. "Nobody would write an internal URL" is not a premise that holds: writing one as an image in the body Markdown is enough to make the server issue an HTTP request to any destination, with the server's own network vantage point.

So all of the following are done here. Drop any one of them and a bypass opens up.

- Only the http / https schemes, checked on every hop, redirect next hops included.
- Check every IP the name resolves to, and fetch nothing if even one lands in a forbidden range (this defeats returning several A records and exposing only the one that passed).
- Connect to the checked IP itself. Resolving again between check and connect leaves a gap in which the DNS answer can be swapped, so the check sees a public IP while the connection goes to an internal one (DNS rebinding). Replacing the URL's host with the checked IP and putting the original hostname in the Host header and SNI pins the resolution while still validating the certificate against the original hostname.
- Never let httpx follow redirects: it would re-resolve inside the library and reach internal addresses without any check. Each hop's Location is resolved here and the IP re-checked per hop.
- Do not trust Content-Length. The declared size need not match what is actually sent, so a peer can understate it and then send without limit. Bytes are counted while streaming and the read is cut off the moment the max is passed. What is counted is the wire bytes before decompression: count after it and the library's decoder has already finished allocating before the check runs, so the max bounds only what is retained.
- Never decompress the response. Accept-Encoding: identity is no more than the requesting side's wish, and a hostile server can ignore it and return Content-Encoding: gzip. So the read goes through a path that does not decompress (aiter_raw), and a response carrying any Content-Encoding other than identity is refused. Without this, a ~1 MB gzip bomb forces tens of times that much memory to be allocated (a header stating a wish is not a countermeasure). Images are already compressed and legitimate image delivery does not gzip the response on top, so refusing costs no real fetches.
- Do not read the proxy environment variables (trust_env=False). Through a proxy the connection target is the proxy rather than the checked IP, which voids the point of pinning the destination.

Refusal reasons never name the resolved IP. An ingest failure comes back to the writer in the article-save response, so naming the IP would let someone write internal hostnames and map the internal network from the reasons that come back. IPs go to the log only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Iterator
from typing import Final
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from common.settings import MediaFetchConfig

logger = logging.getLogger(__name__)

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Schemes allowed for fetching. file / gopher / ftp and friends can be reached merely by being named as a redirect next hop, so this is checked on every hop.
ALLOWED_SCHEMES: Final = ('http', 'https')
_DEFAULT_PORTS: Final[dict[str, int]] = {'http': 80, 'https': 443}
_REDIRECT_STATUS: Final = frozenset({301, 302, 303, 307, 308})
_CHUNK_SIZE: Final = 65536
_USER_AGENT: Final = 'instant-news/1.0 (+image ingest)'
# Ask for no compression. Images are already compressed so there is nothing to gain, while allowing it widens the compression-bomb attack surface. This header only states a wish, though: the measure that actually works is _request reading without decompressing and refusing any Content-Encoding other than identity.
_REQUEST_HEADERS: Final[dict[str, str]] = {
    'User-Agent': _USER_AGENT,
    'Accept': 'image/*;q=0.9,*/*;q=0.1',
    'Accept-Encoding': 'identity',
}
# Content-Encoding values accepted: empty (no header at all) or identity, nothing else.
_ALLOWED_CONTENT_ENCODINGS: Final = frozenset({'', 'identity'})


class FetchError(Exception):
    """An image could not be fetched from an external URL.

    The wording comes back to the writer in the article-save response, so it carries only what the writer already knows about the URL they wrote (hostname, HTTP status code) and never internal information such as the resolved IP.
    """


def _forbidden_reason(ip: IpAddress) -> str | None:
    """Return the reason if the address is in a range we refuse to reach, or None if it is allowed.

    The final is_global is the net that closes off, in one go, the ranges the individual checks above miss (the shared address space 100.64.0.0/10, the documentation ranges, and so on). Public-internet addresses have is_global true, so it costs no legitimate fetches.
    """
    if ip.is_unspecified:
        return '未指定'
    if ip.is_loopback:
        return 'ループバック'
    if ip.is_link_local:
        # 169.254.169.254 is the cloud metadata endpoint: reach it and the credentials leak outright.
        return 'リンクローカル'
    if ip.is_multicast:
        return 'マルチキャスト'
    if ip.is_private:
        return 'プライベート'
    if ip.is_reserved:
        return '予約済み'
    if not ip.is_global:
        return '公開インターネットの範囲外'
    return None


def _candidates(ip: IpAddress) -> Iterator[IpAddress]:
    """Enumerate the addresses to check. Where an IPv6 address embeds an IPv4 one, the embedded IPv4 is checked too.

    An IPv4-mapped address such as ::ffff:127.0.0.1 can make every check read false when judged as IPv6, yet connecting to it really does reach 127.0.0.1. 6to4 and Teredo embed IPv4 the same way. Without pulling those out and checking them, these notations reach loopback and private ranges.
    """
    yield ip
    if not isinstance(ip, ipaddress.IPv6Address):
        return
    if ip.ipv4_mapped is not None:
        yield ip.ipv4_mapped
    if ip.sixtofour is not None:
        yield ip.sixtofour
    if ip.teredo is not None:
        server, client = ip.teredo
        yield server
        yield client


def _check_address(text: str, host: str) -> None:
    """Check one resolved address. FetchError if it is in a range we refuse to reach."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError as exc:
        raise FetchError(f'{host} の解決結果を IP アドレスとして解釈できません。') from exc
    for candidate in _candidates(ip):
        reason = _forbidden_reason(candidate)
        if reason is not None:
            logger.warning('画像の取得を拒否しました: ホスト=%s 解決結果=%s 理由=%s', host, text, reason)
            raise FetchError(f'{host} は到達を許可しない宛先へ解決されるため取得できません。')


async def _resolve_checked(host: str, port: int) -> list[str]:
    """Resolve the hostname, check every IP it yields, and return them. If even one lands in a forbidden range, nothing is fetched.

    If any one of several A records points inside, which one the connection picks is pure luck. Proceed only when all of them are safe.
    """
    loop = asyncio.get_running_loop()
    try:
        # AI_ADDRCONFIG drops address families with no route (AAAA on an IPv4-only host, say), which reduces failures where the one address we pinned turns out to be unreachable.
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_ADDRCONFIG)
    except socket.gaierror as exc:
        raise FetchError(f'ホスト名を解決できません: {host}') from exc
    addresses: list[str] = []
    for *_unused, sockaddr in infos:
        text = str(sockaddr[0])
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise FetchError(f'ホスト名を解決できません: {host}')
    for text in addresses:
        _check_address(text, host)
    return addresses


def _split(url: str) -> tuple[str, str, int, str, str]:
    """Split a URL into (scheme, host, port, path, query). FetchError if the scheme is not allowed or there is no host."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        # urlsplit.port raises ValueError when the port is not numeric, among other cases.
        raise FetchError(f'URL として解釈できません: {url}') from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise FetchError(f'取得できるのは http と https の URL だけです: {url}')
    host = parts.hostname
    if not host:
        raise FetchError(f'URL にホストがありません: {url}')
    return scheme, host, port or _DEFAULT_PORTS[scheme], parts.path or '/', parts.query


def _authority(address: str, port: int) -> str:
    """Build the authority to connect to. IPv6 has to be wrapped in brackets."""
    if ':' in address:
        return f'[{address}]:{port}'
    return f'{address}:{port}'


async def _request(target: str, headers: dict[str, str], sni_hostname: str, cfg: MediaFetchConfig) -> tuple[str | None, bytes | None]:
    """Issue exactly one request to a checked IP. Returns (Location, None) for a redirect, (None, body) for a final response.

    The sni_hostname extension is the value httpx (httpcore) uses as the TLS server_hostname, and it is what lets "connect to the checked IP, but do SNI and certificate validation against the original hostname" hold at once. Without it, connecting to an IP validates the certificate against that IP and every fetch of a legitimate host fails.
    """
    timeout = httpx.Timeout(cfg.timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            async with client.stream('GET', target, headers=headers, extensions={'sni_hostname': sni_hostname}) as response:
                if response.status_code in _REDIRECT_STATUS:
                    location = response.headers.get('Location')
                    if not location:
                        raise FetchError(f'リダイレクトの応答に Location がありません(HTTP {response.status_code})。')
                    return location, None
                if response.status_code != httpx.codes.OK:
                    raise FetchError(f'取得できませんでした(HTTP {response.status_code})。')
                # We asked for identity, so a peer that ignores that and encodes anyway is broken or hostile. What the later re-encoding (transcode) needs is the image bytes themselves, and legitimate image delivery does not compress the response on top, so refusing here costs no real fetches.
                encoding = response.headers.get('Content-Encoding', '').strip().lower()
                if encoding not in _ALLOWED_CONTENT_ENCODINGS:
                    raise FetchError(f'応答が {encoding} で符号化されているため取得できません。')
                buffer = bytearray()
                # Content-Length is not consulted. Nothing guarantees the declaration matches what is sent, so a peer can understate it and then send endlessly. Only what actually arrives is counted.
                # Read with aiter_raw, not aiter_bytes: aiter_bytes honours Content-Encoding and decompresses transparently, so the max check would only run once the decoder had finished expanding, and a ~1 MB gzip bomb would force tens of times that much memory to be allocated. aiter_raw does not decompress, so this count is the wire bytes as they are.
                async for chunk in response.aiter_raw(_CHUNK_SIZE):
                    buffer.extend(chunk)
                    if len(buffer) > cfg.max_bytes:
                        raise FetchError(f'画像が大きすぎます(1 枚あたり {cfg.max_bytes} バイトまで)。')
                if not buffer:
                    raise FetchError('応答が空です。')
                return None, bytes(buffer)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # A failure of the connection itself passes through to the caller, which tries another address.
        raise
    except httpx.HTTPError as exc:
        raise FetchError(f'取得に失敗しました: {exc}') from exc


async def _perform(url: str, cfg: MediaFetchConfig) -> tuple[str | None, bytes | None]:
    """One hop's fetch. Name resolution, checking and pinning the connection target all happen here."""
    scheme, host, port, path, query = _split(url)
    addresses = await _resolve_checked(host, port)
    # The Host header carries the original hostname: even though the connection target is swapped for an IP, the peer must still select its virtual host by the original hostname.
    headers = dict(_REQUEST_HEADERS)
    headers['Host'] = host if port == _DEFAULT_PORTS[scheme] else f'{host}:{port}'
    last_error: Exception | None = None
    for address in addresses:
        target = urlunsplit((scheme, _authority(address, port), path, query, ''))
        try:
            return await _request(target, headers, host, cfg)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Every resolved address has already been checked, so skipping an unreachable one and trying the next is safe.
            last_error = exc
            continue
    raise FetchError(f'{host} へ接続できません: {last_error}')


async def _fetch_following_redirects(url: str, cfg: MediaFetchConfig) -> bytes:
    """Follow redirects ourselves, re-checking the scheme and the destination IP on every hop."""
    current = url
    visited: list[str] = []
    for _hop in range(cfg.max_redirects + 1):
        location, data = await _perform(current, cfg)
        if data is not None:
            return data
        visited.append(current)
        # Location may be relative, so it resolves against the URL that still holds the original hostname (the one from before the connection target was substituted in).
        current = urljoin(current, location)
        if current in visited:
            raise FetchError('リダイレクトが循環しています。')
    raise FetchError(f'リダイレクトが上限({cfg.max_redirects} 回)を超えました。')


async def fetch_image(url: str, cfg: MediaFetchConfig) -> bytes:
    """Fetch an image's bytes from a URL. FetchError if it cannot be fetched.

    timeout_seconds applies as a deadline over the whole per-image fetch. httpx's own timeouts bound only individual operations such as connect and read, and on their own would wait forever on a peer that keeps sending bytes at just under that interval.

    What comes back here is no more than unverified bytes: nothing guarantees it is an image, or that it is in the format it declares. It becomes safe only after transcode has decoded and re-encoded it.
    """
    try:
        async with asyncio.timeout(cfg.timeout_seconds):
            return await _fetch_following_redirects(url, cfg)
    except TimeoutError as exc:
        raise FetchError(f'{cfg.timeout_seconds} 秒以内に取得できませんでした。') from exc
