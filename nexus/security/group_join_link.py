"""Encode/parse the ``nxg://join`` link format.

A join link is a single opaque blob users hand around:

    nxg://join#<base64url(JSON)>

JSON payload:

    {
      "r": ["https://relay-a.example", "https://relay-b.example"],
      "a": "host:port",
      "n": "<admin_node_uuid>",
      "k": "<relay_grid_key>",
      "t": "<invite_token>",
      "g": "<group_id>",
      "v": 1
    }

The ``a`` field is the founder's direct ``host:port``; ``n`` is the
admin's node UUID so the join request can be routed over the relay
 when the admin is behind NAT and ``a`` is unreachable.
``k`` is the founder's relay grid_key — needed to open a
transient WS to a self-hosted relay whose grid_key the joiner doesn't
already have in their settings. The link is already a bearer
credential, so the grid_key is no more sensitive than the invite
token: holding it lets you talk to the relay, but you still need a
valid ``t`` to actually join the group.

Why fragment (``#``)-encoded base64url:

* Fragments do not traverse to the server — protects against browser
  history / referer / log capture.
* Single opaque blob — users can't share a fragment of the secret by
  accident.
* JSON inside keeps it forward-compatible (signature, expiry, etc. can
  be added later).
* The relay URL list is embedded so the joiner immediately has multi-
  relay failover (only attempts the first; fans out).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass


SCHEME = "nxg://join#"
CURRENT_VERSION = 1
SIGNED_VERSION = 2


@dataclass(frozen=True)
class JoinLink:
    relay_urls: tuple[str, ...]
    admin_address: str
    invite_token: str
    group_id: str
    admin_node_id: str = ""
    grid_key: str = ""
    version: int = CURRENT_VERSION
    # Signed envelope ("inv" field) when ``version >= 2``.
    # When present, the join handler validates this against the
    # founder's pubkey and enforces ``max_uses`` + ``max_members``
    # without needing the legacy bearer ``invite_token``.
    signed_invite_hex: str = ""


def encode_join_link(
    *,
    relay_urls: list[str] | tuple[str, ...],
    admin_address: str,
    invite_token: str,
    group_id: str,
    admin_node_id: str = "",
    grid_key: str = "",
    signed_invite_hex: str = "",
) -> str:
    """Pack the bundle into ``nxg://join#<base64url(JSON)>``.

    when ``signed_invite_hex`` is provided the link is emitted
    at ``v=2`` and the ``k`` (grid_key) field is dropped — the joiner
    no longer needs the transport credential because the signed envelope
    is the new gate. Legacy callers (/31) that don't pass it
    still get a v=1 link with grid_key.
    """
    if not invite_token and not signed_invite_hex:
        raise ValueError("invite_token or signed_invite_hex must be provided")
    if not group_id:
        raise ValueError("group_id must be non-empty")
    if signed_invite_hex:
        payload = {
            "r": [u for u in relay_urls if u],
            "a": admin_address or "",
            "n": admin_node_id or "",
            "t": invite_token or "",
            "g": group_id,
            "inv": signed_invite_hex,
            "v": SIGNED_VERSION,
        }
    else:
        payload = {
            "r": [u for u in relay_urls if u],
            "a": admin_address or "",
            "n": admin_node_id or "",
            "k": grid_key or "",
            "t": invite_token,
            "g": group_id,
            "v": CURRENT_VERSION,
        }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    blob = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return SCHEME + blob


def parse_join_link(link: str) -> JoinLink:
    """Reverse of :func:`encode_join_link`. Raises ``ValueError`` on malformed input."""
    link = (link or "").strip()
    if not link:
        raise ValueError("empty join link")
    if not link.startswith(SCHEME):
        raise ValueError(f"join link must start with {SCHEME!r}")
    blob = link[len(SCHEME):]
    if not blob:
        raise ValueError("join link has no payload")
    # Re-pad for urlsafe_b64decode.
    pad_len = (-len(blob)) % 4
    try:
        raw = base64.urlsafe_b64decode(blob + ("=" * pad_len))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"join link payload is not valid base64url: {exc}") from exc
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"join link payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("join link payload must be a JSON object")
    relay_urls = payload.get("r") or []
    if not isinstance(relay_urls, list):
        raise ValueError("join link 'r' must be a list of strings")
    invite_token = str(payload.get("t") or "")
    group_id = str(payload.get("g") or "")
    signed_invite_hex = str(payload.get("inv") or "")
    # v=2 carries the signed envelope; v=1 carries the legacy bearer
    # invite_token. Either must be present.
    if not group_id or (not invite_token and not signed_invite_hex):
        raise ValueError("join link missing required fields 'g' / 't' / 'inv'")
    admin_address = str(payload.get("a") or "")
    admin_node_id = str(payload.get("n") or "")
    grid_key = str(payload.get("k") or "")
    version = int(payload.get("v") or 1)
    return JoinLink(
        relay_urls=tuple(str(u) for u in relay_urls if u),
        admin_address=admin_address,
        invite_token=invite_token,
        group_id=group_id,
        admin_node_id=admin_node_id,
        grid_key=grid_key,
        version=version,
        signed_invite_hex=signed_invite_hex,
    )


__all__ = [
    "SCHEME", "CURRENT_VERSION", "SIGNED_VERSION",
    "JoinLink", "encode_join_link", "parse_join_link",
]
