"""Persistent storage for generated export artifacts."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

EXPORT_ARTIFACT_DIR_ENV = "UMS_EXPORT_ARTIFACT_DIR"
DEFAULT_EXPORT_ARTIFACT_DIR = (
    Path(tempfile.gettempdir()) / "ums-smart-revenue-export-artifacts"
)
EXPORT_ARTIFACT_URI_PREFIX = "file-store://"
DEFAULT_MAX_ARTIFACT_SIZE_BYTES = 500 * 1024 * 1024


@dataclass(frozen=True)
class ExportArtifactMetadata:
    file_url: str
    filename: str
    content_type: str
    byte_size: int
    checksum_sha256: str


class ExportArtifactStorageError(RuntimeError):
    pass


class FileSystemExportArtifactStore:
    """Store generated artifacts on disk behind an object-storage-like URI."""

    def __init__(
        self,
        root_dir: Path | str | None = None,
        *,
        max_artifact_size_bytes: int = DEFAULT_MAX_ARTIFACT_SIZE_BYTES,
    ):
        self._root_dir = Path(root_dir) if root_dir is not None else _default_root_dir()
        if max_artifact_size_bytes < 1:
            raise ExportArtifactStorageError("max_artifact_size_bytes must be positive")
        self._max_artifact_size_bytes = max_artifact_size_bytes

    @classmethod
    def from_environment(cls) -> FileSystemExportArtifactStore:
        configured_root = os.environ.get(EXPORT_ARTIFACT_DIR_ENV)
        if configured_root and configured_root.strip():
            return cls(configured_root.strip())
        return cls()

    def save(
        self,
        *,
        export_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> ExportArtifactMetadata:
        normalized_export_id = _normalize_export_id(export_id)
        normalized_filename = _normalize_filename(filename)
        normalized_content_type = _normalize_content_type(content_type)
        if not content:
            raise ExportArtifactStorageError("artifact content must not be empty")
        if len(content) > self._max_artifact_size_bytes:
            raise ExportArtifactStorageError(
                "artifact size exceeds limit: "
                f"{len(content)} > {self._max_artifact_size_bytes}"
            )

        relative_path = Path("exports") / normalized_export_id / normalized_filename
        target_path = self._root_dir / relative_path
        temp_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_bytes(content)
            temp_path.replace(target_path)
        except OSError as exc:
            _discard_temp_file(temp_path)
            raise ExportArtifactStorageError("artifact storage unavailable") from exc

        return ExportArtifactMetadata(
            file_url=f"{EXPORT_ARTIFACT_URI_PREFIX}{relative_path.as_posix()}",
            filename=normalized_filename,
            content_type=normalized_content_type,
            byte_size=len(content),
            checksum_sha256=hashlib.sha256(content).hexdigest(),
        )


def _default_root_dir() -> Path:
    return DEFAULT_EXPORT_ARTIFACT_DIR


def _normalize_export_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ExportArtifactStorageError("export_id must be a valid UUID") from exc


def _normalize_filename(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
    ):
        raise ExportArtifactStorageError("artifact filename is invalid")
    return normalized


def _normalize_content_type(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ExportArtifactStorageError("artifact content_type is required")
    return normalized


def _discard_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
