"""Foreign-storage T&Cs — minimal version (lays the surface).

The 5b.6 commit fills in canonical signature material + edge-case
copy. For now we expose host / depositor default text and a
``host_terms_text()`` helper that picks up the user's override from
``LOCAL_SETTINGS``.
"""

from __future__ import annotations

from nexus.core import LOCAL_SETTINGS

DEFAULT_HOST_TERMS = (
    "I agree to keep the depositor's encrypted bundle until natural TTL "
    "expiry or until I issue an eviction request. On eviction the "
    "depositor has 1 day to respond (download / forward / let-it-go); "
    "if they don't, the bytes move to my local DB grace for 2 days, then "
    "are permanently deleted. I cannot read the deposit's plaintext."
)

DEFAULT_DEPOSITOR_TERMS = (
    "I am responsible for my session password — losing it makes the "
    "files unrecoverable. I will not deposit content that violates the "
    "host's policies. I understand the host can request eviction at any "
    "time and that I have 1 day to respond; missing the window moves "
    "my bytes to a 2-day DB grace and then permanent deletion."
)


def host_terms_text() -> str:
    """Return the host's T&C text — operator override if set, else default."""
    override = LOCAL_SETTINGS.get("foreign_storage_host_terms") or ""
    return override.strip() or DEFAULT_HOST_TERMS


def depositor_terms_text() -> str:
    return DEFAULT_DEPOSITOR_TERMS


def signing_material(deposit_id: str, terms_sha256: str) -> str:
    """Canonical material for HMAC signature over T&C accept.

    Both depositor and host sign over ``"foreign_storage_terms|<id>|<sha>"``;
    the existing :func:`nexus.security.crypto.sign_bytes` helper supplies
    the HMAC of this material.
    """
    return f"foreign_storage_terms|{deposit_id}|{terms_sha256}"


__all__ = [
    "DEFAULT_HOST_TERMS",
    "DEFAULT_DEPOSITOR_TERMS",
    "host_terms_text",
    "depositor_terms_text",
    "signing_material",
]
