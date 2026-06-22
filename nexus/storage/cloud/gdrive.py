"""Google Drive driver for cloud-eviction tier.

The Google client SDK is imported lazily inside
:meth:`GoogleDriveProvider.from_credential_json` so the rest of the app
boots without the optional `google-api-python-client` / `google-auth`
dependencies.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any, AsyncIterator

from nexus.storage.cloud.base import CloudProvider, ThrottleAcquire, register

_CHUNK_BYTES = 8 * 1024 * 1024  # 8 MiB resumable-upload window
_DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024  # 4 MiB per Drive download slice
_FOLDER_MIME = "application/vnd.google-apps.folder"


class _AsyncChunkBuffer(io.RawIOBase):
    """Sync-stream wrapper around an async chunk iterator.

    `MediaIoBaseUpload` reads via a blocking `read(size)` call. The chunk
    iterator is async, so each `read` schedules the next chunk on the
    event loop via :func:`asyncio.run_coroutine_threadsafe`.
    """

    def __init__(
        self,
        chunks: AsyncIterator[bytes],
        loop: asyncio.AbstractEventLoop,
        throttle_acquire: ThrottleAcquire,
    ) -> None:
        self._chunks = chunks
        self._loop = loop
        self._throttle = throttle_acquire
        self._buf = bytearray()
        self._exhausted = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        target = size if size and size > 0 else _CHUNK_BYTES
        while not self._exhausted and len(self._buf) < target:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._chunks.__anext__(), self._loop
                )
                chunk = future.result()
            except StopAsyncIteration:
                self._exhausted = True
                break
            asyncio.run_coroutine_threadsafe(
                self._throttle(len(chunk)), self._loop
            ).result()
            self._buf.extend(chunk)
        out = bytes(self._buf[:target])
        del self._buf[:target]
        return out


@register
class GoogleDriveProvider(CloudProvider):
    name = "gdrive"

    def __init__(self, credentials_info: dict[str, Any]) -> None:
        self._info = credentials_info

    @classmethod
    def from_credential_json(cls, raw: bytes) -> "GoogleDriveProvider":
        try:
            info = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"credential JSON unparseable: {exc}") from exc
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise ValueError("expected a Google service-account JSON")
        for required in ("client_email", "private_key", "token_uri"):
            if not info.get(required):
                raise ValueError(f"service-account JSON missing '{required}'")
        return cls(info)

    async def upload_stream(
        self,
        dest: str,
        object_name: str,
        chunks: AsyncIterator[bytes],
        total_bytes: int,
        throttle_acquire: ThrottleAcquire,
    ) -> str:
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaIoBaseUpload
        except ImportError as exc:
            raise RuntimeError(
                "google-api-python-client + google-auth required for the "
                "GDrive cloud-eviction tier; pip install them or pick a "
                "different provider"
            ) from exc

        loop = asyncio.get_running_loop()
        creds = service_account.Credentials.from_service_account_info(
            self._info, scopes=["https://www.googleapis.com/auth/drive.file"]
        )
        service = await asyncio.to_thread(
            build, "drive", "v3", credentials=creds, cache_discovery=False
        )
        stream = _AsyncChunkBuffer(chunks, loop, throttle_acquire)
        media = MediaIoBaseUpload(
            stream,
            mimetype="application/octet-stream",
            chunksize=_CHUNK_BYTES,
            resumable=True,
        )
        body: dict[str, Any] = {"name": object_name}
        if dest:
            body["parents"] = [dest]
        request = service.files().create(
            body=body, media_body=media, fields="id"
        )

        def _drive_upload() -> str:
            response = None
            while response is None:
                _status, response = request.next_chunk()
            return str(response.get("id") or "")

        return await asyncio.to_thread(_drive_upload)

    async def download_folder(
        self,
        folder_id: str,
        dest_dir: Path,
        throttle_acquire: ThrottleAcquire,
    ) -> tuple[int, int]:
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaIoBaseDownload
        except ImportError as exc:
            raise RuntimeError(
                "google-api-python-client + google-auth required for the "
                "GDrive task-data tier; pip install them or pick a "
                "different provider"
            ) from exc

        loop = asyncio.get_running_loop()
        creds = service_account.Credentials.from_service_account_info(
            self._info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        service = await asyncio.to_thread(
            build, "drive", "v3", credentials=creds, cache_discovery=False
        )

        def _list_children(parent_id: str) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            page_token: str | None = None
            while True:
                resp = (
                    service.files()
                    .list(
                        q=f"'{parent_id}' in parents and trashed=false",
                        fields="nextPageToken, files(id, name, mimeType, size)",
                        pageSize=1000,
                        pageToken=page_token,
                    )
                    .execute()
                )
                items.extend(resp.get("files", []) or [])
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            return items

        def _walk(parent_id: str, prefix: Path) -> list[tuple[dict, Path]]:
            """Return [(file_meta, relative_dest_path), ...] for non-folder files."""
            collected: list[tuple[dict, Path]] = []
            stack: list[tuple[str, Path]] = [(parent_id, prefix)]
            while stack:
                pid, rel = stack.pop()
                for child in _list_children(pid):
                    name = str(child.get("name") or "").strip()
                    if not name or name in (".", "..") or "/" in name or "\\" in name:
                        continue
                    if child.get("mimeType") == _FOLDER_MIME:
                        stack.append((str(child["id"]), rel / name))
                    else:
                        collected.append((child, rel / name))
            return collected

        files = await asyncio.to_thread(_walk, folder_id, Path())

        file_count = 0
        byte_count = 0
        dest_dir.mkdir(parents=True, exist_ok=True)
        for meta, rel_path in files:
            target = dest_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            req = service.files().get_media(fileId=str(meta["id"]))
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(
                buf, req, chunksize=_DOWNLOAD_CHUNK_BYTES
            )

            done = False
            prev = 0
            while not done:
                status, done = await asyncio.to_thread(downloader.next_chunk)
                progress = int(getattr(status, "resumable_progress", 0)) if status else 0
                if not status and done:
                    progress = buf.tell()
                delta = max(0, progress - prev)
                if delta:
                    await throttle_acquire(delta)
                prev = progress

            data = buf.getvalue()
            await asyncio.to_thread(target.write_bytes, data)
            file_count += 1
            byte_count += len(data)

        return file_count, byte_count


__all__ = ["GoogleDriveProvider"]
