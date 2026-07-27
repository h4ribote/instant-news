"""Normalization of 64-bit IDs (Discord snowflakes and the DB's BIGINT UNSIGNED).

IDs always go into JSON as strings. JavaScript's Number is exact only up to 2^53 and a snowflake is well past that, so an ID passed as a number has its low digits silently rewritten. A corrupted ID raises nothing: it just behaves as "no such article" or "a different user", so the breakage surfaces late. Config files spell IDs as strings for the same reason — written as a YAML integer, the value can still fall into a float somewhere along the path that handles it.
"""

from __future__ import annotations

from typing import Final

# Both a Discord snowflake and the DB's BIGINT UNSIGNED fit in unsigned 64-bit.
MIN_SNOWFLAKE: Final = 0
MAX_SNOWFLAKE: Final = 2**64 - 1


def to_snowflake(value: str | int) -> int:
    """Convert a string or integer to a 64-bit ID. Values that cannot be parsed, and values out of range, raise ValueError.

    bool derives from int and would otherwise pass straight through, turning True into the ID 1, so it is rejected explicitly. Strings take ASCII digits only: leaving signs and full-width digits to int() would silently admit values like '-1' or a full-width '123'.
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
    """Render an ID as the string that goes into JSON. None passes through as None."""
    if value is None:
        return None
    return str(to_snowflake(value))
