"""D3 — outbound webhooks: matcher, payload, signing, normalization, dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest

from nexus.core.config import _normalize_webhooks, normalize_local_settings
from nexus.runtime import webhooks


# --- event_matches -----------------------------------------------------------


def test_matches_exact():
    assert webhooks.event_matches(["task.completed"], "task.completed")
    assert not webhooks.event_matches(["task.completed"], "task.failed")


def test_matches_prefix_wildcard():
    assert webhooks.event_matches(["task.*"], "task.completed")
    assert webhooks.event_matches(["task.*"], "task.failed")
    assert not webhooks.event_matches(["task.*"], "dag.released")


def test_matches_global_wildcard():
    assert webhooks.event_matches(["*"], "anything.here")


def test_matches_empty():
    assert not webhooks.event_matches([], "task.completed")


# --- build_payload / sign_body ----------------------------------------------


def test_build_payload_shape():
    p = webhooks.build_payload("task.completed", {"task_id": "abc"}, node_id="node1")
    assert p["event"] == "task.completed"
    assert p["node"] == "node1"
    assert p["data"] == {"task_id": "abc"}
    assert isinstance(p["ts"], str) and p["ts"]


def test_sign_body_matches_hmac():
    body = b'{"x":1}'
    sig = webhooks.sign_body("s3cr3t", body)
    expected = hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"


def test_sign_body_empty_secret():
    assert webhooks.sign_body("", b"anything") == ""


# --- _normalize_webhooks -----------------------------------------------------


def test_normalize_keeps_valid_hook():
    out = _normalize_webhooks([
        {"id": "h1", "url": "https://example.com/hook",
         "events": ["task.completed", "task.failed"], "secret": "s",
         "enabled": True, "description": "ci"},
    ])
    assert len(out) == 1
    assert out[0]["url"] == "https://example.com/hook"
    assert out[0]["events"] == ["task.completed", "task.failed"]
    assert out[0]["enabled"] is True


def test_normalize_drops_non_http_url():
    out = _normalize_webhooks([
        {"url": "ftp://nope"},
        {"url": "file:///etc/passwd"},
        {"url": "javascript:alert(1)"},
        {"url": "http://ok.test/x", "events": ["task.completed"]},
    ])
    assert [h["url"] for h in out] == ["http://ok.test/x"]


def test_normalize_caps_count_and_fields():
    many = [{"url": f"https://h{i}.test", "events": ["*"]} for i in range(60)]
    assert len(_normalize_webhooks(many)) == 50

    out = _normalize_webhooks([{
        "url": "https://h.test", "events": ["e"] * 50,
        "description": "d" * 500,
    }])
    assert len(out[0]["events"]) == 32
    assert len(out[0]["description"]) == 200


def test_normalize_non_list_returns_empty():
    assert _normalize_webhooks(None) == []
    assert _normalize_webhooks({"url": "https://x"}) == []


def test_round_trips_through_normalize_local_settings():
    merged = normalize_local_settings({"webhooks": [
        {"url": "https://x.test/hook", "events": ["task.completed"]},
    ]})
    assert merged["webhooks"][0]["url"] == "https://x.test/hook"
    assert normalize_local_settings({})["webhooks"] == []


# --- dispatch ----------------------------------------------------------------


def test_dispatch_delivers_to_matching_enabled(monkeypatch):
    from nexus.core import LOCAL_SETTINGS

    sent: list[dict] = []

    async def fake_deliver(sub, event, data, node_id):
        sent.append({"id": sub["id"], "event": event})
        return {"ok": True}

    monkeypatch.setitem(LOCAL_SETTINGS, "webhooks", [
        {"id": "a", "url": "https://a.test", "events": ["task.completed"], "enabled": True},
        {"id": "b", "url": "https://b.test", "events": ["task.failed"], "enabled": True},
        {"id": "c", "url": "https://c.test", "events": ["task.completed"], "enabled": False},
    ])
    monkeypatch.setattr(webhooks, "_deliver", fake_deliver)

    async def run():
        webhooks.dispatch("task.completed", {"task_id": "t1"})
        # let the scheduled delivery tasks run
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert [s["id"] for s in sent] == ["a"]  # only enabled + matching


def test_deliver_records_delivery(monkeypatch):
    """_deliver posts the signed body and records the outcome."""
    captured = {}

    class FakeResp:
        status_code = 204

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["body"] = content
            captured["headers"] = headers
            return FakeResp()

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", FakeClient)
    webhooks._deliveries.clear()

    sub = {"id": "z", "url": "https://z.test/hook", "secret": "topsecret"}
    result = asyncio.run(
        webhooks._deliver(sub, "webhook.test", {"hi": 1}, "node9")
    )
    assert result["ok"] is True
    assert result["status"] == 204
    # body is the signed JSON payload
    payload = json.loads(captured["body"])
    assert payload["event"] == "webhook.test"
    assert payload["node"] == "node9"
    # signature header present and correct
    expected = hmac.new(b"topsecret", captured["body"], hashlib.sha256).hexdigest()
    assert captured["headers"]["X-NexusGrid-Signature"] == f"sha256={expected}"
    # recorded in the log
    assert webhooks.recent_deliveries()[0]["id"] == "z"


def test_deliver_records_failure(monkeypatch):
    class BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", BoomClient)
    webhooks._deliveries.clear()

    result = asyncio.run(
        webhooks._deliver({"id": "f", "url": "https://f.test"}, "task.completed", {}, "")
    )
    assert result["ok"] is False
    assert result["status"] is None
    assert "connection refused" in result["error"]


def test_status_changed_synthesizes_completed(monkeypatch):
    fired: list[str] = []
    monkeypatch.setattr(webhooks, "dispatch", lambda ev, data: fired.append(ev))

    webhooks._on_status_changed({"task_id": "t", "new_status": "completed"})
    assert fired == ["task.status_changed", "task.completed"]

    fired.clear()
    webhooks._on_status_changed({"task_id": "t", "new_status": "failed"})
    assert fired == ["task.status_changed", "task.failed"]

    fired.clear()
    webhooks._on_status_changed({"task_id": "t", "new_status": "processing"})
    assert fired == ["task.status_changed"]
