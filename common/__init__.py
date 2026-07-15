"""server と bot が共有する層。設定の読み込み(config)・データベース接続(db)・ID の正規化(snowflake)を提供する。両方の Docker イメージへそのままコピーされるため、この層は FastAPI にも discord.py にも依存してはならない。"""

from __future__ import annotations
