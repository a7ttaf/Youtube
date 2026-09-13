#!/usr/bin/env python
"""Restore or rehearse one verified database backup into a clean target."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = str(_REPOSITORY_ROOT / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from ums_smart_revenue.ops.database_backup.contracts import BackupToolError  # noqa: E402
from ums_smart_revenue.ops.database_backup.restore import (  # noqa: E402
    rehearse_restore,
    restore_clean_target,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse restore CLI arguments without non-empty-target overrides."""
    parser = argparse.ArgumentParser(
        description=(
            "Verify and restore one database backup. Non-empty targets are always refused."
        )
    )
    parser.add_argument("--backup-dir", type=Path, required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument(
        "--rehearse",
        action="store_true",
        help="create, verify, and remove a new throwaway PostgreSQL container",
    )
    destination.add_argument(
        "--target-container",
        help="explicit already-provisioned clean PostgreSQL container",
    )
    parser.add_argument(
        "--rehearse-image",
        help="operator-selected local image; its immutable id must match the manifest",
    )
    parser.add_argument(
        "--confirm-clean-target",
        action="store_true",
        help="required acknowledgement for a direct clean-target restore",
    )
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--wait-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    if args.timeout_seconds < 1 or args.wait_seconds < 1:
        parser.error("timeouts must be at least 1 second")
    if args.rehearse and not args.rehearse_image:
        parser.error("--rehearse requires --rehearse-image")
    if not args.rehearse and args.rehearse_image:
        parser.error("--rehearse-image is valid only with --rehearse")
    if args.rehearse and args.confirm_clean_target:
        parser.error("--confirm-clean-target is only for --target-container")
    return args


# ============================================================================
# Purpose: Translate clean restore/rehearsal results into stable process codes.
# Database/ORM: Full restore writes are owned by ops/database_backup/restore.py.
# Standards: Thin argparse boundary, typed safe errors, objective count output.
# Blast Radius: Explicit clean target or uniquely named throwaway container only.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/restore.py -> service.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> operator procedure.
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Run one clean restore or throwaway rehearsal.

    Args:
        argv: CLI argument vector; ``None`` reads ``sys.argv``.

    Returns:
        The process exit status: ``0`` on success, or the failing gate's
        documented code (``BackupToolError`` detail is printed, never
        tracebacked).
    """
    try:
        args = _parse_args(argv)
        if args.rehearse:
            result = rehearse_restore(
                repository_root=_REPOSITORY_ROOT,
                backup_directory=args.backup_dir,
                rehearsal_image=args.rehearse_image,
                timeout_seconds=args.timeout_seconds,
                wait_seconds=args.wait_seconds,
            )
        else:
            result = restore_clean_target(
                repository_root=_REPOSITORY_ROOT,
                backup_directory=args.backup_dir,
                target_container=args.target_container,
                timeout_seconds=args.timeout_seconds,
                wait_seconds=args.wait_seconds,
                clean_target_confirmed=args.confirm_clean_target,
            )
    except BackupToolError as exc:
        print(f"DATABASE RESTORE REFUSED: {exc}", file=sys.stderr)
        return exc.exit_code
    except FileNotFoundError:
        print("DATABASE RESTORE REFUSED: selected backup path does not exist", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"DATABASE RESTORE REFUSED: local filesystem operation failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 7
    print(
        f"DATABASE RESTORE VERIFIED: container={result.container} "
        f"tables={len(result.tables)} rows={sum(record.rows for record in result.tables)}"
    )
    if result.kept:
        print("target retained; operator owns its lifecycle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
