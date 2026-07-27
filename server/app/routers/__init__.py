"""HTTP routes. Each router declares the full URL it serves and main.py includes it without adding a prefix, because a single concern can span two trees: ads have routes under both /api/ and /ads/, which no include-side prefix can express."""

from __future__ import annotations
