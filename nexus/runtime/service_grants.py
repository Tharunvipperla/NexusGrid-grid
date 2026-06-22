"""Service-access grant lifecycle (Phase A).

A consumer requests access to a provider's advertised service; the provider
auto-approves *free* services, queues *permission* ones for manual approval, and
refuses *paid* (not available yet). Every control message is signed by its
author (the consumer signs the request, the provider signs status updates) so a
node can't forge a request "from" someone else or a grant it wasn't given.

The grant is the gate the Phase-B data-plane tunnel checks before carrying any
bytes — no ``approved`` grant, no traffic.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from nexus.core.config import LOCAL_SETTINGS
from nexus.core.identity import get_or_create_node_uuid, resolve_uuid_to_ip
from nexus.security.group_keys import get_local_group_privkey, get_local_group_pubkey
from nexus.security.usage_receipt import sign_statement, verify_statement
from nexus.storage import get_session
from nexus.storage.models import (
    Group,
    GroupMember,
    Peer,
    ServiceDbProvision,
    ServiceGrant,
)
from nexus.utils.time import iso_now

_log = logging.getLogger("nexus.runtime.service_grants")

STMT_REQUEST = "service_request"
STMT_GRANT_UPDATE = "service_grant_update"
STMT_DB_CREDS = "service_db_creds"


def find_local_service(name: str) -> dict | None:
    for s in LOCAL_SETTINGS.get("hosted_services") or []:
        if isinstance(s, dict) and s.get("name") == name:
            return s
    return None


def _req_payload(provider, service, consumer, consumer_uuid, ts) -> dict:
    return {"provider": provider, "service": service,
            "consumer": consumer, "consumer_uuid": consumer_uuid, "ts": ts}


def _grant_payload(grant_id, provider, service, consumer, status, decided_at) -> dict:
    return {"grant_id": grant_id, "provider": provider, "service": service,
            "consumer": consumer, "status": status, "decided_at": decided_at}


def _grant_dict(g: ServiceGrant) -> dict:
    return {
        "grant_id": g.grant_id, "service_name": g.service_name,
        "provider_pubkey": g.provider_pubkey, "consumer_pubkey": g.consumer_pubkey,
        "provider_uuid": g.provider_uuid, "consumer_uuid": g.consumer_uuid,
        "status": g.status, "access": g.access,
        "created_at": g.created_at, "decided_at": g.decided_at,
    }


async def _is_known_peer(consumer_uuid: str, consumer_pubkey: str) -> bool:
    """A request is only accepted from a node we're connected to — a trusted
    peer or a group co-member — so a stranger can't spam our inbox.

    Trust binds to the **pubkey** the caller's signature proves ownership of, not
    to the ``consumer_uuid`` (which is gossiped in beacons/profiles and so is not
    a secret — keying on it would let anyone who learns a member's UUID
    impersonate that member with their own keypair). See SECURITY_FINDINGS F-004.
    """
    if not consumer_pubkey:
        return False
    async with get_session() as s:
        member = (await s.execute(
            select(GroupMember.pubkey).where(
                (GroupMember.pubkey == consumer_pubkey)
            ).limit(1)
        )).scalar_one_or_none()
        if member:
            return True
        if not consumer_uuid:
            return False
        ip = resolve_uuid_to_ip(consumer_uuid)
        peer = None
        if ip:
            peer = (await s.execute(
                select(Peer).where(
                    (Peer.ip == ip) | (Peer.resolved_ip == ip)
                ).limit(1)
            )).scalar_one_or_none()
        if peer is None or not str(peer.status or "").startswith("trusted"):
            return False
        bound = peer.peer_group_pubkey or ""
    # Trusted peer: still require the signed pubkey to match the peer's recorded
    # group identity, so a stranger who knows a trusted peer's UUID can't slip in
    # with their own key (F-005). Learn the key from the peer's profile if we
    # don't have it yet (resolved via our own map → a spoofed UUID hits the real
    # peer, whose key won't match the attacker's).
    if not bound:
        bound = await _fetch_peer_group_pubkey(consumer_uuid)
    return bool(bound) and bound == consumer_pubkey


async def _fetch_peer_group_pubkey(peer_uuid: str) -> str:
    """Best-effort: fetch + cache a trusted peer's Ed25519 group pubkey from its
    profile, bound by our own UUID→addr resolution. Returns '' if unreachable."""
    addr = await resolve_peer_addr(peer_uuid)
    if not addr:
        return ""
    from nexus.networking.peer_http import peer_http_post
    try:
        res = await peer_http_post(addr, "/peer/profile", {}, timeout=4.0)
    except Exception:
        return ""
    if res.get("status") != 200:
        return ""
    pub = str((res.get("body") or {}).get("pubkey") or "")
    if pub:
        async with get_session() as s:
            row = (await s.execute(select(Peer).where(
                (Peer.ip == addr) | (Peer.resolved_ip == addr)
            ).limit(1))).scalar_one_or_none()
            if row is not None:
                row.peer_group_pubkey = pub
                await s.commit()
    return pub


# --- provider side ----------------------------------------------------------


async def handle_service_request(body: dict) -> dict:
    """Provider receives a signed access request. Returns the resulting grant."""
    provider = get_local_group_pubkey()
    service = str(body.get("service") or "")
    consumer = str(body.get("consumer_pubkey") or "")
    consumer_uuid = str(body.get("consumer_uuid") or "")
    ts = str(body.get("ts") or "")
    sig = str(body.get("sig") or "")

    payload = _req_payload(provider, service, consumer, consumer_uuid, ts)
    if not consumer or not verify_statement(STMT_REQUEST, payload, sig, consumer):
        return {"ok": False, "error": "bad_signature"}
    if not await _is_known_peer(consumer_uuid, consumer):
        return {"ok": False, "error": "not_connected"}

    svc = find_local_service(service)
    if not svc:
        return {"ok": False, "error": "no_such_service"}
    access = str(svc.get("access") or "free")

    async with get_session() as s:
        existing = (await s.execute(
            select(ServiceGrant).where(
                (ServiceGrant.service_name == service)
                & (ServiceGrant.consumer_pubkey == consumer)
                & (ServiceGrant.provider_pubkey == provider)
            ).limit(1)
        )).scalar_one_or_none()
        # Re-requesting an active/pending grant just returns it (idempotent);
        # a previously denied/revoked one can be re-requested afresh.
        if existing and existing.status in ("approved", "pending"):
            return {"ok": True, "grant": _grant_dict(existing)}

        now = iso_now()
        if access == "paid":
            status, decided = "denied", now
        elif access == "free":
            status, decided = "approved", now
        else:  # permission
            status, decided = "pending", ""

        grant = existing or ServiceGrant(grant_id=uuid.uuid4().hex)
        grant.service_name = service
        grant.provider_pubkey = provider
        grant.consumer_pubkey = consumer
        grant.provider_uuid = get_or_create_node_uuid()
        grant.consumer_uuid = consumer_uuid
        grant.status = status
        grant.access = access
        grant.created_at = grant.created_at or now
        grant.decided_at = decided
        s.add(grant)
        await s.commit()
        out = _grant_dict(grant)
    return {"ok": True, "grant": out}


async def decide_request(grant_id: str, approve: bool) -> dict:
    """Provider approves/denies a pending request, then pushes the result to
    the consumer (best-effort)."""
    async with get_session() as s:
        g = await s.get(ServiceGrant, grant_id)
        if g is None or g.provider_pubkey != get_local_group_pubkey():
            return {"ok": False, "error": "not_found"}
        if g.status != "pending":
            return {"ok": False, "error": "not_pending"}
        g.status = "approved" if approve else "denied"
        g.decided_at = iso_now()
        await s.commit()
        out = _grant_dict(g)
    await _push_grant_update(out)
    return {"ok": True, "grant": out}


async def revoke_grant(grant_id: str) -> dict:
    """Provider revokes a grant it issued; the consumer is notified so it tears
    down any open tunnel (Phase B)."""
    async with get_session() as s:
        g = await s.get(ServiceGrant, grant_id)
        if g is None or g.provider_pubkey != get_local_group_pubkey():
            return {"ok": False, "error": "not_found"}
        g.status = "revoked"
        g.decided_at = iso_now()
        await s.commit()
        out = _grant_dict(g)
    # DBaaS: drop the per-consumer database + login this grant provisioned.
    await deprovision_for_grant(grant_id)
    # Cut any live data-plane tunnel for this grant immediately.
    try:
        from nexus.runtime.service_tunnel import close_grant_streams
        await close_grant_streams(grant_id)
    except Exception:
        _log.debug("close_grant_streams failed", exc_info=True)
    await _push_grant_update(out)
    return {"ok": True, "grant": out}


# --- DBaaS: per-grant database provisioning ------------------------


def _db_provider_cfg(svc: dict) -> dict | None:
    """Return a hosted service's ``db_provider`` block, or None if it isn't a
    DBaaS service. Shape: ``{engine, admin_dsn}`` (host-only config)."""
    cfg = svc.get("db_provider") if isinstance(svc, dict) else None
    if isinstance(cfg, dict) and cfg.get("engine") and cfg.get("admin_dsn"):
        return cfg
    return None


async def provision_for_grant(grant_id: str) -> dict | None:
    """Provider side: ensure a per-consumer database + login exists for an
    approved DB-kind grant. Idempotent — returns the stored record on repeat.
    Returns ``{engine,kind,database,user,password}`` or None when the service
    isn't a DBaaS service / the grant isn't serveable."""
    async with get_session() as s:
        g = await s.get(ServiceGrant, grant_id)
        if (g is None or g.provider_pubkey != get_local_group_pubkey()
                or g.status != "approved"):
            return None
        existing = await s.get(ServiceDbProvision, grant_id)
        if existing is not None:
            return {"engine": existing.engine, "kind": existing.kind,
                    "database": existing.database, "user": existing.username,
                    "password": existing.password}
        service_name, consumer = g.service_name, g.consumer_pubkey

    svc = find_local_service(service_name)
    cfg = _db_provider_cfg(svc) if svc else None
    if not cfg:
        return None

    from nexus.runtime import db_provider
    creds = db_provider.provision(
        cfg["engine"], cfg["admin_dsn"], service_name, consumer)

    async with get_session() as s:
        # Re-check inside the write txn in case a concurrent fetch beat us.
        existing = await s.get(ServiceDbProvision, grant_id)
        if existing is not None:
            return {"engine": existing.engine, "kind": existing.kind,
                    "database": existing.database, "user": existing.username,
                    "password": existing.password}
        s.add(ServiceDbProvision(
            grant_id=grant_id, engine=creds["engine"], kind=creds["kind"],
            database=creds["database"], username=creds["user"],
            password=creds["password"], created_at=iso_now()))
        await s.commit()
    return creds


async def deprovision_for_grant(grant_id: str) -> None:
    """Provider side: drop the provisioned DB + login for *grant_id* (if any)
    and forget the record. Best-effort — a drop failure still removes our row
    so we don't keep serving stale creds."""
    async with get_session() as s:
        row = await s.get(ServiceDbProvision, grant_id)
        g = await s.get(ServiceGrant, grant_id)
        if row is None:
            return
        engine, database, username = row.engine, row.database, row.username
        service_name = g.service_name if g else ""
        consumer = g.consumer_pubkey if g else ""
    svc = find_local_service(service_name) if service_name else None
    cfg = _db_provider_cfg(svc) if svc else None
    if cfg and consumer:
        try:
            from nexus.runtime import db_provider
            db_provider.deprovision(cfg["engine"], cfg["admin_dsn"],
                                    service_name, consumer)
        except Exception:
            _log.warning("DBaaS deprovision failed for %s", grant_id, exc_info=True)
    async with get_session() as s:
        row = await s.get(ServiceDbProvision, grant_id)
        if row is not None:
            await s.delete(row)
            await s.commit()


async def handle_db_credentials(body: dict) -> dict:
    """Provider side: a consumer with an approved DB grant fetches its
    connection. Verified by the same signed-statement model as the access
    request; provisions lazily on first fetch (idempotent)."""
    provider = get_local_group_pubkey()
    service = str(body.get("service") or "")
    consumer = str(body.get("consumer_pubkey") or "")
    consumer_uuid = str(body.get("consumer_uuid") or "")
    ts = str(body.get("ts") or "")
    sig = str(body.get("sig") or "")

    payload = _req_payload(provider, service, consumer, consumer_uuid, ts)
    if not consumer or not verify_statement(STMT_DB_CREDS, payload, sig, consumer):
        return {"ok": False, "error": "bad_signature"}

    async with get_session() as s:
        g = (await s.execute(
            select(ServiceGrant).where(
                (ServiceGrant.service_name == service)
                & (ServiceGrant.consumer_pubkey == consumer)
                & (ServiceGrant.provider_pubkey == provider)
            ).limit(1)
        )).scalar_one_or_none()
        if g is None:
            return {"ok": False, "error": "no_grant"}
        if g.status != "approved":
            return {"ok": False, "error": "not_approved"}
        grant_id = g.grant_id

    creds = await provision_for_grant(grant_id)
    if not creds:
        return {"ok": False, "error": "not_a_db_service"}
    return {"ok": True, "conn": {
        "engine": creds["engine"], "kind": creds["kind"],
        "database": creds["database"], "user": creds["user"],
        "password": creds["password"],
    }}


async def fetch_db_credentials(peer_uuid: str, service_name: str,
                               provider_pubkey: str) -> dict:
    """Consumer side: sign + send a credential request to the provider for an
    approved DB grant. Returns ``{ok, conn}``; creds are NOT persisted locally
    (fetch on demand)."""
    me = get_local_group_pubkey()
    ts = iso_now()
    payload = _req_payload(provider_pubkey, service_name, me,
                           get_or_create_node_uuid(), ts)
    sig = sign_statement(STMT_DB_CREDS, payload, get_local_group_privkey())
    addr = await resolve_peer_addr(peer_uuid) or peer_uuid
    from nexus.networking.peer_http import peer_http_post
    res = await peer_http_post(addr, "/peer/service_db_credentials", {
        "service": service_name, "consumer_pubkey": me,
        "consumer_uuid": get_or_create_node_uuid(), "ts": ts, "sig": sig,
    })
    if res.get("status") != 200:
        return {"ok": False, "error": f"unreachable ({res.get('status')})"}
    body = res.get("body") or {}
    if not body.get("ok"):
        return {"ok": False, "error": body.get("error") or "refused"}
    return {"ok": True, "conn": body.get("conn") or {}}


async def resolve_peer_addr(node_uuid: str) -> str:
    """Resolve a peer's reachable address: live UUID→IP map first, then the
    trusted-Peer row, then a group roster address. Returns "" if unknown."""
    if not node_uuid:
        return ""
    ip = resolve_uuid_to_ip(node_uuid)
    if ip and ip != node_uuid:
        return ip
    async with get_session() as s:
        row = (await s.execute(
            select(Peer.resolved_ip).where(
                (Peer.ip == node_uuid) & (Peer.resolved_ip != "")
            ).limit(1)
        )).scalar_one_or_none()
        if row:
            return row
        addr = (await s.execute(
            select(GroupMember.peer_address).where(
                (GroupMember.node_id == node_uuid) & (GroupMember.peer_address != "")
            ).limit(1)
        )).scalar_one_or_none()
        if addr:
            return addr
    return ""


async def _push_grant_update(grant: dict) -> None:
    payload = _grant_payload(
        grant["grant_id"], grant["provider_pubkey"], grant["service_name"],
        grant["consumer_pubkey"], grant["status"], grant["decided_at"],
    )
    sig = sign_statement(STMT_GRANT_UPDATE, payload, get_local_group_privkey())
    addr = await resolve_peer_addr(grant.get("consumer_uuid") or "")
    if not addr:
        return
    from nexus.networking.peer_http import peer_http_post
    try:
        await peer_http_post(
            addr, "/peer/service_grant_update", {"grant": grant, "sig": sig}
        )
    except Exception:
        _log.debug("grant update push failed", exc_info=True)


# --- consumer side ----------------------------------------------------------


async def apply_grant_update(body: dict) -> dict:
    """Consumer receives a provider-signed status change for a grant it holds.
    The signature must be by the grant's own ``provider_pubkey``, so another
    node can't flip our grant's status."""
    grant = body.get("grant") or {}
    sig = str(body.get("sig") or "")
    provider = str(grant.get("provider_pubkey") or "")
    payload = _grant_payload(
        str(grant.get("grant_id") or ""), provider,
        str(grant.get("service_name") or ""), str(grant.get("consumer_pubkey") or ""),
        str(grant.get("status") or ""), str(grant.get("decided_at") or ""),
    )
    if not verify_statement(STMT_GRANT_UPDATE, payload, sig, provider):
        return {"ok": False, "error": "bad_signature"}
    if str(grant.get("consumer_pubkey") or "") != get_local_group_pubkey():
        return {"ok": False, "error": "not_mine"}

    async with get_session() as s:
        g = await s.get(ServiceGrant, str(grant.get("grant_id") or ""))
        if g is None:
            return {"ok": False, "error": "unknown_grant"}
        # Anti-replay (SECURITY F-006): a provider-signed frame stays valid
        # forever, so a captured *older* update (e.g. an "approved" before a
        # later revoke) could be replayed to revert our state. decided_at is
        # ISO-8601 UTC → lexicographically monotonic, so refuse anything strictly
        # older than what we've already applied.
        incoming_decided = str(grant.get("decided_at") or "")
        if g.decided_at and incoming_decided and incoming_decided < g.decided_at:
            return {"ok": False, "error": "stale_update"}
        g.status = str(grant.get("status") or g.status)
        g.decided_at = incoming_decided or g.decided_at
        await s.commit()
        new_status = g.status
    if new_status in ("revoked", "denied"):
        try:
            from nexus.runtime.service_tunnel import close_grant_streams
            await close_grant_streams(str(grant.get("grant_id") or ""))
        except Exception:
            _log.debug("close_grant_streams failed", exc_info=True)
    return {"ok": True}


async def request_access(peer_uuid: str, service_name: str, provider_pubkey: str) -> dict:
    """Consumer initiates: sign + send a request to *peer_uuid*, store the grant
    it returns. ``provider_pubkey`` is the peer's group key (from its profile)."""
    me = get_local_group_pubkey()
    ts = iso_now()
    payload = _req_payload(provider_pubkey, service_name, me, get_or_create_node_uuid(), ts)
    sig = sign_statement(STMT_REQUEST, payload, get_local_group_privkey())
    addr = await resolve_peer_addr(peer_uuid) or peer_uuid
    from nexus.networking.peer_http import peer_http_post
    res = await peer_http_post(addr, "/peer/service_request", {
        "service": service_name, "consumer_pubkey": me,
        "consumer_uuid": get_or_create_node_uuid(), "ts": ts, "sig": sig,
    })
    if res.get("status") != 200:
        return {"ok": False, "error": f"unreachable ({res.get('status')})"}
    body = res.get("body") or {}
    if not body.get("ok"):
        return {"ok": False, "error": body.get("error") or "refused"}
    g = body.get("grant") or {}
    # Persist our copy of the grant (provider_uuid = whom we asked).
    async with get_session() as s:
        row = await s.get(ServiceGrant, str(g.get("grant_id") or "")) or ServiceGrant(
            grant_id=str(g.get("grant_id") or uuid.uuid4().hex)
        )
        row.service_name = service_name
        row.provider_pubkey = provider_pubkey
        row.consumer_pubkey = me
        row.provider_uuid = peer_uuid
        row.consumer_uuid = get_or_create_node_uuid()
        row.status = str(g.get("status") or "pending")
        row.access = str(g.get("access") or "")
        row.created_at = row.created_at or ts
        row.decided_at = str(g.get("decided_at") or "")
        s.add(row)
        await s.commit()
        out = _grant_dict(row)
    return {"ok": True, "grant": out}


async def list_pending_requests() -> list[dict]:
    """Provider inbox: requests awaiting a decision."""
    async with get_session() as s:
        rows = (await s.execute(
            select(ServiceGrant).where(
                (ServiceGrant.provider_pubkey == get_local_group_pubkey())
                & (ServiceGrant.status == "pending")
            )
        )).scalars().all()
    return [_grant_dict(g) for g in rows]


async def list_grants() -> dict:
    """All grants this node knows: ones we issued (as provider) and ones we hold
    (as consumer)."""
    me = get_local_group_pubkey()
    async with get_session() as s:
        rows = (await s.execute(
            select(ServiceGrant).where(
                (ServiceGrant.provider_pubkey == me)
                | (ServiceGrant.consumer_pubkey == me)
            )
        )).scalars().all()
    issued = [_grant_dict(g) for g in rows if g.provider_pubkey == me]
    held = [_grant_dict(g) for g in rows if g.consumer_pubkey == me and g.provider_pubkey != me]
    return {"issued": issued, "held": held}


async def _discoverable_peers() -> dict[str, dict]:
    """Map ``node_uuid -> {name, source}`` for every node we can ask: trusted
    peers and group co-members (excluding ourselves)."""
    from nexus.core.identity import resolve_ip_to_uuid
    me_uuid = get_or_create_node_uuid()
    peers: dict[str, dict] = {}
    async with get_session() as s:
        members = (await s.execute(
            select(GroupMember.node_id, GroupMember.display_name, Group.name)
            .join(Group, Group.id == GroupMember.group_id)
        )).all()
        for node_id, dname, gname in members:
            uid = (node_id or "").strip()
            if not uid or uid == me_uuid:
                continue
            entry = peers.setdefault(uid, {"name": dname or "", "groups": set()})
            entry["groups"].add(gname or "")
            if dname and not entry["name"]:
                entry["name"] = dname
        trusted = (await s.execute(
            select(Peer.ip, Peer.resolved_ip, Peer.display_name, Peer.status)
        )).all()
    for ip, resolved, dname, status in trusted:
        if not str(status or "").startswith("trusted"):
            continue
        uid = resolve_ip_to_uuid(ip) or ip
        if not uid or uid == me_uuid:
            continue
        entry = peers.setdefault(uid, {"name": dname or "", "groups": set()})
        if dname and not entry["name"]:
            entry["name"] = dname
    return peers


async def discover_services() -> dict:
    """On-demand fan-out: ask every connected peer / co-member for its profile
    and aggregate the services they advertise. Best-effort — unreachable peers
    are skipped. The UI filters by tag/name locally."""
    import asyncio

    from nexus.networking.peer_http import peer_http_post

    peers = await _discoverable_peers()

    async def _one(uid: str, meta: dict):
        addr = await resolve_peer_addr(uid)
        if not addr:
            return []
        try:
            res = await peer_http_post(addr, "/peer/profile", {}, timeout=4.0)
        except Exception:
            return []
        if res.get("status") != 200:
            return []
        body = res.get("body") or {}
        groups = sorted(g for g in meta.get("groups", set()) if g)
        source = ("group: " + ", ".join(groups)) if groups else "connected peer"
        out = []
        for svc in body.get("hosted_services") or []:
            out.append({
                "provider_uuid": uid,
                "provider_name": meta.get("name") or body.get("display_name") or uid,
                "provider_pubkey": body.get("pubkey") or "",
                "source": source,
                "service": svc,
            })
        return out

    results = await asyncio.gather(*[_one(u, m) for u, m in peers.items()])
    flat = [item for sub in results for item in sub]
    return {"services": flat, "peers_queried": len(peers)}


# --- replication via cookbook -------------------------------------


def _cookbook_dir():
    from nexus.core.paths import BASE_DIR
    d = BASE_DIR / "cookbooks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cookbook_filename(provider_uuid: str, service_name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in
                   f"{provider_uuid}_{service_name}").strip("_")[:120]
    return (safe or "cookbook") + ".md"


def _render_cookbook(svc: dict, provider_uuid: str, provider_name: str) -> str:
    """Render a provider's PUBLIC service descriptor as a standalone recipe file.
    Only public fields — the host-only local target was already stripped."""
    lines = [f"# {svc.get('name') or 'service'}", ""]
    meta = [f"- **provider:** {provider_name or provider_uuid} (`{provider_uuid}`)"]
    if svc.get("version"):
        meta.append(f"- **version:** {svc['version']}")
    if svc.get("access"):
        meta.append(f"- **access:** {svc['access']}")
    if svc.get("tags"):
        meta.append(f"- **tags:** {', '.join(svc['tags'])}")
    lines += meta + [""]
    if svc.get("description"):
        lines += [svc["description"], ""]
    comps = svc.get("components") or []
    if comps:
        lines.append("## Components")
        for c in comps:
            proto = f" ({c['protocol']})" if c.get("protocol") else ""
            lines.append(f"- **{c.get('name')}**{proto}")
        lines.append("")
    lines += ["## Readme", "", str(svc.get("readme") or "").strip(), ""]
    lines += ["---",
              "_Copied via NexusGrid cookbook. You run and sandbox this yourself; "
              "NexusGrid does not execute it for you._"]
    return "\n".join(lines)


async def replicate_cookbook(provider_uuid: str, service_name: str) -> dict:
    """Consumer-side: re-fetch the provider's LIVE service, confirm the provider
    marked it ``replicable``, then write its public recipe to a local cookbook
    file the user can run themselves. Never executes anything."""
    addr = await resolve_peer_addr(provider_uuid) or provider_uuid
    from nexus.networking.peer_http import peer_http_post
    res = await peer_http_post(addr, "/peer/profile", {}, timeout=5.0)
    if res.get("status") != 200:
        return {"ok": False, "error": f"unreachable ({res.get('status')})"}
    body = res.get("body") or {}
    svc = next((s for s in (body.get("hosted_services") or [])
                if isinstance(s, dict) and s.get("name") == service_name), None)
    if not svc:
        return {"ok": False, "error": "no_such_service"}
    # Provider opt-in is enforced from the provider's OWN served flag, so a
    # consumer editing its client can't copy a service the host didn't share.
    if not svc.get("replicable"):
        return {"ok": False, "error": "not_replicable"}

    content = _render_cookbook(svc, provider_uuid, body.get("display_name") or "")
    fname = _cookbook_filename(provider_uuid, service_name)
    path = _cookbook_dir() / fname
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "filename": fname, "path": str(path), "content": content}


def list_cookbooks() -> dict:
    """List cookbooks copied to this machine."""
    d = _cookbook_dir()
    items = []
    for p in sorted(d.glob("*.md")):
        try:
            items.append({"filename": p.name, "path": str(p),
                          "size": p.stat().st_size})
        except OSError:
            continue
    return {"cookbooks": items}


__all__ = [
    "handle_service_request", "decide_request", "revoke_grant",
    "apply_grant_update", "request_access", "discover_services",
    "list_pending_requests", "list_grants", "find_local_service",
    "replicate_cookbook", "list_cookbooks",
    "provision_for_grant", "deprovision_for_grant",
    "handle_db_credentials", "fetch_db_credentials",
]
