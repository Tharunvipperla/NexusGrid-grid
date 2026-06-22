"""D2 — OpenAPI helpers for the SDK/CLI.

Pure functions over a parsed OpenAPI dict (the node serves it at
``/openapi.json``). Kept free of network/IO so they're trivially testable; the
CLI fetches the live spec and feeds it here, so the command surface is always
in sync with the running node.
"""

from __future__ import annotations

_METHODS = ("get", "post", "put", "delete", "patch")


def list_operations(
    spec: dict, tag: str = "", grep: str = ""
) -> list[dict]:
    """Flatten an OpenAPI spec's paths into a sorted operation list.

    Each entry: ``{method, path, summary, tag, operation_id, params}``.
    Optional ``tag`` (exact, case-insensitive) and ``grep`` (substring over
    path/summary/tag/operation_id) narrow the result.
    """
    ops: list[dict] = []
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in _METHODS or not isinstance(op, dict):
                continue
            ops.append({
                "method": method.upper(),
                "path": path,
                "summary": op.get("summary", "") or "",
                "tag": (op.get("tags") or ["Other"])[0],
                "operation_id": op.get("operationId", "") or "",
                "params": [p.get("name") for p in (op.get("parameters") or [])
                           if isinstance(p, dict)],
            })
    if tag:
        t = tag.lower()
        ops = [o for o in ops if o["tag"].lower() == t]
    if grep:
        g = grep.lower()
        ops = [o for o in ops if g in
               f"{o['path']} {o['summary']} {o['tag']} {o['operation_id']}".lower()]
    ops.sort(key=lambda o: (o["tag"], o["path"], o["method"]))
    return ops


__all__ = ["list_operations"]
