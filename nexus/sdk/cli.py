"""D2 — OpenAPI-driven CLI: ``python -m nexus.sdk``.

* ``ops``  — list the node's API operations (read live from /openapi.json).
* ``call`` — invoke any endpoint and print the JSON response.

The token is read from ``.nexus_local_token`` unless ``--token`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys

from nexus.sdk.client import NexusClient
from nexus.sdk.openapi import list_operations


def _parse_query(pairs: list[str]) -> dict | None:
    out: dict[str, str] = {}
    for kv in pairs or []:
        if "=" not in kv:
            raise SystemExit(f"--query expects k=v, got: {kv}")
        k, v = kv.split("=", 1)
        out[k] = v
    return out or None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nexus-sdk",
        description="OpenAPI-driven CLI for a NexusGrid node's local API.")
    p.add_argument("--base", default="https://127.0.0.1:8000",
                   help="node base URL (default: %(default)s)")
    p.add_argument("--token", default=None,
                   help="local API token (default: read .nexus_local_token)")
    sub = p.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("ops", help="list API operations from the live spec")
    po.add_argument("--tag", default="", help="filter by exact tag")
    po.add_argument("--grep", default="", help="substring filter")

    pc = sub.add_parser("call", help="call an endpoint and print the response")
    pc.add_argument("method", help="HTTP method, e.g. GET")
    pc.add_argument("path", help="path, e.g. /local/network")
    pc.add_argument("--query", action="append", default=[],
                    help="query param k=v (repeatable)")
    pc.add_argument("--data", default="", help="JSON request body")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    client = NexusClient.from_local(base_url=args.base, token=args.token)

    if args.cmd == "ops":
        rows = list_operations(client.openapi(), tag=args.tag, grep=args.grep)
        cur = None
        for o in rows:
            if o["tag"] != cur:
                cur = o["tag"]
                print(f"\n# {cur}")
            print(f"  {o['method']:6} {o['path']}"
                  + (f"  - {o['summary']}" if o["summary"] else ""))
        print(f"\n{len(rows)} operations", file=sys.stderr)
        return 0

    # call
    body = json.loads(args.data) if args.data else None
    out = client.request(args.method, args.path,
                         params=_parse_query(args.query), json=body)
    print(out if isinstance(out, str) else json.dumps(out, indent=2))
    return 0


__all__ = ["main", "build_parser"]
