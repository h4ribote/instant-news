#!/usr/bin/env python3
"""Print the HTTP API's OpenAPI document, the contract kept in docs/openapi.json.

    python -m server.openapi > docs/openapi.json

README's test section gives the same command run in the test container. The document is generated from the routes and models alone; the app is assembled but never started, so no database is needed. tests/test_openapi_contract.py fails whenever the generated document and docs/openapi.json differ, so a change to the API's shape is always an explicit change to that file.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .main import create_app


def render() -> str:
    """The document as it is written to docs/openapi.json: indented, keys in generation order, Japanese descriptions kept readable, ending in a newline."""
    document: dict[str, Any] = create_app().openapi()
    return json.dumps(document, ensure_ascii=False, indent=2) + '\n'


def main() -> int:
    sys.stdout.write(render())
    return 0


if __name__ == '__main__':
    sys.exit(main())
