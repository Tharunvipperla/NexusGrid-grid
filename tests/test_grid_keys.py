"""Wave 40 — per-context grid_key derivation."""

from __future__ import annotations

from nexus.security.grid_keys import (
    GRID_KEY_LEN,
    derive_group_grid_key,
    derive_pair_grid_key,
)


class TestGroupGridKey:
    def test_stable_for_same_group_id(self):
        gid = "group-abc-123"
        assert derive_group_grid_key(gid) == derive_group_grid_key(gid)

    def test_different_groups_produce_different_keys(self):
        a = derive_group_grid_key("group-a")
        b = derive_group_grid_key("group-b")
        assert a != b
        assert len(a) == GRID_KEY_LEN
        assert len(b) == GRID_KEY_LEN

    def test_empty_group_id_returns_empty_string(self):
        assert derive_group_grid_key("") == ""
        assert derive_group_grid_key("   ") == ""

    def test_only_lowercase_hex(self):
        k = derive_group_grid_key("anything")
        assert all(c in "0123456789abcdef" for c in k)


class TestPairGridKey:
    def test_order_independent(self):
        alice = "alicepubkey1234567890"
        bob = "bobpubkey0987654321"
        assert derive_pair_grid_key(alice, bob) == derive_pair_grid_key(bob, alice)

    def test_different_pairs_produce_different_keys(self):
        alice = "alice"
        bob = "bob"
        charlie = "charlie"
        assert derive_pair_grid_key(alice, bob) != derive_pair_grid_key(alice, charlie)
        assert derive_pair_grid_key(alice, bob) != derive_pair_grid_key(bob, charlie)

    def test_empty_pubkey_returns_empty_string(self):
        assert derive_pair_grid_key("", "bob") == ""
        assert derive_pair_grid_key("alice", "") == ""

    def test_pair_and_group_keys_dont_collide(self):
        # The "nexus:pair:" vs "nexus:group:" namespacing in the digest
        # input means a pair (a, b) and a group named "a|b" never produce
        # the same key. This is a smoke check against a future refactor
        # silently dropping the namespace.
        pair = derive_pair_grid_key("a", "b")
        group = derive_group_grid_key("a|b")
        assert pair != group
