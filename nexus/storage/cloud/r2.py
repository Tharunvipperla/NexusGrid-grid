"""Cloudflare R2 driver — stub. Real implementation in a future wave."""

from __future__ import annotations

from typing import AsyncIterator

from nexus.storage.cloud.base import CloudProvider, ThrottleAcquire, register


@register
class R2Provider(CloudProvider):
    name = "r2"

    @classmethod
    def from_credential_json(cls, raw: bytes) -> "R2Provider":
        return cls()

    async def upload_stream(
        self,
        dest: str,
        object_name: str,
        chunks: AsyncIterator[bytes],
        total_bytes: int,
        throttle_acquire: ThrottleAcquire,
    ) -> str:
        raise NotImplementedError(
            "R2 driver lands in a future wave — see plan"
        )


__all__ = ["R2Provider"]
