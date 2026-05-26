"""Blob storage backends for B2.

Two backends implement BlobStorageBackend (Protocol):
- LocalFileStoreBackend: file-store://{rest} -> {root}/{rest} on disk.
- GcsBlobStorageBackend: gs://{bucket}/{key} -> google-cloud-storage upload/download.

The orchestrator selects the backend by URI scheme; mixed-scheme runs are
not supported in a single orchestrator invocation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class BlobStorageBackend(Protocol):
    def upload(self, *, storage_uri: str, content: bytes) -> None: ...
    def get_bytes(self, *, storage_uri: str) -> bytes: ...


_FILE_STORE_PREFIX = "file-store://"


class LocalFileStoreBackend:
    def __init__(self, *, root: Path) -> None:
        self._root = Path(root)

    def _path_for(self, storage_uri: str) -> Path:
        if not storage_uri.startswith(_FILE_STORE_PREFIX):
            raise ValueError(
                f"LocalFileStoreBackend only handles {_FILE_STORE_PREFIX} URIs, got {storage_uri!r}"
            )
        rel = storage_uri[len(_FILE_STORE_PREFIX) :]
        # Guard against path traversal: any '..' segment is rejected.
        if any(part == ".." for part in rel.split("/")):
            raise ValueError(f"path traversal blocked in {storage_uri!r}")
        return self._root.joinpath(*rel.split("/"))

    def upload(self, *, storage_uri: str, content: bytes) -> None:
        path = self._path_for(storage_uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get_bytes(self, *, storage_uri: str) -> bytes:
        return self._path_for(storage_uri).read_bytes()
