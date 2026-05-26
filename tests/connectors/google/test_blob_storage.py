"""Blob storage backend tests.

Two backends implement BlobStorageBackend:
- LocalFileStoreBackend (file-store://) - test/dev, writes to a tmp dir.
- GcsBlobStorageBackend (gs://) - production, uses google-cloud-storage.

Both must round-trip bytes deterministically; tests assert that get_bytes
after upload returns the same payload.
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from google.api_core import exceptions as gcp_exceptions

from ums_smart_revenue.connectors.google.errors import (
    BlobChecksumMismatchError,
    BlobUploadError,
)
from ums_smart_revenue.connectors.runs.blob_storage import (
    BlobStorageBackend,
    GcsBlobStorageBackend,
    LocalFileStoreBackend,
    compute_checksum,
    deterministic_blob_path,
    upload_and_verify,
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


@pytest.mark.parametrize(
    "bad_uri",
    [
        # Forward-slash .. traversal:
        "file-store://../etc/passwd",
        "file-store://foo/../../../etc/passwd",
        "file-store://foo/bar/..",
        # Windows backslash traversal:
        r"file-store://..\..\..\Windows\Temp\evil.txt",
        r"file-store://foo\..\..\Windows\Temp\evil.txt",
        # Drive-letter / absolute path injection (Windows):
        r"file-store://C:\Windows\Temp\evil.txt",
        r"file-store://D:\evil.txt",
        # NUL byte:
        "file-store://foo\x00.txt",
    ],
)
def test_file_store_rejects_path_traversal(tmp_path, bad_uri: str) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    with pytest.raises(ValueError):
        backend.upload(storage_uri=bad_uri, content=b"x")


def test_gcs_upload_parses_uri_and_calls_blob_upload() -> None:
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_client.bucket.return_value = fake_bucket
    fake_bucket.blob.return_value = fake_blob

    backend = GcsBlobStorageBackend(client=fake_client)
    backend.upload(storage_uri="gs://my-bucket/tenant/yt/key.csv", content=b"x")

    fake_client.bucket.assert_called_once_with("my-bucket")
    fake_bucket.blob.assert_called_once_with("tenant/yt/key.csv")
    fake_blob.upload_from_string.assert_called_once_with(b"x")


def test_gcs_upload_wraps_api_error_as_blob_upload_error() -> None:
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_client.bucket.return_value = fake_bucket
    fake_bucket.blob.return_value = fake_blob
    fake_blob.upload_from_string.side_effect = gcp_exceptions.GoogleAPICallError("fail")

    backend = GcsBlobStorageBackend(client=fake_client)
    with pytest.raises(BlobUploadError) as ctx:
        backend.upload(storage_uri="gs://my-bucket/key", content=b"x")
    assert ctx.value.storage_uri == "gs://my-bucket/key"


def test_gcs_get_bytes_downloads_via_blob() -> None:
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_blob.download_as_bytes.return_value = b"downloaded"
    fake_client.bucket.return_value = fake_bucket
    fake_bucket.blob.return_value = fake_blob

    backend = GcsBlobStorageBackend(client=fake_client)
    assert backend.get_bytes(storage_uri="gs://b/k") == b"downloaded"


def test_gcs_rejects_non_gs_scheme() -> None:
    backend = GcsBlobStorageBackend(client=MagicMock())
    with pytest.raises(ValueError, match="gs://"):
        backend.upload(storage_uri="file-store://x", content=b"x")


def test_deterministic_blob_path_format() -> None:
    path = deterministic_blob_path(
        bucket="my-bucket",
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        connector_key="youtube-reporting",
        report_type="channel_basic_a2",
        month="2026-05",
        checksum="abc123",
        ext="csv",
    )
    assert path == (
        "gs://my-bucket/00000000-0000-0000-0000-000000000001/"
        "youtube-reporting/channel_basic_a2/2026-05/abc123.csv"
    )


def test_compute_checksum_returns_hex_sha256() -> None:
    expected = hashlib.sha256(b"hello").hexdigest()
    assert compute_checksum(b"hello") == expected


def test_upload_and_verify_round_trips(tmp_path) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    uri = "file-store://bucket/tenant/yt/m/abc.csv"
    checksum = upload_and_verify(
        backend=backend, storage_uri=uri, content=b"payload"
    )
    assert checksum == hashlib.sha256(b"payload").hexdigest()


def test_upload_and_verify_raises_on_checksum_mismatch(tmp_path, monkeypatch) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    uri = "file-store://bucket/key"

    def fake_get_bytes(*, storage_uri: str) -> bytes:
        return b"different"

    monkeypatch.setattr(backend, "get_bytes", fake_get_bytes)
    with pytest.raises(BlobChecksumMismatchError):
        upload_and_verify(backend=backend, storage_uri=uri, content=b"original")
