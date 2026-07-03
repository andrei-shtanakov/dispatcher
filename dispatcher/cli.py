"""Command-line entry point: `dispatcher serve`."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from dispatcher.core.discovery import load_config
from dispatcher.server.app import create_app


def build_parser() -> argparse.ArgumentParser:
    """CLI argument parser (separate for testability)."""
    parser = argparse.ArgumentParser(prog="dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the dashboard server")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--config", type=Path, default=None)
    return parser


def main() -> None:
    """Entry point for the `dispatcher` console script."""
    args = build_parser().parse_args()
    config = load_config(args.config)
    port = args.port if args.port is not None else config.port
    uvicorn.run(create_app(config), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
