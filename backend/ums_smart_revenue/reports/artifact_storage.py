"""Persistent storage for generated export artifacts."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

EXPORT_ARTIFACT_DIR_ENV = "UMS_EXPORT_ARTIFACT_DIR"
DEFAULT_EXPORT_ARTIFACT_DIR = Path(tempfile.gettempdir()) / "ums-smart-revenue-export-artifacts"
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
        """Persist one export artifact atomically and return its metadata.

        Args:
            export_id: Owning export job id (validated, becomes one path
                segment under ``exports/``).
            filename: Operator-visible artifact filename (validated, becomes
                the final path segment).
            content_type: Declared MIME type recorded in the metadata.
            content: Non-empty artifact bytes within the configured size cap.

        Returns:
            Metadata describing the artifact actually on disk: its
            ``file-store://`` URI, filename, content type, byte size, and
            sha256 checksum. Under a concurrent second writer, the metadata
            reflects the first-writer's persisted bytes, not the caller's
            input.

        Raises:
            ExportArtifactStorageError: Empty content, a size-cap violation,
                or any filesystem failure; a partial artifact is never left
                at the target path (first-writer-wins persistence via
                ``os.link`` from a private temp file).
        """
        normalized_export_id = _normalize_export_id(export_id)
        normalized_filename = _normalize_filename(filename)
        normalized_content_type = _normalize_content_type(content_type)
        if not content:
            raise ExportArtifactStorageError("artifact content must not be empty")
        if len(content) > self._max_artifact_size_bytes:
            raise ExportArtifactStorageError(
                f"artifact size exceeds limit: {len(content)} > {self._max_artifact_size_bytes}"
            )

        relative_path = Path("exports") / normalized_export_id / normalized_filename
        target_path = self._root_dir / relative_path
        temp_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
        # ================================================================
        # Purpose: First-writer-wins persistence. The temp file is written
        #   then linked into place with os.link so a concurrent second
        #   writer cannot stomp the first writer's bytes on disk. A second
        #   writer observes FileExistsError, drops its temp file, and
        #   leaves the persisted artifact intact for the caller's
        #   terminal-state guard upstream to handle.
        # Database/ORM: None (filesystem only).
        # Standards: Atomic, idempotent writes for finance artifact bytes.
        # Blast Radius: Export artifact on-disk integrity.
        # ================================================================
        first_writer_wins = True
        try:
            # FIX: every directory level must end up with no group/world write —
            # the Compose launcher's storage preflight rejects any such bit
            # inside the bind tree, so umask-derived 0755 intermediates
            # (exports/, the export-id dir) made the launcher reject its own
            # populated store on the next start. Resolved-root containment
            # stops the walk at this store's root; POSIX only, since Windows
            # maps non-writable modes onto the read-only attribute and the
            # Windows preflight enforces ACLs instead of mode bits.
            target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            temp_path.write_bytes(content)
            if os.name != "nt":
                private_dirs = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
                root = self._root_dir.resolve(strict=False)
                for directory in target_path.parents:
                    try:
                        resolved = directory.resolve(strict=False)
                        resolved.relative_to(root)
                    except ValueError:
                        break
                    os.chmod(directory, private_dirs)
                    if resolved == root:
                        break
                temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
            try:
                os.link(temp_path, target_path)
            except FileExistsError:
                first_writer_wins = False
        except OSError as exc:
            _discard_temp_file(temp_path)
            raise ExportArtifactStorageError("artifact storage unavailable") from exc
        finally:
            _discard_temp_file(temp_path)

        if first_writer_wins:
            byte_size = len(content)
            checksum = hashlib.sha256(content).hexdigest()
        else:
            # Another writer's bytes already occupy this path. Return the
            # metadata that actually describes the persisted file so callers
            # cannot confuse the local input with what's now on disk.
            try:
                persisted_bytes = target_path.read_bytes()
            except OSError as exc:
                raise ExportArtifactStorageError("artifact storage unavailable") from exc
            byte_size = len(persisted_bytes)
            checksum = hashlib.sha256(persisted_bytes).hexdigest()
        return ExportArtifactMetadata(
            file_url=f"{EXPORT_ARTIFACT_URI_PREFIX}{relative_path.as_posix()}",
            filename=normalized_filename,
            content_type=normalized_content_type,
            byte_size=byte_size,
            checksum_sha256=checksum,
        )

    def delete(self, *, file_url: str) -> None:
        relative_path = _relative_path_from_file_url(file_url)
        target_path = self._root_dir / relative_path
        try:
            target_path.unlink(missing_ok=True)
        except OSError as exc:
            raise ExportArtifactStorageError("artifact cleanup unavailable") from exc

    def read(self, *, file_url: str) -> bytes:
        relative_path = _relative_path_from_file_url(file_url)
        target_path = self._root_dir / relative_path
        try:
            return target_path.read_bytes()
        except FileNotFoundError as exc:
            raise ExportArtifactStorageError("artifact missing from storage") from exc
        except OSError as exc:
            raise ExportArtifactStorageError("artifact storage unavailable") from exc


def _default_root_dir() -> Path:
    return DEFAULT_EXPORT_ARTIFACT_DIR


def _normalize_export_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ExportArtifactStorageError("export_id must be a valid UUID") from exc


def _normalize_filename(value: str) -> str:
    normalized = value.strip()
    if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ExportArtifactStorageError("artifact filename is invalid")
    return normalized


def _normalize_content_type(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ExportArtifactStorageError("artifact content_type is required")
    return normalized


def _relative_path_from_file_url(value: str) -> Path:
    if not value.startswith(EXPORT_ARTIFACT_URI_PREFIX):
        raise ExportArtifactStorageError("artifact file_url is invalid")
    relative = value[len(EXPORT_ARTIFACT_URI_PREFIX) :].strip()
    relative_path = Path(relative)
    if (
        not relative
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.parts[:1] != ("exports",)
    ):
        raise ExportArtifactStorageError("artifact file_url is invalid")
    return relative_path


def _discard_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
