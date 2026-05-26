"""Blob storage backend tests.

Two backends implement BlobStorageBackend:
- LocalFileStoreBackend (file-store://) - test/dev, writes to a tmp dir.
- GcsBlobStorageBackend (gs://) - production, uses google-cloud-storage.

Both must round-trip bytes deterministically; tests assert that get_bytes
after upload returns the same payload.
"""
from __future__ import annotations

import pytest

from ums_smart_revenue.connectors.runs.blob_storage import (
    BlobStorageBackend,
    LocalFileStoreBackend,
)


def test_file_store_round_trips_bytes(tmp_path) -> None:
    backend: BlobStorageBackend = LocalFileStoreBackend(root=tmp_path)
    uri = "file-store://bucket/tenant/yt/2026-05/abc.csv"
    payload = b"a,b,c\n1,2,3\n"
    backend.upload(storage_uri=uri, content=payload)
    assert backend.get_bytes(storage_uri=uri) == payload


def test_file_store_rejects_non_file_store_scheme(tmp_path) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    with pytest.raises(ValueError, match="file-store://"):
        backend.upload(storage_uri="gs://bucket/key", content=b"x")


def test_file_store_creates_parent_dirs(tmp_path) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    uri = "file-store://bucket/deep/nested/path/key.csv"
    backend.upload(storage_uri=uri, content=b"x")
    assert backend.get_bytes(storage_uri=uri) == b"x"


def test_file_store_get_bytes_missing_raises_file_not_found(tmp_path) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    with pytest.raises(FileNotFoundError):
        backend.get_bytes(storage_uri="file-store://bucket/missing.csv")
