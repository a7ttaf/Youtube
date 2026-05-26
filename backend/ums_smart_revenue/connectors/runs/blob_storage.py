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
        path = self._path_for(storage_uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get_bytes(self, *, storage_uri: str) -> bytes:
        return self._path_for(storage_uri).read_bytes()
