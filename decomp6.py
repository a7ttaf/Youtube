"""Decompose main (complexity 16): dispatch table instead of elif chain."""
import ast
import re
from pathlib import Path

p = Path("scripts/compose_storage.py")
s = p.read_text(encoding="utf-8")

fn_start = s.index("def main(argv: list[str] | None = None) -> int:")

old_tail = '''        else:  # pragma: no cover - argparse enforces the command choices.
            raise StorageContractError(f"unknown storage command: {args.command}")'''
new_tail = '''        else:  # pragma: no cover - argparse enforces the command choices.
            raise StorageContractError(f"unknown storage command: {args.command}")'''

# Replace the long elif chain with per-command handlers.
old_chain_start = s.index('        if args.command == "prepare":', fn_start)
old_chain_end = s.index(old_tail, fn_start)

new_chain = '''        handler = _COMMAND_HANDLERS.get(args.command)
        if handler is None:  # pragma: no cover - argparse enforces choices.
            raise StorageContractError(f"unknown storage command: {args.command}")
        return handler(args)
'''
s = s[:old_chain_start] + new_chain + s[old_chain_end + len(old_tail):]

helpers = '''

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
    """Validate storage readiness, then replace this process with the command."""
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

'''
anchor = "\ndef main(argv: list[str] | None = None) -> int:"
assert s.count(anchor) == 1
s = s.replace(anchor, helpers + anchor, 1)
if "from collections.abc import Callable" not in s:
    s = s.replace("import argparse\n", "import argparse\nfrom collections.abc import Callable\n", 1)
s = re.sub(r"\n{4,}(def )", r"\n\n\n\1", s)
ast.parse(s)
p.write_bytes(s.encode("utf-8"))
print("main decomposed")
