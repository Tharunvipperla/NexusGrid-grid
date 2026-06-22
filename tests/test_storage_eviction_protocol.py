"""Wave 6.3 — wire-protocol additions: extended eviction_response + 3 new frames."""

from __future__ import annotations

import asyncio
import base64

import pytest

from nexus.core import STATE
from nexus.networking.storage_pump import (
    build_storage_cloud_upload_complete,
    build_storage_cloud_upload_failed,
    build_storage_cloud_upload_progress,
    build_storage_eviction_response,
    dispatch_storage_frame,
)


# ---------------------------------------------------------------------------
# Builders carry the right fields
# ---------------------------------------------------------------------------

def test_eviction_response_classic_unchanged():
    """Old call sites that pass only positional args still get the old shape
    (plus empty cloud fields)."""
    frame = build_storage_eviction_response("dep-1", "let_go")
    assert frame["type"] == "storage_eviction_response"
    assert frame["action"] == "let_go"
    assert frame["target_uuid"] == ""
    assert frame["cloud_provider"] == ""
    assert frame["cloud_dest"] == ""
    assert frame["cloud_eviction_nonce_b64"] == ""
    assert frame["cloud_credential_blob_b64"] == ""


def test_eviction_response_forward_unchanged():
    frame = build_storage_eviction_response("dep-1", "forward", "peer-uuid-2")
    assert frame["action"] == "forward"
    assert frame["target_uuid"] == "peer-uuid-2"


def test_eviction_response_cloud_carries_creds():
    nonce = base64.b64encode(b"\x00" * 16).decode()
    blob = base64.b64encode(b"wrapped-credential").decode()
    frame = build_storage_eviction_response(
        "dep-1",
        "cloud",
        cloud_provider="gdrive",
        cloud_dest="folder-xyz",
        cloud_eviction_nonce_b64=nonce,
        cloud_credential_blob_b64=blob,
    )
    assert frame["action"] == "cloud"
    assert frame["cloud_provider"] == "gdrive"
    assert frame["cloud_dest"] == "folder-xyz"
    assert frame["cloud_eviction_nonce_b64"] == nonce
    assert frame["cloud_credential_blob_b64"] == blob


def test_cloud_upload_progress_shape():
    frame = build_storage_cloud_upload_progress("dep-1", 4096, 81920)
    assert frame == {
        "type": "storage_cloud_upload_progress",
        "deposit_id": "dep-1",
        "bytes_sent": 4096,
        "total_bytes": 81920,
    }


def test_cloud_upload_complete_shape():
    frame = build_storage_cloud_upload_complete(
        "dep-1", "drive-file-id-abc", "sha-of-ciphertext"
    )
    assert frame == {
        "type": "storage_cloud_upload_complete",
        "deposit_id": "dep-1",
        "cloud_object_id": "drive-file-id-abc",
        "sha256_uploaded": "sha-of-ciphertext",
    }


def test_cloud_upload_failed_shape():
    frame = build_storage_cloud_upload_failed("dep-1", "provider_unsupported")
    assert frame == {
        "type": "storage_cloud_upload_failed",
        "deposit_id": "dep-1",
        "reason": "provider_unsupported",
    }


# ---------------------------------------------------------------------------
# Dispatcher round-trips the new frames to the workflow handler
# ---------------------------------------------------------------------------

@pytest.fixture
def workflow_handler_recorder():
    """Install a workflow-handler stub that records every frame."""
    seen: list[tuple[str, dict]] = []

    async def _stub(peer_uuid: str, frame: dict) -> None:
        seen.append((peer_uuid, frame))

    prior = getattr(STATE, "foreign_storage_workflow_handler", None)
    setattr(STATE, "foreign_storage_workflow_handler", _stub)
    yield seen
    setattr(STATE, "foreign_storage_workflow_handler", prior)


def test_dispatcher_routes_cloud_eviction_response(workflow_handler_recorder):
    frame = build_storage_eviction_response(
        "dep-1", "cloud", cloud_provider="gdrive"
    )
    asyncio.run(dispatch_storage_frame("peer-x", frame))
    assert len(workflow_handler_recorder) == 1
    seen_peer, seen_frame = workflow_handler_recorder[0]
    assert seen_peer == "peer-x"
    assert seen_frame["action"] == "cloud"


def test_dispatcher_routes_cloud_progress(workflow_handler_recorder):
    asyncio.run(
        dispatch_storage_frame(
            "peer-x", build_storage_cloud_upload_progress("d", 1, 2)
        )
    )
    assert workflow_handler_recorder[0][1]["type"] == (
        "storage_cloud_upload_progress"
    )


def test_dispatcher_routes_cloud_complete(workflow_handler_recorder):
    asyncio.run(
        dispatch_storage_frame(
            "peer-x", build_storage_cloud_upload_complete("d", "id")
        )
    )
    assert workflow_handler_recorder[0][1]["type"] == (
        "storage_cloud_upload_complete"
    )


def test_dispatcher_routes_cloud_failed(workflow_handler_recorder):
    asyncio.run(
        dispatch_storage_frame(
            "peer-x", build_storage_cloud_upload_failed("d", "boom")
        )
    )
    assert workflow_handler_recorder[0][1]["type"] == (
        "storage_cloud_upload_failed"
    )


def test_dispatcher_drops_non_storage_frames(workflow_handler_recorder):
    asyncio.run(
        dispatch_storage_frame("peer-x", {"type": "task_result", "id": "x"})
    )
    assert workflow_handler_recorder == []
