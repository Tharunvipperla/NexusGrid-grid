"""Command-line interface for the NexusGrid node.

Kept minimal on purpose — this module only turns `sys.argv` into a plain argparse
Namespace. Settings that can be changed at runtime (via the UI or API) live in
`nexus.core.config`, not here.

Stub: real arg definitions are migrated from node_modified.py in Step 2.
"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nexus", description="NexusGrid node")
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    parser.add_argument("--peers", default="", help="comma-separated peer list (IP:PORT)")
    parser.add_argument("--relay", default="", help="relay WebSocket URL (wss://...)")
    parser.add_argument("--grid-key", default="nexus-beta-key", help="shared grid key")
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    parser.add_argument(
        "--data-dir",
        default="",
        help="where the node stores its data (DB, keys, caches). Default: a "
        "per-user app-data folder for packaged builds (e.g. %%LOCALAPPDATA%%/"
        "NexusGrid), or the source dir when run from source. Also settable via "
        "the NEXUS_DATA_DIR env var.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="don't auto-open the UI in the default browser on startup",
    )
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="bind plain HTTP instead of HTTPS (TLS is on by default)",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)
