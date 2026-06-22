"""Wave 9.7: validator + worker fetcher tests for cloud task-data sources."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from pathlib import Path

import pytest

from nexus.networking.worker_client import _fetch_data_sources
from nexus.runtime.service_runner import (
    ServiceManifestError,
    validate_data_sources,
)
from nexus.security.cred_crypto import wrap_task_data_for_transit
from nexus.storage.cloud import PROVIDERS
from nexus.storage.cloud.base import CloudProvider, ThrottleAcquire


# ---------------------------------------------------------------------------
# validate_data_sources
# ---------------------------------------------------------------------------

def test_validate_data_sources_happy_path():
    out = validate_data_sources({
        "data_sources": [
            {"type": "gdrive", "credential_id": "cred-1", "folder_id": "F1", "mount_path": "data/"},
            {"type": "gdrive", "credential_id": "cred-2", "folder_id": "F2"},
        ],
        "workspace_source": {"type": "gdrive", "credential_id": "cred-3", "folder_id": "F3"},
    })
    assert len(out["data_sources"]) == 2
    assert out["data_sources"][0]["mount_path"] == "data/"
    assert out["data_sources"][1]["mount_path"] == ""
    assert out["workspace_source"]["folder_id"] == "F3"


def test_validate_data_sources_empty_manifest_returns_empty():
    out = validate_data_sources({})
    assert out == {"data_sources": [], "workspace_source": None}


def test_validate_data_sources_rejects_parent_traversal():
    with pytest.raises(ServiceManifestError, match="must not contain '..'"):
        validate_data_sources({
            "data_sources": [{
                "type": "gdrive", "credential_id": "c", "folder_id": "F",
                "mount_path": "../etc",
            }],
        })


def test_validate_data_sources_rejects_absolute_mount_path():
    with pytest.raises(ServiceManifestError, match="must be relative"):
        validate_data_sources({
            "data_sources": [{
                "type": "gdrive", "credential_id": "c", "folder_id": "F",
                "mount_path": "/etc/passwd",
            }],
        })


def test_validate_data_sources_rejects_unknown_type():
    with pytest.raises(ServiceManifestError, match="type must be one of"):
        validate_data_sources({
            "data_sources": [{
                "type": "ftp", "credential_id": "c", "folder_id": "F",
            }],
        })


def test_validate_data_sources_rejects_missing_credential_id():
    with pytest.raises(ServiceManifestError, match="credential_id is required"):
        validate_data_sources({
            "data_sources": [{"type": "gdrive", "folder_id": "F"}],
        })


def test_validate_data_sources_rejects_invalid_credential_chars():
    with pytest.raises(ServiceManifestError, match="invalid chars"):
        validate_data_sources({
            "data_sources": [{
                "type": "gdrive", "credential_id": "bad cred!", "folder_id": "F",
            }],
        })


def test_validate_data_sources_rejects_missing_folder_id():
    with pytest.raises(ServiceManifestError, match="folder_id is required"):
        validate_data_sources({
            "data_sources": [{"type": "gdrive", "credential_id": "c"}],
        })


# ---------------------------------------------------------------------------
# Worker-side fetcher
# ---------------------------------------------------------------------------

class _FakeProvider(CloudProvider):
    """Records the credential bytes it sees and writes a deterministic tree."""

    name = "fake-test"
    last_creds: bytes | None = None
    fail_next: bool = False
    files: dict[str, bytes] = {"hello.txt": b"hello\n", "sub/dir/data.csv": b"a,b\n1,2\n"}

    @classmethod
    def from_credential_json(cls, raw: bytes) -> "_FakeProvider":
        cls.last_creds = bytes(raw)
        return cls()

    async def upload_stream(
        self, dest, object_name, chunks, total_bytes, throttle_acquire,
    ) -> str:
        return "noop"

    async def download_folder(
        self, folder_id: str, dest_dir: Path, throttle_acquire: ThrottleAcquire,
    ) -> tuple[int, int]:
        if _FakeProvider.fail_next:
            _FakeProvider.fail_next = False
            raise RuntimeError("synthetic provider failure")
        dest_dir.mkdir(parents=True, exist_ok=True)
        total = 0
        for rel, data in _FakeProvider.files.items():
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            await throttle_acquire(len(data))
            total += len(data)
        return len(_FakeProvider.files), total


@pytest.fixture
def fake_provider():
    PROVIDERS["fake-test"] = _FakeProvider
    _FakeProvider.last_creds = None
    _FakeProvider.fail_next = False
    yield _FakeProvider
    PROVIDERS.pop("fake-test", None)


def _build_creds_env(peer_signing_key: str, mapping: dict[str, bytes]) -> tuple[str, bytes]:
    nonce = secrets.token_bytes(16)
    out = {}
    for cid, plaintext in mapping.items():
        out[cid] = base64.b64encode(
            wrap_task_data_for_transit(peer_signing_key, nonce, plaintext)
        ).decode("ascii")
    payload = json.dumps({
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "credentials": out,
    })
    return payload, nonce


def _seed_workspace(tmp_path, manifest: dict) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return ws


def test_fetcher_downloads_data_sources_and_workspace_source(tmp_path, fake_provider):
    creds_plain = b'{"type":"service_account","client_email":"x@y"}'
    PEER_KEY = "peer-signing-key-abc"
    manifest = {
        "data_sources": [{
            "type": "fake-test", "credential_id": "cid1",
            "folder_id": "FOLDER", "mount_path": "data",
        }],
    }
    ws = _seed_workspace(tmp_path, manifest)
    env_payload, _ = _build_creds_env(PEER_KEY, {"cid1": creds_plain})

    asyncio.run(_fetch_data_sources(
        str(ws), {"NEXUS_TASK_DATA_CREDS": env_payload}, PEER_KEY,
        task_id="t1", master_ip="10.0.0.1:9000",
    ))

    # Provider saw the original credential bytes.
    assert _FakeProvider.last_creds == creds_plain
    # Files landed under the mount_path.
    assert (ws / "data" / "hello.txt").read_bytes() == b"hello\n"
    assert (ws / "data" / "sub" / "dir" / "data.csv").exists()


def test_fetcher_workspace_source_writes_to_root(tmp_path, fake_provider):
    PEER_KEY = "peer-signing-key-abc"
    manifest = {
        "workspace_source": {
            "type": "fake-test", "credential_id": "cid1", "folder_id": "F",
        },
    }
    ws = _seed_workspace(tmp_path, manifest)
    env_payload, _ = _build_creds_env(PEER_KEY, {"cid1": b"{}"})

    asyncio.run(_fetch_data_sources(
        str(ws), {"NEXUS_TASK_DATA_CREDS": env_payload}, PEER_KEY,
        task_id="t1", master_ip="10.0.0.1:9000",
    ))
    # Workspace-source landed at the workspace root, not under a subdir.
    assert (ws / "hello.txt").read_bytes() == b"hello\n"


def test_fetcher_no_op_when_manifest_has_no_sources(tmp_path, fake_provider):
    ws = _seed_workspace(tmp_path, {"entrypoint": "python main.py"})
    asyncio.run(_fetch_data_sources(
        str(ws), {}, "peer-key", task_id="t1", master_ip="10.0.0.1:9000",
    ))
    assert _FakeProvider.last_creds is None


def test_fetcher_raises_when_creds_env_missing(tmp_path, fake_provider):
    manifest = {
        "data_sources": [{
            "type": "fake-test", "credential_id": "cid1", "folder_id": "F",
        }],
    }
    ws = _seed_workspace(tmp_path, manifest)
    with pytest.raises(RuntimeError, match="no NEXUS_TASK_DATA_CREDS"):
        asyncio.run(_fetch_data_sources(
            str(ws), {}, "peer-key", task_id="t1", master_ip="10.0.0.1:9000",
        ))


def test_fetcher_raises_for_unknown_provider(tmp_path):
    """If the manifest names a provider that isn't registered, fail fast."""
    PEER_KEY = "peer-signing-key-abc"
    manifest = {
        "data_sources": [{
            "type": "no-such-provider", "credential_id": "cid1", "folder_id": "F",
        }],
    }
    ws = _seed_workspace(tmp_path, manifest)
    env_payload, _ = _build_creds_env(PEER_KEY, {"cid1": b"{}"})
    with pytest.raises(RuntimeError, match="unknown provider"):
        asyncio.run(_fetch_data_sources(
            str(ws), {"NEXUS_TASK_DATA_CREDS": env_payload}, PEER_KEY,
            task_id="t1", master_ip="10.0.0.1:9000",
        ))


def test_fetcher_propagates_provider_failure(tmp_path, fake_provider):
    """A provider that raises mid-fetch must abort the whole call."""
    _FakeProvider.fail_next = True
    PEER_KEY = "peer-signing-key-abc"
    manifest = {
        "data_sources": [{
            "type": "fake-test", "credential_id": "cid1", "folder_id": "F",
        }],
    }
    ws = _seed_workspace(tmp_path, manifest)
    env_payload, _ = _build_creds_env(PEER_KEY, {"cid1": b"{}"})
    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        asyncio.run(_fetch_data_sources(
            str(ws), {"NEXUS_TASK_DATA_CREDS": env_payload}, PEER_KEY,
            task_id="t1", master_ip="10.0.0.1:9000",
        ))


def test_fetcher_zeroizes_credential_bytearray(tmp_path, fake_provider, monkeypatch):
    """The `bytearray` holding the unwrapped credential is zeroed in `finally`.

    We hold a weakref to the bytearray via the fetcher's local dict; after
    the fetch completes, the buffer must be all-zeros (and ultimately
    dropped). We hook the fetcher's local zeroize step indirectly by
    snapshotting the bytes on the way through ``download_folder``.
    """
    captured: list[bytearray] = []

    class _CaptureProvider(_FakeProvider):
        name = "cap-test"

        @classmethod
        def from_credential_json(cls, raw: bytes) -> "_CaptureProvider":
            return cls()

        async def download_folder(self, folder_id, dest_dir, throttle_acquire):
            # Walk up the call chain to find the fetcher's `cred_plaintexts`
            # dict, then keep a reference to each live bytearray so we can
            # probe it after the finally block runs.
            import inspect
            frame = inspect.currentframe()
            while frame is not None:
                if "cred_plaintexts" in frame.f_locals:
                    for buf in frame.f_locals["cred_plaintexts"].values():
                        captured.append(buf)
                    break
                frame = frame.f_back
            return await super().download_folder(folder_id, dest_dir, throttle_acquire)

    PROVIDERS["cap-test"] = _CaptureProvider
    try:
        PEER_KEY = "peer-signing-key-abc"
        manifest = {
            "data_sources": [{
                "type": "cap-test", "credential_id": "cid1", "folder_id": "F",
            }],
        }
        ws = _seed_workspace(tmp_path, manifest)
        env_payload, _ = _build_creds_env(PEER_KEY, {"cid1": b"top-secret-credentials"})

        asyncio.run(_fetch_data_sources(
            str(ws), {"NEXUS_TASK_DATA_CREDS": env_payload}, PEER_KEY,
            task_id="t1", master_ip="10.0.0.1:9000",
        ))

        assert captured, "did not capture the credential buffer"
        # Buffer is zeroed after the finally block runs.
        for buf in captured:
            assert all(b == 0 for b in buf), f"buffer not zeroed: {bytes(buf)!r}"
    finally:
        PROVIDERS.pop("cap-test", None)
