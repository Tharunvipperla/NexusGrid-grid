"""Wave 15.5 — two-node join handshake.

The codebase uses a module-global async DB engine, so a *live*
two-node test (admin + joiner sharing one process, two engines) is
impractical. Instead each test runs in two phases:

1. **Admin phase.** Bind the engine to the admin's DB, create a
   group, mint an invite, call ``/peer/group/join_request`` directly
   on the admin TestClient with a pre-generated joiner pubkey, and
   capture the response (or error).
2. **Joiner phase.** Tear down the admin engine, bind to a fresh
   joiner DB seeded with the joiner's keypair, monkeypatch
   :func:`nexus.api.groups._post_to_admin` to return the captured
   admin response (no real network), and run
   ``/local/groups/join``. Verify joiner state.

This exercises every line of the wire protocol — request format,
response format, joiner-side persistence — while keeping each phase
single-engine-clean.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from typing import Callable, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.group_peer import router as peer_router
from nexus.api.groups import router as local_router
from nexus.security import group_grant, group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database


# ---- DB-lifecycle helpers -----------------------------------------------


def _bind_db(url: str) -> None:
    asyncio.run(database.init_db(0, url=url))


def _unbind_db() -> None:
    async def _go():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_go())


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(local_router)
    app.include_router(peer_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    return app


# ---- key-management helpers --------------------------------------------


def _seed_node_keypair(node_dir, monkeypatch) -> tuple[str, str]:
    """Point key persistence at ``node_dir`` and force a fresh keypair.

    Returns ``(privkey_hex, pubkey_hex)`` for the node.
    """
    node_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", node_dir)
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", node_dir)
    group_keys._reset_for_testing()
    tokens._reset_for_testing()
    return group_keys.get_local_group_privkey(), group_keys.get_local_group_pubkey()


# ---- two-phase fixture --------------------------------------------------


class HandshakeHarness:
    """Coordinator for admin-phase / joiner-phase sequencing."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.admin_dir = tmp_path / "admin"
        self.joiner_dir = tmp_path / "joiner"
        self.admin_db = f"sqlite+aiosqlite:///{(self.admin_dir / 'admin.db').as_posix()}"
        self.joiner_db = (
            f"sqlite+aiosqlite:///{(self.joiner_dir / 'joiner.db').as_posix()}"
        )
        self.admin_priv: Optional[str] = None
        self.admin_pub: Optional[str] = None
        self.joiner_priv: Optional[str] = None
        self.joiner_pub: Optional[str] = None

    # --- admin phase --------------------------------------------------

    def in_admin_phase(self, fn: Callable[[TestClient], object]):
        """Bind admin DB + identity, call ``fn(admin_client)``, return its value."""
        self.admin_priv, self.admin_pub = _seed_node_keypair(
            self.admin_dir, self.monkeypatch
        )
        _bind_db(self.admin_db)
        try:
            app = _make_app()
            with TestClient(app) as c:
                return fn(c)
        finally:
            _unbind_db()

    # --- joiner phase -------------------------------------------------

    def in_joiner_phase(
        self,
        fn: Callable[[TestClient], object],
        admin_response: tuple[int, dict],
    ):
        """Bind joiner DB + identity, monkeypatch _post_to_admin, run fn."""
        self.joiner_priv, self.joiner_pub = _seed_node_keypair(
            self.joiner_dir, self.monkeypatch
        )

        async def _fake_post(
            admin_address, path, body, admin_node_id="",
            link_relay_urls=None, link_grid_key="",
        ):
            return admin_response

        self.monkeypatch.setattr(
            "nexus.api.groups._post_to_admin", _fake_post
        )

        _bind_db(self.joiner_db)
        try:
            app = _make_app()
            with TestClient(app) as c:
                return fn(c)
        finally:
            _unbind_db()

    def joiner_pubkey_only(self) -> str:
        """Pre-compute the joiner's pubkey without binding the DB."""
        if self.joiner_pub is None:
            self.joiner_priv, self.joiner_pub = _seed_node_keypair(
                self.joiner_dir, self.monkeypatch
            )
        return self.joiner_pub


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return HandshakeHarness(tmp_path, monkeypatch)


# ---- helpers used inside admin phase ------------------------------------


def _create_and_invite(client: TestClient, slot_cap: int = 2) -> tuple[dict, dict]:
    g = client.post("/local/groups", json={"name": "GPU Machines"}).json()
    inv = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": slot_cap}
    ).json()
    return g, inv


def _admin_join_request(
    client: TestClient, invite_token: str, joiner_pubkey: str
) -> tuple[int, dict]:
    res = client.post(
        "/peer/group/join_request",
        json={"invite_token": invite_token, "joiner_pubkey": joiner_pubkey},
    )
    try:
        return res.status_code, res.json()
    except ValueError:
        return res.status_code, {}


# ---- happy path ---------------------------------------------------------


def test_admin_endpoint_issues_grant_to_joiner_pubkey(harness):
    joiner_pub = harness.joiner_pubkey_only()

    captured: dict = {}

    def _admin(client):
        g, inv = _create_and_invite(client)
        status, body = _admin_join_request(client, inv["token"], joiner_pub)
        captured.update(group=g, invite=inv, status=status, body=body)

    harness.in_admin_phase(_admin)
    assert captured["status"] == 200
    body = captured["body"]
    assert body["group_id"] == captured["group"]["id"]
    assert body["founder_pubkey"] == harness.admin_pub
    assert harness.admin_pub in body["admin_pubkeys"]
    assert body["default_role"] == "member"
    assert body["grant_blob_b64"]


def test_admin_increments_invite_slots_after_grant(harness):
    joiner_pub = harness.joiner_pubkey_only()

    captured: dict = {}

    def _admin(client):
        g, inv = _create_and_invite(client, slot_cap=3)
        _admin_join_request(client, inv["token"], joiner_pub)
        detail = client.get(f"/local/groups/{g['id']}").json()
        captured["member_count"] = len(detail["members"])

    harness.in_admin_phase(_admin)
    # Founder + joiner.
    assert captured["member_count"] == 2


def test_admin_refuses_second_join_after_cap_filled(harness):
    """slot_cap=1 → first join succeeds, second is refused."""
    joiner_pub_1 = harness.joiner_pubkey_only()
    joiner_pub_2 = group_grant.generate_keypair()[1]

    captured: dict = {}

    def _admin(client):
        _g, inv = _create_and_invite(client, slot_cap=1)
        s1, _ = _admin_join_request(client, inv["token"], joiner_pub_1)
        s2, body2 = _admin_join_request(client, inv["token"], joiner_pub_2)
        captured.update(s1=s1, s2=s2, body2=body2)

    harness.in_admin_phase(_admin)
    assert captured["s1"] == 200
    assert captured["s2"] == 410


def test_admin_diagnoses_shared_group_key_when_joiner_pubkey_is_self(harness):
    """If the joiner's pubkey == the admin's own, surface the shared-dir hint."""
    captured: dict = {}

    def _admin(client):
        _g, inv = _create_and_invite(client)
        # Send the admin's own pubkey back as the joiner — simulates
        # two nodes sharing a .nexus_group_key file.
        s, body = _admin_join_request(client, inv["token"], harness.admin_pub)
        captured.update(status=s, body=body)

    harness.in_admin_phase(_admin)
    assert captured["status"] == 409
    detail = captured["body"].get("detail", "")
    assert "sharing" in detail and ".nexus_group_key" in detail


def test_admin_refuses_join_request_after_rotation(harness):
    joiner_pub = harness.joiner_pubkey_only()

    captured: dict = {}

    def _admin(client):
        g, inv = _create_and_invite(client)
        client.post(f"/local/groups/{g['id']}/invites/{inv['token']}/rotate")
        s, _ = _admin_join_request(client, inv["token"], joiner_pub)
        captured["s"] = s

    harness.in_admin_phase(_admin)
    assert captured["s"] == 410


def test_admin_returns_404_for_unknown_token(harness):
    joiner_pub = harness.joiner_pubkey_only()

    captured: dict = {}

    def _admin(client):
        _create_and_invite(client)
        s, _ = _admin_join_request(client, "no-such-token", joiner_pub)
        captured["s"] = s

    harness.in_admin_phase(_admin)
    assert captured["s"] == 404


# ---- joiner-side persistence -------------------------------------------


def test_joiner_persists_group_and_grant(harness):
    joiner_pub = harness.joiner_pubkey_only()
    captured: dict = {}

    def _admin(client):
        g, inv = _create_and_invite(client)
        status, body = _admin_join_request(client, inv["token"], joiner_pub)
        assert status == 200
        captured["admin_response"] = body
        captured["group_id"] = g["id"]
        captured["founder_pubkey"] = harness.admin_pub

    harness.in_admin_phase(_admin)

    def _joiner(client):
        res = client.post(
            "/local/groups/join",
            json={"admin_address": "x:0", "invite_token": "doesnt-matter"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["group_id"] == captured["group_id"]
        assert body["my_role"] == "member"

        # The joiner now sees the group in its own list.
        listing = client.get("/local/groups").json()
        ids = {g["id"] for g in listing["groups"]}
        assert captured["group_id"] in ids

    harness.in_joiner_phase(
        _joiner, admin_response=(200, captured["admin_response"])
    )


def test_joiner_grant_blob_validates_with_admin_pubkey(harness):
    """The grant blob the joiner persisted must verify under the
    admin's pubkey + survive a challenge-response roundtrip."""
    joiner_pub = harness.joiner_pubkey_only()
    captured: dict = {}

    def _admin(client):
        _g, inv = _create_and_invite(client)
        _, body = _admin_join_request(client, inv["token"], joiner_pub)
        captured["admin_response"] = body
        captured["admin_pub"] = harness.admin_pub

    harness.in_admin_phase(_admin)

    blob_after_b64 = captured["admin_response"]["grant_blob_b64"]
    blob_after = base64.b64decode(blob_after_b64.encode("ascii"))

    grant = group_grant.verify_grant(
        blob_after, group_admin_pubkeys=[captured["admin_pub"]]
    )
    assert grant is not None
    assert grant.member_pubkey == joiner_pub
    assert grant.roles == ("member",)

    # Sign + verify a challenge using the joiner's private key.
    def _joiner_signs(client):
        priv = group_keys.get_local_group_privkey()
        nonce = secrets.token_bytes(16)
        sig = group_grant.sign_challenge(
            grant_blob=blob_after, nonce=nonce, member_privkey=priv
        )
        ok = group_grant.verify_challenge(
            grant_blob=blob_after,
            nonce=nonce,
            signature=sig,
            group_admin_pubkeys=[captured["admin_pub"]],
        )
        captured["challenge_ok"] = ok

    harness.in_joiner_phase(
        _joiner_signs, admin_response=(200, captured["admin_response"])
    )

    assert captured["challenge_ok"] is True


# ---- /peer/group/challenge_verify --------------------------------------


def test_admin_challenge_verify_accepts_real_signature(harness):
    joiner_pub = harness.joiner_pubkey_only()
    captured: dict = {}

    def _admin(client):
        _g, inv = _create_and_invite(client)
        _, body = _admin_join_request(client, inv["token"], joiner_pub)
        captured["group_id"] = body["group_id"]
        captured["blob"] = base64.b64decode(body["grant_blob_b64"].encode("ascii"))

    harness.in_admin_phase(_admin)

    def _joiner_signs(client):
        priv = group_keys.get_local_group_privkey()
        nonce = secrets.token_bytes(16)
        sig = group_grant.sign_challenge(
            grant_blob=captured["blob"], nonce=nonce, member_privkey=priv
        )
        captured["nonce"] = nonce
        captured["sig"] = sig

    harness.in_joiner_phase(
        _joiner_signs, admin_response=(200, {})
    )

    # Back to admin to call /peer/group/challenge_verify with the joiner's sig.
    def _admin_verify(client):
        res = client.post(
            "/peer/group/challenge_verify",
            json={
                "group_id": captured["group_id"],
                "grant_blob_b64": base64.b64encode(captured["blob"]).decode("ascii"),
                "nonce_b64": base64.b64encode(captured["nonce"]).decode("ascii"),
                "signature_b64": base64.b64encode(captured["sig"]).decode("ascii"),
            },
        )
        captured["verify_status"] = res.status_code
        captured["verify_body"] = res.json()

    # Need to bind admin DB again (it was unbound by previous phase),
    # but seed_node_keypair won't regenerate the admin's key because the
    # file already exists -- we'll get the same admin pubkey back.
    harness.in_admin_phase(_admin_verify)

    assert captured["verify_status"] == 200
    assert captured["verify_body"]["ok"] is True


def test_admin_challenge_verify_rejects_wrong_nonce(harness):
    joiner_pub = harness.joiner_pubkey_only()
    captured: dict = {}

    def _admin(client):
        _g, inv = _create_and_invite(client)
        _, body = _admin_join_request(client, inv["token"], joiner_pub)
        captured["group_id"] = body["group_id"]
        captured["blob"] = base64.b64decode(body["grant_blob_b64"].encode("ascii"))

    harness.in_admin_phase(_admin)

    def _joiner_signs(client):
        priv = group_keys.get_local_group_privkey()
        nonce_signed = secrets.token_bytes(16)
        sig = group_grant.sign_challenge(
            grant_blob=captured["blob"],
            nonce=nonce_signed,
            member_privkey=priv,
        )
        captured["sig"] = sig
        captured["wrong_nonce"] = secrets.token_bytes(16)

    harness.in_joiner_phase(
        _joiner_signs, admin_response=(200, {})
    )

    def _admin_verify(client):
        res = client.post(
            "/peer/group/challenge_verify",
            json={
                "group_id": captured["group_id"],
                "grant_blob_b64": base64.b64encode(captured["blob"]).decode("ascii"),
                "nonce_b64": base64.b64encode(captured["wrong_nonce"]).decode("ascii"),
                "signature_b64": base64.b64encode(captured["sig"]).decode("ascii"),
            },
        )
        captured["verify_body"] = res.json()

    harness.in_admin_phase(_admin_verify)

    assert captured["verify_body"]["ok"] is False
