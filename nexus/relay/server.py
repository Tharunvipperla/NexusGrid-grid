"""
Nexus Relay / Signaling Server
==============================
A lightweight WebSocket relay that enables Nexus nodes on different networks
to discover each other and exchange messages without direct connectivity.

Deploy on any host (e.g., Render free tier):
    uvicorn nexus.relay.server:app --host 0.0.0.0 --port 9000

Nodes connect with a shared grid key for authentication.
"""

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime
from typing import Dict, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("relay")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_list(name: str) -> list[str]:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


GRID_KEY = os.getenv("NEXUS_GRID_KEY", "nexus-beta-key").strip()

# Wave 41: code fingerprint advertised to every connecting client.
# Clients running against a group with a frozen fingerprint compare this
# to the group's expected value and disconnect on mismatch — the gate
# that stops a rogue host from substituting different code under a
# group's nose. Override the version via env so the same source can
# build different declared versions in a controlled rollout.
_RELAY_NEXUS_VERSION = os.getenv("NEXUS_RELAY_VERSION", "0.2.0")

def _compute_self_fingerprint() -> str:
    try:
        import hashlib
        with open(__file__, "rb") as f:
            body = f.read()
        digest = hashlib.sha256(
            b"nexus-relay-codeprint:"
            + _RELAY_NEXUS_VERSION.encode("utf-8")
            + b":"
            + body
        ).hexdigest()
        return digest[:32]
    except Exception:
        return ""

CODE_FINGERPRINT = _compute_self_fingerprint()

REQUIRE_NON_DEFAULT_KEY = _env_flag("NEXUS_RELAY_REQUIRE_NON_DEFAULT_KEY", False)
HEARTBEAT_TIMEOUT = _env_int("NEXUS_RELAY_HEARTBEAT_TIMEOUT", 30, 10, 300)
CLEANUP_INTERVAL = _env_int("NEXUS_RELAY_CLEANUP_INTERVAL", 10, 5, 60)
REGISTER_TIMEOUT = _env_int("NEXUS_RELAY_REGISTER_TIMEOUT", 10, 3, 60)
MAX_NODE_ID_LEN = _env_int("NEXUS_RELAY_MAX_NODE_ID_LEN", 128, 16, 256)
MAX_DISPLAY_NAME_LEN = _env_int("NEXUS_RELAY_MAX_DISPLAY_NAME_LEN", 80, 10, 200)
MAX_PORT = 65535
MAX_CAPABILITIES_BYTES = _env_int("NEXUS_RELAY_MAX_CAPS_BYTES", 16384, 512, 262144)
MAX_PAYLOAD_BYTES = _env_int("NEXUS_RELAY_MAX_PAYLOAD_BYTES", 262144, 1024, 1048576)
MAX_PEERS_RETURNED = _env_int("NEXUS_RELAY_MAX_PEERS", 5000, 10, 20000)
ALLOWED_ORIGINS = _env_list("NEXUS_RELAY_ALLOWED_ORIGINS")
CORS_ORIGINS = _env_list("NEXUS_RELAY_CORS_ORIGINS")

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

app = FastAPI(title="Nexus Relay Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["Authorization", "Content-Type"],
)


RATE_LIMIT_WINDOW = _env_int("NEXUS_RELAY_RATE_LIMIT_WINDOW", 10, 5, 60)
RATE_LIMIT_MAX_MSGS = _env_int("NEXUS_RELAY_RATE_LIMIT_MAX_MSGS", 60, 10, 500)
RATE_LIMIT_MAX_BROADCAST = _env_int("NEXUS_RELAY_RATE_LIMIT_MAX_BROADCAST", 5, 1, 50)
RATE_LIMIT_DISCONNECT_MULT = 3  # disconnect if exceeded by this multiplier


class ConnectedNode:
    __slots__ = (
        "node_id",
        "display_name",
        "port",
        "capabilities",
        "ws",
        "last_heartbeat",
        "registered_at",
        "hide_profile",
        "msg_timestamps",
        "broadcast_timestamps",
        "region",
        "status",
        # Wave 40: per-context grid_keys this node is subscribed to. The
        # relay buckets all broadcast traffic by grid_key, so a node only
        # sees frames for contexts it's a member of.
        "grid_keys",
    )

    def __init__(
        self,
        node_id: str,
        ws: WebSocket,
        display_name: str = "",
        port: int = 8000,
        capabilities: Optional[dict] = None,
        hide_profile: bool = False,
        region: Optional[str] = None,
        grid_keys: Optional[set] = None,
    ):
        self.node_id = node_id
        self.ws = ws
        self.display_name = display_name
        self.port = port
        self.capabilities = capabilities or {}
        self.hide_profile = hide_profile
        self.region = region or ""
        self.status = "online"
        self.last_heartbeat = time.time()
        self.registered_at = time.time()
        self.msg_timestamps: list = []
        self.broadcast_timestamps: list = []
        self.grid_keys: set = set(grid_keys or set())


def _check_rate_limit(node: ConnectedNode, is_broadcast: bool = False) -> str:
    """Check if a node has exceeded its rate limit.
    Returns: '' if OK, 'limited' if soft limit, 'disconnect' if severe."""
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW

    node.msg_timestamps = [t for t in node.msg_timestamps if t > cutoff]
    node.msg_timestamps.append(now)

    if is_broadcast:
        node.broadcast_timestamps = [t for t in node.broadcast_timestamps if t > cutoff]
        node.broadcast_timestamps.append(now)
        if len(node.broadcast_timestamps) > RATE_LIMIT_MAX_BROADCAST * RATE_LIMIT_DISCONNECT_MULT:
            return "disconnect"
        if len(node.broadcast_timestamps) > RATE_LIMIT_MAX_BROADCAST:
            return "limited"

    if len(node.msg_timestamps) > RATE_LIMIT_MAX_MSGS * RATE_LIMIT_DISCONNECT_MULT:
        return "disconnect"
    if len(node.msg_timestamps) > RATE_LIMIT_MAX_MSGS:
        return "limited"

    return ""


connected_nodes: Dict[str, ConnectedNode] = {}
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Wave 36.F.2: pair-invite handshake (no grid_key required)
# ---------------------------------------------------------------------------
#
# A new WebSocket endpoint ``/relay/pair_invite/{issuer_node_id}`` lets a
# stranger present a signed pair-invite token (issued by the target's owner
# offline, e.g. via a publicly-shared nxg://pair#... link). The relay verifies
# the Ed25519 signature, enforces single-use redemption, and forwards a single
# ``pair_invite_probe`` to the issuer's already-registered WS connection. The
# issuer's client replies with ``pair_reply`` (accept or reject), which the
# relay forwards back and then closes the probe's WS.
#
# Trust model:
#   * No grid_key on this endpoint — possession of a valid signed invite is
#     the gate, and the relay verifies the signature without needing the
#     issuer's pubkey out-of-band (it's embedded in the signed payload).
#   * The connection's only allowed message is ``pair_invite_probe``; after
#     forwarding, the WS only awaits the reply (or times out).
#   * Replay protection: each ``invite_id`` is consumed on first probe; cache
#     is capped and evicts oldest when full.

PAIR_INVITE_PROBE_TIMEOUT = _env_int(
    "NEXUS_RELAY_PAIR_PROBE_TIMEOUT", 10, 3, 60
)
PAIR_INVITE_REPLY_TIMEOUT = _env_int(
    "NEXUS_RELAY_PAIR_REPLY_TIMEOUT", 60, 10, 300
)
PAIR_REDEMPTION_CACHE_MAX = _env_int(
    "NEXUS_RELAY_PAIR_REDEMPTION_CACHE_MAX", 10000, 100, 1000000
)

# invite_id -> (issuer_pubkey, redeemed_at_epoch). Capped at
# PAIR_REDEMPTION_CACHE_MAX; oldest evicted when full. Lost on restart —
# acceptable trade-off since invites are time-bounded and rare.
_pair_redemptions: Dict[str, tuple] = {}
_pair_redemption_lock = asyncio.Lock()

# transient_id -> probe's WebSocket. Set when the probe is forwarded to the
# issuer; popped when the issuer's ``pair_reply`` arrives (or the probe times
# out / disconnects).
_pair_probe_pending: Dict[str, WebSocket] = {}
_pair_probe_lock = asyncio.Lock()


def _b64url_decode(text: str) -> bytes:
    """URL-safe base64 decode with restored padding."""
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _verify_pair_invite(inv_b64: str, *, now: float) -> Optional[dict]:
    """Verify a signed pair-invite blob from a ``pair_invite_probe`` frame.

    Returns the payload dict on success, ``None`` on any failure. Checks:
      * base64url + JSON parse
      * Ed25519 signature against embedded ``issuer_pubkey``
      * ``expires_at`` is in the future (relative to *now*)
      * Required fields present and of expected shape.

    The issuer's client re-verifies before showing the request in their UI,
    so this server-side check is a *spam gate* rather than the authoritative
    source — both layers must pass.
    """
    try:
        envelope = json.loads(_b64url_decode(inv_b64).decode("utf-8"))
        payload = envelope["payload"]
        sig_hex = envelope["signature"]
        issuer_pubkey = str(payload["issuer_pubkey"])
        expires_at = str(payload["expires_at"])
        invite_id = str(payload["invite_id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None

    if len(issuer_pubkey) != 64 or len(sig_hex) != 128 or not invite_id:
        return None

    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(issuer_pubkey))
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        pub.verify(bytes.fromhex(sig_hex), canonical)
    except (InvalidSignature, ValueError):
        return None

    try:
        exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if exp_dt.timestamp() <= now:
            return None
    except ValueError:
        return None

    return payload


async def _claim_invite(invite_id: str, issuer_pubkey: str, now: float) -> bool:
    """Atomically mark ``invite_id`` as redeemed. Returns True on first claim,
    False if already redeemed."""
    async with _pair_redemption_lock:
        if invite_id in _pair_redemptions:
            return False
        _pair_redemptions[invite_id] = (issuer_pubkey, now)
        if len(_pair_redemptions) > PAIR_REDEMPTION_CACHE_MAX:
            # Evict oldest until back at cap. O(n log n) but only runs at the
            # eviction threshold; acceptable for the realistic cache size.
            ordered = sorted(
                _pair_redemptions.items(), key=lambda kv: kv[1][1]
            )
            drop = len(_pair_redemptions) - PAIR_REDEMPTION_CACHE_MAX
            for k, _ in ordered[:drop]:
                _pair_redemptions.pop(k, None)
    return True


def _safe_compare_grid_key(candidate: str) -> bool:
    if not GRID_KEY:
        return False
    return hmac.compare_digest(str(candidate or ""), GRID_KEY)


# Wave 40: per-context grid_keys. Each entry is a 32-char hex digest
# derived from a stable context (group_id or sorted pair pubkeys). The
# relay is a dumb router that buckets broadcasts by grid_key — end-to-end
# encryption inside the bucket is the real security layer.
_GRID_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_GRID_KEYS_PER_NODE = _env_int(
    "NEXUS_RELAY_MAX_GRID_KEYS_PER_NODE", 256, 1, 4096
)


def _normalize_grid_keys(raw) -> Optional[set]:
    """Parse an inbound ``grid_keys`` list into a validated set.

    Returns ``None`` if the input is malformed (caller closes the WS).
    Returns ``set()`` for an explicitly empty list — that's a valid
    intermediate state (no contexts) and shouldn't block the connection.
    """
    if not isinstance(raw, list):
        return None
    if len(raw) > MAX_GRID_KEYS_PER_NODE:
        return None
    out: set = set()
    for item in raw:
        s = str(item or "").strip().lower()
        if not _GRID_KEY_RE.fullmatch(s):
            return None
        out.add(s)
    return out


def _validate_node_id(node_id: str) -> bool:
    if not node_id or len(node_id) > MAX_NODE_ID_LEN:
        return False
    return bool(_NODE_ID_RE.fullmatch(node_id))


def _normalize_display_name(value: str) -> str:
    clean = str(value or "").strip()
    return clean[:MAX_DISPLAY_NAME_LEN]


def _normalize_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = 8000
    if 1 <= port <= MAX_PORT:
        return port
    return 8000


def _normalize_capabilities(value) -> dict:
    if not isinstance(value, dict):
        return {}
    try:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    except Exception:
        return {}
    if len(encoded) > MAX_CAPABILITIES_BYTES:
        return {}
    return value


def _payload_size_ok(payload) -> bool:
    try:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except Exception:
        return False
    return len(encoded) <= MAX_PAYLOAD_BYTES


def _origin_allowed(origin: Optional[str]) -> bool:
    if not ALLOWED_ORIGINS:
        return True
    if not origin:
        return True
    return origin in ALLOWED_ORIGINS


async def _safe_send_json(ws: WebSocket, message: dict) -> bool:
    try:
        await ws.send_text(json.dumps(message))
        return True
    except Exception:
        return False


async def _evict_stale_nodes():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time.time()
        stale = []
        async with _lock:
            for nid, node in list(connected_nodes.items()):
                if now - node.last_heartbeat > HEARTBEAT_TIMEOUT:
                    stale.append((nid, node))
            for nid, node in stale:
                logger.info("Evicting stale node: %s", nid)
                connected_nodes.pop(nid, None)
                try:
                    await node.ws.close(1001, "heartbeat timeout")
                except Exception:
                    pass
        if stale:
            await _broadcast_peer_list()


def _build_peer_list() -> list:
    peers = []
    for node in list(connected_nodes.values())[:MAX_PEERS_RETURNED]:
        peers.append(
            {
                "node_id": node.node_id,
                "display_name": node.display_name,
                "port": node.port,
                "capabilities": node.capabilities,
                "online_since": node.registered_at,
                "hide_profile": node.hide_profile,
                "status": node.status,
                "last_heartbeat": node.last_heartbeat,
                "region": node.region,
            }
        )
    return peers


def _build_presence_table() -> dict:
    table = {}
    for node in list(connected_nodes.values())[:MAX_PEERS_RETURNED]:
        table[node.node_id] = {
            "status": node.status,
            "last_heartbeat": node.last_heartbeat,
            "region": node.region,
        }
    return table


async def _broadcast_peer_list():
    peer_list = _build_peer_list()
    dead_nodes = []
    async with _lock:
        for nid, node in list(connected_nodes.items()):
            ok = await _safe_send_json(node.ws, {"type": "peer_list", "peers": peer_list})
            if not ok:
                dead_nodes.append(nid)
        for nid in dead_nodes:
            connected_nodes.pop(nid, None)


@app.on_event("startup")
async def startup():
    if not GRID_KEY:
        raise RuntimeError("NEXUS_GRID_KEY must be set.")
    if GRID_KEY == "nexus-beta-key":
        msg = "Relay is using the default grid key. Set NEXUS_GRID_KEY before production use."
        if REQUIRE_NON_DEFAULT_KEY:
            raise RuntimeError(msg)
        logger.warning(msg)
    asyncio.create_task(_evict_stale_nodes())
    logger.info("Relay server started.")


@app.get("/")
def root():
    return {
        "service": "Nexus Relay Server",
        "nodes_online": len(connected_nodes),
        "status": "running",
        "default_key_in_use": GRID_KEY == "nexus-beta-key",
    }


@app.get("/peers")
def get_peers(grid_key: str = Query(...)):
    if not _safe_compare_grid_key(grid_key):
        raise HTTPException(403, "Invalid grid key")
    return {"peers": _build_peer_list()}


@app.get("/relay/presence")
def get_presence(grid_key: str = Query(...)):
    if not _safe_compare_grid_key(grid_key):
        raise HTTPException(403, "Invalid grid key")
    return {"presence": _build_presence_table()}


@app.websocket("/relay/{node_id}")
async def relay_ws(websocket: WebSocket, node_id: str):
    origin = websocket.headers.get("origin")
    if not _validate_node_id(node_id):
        await websocket.close(1008, "invalid node id")
        return
    if not _origin_allowed(origin):
        await websocket.close(1008, "origin not allowed")
        return

    await websocket.accept()

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=REGISTER_TIMEOUT)
        reg = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError):
        await websocket.close(4001, "Expected registration message")
        return

    if reg.get("type") != "register":
        await websocket.close(4003, "Expected type=register")
        return
    # Wave 40: the optional shared ``GRID_KEY`` env still gates who can
    # connect to a private relay, but the per-context ``grid_keys`` field
    # is what the relay actually uses for routing.
    if GRID_KEY and not _safe_compare_grid_key(reg.get("grid_key")):
        await websocket.close(4003, "Invalid grid key")
        return
    grid_keys = _normalize_grid_keys(reg.get("grid_keys", []))
    if grid_keys is None:
        await websocket.close(
            4003, "grid_keys must be a list of 32-char hex digests",
        )
        return

    node = ConnectedNode(
        node_id=node_id,
        ws=websocket,
        display_name=_normalize_display_name(reg.get("display_name", "")),
        port=_normalize_port(reg.get("port", 8000)),
        capabilities=_normalize_capabilities(reg.get("capabilities", {})),
        hide_profile=bool(reg.get("hide_profile", False)),
        region=_normalize_display_name(reg.get("region", "")),
        grid_keys=grid_keys,
    )

    async with _lock:
        old = connected_nodes.pop(node_id, None)
        if old and old.ws:
            try:
                await old.ws.close(1001, "replaced by new connection")
            except Exception:
                pass
        connected_nodes[node_id] = node

    logger.info("Node registered: %s (%s)", node_id, node.display_name or "unnamed")
    await _safe_send_json(websocket, {"type": "peer_list", "peers": _build_peer_list()})
    await _broadcast_peer_list()
    # Wave 41: surface this relay's code fingerprint + version in the
    # register ack so clients verifying against a group's frozen
    # fingerprint can disconnect immediately on mismatch.
    await _safe_send_json(
        websocket,
        {
            "type": "registered",
            "node_id": node_id,
            "code_fingerprint": CODE_FINGERPRINT,
            "nexus_version": _RELAY_NEXUS_VERSION,
        },
    )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await _safe_send_json(websocket, {"type": "error", "message": "invalid json"})
                continue

            msg_type = str(data.get("type", "")).strip()

            # --- Rate limiting ---
            rl_result = _check_rate_limit(node, is_broadcast=(msg_type == "broadcast"))
            if rl_result == "disconnect":
                logger.warning("Rate limit severely exceeded by %s — disconnecting.", node_id)
                await _safe_send_json(websocket, {
                    "type": "error", "message": "rate limit exceeded — disconnected"
                })
                await websocket.close(4029, "rate limit exceeded")
                break
            elif rl_result == "limited":
                await _safe_send_json(websocket, {
                    "type": "error", "message": "rate limited — slow down"
                })
                continue

            if msg_type == "heartbeat":
                node.last_heartbeat = time.time()
                if node.status != "online":
                    node.status = "online"
                    asyncio.create_task(_broadcast_peer_list())
                await _safe_send_json(websocket, {"type": "heartbeat_ack"})

            elif msg_type == "bye":
                # Graceful shutdown notification — flip status BEFORE closing so the
                # peer_list rebroadcast in the finally clause carries the offline transition.
                node.status = "offline"
                logger.info("Node %s sent bye", node_id)
                break

            elif msg_type == "relay":
                target_id = str(data.get("target", "")).strip()
                payload = data.get("payload")
                if not _validate_node_id(target_id) or payload is None:
                    await _safe_send_json(
                        websocket,
                        {"type": "error", "message": "relay requires valid target and payload"},
                    )
                    continue
                if not _payload_size_ok(payload):
                    await _safe_send_json(
                        websocket,
                        {"type": "relay_failed", "target": target_id, "reason": "payload too large"},
                    )
                    continue

                async with _lock:
                    target_node = connected_nodes.get(target_id)

                if target_node:
                    ok = await _safe_send_json(
                        target_node.ws,
                        {"type": "relayed", "from": node_id, "payload": payload},
                    )
                    if ok:
                        await _safe_send_json(websocket, {"type": "relay_ack", "target": target_id})
                    else:
                        await _safe_send_json(
                            websocket,
                            {"type": "relay_failed", "target": target_id, "reason": "send failed"},
                        )
                else:
                    await _safe_send_json(
                        websocket,
                        {"type": "relay_failed", "target": target_id, "reason": "node not connected"},
                    )

            elif msg_type == "pair_reply":
                # Wave 36.F.2: issuer's accept/reject reply to a forwarded
                # pair_invite_probe. We route it to the parked probe WS by
                # transient_id and then close that side.
                transient_id = str(data.get("transient_id", "")).strip()
                decision = str(data.get("decision", "")).strip()
                if decision not in ("accept", "reject") or not transient_id:
                    await _safe_send_json(
                        websocket,
                        {"type": "error", "message": "pair_reply requires transient_id + decision"},
                    )
                    continue
                async with _pair_probe_lock:
                    probe_ws = _pair_probe_pending.pop(transient_id, None)
                if probe_ws is None:
                    # Probe timed out / disconnected; not actionable.
                    await _safe_send_json(
                        websocket,
                        {"type": "pair_reply_ack", "transient_id": transient_id, "delivered": False},
                    )
                    continue
                reply = {
                    "type": "pair_reply",
                    "decision": decision,
                    "issuer_pubkey": node_id,
                    "payload": data.get("payload", {}),
                }
                sent = await _safe_send_json(probe_ws, reply)
                try:
                    await probe_ws.close(1000, "pair handshake complete")
                except Exception:
                    pass
                await _safe_send_json(
                    websocket,
                    {"type": "pair_reply_ack", "transient_id": transient_id, "delivered": sent},
                )

            elif msg_type == "broadcast":
                payload = data.get("payload", {})
                # Wave 40: every broadcast must carry the ``grid_key`` of the
                # context it belongs to. The relay fans out only to other
                # subscribers holding that key — no cross-context leakage.
                broadcast_key = str(data.get("grid_key", "")).strip().lower()
                if not _GRID_KEY_RE.fullmatch(broadcast_key):
                    await _safe_send_json(
                        websocket,
                        {"type": "error", "message": "broadcast requires grid_key"},
                    )
                    continue
                if broadcast_key not in node.grid_keys:
                    await _safe_send_json(
                        websocket,
                        {"type": "error", "message": "sender not subscribed to that grid_key"},
                    )
                    continue
                if not _payload_size_ok(payload):
                    await _safe_send_json(
                        websocket,
                        {"type": "error", "message": "broadcast payload too large"},
                    )
                    continue
                dead_nodes = []
                async with _lock:
                    for nid, target_node in list(connected_nodes.items()):
                        if nid == node_id:
                            continue
                        if broadcast_key not in target_node.grid_keys:
                            continue
                        ok = await _safe_send_json(
                            target_node.ws,
                            {
                                "type": "relayed_broadcast",
                                "from": node_id,
                                "grid_key": broadcast_key,
                                "payload": payload,
                            },
                        )
                        if not ok:
                            dead_nodes.append(nid)
                    for nid in dead_nodes:
                        connected_nodes.pop(nid, None)

            elif msg_type == "add_grid_keys":
                # Wave 40: extend subscription set without reconnecting.
                added = _normalize_grid_keys(data.get("grid_keys", []))
                if added is None:
                    await _safe_send_json(
                        websocket,
                        {"type": "error", "message": "grid_keys must be a list of 32-char hex digests"},
                    )
                    continue
                # Cap total keys per node to bound server-side memory.
                async with _lock:
                    merged = node.grid_keys | added
                    if len(merged) > MAX_GRID_KEYS_PER_NODE:
                        await _safe_send_json(
                            websocket,
                            {"type": "error", "message": "max grid_keys per node exceeded"},
                        )
                        continue
                    node.grid_keys = merged
                await _safe_send_json(
                    websocket,
                    {"type": "grid_keys_ack", "count": len(node.grid_keys)},
                )

            elif msg_type == "remove_grid_keys":
                removed = _normalize_grid_keys(data.get("grid_keys", []))
                if removed is None:
                    await _safe_send_json(
                        websocket,
                        {"type": "error", "message": "grid_keys must be a list of 32-char hex digests"},
                    )
                    continue
                async with _lock:
                    node.grid_keys = node.grid_keys - removed
                await _safe_send_json(
                    websocket,
                    {"type": "grid_keys_ack", "count": len(node.grid_keys)},
                )

            else:
                await _safe_send_json(websocket, {"type": "error", "message": "unknown message type"})

    except (WebSocketDisconnect, Exception) as exc:
        logger.info("Node disconnected: %s (%s)", node_id, type(exc).__name__)
    finally:
        node.status = "offline"
        async with _lock:
            if connected_nodes.get(node_id) is node:
                del connected_nodes[node_id]
        await _broadcast_peer_list()


@app.websocket("/relay/pair_invite/{issuer_node_id}")
async def pair_invite_probe_ws(websocket: WebSocket, issuer_node_id: str):
    """Wave 36.F.2: relay-mediated pair handshake (no grid_key required).

    The connecter presents a signed pair-invite issued by the owner of
    ``issuer_node_id`` (anyone who possesses the link can connect — the
    invite signature is the gate). On success, the relay forwards the
    probe to the issuer's already-registered WS and parks this WS in
    the pending map until the issuer's ``pair_reply`` lands.

    Single-purpose connection: the only allowed inbound frame is the
    initial ``pair_invite_probe``; further frames are ignored. The WS
    closes when the issuer replies (or on timeout).
    """
    origin = websocket.headers.get("origin")
    if not _validate_node_id(issuer_node_id):
        await websocket.close(1008, "invalid node id")
        return
    if not _origin_allowed(origin):
        await websocket.close(1008, "origin not allowed")
        return
    await websocket.accept()

    # (1) read the probe frame
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=PAIR_INVITE_PROBE_TIMEOUT
        )
        probe = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError):
        await _safe_send_json(
            websocket,
            {"type": "pair_reject", "reason": "expected pair_invite_probe"},
        )
        try:
            await websocket.close(4001)
        except Exception:
            pass
        return

    if probe.get("type") != "pair_invite_probe":
        await _safe_send_json(
            websocket,
            {"type": "pair_reject", "reason": "unexpected message type"},
        )
        try:
            await websocket.close(4003)
        except Exception:
            pass
        return

    inv_b64 = str(probe.get("inv", ""))
    if not inv_b64:
        await _safe_send_json(
            websocket,
            {"type": "pair_reject", "reason": "missing inv"},
        )
        try:
            await websocket.close(4003)
        except Exception:
            pass
        return

    now = time.time()
    payload = _verify_pair_invite(inv_b64, now=now)
    if payload is None:
        await _safe_send_json(
            websocket,
            {"type": "pair_reject", "reason": "invalid or expired invite"},
        )
        try:
            await websocket.close(4003)
        except Exception:
            pass
        return

    invite_id = payload["invite_id"]
    issuer_pubkey = payload["issuer_pubkey"]

    # (2) atomic single-use claim
    if not await _claim_invite(invite_id, issuer_pubkey, now):
        await _safe_send_json(
            websocket,
            {"type": "pair_reject", "reason": "invite already redeemed"},
        )
        try:
            await websocket.close(4003)
        except Exception:
            pass
        return

    # (3) locate issuer's registered WS
    async with _lock:
        issuer_node = connected_nodes.get(issuer_node_id)
    if issuer_node is None:
        await _safe_send_json(
            websocket,
            {"type": "pair_reject", "reason": "issuer offline"},
        )
        try:
            await websocket.close(4004)
        except Exception:
            pass
        return

    # (4) park ourselves for the reply
    transient_id = "pair-probe-" + secrets.token_hex(16)
    async with _pair_probe_lock:
        _pair_probe_pending[transient_id] = websocket

    # (5) forward the probe to the issuer
    forwarded = {
        "type": "pair_invite_probe",
        "transient_id": transient_id,
        "inv_payload": payload,
        "bob_pubkey": str(probe.get("bob_pubkey", "")),
        "bob_relay_urls": probe.get("bob_relay_urls") or [],
        "bob_display_name": str(probe.get("bob_display_name", "")),
        "request_id": str(probe.get("request_id", "")),
    }
    delivered = await _safe_send_json(issuer_node.ws, forwarded)
    if not delivered:
        async with _pair_probe_lock:
            _pair_probe_pending.pop(transient_id, None)
        await _safe_send_json(
            websocket,
            {"type": "pair_reject", "reason": "could not deliver to issuer"},
        )
        try:
            await websocket.close(4005)
        except Exception:
            pass
        return

    # (6) wait for the issuer's reply (forwarded by the main /relay/{id}
    # loop's pair_reply branch — see below). If the issuer never responds
    # within the timeout, surface that and close.
    try:
        try:
            await asyncio.wait_for(
                websocket.receive_text(),
                timeout=PAIR_INVITE_REPLY_TIMEOUT,
            )
            # The probe side shouldn't send anything after the initial frame.
            # If they do, we ignore it and continue waiting.
        except asyncio.TimeoutError:
            # Only reach here if pair_reply hasn't arrived AND the client
            # hasn't sent anything (which is the normal case).
            await _safe_send_json(
                websocket,
                {"type": "pair_reject", "reason": "issuer did not respond in time"},
            )
        except WebSocketDisconnect:
            pass
    finally:
        async with _pair_probe_lock:
            _pair_probe_pending.pop(transient_id, None)
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "9000"))
    logger.info("Starting relay server on port %s", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
