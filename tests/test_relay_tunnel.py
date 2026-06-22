"""Wave 27 — auto-tunnel manager (cloudflared quick tunnel).

The download + a real tunnel need the internet, so those are exercised
manually. These cover the network-free helpers.
"""

from __future__ import annotations

from nexus.runtime import relay_tunnel


def test_status_when_stopped():
    st = relay_tunnel.status()
    assert st["running"] is False
    assert st["public_url"] == ""
    assert st["relay_url"] == ""


def test_cloudflared_asset_name_is_platform_specific():
    asset = relay_tunnel._cloudflared_asset()
    assert asset.startswith("cloudflared-")
    assert ("amd64" in asset) or ("arm64" in asset)


def test_tunnel_url_regex_extracts_from_cloudflared_log():
    line = (
        "2026-05-21T10:00:00Z INF |  "
        "https://random-three-words-here.trycloudflare.com  |"
    )
    match = relay_tunnel._TUNNEL_URL_RE.search(line)
    assert match is not None
    assert match.group(0) == "https://random-three-words-here.trycloudflare.com"


def test_tunnel_url_regex_ignores_non_tunnel_urls():
    assert relay_tunnel._TUNNEL_URL_RE.search("visit https://example.com") is None


def test_status_relay_url_is_wss_form(monkeypatch):
    """A running tunnel reports its public URL in ``wss://`` form."""

    class _FakeProc:
        def poll(self):
            return None  # still alive

    monkeypatch.setattr(relay_tunnel, "_proc", _FakeProc())
    monkeypatch.setattr(
        relay_tunnel, "_url", "https://abc-def.trycloudflare.com"
    )
    st = relay_tunnel.status()
    assert st["running"] is True
    assert st["public_url"] == "https://abc-def.trycloudflare.com"
    assert st["relay_url"] == "wss://abc-def.trycloudflare.com"
