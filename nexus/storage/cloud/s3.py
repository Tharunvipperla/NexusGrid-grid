"""AWS S3 driver — stub. Real implementation in a future wave."""

from __future__ import annotations

from typing import AsyncIterator

from nexus.storage.cloud.base import CloudProvider, ThrottleAcquire, register


@register
class S3Provider(CloudProvider):
    name = "s3"

    @classmethod
    def from_credential_json(cls, raw: bytes) -> "S3Provider":
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
            "S3 driver lands in a future wave — see plan"
        )


__all__ = ["S3Provider"]
