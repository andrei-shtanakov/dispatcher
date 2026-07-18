"""Command-line entry point: `dispatcher serve|tui|mcp|publish-snapshot`."""

from __future__ import annotations

import argparse
from pathlib import Path

from dispatcher.core.discovery import load_config


def build_parser() -> argparse.ArgumentParser:
    """CLI argument parser (separate for testability)."""
    parser = argparse.ArgumentParser(prog="dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the dashboard server")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--config", type=Path, default=None)
    tui = sub.add_parser("tui", help="run the terminal dashboard")
    tui.add_argument("--config", type=Path, default=None)
    mcp = sub.add_parser(
        "mcp",
        help="run the MCP stdio server over the read API (for agents)",
    )
    mcp.add_argument("--config", type=Path, default=None)
    publish = sub.add_parser(
        "publish-snapshot",
        help="publish this host's sync snapshot to the KB (derived/snapshots/)",
    )
    publish.add_argument("--config", type=Path, default=None)
    publish.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="polyrepo root (default: first configured root that exists on disk)",
    )
    publish.add_argument(
        "--no-push",
        action="store_true",
        help="commit to the KB repo without pushing (local testing)",
    )
    return parser


def main() -> None:
    """Entry point for the `dispatcher` console script."""
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.command == "publish-snapshot":
        import sys

        from dispatcher.core.publish import PublishError, publish

        workspace = args.workspace or next(
            (root for root in config.roots if root.is_dir()), None
        )
        if workspace is None:
            print("no existing workspace root", file=sys.stderr)
            raise SystemExit(1)
        try:
            print(publish(workspace, push=not args.no_push))
        except PublishError as err:
            # non-zero is the cron-visibility contract (RK-03)
            print(f"publish failed: {err}", file=sys.stderr)
            raise SystemExit(1) from err
        return
    if args.command == "mcp":
        # Imported lazily: serve/tui should not pay fastmcp's import cost.
        from dispatcher.mcp_server import build_server

        build_server(config).run()
        return
    if args.command == "tui":
        # Imported lazily: `serve` should not pay textual's import cost.
        from dispatcher.tui.app import DispatcherApp

        DispatcherApp(config).run()
        return
    import uvicorn

    from dispatcher.server.app import create_app

    port = args.port if args.port is not None else config.port
    uvicorn.run(create_app(config), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
