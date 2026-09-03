"""Prepare, back up, verify, and restore the Compose application-data bind."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

LOGGER = logging.getLogger("ums.compose_storage")


CONTRACT_NAME = "ums-compose-storage-v1"


MANIFEST_NAME = "ums-compose-backup-v1"


COMPOSE_RECOVERY_PROFILE = "ums-compose-recovery-v1"


GENERIC_BACKUP_PROFILE = "ums-backup-files-v1"


MARKER_FILENAME = ".ums-storage-root.json"


RESTORE_PENDING_FILENAME = ".ums-restore-pending"


RESTORE_JOURNAL_FILENAME = ".ums-restore-journal.json"


RESTORE_STAGE_PREFIX = ".ums-restore-stage-"


READY_FILENAME = ".ums-storage-ready.json"


STORAGE_DIRECTORIES = ("artifacts", "blobs")


REQUIRED_RECOVERY_MEMBERS = frozenset(
    {"git-revision.txt", "running-services.txt", "ums-app-data.tgz"}
)


DATABASE_RECOVERY_MEMBERS = frozenset({"database.dump", "roles.sql", "database-manifest.json"})


DATABASE_RUN_NAME_RE = re.compile(r"^ums-database-backup-\d{8}T\d{6}Z-[0-9a-f]{8}$")


GCS_SNAPSHOT_MEMBER = "gcs-snapshot.json"


DEFAULT_GCS_BUCKET = "ums-smart-revenue-raw"


CHUNK_SIZE = 1024 * 1024


MAX_POSIX_ID = 2_147_483_647


HOST_PATH_ENV = "UMS_APP_DATA_HOST_CONTRACT"


HOST_CANONICAL_ENV = "UMS_APP_DATA_HOST_CANONICAL_CONTRACT"


HOST_CONFIGURED_ENV = "UMS_APP_DATA_HOST_CONFIGURED"


class StorageContractError(RuntimeError):
    """Raised when a storage path or backup violates the safety contract."""


def _repository_root() -> Path:
    """Locate the repository root from this script's own location."""
    return Path(__file__).resolve().parents[1]


def _normalized_path(path: Path) -> str:
    """Return the portable POSIX spelling of a path."""
    return os.path.normcase(str(path.resolve(strict=False)))


def _is_redirect(path: Path) -> bool:
    """Report whether the path is a symlink or Windows reparse point."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _reject_existing_redirects(path: Path, *, stop_at: Path | None = None) -> None:
    """Fail if any path on the way to the target is a redirect."""
    candidate = path
    while True:
        if candidate.exists() and _is_redirect(candidate):
            raise StorageContractError(f"storage paths may not traverse a link: {candidate}")
        if candidate in (candidate.parent, stop_at):
            return
        candidate = candidate.parent


def _raw_path(raw_path: str, *, base: Path) -> Path:
    """Resolve a configured storage path under its declared base."""
    if not raw_path or not raw_path.strip():
        raise StorageContractError("storage path must be explicitly set and non-empty")
    if raw_path.strip() in {".", "./", ".\\"}:
        raise StorageContractError("storage path may not be the repository/workspace directory")
    path = Path(raw_path.strip()).expanduser()
    return path if path.is_absolute() else base / path


def _configured_path_key(raw_path: str) -> str:
    """Normalize and validate a configured storage path into its key."""
    if not raw_path or not raw_path.strip():
        raise StorageContractError("storage path must be explicitly set and non-empty")
    portable = raw_path.strip().replace("\\", "/")
    raw_parts = [part for part in portable.split("/") if part]
    if ".." in raw_parts:
        raise StorageContractError("storage path may not contain parent traversal")
    normalized = posixpath.normpath(portable)
    drive_root = (
        len(normalized) in {2, 3}
        and normalized[0].isalpha()
        and normalized[1] == ":"
        and normalized[2:] in {"", "/"}
    )
    unc_parts = [part for part in normalized.split("/") if part]
    unc_root = normalized.startswith("//") and len(unc_parts) <= 2
    if normalized in {".", "/"} or drive_root or unc_root:
        raise StorageContractError(
            "storage path may not be a root or repository/workspace dot path"
        )
    return normalized.casefold()


def _validate_host_candidate(
    raw_path: str,
    *,
    safe_root: Path,
    repository_root: Path,
) -> Path:
    """Resolve a host-side storage path and refuse traversal or redirects."""
    unresolved = _raw_path(raw_path, base=repository_root)
    unresolved_safe_root = safe_root.expanduser()
    if not unresolved_safe_root.is_absolute():
        unresolved_safe_root = repository_root / unresolved_safe_root

    _reject_existing_redirects(unresolved)
    _reject_existing_redirects(unresolved_safe_root)

    target = unresolved.resolve(strict=False)
    approved_root = unresolved_safe_root.resolve(strict=False)
    repository = repository_root.resolve(strict=False)
    filesystem_root = Path(target.anchor).resolve(strict=False)
    home = Path.home().resolve(strict=False)

    if target in {filesystem_root, repository, approved_root, home}:
        raise StorageContractError(
            "storage path must be a dedicated child directory, not a filesystem, home, "
            "repository/workspace, or approved-root directory"
        )
    if approved_root in {Path(approved_root.anchor).resolve(strict=False), repository, home}:
        raise StorageContractError(
            "safe root must be a dedicated directory, not a filesystem, home, or repository"
        )
    if not target.is_relative_to(approved_root):
        raise StorageContractError(
            f"storage path {target} is outside the approved safe root {approved_root}"
        )
    if target.parent != approved_root:
        raise StorageContractError("storage path must be a direct child of the approved safe root")
    if target.exists() and not target.is_dir():
        raise StorageContractError(f"storage path is not a directory: {target}")
    return target


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk, refusing duplicate keys."""
    if _is_redirect(path):
        raise StorageContractError(f"contract file may not be a link: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StorageContractError(
            f"storage contract is missing: {path}; run the prepare command first"
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageContractError(f"storage contract is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise StorageContractError(f"storage contract is not a JSON object: {path}")
    return payload


def _sync_file(path: Path) -> None:
    """Flush a file's contents and metadata to stable storage."""
    flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    """Flush a directory entry to stable storage."""
    if os.name == "nt":
        # Windows directory handles do not support FlushFileBuffers. File
        # creation is flushed through the final file; renames use WRITE_THROUGH.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, destination: Path) -> None:
    """Atomically replace the destination with the source and sync parents."""
    if os.name == "nt":
        import ctypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file.restype = ctypes.c_int
        movefile_replace_existing = 0x1
        movefile_write_through = 0x8
        if not move_file(
            str(source),
            str(destination),
            movefile_replace_existing | movefile_write_through,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(source, destination)
    _sync_directory(destination.parent)
    if source.parent != destination.parent:
        _sync_directory(source.parent)


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    """Create a JSON file that must not already exist."""
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    temporary = path.with_name(f".{path.name}.partial-{os.getpid()}-{secrets.token_hex(4)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
        _sync_file(path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON through a same-directory temporary and durable rename."""
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    temporary = path.with_name(f".{path.name}.partial-{os.getpid()}-{secrets.token_hex(4)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        _durable_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_marker_payload(payload: dict[str, Any], marker_path: Path) -> None:
    """Check that a storage marker matches the configured mount."""
    if payload.get("contract") != CONTRACT_NAME:
        raise StorageContractError(f"unknown storage contract in {marker_path}")
    if set(payload) != {"canonical_path", "configured_path_key", "contract", "safe_root"}:
        raise StorageContractError(f"storage contract has unexpected fields: {marker_path}")
    if not all(isinstance(payload.get(key), str) for key in payload):
        raise StorageContractError(f"storage contract fields must be strings: {marker_path}")


# ============================================================================
# Purpose: Approve one dedicated host directory before Compose may mount it.
# Database/ORM: None.
# Standards: Fail closed on ambiguous paths, redirects, and unmarked content.
# Blast Radius: Artifact and connector-blob storage; no database state changed.
# Connections:
#   - File: docker-compose.yml -> app-data-init requires this marker.
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> operator preparation contract.
# ============================================================================


def prepare_storage(
    raw_path: str,
    *,
    safe_root: Path,
    repository_root: Path | None = None,
) -> Path:
    """Create or validate the host-side marker for a dedicated storage target."""
    repository = (repository_root or _repository_root()).resolve(strict=False)
    target = _validate_host_candidate(
        raw_path,
        safe_root=safe_root,
        repository_root=repository,
    )
    approved_root = safe_root.expanduser()
    if not approved_root.is_absolute():
        approved_root = repository / approved_root
    approved_root = approved_root.resolve(strict=False)
    marker_path = target / MARKER_FILENAME
    configured_path_key = _configured_path_key(raw_path)

    if marker_path.exists():
        payload = _read_json(marker_path)
        _validate_marker_payload(payload, marker_path)
        if _normalized_path(Path(payload["canonical_path"])) != _normalized_path(target):
            raise StorageContractError("storage marker belongs to a different host path")
        if _normalized_path(Path(payload["safe_root"])) != _normalized_path(approved_root):
            raise StorageContractError("storage marker belongs to a different approved safe root")
        if payload["configured_path_key"] != configured_path_key:
            raise StorageContractError("storage marker belongs to a different configured host path")
        return target

    if target.exists() and any(target.iterdir()):
        raise StorageContractError(
            "unmarked storage directory is not empty; refusing to claim existing data"
        )

    approved_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.mkdir(mode=0o700, parents=False, exist_ok=True)
    _write_json_exclusive(
        marker_path,
        {
            "canonical_path": str(target),
            "configured_path_key": configured_path_key,
            "contract": CONTRACT_NAME,
            "safe_root": str(approved_root),
        },
    )
    return target


def check_host_storage(raw_path: str, *, repository_root: Path | None = None) -> Path:
    """Validate an already-prepared host path without creating or mutating it."""
    repository = (repository_root or _repository_root()).resolve(strict=False)
    unresolved = _raw_path(raw_path, base=repository)
    target = unresolved.resolve(strict=False)
    marker_path = target / MARKER_FILENAME
    payload = _read_json(marker_path)
    _validate_marker_payload(payload, marker_path)
    safe_root = Path(payload["safe_root"])
    validated = _validate_host_candidate(
        raw_path,
        safe_root=safe_root,
        repository_root=repository,
    )
    if _normalized_path(Path(payload["canonical_path"])) != _normalized_path(validated):
        raise StorageContractError("storage marker belongs to a different host path")
    if payload["configured_path_key"] != _configured_path_key(raw_path):
        raise StorageContractError("storage marker belongs to a different configured host path")
    return validated


# ============================================================================
# Purpose: Bind host canonical-path validation to the Compose process invocation.
# Database/ORM: None.
# Standards: Resolve the marked path on the host and pass an ephemeral receipt.
# Blast Radius: Compose lifecycle commands; no mutation occurs before validation.
# Connections:
#   - File: docker-compose.yml -> app-data-init consumes the canonical receipt.
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> required lifecycle wrapper.
# ============================================================================


def run_compose_with_preflight(
    raw_path: str,
    compose_args: list[str],
    *,
    repository_root: Path | None = None,
    runner: Any = subprocess.run,
) -> int:
    """Run Docker Compose only after host-side canonical marker validation."""
    if not compose_args:
        raise StorageContractError("compose wrapper requires arguments after --")
    repository = (repository_root or _repository_root()).resolve(strict=False)
    canonical = check_host_storage(raw_path, repository_root=repository)
    environment = dict(os.environ)
    # FIX: Compose now receives the already-resolved source, not the spelling
    # that was checked before a host redirect could be substituted.
    environment["UMS_APP_DATA_HOST"] = str(canonical)
    environment["UMS_APP_DATA_HOST_CANONICAL"] = str(canonical)
    environment[HOST_CONFIGURED_ENV] = raw_path
    completed = runner(
        ["docker", "compose", *compose_args],
        cwd=repository,
        env=environment,
        check=False,
    )
    return int(completed.returncode)


def _validate_mounted_marker(
    mount_root: Path,
    *,
    configured_host_path: str | None = None,
) -> dict[str, Any]:
    """Validate the initialized-storage marker inside a mounted volume."""
    if not mount_root.is_dir() or _is_redirect(mount_root):
        raise StorageContractError(f"mounted storage root is not a real directory: {mount_root}")
    marker_path = mount_root / MARKER_FILENAME
    payload = _read_json(marker_path)
    _validate_marker_payload(payload, marker_path)
    configured = configured_host_path or os.environ.get(HOST_PATH_ENV)
    if configured is None:
        raise StorageContractError(f"{HOST_PATH_ENV} is required for mounted-path validation")
    if payload["configured_path_key"] != _configured_path_key(configured):
        raise StorageContractError("mounted marker does not match UMS_APP_DATA_HOST")
    canonical = os.environ.get(HOST_CANONICAL_ENV)
    if canonical is None or not canonical.strip():
        raise StorageContractError(
            "host canonical-path receipt is missing; use the compose wrapper"
        )
    marker_canonical = payload["canonical_path"].replace("\\", "/").rstrip("/").casefold()
    receipt_canonical = canonical.strip().replace("\\", "/").rstrip("/").casefold()
    if marker_canonical != receipt_canonical:
        raise StorageContractError("host canonical-path receipt does not match the marker")
    return payload


def _ready_payload(marker: dict[str, Any]) -> dict[str, str]:
    """Build the marker payload published once storage is ready."""
    return {
        "canonical_path": marker["canonical_path"],
        "configured_path_key": marker["configured_path_key"],
        "contract": CONTRACT_NAME,
        "state": "initialized",
    }


def _validate_ready_storage(mount_root: Path) -> None:
    """Verify an already-initialized storage mount end to end."""
    if not mount_root.is_dir() or _is_redirect(mount_root):
        raise StorageContractError(f"mounted storage root is not a real directory: {mount_root}")
    ready_path = mount_root / READY_FILENAME
    if (mount_root / RESTORE_PENDING_FILENAME).exists() or (
        mount_root / RESTORE_JOURNAL_FILENAME
    ).exists():
        raise StorageContractError("storage restore is not fully initialized")
    payload = _read_json(ready_path)
    if set(payload) != {"canonical_path", "configured_path_key", "contract", "state"}:
        raise StorageContractError("storage readiness marker has unexpected fields")
    if payload.get("contract") != CONTRACT_NAME or payload.get("state") != "initialized":
        raise StorageContractError("storage readiness marker has an unknown contract or state")
    configured = os.environ.get(HOST_PATH_ENV)
    canonical = os.environ.get(HOST_CANONICAL_ENV)
    if not configured or payload.get("configured_path_key") != _configured_path_key(configured):
        raise StorageContractError("storage readiness marker lacks the wrapper configured receipt")
    if not canonical:
        raise StorageContractError("storage readiness marker lacks the wrapper canonical receipt")
    ready_canonical = str(payload.get("canonical_path", "")).replace("\\", "/").rstrip("/")
    receipt_canonical = canonical.strip().replace("\\", "/").rstrip("/")
    if ready_canonical.casefold() != receipt_canonical.casefold():
        raise StorageContractError("storage readiness marker does not match the canonical receipt")
    for name in STORAGE_DIRECTORIES:
        root = mount_root / name
        if not root.is_dir() or _is_redirect(root):
            raise StorageContractError(f"initialized storage root is missing: {root}")


def exec_with_ready_storage(mount_root: Path, command: list[str]) -> None:
    """Fail closed before the application process starts, even with --no-deps."""
    if not command:
        raise StorageContractError("storage-gated exec requires a command after --")
    _validate_ready_storage(mount_root)
    # FIX: execvp previously resolved a partial executable path through PATH
    # at exec time (B607) and the exec-family call itself is flagged as a
    # process start without a shell (B606). The merged #221 entrypoint no
    # longer routes through this path, so running the validated command as
    # a child and mirroring its exit status preserves the CLI contract.
    resolved = shutil.which(command[0])
    if resolved is None:
        raise StorageContractError("storage-gated exec requires an executable on PATH")
    completed = subprocess.run([resolved, *command[1:]], check=False)
    raise SystemExit(completed.returncode)


def _walk_without_redirects(root: Path) -> list[Path]:
    """Walk a tree while refusing symlinks and reparse points."""
    paths = [root]
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        directory_names.sort()
        file_names.sort()
        for name in [*directory_names, *file_names]:
            child = current / name
            if _is_redirect(child):
                raise StorageContractError(f"storage tree contains a link: {child}")
            paths.append(child)
    return paths


def _probe_as_identity(roots: tuple[Path, ...], *, uid: int, gid: int) -> None:
    """Run a storage probe under an explicit uid and gid."""
    original_euid = os.geteuid()
    original_egid = os.getegid()
    original_groups = os.getgroups()
    try:
        os.setgroups([])
        os.setegid(gid)
        os.seteuid(uid)
        for root in roots:
            probe = root / f".rw-probe-{secrets.token_hex(8)}"
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            probe.unlink()
    finally:
        os.seteuid(original_euid)
        os.setegid(original_egid)
        os.setgroups(original_groups)


# ============================================================================
# Purpose: Prove the runtime uid can traverse and read every restored entry.
# Database/ORM: None.
# Standards: Drop effective identity and reject redirects or special files.
# Blast Radius: Restore readiness only; restored bytes remain unchanged.
# Connections:
#   - File: docker-compose.yml -> app-data-init gates application startup.
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> pending-restore contract.
# ============================================================================


def _verify_tree_readable_as_identity(roots: tuple[Path, ...], *, uid: int, gid: int) -> None:
    """Verify every tree entry is readable by the runtime identity."""
    paths: list[Path] = []
    for root in roots:
        paths.extend(_walk_without_redirects(root))
    original_euid = os.geteuid()
    original_egid = os.getegid()
    original_groups = os.getgroups()
    try:
        os.setgroups([])
        os.setegid(gid)
        os.seteuid(uid)
        for path in paths:
            if path.is_dir():
                _require_search_access(path)
                with os.scandir(path) as entries:
                    list(entries)
            elif path.is_file():
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    os.read(descriptor, 1)
                finally:
                    os.close(descriptor)
            else:
                raise StorageContractError(f"restored entry is not a regular file: {path}")
    finally:
        os.seteuid(original_euid)
        os.setegid(original_egid)
        os.setgroups(original_groups)


def _require_search_access(path: Path) -> None:
    """Require search permission on a directory path."""
    if not os.access(path, os.X_OK, effective_ids=True):
        raise PermissionError(f"runtime identity cannot search directory: {path}")


def _runtime_identity(app_user: str) -> tuple[int, int]:
    """Resolve the container app account to a uid and gid pair."""
    try:
        import pwd
    except ModuleNotFoundError as exc:  # pragma: no cover - container is Linux.
        raise StorageContractError("container initialization requires a POSIX image") from exc
    try:
        account = pwd.getpwnam(app_user)
    except KeyError as exc:
        raise StorageContractError(f"runtime account does not exist: {app_user}") from exc
    return account.pw_uid, account.pw_gid


# ============================================================================
# Purpose: Provision the marked bind only after its host contract is proven.
# Database/ORM: None.
# Standards: Derive the runtime identity from the image and probe as that user.
# Blast Radius: Ownership and modes under the dedicated artifact/blob bind only.
# Connections:
#   - File: Dockerfile -> the app account is built with configurable APP_UID.
#   - File: docker-compose.yml -> root one-shot invokes this entry point.
# ============================================================================


def _resolve_restore_markers(
    *,
    pending_restore: Path,
    restore_journal: Path,
    ready_path: Path,
    marker_payload: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Reconcile the ready, journal, and pending markers on one mount."""
    expected_ready = _ready_payload(marker_payload)
    ready_matches = False
    if ready_path.exists():
        if _read_json(ready_path) != expected_ready:
            raise StorageContractError("storage readiness marker does not match the host marker")
        ready_matches = True
    if restore_journal.exists() != pending_restore.exists() and not ready_matches:
        raise StorageContractError(
            "restore journal and pending marker disagree; rerun restore-artifacts"
        )
    if ready_matches and not (restore_journal.exists() or pending_restore.exists()):
        _unlink_and_sync(ready_path)
        ready_matches = False
    return ready_matches, expected_ready


def _pending_restore_payload_from_marker(
    pending_restore: Path,
) -> dict[str, Any] | None:
    """Return the validated pending-restore payload, if the marker exists."""
    if not pending_restore.exists():
        return None
    if _is_redirect(pending_restore) or not pending_restore.is_file():
        raise StorageContractError("restore-pending marker is not a regular file")
    payload = _read_json(pending_restore)
    _validate_pending_restore_payload(payload)
    return payload


def _ownership_targets_for(
    mount_root: Path, roots: tuple[Path, ...], pending_restore: Path
) -> list[Path]:
    """Collect the paths a restore-aware initialization must chown."""
    if not pending_restore.exists():
        return [mount_root, *roots]
    targets = [mount_root]
    for root in roots:
        targets.extend(_walk_without_redirects(root))
    return targets


def _chown_targets(targets: list[Path], *, uid: int, gid: int) -> list[str]:
    """Chown every target, returning the failures instead of raising."""
    failures: list[str] = []
    for path in targets:
        try:
            os.chown(path, uid, gid)
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    return failures


def initialize_container_storage(
    mount_root: Path,
    *,
    app_user: str,
    configured_host_path: str | None = None,
) -> None:
    """Initialize and prove mounted storage as the image's actual app identity."""
    marker_payload = _validate_mounted_marker(
        mount_root,
        configured_host_path=configured_host_path,
    )
    if os.geteuid() != 0:
        raise StorageContractError("container initialization must run as root")
    app_uid, app_gid = _runtime_identity(app_user)

    pending_restore = mount_root / RESTORE_PENDING_FILENAME
    restore_journal = mount_root / RESTORE_JOURNAL_FILENAME
    ready_path = mount_root / READY_FILENAME
    ready_matches, expected_ready = _resolve_restore_markers(
        pending_restore=pending_restore,
        restore_journal=restore_journal,
        ready_path=ready_path,
        marker_payload=marker_payload,
    )
    roots = tuple(mount_root / name for name in STORAGE_DIRECTORIES)
    for root in roots:
        root.mkdir(mode=0o750, exist_ok=True)

    _pending_restore_payload_from_marker(pending_restore)
    ownership_targets = _ownership_targets_for(mount_root, roots, pending_restore)
    chown_failures = _chown_targets(ownership_targets, uid=app_uid, gid=app_gid)

    private_directory_mode = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
    for root in (mount_root, *roots):
        os.chmod(root, private_directory_mode)

    # FIX: A root-directory write probe did not prove that the runtime identity
    # could traverse and read restored descendants after a host chown failure.
    if pending_restore.exists():
        _verify_tree_readable_as_identity(
            roots,
            uid=app_uid,
            gid=app_gid,
        )
    _probe_as_identity(roots, uid=app_uid, gid=app_gid)
    if not ready_matches:
        _write_json_exclusive(ready_path, expected_ready)
    os.chmod(ready_path, 0o444)
    if os.name != "nt":
        _sync_file(ready_path)
    if pending_restore.exists() or restore_journal.exists():
        _finish_pending_restore_initialization(mount_root)
    if chown_failures:
        LOGGER.warning(
            "host bind did not accept chown; runtime-identity write probe passed: %s",
            "; ".join(chown_failures),
        )


def _assert_sensitive_output(path: Path, *, repository_root: Path) -> Path:
    """Resolve backup output outside the repository without overwriting."""
    output = path.expanduser().resolve(strict=False)
    repository = repository_root.resolve(strict=False)
    if output.is_relative_to(repository):
        raise StorageContractError("sensitive backup output must be outside the repository")
    if output.exists():
        raise StorageContractError(f"refusing to overwrite existing output: {output}")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return output


def _archive_storage_tree(storage: Path, archive: Path) -> Path:
    """Write the storage tree into a deterministic tar archive."""
    roots = tuple(storage / name for name in STORAGE_DIRECTORIES)
    if any(not root.is_dir() for root in roots):
        raise StorageContractError("artifact and blob directories must exist before backup")
    entries: list[Path] = []
    for root in roots:
        entries.extend(_walk_without_redirects(root))

    temporary = archive.with_name(f".{archive.name}.tmp-{os.getpid()}")
    try:
        with tarfile.open(temporary, mode="x:gz") as handle:
            for entry in entries:
                handle.add(entry, arcname=entry.relative_to(storage).as_posix(), recursive=False)
        os.chmod(temporary, 0o600)
        _sync_file(temporary)
        os.link(temporary, archive)
        _sync_file(archive)
        _sync_directory(archive.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


# ============================================================================
# Purpose: Archive quiesced artifact and connector-blob bytes cross-platform.
# Database/ORM: None; database and roles dumps are separate bundle members.
# Standards: Refuse live-writer ambiguity, links, worktree output, and overwrite.
# Blast Radius: Reads artifact/blob storage and writes one external archive.
# Connections:
#   - File: docker-compose.yml -> app and app-dev are the bind writers.
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> coordinated backup sequence.
# ============================================================================


def create_artifact_archive(
    raw_path: str,
    *,
    output: Path,
    writers_stopped: bool,
    repository_root: Path | None = None,
) -> Path:
    """Create a tar.gz archive after explicit confirmation that writers stopped."""
    if not writers_stopped:
        raise StorageContractError("stop app and app-dev, then pass --writers-stopped")
    repository = (repository_root or _repository_root()).resolve(strict=False)
    storage = check_host_storage(raw_path, repository_root=repository)
    archive = _assert_sensitive_output(output, repository_root=repository)
    return _archive_storage_tree(storage, archive)


# ============================================================================
# Purpose: Archive the same stopped bind from the root init image on POSIX.
# Database/ORM: None.
# Standards: Validate the marker, publish no-clobber, and return host ownership.
# Blast Radius: Reads artifact/blob bytes and writes one external archive.
# Connections:
#   - File: docker-compose.yml -> app-data-init supplies the mounted bind.
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> Linux backup command.
# ============================================================================


def create_mounted_artifact_archive(
    mount_root: Path,
    *,
    output: Path,
    writers_stopped: bool,
    output_uid: int | None = None,
    output_gid: int | None = None,
) -> Path:
    """Archive mounted storage as root, optionally returning ownership to the host user."""
    if not writers_stopped:
        raise StorageContractError("stop app and app-dev, then pass --writers-stopped")
    if (output_uid is None) != (output_gid is None):
        raise StorageContractError("output uid and gid must be provided together")
    # FIX: The documented root-operator path supplied 0:0 but validation
    # rejected zero before the archive command could run.
    if output_uid is not None and not 0 <= output_uid <= MAX_POSIX_ID:
        raise StorageContractError("output uid is outside the supported non-negative range")
    if output_gid is not None and not 0 <= output_gid <= MAX_POSIX_ID:
        raise StorageContractError("output gid is outside the supported non-negative range")
    if output_uid is not None and output_gid is not None and os.geteuid() != 0:
        raise StorageContractError("changing backup output ownership requires root")
    _validate_mounted_marker(mount_root)
    archive = _assert_sensitive_output(output, repository_root=_repository_root())
    result = _archive_storage_tree(mount_root, archive)
    if output_uid is not None and output_gid is not None:
        os.chown(result, output_uid, output_gid)
    return result


def _require_member_shape(member: tarfile.TarInfo, seen: set[str]) -> PurePosixPath:
    """Validate one archive member and return its canonical posix path."""
    path = PurePosixPath(member.name)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise StorageContractError(f"unsafe archive path: {member.name}")
    if "\\" in member.name or path.parts[0] not in STORAGE_DIRECTORIES:
        raise StorageContractError(f"archive member is outside storage roots: {member.name}")
    if member.name.rstrip("/") != path.as_posix():
        raise StorageContractError(f"non-canonical archive member: {member.name}")
    if path.as_posix() in seen:
        raise StorageContractError(f"duplicate archive member: {member.name}")
    if not (member.isdir() or member.isreg()):
        raise StorageContractError(f"archive links/devices are forbidden: {member.name}")
    seen.add(path.as_posix())
    if len(path.parts) == 1 and not member.isdir():
        raise StorageContractError(f"archive storage root must be a directory: {member.name}")
    return path


def _validated_archive_members(handle: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Read archive members while refusing unsafe or redirected paths."""
    members = handle.getmembers()
    seen: set[str] = set()
    top_levels: set[str] = set()
    directory_roots: set[str] = set()
    for member in members:
        normalized_name = _require_member_shape(member, seen)
        top_levels.add(normalized_name.parts[0])
        # FIX: A regular file named artifacts or blobs previously counted as
        # the required root even though extraction needs actual directories.
        if len(normalized_name.parts) == 1:
            directory_roots.add(normalized_name.parts[0])
    if top_levels != set(STORAGE_DIRECTORIES):
        raise StorageContractError("archive must contain both artifacts and blobs roots")
    if directory_roots != set(STORAGE_DIRECTORIES):
        raise StorageContractError(
            "archive must contain explicit artifacts and blobs directory roots"
        )
    return members


def verify_artifact_archive(archive: Path) -> None:
    """Verify archive readability and member-path safety without extracting it."""
    try:
        with tarfile.open(archive, mode="r:gz") as handle:
            _validated_archive_members(handle)
    except (OSError, tarfile.TarError) as exc:
        raise StorageContractError(f"artifact archive is invalid: {archive}") from exc


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(handle: BinaryIO) -> str:
    """Return the SHA-256 hex digest of an open binary stream."""
    position = handle.tell()
    digest = hashlib.sha256()
    try:
        handle.seek(0)
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    finally:
        handle.seek(position)
    return digest.hexdigest()


def _configured_gcs_bucket(value: str | None) -> str:
    """Require a GCS bucket name when the blob backend is GCS."""
    bucket = value if value is not None else os.environ.get("UMS_GCS_BUCKET", DEFAULT_GCS_BUCKET)
    if not isinstance(bucket, str) or not bucket or bucket != bucket.strip():
        raise StorageContractError("configured GCS bucket must be explicit and non-empty")
    return bucket


def _decode_gcs_snapshot(data: bytes, *, source: Path) -> dict[str, Any]:
    """Decode a GCS snapshot file into a JSON object."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageContractError(f"GCS snapshot is unreadable: {source}") from exc
    if not isinstance(payload, dict):
        raise StorageContractError("GCS snapshot is not a JSON object")
    return payload


def _validate_gcs_snapshot_payload(
    payload: dict[str, Any],
    *,
    expected_bucket: str,
) -> None:
    """Validate a GCS snapshot payload against the expected bucket."""
    if (
        set(payload) != {"bucket", "objects", "schema"}
        or payload.get("schema") != "ums-gcs-snapshot-v1"
    ):
        raise StorageContractError("GCS snapshot has an unknown schema")
    bucket = payload.get("bucket")
    objects = payload.get("objects")
    if not isinstance(bucket, str) or not bucket.strip():
        raise StorageContractError("GCS snapshot bucket is missing")
    if bucket != expected_bucket:
        raise StorageContractError(
            f"GCS snapshot bucket must match configured bucket {expected_bucket!r}"
        )
    if not isinstance(objects, list) or not objects:
        raise StorageContractError("GCS snapshot must contain generation-pinned objects")
    names: set[str] = set()
    for record in objects:
        _require_gcs_snapshot_record(record, names)
        names.add(str(record.get("name")))


def _require_gcs_snapshot_record(record: object, names: set[str]) -> None:
    """Validate one generation-pinned GCS snapshot object record."""
    if not isinstance(record, dict) or set(record) != {"crc32c", "generation", "name"}:
        raise StorageContractError("GCS snapshot object record is malformed")
    name = record.get("name")
    generation = record.get("generation")
    crc32c = record.get("crc32c")
    if (
        not isinstance(name, str)
        or not name
        or name in names
        or not isinstance(generation, str)
        or not generation.isdecimal()
        or int(generation) <= 0
        or not isinstance(crc32c, str)
    ):
        raise StorageContractError("GCS snapshot object record is invalid")
    try:
        decoded_crc32c = base64.b64decode(crc32c, validate=True)
    except ValueError as exc:
        raise StorageContractError("GCS snapshot CRC32C is not canonical base64") from exc
    # FIX: A non-empty arbitrary string previously passed as a checksum and
    # could make an unrelated provider inventory look recovery-complete.
    if len(decoded_crc32c) != 4 or base64.b64encode(decoded_crc32c).decode("ascii") != crc32c:
        raise StorageContractError("GCS snapshot CRC32C must encode exactly four bytes")


def _required_compose_recovery_members(record_names: set[str]) -> set[str]:
    """Return the fixed outer members plus one structurally complete DB package."""
    database_packages: dict[str, set[str]] = {}
    for name in record_names:
        relative = PurePosixPath(name)
        if len(relative.parts) != 2 or relative.name not in DATABASE_RECOVERY_MEMBERS:
            continue
        parent = relative.parts[0]
        if DATABASE_RUN_NAME_RE.fullmatch(parent):
            database_packages.setdefault(parent, set()).add(relative.name)
    if len(database_packages) != 1:
        raise StorageContractError(
            "compose recovery manifest requires exactly one structural database backup package"
        )
    run_name, members = next(iter(database_packages.items()))
    if members != DATABASE_RECOVERY_MEMBERS:
        missing = sorted(DATABASE_RECOVERY_MEMBERS - members)
        raise StorageContractError(
            "structural database backup package is incomplete; missing: " + ", ".join(missing)
        )
    return set(REQUIRED_RECOVERY_MEMBERS) | {
        f"{run_name}/{member}" for member in DATABASE_RECOVERY_MEMBERS
    }


# ============================================================================
# Purpose: Seal database, role, and artifact backups into one checksum manifest.
# Database/ORM: PostgreSQL dumps are opaque files; no live database is accessed.
# Standards: Record relative names, sizes, SHA-256 hashes, and restrictive mode.
# Blast Radius: Backup integrity metadata only; runtime state is unchanged.
# Connections:
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> backup and recovery gates.
#   - File: docker-compose.yml -> documents the three coordinated data planes.
# ============================================================================


def _bundle_member_records(
    files: list[Path], manifest: Path, bundle_root: Path
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Build one digest record per bundle member, capturing a GCS snapshot."""
    records: list[dict[str, Any]] = []
    gcs_snapshot_payload: dict[str, Any] | None = None
    for file_path in files:
        unresolved_candidate = file_path.expanduser()
        if _is_redirect(unresolved_candidate):
            raise StorageContractError(f"manifest member may not be a link: {file_path}")
        candidate = unresolved_candidate.resolve(strict=True)
        if not candidate.is_file():
            raise StorageContractError(f"manifest member is not a regular file: {candidate}")
        try:
            relative = candidate.relative_to(bundle_root)
        except ValueError as exc:
            raise StorageContractError(
                "manifest members must be inside the bundle directory"
            ) from exc
        if candidate == manifest:
            raise StorageContractError("manifest cannot include itself")
        relative_name = relative.as_posix()
        if relative_name == GCS_SNAPSHOT_MEMBER:
            snapshot_bytes = candidate.read_bytes()
            actual_digest = hashlib.sha256(snapshot_bytes).hexdigest()
            actual_size = len(snapshot_bytes)
            gcs_snapshot_payload = _decode_gcs_snapshot(snapshot_bytes, source=candidate)
        else:
            actual_digest = _sha256(candidate)
            actual_size = candidate.stat().st_size
        records.append(
            {
                "path": relative_name,
                "sha256": actual_digest,
                "size": actual_size,
            }
        )
    return records, gcs_snapshot_payload


def _require_complete_recovery_members(records: list[dict[str, Any]]) -> None:
    """Refuse a coordinated recovery bundle that is partial or hollow."""
    record_names = {record["path"] for record in records}
    required_recovery_members = _required_compose_recovery_members(record_names)
    missing = sorted(required_recovery_members - record_names)
    if missing:
        raise StorageContractError(
            "compose recovery manifest is incomplete; missing: " + ", ".join(missing)
        )
    empty = sorted(
        record["path"]
        for record in records
        if record["path"] in required_recovery_members and not record["size"]
    )
    if empty:
        raise StorageContractError(
            "compose recovery manifest has empty required members: " + ", ".join(empty)
        )


def _require_gcs_snapshot_when_needed(
    records: list[dict[str, Any]],
    gcs_snapshot_payload: dict[str, Any] | None,
    *,
    blob_backend: str,
    expected_gcs_bucket: str | None,
) -> None:
    """Require and validate the GCS snapshot record when the backend is gcs."""
    if blob_backend != "gcs":
        return
    record_names = {record["path"] for record in records}
    if GCS_SNAPSHOT_MEMBER not in record_names:
        raise StorageContractError(f"GCS recovery manifest requires {GCS_SNAPSHOT_MEMBER}")
    gcs_snapshot_size = next(
        (record["size"] for record in records if record["path"] == GCS_SNAPSHOT_MEMBER),
        0,
    )
    if not gcs_snapshot_size:
        raise StorageContractError("GCS snapshot record must not be empty")
    if gcs_snapshot_payload is None:
        raise StorageContractError("GCS snapshot bytes were not captured")
    _validate_gcs_snapshot_payload(
        gcs_snapshot_payload,
        expected_bucket=_configured_gcs_bucket(expected_gcs_bucket),
    )


def create_bundle_manifest(
    output: Path,
    files: list[Path],
    *,
    profile: str = COMPOSE_RECOVERY_PROFILE,
    blob_backend: str = "file-store",
    expected_gcs_bucket: str | None = None,
    repository_root: Path | None = None,
) -> Path:
    """Write a SHA-256 manifest for all members of one external backup bundle."""
    if profile not in {COMPOSE_RECOVERY_PROFILE, GENERIC_BACKUP_PROFILE}:
        raise StorageContractError(f"unknown backup manifest profile: {profile}")
    if blob_backend not in {"file-store", "gcs"}:
        raise StorageContractError(f"unknown backup blob backend: {blob_backend}")
    repository = (repository_root or _repository_root()).resolve(strict=False)
    manifest = _assert_sensitive_output(output, repository_root=repository)
    bundle_root = manifest.parent
    records, gcs_snapshot_payload = _bundle_member_records(files, manifest, bundle_root)
    if not records:
        raise StorageContractError("backup manifest needs at least one file")
    record_names = {record["path"] for record in records}
    if len(record_names) != len(records):
        raise StorageContractError("backup manifest contains duplicate members")
    if profile == COMPOSE_RECOVERY_PROFILE:
        # FIX: A partial set could previously be sealed and described as a
        # complete coordinated recovery bundle.
        _require_complete_recovery_members(records)
        _require_gcs_snapshot_when_needed(
            records,
            gcs_snapshot_payload,
            blob_backend=blob_backend,
            expected_gcs_bucket=expected_gcs_bucket,
        )
    _write_json_exclusive(
        manifest,
        {
            "blob_backend": blob_backend,
            "files": sorted(records, key=lambda record: record["path"]),
            "profile": profile,
            "schema": MANIFEST_NAME,
        },
    )
    return manifest


def verify_bundle_manifest(
    manifest: Path,
    *,
    required_profile: str | None = None,
    required_blob_backend: str | None = None,
    expected_gcs_bucket: str | None = None,
    _pinned_files: dict[str, BinaryIO] | None = None,
    _manifest_digest_out: list[str] | None = None,
    _verified_digests_out: dict[str, str] | None = None,
) -> dict[str, Path]:
    """Verify every size and SHA-256 recorded in a backup bundle manifest."""
    resolved_manifest, manifest_bytes = _resolved_manifest(manifest)
    payload = _decoded_manifest_object(manifest, manifest_bytes)
    if _manifest_digest_out is not None:
        _manifest_digest_out.append(hashlib.sha256(manifest_bytes).hexdigest())
    profile, blob_backend, records = _manifest_envelope(
        payload,
        manifest,
        required_profile=required_profile,
        required_blob_backend=required_blob_backend,
    )
    verified: dict[str, Path] = {}
    verified_sizes: dict[str, int] = {}
    gcs_snapshot_payload: dict[str, Any] | None = None
    bundle_root = resolved_manifest.parent
    pinned_files = _pinned_files or {}
    for record in records:
        name, candidate, actual_size, actual_digest, snapshot_payload = _verified_member(
            record, bundle_root=bundle_root, pinned_files=pinned_files
        )
        if snapshot_payload is not None:
            gcs_snapshot_payload = snapshot_payload
        if name in verified:
            raise StorageContractError(f"duplicate backup manifest path: {name}")
        verified[name] = candidate
        verified_sizes[name] = actual_size
        if _verified_digests_out is not None:
            _verified_digests_out[name] = actual_digest
    if profile == COMPOSE_RECOVERY_PROFILE:
        _require_complete_verified_members(
            verified,
            verified_sizes,
            blob_backend=blob_backend,
            expected_gcs_bucket=expected_gcs_bucket,
            gcs_snapshot_payload=gcs_snapshot_payload,
        )
    return verified


def _resolved_manifest(manifest: Path) -> tuple[Path, bytes]:
    """Resolve and read one backup manifest, refusing links."""
    unresolved_manifest = manifest.expanduser()
    if _is_redirect(unresolved_manifest):
        raise StorageContractError(f"contract file may not be a link: {manifest}")
    resolved_manifest = unresolved_manifest.resolve(strict=True)
    try:
        manifest_bytes = resolved_manifest.read_bytes()
    except OSError as exc:
        raise StorageContractError(f"storage contract is unreadable: {manifest}") from exc
    return resolved_manifest, manifest_bytes


def _decoded_manifest_object(manifest: Path, manifest_bytes: bytes) -> dict[str, Any]:
    """Decode one manifest into a JSON object with the exact member schema."""
    try:
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageContractError(f"storage contract is unreadable: {manifest}") from exc
    if not isinstance(payload, dict):
        raise StorageContractError(f"storage contract is not a JSON object: {manifest}")
    if (
        set(payload) != {"blob_backend", "files", "profile", "schema"}
        or payload.get("schema") != MANIFEST_NAME
    ):
        raise StorageContractError(f"unknown backup manifest schema: {manifest}")
    return payload


def _manifest_envelope(
    payload: dict[str, Any],
    manifest: Path,
    *,
    required_profile: str | None,
    required_blob_backend: str | None,
) -> tuple[str, str, list[Any]]:
    """Validate the profile/backend envelope and return its file records."""
    blob_backend = payload.get("blob_backend")
    if blob_backend not in {"file-store", "gcs"}:
        raise StorageContractError(f"unknown backup blob backend in {manifest}: {blob_backend}")
    if required_blob_backend is not None and blob_backend != required_blob_backend:
        raise StorageContractError(
            f"backup manifest blob backend must be {required_blob_backend}, not {blob_backend}"
        )
    profile = payload.get("profile")
    if profile not in {COMPOSE_RECOVERY_PROFILE, GENERIC_BACKUP_PROFILE}:
        raise StorageContractError(f"unknown backup manifest profile in {manifest}: {profile}")
    if required_profile is not None and profile != required_profile:
        raise StorageContractError(
            f"backup manifest profile must be {required_profile}, not {profile}"
        )
    records = payload.get("files")
    if not isinstance(records, list) or not records:
        raise StorageContractError("backup manifest has no file records")
    return str(profile), str(blob_backend), records


def _verified_member(
    record: Any,
    *,
    bundle_root: Path,
    pinned_files: dict[str, BinaryIO],
) -> tuple[str, Path, int, str, dict[str, Any] | None]:
    """Verify one manifest record against its on-disk (or pinned) member."""
    name, digest, size = _member_record_fields(record)
    candidate = _member_bundle_path(name, bundle_root=bundle_root)
    pinned = pinned_files.get(name)
    if pinned is not None:
        pinned_stat = os.fstat(pinned.fileno())
        candidate_stat = candidate.stat()
        if (pinned_stat.st_dev, pinned_stat.st_ino) != (
            candidate_stat.st_dev,
            candidate_stat.st_ino,
        ):
            raise StorageContractError(f"backup member changed during verification: {name}")
    actual_size, actual_digest, snapshot_payload = _member_digest(
        name, candidate, pinned=pinned
    )
    if actual_size != size or actual_digest != digest:
        raise StorageContractError(f"backup checksum mismatch: {name}")
    return name, candidate, actual_size, actual_digest, snapshot_payload


def _member_record_fields(record: Any) -> tuple[str, str, int]:
    """Validate one manifest record's shape and field values."""
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
        raise StorageContractError("backup manifest record is malformed")
    name = record["path"]
    digest = record["sha256"]
    size = record["size"]
    if (
        not isinstance(name, str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
    ):
        raise StorageContractError("backup manifest record has invalid field values")
    return name, digest, size


def _member_bundle_path(name: str, *, bundle_root: Path) -> Path:
    """Resolve one member path inside its bundle, refusing escapes."""
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise StorageContractError(f"unsafe backup manifest path: {name}")
    unresolved_candidate = bundle_root.joinpath(*relative.parts)
    _reject_existing_redirects(unresolved_candidate, stop_at=bundle_root)
    candidate = unresolved_candidate.resolve(strict=True)
    if not candidate.is_relative_to(bundle_root) or not candidate.is_file():
        raise StorageContractError(f"backup member escapes the bundle: {name}")
    return candidate


def _member_digest(
    name: str, candidate: Path, *, pinned: BinaryIO | None
) -> tuple[int, str, dict[str, Any] | None]:
    """Digest one member, capturing a GCS snapshot payload when present."""
    if name == GCS_SNAPSHOT_MEMBER:
        if pinned is not None:
            position = pinned.tell()
            try:
                pinned.seek(0)
                snapshot_bytes = pinned.read()
            finally:
                pinned.seek(position)
        else:
            snapshot_bytes = candidate.read_bytes()
        actual_size = len(snapshot_bytes)
        actual_digest = hashlib.sha256(snapshot_bytes).hexdigest()
        snapshot_payload = _decode_gcs_snapshot(snapshot_bytes, source=candidate)
        return actual_size, actual_digest, snapshot_payload
    if pinned is not None:
        actual_size = os.fstat(pinned.fileno()).st_size
        return actual_size, _sha256_stream(pinned), None
    return candidate.stat().st_size, _sha256(candidate), None


def _require_complete_verified_members(
    verified: dict[str, Path],
    verified_sizes: dict[str, int],
    *,
    blob_backend: str,
    expected_gcs_bucket: str | None,
    gcs_snapshot_payload: dict[str, Any] | None,
) -> None:
    """Refuse a coordinated recovery bundle that verified as partial."""
    required_recovery_members = _required_compose_recovery_members(set(verified))
    missing = sorted(required_recovery_members - verified.keys())
    if missing:
        raise StorageContractError(
            "compose recovery manifest is incomplete; missing: " + ", ".join(missing)
        )
    empty = sorted(name for name in required_recovery_members if verified_sizes[name] == 0)
    if empty:
        raise StorageContractError(
            "compose recovery manifest has empty required members: " + ", ".join(empty)
        )
    if blob_backend == "gcs" and GCS_SNAPSHOT_MEMBER not in verified:
        raise StorageContractError(f"GCS recovery manifest requires {GCS_SNAPSHOT_MEMBER}")
    if blob_backend == "gcs" and verified_sizes[GCS_SNAPSHOT_MEMBER] == 0:
        raise StorageContractError("GCS snapshot record must not be empty")
    if blob_backend == "gcs":
        if gcs_snapshot_payload is None:
            raise StorageContractError("GCS snapshot bytes were not captured")
        _validate_gcs_snapshot_payload(
            gcs_snapshot_payload,
            expected_bucket=_configured_gcs_bucket(expected_gcs_bucket),
        )


def _ensure_empty_restore_target(storage: Path) -> None:
    """Refuse to initialize restore storage over existing content."""
    pending = storage / RESTORE_PENDING_FILENAME
    if pending.exists():
        raise StorageContractError("an earlier restore is still pending container initialization")
    if (storage / READY_FILENAME).exists():
        raise StorageContractError("restore target was already initialized; prepare a new target")
    journal = storage / RESTORE_JOURNAL_FILENAME
    if journal.exists():
        raise StorageContractError("an earlier restore publication has not been reconciled")
    if any(storage.glob(f"{RESTORE_STAGE_PREFIX}*")):
        raise StorageContractError("an unjournaled restore stage requires operator review")
    for name in STORAGE_DIRECTORIES:
        root = storage / name
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise StorageContractError(f"restore target is not empty: {root}")


# ============================================================================
# Purpose: Journal two-root restore publication for rollback and crash retry.
# Database/ORM: None; the verified database restore remains a prior step.
# Standards: Exact state schema, same-filesystem renames, fail-closed recovery.
# Blast Radius: Marked empty artifact/blob recovery targets only.
# Connections:
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> operator retry procedure.
#   - File: docker-compose.yml -> app-data-init consumes the pending marker.
# ============================================================================


def _restore_journal_payload(
    archive_digest: str,
    manifest_digest: str,
    blob_backend: str,
    gcs_bucket: str | None,
    stage_name: str,
) -> dict[str, Any]:
    """Build the journal payload describing a staged restore."""
    return {
        "archive_sha256": archive_digest,
        "blob_backend": blob_backend,
        "contract": CONTRACT_NAME,
        "gcs_bucket": gcs_bucket,
        "manifest_sha256": manifest_digest,
        "published": [],
        "stage": stage_name,
        "state": "staging",
    }


def _validate_restore_journal(
    storage: Path,
    payload: dict[str, Any],
    archive_digest: str,
    manifest_digest: str,
    blob_backend: str,
    gcs_bucket: str | None,
    *,
    allow_missing_stage: bool = False,
) -> Path:
    """Validate a staged restore journal against the declared digests."""
    _require_journal_schema(payload)
    _require_journal_identity(payload, archive_digest, manifest_digest, blob_backend, gcs_bucket)
    stage_name, published = _require_journal_stage_fields(payload)
    if payload["state"] == "staging":
        _require_clean_staging_state(storage, published)
    return _require_journal_stage(
        storage,
        stage_name,
        state=str(payload["state"]),
        allow_missing_stage=allow_missing_stage,
    )


def _require_journal_schema(payload: dict[str, Any]) -> None:
    """Refuse a journal whose field set, contract, or state is unknown."""
    if set(payload) != {
        "archive_sha256",
        "blob_backend",
        "contract",
        "gcs_bucket",
        "manifest_sha256",
        "published",
        "stage",
        "state",
    }:
        raise StorageContractError("restore journal has unexpected fields")
    if payload.get("contract") != CONTRACT_NAME or payload.get("state") not in {
        "complete",
        "initialized",
        "publishing",
        "rolled-back",
        "rolling-back",
        "staging",
    }:
        raise StorageContractError("restore journal has an unknown contract or state")


def _require_journal_identity(
    payload: dict[str, Any],
    archive_digest: str,
    manifest_digest: str,
    blob_backend: str,
    gcs_bucket: str | None,
) -> None:
    """Require the journal to carry one well-formed, matching identity."""
    journal_backend = payload.get("blob_backend")
    journal_bucket = payload.get("gcs_bucket")
    if (
        journal_backend not in {"file-store", "gcs"}
        or (journal_backend == "file-store" and journal_bucket is not None)
        or (
            journal_backend == "gcs" and (not isinstance(journal_bucket, str) or not journal_bucket)
        )
        or not _journal_digests_well_formed(payload)
    ):
        raise StorageContractError("restore journal recovery identity is malformed")
    if (
        payload.get("archive_sha256") != archive_digest
        or payload.get("manifest_sha256") != manifest_digest
        or payload.get("blob_backend") != blob_backend
        or payload.get("gcs_bucket") != gcs_bucket
    ):
        raise StorageContractError("restore journal belongs to a different recovery contract")



def _journal_digests_well_formed(payload: dict[str, Any]) -> bool:
    """Return whether both journal recovery digests are lowercase SHA-256."""
    return not any(
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for digest in (
            payload.get("archive_sha256"),
            payload.get("manifest_sha256"),
        )
    )


def _require_journal_stage_fields(payload: dict[str, Any]) -> tuple[str, list[str]]:
    """Validate the stage name and published-members list shapes."""
    stage_name = payload.get("stage")
    published = payload.get("published")
    if (
        not isinstance(stage_name, str)
        or not stage_name.startswith(RESTORE_STAGE_PREFIX)
        or Path(stage_name).name != stage_name
        or not isinstance(published, list)
        or any(not isinstance(name, str) for name in published)
        or len(published) != len(set(published))
        or any(name not in STORAGE_DIRECTORIES for name in published)
    ):
        raise StorageContractError("restore journal is malformed")
    return stage_name, published


def _require_clean_staging_state(storage: Path, published: list[str]) -> None:
    """Refuse a staging journal that already carries publication state."""
    if published or (storage / RESTORE_PENDING_FILENAME).exists():
        raise StorageContractError("staging restore journal has publication state")
    for name in STORAGE_DIRECTORIES:
        root = storage / name
        if root.exists() and (not root.is_dir() or _is_redirect(root) or any(root.iterdir())):
            raise StorageContractError("staging restore target is not empty")


def _require_journal_stage(
    storage: Path,
    stage_name: str,
    *,
    state: str,
    allow_missing_stage: bool,
) -> Path:
    """Validate the journaled stage directory against on-disk reality."""
    stage = storage / stage_name
    unexpected_stages = [
        candidate for candidate in storage.glob(f"{RESTORE_STAGE_PREFIX}*") if candidate != stage
    ]
    if unexpected_stages:
        raise StorageContractError("restore target contains an unjournaled stage")
    if stage.exists() and (not stage.is_dir() or _is_redirect(stage)):
        raise StorageContractError("restore journal stage is missing or redirected")
    if not stage.exists() and state != "staging" and not allow_missing_stage:
        raise StorageContractError("restore journal stage is missing or redirected")
    return stage


def _required_journal_string(journal: dict[str, object], key: str) -> str:
    """Return one required journal string field with its type proven."""
    value = journal.get(key)
    if not isinstance(value, str):
        raise StorageContractError(f"restore journal field {key} is missing or not a string")
    return value


def _optional_journal_string(journal: dict[str, object], key: str) -> str | None:
    """Return one optional journal string field with its type proven."""
    value = journal.get(key)
    if value is None or isinstance(value, str):
        return value
    raise StorageContractError(f"restore journal field {key} is not a string")


def _pending_restore_payload(
    archive_digest: str,
    manifest_digest: str,
    blob_backend: str,
    gcs_bucket: str | None,
) -> dict[str, str | None]:
    """Build the pending-restore record for a staged publication."""
    return {
        "archive_sha256": archive_digest,
        "blob_backend": blob_backend,
        "contract": CONTRACT_NAME,
        "gcs_bucket": gcs_bucket,
        "manifest_sha256": manifest_digest,
        "state": "ownership-pending",
    }


def _validate_pending_restore_payload(payload: dict[str, Any]) -> None:
    """Validate the shape of a pending-restore record."""
    archive_digest = payload.get("archive_sha256")
    manifest_digest = payload.get("manifest_sha256")
    blob_backend = payload.get("blob_backend")
    gcs_bucket = payload.get("gcs_bucket")
    if (
        set(payload)
        != {
            "archive_sha256",
            "blob_backend",
            "contract",
            "gcs_bucket",
            "manifest_sha256",
            "state",
        }
        or payload.get("contract") != CONTRACT_NAME
        or payload.get("state") != "ownership-pending"
        or blob_backend not in {"file-store", "gcs"}
        or (blob_backend == "file-store" and gcs_bucket is not None)
        or (blob_backend == "gcs" and (not isinstance(gcs_bucket, str) or not gcs_bucket))
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in (archive_digest, manifest_digest)
        )
    ):
        raise StorageContractError("restore-pending marker is malformed")


def _rollback_restore_publication(
    storage: Path,
    stage: Path,
    *,
    archive_digest: str,
    manifest_digest: str,
    blob_backend: str,
    gcs_bucket: str | None,
    journal: dict[str, Any],
) -> None:
    """Undo a partially published restore and restore the pending marker."""
    journal_path = storage / RESTORE_JOURNAL_FILENAME
    journal["state"] = "rolling-back"
    _write_json_atomic(journal_path, journal)
    pending = storage / RESTORE_PENDING_FILENAME
    if pending.exists():
        if _read_json(pending) != _pending_restore_payload(
            archive_digest,
            manifest_digest,
            blob_backend,
            gcs_bucket,
        ):
            raise StorageContractError("restore-pending marker does not match the journal")
        pending.unlink()
        _sync_directory(storage)
    for name in reversed(STORAGE_DIRECTORIES):
        source = stage / name
        destination = storage / name
        if source.exists() and destination.exists():
            raise StorageContractError(f"restore rollback found both copies of {name}")
        if destination.exists():
            if not destination.is_dir() or _is_redirect(destination):
                raise StorageContractError(f"restore rollback target is unsafe: {destination}")
            # Rollback swaps direction: the staged copy becomes the source
            # and the published location becomes the replacement target.
            _durable_replace(source=destination, destination=source)
    journal["published"] = []
    journal["state"] = "rolled-back"
    _write_json_atomic(journal_path, journal)


def _finish_pending_restore_initialization(storage: Path) -> None:
    """Complete a previously interrupted storage initialization."""
    for name in STORAGE_DIRECTORIES:
        root = storage / name
        if not root.is_dir() or _is_redirect(root):
            raise StorageContractError(f"completed restore root is missing: {root}")
    pending_path = storage / RESTORE_PENDING_FILENAME
    pending_payload: dict[str, Any] | None = None
    if pending_path.exists():
        pending_payload = _read_json(pending_path)
        _validate_pending_restore_payload(pending_payload)
    journal_path = storage / RESTORE_JOURNAL_FILENAME
    if journal_path.exists():
        journal = _read_json(journal_path)
        archive_digest = journal.get("archive_sha256")
        manifest_digest = _required_journal_string(journal, "manifest_sha256")
        archive_digest = _required_journal_string(journal, "archive_sha256")
        manifest_digest = _required_journal_string(journal, "manifest_sha256")
        blob_backend = _required_journal_string(journal, "blob_backend")
        gcs_bucket = _optional_journal_string(journal, "gcs_bucket")
        stage = _validate_restore_journal(
            storage,
            journal,
            archive_digest,
            manifest_digest,
            blob_backend,
            gcs_bucket,
            allow_missing_stage=True,
        )
        if pending_payload is not None and pending_payload != _pending_restore_payload(
            archive_digest,
            manifest_digest,
            blob_backend,
            gcs_bucket,
        ):
            raise StorageContractError("restore-pending marker does not match the journal")
        if journal["state"] not in {"complete", "initialized"}:
            raise StorageContractError("restore journal is not ready for initialization cleanup")
        if journal["state"] == "complete":
            journal["state"] = "initialized"
            _write_json_atomic(journal_path, journal)
        if stage.exists():
            if any(stage.iterdir()):
                raise StorageContractError("completed restore stage is unexpectedly non-empty")
            _remove_empty_stage(stage, storage)
        _unlink_and_sync(journal_path)
    if pending_path.exists():
        _unlink_and_sync(pending_path)


def _remove_empty_stage(stage: Path, storage: Path) -> None:
    """Remove a stage directory only when it holds nothing."""
    stage.rmdir()
    _sync_directory(storage)


def _unlink_and_sync(path: Path) -> None:
    """Unlink a file and flush its parent directory entry."""
    path.unlink()
    _sync_directory(path.parent)


def _staged_restore_locations(
    storage: Path, stage: Path
) -> tuple[dict[str, Path], set[str]]:
    """Locate exactly one copy of each storage root and list its members."""
    locations: dict[str, Path] = {}
    actual_names: set[str] = set()
    for name in STORAGE_DIRECTORIES:
        staged = stage / name
        published = storage / name
        if staged.exists() == published.exists():
            raise StorageContractError(f"restore verification needs exactly one copy of {name}")
        root = staged if staged.exists() else published
        if not root.is_dir() or _is_redirect(root):
            raise StorageContractError(f"restore verification root is unsafe: {root}")
        locations[name] = root
        for path in _walk_without_redirects(root):
            suffix = path.relative_to(root)
            actual_names.add(PurePosixPath(name, *suffix.parts).as_posix())
    return locations, actual_names


def _require_restored_member(
    member: tarfile.TarInfo,
    *,
    locations: dict[str, Path],
    handle: tarfile.TarFile,
) -> None:
    """Compare one restored member's type, size, and bytes to the archive."""
    relative = PurePosixPath(member.name)
    candidate = locations[relative.parts[0]].joinpath(*relative.parts[1:])
    if member.isdir():
        if not candidate.is_dir() or _is_redirect(candidate):
            raise StorageContractError(f"restored directory type mismatch: {member.name}")
        return
    if not candidate.is_file() or candidate.stat().st_size != member.size:
        raise StorageContractError(f"restored file size mismatch: {member.name}")
    source = handle.extractfile(member)
    if source is None:
        raise StorageContractError(f"archive file has no payload: {member.name}")
    with source, candidate.open("rb") as restored:
        while True:
            expected_chunk = source.read(CHUNK_SIZE)
            restored_chunk = restored.read(CHUNK_SIZE)
            if expected_chunk != restored_chunk:
                raise StorageContractError(f"restored file content mismatch: {member.name}")
            if not expected_chunk:
                break


def _verify_restore_bytes(storage: Path, stage: Path, archive: BinaryIO) -> None:
    """Stream-compare restored bytes against the source archive."""
    locations, actual_names = _staged_restore_locations(storage, stage)

    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:gz") as handle:
        members = _validated_archive_members(handle)
        expected_names = {PurePosixPath(member.name).as_posix() for member in members}
        if actual_names != expected_names:
            raise StorageContractError("restore stage entries do not match the artifact archive")
        for member in members:
            _require_restored_member(member, locations=locations, handle=handle)


# ============================================================================
# Purpose: Rebuild only the partial stage named by durable restore intent.
# Database/ORM: None.
# Standards: Reject redirects/special entries; fsync intent before extraction.
# Blast Radius: One marked, empty recovery target; published roots are excluded.
# Connections:
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> crash retry contract.
#   - File: tests/scripts/test_compose_storage.py -> interruption counterexamples.
# ============================================================================


def _discard_journaled_stage(stage: Path, storage: Path) -> None:
    """Remove only a redirect-free stage named by the durable restore journal."""
    if not stage.exists():
        return
    paths = _walk_without_redirects(stage)
    for path in reversed(paths[1:]):
        if path.is_dir():
            path.rmdir()
        elif path.is_file():
            path.unlink()
        else:
            raise StorageContractError(f"restore stage contains a special entry: {path}")
    _remove_empty_stage(stage, storage)


def _stage_verified_restore(
    storage: Path,
    stage: Path,
    archive: BinaryIO,
    journal: dict[str, Any],
) -> None:
    """Restart journaled staging from zero, then publish its durable state."""
    journal_path = storage / RESTORE_JOURNAL_FILENAME
    if _sha256_stream(archive) != journal["archive_sha256"]:
        raise StorageContractError("verified artifact archive changed before staging")
    _discard_journaled_stage(stage, storage)
    stage.mkdir(mode=0o700)
    _sync_directory(storage)
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:gz") as handle:
        members = _validated_archive_members(handle)
        for member in members:
            destination = stage.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = handle.extractfile(member)
            if source is None:
                raise StorageContractError(f"archive file has no payload: {member.name}")
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with source, os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(source, output, length=CHUNK_SIZE)
                output.flush()
                os.fsync(output.fileno())
    for path in reversed(_walk_without_redirects(stage)):
        if path.is_dir():
            _sync_directory(path)
    for name in STORAGE_DIRECTORIES:
        destination = storage / name
        if destination.exists():
            if not destination.is_dir() or _is_redirect(destination) or any(destination.iterdir()):
                raise StorageContractError(f"restore target is not empty: {destination}")
            destination.rmdir()
            _sync_directory(storage)
    _verify_restore_bytes(storage, stage, archive)
    if _sha256_stream(archive) != journal["archive_sha256"]:
        raise StorageContractError("verified artifact archive changed during staging")
    journal["state"] = "publishing"
    _write_json_atomic(journal_path, journal)


# ============================================================================
# Purpose: Restore verified artifact/blob bytes into an empty prepared target.
# Database/ORM: None; roles and database restore remain explicit prior steps.
# Standards: Verify complete bundle, journal publication, and roll back on error.
# Blast Radius: Writes only the marked empty storage target selected by operator.
# Connections:
#   - File: docker-compose.yml -> app-data-init adopts restored ownership.
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> coordinated recovery order.
# ============================================================================


def restore_artifact_archive(
    raw_path: str,
    *,
    archive: Path,
    manifest: Path,
    blob_backend: str = "file-store",
    expected_gcs_bucket: str | None = None,
    repository_root: Path | None = None,
) -> None:
    """Restore one verified archive without overwriting existing storage bytes."""
    if blob_backend not in {"file-store", "gcs"}:
        raise StorageContractError(f"unknown backup blob backend: {blob_backend}")
    if blob_backend == "gcs" and expected_gcs_bucket is None:
        raise StorageContractError("GCS restore requires an explicit expected bucket")
    gcs_bucket = _configured_gcs_bucket(expected_gcs_bucket) if blob_backend == "gcs" else None
    repository = (repository_root or _repository_root()).resolve(strict=False)
    storage = check_host_storage(raw_path, repository_root=repository)
    resolved_manifest = manifest.expanduser().resolve(strict=True)
    resolved_archive = archive.expanduser().resolve(strict=True)
    if _is_redirect(resolved_archive):
        raise StorageContractError("artifact archive may not be a link")
    manifest_digests: list[str] = []
    verified_digests: dict[str, str] = {}
    with resolved_archive.open("rb") as archive_handle:
        verified = verify_bundle_manifest(
            resolved_manifest,
            required_profile=COMPOSE_RECOVERY_PROFILE,
            required_blob_backend=blob_backend,
            expected_gcs_bucket=gcs_bucket,
            _pinned_files={"ums-app-data.tgz": archive_handle},
            _manifest_digest_out=manifest_digests,
            _verified_digests_out=verified_digests,
        )
        if verified.get("ums-app-data.tgz") != resolved_archive:
            raise StorageContractError(
                "artifact archive must be the verified ums-app-data.tgz bundle member"
            )
        if len(manifest_digests) != 1 or "ums-app-data.tgz" not in verified_digests:
            raise StorageContractError("recovery bundle verification did not pin its inputs")
        if _sha256_stream(archive_handle) != verified_digests["ums-app-data.tgz"]:
            raise StorageContractError("verified artifact archive changed after verification")
        _restore_artifact_archive_from_stream(
            storage,
            archive_handle,
            archive_digest=verified_digests["ums-app-data.tgz"],
            manifest_digest=manifest_digests[0],
            blob_backend=blob_backend,
            gcs_bucket=gcs_bucket,
        )


def _stage_verified_restore_wrapped(
    storage: Path, stage: Path, archive: BinaryIO, journal: dict[str, Any]
) -> None:
    """Stage one verified restore, wrapping unexpected failures as typed."""
    try:
        # FIX: The journal is durable before stage creation. A retry
        # discards only its bound partial tree and re-extracts exactly.
        _stage_verified_restore(storage, stage, archive, journal)
    except Exception as exc:
        if isinstance(exc, StorageContractError):
            raise
        raise StorageContractError("artifact restore staging failed") from exc


def _resolve_restore_state(
    storage: Path,
    archive: BinaryIO,
    *,
    archive_digest: str,
    manifest_digest: str,
    blob_backend: str,
    gcs_bucket: str | None,
) -> tuple[Path, dict[str, Any], Path]:
    """Reconcile or create the durable restore journal and its stage."""
    journal_path = storage / RESTORE_JOURNAL_FILENAME
    if journal_path.exists():
        return _existing_restore_state(
            storage,
            archive,
            journal_path=journal_path,
            archive_digest=archive_digest,
            manifest_digest=manifest_digest,
            blob_backend=blob_backend,
            gcs_bucket=gcs_bucket,
        )
    return _new_restore_state(
        storage,
        archive,
        journal_path=journal_path,
        archive_digest=archive_digest,
        manifest_digest=manifest_digest,
        blob_backend=blob_backend,
        gcs_bucket=gcs_bucket,
    )


def _existing_restore_state(
    storage: Path,
    archive: BinaryIO,
    *,
    journal_path: Path,
    archive_digest: str,
    manifest_digest: str,
    blob_backend: str,
    gcs_bucket: str | None,
) -> tuple[Path, dict[str, Any], Path]:
    """Advance one journaled restore to its next retry-safe step."""
    journal = _read_json(journal_path)
    stage = _validate_restore_journal(
        storage,
        journal,
        archive_digest,
        manifest_digest,
        blob_backend,
        gcs_bucket,
        allow_missing_stage=journal.get("state") == "initialized",
    )
    if journal["state"] == "staging":
        _stage_verified_restore_wrapped(storage, stage, archive, journal)
    else:
        _verify_restore_bytes(storage, stage, archive)
        if _sha256_stream(archive) != archive_digest:
            raise StorageContractError("verified artifact archive changed during retry")
    if journal["state"] == "initialized":
        marker = _read_json(storage / MARKER_FILENAME)
        if _read_json(storage / READY_FILENAME) != _ready_payload(marker):
            raise StorageContractError("initialized restore lacks its readiness marker")
        return stage, journal, journal_path
    if journal["state"] == "complete":
        pending = storage / RESTORE_PENDING_FILENAME
        if _read_json(pending) != _pending_restore_payload(
            archive_digest,
            manifest_digest,
            blob_backend,
            gcs_bucket,
        ):
            raise StorageContractError("completed restore lacks its matching pending marker")
        return stage, journal, journal_path
    if journal["state"] == "rolling-back":
        _rollback_restore_publication(
            storage,
            stage,
            archive_digest=archive_digest,
            manifest_digest=manifest_digest,
            blob_backend=blob_backend,
            gcs_bucket=gcs_bucket,
            journal=journal,
        )
    if journal["state"] == "rolled-back":
        journal["state"] = "publishing"
        _write_json_atomic(journal_path, journal)
    return stage, journal, journal_path


def _new_restore_state(
    storage: Path,
    archive: BinaryIO,
    *,
    journal_path: Path,
    archive_digest: str,
    manifest_digest: str,
    blob_backend: str,
    gcs_bucket: str | None,
) -> tuple[Path, dict[str, Any], Path]:
    """Create the durable journal and its first verified stage."""
    _ensure_empty_restore_target(storage)
    stage = storage / f"{RESTORE_STAGE_PREFIX}{secrets.token_hex(8)}"
    journal = _restore_journal_payload(
        archive_digest,
        manifest_digest,
        blob_backend,
        gcs_bucket,
        stage.name,
    )
    # FIX: A stage created before its journal could survive a process or
    # host crash but had no safe identity for retry or cleanup.
    _write_json_exclusive(journal_path, journal)
    _stage_verified_restore_wrapped(storage, stage, archive, journal)
    return stage, journal, journal_path


def _restore_artifact_archive_from_stream(
    storage: Path,
    archive: BinaryIO,
    *,
    archive_digest: str,
    manifest_digest: str,
    blob_backend: str,
    gcs_bucket: str | None,
) -> None:
    """Publish bytes from the same open archive stream verified by the manifest."""
    stage, journal, journal_path = _resolve_restore_state(
        storage,
        archive,
        archive_digest=archive_digest,
        manifest_digest=manifest_digest,
        blob_backend=blob_backend,
        gcs_bucket=gcs_bucket,
    )
    try:
        # FIX: Sequential unjournaled replaces could expose only one storage
        # root after a crash. Every replace is now inferred and retryable.
        for name in STORAGE_DIRECTORIES:
            source = stage / name
            destination = storage / name
            if source.exists() and destination.exists():
                raise StorageContractError(f"restore publication found two copies of {name}")
            if source.exists():
                if not source.is_dir() or _is_redirect(source):
                    raise StorageContractError(f"restore stage root is unsafe: {source}")
                _durable_replace(source, destination)
            elif not destination.is_dir() or _is_redirect(destination):
                raise StorageContractError(f"restore publication lost storage root: {name}")
            if name not in journal["published"]:
                journal["published"].append(name)
                _write_json_atomic(journal_path, journal)

        pending = storage / RESTORE_PENDING_FILENAME
        pending_payload = _pending_restore_payload(
            archive_digest,
            manifest_digest,
            blob_backend,
            gcs_bucket,
        )
        if pending.exists():
            if _read_json(pending) != pending_payload:
                raise StorageContractError(
                    "restore-pending marker does not match the recovery contract"
                )
        else:
            _write_json_exclusive(pending, pending_payload)
        journal["state"] = "complete"
        _write_json_atomic(journal_path, journal)
    except Exception as exc:
        try:
            _rollback_restore_publication(
                storage,
                stage,
                archive_digest=archive_digest,
                manifest_digest=manifest_digest,
                blob_backend=blob_backend,
                gcs_bucket=gcs_bucket,
                journal=journal,
            )
        except Exception as rollback_exc:
            raise StorageContractError(
                "restore publication and rollback are incomplete; rerun the same restore"
            ) from rollback_exc
        raise StorageContractError(
            "artifact restore publication failed and was rolled back"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    """Build the compose storage CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="approve a dedicated host storage path")
    prepare.add_argument("--path", required=True)
    prepare.add_argument("--safe-root", type=Path, default=Path("data"))

    check = subparsers.add_parser("check", help="validate a prepared host storage path")
    check.add_argument("--path", required=True)

    compose = subparsers.add_parser(
        "compose", help="run Docker Compose through canonical host preflight"
    )
    compose.add_argument("--path", required=True)
    compose.add_argument("compose_args", nargs=argparse.REMAINDER)

    container_init = subparsers.add_parser("container-init", help="provision a mounted path")
    container_init.add_argument("--path", required=True, type=Path)
    container_init.add_argument("--app-user", default="app")

    container_exec = subparsers.add_parser(
        "container-exec", help="exec an application only after storage readiness"
    )
    container_exec.add_argument("--path", required=True, type=Path)
    container_exec.add_argument("container_command", nargs=argparse.REMAINDER)

    archive = subparsers.add_parser("archive", help="archive stopped-writer artifact storage")
    archive.add_argument("--path", required=True)
    archive.add_argument("--output", required=True, type=Path)
    archive.add_argument("--writers-stopped", action="store_true")

    mounted_archive = subparsers.add_parser(
        "archive-mounted", help="archive a mounted path from the root init image"
    )
    mounted_archive.add_argument("--path", required=True, type=Path)
    mounted_archive.add_argument("--output", required=True, type=Path)
    mounted_archive.add_argument("--writers-stopped", action="store_true")
    mounted_archive.add_argument("--output-uid", type=int)
    mounted_archive.add_argument("--output-gid", type=int)

    manifest = subparsers.add_parser("manifest", help="create a bundle checksum manifest")
    manifest.add_argument("--output", required=True, type=Path)
    manifest.add_argument(
        "--profile",
        choices=("compose-recovery",),
        default="compose-recovery",
    )
    manifest.add_argument(
        "--blob-backend",
        choices=("file-store", "gcs"),
        default=os.environ.get("UMS_BLOB_BACKEND", "file-store"),
    )
    manifest.add_argument(
        "--gcs-bucket",
        default=os.environ.get("UMS_GCS_BUCKET", DEFAULT_GCS_BUCKET),
        help="expected snapshot bucket when --blob-backend=gcs",
    )
    manifest.add_argument("files", nargs="+", type=Path)

    verify = subparsers.add_parser("verify", help="verify a complete bundle manifest")
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--artifact-archive", type=Path)
    verify.add_argument(
        "--blob-backend",
        choices=("file-store", "gcs"),
        default=os.environ.get("UMS_BLOB_BACKEND", "file-store"),
    )
    verify.add_argument(
        "--gcs-bucket",
        default=os.environ.get("UMS_GCS_BUCKET", DEFAULT_GCS_BUCKET),
        help="expected snapshot bucket when --blob-backend=gcs",
    )

    restore = subparsers.add_parser("restore-artifacts", help="restore into empty prepared storage")
    restore.add_argument("--path", required=True)
    restore.add_argument("--archive", required=True, type=Path)
    restore.add_argument("--manifest", required=True, type=Path)
    restore.add_argument(
        "--blob-backend",
        choices=("file-store", "gcs"),
        default=os.environ.get("UMS_BLOB_BACKEND", "file-store"),
    )
    restore.add_argument(
        "--gcs-bucket",
        default=os.environ.get("UMS_GCS_BUCKET", DEFAULT_GCS_BUCKET),
        help="expected snapshot bucket when --blob-backend=gcs",
    )
    return parser


def _run_prepare(args: argparse.Namespace) -> int:
    """Prepare one storage path and print it."""
    print(prepare_storage(args.path, safe_root=args.safe_root))
    return 0


def _run_check(args: argparse.Namespace) -> int:
    """Check one storage path and print its canonical form."""
    print(check_host_storage(args.path))
    return 0


def _run_compose(args: argparse.Namespace) -> int:
    """Run one compose invocation through the storage preflight."""
    compose_args = args.compose_args
    if compose_args[:1] == ["--"]:
        compose_args = compose_args[1:]
    return run_compose_with_preflight(args.path, compose_args)


def _run_container_init(args: argparse.Namespace) -> int:
    """Initialize mounted storage as the image's app identity."""
    initialize_container_storage(args.path, app_user=args.app_user)
    return 0


def _run_container_exec(args: argparse.Namespace) -> int:
    """Validate storage readiness, then run the requested command."""
    container_command = args.container_command
    if container_command[:1] == ["--"]:
        container_command = container_command[1:]
    exec_with_ready_storage(args.path, container_command)
    return 0


def _run_archive(args: argparse.Namespace) -> int:
    """Archive host storage into one sensitive external archive."""
    print(
        create_artifact_archive(
            args.path,
            output=args.output,
            writers_stopped=args.writers_stopped,
        )
    )
    return 0


def _run_archive_mounted(args: argparse.Namespace) -> int:
    """Archive mounted storage as root, returning host ownership."""
    print(
        create_mounted_artifact_archive(
            args.path,
            output=args.output,
            writers_stopped=args.writers_stopped,
            output_uid=args.output_uid,
            output_gid=args.output_gid,
        )
    )
    return 0


def _run_manifest(args: argparse.Namespace) -> int:
    """Create the coordinated recovery bundle manifest."""
    print(
        create_bundle_manifest(
            args.output,
            args.files,
            profile=COMPOSE_RECOVERY_PROFILE,
            blob_backend=args.blob_backend,
            expected_gcs_bucket=args.gcs_bucket,
        )
    )
    return 0


def _run_verify(args: argparse.Namespace) -> int:
    """Verify one coordinated recovery bundle end to end."""
    verified = verify_bundle_manifest(
        args.manifest,
        required_profile=COMPOSE_RECOVERY_PROFILE,
        required_blob_backend=args.blob_backend,
        expected_gcs_bucket=args.gcs_bucket,
    )
    archive = verified["ums-app-data.tgz"]
    if args.artifact_archive is not None:
        requested_archive = args.artifact_archive.expanduser().resolve(strict=True)
        if requested_archive != archive:
            raise StorageContractError(
                "artifact archive is not the verified ums-app-data.tgz member"
            )
    verify_artifact_archive(archive)
    print(f"verified {len(verified)} backup files")
    return 0


def _run_restore_artifacts(args: argparse.Namespace) -> int:
    """Restore one verified bundle into marked, empty storage."""
    restore_artifact_archive(
        args.path,
        archive=args.archive,
        manifest=args.manifest,
        blob_backend=args.blob_backend,
        expected_gcs_bucket=args.gcs_bucket,
    )
    return 0


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "prepare": _run_prepare,
    "check": _run_check,
    "compose": _run_compose,
    "container-init": _run_container_init,
    "container-exec": _run_container_exec,
    "archive": _run_archive,
    "archive-mounted": _run_archive_mounted,
    "manifest": _run_manifest,
    "verify": _run_verify,
    "restore-artifacts": _run_restore_artifacts,
}


def main(argv: list[str] | None = None) -> int:
    """Run the requested storage safety operation."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _build_parser().parse_args(argv)
    try:
        handler = _COMMAND_HANDLERS.get(args.command)
        if handler is None:  # pragma: no cover - argparse enforces choices.
            raise AssertionError(f"unknown command: {args.command}")
        return handler(args)
    except (OSError, StorageContractError) as exc:
        LOGGER.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
