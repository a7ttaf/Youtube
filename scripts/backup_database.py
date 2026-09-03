#!/usr/bin/env python
"""Create one snapshot-consistent, database-only PostgreSQL backup.

This entrypoint deliberately does not archive export artifacts or connector
blobs. ``scripts/compose_storage.py`` owns the coordinated outer bundle.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = str(_REPOSITORY_ROOT / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from ums_smart_revenue.ops.database_backup.backup import run_backup  # noqa: E402
from ums_smart_revenue.ops.database_backup.contracts import (  # noqa: E402
    SEED_TABLES as _SEED_TABLES,
)
from ums_smart_revenue.ops.database_backup.contracts import (  # noqa: E402
    BackupToolError,
)
from ums_smart_revenue.ops.database_backup.filesystem import (  # noqa: E402
    resolve_output_directory,
)

SEED_TABLES = _SEED_TABLES


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse backup CLI arguments without retention or prune options."""
    parser = argparse.ArgumentParser(
        description=(
            "Create a custom-format PostgreSQL dump, canonical role SQL copy, and strict "
            "semantic manifest in an atomic host-directory run."
        )
    )
    parser.add_argument(
        "--container",
        required=True,
        help="exact running PostgreSQL container name or id",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="dedicated host directory outside the repository",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="per native command timeout (default: 900)",
    )
    parser.add_argument(
        "--coordinated-bundle",
        action="store_true",
        help="write one direct child run into a pre-created owner-only P0-a bundle",
    )
    parser.add_argument(
        "--confirm-writers-quiesced",
        action="store_true",
        help=(
            "confirm all application/scheduled writers are stopped and will remain stopped "
            "until this command finishes"
        ),
    )
    args = parser.parse_args(argv)
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")
    return args


# ============================================================================
# Purpose: Translate the database backup domain contract into a stable CLI.
# Database/ORM: Read-only access is owned by ops/database_backup/backup.py.
# Standards: Thin argparse boundary, typed errors, no credential output.
# Blast Radius: Creates one host backup directory; never prunes old backups.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/backup.py -> service.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> operator commands.
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Run the backup command and return its documented process code.

    Args:
        argv: CLI argument vector; ``None`` reads ``sys.argv``.

    Returns:
        The process exit status: ``0`` on success, or the failing gate's
        documented code (``BackupToolError`` detail is printed, never
        tracebacked).
    """
    try:
        args = _parse_args(argv)
        if not args.confirm_writers_quiesced:
            raise BackupToolError(
                "--confirm-writers-quiesced is required after stopping all source writers",
                exit_code=2,
            )
        output = resolve_output_directory(
            args.out_dir,
            repository_root=_REPOSITORY_ROOT,
            coordinated_bundle=args.coordinated_bundle,
        )
        result = run_backup(
            repository_root=_REPOSITORY_ROOT,
            output_directory=output,
            container=args.container,
            timeout_seconds=args.timeout_seconds,
            writers_quiesced=args.confirm_writers_quiesced,
        )
    except BackupToolError as exc:
        print(f"DATABASE BACKUP REFUSED: {exc}", file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(
            f"DATABASE BACKUP REFUSED: local filesystem operation failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 7
    print(f"DATABASE BACKUP VERIFIED: {result.path}")
    print(
        f"tables={len(result.manifest.tables)} "
        f"rows={sum(record.rows for record in result.manifest.tables)} "
        f"alembic_head={result.manifest.source.migration_heads[0]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
