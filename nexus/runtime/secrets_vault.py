"""C4: node-local secrets vault.

A small encrypted store for task/service env secrets. Run-spec env is "no
secrets" today — values are echoed back over the API and can travel in
specs. The vault fixes that: store a secret once (encrypted at rest with the
same ``cred_crypto`` wrap the cloud-credential tier uses), then reference it
from a spec as ``secret://NAME``. The plaintext is **never** returned over
the API and is only resolved to its value at execution time on the node that
runs the work.

Design notes:

* The value is wrapped with :func:`nexus.security.cred_crypto.wrap_credential_blob`
  (AES-256-GCM keyed off ``.nexus_secret``) — same at-rest scheme, audited
  and tamper-evident.
* ``name`` is the reference key and must look like an env var
  (``[A-Z_][A-Z0-9_]*``) so ``secret://NAME`` is unambiguous and safe to
  splice into ``KEY=secret://NAME`` env entries.
* Resolution (:func:`resolve_refs`) only ever *consumes* secrets to build a
  runtime env; it bumps ``last_used_at`` for audit and never logs values.

Remote delivery (dispatching a task that needs a secret to a *different*
worker) is a follow-up: it reuses ``cred_crypto.wrap_task_data_for_transit``
exactly like the Wave-9 task-data credential path. v1 covers the vault +
local resolution, which is what A1 (custom build context) and A2 (cloud
connector) build on.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from nexus.utils.time import iso_now

_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_REF_PREFIX = "secret://"


class SecretError(Exception):
    """Raised for invalid names or unknown references."""


def valid_name(name: str) -> bool:
    """True if *name* is a legal secret key (env-var shaped)."""
    return bool(_NAME_RE.fullmatch(name or ""))


def is_ref(value: object) -> bool:
    """True if *value* is a ``secret://NAME`` reference string."""
    return isinstance(value, str) and value.startswith(_REF_PREFIX)


def ref_name(value: str) -> str:
    """Extract ``NAME`` from a ``secret://NAME`` reference."""
    return value[len(_REF_PREFIX):] if is_ref(value) else ""


async def set_secret(name: str, value: str, description: str = "") -> None:
    """Create or replace the secret *name* with *value* (encrypted at rest)."""
    from nexus.security.cred_crypto import wrap_credential_blob
    from nexus.storage import Secret, get_session

    if not valid_name(name):
        raise SecretError(
            "name must be UPPER_SNAKE_CASE (A-Z, 0-9, _), 1-128 chars"
        )
    blob = wrap_credential_blob(str(value).encode("utf-8"))
    now = iso_now()
    async with get_session() as db:
        row = (
            await db.execute(select(Secret).filter(Secret.name == name))
        ).scalar_one_or_none()
        if row is None:
            db.add(
                Secret(
                    name=name,
                    encrypted_blob=blob,
                    description=str(description or "")[:200],
                    created_at=now,
                    updated_at=now,
                    last_used_at="",
                )
            )
        else:
            row.encrypted_blob = blob
            if description:
                row.description = str(description)[:200]
            row.updated_at = now
        await db.commit()


async def list_secrets() -> list[dict]:
    """Names + metadata only — **never** the values."""
    from nexus.storage import Secret, get_session

    async with get_session() as db:
        rows = (await db.execute(select(Secret))).scalars().all()
    return [
        {
            "name": r.name,
            "description": r.description or "",
            "created_at": r.created_at or "",
            "updated_at": r.updated_at or "",
            "last_used_at": r.last_used_at or "",
        }
        for r in sorted(rows, key=lambda r: r.name)
    ]


async def delete_secret(name: str) -> bool:
    """Remove a secret. Returns True if a row was deleted."""
    from nexus.storage import Secret, get_session

    async with get_session() as db:
        row = (
            await db.execute(select(Secret).filter(Secret.name == name))
        ).scalar_one_or_none()
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
    return True


async def get_value(name: str, *, mark_used: bool = True) -> str | None:
    """Decrypt and return the secret value (internal use). ``None`` if absent."""
    from sqlalchemy.orm import undefer

    from nexus.security.cred_crypto import unwrap_credential_blob
    from nexus.storage import Secret, get_session

    async with get_session() as db:
        row = (
            await db.execute(
                select(Secret)
                .options(undefer(Secret.encrypted_blob))
                .filter(Secret.name == name)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        blob = bytes(row.encrypted_blob or b"")
        if mark_used:
            row.last_used_at = iso_now()
            await db.commit()
    try:
        return unwrap_credential_blob(blob).decode("utf-8")
    except Exception:
        return None


async def resolve_refs(env):
    """Resolve ``secret://NAME`` references in a run-spec env to their values.

    Accepts either a list of ``"KEY=VALUE"`` strings or a ``{KEY: VALUE}``
    dict and returns the same shape with any ``secret://NAME`` values
    replaced by the decrypted secret. Non-reference values pass through
    untouched, so specs with no secrets are unchanged. An unknown reference
    raises :class:`SecretError` so a missing secret fails loudly rather than
    silently shipping the literal ``secret://NAME`` into the workload.
    """
    if isinstance(env, dict):
        out: dict = {}
        for k, v in env.items():
            out[k] = await _resolve_one(v)
        return out
    if isinstance(env, (list, tuple)):
        out_list: list = []
        for item in env:
            if isinstance(item, str) and "=" in item:
                k, _, v = item.partition("=")
                out_list.append(f"{k}={await _resolve_one(v)}")
            else:
                out_list.append(item)
        return out_list
    return env


async def _resolve_one(value):
    if not is_ref(value):
        return value
    name = ref_name(value)
    val = await get_value(name)
    if val is None:
        raise SecretError(f"unknown secret: {name}")
    return val


__all__ = [
    "SecretError",
    "valid_name",
    "is_ref",
    "ref_name",
    "set_secret",
    "list_secrets",
    "delete_secret",
    "get_value",
    "resolve_refs",
]
