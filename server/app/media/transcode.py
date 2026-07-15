"""受け取ったバイト列を復号し、WebP へ再符号化する。

受け取ったバイト列をそのまま保存してはならない。復号して再符号化することが、この経路で単独で最も効く対策である。

- ポリグロット(GIF としても HTML としても JavaScript としても妥当なファイル)が壊れる。再符号化の出力は、こちらが持つ符号化器が書いたバイト列だけで構成され、元のバイト列は 1 バイトも残らない。
- EXIF が落ちる。撮影地の座標や端末の識別子が、本人の意図と無関係に画像へ埋まっている。
- 配信時の Content-Type を自分で決められる。相手の申告した形式を信じる必要が無くなる。

展開爆弾(数 KB の PNG が展開時に数十 GB の画素になる)への対策はバイト数では効かない。バイト数の上限を通り抜けたものが、復号した瞬間にメモリを食い尽くす。画素数で上限を設け、復号の前に検査する。

メタデータは EXIF に限らず全て捨てる。EXIF は位置情報を含み、XMP も同様の情報を持ちうる。ICC プロファイルに秘密は無いが、mode を変換した後のデータに元のプロファイル(例えば CMYK 用のもの)が付いたままになると、変換後のデータを説明しないプロファイルが残り、色が壊れる。全て捨てて sRGB として扱うのが、この用途では最も破綻が少ない。
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any, Final

from PIL import Image, ImageSequence

# 変種ごとの横幅。名前の一覧は store.VARIANTS が持つ('orig' は原寸であるためここには現れない)。
VARIANT_WIDTHS: Final[dict[str, int]] = {'1200w': 1200, '400w': 400}

# 非可逆の品質。可逆のときは Pillow がこの値を圧縮の手間として解釈する(値の意味が変わるだけで、指定しない理由にはならない)。
WEBP_QUALITY: Final = 90
# 符号化の手間(0〜6)。静止画は最良を選ぶ。アニメーションは全フレームに掛かるため、記事の保存を待たせない値に留める。
_METHOD_STILL: Final = 6
_METHOD_ANIMATED: Final = 4
# WebP が表現できる辺の上限。これを超える画像は、そもそも WebP として保存できない。
WEBP_MAX_DIMENSION: Final = 16383
# アルファを持つ mode。P の透過は info の transparency として現れるため、mode だけでは判定できない。
_ALPHA_MODES: Final = frozenset({'RGBA', 'LA', 'PA', 'RGBa', 'La'})
# フレームの表示時間がこれ未満の GIF は、閲覧器が既定値へ丸めて再生する。そのまま写すと WebP では瞬いて見えるため、同じ丸めを行う。
_MIN_FRAME_DURATION_MS: Final = 20
_DEFAULT_FRAME_DURATION_MS: Final = 100


class TranscodeError(Exception):
    """画像として復号できない、大きすぎる、WebP へ符号化できないなど、変換が成立しなかった。"""


@dataclass(frozen=True)
class EncodedImage:
    """変換の結果。content_hash は変換後の orig の SHA-256 であり、変換前の原本のハッシュではない。

    配信するバイト列そのもののハッシュにすることで、URL が実際に配信される内容を指す。原本のハッシュにすると、符号化器の版が変わって出力が変わっても URL が変わらず、内容アドレスとして成立しなくなる(永久キャッシュを許している以上、これは古い画像が二度と更新されないことを意味する)。
    """

    content_hash: str
    width: int
    height: int
    byte_size: int
    is_animated: bool
    is_lossless: bool
    # 'orig' は必須。アニメーションは 'orig' のみ。原寸より広い変種は含まない。
    variants: dict[str, bytes]


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in _ALPHA_MODES or 'transparency' in image.info


def _strip_metadata(image: Image.Image) -> Image.Image:
    """付随する情報を全て落とす。

    Pillow は convert や resize で info を引き継ぎ、WebP の保存時に im.info の exif をそのまま書き出す。ここで空にしておかないと、再符号化しても EXIF だけが原本から生き残る。
    """
    image.info = {}
    return image


def _to_target_mode(image: Image.Image) -> Image.Image:
    """WebP へ書ける mode へ変換する。透過は非可逆でも保つ(WebP はアルファを別途可逆で持てる)。"""
    converted = image.convert('RGBA' if _has_alpha(image) else 'RGB')
    return _strip_metadata(converted)


def _save_webp(image: Image.Image, *, lossless: bool, method: int, **extra: Any) -> bytes:
    buffer = io.BytesIO()
    try:
        image.save(buffer, format='WEBP', lossless=lossless, quality=WEBP_QUALITY, method=method, **extra)
    except Exception as exc:
        raise TranscodeError(f'WebP へ変換できませんでした: {exc}') from exc
    return buffer.getvalue()


def _check_pixels(width: int, height: int, frames: int, max_pixels: int) -> None:
    """復号の前に画素数を検査する。

    Pillow の MAX_IMAGE_PIXELS は上限の 2 倍を超えて初めて例外にし、その間は警告を出すだけである(警告は既定で握り潰される)。上限をそのまま強制するのはこちらの検査であり、MAX_IMAGE_PIXELS の設定は Pillow の内部で遅れて復号される経路に対する二重の備えに過ぎない。
    """
    if width <= 0 or height <= 0:
        raise TranscodeError(f'画像の寸法が不正です: {width}x{height}')
    pixels = width * height
    if pixels > max_pixels:
        raise TranscodeError(f'画像の画素数({pixels})が上限({max_pixels})を超えています。')
    # アニメーションで実際に復号される画素数はフレーム数の分だけ増える。1 フレームだけを見て通すと、小さなフレームを大量に並べた爆弾が抜ける。
    if pixels * frames > max_pixels:
        raise TranscodeError(f'アニメーションの総画素数({pixels} × {frames} フレーム)が上限({max_pixels})を超えています。')


def _check_dimensions(width: int, height: int) -> None:
    if width > WEBP_MAX_DIMENSION or height > WEBP_MAX_DIMENSION:
        raise TranscodeError(f'画像の辺が WebP の上限({WEBP_MAX_DIMENSION} ピクセル)を超えています: {width}x{height}')


def _frame_count(image: Image.Image) -> int:
    if not getattr(image, 'is_animated', False):
        return 1
    return max(1, int(getattr(image, 'n_frames', 1)))


def _encode_still(base: Image.Image, *, lossless: bool) -> dict[str, bytes]:
    """原寸と、原寸より狭い変種を符号化する。"""
    variants = {'orig': _save_webp(base, lossless=lossless, method=_METHOD_STILL)}
    for name, target_width in VARIANT_WIDTHS.items():
        # 原寸以上の幅の変種は作らない。引き伸ばしても情報は増えず、原寸より重いファイルができるだけになる。
        if base.width <= target_width:
            continue
        target_height = max(1, round(base.height * target_width / base.width))
        resized = base.resize((target_width, target_height), Image.Resampling.LANCZOS)
        variants[name] = _save_webp(resized, lossless=lossless, method=_METHOD_STILL)
    return variants


def _frame_duration(frame: Image.Image) -> int:
    raw = frame.info.get('duration', 0)
    try:
        duration = int(raw)
    except (TypeError, ValueError):
        duration = 0
    if duration < _MIN_FRAME_DURATION_MS:
        return _DEFAULT_FRAME_DURATION_MS
    return duration


def _encode_animation(source: Image.Image, *, lossless: bool) -> bytes:
    """アニメーションをアニメーション WebP へ符号化する。変種は作らない(縮小版のアニメーションに需要が無く、フレーム数だけ変換の費用が掛かる)。"""
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(source):
        durations.append(_frame_duration(frame))
        frames.append(_to_target_mode(frame))
    if not frames:
        raise TranscodeError('アニメーションのフレームがありません。')
    # GIF の loop は「繰り返す回数」で 0 が無限を意味する。指定が無い GIF は 1 回だけ再生されるのが本来の挙動であるため、無限(0)へ寄せない。
    raw_loop = source.info.get('loop')
    loop = 1 if raw_loop is None else max(0, int(raw_loop))
    return _save_webp(
        frames[0],
        lossless=lossless,
        method=_METHOD_ANIMATED,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=loop,
    )


def encode_to_webp(data: bytes, *, max_pixels: int, lossless: bool = False) -> EncodedImage:
    """バイト列を復号して WebP へ再符号化し、原寸と変種を返す。復号・符号化できない場合は TranscodeError。

    CPU を占有するため、要求を処理する最中に呼ぶ場合は別スレッドへ回すこと。
    """
    if not data:
        raise TranscodeError('画像が空です。')
    # 復号の上限を Pillow 側にも伝える。この代入は設定由来の同じ値で上書きされ続けるため、並行して呼ばれても値は変わらない。
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        source = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise TranscodeError(f'画像の画素数が上限({max_pixels})を超えています。') from exc
    except Exception as exc:
        raise TranscodeError('画像として復号できません。') from exc
    try:
        with source:
            width, height = source.size
            frames = _frame_count(source)
            _check_pixels(width, height, frames, max_pixels)
            _check_dimensions(width, height)
            is_animated = frames > 1
            if is_animated:
                variants = {'orig': _encode_animation(source, lossless=lossless)}
            else:
                variants = _encode_still(_to_target_mode(source), lossless=lossless)
    except Image.DecompressionBombError as exc:
        raise TranscodeError(f'画像の画素数が上限({max_pixels})を超えています。') from exc
    except TranscodeError:
        raise
    except Exception as exc:
        raise TranscodeError('画像を変換できませんでした。') from exc
    original = variants['orig']
    return EncodedImage(
        content_hash=hashlib.sha256(original).hexdigest(),
        width=width,
        height=height,
        byte_size=len(original),
        is_animated=is_animated,
        is_lossless=lossless,
        variants=variants,
    )
