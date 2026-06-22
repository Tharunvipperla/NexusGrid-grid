"""Wave 7.1 — session-key cache + TTL GC + scrubbing."""

from __future__ import annotations

import time

import pytest

from nexus.runtime import foreign_storage_keys


@pytest.fixture(autouse=True)
def _isolate_cache():
    foreign_storage_keys.reset_for_testing()
    yield
    foreign_storage_keys.reset_for_testing()


def test_store_and_get_round_trip():
    foreign_storage_keys.store("dep-a", b"\x01" * 32, file_path="/tmp/f")
    assert foreign_storage_keys.get("dep-a") == b"\x01" * 32
    entry = foreign_storage_keys.get_entry("dep-a")
    assert entry["file_path"] == "/tmp/f"


def test_get_returns_none_when_absent():
    assert foreign_storage_keys.get("missing") is None
    assert not foreign_storage_keys.is_unlocked("missing")


def test_get_bumps_last_used_at():
    foreign_storage_keys.store("dep-b", b"\x02" * 32)
    first = foreign_storage_keys.get_entry("dep-b")["last_used_at"]
    time.sleep(0.01)
    foreign_storage_keys.get("dep-b")
    second = foreign_storage_keys.get_entry("dep-b")["last_used_at"]
    assert second > first


def test_drop_zeros_underlying_bytes():
    """The bytearray must be wiped before dropping the entry."""
    foreign_storage_keys.store("dep-c", b"\xAA" * 32)
    entry = foreign_storage_keys.get_entry("dep-c")
    backing = entry["key"]
    assert isinstance(backing, bytearray)
    assert bytes(backing) == b"\xAA" * 32
    foreign_storage_keys.drop("dep-c")
    assert bytes(backing) == b"\x00" * 32  # in-place scrub
    assert foreign_storage_keys.get("dep-c") is None


def test_store_replaces_and_scrubs_previous_key():
    foreign_storage_keys.store("dep-d", b"\xAA" * 32)
    prev = foreign_storage_keys.get_entry("dep-d")["key"]
    foreign_storage_keys.store("dep-d", b"\xBB" * 32)
    # The old bytearray should have been zeroed before the entry was replaced.
    assert bytes(prev) == b"\x00" * 32
    assert foreign_storage_keys.get("dep-d") == b"\xBB" * 32


def test_gc_evicts_entries_idle_past_ttl():
    foreign_storage_keys.store("idle-1", b"\x11" * 32)
    foreign_storage_keys.store("idle-2", b"\x22" * 32)
    # Force last_used_at into the past.
    foreign_storage_keys.get_entry("idle-1")["last_used_at"] = (
        time.monotonic() - 9999
    )
    foreign_storage_keys.get_entry("idle-2")["last_used_at"] = time.monotonic()

    evicted = foreign_storage_keys.gc(idle_ttl_s=300)
    assert evicted == ["idle-1"]
    assert foreign_storage_keys.get("idle-1") is None
    assert foreign_storage_keys.get("idle-2") == b"\x22" * 32


def test_list_unlocked_does_not_leak_key_material():
    foreign_storage_keys.store("dep-e", b"\xCD" * 32)
    foreign_storage_keys.store("dep-f", b"\xEF" * 32)

    listed = foreign_storage_keys.list_unlocked()
    ids = {row["deposit_id"] for row in listed}
    assert ids == {"dep-e", "dep-f"}
    for row in listed:
        assert "key" not in row
        assert "encrypted_blob" not in row
        assert "unlocked_at" in row
        assert "last_used_at" in row


def test_drop_returns_false_when_absent():
    assert foreign_storage_keys.drop("never-stored") is False
