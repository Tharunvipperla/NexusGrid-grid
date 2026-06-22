"""Wave 6.2 — provider registry + driver smoke tests.

Real GDrive upload is exercised against a mocked SDK; integration testing
against the real Drive API lives in `scripts/wave6_cloud_eviction_e2e.py`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import AsyncIterator

import pytest

from nexus.storage.cloud import PROVIDERS
from nexus.storage.cloud.base import CloudProvider
from nexus.storage.cloud.b2 import B2Provider
from nexus.storage.cloud.gdrive import GoogleDriveProvider
from nexus.storage.cloud.r2 import R2Provider
from nexus.storage.cloud.s3 import S3Provider


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_all_four_providers_register():
    assert set(PROVIDERS.keys()) == {"gdrive", "s3", "r2", "b2"}


def test_each_registered_class_subclasses_cloud_provider():
    for cls in PROVIDERS.values():
        assert issubclass(cls, CloudProvider)


# ---------------------------------------------------------------------------
# Stub drivers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider_cls", [S3Provider, R2Provider, B2Provider])
def test_stub_drivers_raise_not_implemented(provider_cls):
    async def empty() -> AsyncIterator[bytes]:
        if False:
            yield b""

    async def main():
        async def throttle(_: int) -> None:
            return None

        provider = provider_cls.from_credential_json(b"{}")
        with pytest.raises(NotImplementedError):
            await provider.upload_stream("dest", "obj", empty(), 0, throttle)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# GDrive credential validation
# ---------------------------------------------------------------------------

def test_gdrive_requires_service_account_json():
    with pytest.raises(ValueError):
        GoogleDriveProvider.from_credential_json(b"")
    with pytest.raises(ValueError):
        GoogleDriveProvider.from_credential_json(b"not json")
    with pytest.raises(ValueError):
        GoogleDriveProvider.from_credential_json(b'{"type":"oauth2"}')
    with pytest.raises(ValueError):
        GoogleDriveProvider.from_credential_json(
            json.dumps({"type": "service_account"}).encode()
        )


def test_gdrive_accepts_well_formed_json():
    payload = {
        "type": "service_account",
        "client_email": "sa@proj.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    drv = GoogleDriveProvider.from_credential_json(
        json.dumps(payload).encode()
    )
    assert isinstance(drv, GoogleDriveProvider)


# ---------------------------------------------------------------------------
# GDrive upload pipeline (SDK mocked)
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, recorder: list[bytes], stream) -> None:
        self._stream = stream
        self._recorder = recorder
        self._done = False

    def next_chunk(self):
        if self._done:
            return None, types.SimpleNamespace(get=lambda k: "fake-file-id")
        chunk = self._stream.read(8 * 1024 * 1024)
        if not chunk:
            self._done = True
            return None, types.SimpleNamespace(get=lambda k: "fake-file-id")
        self._recorder.append(chunk)
        return None, None


class _FakeFiles:
    def __init__(self, recorder: list[bytes]) -> None:
        self._recorder = recorder
        self.last_body: dict | None = None

    def create(self, body, media_body, fields):
        self.last_body = body
        return _FakeRequest(self._recorder, media_body._fd)


class _FakeService:
    def __init__(self, recorder: list[bytes]) -> None:
        self._files = _FakeFiles(recorder)

    def files(self):
        return self._files


def _install_fake_google_sdk(recorder: list[bytes]) -> None:
    google = types.ModuleType("google")
    google.__path__ = []  # type: ignore[attr-defined]
    google_oauth2 = types.ModuleType("google.oauth2")
    google_oauth2.__path__ = []  # type: ignore[attr-defined]
    google_oauth2_sa = types.ModuleType("google.oauth2.service_account")

    class _Creds:
        @classmethod
        def from_service_account_info(cls, info, scopes):
            return cls()

    google_oauth2_sa.Credentials = _Creds  # type: ignore[attr-defined]
    google_auth = types.ModuleType("google.auth")
    google_auth.__path__ = []  # type: ignore[attr-defined]
    google_auth_transport = types.ModuleType("google.auth.transport")
    google_auth_transport.__path__ = []  # type: ignore[attr-defined]
    google_auth_transport_requests = types.ModuleType(
        "google.auth.transport.requests"
    )

    googleapiclient = types.ModuleType("googleapiclient")
    googleapiclient.__path__ = []  # type: ignore[attr-defined]
    googleapiclient_discovery = types.ModuleType("googleapiclient.discovery")

    def _build(_kind, _ver, credentials=None, cache_discovery=False):
        return _FakeService(recorder)

    googleapiclient_discovery.build = _build  # type: ignore[attr-defined]
    googleapiclient_http = types.ModuleType("googleapiclient.http")

    class _MediaIoBaseUpload:
        def __init__(self, fd, mimetype, chunksize, resumable):
            self._fd = fd

    googleapiclient_http.MediaIoBaseUpload = _MediaIoBaseUpload  # type: ignore[attr-defined]

    for name, module in [
        ("google", google),
        ("google.oauth2", google_oauth2),
        ("google.oauth2.service_account", google_oauth2_sa),
        ("google.auth", google_auth),
        ("google.auth.transport", google_auth_transport),
        ("google.auth.transport.requests", google_auth_transport_requests),
        ("googleapiclient", googleapiclient),
        ("googleapiclient.discovery", googleapiclient_discovery),
        ("googleapiclient.http", googleapiclient_http),
    ]:
        sys.modules[name] = module


@pytest.fixture
def fake_google_sdk(monkeypatch):
    recorder: list[bytes] = []
    _install_fake_google_sdk(recorder)
    yield recorder
    for name in list(sys.modules):
        if name.startswith("google") or name.startswith("googleapiclient"):
            sys.modules.pop(name, None)


def test_gdrive_upload_streams_chunks_and_throttles(fake_google_sdk):
    payload = {
        "type": "service_account",
        "client_email": "sa@proj.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    drv = GoogleDriveProvider.from_credential_json(
        json.dumps(payload).encode()
    )
    chunks_in = [b"a" * 1024, b"b" * 2048, b"c" * 512]
    throttled: list[int] = []

    async def chunk_iter() -> AsyncIterator[bytes]:
        for c in chunks_in:
            yield c

    async def throttle(n: int) -> None:
        throttled.append(n)

    async def main():
        return await drv.upload_stream(
            "folder-id", "deposit-99.enc",
            chunk_iter(), sum(len(c) for c in chunks_in), throttle,
        )

    file_id = asyncio.run(main())
    assert file_id == "fake-file-id"
    assert throttled == [len(c) for c in chunks_in]
    received = b"".join(fake_google_sdk)
    assert received == b"".join(chunks_in)


def test_gdrive_missing_sdk_surfaces_clear_error(monkeypatch):
    """No google modules in sys.modules → upload raises a clear RuntimeError."""
    for name in list(sys.modules):
        if name.startswith("google") or name.startswith("googleapiclient"):
            sys.modules.pop(name, None)
    monkeypatch.setattr(
        "builtins.__import__",
        _make_import_blocker(),
    )

    payload = {
        "type": "service_account",
        "client_email": "sa@proj.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    drv = GoogleDriveProvider.from_credential_json(
        json.dumps(payload).encode()
    )

    async def empty() -> AsyncIterator[bytes]:
        if False:
            yield b""

    async def throttle(_: int) -> None:
        return None

    async def main():
        with pytest.raises(RuntimeError, match="google-api-python-client"):
            await drv.upload_stream("d", "n", empty(), 0, throttle)

    asyncio.run(main())


def _make_import_blocker():
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _blocked(name, *args, **kwargs):
        if name.startswith("google") or name.startswith("googleapiclient"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    return _blocked
