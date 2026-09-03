"""Blob storage backends for B2.

Two backends implement BlobStorageBackend (Protocol):
- LocalFileStoreBackend: file-store://{rest} -> {root}/{rest} on disk.
- GcsBlobStorageBackend: gs://{bucket}/{key} -> google-cloud-storage upload/download.

The orchestrator selects the backend by URI scheme; mixed-scheme runs are
not supported in a single orchestrator invocation.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Protocol
from uuid import UUID

from google.api_core import exceptions as gcp_exceptions
from google.cloud.storage import Client as GcsClient  # type: ignore[import-untyped]

from ums_smart_revenue.connectors.google.errors import (
    BlobChecksumMismatchError,
    BlobDownloadError,
    BlobUploadError,
)


class BlobStorageBackend(Protocol):
    """Storage contract every connector blob backend implements."""

    def upload(self, *, storage_uri: str, content: bytes) -> None:
        """Persist one payload at the backend URI.

        Args:
            storage_uri: Backend-scheme URI naming the payload location.
            content: Exact bytes to persist; an existing payload at the same
                URI is truncated and replaced.

        Returns:
            ``None``. Persisted bytes are read back via :meth:`get_bytes`.
        """

    def get_bytes(self, *, storage_uri: str) -> bytes:
        """Return the exact payload stored at the backend URI.

        Args:
            storage_uri: Backend-scheme URI of a payload written by
                :meth:`upload`.

        Returns:
            The stored bytes, unmodified.
        """


_FILE_STORE_PREFIX = "file-store://"


class LocalFileStoreBackend:
    """Filesystem blob backend serving ``file-store://`` URIs under one root."""

    def __init__(self, *, root: Path) -> None:
        """Bind the backend to one storage ``root`` directory.

        Args:
            root: Directory every ``file-store://`` path resolves under.

        Returns:
            ``None``. The root is stored unresolved; containment checks
            compare resolved forms.
        """
        self._root = Path(root)

    def _path_for(self, storage_uri: str) -> Path:
        """Resolve one ``file-store://`` URI to a contained on-disk path.

        Args:
            storage_uri: ``file-store://`` URI whose path is joined onto this
                backend's root.

        Returns:
            The unresolved on-disk candidate path (no symlink following);
            containment is proven on the resolved form before returning.

        Raises:
            ValueError: The URI uses a foreign scheme, a backslash or NUL
                byte, ``..`` segments, or resolves outside the root.
        """
        if not storage_uri.startswith(_FILE_STORE_PREFIX):
            raise ValueError(
                f"LocalFileStoreBackend only handles {_FILE_STORE_PREFIX} URIs, got {storage_uri!r}"
            )
        rel = storage_uri[len(_FILE_STORE_PREFIX) :]
        # Reject path-separator backslashes outright: file-store:// URIs use
        # forward slash only. On Windows, Path.joinpath would otherwise treat
        # backslash as a separator and allow ..\\ traversal.
        if "\\" in rel:
            raise ValueError(f"backslash not allowed in file-store URI: {storage_uri!r}")
        # Reject NUL byte: some syscalls truncate at NUL, others raise unhelpful
        # generic errors. Fail closed with a domain message.
        if "\x00" in rel:
            raise ValueError(f"NUL byte not allowed in file-store URI: {storage_uri!r}")
        # Reject literal '..' segments even if they happen to resolve back
        # inside root (e.g. 'foo/bar/..'). Such URIs are malformed for a blob
        # store: the segment-as-filename is rejected by Windows and on POSIX
        # would silently write to the parent directory.
        segments = rel.split("/")
        if any(part == ".." for part in segments):
            raise ValueError(f"'..' segment not allowed in file-store URI: {storage_uri!r}")
        # Containment check: resolve the candidate against the (resolved) root
        # and confirm the result stays under it. This catches '..' segments
        # (front, middle, end), drive-letter injection on Windows, and
        # absolute-path injection. We return the unresolved candidate so
        # writes target the literal location without following symlinks the
        # caller did not opt into; the containment check uses the resolved
        # form for security.
        candidate = self._root.joinpath(*segments)
        try:
            resolved_candidate = candidate.resolve(strict=False)
            resolved_root = self._root.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"path resolution failed for {storage_uri!r}: {exc}") from exc
        if not resolved_candidate.is_relative_to(resolved_root):
            raise ValueError(f"path traversal blocked in {storage_uri!r}")
        return candidate

    def upload(self, *, storage_uri: str, content: bytes) -> None:
        """Persist one blob payload at ``storage_uri`` with private on-disk modes.

        Args:
            storage_uri: A ``file-store://`` URI whose path stays contained
                within this backend's root (see :meth:`_path_for`).
            content: Exact bytes to persist; an existing payload at the same
                URI is truncated and replaced.

        Returns:
            ``None``. The payload lives at the store root joined with the
            URI path.

        Raises:
            ValueError: The URI fails the ``..``-segment or containment
                validation in :meth:`_path_for`.
            OSError: Directory creation or the file write fails.
        """
        path = self._path_for(storage_uri)
        # FIX: every directory level must end up with no group/world write —
        # the Compose launcher's storage preflight rejects any such bit inside
        # the bind tree, so umask-derived 0755 intermediates made the launcher
        # reject its own populated store on the next start. Resolved-root
        # containment stops the walk at this backend's root even for a
        # degenerate URI whose parents never equal the root lexically. POSIX
        # only: Windows maps non-writable modes onto the read-only attribute.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if os.name != "nt":
            private_dirs = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
            root = self._root.resolve(strict=False)
            for directory in path.parents:
                resolved = directory.resolve(strict=False)
                try:
                    resolved.relative_to(root)
                except ValueError:
                    break
                if resolved == root:
                    # The configured root's own mode belongs to the operator
                    # and the provisioning policy, not to this write.
                    break
                os.chmod(resolved, private_dirs)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
            0o640,
        )
        try:
            os.write(descriptor, content)
        finally:
            os.close(descriptor)

    def get_bytes(self, *, storage_uri: str) -> bytes:
        """Return the exact bytes previously persisted at ``storage_uri``.

        Args:
            storage_uri: A ``file-store://`` URI whose payload was written by
                :meth:`upload`.

        Returns:
            The stored bytes, unmodified.

        Raises:
            ValueError: The URI fails the containment validation.
            OSError: The payload is missing or unreadable.
        """
        return self._path_for(storage_uri).read_bytes()


_GCS_PREFIX = "gs://"


class GcsBlobStorageBackend:
    """Google Cloud Storage blob backend serving ``gs://`` URIs."""

    def __init__(self, *, client: GcsClient) -> None:
        """Bind the backend to one authenticated GCS ``client``.

        Args:
            client: Authenticated ``google.cloud.storage.Client`` used for
                every bucket/blob call.

        Returns:
            ``None``. The client is retained for the backend's lifetime.
        """
        self._client = client

    @staticmethod
    def _parse_uri(storage_uri: str) -> tuple[str, str]:
        """Split one ``gs://`` URI into its ``(bucket, key)`` parts.

        Args:
            storage_uri: ``gs://`` URI naming one bucket object.

        Returns:
            The ``(bucket, key)`` pair addressed by the URI.

        Raises:
            ValueError: The URI uses a foreign scheme or lacks a bucket/key.
        """
        if not storage_uri.startswith(_GCS_PREFIX):
            raise ValueError(
                f"GcsBlobStorageBackend only handles {_GCS_PREFIX} URIs, got {storage_uri!r}"
            )
        rest = storage_uri[len(_GCS_PREFIX) :]
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            raise ValueError(f"malformed gs:// URI: {storage_uri!r}")
        return bucket, key

    def upload(self, *, storage_uri: str, content: bytes) -> None:
        """Upload one payload to the ``gs://`` location.

        Args:
            storage_uri: ``gs://`` URI naming the target bucket and key.
            content: Exact bytes to persist; an existing object at the same
                URI is overwritten.

        Returns:
            ``None``. Persisted bytes are read back via :meth:`get_bytes`.

        Raises:
            BlobUploadError: The Google API call fails.
            ValueError: The URI fails :meth:`_parse_uri` validation.
        """
        bucket, key = self._parse_uri(storage_uri)
        try:
            blob = self._client.bucket(bucket).blob(key)
            blob.upload_from_string(content)
        except gcp_exceptions.GoogleAPICallError as exc:
            raise BlobUploadError(storage_uri=storage_uri, inner=exc) from exc

    def get_bytes(self, *, storage_uri: str) -> bytes:
        """Download and return the payload stored at the ``gs://`` location.

        Args:
            storage_uri: ``gs://`` URI of an object uploaded by
                :meth:`upload`.

        Returns:
            The stored bytes, unmodified.

        Raises:
            BlobDownloadError: The Google API call fails.
            ValueError: The URI fails :meth:`_parse_uri` validation.
        """
        bucket, key = self._parse_uri(storage_uri)
        try:
            return self._client.bucket(bucket).blob(key).download_as_bytes()
        except gcp_exceptions.GoogleAPICallError as exc:
            raise BlobDownloadError(storage_uri=storage_uri, inner=exc) from exc


def deterministic_blob_path(
    *,
    scheme: str,
    bucket: str,
    tenant_id: UUID,
    connector_key: str,
    report_type: str,
    month: str,
    checksum: str,
    ext: str,
) -> str:
    """Build the deterministic blob URI for a raw report.

    Path shape:
        {scheme}://{bucket}/{tenant_id}/{connector_key}/
        {report_type}/{month}/{checksum}.{ext}

    Args:
        scheme: Backend scheme (``file-store`` or ``gs``); MUST match the
            backend the orchestrator built.
        bucket: Backend bucket name (the store root for file-store).
        tenant_id: Tenant that owns the report run.
        connector_key: Connector identifier (e.g. ``youtube_reporting``).
        report_type: Report type id the payload belongs to.
        month: ``YYYY-MM`` report month.
        checksum: SHA-256 hex digest of the payload bytes.
        ext: Filename extension (e.g. ``csv``).

    Returns:
        The deterministic URI; the same inputs always map to the same
        path, so idempotent re-uploads on retry overwrite or hit the
        existing object.

    Note: account_id is intentionally NOT in the path - run context lives on
    connector_runs.
    """
    return f"{scheme}://{bucket}/{tenant_id}/{connector_key}/{report_type}/{month}/{checksum}.{ext}"


def compute_checksum(content: bytes) -> str:
    """Return the SHA-256 hex digest of ``content``.

    Args:
        content: Exact bytes to hash.

    Returns:
        The 64-character lowercase SHA-256 hex digest.
    """
    return hashlib.sha256(content).hexdigest()


def upload_and_verify(
    *,
    backend: BlobStorageBackend,
    storage_uri: str,
    content: bytes,
) -> str:
    """Upload, re-read, verify SHA-256, return the computed checksum.

    Args:
        backend: Storage backend that owns the URI namespace.
        storage_uri: Destination URI within ``backend``'s scheme.
        content: Exact bytes to persist and verify.

    Returns:
        The SHA-256 hex digest of the verified payload.

    Raises:
        BlobUploadError: Passed through when ``backend.upload`` fails.
        BlobDownloadError: Passed through when the verification re-read
            (``backend.get_bytes``) fails.
        BlobChecksumMismatchError: The re-read bytes hash differently
            (e.g. the backend silently truncated the payload).
    """
    computed = compute_checksum(content)
    backend.upload(storage_uri=storage_uri, content=content)
    read_back = backend.get_bytes(storage_uri=storage_uri)
    read_back_hash = compute_checksum(read_back)
    if read_back_hash != computed:
        raise BlobChecksumMismatchError(
            storage_uri=storage_uri, computed=computed, read=read_back_hash
        )
    return computed
