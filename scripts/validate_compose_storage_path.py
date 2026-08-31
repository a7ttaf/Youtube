"""Validate and optionally provision the host directory used by Compose.

The validator is deliberately dependency-free. It runs on the operator's
host before Docker receives a bind source, and it requires a positive
application-storage contract instead of guessing whether a familiar
directory name is safe.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STORAGE_SOURCE = "./data/ums"
STORAGE_ENV_VAR = "UMS_APP_DATA_HOST"
STORAGE_CHILDREN = ("artifacts", "blobs")
STORAGE_SENTINEL_NAME = ".ums-smart-revenue-storage"
STORAGE_SENTINEL_VERSION = "UMS Smart Revenue storage v1"


class StoragePathError(ValueError):
    """Raised when a Compose storage source is unsafe or unusable."""


def _is_relative_to(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is ``parent`` or a descendant of ``parent``."""

    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _canonical_identity(path: Path) -> str:
    """Return the stable path spelling stored in the application sentinel."""

    return os.path.normcase(str(path))


def storage_path_identity(path: Path) -> str:
    """Expose the exact canonical identity used by the storage sentinel."""

    return _canonical_identity(path)


def storage_sentinel_content(path: Path) -> str:
    """Return the exact marker content bound to one canonical storage path."""

    return f"{STORAGE_SENTINEL_VERSION}\ncanonical_path={_canonical_identity(path)}\n"


def _default_storage_path(root: Path) -> Path:
    """Return the one checkout-local path explicitly reserved for this app."""

    return (root / DEFAULT_STORAGE_SOURCE).resolve(strict=False)


def _system_roots() -> tuple[Path, ...]:
    """Return host paths that are never valid application-data roots."""

    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        roots = [system_root]
        for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            value = os.environ.get(name)
            if value:
                roots.append(Path(value))
        return tuple(root.resolve(strict=False) for root in roots)

    return tuple(
        Path(value).resolve(strict=False)
        for value in (
            "/bin",
            "/boot",
            "/dev",
            "/etc",
            "/lib",
            "/lib64",
            "/opt",
            "/proc",
            "/root",
            "/sbin",
            "/sys",
            "/usr",
            "/var/cache",
            "/var/log",
            "/var/tmp",
            "/var/lib/containers",
            "/var/lib/docker",
            "/var/lib/kubelet",
            "/var/lib/mysql",
            "/var/lib/postgresql",
            "/var/lib/redis",
            "/var/lib/systemd",
        )
    )


def _reject_dotdot_segments(raw_value: str) -> None:
    """Reject parent traversal while allowing the documented ``./data`` form."""

    slash_normalized = raw_value.replace("\\", "/")
    segments = slash_normalized.split("/")
    has_nonleading_dot = any(
        segment == "." and index != 0 for index, segment in enumerate(segments)
    )
    if (
        raw_value.strip() in {".", ".."}
        or any(segment == ".." for segment in segments)
        or has_nonleading_dot
    ):
        raise StoragePathError(
            f"{STORAGE_ENV_VAR} must not be '.'/'..' or contain dot traversal segments: "
            f"{raw_value!r}"
        )


def _is_link(path: Path) -> bool:
    """Return whether a path is a symbolic link or Windows junction."""

    if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except (OSError, TypeError):
        return False
    # FILE_ATTRIBUTE_REPARSE_POINT; this catches junctions on Python versions
    # that predate Path.is_junction().
    return bool(attributes & 0x400)


def _first_symlink(path: Path) -> Path | None:
    """Find a symlink in ``path`` or any existing ancestor without following it."""

    cursor = path
    while True:
        try:
            if _is_link(cursor):
                return cursor
        except OSError as exc:
            raise StoragePathError(f"cannot inspect storage path component {cursor!s}") from exc
        if cursor.parent == cursor:
            return None
        cursor = cursor.parent


def _directory_entries(path: Path) -> tuple[Path, ...]:
    """List a storage root without following child links."""

    try:
        children = tuple(path.iterdir())
    except OSError as exc:
        raise StoragePathError(f"cannot inspect storage directory {path!s}") from exc

    for child in children:
        try:
            if _is_link(child):
                raise StoragePathError(
                    f"storage directory contains symlink or junction {child!s}; "
                    "remove it before Compose starts"
                )
        except OSError as exc:
            raise StoragePathError(f"cannot inspect storage child {child!s}") from exc
    return children


def _reject_unexpected_entries(path: Path) -> tuple[Path, ...]:
    """Require a dedicated root containing only the sentinel and known stores."""

    children = _directory_entries(path)
    allowed = {
        STORAGE_SENTINEL_NAME.casefold(),
        *(name.casefold() for name in STORAGE_CHILDREN),
    }
    unexpected = sorted(child.name for child in children if child.name.casefold() not in allowed)
    if unexpected:
        names = ", ".join(unexpected[:8])
        suffix = " ..." if len(unexpected) > 8 else ""
        raise StoragePathError(
            f"storage source {path!s} is not a dedicated UMS directory; "
            f"unexpected direct entries: {names}{suffix}"
        )

    for child_name in STORAGE_CHILDREN:
        child = path / child_name
        if child.exists() and not child.is_dir():
            raise StoragePathError(f"storage child {child!s} exists but is not a directory")
    return children


def _mountpoints_under(path: Path) -> tuple[Path, ...]:
    """Find mounted filesystems below a storage root without traversing data."""

    if os.name == "nt":
        return ()
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        # The direct checks below still catch the common case on non-Linux
        # hosts; systems without mountinfo cannot claim recursive proof.
        return ()
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise StoragePathError(f"cannot inspect mount points below {path!s}") from exc

    mounts: list[Path] = []
    for line in lines:
        fields = line.split(" - ", 1)[0].split()
        if len(fields) < 5:
            continue
        mount_text = (
            fields[4]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\")
        )
        mount = Path(mount_text)
        if _is_relative_to(mount, path) and mount != path:
            mounts.append(mount)
    return tuple(mounts)


def _reject_storage_submounts(path: Path) -> None:
    """Reject mounts/reparse points below the directories root init may touch."""

    for child_name in STORAGE_CHILDREN:
        child = path / child_name
        if not child.exists():
            continue
        try:
            if os.path.ismount(child) or bool(getattr(child, "is_mount", lambda: False)()):
                raise StoragePathError(f"storage child is a mount point: {child!s}")
        except OSError as exc:
            raise StoragePathError(f"cannot inspect storage mount point {child!s}") from exc
        for mount in _mountpoints_under(child):
            raise StoragePathError(
                f"storage contains a mounted/reparse target below {child!s}: {mount!s}"
            )


def _reject_nested_reparse_points(path: Path) -> None:
    """Walk expected stores and reject every nested mount/link escape."""

    for child_name in STORAGE_CHILDREN:
        child = path / child_name
        if not child.exists():
            continue
        pending = [child]
        while pending:
            current = pending.pop()
            for entry in _directory_entries(current):
                if entry.is_dir():
                    try:
                        if os.path.ismount(entry) or bool(
                            getattr(entry, "is_mount", lambda: False)()
                        ):
                            raise StoragePathError(
                                f"storage contains a nested mount point: {entry!s}"
                            )
                    except OSError as exc:
                        raise StoragePathError(
                            f"cannot inspect nested storage mount {entry!s}"
                        ) from exc
                    pending.append(entry)


# ============================================================================
# Purpose: Capture the exact host identities of the storage root, direct stores,
#   and path-bound sentinel so a launcher can detect replacement during a probe.
# Database/ORM: None.
# Standards: lstat-style reads only; junctions/symlinks and type drift fail
#   closed; large integer identifiers remain Python integers without JSON loss.
# Blast Radius: Durable artifacts/blob path identity.
# Connections:
#   - File: scripts/compose.py -> Compares identities around the Docker probe.
# ============================================================================
def storage_tree_identity(path: Path) -> tuple[tuple[str, int, int], ...]:
    """Return stable ``(name, device, inode)`` identities for bounded roots."""

    entries = (
        (".", path, stat.S_ISDIR),
        *((name, path / name, stat.S_ISDIR) for name in STORAGE_CHILDREN),
        (STORAGE_SENTINEL_NAME, path / STORAGE_SENTINEL_NAME, stat.S_ISREG),
    )
    identities: list[tuple[str, int, int]] = []
    for name, entry, expected_type in entries:
        try:
            if _is_link(entry):
                raise StoragePathError(f"storage identity path is a symlink or junction: {entry!s}")
            metadata = entry.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise StoragePathError(f"storage identity path is missing: {entry!s}") from exc
        except OSError as exc:
            raise StoragePathError(f"cannot inspect storage identity {entry!s}") from exc
        if not expected_type(metadata.st_mode):
            raise StoragePathError(f"storage identity path has the wrong type: {entry!s}")
        identities.append((name, metadata.st_dev, metadata.st_ino))
    return tuple(identities)


@dataclass(frozen=True)
class StorageIdentityGuard:
    """An open identity boundary retained while Docker resolves the bind."""

    canonical_path: Path
    docker_source: Path
    identity: tuple[tuple[str, int, int], ...]
    _descriptors: tuple[int, ...] = ()

    def assert_current(self) -> None:
        """Fail if any guarded root/direct child no longer has its proven identity."""

        if self._descriptors:
            for expected, descriptor in zip(self.identity, self._descriptors, strict=True):
                name, expected_device, expected_inode = expected
                try:
                    metadata = os.fstat(descriptor)
                except OSError as exc:
                    raise StoragePathError(f"cannot recheck open storage identity {name}") from exc
                if (metadata.st_dev, metadata.st_ino) != (
                    expected_device,
                    expected_inode,
                ):
                    raise StoragePathError(f"open storage identity changed unexpectedly: {name}")
            return
        if storage_tree_identity(self.canonical_path) != self.identity:
            raise StoragePathError(
                "storage root or direct child identity changed while Docker was starting"
            )


def _open_windows_identity_handles(path: Path) -> tuple[int, ...]:
    """Open non-delete-sharing handles for every bounded Windows identity."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    # FIX: An attribute-only handle does not block MoveFileEx on current
    # Windows. DELETE access makes the absence of FILE_SHARE_DELETE load-bearing.
    file_read_attributes_and_delete = 0x0080 | 0x00010000
    file_share_read_write = 0x0001 | 0x0002
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    flags = file_flag_backup_semantics | file_flag_open_reparse_point
    invalid_handle = ctypes.c_void_p(-1).value
    paths = (
        path,
        *(path / child for child in STORAGE_CHILDREN),
        path / STORAGE_SENTINEL_NAME,
    )
    handles: list[int] = []
    try:
        for entry in paths:
            handle = create_file(
                str(entry),
                file_read_attributes_and_delete,
                file_share_read_write,
                None,
                open_existing,
                flags,
                None,
            )
            handle_value = int(handle) if handle is not None else invalid_handle
            if handle_value == invalid_handle:
                error_code = ctypes.get_last_error()
                raise StoragePathError(
                    f"cannot lock storage identity against replacement: {entry!s} "
                    f"(Windows error {error_code})"
                )
            handles.append(handle_value)
    except BaseException:
        for handle in reversed(handles):
            close_handle(wintypes.HANDLE(handle))
        raise
    return tuple(handles)


def _close_windows_identity_handles(handles: tuple[int, ...]) -> None:
    """Close handles created only by ``_open_windows_identity_handles``."""

    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    for handle in reversed(handles):
        close_handle(wintypes.HANDLE(handle))


def _open_posix_identity_descriptors(
    path: Path,
    identity: tuple[tuple[str, int, int], ...],
) -> tuple[int, ...]:
    """Open no-follow directory-relative descriptors for the bounded tree."""

    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required) or os.open not in os.supports_dir_fd:
        raise StoragePathError(
            "this POSIX host cannot preserve a no-follow storage identity for Docker"
        )
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        root_descriptor = os.open(path, directory_flags)
        descriptors.append(root_descriptor)
        for name in STORAGE_CHILDREN:
            descriptors.append(os.open(name, directory_flags, dir_fd=root_descriptor))
        descriptors.append(os.open(STORAGE_SENTINEL_NAME, file_flags, dir_fd=root_descriptor))
        for expected, descriptor in zip(identity, descriptors, strict=True):
            name, expected_device, expected_inode = expected
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != (
                expected_device,
                expected_inode,
            ):
                raise StoragePathError(
                    f"storage identity changed while acquiring the guard: {name}"
                )
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    return tuple(descriptors)


# ============================================================================
# Purpose: Retain the proven storage identities until Docker has resolved and
#   created every container that receives the bind source.
# Database/ORM: None.
# Standards: Windows handles deny delete sharing; POSIX uses no-follow open
#   descriptors and exposes the root descriptor through procfs to the daemon.
# Blast Radius: Durable artifacts/blob bind identity during container creation.
# Connections:
#   - File: scripts/compose.py -> Holds this guard through probe and final up.
#   - File: tests/scripts/test_compose_storage_preflight.py -> Real rename proof.
# ============================================================================
@contextlib.contextmanager
def hold_storage_identity(path: Path) -> Iterator[StorageIdentityGuard]:
    """Yield a stable Docker bind source and retain its OS identity boundary."""

    canonical = path.resolve(strict=True)
    identity = storage_tree_identity(canonical)
    if os.name == "nt":
        handles = _open_windows_identity_handles(canonical)
        try:
            if storage_tree_identity(canonical) != identity:
                raise StoragePathError("storage identity changed while acquiring the Windows guard")
            guard = StorageIdentityGuard(canonical, canonical, identity)
            yield guard
            guard.assert_current()
        finally:
            _close_windows_identity_handles(handles)
        return

    descriptors = _open_posix_identity_descriptors(canonical, identity)
    root_descriptor = descriptors[0]
    proc_source = Path(f"/proc/{os.getpid()}/fd/{root_descriptor}")
    try:
        if not proc_source.exists():
            raise StoragePathError("procfs cannot expose the guarded storage descriptor to Docker")
        guard = StorageIdentityGuard(
            canonical,
            proc_source,
            identity,
            descriptors,
        )
        guard.assert_current()
        yield guard
        guard.assert_current()
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _validate_sentinel(path: Path, *, required: bool) -> bool:
    """Check that the marker is a regular file bound to this exact path."""

    sentinel = path / STORAGE_SENTINEL_NAME
    try:
        if _is_link(sentinel):
            raise StoragePathError(f"storage sentinel is a symlink or junction: {sentinel!s}")
        exists = sentinel.exists()
    except OSError as exc:
        raise StoragePathError(f"cannot inspect storage sentinel {sentinel!s}") from exc
    if not exists:
        if required:
            raise StoragePathError(
                f"storage source {path!s} has no {STORAGE_SENTINEL_NAME} attestation; "
                "use the launcher to create it or explicitly adopt a checked directory"
            )
        return False
    if not sentinel.is_file():
        raise StoragePathError(f"storage sentinel is not a regular file: {sentinel!s}")
    try:
        content = sentinel.read_text(encoding="utf-8")
    except OSError as exc:
        raise StoragePathError(f"cannot read storage sentinel {sentinel!s}") from exc
    expected = storage_sentinel_content(path)
    if content != expected:
        raise StoragePathError(f"storage sentinel {sentinel!s} is not bound to this canonical path")
    return True


def _require_operator_group_access(path: Path) -> None:
    """Ensure the host operator can retain access after provisioning."""

    try:
        readable = os.access(path, os.R_OK | os.W_OK | os.X_OK)
    except OSError as exc:
        raise StoragePathError(f"cannot check operator access to {path!s}") from exc
    if not readable:
        raise StoragePathError(
            f"current operator cannot read/write/traverse storage directory {path!s}"
        )

    if os.name == "nt" or not hasattr(os, "getgroups"):
        return
    try:
        operator_groups = {os.getgid(), *os.getgroups()}
        source_gid = path.stat().st_gid
    except OSError as exc:
        raise StoragePathError(f"cannot inspect storage ownership for {path!s}") from exc
    if getattr(os, "geteuid", lambda: 1)() != 0 and source_gid not in operator_groups:
        raise StoragePathError(
            f"storage directory group {source_gid} is not an operator group; "
            "set the directory group before starting Compose"
        )


_WINDOWS_ACL_QUERY = r"""
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Security -ErrorAction Stop
$root = $env:UMS_STORAGE_ACL_PATH
$queue = [System.Collections.Generic.Queue[string]]::new()
$paths = [System.Collections.Generic.List[string]]::new()
$queue.Enqueue($root)
while ($queue.Count -gt 0) {
  $current = $queue.Dequeue()
  $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
  if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Refusing reparse point: $($item.FullName)"
  }
  [void]$paths.Add($item.FullName)
  if ($item.PSIsContainer) {
    foreach ($child in @(Get-ChildItem -LiteralPath $item.FullName -Force -ErrorAction Stop)) {
      if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing nested reparse point: $($child.FullName)"
      }
      if ($child.PSIsContainer) {
        $queue.Enqueue($child.FullName)
      } else {
        [void]$paths.Add($child.FullName)
      }
    }
  }
}
$owners = foreach ($path in $paths) {
  $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
  [pscustomobject]@{
    Path = $path
    OwnerSid = ([System.Security.Principal.NTAccount]$acl.Owner).Translate(
      [System.Security.Principal.SecurityIdentifier]).Value
    AreAccessRulesProtected = [bool]$acl.AreAccessRulesProtected
  }
}
$rows = foreach ($path in $paths) {
  $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
  foreach ($entry in $acl.Access) {
    $sid = $entry.IdentityReference.Translate(
      [System.Security.Principal.SecurityIdentifier]).Value
    [pscustomobject]@{
      Path = $path
      Sid = $sid
      Type = $entry.AccessControlType.ToString()
      Rights = $entry.FileSystemRights.ToString()
      RightsMask = [int64]$entry.FileSystemRights
      InheritanceFlags = $entry.InheritanceFlags.ToString()
      PropagationFlags = $entry.PropagationFlags.ToString()
      IsInherited = [bool]$entry.IsInherited
    }
  }
}
[pscustomobject]@{
  CurrentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  Paths = @($paths)
  Owners = @($owners)
  Access = @($rows)
} | ConvertTo-Json -Compress -Depth 5
"""

# .NET's FileSystemRights enum uses these stable masks.  Checking the numeric
# mask prevents names such as WriteAttributes from being mistaken for the
# create/write/delete/traverse rights required by the launcher.
_WINDOWS_MODIFY_RIGHTS = 197055
_WINDOWS_FULL_CONTROL_RIGHTS = 2032127
_WINDOWS_INHERITANCE_FLAGS = {"containerinherit", "objectinherit"}


def _windows_acl_process_spec() -> tuple[list[str], dict[str, str]]:
    """Return a PowerShell command pinned to its own trusted module root."""

    environment = os.environ.copy()
    executable = (
        shutil.which("pwsh.exe")
        or shutil.which("pwsh")
        or shutil.which("powershell.exe")
        or shutil.which("powershell")
    )
    if executable:
        # Do not inherit a pwsh/Windows-PowerShell cross-version PSModulePath.
        # Microsoft.PowerShell.Security (Get-Acl) ships below the executable's
        # own PSHOME, so a single trusted root is sufficient for either host.
        module_root = Path(executable).resolve(strict=True).parent / "Modules"
        environment["PSModulePath"] = str(module_root)
        return [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
        ], environment

    raise StoragePathError(
        "cannot inspect Windows host ACLs; install PowerShell 7 (pwsh) or "
        "Windows PowerShell and retry"
    )


def _windows_acl_remediation(path: Path) -> str:
    """Return the operator command that provisions a restricted host DACL."""

    return (
        f'icacls "{path}" /inheritance:r /grant:r '
        '"<YOUR-WINDOWS-ACCOUNT>:(OI)(CI)M" '
        '"SYSTEM:(OI)(CI)F" "BUILTIN\\Administrators:(OI)(CI)F" /T'
    )


def _windows_rights_satisfy_modify(row: dict[str, object]) -> bool:
    """Return whether numeric FileSystemRights prove Modify or FullControl."""

    try:
        mask = int(row.get("RightsMask", ""))
    except (TypeError, ValueError):
        return False
    return (
        mask & _WINDOWS_MODIFY_RIGHTS == _WINDOWS_MODIFY_RIGHTS
        or mask & _WINDOWS_FULL_CONTROL_RIGHTS == _WINDOWS_FULL_CONTROL_RIGHTS
    )


def _windows_row_is_inheritable(row: dict[str, object]) -> bool:
    """Return whether a root ACE propagates to both child directories and files."""

    flags = {
        item.strip().casefold()
        for item in str(row.get("InheritanceFlags", "")).split(",")
        if item.strip()
    }
    propagation = {
        item.strip().casefold()
        for item in str(row.get("PropagationFlags", "")).split(",")
        if item.strip()
    }
    # FIX: CI/OI plus NoPropagateInherit reaches only one generation, so it
    # cannot prove operator recovery access throughout the bounded tree.
    disallowed_propagation = {"inheritonly", "nopropagateinherit"}
    return _WINDOWS_INHERITANCE_FLAGS <= flags and not (disallowed_propagation & propagation)


def _windows_path_key(value: object) -> str:
    """Return a case-insensitive normalized key for one reported Windows path."""

    if not isinstance(value, str) or not value:
        raise ValueError("ACL path must be a non-empty string")
    return os.path.normcase(os.path.abspath(value))


# ============================================================================
# Purpose: Query the Windows DACL for every non-reparse storage descendant and
#   prove exact owner, deny, allow-rights, root-protection, and inheritance rules.
# Database/ORM: None.
# Standards: Trusted PowerShell module root; numeric rights masks; all query
#   rows are reconciled to the enumerated path set; malformed evidence fails closed.
# Blast Radius: Host confidentiality and operator recoverability of finance data.
# Connections:
#   - File: docker-compose.yml -> Windows host-ACL operating contract.
#   - File: tests/scripts/test_compose_storage_preflight.py -> ACL counterexamples.
# ============================================================================
def _require_windows_host_acl(path: Path) -> None:
    """Fail closed unless Windows host ACLs contain only approved principals.

    Docker Desktop's Linux VM ``stat`` output is intentionally not used as a
    confidentiality proof. The NTFS DACL is the source of truth on Windows;
    this query checks every non-reparse descendant and rejects any broad allow
    ACE. The error includes the explicit operator-run ``icacls`` action.
    """

    if os.name != "nt":
        return
    try:
        shell_command, shell_environment = _windows_acl_process_spec()
        shell_environment["UMS_STORAGE_ACL_PATH"] = str(path)
        result = subprocess.run(
            [*shell_command, "-Command", _WINDOWS_ACL_QUERY],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=shell_environment,
            check=False,
        )
    except (OSError, StoragePathError) as exc:
        raise StoragePathError(
            "cannot inspect Windows host ACLs; install/enable PowerShell and run "
            f"{_windows_acl_remediation(path)}"
        ) from exc
    if result.returncode != 0:
        raise StoragePathError(
            "Windows host ACL inspection failed; run the explicit remediation command "
            f"and retry: {_windows_acl_remediation(path)}"
        )
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("ACL payload must be an object")
        current_sid = payload["CurrentSid"]
        paths = payload.get("Paths", [])
        rows = payload.get("Access", [])
        owners = payload.get("Owners", [])
        if not isinstance(current_sid, str) or not current_sid.startswith("S-"):
            raise ValueError("current SID is invalid")
        if isinstance(rows, dict):
            rows = [rows]
        if isinstance(paths, str):
            paths = [paths]
        if isinstance(owners, dict):
            owners = [owners]
        if (
            not isinstance(rows, list)
            or not isinstance(paths, list)
            or not isinstance(owners, list)
        ):
            raise ValueError("ACL paths/access/owners must be arrays")
        path_keys = [_windows_path_key(item) for item in paths]
        if not path_keys or len(set(path_keys)) != len(path_keys):
            raise ValueError("ACL path inventory is empty or duplicated")
        root_key = _windows_path_key(str(path))
        if root_key not in path_keys:
            raise ValueError("ACL path inventory omits the storage root")
    except (TypeError, ValueError, KeyError) as exc:
        raise StoragePathError(
            "Windows host ACL inspection returned invalid data; run the explicit "
            f"remediation command and retry: {_windows_acl_remediation(path)}"
        ) from exc

    allowed_sids = {
        current_sid.casefold(),
        "S-1-5-18".casefold(),
        "S-1-5-32-544".casefold(),
    }
    normalized_rows: list[tuple[str, dict[str, object]]] = []
    try:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("ACL row must be an object")
            row_key = _windows_path_key(row.get("Path"))
            if row_key not in path_keys:
                raise ValueError("ACL row names a path outside the inventory")
            normalized_rows.append((row_key, row))
    except ValueError as exc:
        raise StoragePathError(
            "Windows host ACL inspection returned invalid data; run the explicit "
            f"remediation command and retry: {_windows_acl_remediation(path)}"
        ) from exc

    broad_allow = [
        row
        for _, row in normalized_rows
        if str(row.get("Type", "")).casefold() == "allow"
        and str(row.get("Sid", "")).casefold() not in allowed_sids
    ]
    if broad_allow:
        sample = ", ".join(
            f"{row.get('Sid', '?')} on {row.get('Path', path)}" for row in broad_allow[:4]
        )
        raise StoragePathError(
            "Windows host ACL allows unapproved principals (container chmod/stat is "
            f"not proof of NTFS confidentiality: {sample}); run {_windows_acl_remediation(path)}"
        )
    rows_by_path: dict[str, list[dict[str, object]]] = {path_key: [] for path_key in path_keys}
    for row_key, row in normalized_rows:
        rows_by_path[row_key].append(row)

    owners_by_path: dict[str, dict[str, object]] = {}
    try:
        for owner in owners:
            if not isinstance(owner, dict):
                raise ValueError("ACL owner row must be an object")
            owner_key = _windows_path_key(owner.get("Path"))
            if owner_key not in rows_by_path or owner_key in owners_by_path:
                raise ValueError("ACL owner inventory is outside or duplicates the path set")
            if not isinstance(owner.get("OwnerSid"), str):
                raise ValueError("ACL owner SID is invalid")
            if not isinstance(owner.get("AreAccessRulesProtected"), bool):
                raise ValueError("ACL protection evidence is missing")
            owners_by_path[owner_key] = owner
        if set(owners_by_path) != set(rows_by_path):
            raise ValueError("ACL owner inventory does not match the path set")
    except ValueError as exc:
        raise StoragePathError(
            "Windows host ACL inspection returned invalid data; run the explicit "
            f"remediation command and retry: {_windows_acl_remediation(path)}"
        ) from exc

    display_by_key = dict(zip(path_keys, (str(item) for item in paths), strict=True))
    for checked_key, path_rows in rows_by_path.items():
        checked_path = display_by_key[checked_key]
        owner = owners_by_path[checked_key]
        owner_sid = str(owner["OwnerSid"]).casefold()
        if owner_sid not in allowed_sids:
            raise StoragePathError(
                f"Windows host ACL owner is not an approved principal on {checked_path}; "
                f"run {_windows_acl_remediation(path)}"
            )
        if not path_rows:
            raise StoragePathError(
                f"Windows host ACL has no readable entries for {checked_path}; "
                f"run {_windows_acl_remediation(path)}"
            )
        if any(str(row.get("Type", "")).casefold() == "deny" for row in path_rows):
            raise StoragePathError(
                f"Windows host ACL contains a deny entry on {checked_path}; "
                f"run {_windows_acl_remediation(path)}"
            )
        operator_allow = [
            row
            for row in path_rows
            if str(row.get("Sid", "")).casefold() == current_sid.casefold()
            and str(row.get("Type", "")).casefold() == "allow"
        ]
        if not operator_allow or not any(
            _windows_rights_satisfy_modify(row) for row in operator_allow
        ):
            raise StoragePathError(
                f"Windows host ACL does not prove operator read/write access on {checked_path}; "
                f"run {_windows_acl_remediation(path)}"
            )
        if checked_key == root_key:
            if owner["AreAccessRulesProtected"] is not True:
                raise StoragePathError(
                    "Windows storage root still inherits parent ACL changes; run "
                    f"{_windows_acl_remediation(path)}"
                )
            if not any(
                _windows_rights_satisfy_modify(row)
                and _windows_row_is_inheritable(row)
                and row.get("IsInherited") is False
                for row in operator_allow
            ):
                raise StoragePathError(
                    "Windows host ACL does not prove inheritable operator access on the "
                    f"fresh storage root {checked_path}; run {_windows_acl_remediation(path)}"
                )


# ============================================================================
# Purpose: Canonicalize and validate the host directory before Docker mounts it.
# Database/ORM: None.
# Standards: Fail closed on traversal, roots/system paths, checkout paths,
#   symlinks, broad directories, missing attestation, inaccessible sources, and
#   unverified Windows host ACLs; never mutate an unsafe target.
# Blast Radius: Host filesystem path selection and durable artifact/blob data.
# Connections:
#   - File: docker-compose.yml -> Consumes the canonical path for bind mounts.
#   - File: scripts/compose.py -> Runs this preflight before Docker Compose.
# ============================================================================
def validate_storage_path(
    raw_value: str | os.PathLike[str] | None = None,
    *,
    project_root: Path | None = None,
    require_exists: bool = True,
    require_attestation: bool = True,
) -> Path:
    """Return a safe canonical storage path, optionally before attestation."""

    if raw_value is None:
        raw_text = os.environ.get(STORAGE_ENV_VAR, "") or DEFAULT_STORAGE_SOURCE
    else:
        raw_text = os.fspath(raw_value)
    raw_text = raw_text.strip()
    if not raw_text:
        raw_text = DEFAULT_STORAGE_SOURCE
    if "\x00" in raw_text or "\n" in raw_text or "\r" in raw_text:
        raise StoragePathError(f"{STORAGE_ENV_VAR} contains a forbidden control character")
    _reject_dotdot_segments(raw_text)

    root = (project_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    candidate = Path(raw_text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate

    symlink = _first_symlink(candidate)
    if symlink is not None:
        raise StoragePathError(
            f"storage source or parent is a symlink or junction ({symlink!s}); "
            "use a real dedicated directory"
        )
    try:
        canonical = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise StoragePathError(f"cannot canonicalize storage path {candidate!s}") from exc

    if canonical.parent == canonical:
        raise StoragePathError(f"storage source must not be a filesystem root: {canonical!s}")
    default_path = _default_storage_path(root)
    if _is_relative_to(canonical, root) and canonical != default_path:
        raise StoragePathError(
            "storage source under the checkout is reserved; only the dedicated "
            f"default {default_path!s} is allowed, not {canonical!s}"
        )
    for system_root in _system_roots():
        if canonical == system_root or _is_relative_to(canonical, system_root):
            raise StoragePathError(
                f"storage source is under a protected system path: {canonical!s}"
            )
    if os.name != "nt" and canonical in {
        Path("/home").resolve(strict=False),
        Path("/media").resolve(strict=False),
        Path("/mnt").resolve(strict=False),
        Path("/run").resolve(strict=False),
        Path("/srv").resolve(strict=False),
        Path("/tmp").resolve(strict=False),
        Path("/var").resolve(strict=False),
        Path("/var/lib").resolve(strict=False),
    }:
        raise StoragePathError(f"storage source must not be a transient system root: {canonical!s}")

    if not canonical.exists():
        if require_exists:
            raise StoragePathError(
                f"storage source does not exist: {canonical!s}; run the Compose launcher "
                "or the validator with --create"
            )
        return canonical
    if not canonical.is_dir():
        raise StoragePathError(f"storage source is not a directory: {canonical!s}")
    _reject_unexpected_entries(canonical)
    _reject_storage_submounts(canonical)
    _reject_nested_reparse_points(canonical)
    _validate_sentinel(canonical, required=require_attestation)
    _require_operator_group_access(canonical)
    _require_windows_host_acl(canonical)
    return canonical


def _create_sentinel(path: Path) -> None:
    """Create the path-bound marker without replacing an existing marker."""

    sentinel = path / STORAGE_SENTINEL_NAME
    try:
        with sentinel.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(storage_sentinel_content(path))
        if os.name != "nt":
            os.chmod(sentinel, 0o600)
    except FileExistsError:
        _validate_sentinel(path, required=True)
    except OSError as exc:
        raise StoragePathError(f"cannot create storage sentinel {sentinel!s}") from exc


def _real_directory_identity(path: Path) -> tuple[int, int]:
    """Return a non-link directory identity used around bounded host writes."""

    try:
        if _is_link(path):
            raise StoragePathError(f"storage directory became a link: {path!s}")
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise StoragePathError(f"cannot inspect storage directory {path!s}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise StoragePathError(f"storage path is not a real directory: {path!s}")
    return metadata.st_dev, metadata.st_ino


# ============================================================================
# Purpose: Create only the reserved default root or explicitly adopt an existing
#   custom root, with identity checks around every bounded host write.
# Database/ORM: None.
# Standards: Missing custom paths are never created; existing custom paths are
#   not mutated before --adopt-existing; Windows ACL and path identity checks
#   bracket child/sentinel creation; no container receives root host access.
# Blast Radius: Host filesystem path creation and Compose data durability.
# Connections:
#   - File: scripts/compose.py -> Uses this before startup.
#   - File: docker-compose.yml -> Mounts only this canonical source.
# ============================================================================
def prepare_storage_path(
    raw_value: str | os.PathLike[str] | None = None,
    *,
    project_root: Path | None = None,
    adopt_existing: bool = False,
) -> Path:
    """Validate an existing source or create/adopt a dedicated source safely."""

    root = (project_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    canonical = validate_storage_path(
        raw_value,
        project_root=root,
        require_exists=False,
        require_attestation=False,
    )
    default_path = _default_storage_path(root)
    if not canonical.exists():
        if canonical != default_path:
            raise StoragePathError(
                f"custom storage path does not exist: {canonical!s}; create and secure "
                "it explicitly before --adopt-existing"
            )
        try:
            canonical.parent.mkdir(mode=0o770, exist_ok=True)
            if _first_symlink(canonical) is not None:
                raise StoragePathError(
                    "reserved storage parent became a symlink or junction during creation"
                )
            canonical.mkdir(mode=0o770, exist_ok=False)
        except OSError as exc:
            raise StoragePathError(f"cannot create storage source {canonical!s}") from exc
        if canonical.resolve(strict=True) != default_path:
            raise StoragePathError("reserved storage identity changed during creation")

    # Reject a custom un-attested directory before creating children or a
    # sentinel. Adoption is a separate explicit operator action.
    _reject_unexpected_entries(canonical)
    _reject_storage_submounts(canonical)
    _reject_nested_reparse_points(canonical)
    sentinel_exists = _validate_sentinel(canonical, required=False)
    if not sentinel_exists and canonical != default_path and not adopt_existing:
        raise StoragePathError(
            f"custom existing storage {canonical!s} has no {STORAGE_SENTINEL_NAME}; "
            "run the validator once with --create --adopt-existing after reviewing it"
        )
    if canonical != default_path and not adopt_existing:
        missing_children = [name for name in STORAGE_CHILDREN if not (canonical / name).exists()]
        if missing_children:
            raise StoragePathError(
                f"custom storage {canonical!s} is incomplete ({', '.join(missing_children)}); "
                "the launcher will not mutate it implicitly, so prepare it explicitly "
                "with --create --adopt-existing"
            )

    _require_operator_group_access(canonical)
    _require_windows_host_acl(canonical)
    root_identity = _real_directory_identity(canonical)

    # Create only the two reviewed direct children, as the current host
    # operator. Recheck the root identity immediately before and after each
    # write so a junction/root replacement cannot redirect later writes.
    for child_name in STORAGE_CHILDREN:
        child = canonical / child_name
        if not child.exists():
            if _real_directory_identity(canonical) != root_identity:
                raise StoragePathError("storage root identity changed before child creation")
            try:
                child.mkdir(mode=0o770, exist_ok=False)
            except OSError as exc:
                raise StoragePathError(f"cannot create storage child {child!s}") from exc
        _real_directory_identity(child)
        if _real_directory_identity(canonical) != root_identity:
            raise StoragePathError("storage root identity changed during child creation")

    _reject_storage_submounts(canonical)
    _reject_nested_reparse_points(canonical)
    _require_windows_host_acl(canonical)
    if not sentinel_exists:
        if _real_directory_identity(canonical) != root_identity:
            raise StoragePathError("storage root identity changed before attestation")
        _create_sentinel(canonical)

    if _real_directory_identity(canonical) != root_identity:
        raise StoragePathError("storage root identity changed during preparation")
    return validate_storage_path(canonical, project_root=root, require_exists=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        help=f"storage source; defaults to ${STORAGE_ENV_VAR} or {DEFAULT_STORAGE_SOURCE}",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="checkout root used to resolve relative paths",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="create a missing, otherwise safe source directory",
    )
    parser.add_argument(
        "--adopt-existing",
        action="store_true",
        help="explicitly attest a custom existing directory after its direct layout is reviewed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the host preflight CLI and return a process exit status."""

    args = _build_parser().parse_args(argv)
    try:
        path = (
            prepare_storage_path(
                args.path,
                project_root=args.project_root,
                adopt_existing=args.adopt_existing,
            )
            if args.create
            else validate_storage_path(args.path, project_root=args.project_root)
        )
    except StoragePathError as exc:
        print(f"storage preflight failed: {exc}", file=sys.stderr)
        return 2
    print(f"validated {STORAGE_ENV_VAR}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
