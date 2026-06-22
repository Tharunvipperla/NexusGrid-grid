"""Security F-012 — cloud_connector SSRF guard.

Task-input URLs are attacker-controlled and fetched on the worker, so a fetch to
a private / loopback / link-local (cloud-metadata) address must be refused.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.runtime import cloud_connector as cc


@pytest.mark.parametrize("host,blocked", [
    ("169.254.169.254", True),   # cloud metadata (link-local)
    ("127.0.0.1", True),         # loopback
    ("10.0.0.5", True),          # RFC1918
    ("192.168.1.1", True),       # RFC1918
    ("172.16.0.1", True),        # RFC1918
    ("0.0.0.0", True),           # unspecified
    ("", True),                  # empty
    ("8.8.8.8", False),          # public
    ("1.1.1.1", False),          # public
])
def test_host_blocked_classification(host, blocked):
    assert cc._host_blocked(host) is blocked


def test_download_refuses_metadata_endpoint(tmp_path):
    dest = tmp_path / "stolen"
    ok, reason = asyncio.run(cc.download(
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/role",
        str(dest),
    ))
    assert ok is False and reason == "blocked_host"
    assert not dest.exists()  # nothing fetched/written


def test_download_refuses_loopback(tmp_path):
    dest = tmp_path / "x"
    ok, reason = asyncio.run(cc.download("http://127.0.0.1:9999/secret", str(dest)))
    assert ok is False and reason == "blocked_host"


def test_redirect_to_internal_is_blocked(tmp_path, monkeypatch):
    """A public URL that 302-redirects to the metadata IP must be caught on the
    redirect hop, not followed."""
    import httpx

    class _Resp:
        def __init__(self, redirect_to=None):
            self._loc = redirect_to
            self.is_redirect = redirect_to is not None
            self.headers = {"location": redirect_to} if redirect_to else {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        async def aiter_bytes(self):
            if False:
                yield b""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **k):
            # First hop (public host) returns a redirect to the metadata IP.
            return _Resp(redirect_to="http://169.254.169.254/latest/meta-data/")

    # Make the first host look public so we get past the initial check.
    monkeypatch.setattr(cc, "_host_blocked",
                        lambda h: h == "169.254.169.254")
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ok, reason = asyncio.run(cc.download("http://public.example/file", str(tmp_path / "y")))
    assert ok is False and reason == "blocked_host"
