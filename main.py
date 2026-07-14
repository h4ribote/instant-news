#!/usr/bin/env python3
"""instant-news server launcher.

Usage:
    python main.py -p 8000
"""

from __future__ import annotations

import argparse
import uvicorn

# import fastapi app from server 
# from server... import app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py"
    )
    parser.add_argument("-p", "--port", type=int, default=8000, help="port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="host/interface to bind")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(f"Serving on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
