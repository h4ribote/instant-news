"""Layer shared by server and bot: config loading (config), database connections (db), ID normalization (snowflake). Both Docker images copy this layer verbatim, so it must not depend on FastAPI or discord.py."""

from __future__ import annotations
