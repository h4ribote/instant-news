"""Decode the received bytes and re-encode them as WebP.

Never store the received bytes as they arrived. Decoding and re-encoding is the single most effective measure on this path.

- Polyglots (a file that is valid GIF and valid HTML and valid JavaScript at once) break. The re-encoded output consists solely of bytes written by our own encoder; not one byte of the input survives.
- EXIF is dropped. Capture coordinates and device identifiers sit embedded in images without their owner ever intending it.
- We choose the Content-Type we serve, so the format the remote claimed no longer has to be trusted.

Byte counts are no defence against a decompression bomb (a few-KB PNG that expands into tens of GB of pixels): whatever slips past the byte cap eats all memory the moment it is decoded. Cap on pixel count instead, and check before decoding.

Drop all metadata, not just EXIF. EXIF carries location, and XMP can carry the same. ICC profiles hold no secrets, but leaving the original profile (a CMYK one, say) attached to data whose mode has been converted leaves a profile that no longer describes the data, and colours break. Dropping everything and treating the result as sRGB breaks least for this use.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any, Final

from PIL import Image, ImageSequence

# Width per variant. store.VARIANTS holds the list of names; 'orig' is the original size, so it does not appear here.
VARIANT_WIDTHS: Final[dict[str, int]] = {'1200w': 1200, '400w': 400}

# Lossy quality. When lossless, Pillow reads this value as compression effort instead — only its meaning changes, which is no reason to leave it unset.
WEBP_QUALITY: Final = 90
# Encoder effort (0-6). Stills take the best. Animation pays it on every frame, so keep it at a value that does not hold up an article save.
_METHOD_STILL: Final = 6
_METHOD_ANIMATED: Final = 4
# Max side length WebP can represent. An image larger than this cannot be saved as WebP at all.
WEBP_MAX_DIMENSION: Final = 16383
# Modes carrying alpha. P-mode transparency shows up as a transparency key in info, so mode alone cannot decide.
_ALPHA_MODES: Final = frozenset({'RGBA', 'LA', 'PA', 'RGBa', 'La'})
# Browsers round a GIF frame shorter than this up to their own default on playback. Copying the raw duration through would leave the WebP visibly flickering, so apply the same rounding here.
_MIN_FRAME_DURATION_MS: Final = 20
_DEFAULT_FRAME_DURATION_MS: Final = 100


class TranscodeError(Exception):
    """The conversion did not go through: not decodable as an image, too large, not encodable as WebP, and so on."""


@dataclass(frozen=True)
class EncodedImage:
    """The result of a conversion. content_hash is the SHA-256 of the post-conversion 'orig', not a hash of the pre-conversion original file.

    Hashing the served bytes themselves makes the URL name the content actually served. Hashing the original file would leave the URL unchanged when a new encoder version changes the output, which stops it being content-addressed — and since permanent caching is allowed, that means an old image would never be updated again.
    """

    content_hash: str
    width: int
    height: int
    byte_size: int
    is_animated: bool
    is_lossless: bool
    # 'orig' is mandatory. An animation has 'orig' only. No variant wider than the original size is included.
    variants: dict[str, bytes]


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in _ALPHA_MODES or 'transparency' in image.info


def _strip_metadata(image: Image.Image) -> Image.Image:
    """Drop every piece of attached metadata.

    Pillow carries info over through convert and resize, and writes im.info's exif straight out when saving WebP. Without emptying it here, EXIF alone survives the re-encode from the original file.
    """
    image.info = {}
    return image


def _to_target_mode(image: Image.Image) -> Image.Image:
    """Convert to a mode WebP can write. Transparency is kept even when lossy — WebP can hold alpha losslessly on the side."""
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
    """Check the pixel count before decoding.

    Pillow's MAX_IMAGE_PIXELS only raises once twice the limit is exceeded, and merely warns in between (warnings are swallowed by default). This check is what enforces the limit as written; setting MAX_IMAGE_PIXELS is no more than a second line of defence for paths that decode late inside Pillow.
    """
    if width <= 0 or height <= 0:
        raise TranscodeError(f'画像の寸法が不正です: {width}x{height}')
    pixels = width * height
    if pixels > max_pixels:
        raise TranscodeError(f'画像の画素数({pixels})が上限({max_pixels})を超えています。')
    # An animation actually decodes frame-count times as many pixels. Passing it on the strength of a single frame lets through a bomb built from a huge number of small frames.
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
    """Encode the original size, plus every variant narrower than it."""
    variants = {'orig': _save_webp(base, lossless=lossless, method=_METHOD_STILL)}
    for name, target_width in VARIANT_WIDTHS.items():
        # No variant at or above the original width. Upscaling adds no information and only produces a file heavier than the original size.
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
    """Encode an animation as animated WebP. No variants are produced: nobody wants a downscaled animation, and it would cost one conversion per frame."""
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(source):
        durations.append(_frame_duration(frame))
        frames.append(_to_target_mode(frame))
    if not frames:
        raise TranscodeError('アニメーションのフレームがありません。')
    # A GIF's loop is a repeat count in which 0 means infinite. A GIF that does not specify one is meant to play exactly once, so do not round it towards infinite (0).
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
    """Decode the bytes, re-encode them as WebP, and return the original size and its variants. TranscodeError if they cannot be decoded or encoded.

    This monopolises the CPU, so hand it to a separate thread when calling it in the middle of handling a request.
    """
    if not data:
        raise TranscodeError('画像が空です。')
    # Tell Pillow the decode limit as well. This assignment keeps being overwritten with the same settings-derived value, so the value does not change even under concurrent calls.
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
