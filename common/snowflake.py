"""64bit の ID(Discord のスノーフレークおよび DB の BIGINT UNSIGNED)の正規化。

JSON へ ID を出すときは必ず文字列にする。JavaScript の Number は 2^53 までしか整数を正確に表せず、スノーフレークはその範囲を優に超えるため、数値のまま渡すと下位の桁が黙って書き換わる。壊れた ID はエラーにならず「存在しない記事」「別のユーザー」として振る舞うため、発覚が遅れる。設定ファイルで ID を文字列として書かせているのも同じ理由である(YAML の整数として書くと、値を扱う経路のどこかで浮動小数点数へ落ちる余地が残る)。
"""

from __future__ import annotations

from typing import Final

# Discord のスノーフレークも DB の BIGINT UNSIGNED も符号なし 64bit に収まる。
MIN_SNOWFLAKE: Final = 0
MAX_SNOWFLAKE: Final = 2**64 - 1


def to_snowflake(value: str | int) -> int:
    """文字列または整数を 64bit の ID へ変換する。解釈できない値・範囲外の値は ValueError。

    真偽値は int の派生であるため素通りしてしまう(True が ID の 1 になる)。明示的に拒否する。文字列は ASCII の数字のみを受ける。符号や全角数字を int() に任せると、'-1' や '１２３' のような値が静かに通ってしまう。
    """
    if isinstance(value, bool):
        raise ValueError(f'ID に真偽値は使えません: {value!r}')
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError('ID が空です。')
        if not (text.isascii() and text.isdigit()):
            raise ValueError(f'ID は 10 進数の数字のみで表記してください: {value!r}')
        candidate = int(text)
    else:
        raise ValueError(f'ID は文字列または整数で指定してください(実際は {type(value).__name__}): {value!r}')
    if not MIN_SNOWFLAKE <= candidate <= MAX_SNOWFLAKE:
        raise ValueError(f'ID が符号なし 64bit の範囲({MIN_SNOWFLAKE}〜{MAX_SNOWFLAKE})を外れています: {candidate}')
    return candidate


def snowflake_str(value: int | None) -> str | None:
    """ID を JSON へ出すための文字列にする。None はそのまま None を返す。"""
    if value is None:
        return None
    return str(to_snowflake(value))
