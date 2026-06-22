"""Cloud-provider abstraction for the eviction-side upload path.

Each provider exposes a single async streaming upload entry-point that takes
an async iterator of ciphertext chunks (the host never sees plaintext) and
the throttle hook so cloud uploads share the same busy/idle bandwidth
profile as P2P transfers.

Drivers register themselves in :data:`PROVIDERS` so the depositor UI can
enumerate available targets. ships the GDrive driver only; the
others stub :meth:`upload_stream` with ``NotImplementedError``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

ThrottleAcquire = Callable[[int], Awaitable[None]]

PROVIDERS: dict[str, "type[CloudProvider]"] = {}


def register(cls: "type[CloudProvider]") -> "type[CloudProvider]":
    """Class decorator: add ``cls`` to the global :data:`PROVIDERS` registry."""
    if not cls.name:
        raise ValueError(f"{cls.__name__} must set a non-empty 'name'")
    PROVIDERS[cls.name] = cls
    return cls


class CloudProvider(ABC):
    """Abstract async streaming upload to an external object store."""

    name: str = ""

    @classmethod
    @abstractmethod
    def from_credential_json(cls, raw: bytes) -> "CloudProvider":
        """Construct a driver instance from a provider-specific JSON blob."""
        raise NotImplementedError

    @abstractmethod
    async def upload_stream(
        self,
        dest: str,
        object_name: str,
        chunks: AsyncIterator[bytes],
        total_bytes: int,
        throttle_acquire: ThrottleAcquire,
    ) -> str:
        """Upload ``chunks`` to ``dest`` under ``object_name``.

        Returns the provider's object id (gdrive file id, S3 key, etc.).
        Implementations must ``await throttle_acquire(len(chunk))`` before
        sending each chunk so cloud uploads share the global storage
        bandwidth profile.
        """
        raise NotImplementedError

    async def download_folder(
        self,
        folder_id: str,
        dest_dir: Path,
        throttle_acquire: ThrottleAcquire,
    ) -> tuple[int, int]:
        """Recursively download a provider folder into ``dest_dir``.

        Preserves the source folder's subdirectory structure. Returns
        ``(file_count, byte_count)``. Implementations must
        ``await throttle_acquire(n)`` per chunk for throughput parity.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not yet implement download_folder"
        )


__all__ = ["CloudProvider", "PROVIDERS", "ThrottleAcquire", "register"]
