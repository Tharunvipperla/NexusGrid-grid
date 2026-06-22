"""Backblaze B2 driver — stub. Real implementation in a future wave."""

from __future__ import annotations

from typing import AsyncIterator

from nexus.storage.cloud.base import CloudProvider, ThrottleAcquire, register


@register
class B2Provider(CloudProvider):
    name = "b2"

    @classmethod
    def from_credential_json(cls, raw: bytes) -> "B2Provider":
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
            "B2 driver lands in a future wave — see plan"
        )


__all__ = ["B2Provider"]
