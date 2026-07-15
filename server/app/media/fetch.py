"""外部の URL から画像を取得する。任意の URL をサーバーに取得させる機能であり、対策を欠くとそのまま SSRF になる。

このサービスは MySQL と同じ Docker ネットワークに居るため http://db:3306 は実際に到達可能であり、クラウド上では 169.254.169.254 がインスタンスの資格情報を返す。「内部の URL など誰も書かない」という前提は成り立たない。本文の Markdown に画像として書くだけで、サーバーの目線で任意の宛先へ HTTP 要求を出させられる。

そのため、ここでは次の全てを行う。1 つでも欠けると擦り抜けが成立する。

- スキームは http / https のみ。リダイレクト先も含め毎回検査する。
- 名前解決して得た全ての IP を検査する。1 つでも禁止範囲に当たれば取得しない(A レコードを複数返し、検査を通った 1 つだけを見せる細工を防ぐ)。
- 検査した IP そのものへ接続する。検査と接続の間にもう一度名前解決すると、その隙に DNS の応答を差し替えられ、検査は公開 IP・接続は内部 IP という擦り抜けが成立する(DNS リバインディング)。URL のホストを検査済みの IP へ差し替え、Host ヘッダと SNI に元のホスト名を入れることで、解決を固定したまま証明書の検証を元のホスト名に対して行う。
- リダイレクトを httpx に追わせない。追わせると、その追跡はライブラリの中で名前解決からやり直され、検査を経ずに内部へ到達する。各ホップの Location を自前で解決し、ホップごとに IP を検査し直す。
- Content-Length を信用しない。宣言と実際の送信量は一致する必要がなく、小さいと偽って無制限に送りつけられる。ストリームしながら数え、上限を超えた時点で打ち切る。数えるのは伸長前の実通信バイト数である。伸長後を数えると、上限の検査に届く前にライブラリの復号器が既に確保を終えており、上限は「保持する量」しか縛らない。
- 応答を伸長させない。Accept-Encoding: identity は要求側の希望に過ぎず、敵対的なサーバーはこれを無視して Content-Encoding: gzip を返せる。したがって、伸長を行わない経路で読み(aiter_raw)、identity 以外の Content-Encoding が付いた応答は拒否する。これが無いと、約 1 MB の gzip 爆弾で 2 桁倍のメモリを確保させられる(希望を書いたヘッダは対策ではない)。画像は既に圧縮されており、正当な画像配信が応答を更に gzip することはないため、拒否しても取得を妨げない。
- プロキシの環境変数を読ませない(trust_env=False)。プロキシを経由すると、接続先はプロキシであって検査した IP ではなくなり、宛先を固定した意味が消える。

拒否した理由に解決結果の IP を含めない。取り込みの失敗は記事の保存の応答として執筆者へ返るため、IP を書くと「内部のホスト名を書いて、返ってきた理由から内部ネットワークを地図にする」ことができてしまう。IP はログにだけ残す。
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

# 取得を許すスキーム。file / gopher / ftp などは、リダイレクト先として指定されるだけでも到達しうるため毎ホップ検査する。
ALLOWED_SCHEMES: Final = ('http', 'https')
_DEFAULT_PORTS: Final[dict[str, int]] = {'http': 80, 'https': 443}
_REDIRECT_STATUS: Final = frozenset({301, 302, 303, 307, 308})
_CHUNK_SIZE: Final = 65536
_USER_AGENT: Final = 'instant-news/1.0 (+image ingest)'
# 応答を伸長させない。画像は既に圧縮されており伸長の利得は無い一方、伸長を許すと圧縮爆弾の面が増える。ただしこのヘッダは希望を伝えるだけであり、対策として働くのは _request が伸長せずに読み、identity 以外の Content-Encoding を拒むことによる。
_REQUEST_HEADERS: Final[dict[str, str]] = {
    'User-Agent': _USER_AGENT,
    'Accept': 'image/*;q=0.9,*/*;q=0.1',
    'Accept-Encoding': 'identity',
}
# 内容の符号化として受け入れる値。空(ヘッダ自体が無い)か identity のみ。
_ALLOWED_CONTENT_ENCODINGS: Final = frozenset({'', 'identity'})


class FetchError(Exception):
    """外部の URL から画像を取得できなかった。

    文言は記事の保存の応答として執筆者へ返るため、執筆者が書いた URL について執筆者が既に知っていること(ホスト名・HTTP の状態コードなど)だけを含め、解決結果の IP のような内部の情報は含めない。
    """


def _forbidden_reason(ip: IpAddress) -> str | None:
    """到達を許さない範囲なら理由を返す。許す範囲なら None。

    最後の is_global は、上の個別の判定から漏れる範囲(共有アドレス空間 100.64.0.0/10 や文書用の範囲など)をまとめて塞ぐための網である。公開インターネットのアドレスは is_global が真であるため、正当な取得を妨げない。
    """
    if ip.is_unspecified:
        return '未指定'
    if ip.is_loopback:
        return 'ループバック'
    if ip.is_link_local:
        # 169.254.169.254 はクラウドのメタデータ取得口であり、到達できると資格情報がそのまま漏れる。
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
    """検査すべきアドレスを列挙する。IPv6 に IPv4 が埋め込まれている場合は、埋め込まれた IPv4 も検査する。

    ::ffff:127.0.0.1 のような IPv4 射影アドレスは、IPv6 として見ると各判定が偽になりうる一方、接続すると実際には 127.0.0.1 へ届く。6to4 と Teredo も同様に IPv4 を内包する。取り出して検査しないと、これらの表記でループバックやプライベートへ到達できる。
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
    """解決結果のアドレス 1 つを検査する。到達を許さない範囲なら FetchError。"""
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
    """ホスト名を解決し、得られた全ての IP を検査して返す。1 つでも禁止範囲に当たれば取得しない。

    複数の A レコードのうち 1 つでも内部を指すなら、接続に選ばれるのがどれかは運でしかない。全て安全である場合にだけ先へ進む。
    """
    loop = asyncio.get_running_loop()
    try:
        # AI_ADDRCONFIG … 経路を持たないアドレス族(IPv4 のみの環境での AAAA など)を除く。固定した 1 つが到達不能で失敗する事態を減らす。
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
    """URL を (スキーム, ホスト, ポート, パス, クエリ)へ分解する。スキームが許可外・ホストが無い場合は FetchError。"""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        # ポートが数値でない場合などに urlsplit.port が ValueError を投げる。
        raise FetchError(f'URL として解釈できません: {url}') from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise FetchError(f'取得できるのは http と https の URL だけです: {url}')
    host = parts.hostname
    if not host:
        raise FetchError(f'URL にホストがありません: {url}')
    return scheme, host, port or _DEFAULT_PORTS[scheme], parts.path or '/', parts.query


def _authority(address: str, port: int) -> str:
    """接続先の authority を組み立てる。IPv6 はブラケットで囲む必要がある。"""
    if ':' in address:
        return f'[{address}]:{port}'
    return f'{address}:{port}'


async def _request(target: str, headers: dict[str, str], sni_hostname: str, cfg: MediaFetchConfig) -> tuple[str | None, bytes | None]:
    """検査済みの IP へ 1 回だけ要求を出す。リダイレクトなら (Location, None)、最終応答なら (None, 本体)。

    extensions の sni_hostname は httpx(httpcore)が TLS の server_hostname に使う値であり、これによって「接続先は検査した IP、SNI と証明書の検証は元のホスト名」が両立する。この指定が無いと、IP へ接続した時点で証明書の検証が IP に対して行われ、正当なホストの取得が全て失敗する。
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
                # identity を要求している以上、それを無視して符号化を掛けてくる相手は壊れているか敵対的である。後段の再符号化(transcode)が要るのは画像のバイト列そのものであり、正当な画像配信が応答を更に圧縮することはないため、ここで拒んでも取得を妨げない。
                encoding = response.headers.get('Content-Encoding', '').strip().lower()
                if encoding not in _ALLOWED_CONTENT_ENCODINGS:
                    raise FetchError(f'応答が {encoding} で符号化されているため取得できません。')
                buffer = bytearray()
                # Content-Length は見ない。宣言と実際の送信量が一致する保証はなく、小さいと偽って際限なく送りつけられる。実際に受け取った量だけを数える。
                # aiter_bytes ではなく aiter_raw で読む。aiter_bytes は Content-Encoding を見て透過的に伸長するため、上限の検査は復号器が伸長を終えた後にしか働かず、約 1 MB の gzip 爆弾で 2 桁倍のメモリを確保させられる。aiter_raw は伸長を行わないため、この数はそのまま実通信バイト数になる。
                async for chunk in response.aiter_raw(_CHUNK_SIZE):
                    buffer.extend(chunk)
                    if len(buffer) > cfg.max_bytes:
                        raise FetchError(f'画像が大きすぎます(1 枚あたり {cfg.max_bytes} バイトまで)。')
                if not buffer:
                    raise FetchError('応答が空です。')
                return None, bytes(buffer)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # 接続そのものの失敗は、別のアドレスを試す呼び出し元へ渡す。
        raise
    except httpx.HTTPError as exc:
        raise FetchError(f'取得に失敗しました: {exc}') from exc


async def _perform(url: str, cfg: MediaFetchConfig) -> tuple[str | None, bytes | None]:
    """1 ホップ分の取得。名前解決・検査・接続先の固定をここで行う。"""
    scheme, host, port, path, query = _split(url)
    addresses = await _resolve_checked(host, port)
    # Host ヘッダには元のホスト名を入れる。接続先を IP に差し替えても、相手のバーチャルホストの選択は元のホスト名で行われる必要がある。
    headers = dict(_REQUEST_HEADERS)
    headers['Host'] = host if port == _DEFAULT_PORTS[scheme] else f'{host}:{port}'
    last_error: Exception | None = None
    for address in addresses:
        target = urlunsplit((scheme, _authority(address, port), path, query, ''))
        try:
            return await _request(target, headers, host, cfg)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # 解決結果は全て検査済みであるため、到達できないものを飛ばして次を試してよい。
            last_error = exc
            continue
    raise FetchError(f'{host} へ接続できません: {last_error}')


async def _fetch_following_redirects(url: str, cfg: MediaFetchConfig) -> bytes:
    """リダイレクトを自前で追う。各ホップで scheme と宛先 IP を検査し直す。"""
    current = url
    visited: list[str] = []
    for _hop in range(cfg.max_redirects + 1):
        location, data = await _perform(current, cfg)
        if data is not None:
            return data
        visited.append(current)
        # Location は相対でもよい。元のホスト名を持つ URL(接続先へ差し替える前の URL)を基準に解決する。
        current = urljoin(current, location)
        if current in visited:
            raise FetchError('リダイレクトが循環しています。')
    raise FetchError(f'リダイレクトが上限({cfg.max_redirects} 回)を超えました。')


async def fetch_image(url: str, cfg: MediaFetchConfig) -> bytes:
    """URL から画像のバイト列を取得する。取得できない場合は FetchError。

    timeout_seconds は 1 枚あたりの取得全体の締切として掛ける。httpx のタイムアウトは接続・読み取りといった操作ごとにしか効かないため、それだけでは「上限より僅かに短い間隔でバイトを送り続ける」相手に対して無限に待つことになる。

    ここで返るのは未検証のバイト列に過ぎない。これが画像であることも、宣言された形式であることも保証しない。安全になるのは transcode で復号して再符号化した後である。
    """
    try:
        async with asyncio.timeout(cfg.timeout_seconds):
            return await _fetch_following_redirects(url, cfg)
    except TimeoutError as exc:
        raise FetchError(f'{cfg.timeout_seconds} 秒以内に取得できませんでした。') from exc
