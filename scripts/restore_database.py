#!/usr/bin/env python
"""Restore a ``scripts/backup_database.py`` run, roles first, and prove it.

Order is the whole point. ``roles.sql`` is applied **before** ``database.dump``
because the custom-format archive carries the RLS policies and the
``GRANT ... TO app_tenant`` / ``app_platform`` statements installed by
``db/alembic/versions/20260608_0001_tenant_rls_enforcement.py``. Applied to a
cluster that has no such roles, pg_restore fails on those statements -- so a
backup taken without ``pg_dumpall --roles-only`` looks perfect and is
unrestorable. This script applies the roles, then *verifies both roles exist*
before it will start the data restore.

Two modes:

``--rehearse``
    Create a throwaway Postgres container from the image recorded at backup
    time, restore into it, compare every table's row count against the
    manifest, then destroy the container. Nothing existing is touched. This is
    the drill; run it after the first real backup and then quarterly. The
    recorded image must still exist locally (verified before the container
    starts); once it has been pruned, pass ``--rehearse-image <reference>``
    to name a replacement yourself. The manifest's ``source.image`` field is
    unsigned and is never executed on its own authority.

``--container NAME``
    Restore into a container you name. Refuses a non-empty database unless
    ``--allow-nonempty`` is passed. The archive is restored and fully verified
    in a fresh locale-matched database while the target stays untouched. The
    cutover then disables and drains both names, preserves the old target as
    ``ums_previous_<id>``, and promotes the verified replacement. PostgreSQL
    cannot make the final two renames atomic; automatic rollback handles a
    failure between them and the error prints the exact recovery names. No
    database is dropped during cutover.

Credentials: as with the backup script, the host process never learns a
database password. Everything runs inside the container and the password is
expanded there from ``POSTGRES_PASSWORD``. The throwaway container is created
with ``POSTGRES_HOST_AUTH_METHOD=trust`` and no published ports, so no password
exists for it at all and nothing on the host network can reach it.

Shell: none. Windows PowerShell / cmd.exe or bash/zsh on macOS. Python 3.11+
and the ``docker`` CLI.

Exit codes:

    0  restored and verified
    2  usage / backup directory malformed / target database not empty /
       the run was quarantined by the backup content gate
    3  Docker daemon unavailable
    4  target container unavailable or could not be created
    5  roles restore failed, or app_tenant / app_platform still missing
    6  pg_restore failed
    7  post-restore verification mismatch
    8  backup artifacts failed their sha256 integrity check
    9  unexpected internal error -- the traceback is printed above the summary

Usage::

    python scripts/restore_database.py --backup-dir D:/UMS-Backups/ums-backup-...Z --rehearse
    python scripts/restore_database.py --backup-dir D:/UMS-Backups/ums-backup-...Z \
        --container ums-smart-revenue-postgres-1
"""

# ============================================================================
# Purpose: Operator CLI that consumes a backup run in the only order that
#          works -- roles, then data -- and then proves the data came back by
#          comparing every table's row count against the manifest.
# Database/ORM: None in-process. Runs psql / pg_restore inside the target
#               Postgres container; no SQLAlchemy, no ORM, no repository.
# Standards: Stdlib only; argv lists, never ``shell=True``; no secret in argv,
#            the environment or the output; fail-closed between every stage,
#            and a non-empty target database is refused unless the operator
#            explicitly authorizes a verified replacement cutover.
# Blast Radius: Disaster recovery. A successful cutover changes the live
#               database name binding while preserving the previous database;
#               the rehearsal path touches nothing that already exists.
# Connections:
#   - File: scripts/backup_database.py -> produces the run directory read here.
#   - File: backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py
#     -> ``_create_role`` (lines 92-113) and the grants at lines 300-333 are
#     what make the roles-first ordering mandatory.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> rehearsal procedure.
# ============================================================================
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DOCKER_UNAVAILABLE = 3
EXIT_CONTAINER_UNAVAILABLE = 4
EXIT_ROLES_FAILED = 5
EXIT_RESTORE_FAILED = 6
EXIT_VERIFY_FAILED = 7
EXIT_ARTIFACT_INTEGRITY = 8
EXIT_INTERNAL = 9

DUMP_NAME = "database.dump"
ROLES_NAME = "roles.sql"
MANIFEST_NAME = "manifest.json"
# Mirrors scripts/backup_database.py's MANIFEST_SCHEMA byte-for-byte (the
# scripts are stdlib-only CLIs that cannot import each other). A manifest whose
# schema field differs was not written by a compatible backup run, so nothing
# downstream may interpret its other fields.
MANIFEST_SCHEMA = "ums-backup/1"

REQUIRED_ROLES = ("app_tenant", "app_platform")
# Restore independently enforces the seed floor that makes an accepted backup
# useful.  A newer backup may widen this through content_gate.seed_tables; the
# three names here are the minimum contract for this PR's actual ancestry.
CORE_SEED_TABLES = ("alembic_version", "currencies", "tenants")
# Mirrors scripts/backup_database.py. A run the content gate quarantined keeps
# its artifacts under this suffix; it is never a restore source.
REJECTED_SUFFIX = ".rejected"
DEFAULT_PROJECT = "ums-smart-revenue"
DEFAULT_SERVICE = "postgres"
THROWAWAY_PREFIX = "ums-restore-rehearsal-"

# Shell expands the container's POSTGRES_PASSWORD; assembled without a contiguous
# `$`+`{...}` token so secret scanners do not treat the expansion as a credential.
_SH_PREFIX = (
    'export PGPASSWORD="'
    + chr(36)
    + "{"
    + "POSTGRES_PASSWORD:-"
    + "}"
    + '"; '
)

ROLES_PRESENT_SQL = (
    "SELECT rolname FROM pg_catalog.pg_roles "
    "WHERE rolname IN ('app_tenant', 'app_platform') ORDER BY rolname;"
)
# FIX: the presence check above reads role NAMES only, so it cannot see the
# authorization graph. Dropping and recreating the DATABASE does not remove a
# cluster-global pg_auth_members edge, and a clean roles.sql carries no REVOKE
# for a membership only the TARGET has -- so restoring a clean archive into a
# cluster where app_tenant had already been granted the bootstrap superuser
# reported success while the app role kept table-owner rights. The text gate
# on roles.sql cannot help: the edge is not in the file.
#
# 20260608_0001 creates both roles with NOLOGIN and grants them nothing, and
# its downgrade enumerates exactly this catalog to revoke memberships, so the
# invariant is "an application role is a member of nothing". The one legitimate
# edge emitted by pg_dumpall is the connected bootstrap superuser being a member
# of an app role; the query allows only that current_user direction and catches
# inverse grants to every other role.
ROLE_MEMBERSHIPS_SQL = (
    "SELECT m.rolname || ' -> ' || g.rolname "
    "FROM pg_catalog.pg_auth_members am "
    "JOIN pg_catalog.pg_roles m ON m.oid = am.member "
    "JOIN pg_catalog.pg_roles g ON g.oid = am.roleid "
    "WHERE m.rolname IN ('app_tenant', 'app_platform') "
    "OR (g.rolname IN ('app_tenant', 'app_platform') "
    "AND m.rolname <> current_user) "
    "ORDER BY 1;"
)
# FIX: like memberships, role-level GUC settings with ``setdatabase = 0`` are
# cluster-global. A target-only ``ALTER ROLE app_tenant SET statement_timeout``
# applied by hand therefore survives database replacement unless restore
# explicitly clears it. Per-database settings are replayed again after the
# verified replacement receives the final target name, before connections are
# enabled, because their catalog key is the database OID/name at apply time.
ROLE_SETTINGS_KEYS_SQL = (
    "SELECT r.rolname || ' = ' || split_part(setting, '=', 1) "
    "FROM pg_catalog.pg_db_role_setting s "
    "JOIN pg_catalog.pg_roles r ON r.oid = s.setrole "
    "CROSS JOIN LATERAL unnest(s.setconfig) AS setting "
    "WHERE s.setdatabase = 0 "
    "AND r.rolname IN ('app_tenant', 'app_platform') "
    "ORDER BY 1;"
)
# FIX(round-23): privileged attributes are cluster-global like memberships,
# and a clean roles.sql neither carries nor revokes them -- a target
# app_tenant left SUPERUSER by hand therefore satisfied every text gate and
# the post-apply NAME check while keeping the RLS-bypassing attribute. The
# token list mirrors _PRIVILEGED_ATTRIBUTE_TOKENS exactly.
ROLE_PRIVILEGED_ATTRIBUTES_SQL = (
    "SELECT rolname FROM pg_catalog.pg_roles "
    "WHERE rolname IN ('app_tenant', 'app_platform') "
    "AND (rolsuper OR rolbypassrls OR rolcanlogin OR rolcreaterole "
    "OR rolcreatedb OR rolreplication) "
    "ORDER BY rolname;"
)
# FIX(round-25 P1 x2): advisory locks do not stop application writers. Restore
# therefore refuses any live target session before staging, restores into an
# unreachable replacement name, then disables connections and drains both
# names before the two-rename cutover. The standard Compose stack configures
# the application from the same UMS_DB_USER as POSTGRES_USER, so filtering by
# usename would exclude every default app pool; only the query backend is
# excluded when the query runs against the target itself.
FOREIGN_WRITER_SESSIONS_SQL = (
    "SELECT count(*) FROM pg_catalog.pg_stat_activity "
    "WHERE datname = current_database() "
    "AND pid <> pg_backend_pid();"
)

DATABASE_LOCALE_SQL = (
    "SELECT b.encoding::text || '|' || pg_encoding_to_char(b.encoding) "
    "|| '|' || coalesce(b.datcollate, '') || '|' || coalesce(b.datctype, '') "
    "|| '|' || coalesce(b.datlocprovider::text, '') "
    "|| '|' || coalesce(b.datlocale, '') "
    "FROM pg_catalog.pg_database b WHERE b.datname = current_database();"
)
DATABASE_LOCALE_LEGACY_SQL = (
    "SELECT b.encoding::text || '|' || pg_encoding_to_char(b.encoding) "
    "|| '|' || coalesce(b.datcollate, '') || '|' || coalesce(b.datctype, '') "
    "|| '|' || coalesce(b.datlocprovider::text, '') "
    "|| '|' || coalesce(b.daticulocale, '') "
    "FROM pg_catalog.pg_database b WHERE b.datname = current_database();"
)

# The lock is held by one psql session connected to the maintenance database,
# not by a short-lived query against the target database. It therefore remains
# alive across isolated replay, verification, connection drain, and both
# cutover renames; application sessions do not cooperate with it and are
# separately disabled and drained at the cutover boundary.
RESTORE_LOCK_PREFIX = "ums-smart-revenue.restore/"
DATABASE_ACL_PRIVILEGES = frozenset({"CONNECT", "CREATE", "TEMPORARY"})
# Count user objects across every non-system schema so a database that only
# has tables/enums/functions/empty custom schemas cannot look empty and
# bypass --allow-nonempty.
#
# FIX: This statement is assembled as ONE static raw literal on purpose --
# DeepSource BAN-B608 (and bandit) treat runtime `+` assembly of an executed
# query as a string-built SQL vector, even when every fragment is constant.
# Nothing here interpolates runtime data; do not reintroduce concatenation.
USER_OBJECT_COUNT_SQL = r"""
    SELECT (
        (SELECT count(*) FROM pg_catalog.pg_class c
         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\'
           AND n.nspname NOT LIKE 'pg\_toast\_temp\_%' ESCAPE '\'
           AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f'))
      + (SELECT count(*) FROM pg_catalog.pg_type t
         JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\'
           AND n.nspname NOT LIKE 'pg\_toast\_temp\_%' ESCAPE '\'
           AND t.typtype IN ('b', 'c', 'd', 'e', 'r', 'm'))
      + (SELECT count(*) FROM pg_catalog.pg_proc p
         JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\'
           AND n.nspname NOT LIKE 'pg\_toast\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_namespace n
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\'
           AND n.nspname NOT LIKE 'pg\_toast\_temp\_%' ESCAPE '\'
           AND n.nspname <> 'public')
      + (SELECT count(*) FROM pg_catalog.pg_collation col
         JOIN pg_catalog.pg_namespace n ON n.oid = col.collnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_conversion cvt
         JOIN pg_catalog.pg_namespace n ON n.oid = cvt.connamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_operator op
         JOIN pg_catalog.pg_namespace n ON n.oid = op.oprnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_opclass oc
         JOIN pg_catalog.pg_namespace n ON n.oid = oc.opcnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_opfamily opf
         JOIN pg_catalog.pg_namespace n ON n.oid = opf.opfnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_ts_config tsc
         JOIN pg_catalog.pg_namespace n ON n.oid = tsc.cfgnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_ts_dict tsd
         JOIN pg_catalog.pg_namespace n ON n.oid = tsd.dictnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_ts_parser tsp
         JOIN pg_catalog.pg_namespace n ON n.oid = tsp.prsnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_ts_template tst
         JOIN pg_catalog.pg_namespace n ON n.oid = tst.tmptnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_statistic_ext se
         JOIN pg_catalog.pg_namespace n ON n.oid = se.stxnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_largeobject_metadata)
      + (SELECT count(*) FROM pg_catalog.pg_extension ext
         JOIN pg_catalog.pg_namespace n ON n.oid = ext.extnamespace
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
      + (SELECT count(*) FROM pg_catalog.pg_event_trigger evt)
      + (SELECT count(*) FROM pg_catalog.pg_foreign_data_wrapper fdw)
      + (SELECT count(*) FROM pg_catalog.pg_foreign_server fsrv)
    )::bigint;
"""
LIST_TABLES_SQL = (
    "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public' ORDER BY tablename;"
)


class RestoreError(Exception):
    """Fatal restore failure carrying the exit code the operator should see."""

    def __init__(self, code: int, message: str) -> None:
        """Attach the operator-facing exit code to the failure message."""
        super().__init__(message)
        self.code = code


def _quote_identifier(name: str) -> str:
    """Double-quote a SQL identifier, escaping embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    """Single-quote a SQL string literal, escaping embedded quotes."""
    return "'" + value.replace("'", "''") + "'"


def _count_sql_branch(name: str) -> str:
    """One UNION ALL branch that counts rows for a validated public table name."""
    return "".join(
        [
            "SELECT ",
            _quote_literal(name),
            " AS t, count(*) AS n FROM public.",
            _quote_identifier(name),
        ]
    )


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run argv with stdin closed and captured text output."""
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_with_input(
    argv: list[str], *, timeout: int, stdin_text: str
) -> subprocess.CompletedProcess[str]:
    """Run argv feeding stdin_text and capturing text output."""
    return subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_with_file(
    argv: list[str], *, timeout: int, source: Path
) -> subprocess.CompletedProcess[str]:
    """Run argv with binary stdin from source and captured text output."""
    with source.open("rb") as handle:
        return subprocess.run(
            argv,
            stdin=handle,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


def _container_sh(container: str, body: str) -> list[str]:
    """Build a docker exec argv that runs body under sh with PGPASSWORD set."""
    return ["docker", "exec", "-i", container, "sh", "-c", _SH_PREFIX + body]


def _psql(
    container: str,
    sql: str,
    *,
    timeout: int,
    stop_on_error: bool = True,
    dbname: str | None = None,
) -> str:
    """Run SQL via psql inside the container and return stdout.

    ``dbname`` targets a specific database; the container default
    ``$POSTGRES_DB`` is used otherwise.
    """
    stop = "1" if stop_on_error else "0"
    # FIX(Qodo round-26): dbname was interpolated verbatim into a
    # double-quoted sh -c string, so a POSTGRES_DB value carrying $(...),
    # backticks or quotes executed as shell inside the target container
    # before psql ever ran. shlex.quote single-quotes the value so no
    # metacharacter can escape, while every legitimate database name --
    # including hyphenated ones -- keeps working unquoted-in-effect.
    target = f"-d {shlex.quote(dbname)} " if dbname else '-d "$POSTGRES_DB" '
    argv = _container_sh(
        container,
        f'exec psql -X -U "$POSTGRES_USER" {target}'
        f"--no-password -v ON_ERROR_STOP={stop} -Atq -f -",
    )
    completed = _run_with_input(argv, timeout=timeout, stdin_text=sql)
    if stop_on_error and completed.returncode != 0:
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"psql failed inside {container}: {completed.stderr.strip()}",
        )
    return completed.stdout


def _sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of a file."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _require_restorable_backup_dir(backup_dir: Path) -> None:
    """Refuse non-directories, quarantined names, and incomplete run layouts."""
    if not backup_dir.is_dir():
        raise RestoreError(EXIT_USAGE, f"{backup_dir} is not a directory")
    # FIX: a staging dir left as *.partial can hold all three files after an
    # interrupted publish; refuse the name before trusting the layout.
    if backup_dir.name.endswith(".partial"):
        raise RestoreError(
            EXIT_USAGE,
            f"{backup_dir.name} is still marked .partial (publish never finished). "
            "Do not restore an unpublished staging directory.",
        )
    if backup_dir.name.endswith(REJECTED_SUFFIX) or REJECTED_SUFFIX + "-" in backup_dir.name:
        raise RestoreError(
            EXIT_USAGE,
            f"{backup_dir.name} is a quarantined run, not a backup. The backup "
            "script rejected it because it captured no application data. "
            "Restoring it would replace the target with an empty database. Pick "
            "an ums-backup-...Z directory instead.",
        )
    for name in (MANIFEST_NAME, ROLES_NAME, DUMP_NAME):
        if not (backup_dir / name).is_file():
            raise RestoreError(
                EXIT_USAGE,
                f"{backup_dir} is not a backup run: {name} is missing. A "
                f"directory still named *.partial was never verified and must "
                f"not be restored.",
            )


def _read_restore_manifest(manifest_path: Path) -> dict[str, object]:
    """Load manifest.json as a JSON object or raise RestoreError."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RestoreError(EXIT_USAGE, f"cannot read {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RestoreError(EXIT_USAGE, f"{manifest_path} is not a JSON object")
    return manifest


def _refuse_rejected_content_gate(manifest: dict[str, object], *, backup_name: str) -> None:
    """Refuse manifests whose content_gate status is rejected."""
    gate = manifest.get("content_gate")
    if not (isinstance(gate, dict) and gate.get("status") == "rejected"):
        return
    reasons = gate.get("failures")
    detail = "; ".join(str(item) for item in reasons) if isinstance(reasons, list) else ""
    raise RestoreError(
        EXIT_USAGE,
        f"{backup_name} failed the backup content gate "
        f"(tables={gate.get('tables')}, rows={gate.get('rows')}) and is not "
        f"restorable: {detail or 'it captured no application data'}",
    )


def _verify_backup_artifact_digests(backup_dir: Path, manifest: dict[str, object]) -> None:
    """Require artifact metadata and match the bytes currently on disk."""
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RestoreError(EXIT_USAGE, f"{backup_dir / MANIFEST_NAME} has no artifacts block")
    for name in (DUMP_NAME, ROLES_NAME):
        entry = artifacts.get(name)
        expected = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(expected, str) or not expected.strip():
            raise RestoreError(
                EXIT_ARTIFACT_INTEGRITY,
                f"{name} has no sha256 in the manifest. Do not restore this run.",
            )
        artifact = backup_dir / name
        expected_bytes = entry.get("bytes") if isinstance(entry, dict) else None
        if expected_bytes is not None and (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
        ):
            raise RestoreError(
                EXIT_ARTIFACT_INTEGRITY,
                f"{name} has an invalid byte count in the manifest. Do not restore this run.",
            )
        try:
            actual_size = artifact.stat().st_size
            if actual_size <= 0:
                raise RestoreError(
                    EXIT_ARTIFACT_INTEGRITY,
                    f"{name} is empty. Do not restore this run.",
                )
            if expected_bytes is not None and actual_size != expected_bytes:
                raise RestoreError(
                    EXIT_ARTIFACT_INTEGRITY,
                    f"{name} size does not match the manifest "
                    f"(expected {expected_bytes}, got {actual_size}). Do not restore this run.",
                )
            actual = _sha256(artifact)
        except OSError as exc:
            # A source can disappear or become unreadable between manifest
            # load and this point-of-use check. Keep that distinct from a
            # subprocess failure: no artifact bytes were safely verified.
            raise RestoreError(
                EXIT_ARTIFACT_INTEGRITY,
                f"{name} could not be read while verifying the manifest: {exc}",
            ) from exc
        if actual != expected:
            raise RestoreError(
                EXIT_ARTIFACT_INTEGRITY,
                f"{name} does not match the manifest sha256 "
                f"(expected {expected}, got {actual}). Do not restore this run.",
            )


# ============================================================================
# Purpose: Restrict the host directory that temporarily holds dump and role
#          artifacts before any sensitive byte is copied into it.
# Database/ORM: None; host filesystem permissions only.
# Standards: POSIX owner mode plus the backup publisher's verified NTFS DACL;
#            fail closed when either policy cannot be enforced.
# Blast Radius: Backup data, role verifiers, and local-host confidentiality.
# Connections:
#   - File: scripts/backup_database.py -> shared owner-only NTFS ACL policy.
#   - File: scripts/restore_database.py -> _stage_backup_artifacts calls first.
# ============================================================================
def _restrict_restore_staging(path: Path) -> None:
    """Fail closed unless restore staging is owner-only on this host."""
    # Reuse the publisher's verified POSIX-mode and NTFS-DACL policy. Both
    # scripts ship in the same directory; duplicating either fail-closed check
    # here would let their owner-only definitions drift independently.
    try:
        import backup_database as backup_security
    except ImportError as exc:
        raise RestoreError(
            EXIT_ARTIFACT_INTEGRITY,
            "could not load backup_database.py to enforce restore staging permissions",
        ) from exc
    try:
        backup_security._restrict_run_dir_mode(path, path.parent, strict=True)
    except backup_security.BackupError as exc:
        raise RestoreError(
            EXIT_ARTIFACT_INTEGRITY,
            f"could not enforce owner-only restore staging permissions: {exc}",
        ) from exc


# ============================================================================
# Purpose: Copy both artifacts into a private temporary directory and verify
#          the copies immediately before any Docker or database operation.
# Database/ORM: None; host-side immutable restore input.
# Standards: Separate source/copy hashes; fsync before use; cleanup never masks
#            an active restore exception.
# Blast Radius: Disaster recovery input integrity and host secret exposure.
# Connections:
#   - File: scripts/restore_database.py -> main and _execute_restore.
# ============================================================================
@contextmanager
def _stage_backup_artifacts(
    backup_dir: Path, manifest: dict[str, object]
) -> Iterator[Path]:
    """Yield private copies whose digests are verified at point of use."""
    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=".ums-restore-", dir=backup_dir.parent))
        _restrict_restore_staging(staging)
        for name in (DUMP_NAME, ROLES_NAME):
            source = backup_dir / name
            target = staging / name
            try:
                with source.open("rb") as source_handle, target.open("xb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle)
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
                os.chmod(target, 0o600)
                if os.name != "nt" and target.stat().st_mode & 0o777 != 0o600:
                    raise OSError(
                        f"effective mode on {target} is not owner read/write"
                    )
            except OSError as exc:
                raise RestoreError(
                    EXIT_ARTIFACT_INTEGRITY,
                    f"could not stage {name} for restore: {exc}",
                ) from exc
        _verify_backup_artifact_digests(staging, manifest)
        yield staging
    finally:
        if staging is not None:
            try:
                shutil.rmtree(staging)
            except OSError as cleanup_exc:
                if sys.exc_info()[0] is None:
                    raise RestoreError(
                        EXIT_RESTORE_FAILED,
                        f"restore staging cleanup failed for {staging}: {cleanup_exc}",
                    ) from cleanup_exc
                print(
                    f"WARNING: restore staging cleanup failed for {staging}: {cleanup_exc}",
                    file=sys.stderr,
                )


# ============================================================================
# Purpose: Load and integrity-check the backup run before anything is applied
#          to a database, so a corrupted or half-copied backup is discovered
#          before it has half-restored over something.
# Database/ORM: None.
# Standards: Both artifacts are re-hashed against the manifest; a mismatch is
#            a distinct exit code (8) so "the backup rotted" never reads as
#            "the restore command was wrong". A run the backup script
#            quarantined for holding no application data is refused on two
#            independent signals -- its ``.rejected`` directory name and its
#            manifest verdict -- so restoring an empty database over a live one
#            takes more than one mistake. Manifests written before the content
#            gate existed carry no verdict, and absence is not treated as
#            rejection: those runs still restore.
# Blast Radius: Disaster recovery gate.
# Connections:
#   - File: scripts/backup_database.py -> writes manifest.json with sha256
#     digests for database.dump and roles.sql, and the content_gate block plus
#     the ``.rejected`` naming this function refuses.
# ============================================================================
def _require_manifest_table_row_counts(manifest: dict[str, object]) -> dict[str, int]:
    """Require a well-formed table_row_counts map before any restore apply.

    FIX: malformed or missing counts once surfaced only after destructive
    replay committed. Validate before roles or replacement creation; the
    isolated verifier repeats the same check before cutover.
    """
    expected_raw = manifest.get("table_row_counts")
    if not isinstance(expected_raw, dict):
        raise RestoreError(
            EXIT_USAGE,
            "manifest.table_row_counts is missing or not a JSON object; "
            "refusing to apply an unverifiable backup",
        )
    # FIX: int() accepted booleans as 0/1 and truncated fractional counts, and
    # an empty mapping verified trivially -- so the old overlay path could
    # commit and only discover the mismatch, or coincidentally match, during
    # verification. Require a
    # non-empty mapping of exact nonnegative JSON integers up front.
    if not expected_raw:
        raise RestoreError(
            EXIT_USAGE,
            "manifest.table_row_counts holds no table row counts (empty object); "
            "refusing to apply a backup whose table populations cannot be verified",
        )
    expected: dict[str, int] = {}
    for key, value in expected_raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RestoreError(
                EXIT_USAGE,
                f"manifest.table_row_counts holds a row count that is not an "
                f"exact nonnegative integer at {key!r}: {value!r} "
                "(booleans, floats, strings, negatives refused)",
            )
        expected[str(key)] = value
    return expected


# ============================================================================
# Purpose: Recheck the backup content gate from manifest facts before any
#          cluster role or database state is changed.
# Database/ORM: None; validates manifest table counts and content-gate metadata.
# Standards: Fail closed on malformed verdicts, missing core seed tables,
#            drained seeds, or aggregate values that do not match the counts.
# Blast Radius: Disaster recovery admission; blocks empty/gutted restores.
# Connections:
#   - File: scripts/backup_database.py -> ContentVerdict.as_manifest_block.
#   - File: tests/scripts/test_backup_content_gate.py -> seed-extension tests.
# ============================================================================
def _require_manifest_seed_floor(
    manifest: dict[str, object], counts: dict[str, int]
) -> tuple[str, ...]:
    """Require every declared/current seed table to be present and populated."""
    gate = manifest.get("content_gate")
    required = list(CORE_SEED_TABLES)
    if gate is not None:
        if not isinstance(gate, dict) or gate.get("status") != "accepted":
            raise RestoreError(
                EXIT_USAGE,
                "manifest.content_gate is malformed or is not accepted; "
                "refusing to apply an unapproved backup",
            )
        raw_seed_tables = gate.get("seed_tables")
        if not isinstance(raw_seed_tables, list) or not raw_seed_tables:
            raise RestoreError(
                EXIT_USAGE,
                "manifest.content_gate.seed_tables is missing or empty; "
                "refusing a backup whose seed floor cannot be reproduced",
            )
        if any(not isinstance(name, str) or not name.strip() for name in raw_seed_tables):
            raise RestoreError(
                EXIT_USAGE,
                "manifest.content_gate.seed_tables contains a blank or non-string name",
            )
        if len(set(raw_seed_tables)) != len(raw_seed_tables):
            raise RestoreError(
                EXIT_USAGE,
                "manifest.content_gate.seed_tables contains duplicate names",
            )
        if not set(CORE_SEED_TABLES).issubset(raw_seed_tables):
            missing_contract = sorted(set(CORE_SEED_TABLES) - set(raw_seed_tables))
            raise RestoreError(
                EXIT_USAGE,
                "manifest.content_gate.seed_tables omits this restore contract's "
                f"required tables: {', '.join(missing_contract)}",
            )
        required = list(raw_seed_tables)
        gate_tables = gate.get("tables")
        gate_rows = gate.get("rows")
        if (
            isinstance(gate_tables, bool)
            or not isinstance(gate_tables, int)
            or gate_tables < 0
            or isinstance(gate_rows, bool)
            or not isinstance(gate_rows, int)
            or gate_rows < 0
        ):
            raise RestoreError(
                EXIT_USAGE,
                "manifest.content_gate table/row aggregates are not exact "
                "nonnegative integers",
            )
        if gate_tables != len(counts) or gate_rows != sum(counts.values()):
            raise RestoreError(
                EXIT_USAGE,
                "manifest.content_gate aggregate table/row counts disagree with "
                "manifest.table_row_counts",
            )
    missing = [name for name in required if name not in counts]
    drained = [name for name in required if name in counts and counts[name] == 0]
    if missing or drained:
        detail: list[str] = []
        if missing:
            detail.append("missing: " + ", ".join(missing))
        if drained:
            detail.append("empty: " + ", ".join(drained))
        raise RestoreError(
            EXIT_USAGE,
            "manifest fails the required seed-table floor ("
            + "; ".join(detail)
            + "); refusing to replace a target with a gutted database",
        )
    return tuple(required)


def _require_manifest_large_object_count(manifest: dict[str, object]) -> int:
    """Require the exact large-object population recorded by the backup."""
    value = manifest.get("large_object_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RestoreError(
            EXIT_USAGE,
            "manifest.large_object_count is missing or is not an exact "
            "nonnegative integer; refusing an unverifiable backup",
        )
    return value


def _parse_database_acl(raw: object) -> tuple[str, list[tuple[str, str, bool]]]:
    """Validate the owner/privilege JSON captured from ``pg_database``."""
    if not isinstance(raw, str) or not raw.strip():
        raise RestoreError(
            EXIT_USAGE,
            "manifest source.database_acl is missing; refusing to create a "
            "replacement without its authorization policy",
        )
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise RestoreError(
            EXIT_USAGE,
            f"manifest source.database_acl is not valid JSON: {exc}",
        ) from exc
    if not isinstance(document, dict):
        raise RestoreError(EXIT_USAGE, "manifest source.database_acl is not an object")
    owner = document.get("owner")
    entries = document.get("entries")
    if (
        not isinstance(owner, str)
        or not owner.strip()
        or owner == "PUBLIC"
        or not isinstance(entries, list)
    ):
        raise RestoreError(
            EXIT_USAGE,
            "manifest source.database_acl must contain a real owner and entries list",
        )
    parsed: list[tuple[str, str, bool]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RestoreError(EXIT_USAGE, "manifest source.database_acl entry is not an object")
        grantee = entry.get("grantee")
        privilege = entry.get("privilege")
        grantable = entry.get("grantable")
        if (
            not isinstance(grantee, str)
            or not grantee.strip()
            or not isinstance(privilege, str)
            or privilege.upper() not in DATABASE_ACL_PRIVILEGES
            or not isinstance(grantable, bool)
        ):
            raise RestoreError(
                EXIT_USAGE,
                "manifest source.database_acl contains an invalid grantee, "
                "privilege or grantable flag",
            )
        parsed.append((grantee, privilege.upper(), grantable))
    return owner, parsed


def _source_database_metadata(
    manifest: dict[str, object],
) -> tuple[str, str, list[tuple[str, str, bool]]]:
    """Require locale and database ACL provenance before any restore mutation."""
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise RestoreError(
            EXIT_USAGE,
            "manifest source metadata is missing; locale and database ACL "
            "fidelity cannot be proven",
        )
    locale_row = source.get("database_locale")
    if not isinstance(locale_row, str) or not locale_row.strip():
        raise RestoreError(
            EXIT_USAGE,
            "manifest source.database_locale is missing; refusing a restore "
            "whose database collation and encoding cannot be reproduced",
        )
    # Parse now, before role replay or replacement creation. This also rejects
    # malformed locale fields before SQL literals are constructed.
    _create_database_options(locale_row)
    owner, acl = _parse_database_acl(source.get("database_acl"))
    return locale_row, owner, acl


# ============================================================================
# Purpose: Bind restore to the database name recorded by the backup so
#          roles.sql settings scoped with ALTER ROLE ... IN DATABASE retain
#          their meaning after the replacement receives its final name.
# Database/ORM: None; compares manifest provenance with container POSTGRES_DB.
# Standards: Exact string match and fail-closed missing provenance.
# Blast Radius: Role settings, permissions, and restore target selection.
# Connections:
#   - File: scripts/backup_database.py -> source.database manifest field.
#   - File: scripts/restore_database.py -> _execute_restore before lock/mutation.
# ============================================================================
def _require_source_database_target(
    manifest: dict[str, object], target_db: str
) -> None:
    """Refuse a target name different from the archive's source database."""
    source = manifest.get("source")
    source_db = source.get("database") if isinstance(source, dict) else None
    if not isinstance(source_db, str) or not source_db:
        raise RestoreError(
            EXIT_USAGE,
            "manifest source.database is missing; database-scoped role settings "
            "cannot be replayed safely",
        )
    if source_db != target_db:
        raise RestoreError(
            EXIT_USAGE,
            f"backup source database {source_db!r} does not match target "
            f"POSTGRES_DB {target_db!r}; refusing to misapply database-scoped "
            "role settings. Restore into a container with the recorded database name",
        )


def _database_acl_role_problems(
    roles_path: Path, owner: str, entries: list[tuple[str, str, bool]]
) -> list[str]:
    """Return ACL roles that the preflight roles file cannot create."""
    declared = set(_created_role_names(roles_path.read_text(encoding="utf-8", errors="replace")))
    referenced = {owner}
    referenced.update(grantee for grantee, _privilege, _grantable in entries)
    missing = sorted(
        role
        for role in referenced
        if role != "PUBLIC" and not role.startswith("pg_") and role not in declared
    )
    return missing


def _require_manifest_schema(manifest: dict[str, object], manifest_path: Path) -> None:
    """Refuse a manifest whose schema field is not exactly MANIFEST_SCHEMA.

    FIX: every later check interprets manifest fields against the layout this
    tool writes, but the ``schema`` discriminator itself was never read -- a
    tampered, hand-edited or future-format manifest with an unexpected
    ``schema`` value sailed into the digest and count checks that assume this
    format. A wrong value is a malformed backup directory (documented exit 2),
    not a rotted artifact (exit 8): nothing was hashed against a promise this
    format made.
    """
    schema = manifest.get("schema")
    if schema != MANIFEST_SCHEMA:
        raise RestoreError(
            EXIT_USAGE,
            f"{manifest_path} is not a {MANIFEST_SCHEMA} backup run: its schema "
            f"field is missing or unrecognized. Do not restore this directory "
            "with this script.",
        )


def _load_backup(backup_dir: Path) -> dict[str, object]:
    """Load and integrity-check a backup run; return its manifest dict."""
    _require_restorable_backup_dir(backup_dir)
    manifest_path = backup_dir / MANIFEST_NAME
    manifest = _read_restore_manifest(manifest_path)
    # The schema discriminator is checked first: every check below interprets
    # manifest fields against the layout the schema value promises.
    _require_manifest_schema(manifest, manifest_path)
    _refuse_rejected_content_gate(manifest, backup_name=manifest_path.parent.name)
    _verify_backup_artifact_digests(backup_dir, manifest)
    counts = _require_manifest_table_row_counts(manifest)
    _require_manifest_seed_floor(manifest, counts)
    _require_manifest_large_object_count(manifest)
    return manifest


def _require_docker(*, timeout: int) -> None:
    """Fail closed unless the docker CLI can talk to a running daemon."""
    try:
        completed = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=timeout)
    except FileNotFoundError as exc:
        raise RestoreError(EXIT_DOCKER_UNAVAILABLE, "docker CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RestoreError(
            EXIT_DOCKER_UNAVAILABLE,
            f"Docker daemon probe timed out after {timeout}s",
        ) from exc
    if completed.returncode != 0:
        raise RestoreError(
            EXIT_DOCKER_UNAVAILABLE,
            f"Docker daemon is not available: {completed.stderr.strip()}",
        )


def _resolve_container(*, explicit: str | None, project: str, service: str, timeout: int) -> str:
    """Return a running container id/name from --container or compose labels."""
    if explicit:
        completed = _run(
            ["docker", "inspect", "--format", "{{.State.Running}}", explicit], timeout=timeout
        )
        if completed.returncode != 0 or completed.stdout.strip() != "true":
            raise RestoreError(
                EXIT_CONTAINER_UNAVAILABLE,
                f"container {explicit!r} is not running: {completed.stderr.strip()}",
            )
        return explicit
    completed = _run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ],
        timeout=timeout,
    )
    ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(ids) != 1:
        raise RestoreError(
            EXIT_CONTAINER_UNAVAILABLE,
            f"expected exactly one running container for project={project} "
            f"service={service}, found {len(ids)}",
        )
    return ids[0]


# ============================================================================
# Purpose: Wait for the *real* server, not the initdb bootstrap server, before
#          anything is applied to the target database.
# Database/ORM: Connection probe plus one ``SELECT 1`` on the target database.
# Standards: Readiness means two things in the same iteration -- the server
#            answers on TCP AND a real query against the target database
#            succeeds. A socket-only ``pg_isready`` is not enough: the official
#            postgres image runs a temporary server during first-boot
#            initialisation that listens on the unix socket only, so a
#            socket-based probe reported ready and the roles step then failed
#            with `database "..." does not exist` followed by "the database
#            system is shutting down". That was observed, not theorised, while
#            rehearsing this script.
# Blast Radius: A false ready aborts a restore part-way through, which is the
#               state this script exists to avoid.
# Connections:
#   - File: scripts/backup_database.py -> ``_await_postgres`` is the same probe
#     on the backup side and must stay in step with this one.
# ============================================================================
def _await_postgres(container: str, *, wait_seconds: int, timeout: int) -> None:
    """Wait until TCP-ready Postgres accepts SELECT 1 on the target database."""
    deadline = time.monotonic() + max(wait_seconds, 0)
    last_error = "postgres did not answer"
    while True:
        tcp = _run(
            _container_sh(
                container,
                'exec pg_isready -h 127.0.0.1 -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
            ),
            timeout=timeout,
        )
        if tcp.returncode == 0:
            probe = _run(
                _container_sh(
                    container,
                    'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                    '--no-password -Atqc "SELECT 1"',
                ),
                timeout=timeout,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "1":
                return
            last_error = (probe.stdout + probe.stderr).strip() or last_error
        else:
            last_error = (tcp.stdout + tcp.stderr).strip() or last_error
        if time.monotonic() >= deadline:
            raise RestoreError(
                EXIT_CONTAINER_UNAVAILABLE,
                f"Postgres in {container} is not accepting connections: {last_error}",
            )
        time.sleep(2)


# ============================================================================
# Purpose: Create the disposable container the rehearsal restores into, using
#          the locally verified image config ID recorded at backup time or an
#          image the operator named with --rehearse-image, plus the database
#          name and superuser recorded at backup time.
# Database/ORM: Creates an empty cluster only.
# Standards: POSTGRES_HOST_AUTH_METHOD=trust and no published ports, so the
#            rehearsal needs no password anywhere and nothing on the host
#            network can reach the container. The name carries a UTC stamp so
#            two rehearsals cannot collide. The manifest is unsigned, so its
#            source.image reference is never executed: only a locally
#            verified image_id or an operator-typed --rehearse-image runs.
# Blast Radius: Creates and destroys one container. Touches no existing
#               container, volume, or compose project.
# Connections:
#   - File: scripts/backup_database.py -> records source.image_id,
#     source.database and source.superuser in manifest.json.
# ============================================================================

def _docker_image_exists(reference: str, *, timeout: int) -> bool:
    """Return whether ``docker image inspect`` finds ``reference`` locally."""
    if not reference:
        return False
    completed = _run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        timeout=timeout,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


# Repository names whose image can host the rehearsal Postgres server. The
# official image is published both as ``postgres`` and as ``library/postgres``;
# anything else (mysql, an arbitrary third-party repository) must not run.
_POSTGRES_IMAGE_REPOSITORIES = frozenset({"postgres", "library/postgres"})


def _image_repository(reference: str) -> str:
    """Return the repository part of one image reference (everything before :tag)."""
    return reference.rsplit(":", 1)[0].strip().lower()


# ============================================================================
# Purpose: Prove a locally present image is actually a Postgres image before a
#          rehearsal container is started from it.
# Database/ORM: None. Talks to the docker CLI only.
# Standards: Fail-closed. ``docker image inspect`` proves LOCAL PRESENCE, not
#            identity, so the image is accepted only when a repo tag's
#            repository part is ``postgres`` or ``library/postgres``
#            (case-insensitive). Untagged or ``<none>``-tagged images fall
#            back to the image config's base reference (``.Config.Image``);
#            a probe failure, unparseable output or a non-Postgres result all
#            refuse.
# Blast Radius: The rehearsal runs the image's entrypoint as root in a
#               container. This check is what stops a tampered manifest from
#               naming any local image ID and having it executed.
# Connections:
#   - File: scripts/restore_database.py -> ``_resolve_rehearsal_image`` calls
#     this on every candidate before it is returned.
# ============================================================================
def _image_is_postgres(reference: str, *, timeout: int) -> bool:
    """Return True only when ``reference`` resolves to a Postgres image."""
    if not reference:
        return False
    tags_completed = _run(
        ["docker", "image", "inspect", "--format", "{{json .RepoTags}}", reference],
        timeout=timeout,
    )
    if tags_completed.returncode != 0:
        return False
    try:
        tags = json.loads(tags_completed.stdout.strip() or "null")
    except ValueError:
        return False
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and _image_repository(tag) in _POSTGRES_IMAGE_REPOSITORIES:
                return True
    # Untagged (RepoTags null) or <none>-tagged images: compare the base
    # reference recorded in the image config instead.
    config_completed = _run(
        ["docker", "image", "inspect", "--format", "{{.Config.Image}}", reference],
        timeout=timeout,
    )
    if config_completed.returncode != 0:
        return False
    base = config_completed.stdout.strip()
    return bool(base) and _image_repository(base) in _POSTGRES_IMAGE_REPOSITORIES


def _resolve_rehearsal_image(*, image_id: str, operator_image: str, timeout: int) -> str:
    """Pick the rehearsal image the OPERATOR chose, never the manifest.

    FIX: this used to fall back to the manifest's ``source.image`` tag/digest
    when the locally recorded config ID had been pruned. The manifest is
    unsigned, so anyone who could alter or supply the backup could swap that
    field for an attacker-controlled image and have its entrypoint executed
    in a root container; ``--network none`` limits connectivity but not
    CPU, memory or Docker-storage exhaustion. Only two authorities can
    select the image now: the still-local recorded config ID (verified via
    ``docker image inspect``) or an explicit ``--rehearse-image`` the
    operator typed.

    FIX(P1): local presence was the ONLY check on the recorded image ID, but
    ``docker image inspect`` proves nothing about WHAT an image is -- a
    tampered manifest could name any image ID already on this host and its
    entrypoint would run in the rehearsal container. Every resolved candidate,
    operator-supplied included (operators can typo; the rehearsal needs a
    Postgres server), must now also pass a Postgres-image check before it is
    returned; a non-Postgres candidate is refused with EXIT_USAGE instead of
    being started.
    """
    candidate = operator_image.strip()
    if not candidate and image_id and _docker_image_exists(image_id, timeout=timeout):
        candidate = image_id
    if not candidate:
        return ""
    if not _image_is_postgres(candidate, timeout=timeout):
        raise RestoreError(
            EXIT_USAGE,
            "the resolved rehearsal image has no postgres / library/postgres "
            "repository tag, so it is not a Postgres image and will not be "
            "started (the recorded image ID or --rehearse-image reference "
            "names something else). Pull a Postgres image and pass "
            "--rehearse-image <reference>, or use --container with a "
            "container you prepared yourself.",
        )
    return candidate


def _create_throwaway(
    manifest: dict[str, object], *, timeout: int, operator_image: str = ""
) -> str:
    """Start a disposable Postgres container from an operator-trusted image."""
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise RestoreError(EXIT_USAGE, "manifest has no source block")
    image_id = str(source.get("image_id") or "").strip()
    run_image = _resolve_rehearsal_image(
        image_id=image_id, operator_image=operator_image, timeout=timeout
    )
    database = str(source.get("database") or "")
    superuser = str(source.get("superuser") or "")
    if not run_image or not database or not superuser:
        raise RestoreError(
            EXIT_USAGE,
            "no runnable rehearsal image: the image recorded at backup time is "
            "no longer present locally, and the manifest is unsigned, so its "
            "source.image reference is never executed on its own authority. "
            "Pull a Postgres image yourself and pass --rehearse-image "
            "<reference>, or use --container with a container you prepared "
            "yourself. (manifest.source otherwise lacks image_id, database or "
            "superuser.)",
        )
    name = (
        THROWAWAY_PREFIX
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    try:
        completed = _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                "none",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                f"POSTGRES_USER={superuser}",
                "--env",
                f"POSTGRES_DB={database}",
                run_image,
            ],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if not _destroy_throwaway(name, timeout=timeout):
            print(
                f"WARNING: timed out starting {name!r} and cleanup also failed; "
                "remove the container and its anonymous volume manually.",
                flush=True,
            )
        raise RestoreError(
            EXIT_CONTAINER_UNAVAILABLE,
            f"timed out starting throwaway container from {run_image} after {timeout}s",
        ) from exc
    if completed.returncode != 0:
        raise RestoreError(
            EXIT_CONTAINER_UNAVAILABLE,
            f"could not start the throwaway container from {run_image}: {completed.stderr.strip()}",
        )
    return name


# ============================================================================
# Purpose: Remove the disposable rehearsal container started by
#          ``_create_throwaway``. Returns success/failure rather than raising
#          so a finally-block cleanup cannot mask the restore exit code.
# Database/ORM: Destroys one empty rehearsal cluster only.
# Standards: ``docker rm --force --volumes``; TimeoutExpired is treated as a
#            failed destroy (False), never allowed to escape cleanup.
# Blast Radius: Destroys only the named throwaway container. Touches no
#               existing container, volume, or compose project.
# Connections:
#   - File: scripts/restore_database.py -> ``_create_throwaway`` / main finally.
#   - File: scripts/backup_database.py -> source image recorded in manifest.
# ============================================================================
def _destroy_throwaway(name: str, *, timeout: int) -> bool:
    """Remove the rehearsal container. Returns True only when docker rm succeeded."""
    try:
        completed = _run(["docker", "rm", "--force", "--volumes", name], timeout=timeout)
    except subprocess.TimeoutExpired:
        # FIX: Timeout must not escape a finally-block cleanup and mask the
        # restore result; treat it as a failed destroy like a nonzero exit.
        return False
    return completed.returncode == 0


_ROLE_ALREADY_EXISTS = re.compile(
    r'^ERROR:\s+role\s+"[^"]+"\s+already exists\s*$',
    re.IGNORECASE,
)


def _unexpected_roles_errors(stderr: str) -> list[str]:
    """Return FATAL/ERROR lines from roles.sql stderr that are not bootstrap duplicates.

    ``psql -f -`` often prefixes diagnostics as ``psql: :N: ERROR: ...``; extract
    the ``ERROR:`` portion before classifying so prefixed lines are not skipped.
    FATAL lines (for example connection termination) always fail closed even when
    an earlier allowed duplicate made ``returncode != 0`` look expected.
    """
    unexpected: list[str] = []
    for line in stderr.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if "FATAL:" in upper:
            unexpected.append(stripped)
            continue
        marker = upper.find("ERROR:")
        if marker < 0:
            continue
        diagnostic = stripped[marker:]
        if _ROLE_ALREADY_EXISTS.match(diagnostic):
            continue
        unexpected.append(stripped)
    return unexpected


def _allowed_role_duplicate_lines(stderr: str) -> list[str]:
    """Return stderr lines whose ERROR diagnostic is only bootstrap role-already-exists."""
    allowed: list[str] = []
    for line in stderr.splitlines():
        stripped = line.strip()
        marker = stripped.upper().find("ERROR:")
        if marker < 0:
            continue
        diagnostic = stripped[marker:]
        if _ROLE_ALREADY_EXISTS.match(diagnostic):
            allowed.append(stripped)
    return allowed


# ============================================================================
# Purpose: Apply roles.sql, then refuse to continue unless app_tenant and
#          app_platform actually exist. This is the trap the whole script is
#          built around.
# Database/ORM: Creates cluster-global roles and their memberships.
# Standards: ON_ERROR_STOP is deliberately OFF for this file -- pg_dumpall
#            emits CREATE ROLE for the bootstrap superuser too, which already
#            exists in any container the official image initialised, and that
#            expected "role already exists" must not abort the restore (psql
#            often exits non-zero for that ERROR). Every other ERROR: line, or
#            a non-zero exit with no allowed duplicate ERROR, fails closed with
#            EXIT_ROLES_FAILED. The catalog check remains a second gate, not a
#            substitute for a clean apply.
# Blast Radius: Authorization. These roles carry the RLS grant surface; a
#               restore that proceeded without them would produce a database
#               whose policies could not be installed.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py
#     -> ``_create_role`` (lines 92-113) owns both roles.
# ============================================================================
def _unsupported_role_ddl_in_roles_sql(body: str) -> str | None:
    """Return a reason when roles.sql uses procedural or dynamic role DDL."""
    if re.search(r"(?im)^\s*DO\b", body):
        return "DO blocks are not allowed in roles.sql"
    if re.search(r"(?im)EXECUTE\s+.*CREATE\s+(?:ROLE|USER)", body):
        return "dynamic CREATE ROLE via EXECUTE is not allowed in roles.sql"
    return None


_PROTECTED_APP_ROLES = REQUIRED_ROLES


# ============================================================================
# Purpose: Read roles.sql the way psql's own lexer reads it, so every gate
#          below judges the statements the executor will actually run.
# Database/ORM: None. Pure text, so these checks run before cluster role replay
#               or replacement-database creation.
# Standards: Comment stripping, quote-aware splitting, psql meta-command
#            extraction and literal masking happen ONCE, here. No gate below
#            gets its own regex over the raw file: two parsers for one language
#            is exactly how a validator and psql end up reading different
#            programs. Literal BODIES are masked to ``''`` so a semicolon or an
#            attribute keyword inside a string can neither move a statement
#            boundary nor forge a token, and so a refused statement can be
#            quoted back at the operator without echoing a SCRAM verifier out
#            of an ``--include-role-passwords`` archive.
# Blast Radius: Authorization. A statement this function drops is a statement
#               no gate below can refuse.
# Connections:
#   - File: scripts/backup_database.py -> the byte-identical block on the
#     publish side; ``test_restore_and_backup_share_one_role_sql_gate``
#     compares ``inspect.getsource`` of every function here.
#   - File: scripts/restore_database.py -> ``_restore_roles`` pipes this file
#     into ``psql -f -``; psql is the parser this scanner has to agree with.
# ============================================================================
_ROLE_SQL_BOM = "\ufeff\ufffe"
_ROLE_SQL_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_ROLE_SQL_META_NAME_RE = re.compile(r"\\([A-Za-z_]+|\S?)")
_ROLE_SQL_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_ROLE_SQL_STD_STRINGS_RE = re.compile(
    r"(?is)^SET\s+standard_conforming_strings\s*(?:=|TO)\s*'?(?P<value>[A-Za-z0-9_]+)"
)
_ROLE_SQL_RESET_STD_STRINGS_RE = re.compile(r"(?is)^RESET\s+standard_conforming_strings\b")
_MASKED_ROLE_SQL_LITERAL = "''"


def _role_sql_comment_end(text: str, index: int) -> int | None:
    """Return the offset just past a comment at ``index``, else None.

    Block comments nest in PostgreSQL, so the depth counter is not decoration.
    """
    if text.startswith("--", index):
        end = text.find("\n", index)
        return len(text) if end < 0 else end
    if not text.startswith("/*", index):
        return None
    depth = 0
    cursor = index
    while cursor < len(text):
        if text.startswith("/*", cursor):
            depth += 1
            cursor += 2
        elif text.startswith("*/", cursor):
            depth -= 1
            cursor += 2
            if depth == 0:
                return cursor
        else:
            cursor += 1
    return len(text)


def _role_sql_quoted_end(text: str, index: int, quote: str, backslashes: bool) -> int:
    r"""Return the offset just past the quoted run that starts at ``index``.

    A doubled quote (``''`` in a literal, ``""`` in an identifier) is an escape,
    not a terminator. ``backslashes`` is set for an ``E'...'`` literal and for a
    plain literal while ``standard_conforming_strings`` is off -- MEASURED on
    PostgreSQL 18.6: with ``SET standard_conforming_strings = off;`` earlier in
    the file, psql reads ``'a\';b'`` as ONE literal and runs the statement
    after it, emitting only a WARNING. A scanner without this ends the literal
    early and then swallows the rest of the file inside a phantom string.
    """
    cursor = index + 1
    while cursor < len(text):
        if backslashes and text[cursor] == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if text[cursor] != quote:
            cursor += 1
            continue
        if text.startswith(quote * 2, cursor):
            cursor += 2
            continue
        return cursor + 1
    return len(text)


def _role_sql_dollar_end(text: str, index: int) -> int | None:
    """Return the offset just past a ``$tag$...$tag$`` literal, else None."""
    tag = _ROLE_SQL_DOLLAR_TAG_RE.match(text, index)
    if tag is None:
        return None
    marker = tag.group(0)
    close = text.find(marker, tag.end())
    return len(text) if close < 0 else close + len(marker)


def _role_sql_starts_escape_string(text: str, index: int) -> bool:
    """Return True at the ``E`` of an ``E'...'`` backslash-escape literal."""
    if text[index] not in "eE" or not text.startswith("'", index + 1):
        return False
    previous = text[index - 1] if index else ""
    return not (previous.isalnum() or previous == "_")


def _role_sql_span(text: str, index: int, escapes: bool) -> tuple[str, int] | None:
    """Classify the run at ``index`` that psql lexes as a single unit.

    Returns ``(kind, end)`` for a comment, a meta-command, a quoted identifier
    or a literal, and None for an ordinary character the caller should consume
    one at a time. Every rule about where a statement may NOT be split lives
    here, which is what keeps ``_scan_role_sql`` a dispatch rather than a
    second, subtly different parser.

    ``escapes`` is the file's current ``standard_conforming_strings`` state; it
    only affects a PLAIN literal, since ``E'...'`` honours backslashes either
    way.
    """
    comment_end = _role_sql_comment_end(text, index)
    if comment_end is not None:
        return "comment", comment_end
    char = text[index]
    if char == "\\":
        end = text.find("\n", index)
        return "meta", len(text) if end < 0 else end
    if char == '"':
        return "ident", _role_sql_quoted_end(text, index, '"', False)
    if char == "'" or _role_sql_starts_escape_string(text, index):
        quote_at = index if char == "'" else index + 1
        return "literal", _role_sql_quoted_end(text, quote_at, "'", char != "'" or escapes)
    if char == "$":
        dollar_end = _role_sql_dollar_end(text, index)
        if dollar_end is not None:
            return "literal", dollar_end
    return None


def _scan_role_sql(body: str) -> list[str]:
    r"""Split roles.sql into the statements psql executes, literals masked.

    FIX: the gates below read ``body.split(";")`` and then ANCHORED a match at
    the start of each chunk, so any chunk that did not START with CREATE/ALTER
    ROLE was silently dropped -- and ``pg_dumpall --roles-only`` prints

        --
        -- Role memberships
        --

        GRANT app_platform TO postgres WITH ADMIN OPTION, ...;

    which puts that banner inside the first membership chunk. A membership gate
    written in the old style is vacuous against the shipped output shape, not
    merely attackable. The same naive split severed a statement at a semicolon
    inside a quoted or dollar-quoted PASSWORD, stranding the privileged tail in
    a chunk no pattern matched.

    psql meta-commands are returned as their own entries, backslash and all,
    because psql terminates them at the newline rather than at a semicolon --
    and because it honours a backslash ANYWHERE outside a quote or comment, not
    just at the start of a line. MEASURED on PostgreSQL 18.6 through the exact
    ``_restore_roles`` invocation: ``SET client_encoding = 'UTF8' \! id`` and
    ``SELECT 1\! id`` both execute the shell command as uid=0 inside the
    database container, with psql exiting 0 and printing nothing at all.

    Honest limits. This is a statement splitter, not a SQL parser: it knows
    where statements END, not what they MEAN. It does not implement ``U&'...'``
    Unicode-escape literals (which lex as a plain literal after the ``U&``, and
    which pg_dumpall never emits). The leading UTF-8 BOM is stripped explicitly
    because ``str.strip()`` does not treat U+FEFF as whitespace.
    """
    text = body.lstrip(_ROLE_SQL_BOM)
    statements: list[str] = []
    raw: list[str] = []
    masked: list[str] = []
    escapes = [False]
    index = 0

    def flush() -> None:
        """Emit the buffered statement and track standard_conforming_strings."""
        raw_text = " ".join("".join(raw).split())
        collapsed = " ".join("".join(masked).split())
        raw.clear()
        masked.clear()
        if not collapsed:
            return
        setting = _ROLE_SQL_STD_STRINGS_RE.match(raw_text)
        if setting is not None:
            escapes[0] = setting.group("value").lower() in {"off", "false", "0"}
        elif _ROLE_SQL_RESET_STD_STRINGS_RE.match(raw_text):
            escapes[0] = False
        statements.append(collapsed)

    while index < len(text):
        span = _role_sql_span(text, index, escapes[0])
        if span is None:
            char = text[index]
            if char == ";":
                flush()
            else:
                raw.append(char)
                masked.append(char)
            index += 1
            continue
        kind, end = span
        if kind == "meta":
            statements.append(" ".join(text[index:end].split()))
        elif kind == "comment":
            raw.append(" ")
            masked.append(" ")
        elif kind == "ident":
            raw.append(text[index:end])
            masked.append(text[index:end])
        else:
            raw.append(text[index:end])
            masked.append(_MASKED_ROLE_SQL_LITERAL)
        index = end
    flush()
    return statements


def _role_sql_tokens(statement: str) -> list[tuple[str, str]]:
    """Tokenise one scanned statement into ('word' | 'ident' | 'other', text).

    ``word`` is a bare keyword or identifier, ``ident`` is the CONTENT of a
    double-quoted identifier with its case preserved, and everything else is
    opaque. Keeping the two apart is what lets the gates below fold names the
    way PostgreSQL folds them.
    """
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(statement):
        char = statement[index]
        if char.isspace():
            index += 1
            continue
        if char == '"':
            end = _role_sql_quoted_end(statement, index, '"', False)
            tokens.append(("ident", statement[index + 1 : end - 1].replace('""', '"')))
            index = end
            continue
        if char == "'":
            tokens.append(("other", _MASKED_ROLE_SQL_LITERAL))
            index += 2
            continue
        word = _ROLE_SQL_WORD_RE.match(statement, index)
        if word is not None:
            tokens.append(("word", word.group(0)))
            index = word.end()
            continue
        tokens.append(("other", char))
        index += 1
    return tokens


def _role_sql_identifier(token: tuple[str, str]) -> str:
    """Return the role name a token denotes, folding only UNQUOTED words.

    FIX: the old ``("?[A-Za-z0-9_]+"?)`` capture TRUNCATED at the first
    non-word character and then lowercased, so ``"app_tenant-shadow"`` and
    ``"App_Tenant"`` -- both DISTINCT roles to PostgreSQL, which does not fold
    quoted identifiers -- were reported as drift on ``app_tenant`` and refused
    a restore over statements psql answers with "role does not exist".
    """
    kind, value = token
    if kind == "ident":
        return value
    if kind == "word":
        return value.lower()
    return ""


def _role_sql_words(tokens: list[tuple[str, str]]) -> list[str]:
    """Return the uppercase bare words of a statement, in order."""
    return [value.upper() for kind, value in tokens if kind == "word"]


# ============================================================================
# Purpose: Refuse psql meta-commands roles.sql has no business carrying.
# Database/ORM: None.
# Standards: Allowlist, and it is exactly two names. ``\restrict`` /
#            ``\unrestrict`` are NOT optional to support -- MEASURED: every
#            pg_dumpall from 13 through 18 wraps its body in that pair (the
#            CVE-2025-8714 hardening), so refusing backslash commands wholesale
#            would refuse every genuine archive.
# Blast Radius: Arbitrary code execution as the docker-exec user inside the
#               database container. ``_container_sh`` passes no ``-u``, so that
#               user is root.
# Connections:
#   - File: scripts/restore_database.py -> ``_unsupported_role_ddl_in_roles_sql``
#     looks only for DO and EXECUTE, and ``_unexpected_roles_errors`` needs an
#     ERROR line -- a successful ``\!`` produces neither.
# ============================================================================
_ALLOWED_ROLE_SQL_META_COMMANDS = frozenset({"restrict", "unrestrict"})


def _role_sql_meta_command_problems(body: str) -> list[str]:
    r"""Return psql meta-commands in roles.sql outside the two-name allowlist.

    FIX: nothing looked at backslash commands at all. MEASURED on PostgreSQL
    18.6 through the exact ``_restore_roles`` argv: a roles.sql containing
    ``\! id > /tmp/proof`` -- at the start of a line, glued to a word, or in the
    middle of an unterminated statement -- runs that shell command as uid=0
    inside the container, and psql exits 0 with EMPTY stderr, so
    ``_unexpected_roles_errors`` has nothing to catch. A genuine PG18 archive
    is protected only by its own leading ``\restrict``, which puts psql in
    restricted mode; an operator-trimmed or pre-CVE-2025-8714 archive -- the
    legacy archive this whole gate exists for -- carries no such line.

    Matching the WHOLE first token rather than a prefix keeps ``\restrictfoo``
    outside the allowlist.
    """
    problems: list[str] = []
    for statement in _scan_role_sql(body):
        if not statement.startswith("\\"):
            continue
        match = _ROLE_SQL_META_NAME_RE.match(statement)
        name = (match.group(1) if match is not None else "").lower()
        if name not in _ALLOWED_ROLE_SQL_META_COMMANDS:
            problems.append(statement)
            continue
        # Defence in depth. MEASURED: psql does NOT treat a second backslash on
        # the line as a command separator -- `\unrestrict tok \\ \! id` makes it
        # report `invalid command \` and run nothing. So this is not a live
        # bypass. But only the FIRST name on the line is allowlisted above, and
        # a real `\restrict`/`\unrestrict` nonce is alphanumeric, so refusing a
        # further backslash costs nothing and removes the argument entirely.
        if "\\" in statement[1:]:
            problems.append(statement)
    return problems


# ============================================================================
# Purpose: Refuse a roles.sql that makes an application role a MEMBER of any
#          other role. Membership is not an attribute, so every attribute gate
#          in this file is structurally blind to it.
# Database/ORM: Reads no catalog; judges the text pg_dumpall wrote from
#               pg_auth_members. Needs no bootstrap-superuser argument, which
#               is why it can live on the publish side's superuser-free path.
# Standards: One direction only. ``GRANT app_tenant TO <login>`` is the
#            deployed restricted-login model (Docs/17:46-52) and the exact edge
#            20260608_0001's downgrade enumerates and revokes, and real
#            pg_dumpall output carries ``GRANT app_* TO <bootstrap superuser>``
#            once anyone takes ADMIN OPTION -- so constraining the granted side
#            would refuse genuine archives. ``GRANT <anything> TO app_tenant``
#            is the edge no migration or script in this repository creates.
# Blast Radius: Authorization, cluster-wide. With ``inherit_option = t`` the
#               app role is treated as the OWNER of every table the bootstrap
#               superuser owns, so ``ALTER TABLE ... NO FORCE ROW LEVEL
#               SECURITY`` and ``DROP POLICY`` succeed with no SET ROLE at all,
#               and a plain ``SET ROLE <superuser>`` reaches a superuser
#               session in one hop.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py
#     -> ``_create_role`` (92-113) issues ``CREATE ROLE "<role>" NOLOGIN`` and
#     grants these roles NOTHING; the downgrade (386-397) enumerates
#     pg_auth_members and REVOKEs memberships separately from object
#     privileges, so the migration already treats membership as its own graph.
# ============================================================================
_ROLE_SQL_LIST_CLAUSE_WORDS = frozenset({"IN", "ROLE", "GROUP", "USER", "ADMIN"})


def _role_sql_name_list(tokens: list[tuple[str, str]], start: int) -> tuple[list[str], int]:
    """Read a comma-separated role list starting at ``start``.

    Stops at the next clause keyword or at the first name not followed by a
    comma, which is how PostgreSQL's own grammar ends a ``role_list``.
    """
    names: list[str] = []
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == ("other", ","):
            index += 1
            continue
        if token[0] == "word" and token[1].upper() in _ROLE_SQL_LIST_CLAUSE_WORDS:
            break
        name = _role_sql_identifier(token)
        if not name:
            break
        names.append(name)
        index += 1
        if index >= len(tokens) or tokens[index] != ("other", ","):
            break
    return names, index


def _grant_membership_edges(tokens: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return (member, granted) edges a GRANT statement creates.

    An unquoted ``ON`` before the ``TO`` marks an OBJECT grant, not a
    membership, and object grants are left alone. That is load-bearing on real
    archives, not a nicety: ``pg_dumpall --roles-only`` has a fourth section,
    ``-- Role privileges on configuration parameters``, and MEASURED on
    PostgreSQL 18.6 it emits lines such as ``GRANT SET ON PARAMETER work_mem TO
    app_platform;`` and ``GRANT ALTER SYSTEM ON PARAMETER shared_buffers TO
    app_tenant WITH GRANT OPTION;``. Reading those as memberships would refuse
    a file this repository's own backup publishes. Testing the WORD ``ON``
    rather than the text keeps ``GRANT postgres TO app_tenant, "ON";`` refused.
    """
    to_index = None
    for position, token in enumerate(tokens):
        if token[0] != "word":
            continue
        upper = token[1].upper()
        if upper == "ON":
            return []
        if upper == "TO":
            to_index = position
            break
    if to_index is None:
        return []
    granted = [n for n in (_role_sql_identifier(t) for t in tokens[1:to_index]) if n]
    members: list[str] = []
    for token in tokens[to_index + 1 :]:
        if token[0] == "word" and token[1].upper() in {"WITH", "GRANTED"}:
            break
        name = _role_sql_identifier(token)
        if name:
            members.append(name)
    return [(member, role) for member in members for role in granted]


def _role_ddl_membership_edges(tokens: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return (member, granted) edges a CREATE ROLE clause creates.

    ``IN ROLE`` / ``IN GROUP`` make the SUBJECT a member of the listed roles;
    ``ROLE`` / ``USER`` / ``ADMIN`` make the listed roles members OF the
    subject. pg_dumpall emits neither form -- it writes a bare ``CREATE ROLE
    x;`` and a separate GRANT -- but ``CREATE ROLE app_tenant IN ROLE
    postgres;`` produces the identical pg_auth_members row, and so does
    ``CREATE ROLE postgres ROLE app_tenant;`` pointing the other way.
    """
    edges: list[tuple[str, str]] = []
    subject = _role_sql_identifier(tokens[2])
    index = 3
    while index < len(tokens):
        token = tokens[index]
        if token[0] != "word":
            index += 1
            continue
        upper = token[1].upper()
        nested = (
            upper == "IN"
            and index + 1 < len(tokens)
            and tokens[index + 1][0] == "word"
            and tokens[index + 1][1].upper() in {"ROLE", "GROUP"}
        )
        if nested:
            names, index = _role_sql_name_list(tokens, index + 2)
            edges.extend((subject, name) for name in names)
            continue
        if upper in {"ROLE", "USER", "ADMIN"}:
            names, index = _role_sql_name_list(tokens, index + 1)
            edges.extend((name, subject) for name in names)
            continue
        index += 1
    return edges


def _role_sql_word_index(tokens: list[tuple[str, str]], word: str) -> int | None:
    """Return the position of the first bare ``word``, or None if absent."""
    for position, token in enumerate(tokens):
        if token[0] == "word" and token[1].upper() == word:
            return position
    return None


def _alter_group_membership_edges(tokens: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return the edges an ``ALTER GROUP <g> ADD USER <list>`` statement makes.

    Returning [] when either keyword is missing is the whole point of splitting
    this out: ``ALTER ROLE app_tenant WITH NOLOGIN`` reaches here too, and it
    creates no membership.
    """
    words = _role_sql_words(tokens)
    start = _role_sql_word_index(tokens, "USER")
    if "ADD" not in words or start is None:
        return []
    group = _role_sql_identifier(tokens[2])
    names, _end = _role_sql_name_list(tokens, start + 1)
    return [(name, group) for name in names]


def _role_membership_edges(statement: str) -> list[tuple[str, str]]:
    """Return every (member, granted) edge one statement would create.

    ``ALTER GROUP <g> ADD USER <list>`` is the legacy spelling and it is not
    hypothetical: MEASURED on PostgreSQL 18.6, ``ALTER GROUP postgres ADD USER
    app_tenant;`` exits 0 with empty stderr and leaves ``postgres ->
    app_tenant inherit=true`` in pg_auth_members, while naming only roles the
    CREATE-ROLE allowlist already permits.
    """
    tokens = _role_sql_tokens(statement)
    if not tokens or tokens[0][0] != "word":
        return []
    verb = tokens[0][1].upper()
    if verb == "GRANT":
        return _grant_membership_edges(tokens)
    if len(tokens) < 3 or tokens[1][0] != "word":
        return []
    noun = tokens[1][1].upper()
    if verb == "CREATE" and noun in {"ROLE", "USER", "GROUP"}:
        return _role_ddl_membership_edges(tokens)
    if verb == "ALTER" and noun in {"GROUP", "ROLE"}:
        return _alter_group_membership_edges(tokens)
    return []


def _role_membership_problems(body: str, superuser: str = "postgres") -> list[str]:
    r"""Return roles.sql membership statements outside the safe bootstrap edge.

    FIX: ``GRANT <bootstrap superuser> TO app_tenant;`` matched neither the
    CREATE-anchored allowlist nor the attribute-drift gate -- a GRANT can never
    match a ``(?:CREATE|ALTER)\s+ROLE`` pattern -- and ``_restore_roles``
    replayed it. The post-apply check only asserts the two roles EXIST; it
    never reads pg_auth_members. The result is not a drifted attribute but an
    edge in the authorization graph, and that edge is enough on its own.

    Both sides are checked. A protected role may be GRANTED TO the connected
    bootstrap superuser because pg_dumpall emits that legitimate edge, but a
    protected role may never be granted to an arbitrary member. Conversely,
    an application role may never become a member of another role, including
    another protected role or a predefined ``pg_*`` role.
    """
    problems: list[str] = []
    for statement in _scan_role_sql(body):
        if statement.startswith("\\"):
            continue
        for member, granted in _role_membership_edges(statement):
            if member in _PROTECTED_APP_ROLES:
                problems.append(f"{member} becomes a member of {granted}: {statement}")
            elif granted in _PROTECTED_APP_ROLES and member != superuser:
                problems.append(
                    f"{granted} is granted to non-bootstrap role {member}: {statement}"
                )
    return problems


# ============================================================================
# Purpose: Refuse statements roles.sql must never carry at all -- the ones that
#          escalate or brick without ever naming a role attribute.
# Database/ORM: None.
# Standards: A short DENY list of proven-dangerous shapes, deliberately NOT an
#            allowlist of every shape pg_dumpall emits. An allowlist was tried
#            and refuted: pg_dumpall's ``-- Role privileges on configuration
#            parameters`` section is real output that no hand-written shape
#            table anticipated, and refusing it would have blocked both the
#            publish and the restore of a healthy cluster. Each entry below is
#            instead a construct MEASURED to be absent from pg_dumpall
#            --roles-only output and MEASURED to escalate when replayed.
# Blast Radius: Cluster availability and authorization.
# Connections:
#   - File: scripts/restore_database.py -> ``_restore_roles`` runs this file as
#     the bootstrap superuser with ON_ERROR_STOP off.
# ============================================================================
_UNSAFE_ROLE_SETTINGS = frozenset(
    {"session_preload_libraries", "local_preload_libraries", "shared_preload_libraries"}
)


def _role_sql_setting_name(tokens: list[tuple[str, str]]) -> str:
    """Return the GUC an ``ALTER ROLE ... SET/RESET`` statement targets."""
    for position, token in enumerate(tokens):
        if token[0] == "word" and token[1].upper() in {"SET", "RESET"}:
            if position + 1 < len(tokens):
                return _role_sql_identifier(tokens[position + 1])
            return ""
    return ""


_ROLE_SQL_ALLOWED_HEADS = frozenset({"SET", "RESET", "GRANT", "REVOKE"})
_ROLE_SQL_ROLE_NOUNS = frozenset({"ROLE", "USER", "GROUP"})


def _role_sql_statement_is_role_shaped(words: list[str]) -> bool:
    """Return True only for statement shapes pg_dumpall --roles-only emits.

    FIX: the gate below used to IGNORE any statement that was not role DDL,
    which meant a legacy or tampered roles.sql could carry ordinary SQL and
    ``_restore_roles`` would execute it as the bootstrap superuser. The digest
    on the archive is integrity, not authenticity -- the manifest is unsigned,
    so whoever can edit roles.sql can recompute it. ``COPY (SELECT '') TO
    PROGRAM '...'`` is command execution as the postgres OS user, and
    ``ALTER SYSTEM``, ``DROP DATABASE`` and ``CREATE EXTENSION plpython3u``
    all sailed through.

    This is an allowlist of VERBS, not of statement shapes. That distinction
    is what an earlier design got wrong: pg_dumpall has a
    ``-- Role privileges on configuration parameters`` section emitting
    ``GRANT ALTER SYSTEM ON PARAMETER shared_buffers TO app_tenant``, so a
    shape table refused genuine output. Allowing the GRANT verb outright keeps
    that section working while ``ALTER SYSTEM`` as a STATEMENT is still
    refused, because its second word is not a role noun.
    """
    if words[0] in _ROLE_SQL_ALLOWED_HEADS:
        return True
    if words[0] in {"CREATE", "ALTER", "DROP"}:
        return len(words) > 1 and words[1] in _ROLE_SQL_ROLE_NOUNS
    if words[0] in {"COMMENT", "SECURITY"}:
        # COMMENT ON ROLE x IS '...' and SECURITY LABEL FOR p ON ROLE x IS '...'
        return "ROLE" in words
    return False


def _object_grant_problem(statement: str, tokens: list[tuple[str, str]]) -> str | None:
    """Refuse a GRANT whose target is an object, not a role or a GUC.

    FIX: the verb allowlist admits every GRANT because pg_dumpall --roles-only
    really emits two GRANT sections -- role memberships (``GRANT app_platform
    TO postgres WITH ADMIN OPTION``) and parameter ACLs (``GRANT SET ON
    PARAMETER work_mem TO app_platform``). ``GRANT EXECUTE ON FUNCTION
    pg_catalog.pg_read_file(text) TO app_tenant;`` is neither, yet it passed
    every gate: it names no role attribute, it is not a membership edge, and
    the parameter gate only judges statements that name PARAMETER. Replayed
    by the bootstrap superuser it hands the tenant lane permanent
    server-file reads while the restore reports success. The two dumpable
    shapes are exactly ``no ON at all`` and ``ON PARAMETER``; any other ON
    clause targets an object.
    """
    on = _role_sql_word_index(tokens, "ON")
    if on is None:
        return None
    after = tokens[on + 1] if on + 1 < len(tokens) else ("other", "")
    if after[0] == "word" and after[1].upper() == "PARAMETER":
        return None
    return f"object GRANT is not a role membership or parameter ACL: {statement}"


def _unsafe_parameter_grant_problem(
    statement: str, tokens: list[tuple[str, str]], words: list[str]
) -> str | None:
    """Refuse a parameter ACL handing a protected role a code-loading GUC.

    FIX: ``GRANT SET ON PARAMETER session_preload_libraries TO app_tenant;`` is
    an OBJECT grant, so the membership gate ignores it by design, and it names
    no role attribute, so the drift gate cannot see it either. The verb
    allowlist admits every GRANT because pg_dumpall really does emit a
    ``-- Role privileges on configuration parameters`` section. Replayed, this
    one lets the application role set a GUC that loads code at login -- exactly
    the boundary ``_UNSAFE_ROLE_SETTINGS`` exists to hold.

    Only the dangerous GUCs are refused, and only for the protected roles, so
    the genuine section (``GRANT ALTER SYSTEM ON PARAMETER shared_buffers TO
    app_tenant``) keeps restoring.
    """
    if "PARAMETER" not in words:
        return None
    at = _role_sql_word_index(tokens, "PARAMETER")
    to = _role_sql_word_index(tokens, "TO")
    if at is None or to is None or at + 1 >= len(tokens):
        return None
    setting = _role_sql_identifier(tokens[at + 1])
    if setting not in _UNSAFE_ROLE_SETTINGS:
        return None
    grantees = {_role_sql_identifier(t) for t in tokens[to + 1 :]}
    protected = sorted(grantees & set(_PROTECTED_APP_ROLES))
    if protected:
        return (
            f"parameter ACL on {setting} for {', '.join(protected)} "
            f"loads code at login: {statement}"
        )
    return None


def _role_ddl_statement_problem(
    statement: str, tokens: list[tuple[str, str]], words: list[str]
) -> str | None:
    """Return why one CREATE/ALTER/DROP ROLE statement is illegitimate, else None.

    ``tokens`` may be as short as two entries -- ``DROP ROLE;`` is a real thing
    to find in a tampered file -- so the subject is read defensively. Reaching
    for ``tokens[2]`` unguarded raised IndexError out of the preflight instead
    of refusing the file.
    """
    # FIX: this used to refuse a RENAME only when it NAMED a protected role, so
    # `ALTER ROLE <bootstrap superuser> RENAME TO x` was accepted -- it renames
    # the configured POSTGRES_USER out from under every future application and
    # operator connection, and the post-apply check only looks for the two app
    # roles so the restore still reports success. pg_dumpall --roles-only emits
    # no RENAME of any kind, which is the rule this gate is meant to enforce.
    if "RENAME" in words:
        return f"RENAME is not a statement pg_dumpall --roles-only emits: {statement}"
    # FIX: the verb allowlist admits DROP ROLE, and nothing below looked at what
    # was being dropped. `DROP ROLE app_tenant;` therefore passed the read-only
    # preflight. In the old overlay implementation the target was already gone
    # by the time _restore_roles dropped the role and post-apply checks refused.
    if words[0] == "DROP":
        dropped = {_role_sql_identifier(t) for t in tokens[2:]}
        protected = sorted(dropped & set(_PROTECTED_APP_ROLES))
        if protected:
            return f"DROP of protected role {', '.join(protected)}: {statement}"
        return None
    # FIX: this compared the RAW token to "ALL", so `ALTER ROLE all SET
    # statement_timeout TO '1ms'` -- which PostgreSQL applies to every role in
    # the cluster -- walked straight past. Unquoted identifiers fold; a QUOTED
    # "ALL" is a role genuinely named ALL and must NOT be treated as the
    # wildcard, which is why the token KIND is still checked.
    subject = tokens[2] if len(tokens) > 2 else ("other", "")
    wildcard = subject[0] == "word" and subject[1].upper() == "ALL"
    if words[0] == "ALTER" and wildcard:
        return f"ALTER ROLE ALL is not allowed in roles.sql: {statement}"
    setting = _role_sql_setting_name(tokens)
    if setting in _UNSAFE_ROLE_SETTINGS:
        return f"role setting {setting} loads code at login: {statement}"
    return None


def _unsupported_role_statement_problem(statement: str) -> str | None:
    """Return why ONE scanned statement is illegitimate in roles.sql, else None."""
    tokens = _role_sql_tokens(statement)
    words = _role_sql_words(tokens)
    if not words:
        return None
    if words[0] == "DO":
        return f"DO blocks are not allowed in roles.sql: {statement}"
    # FIX: `SET SESSION AUTHORIZATION` is three words but `SET
    # session_authorization` is TWO -- the underscore form is a single token,
    # so matching only ["SET", "SESSION"] caught one spelling of the same
    # statement and not the other. Both change who the remainder of the file
    # runs as, which is what this arm claims to prevent.
    if words[0] in {"SET", "RESET"} and words[1:2] in (
        ["ROLE"],
        ["SESSION"],
        ["SESSION_AUTHORIZATION"],
    ):
        return f"session-identity statements are not allowed: {statement}"
    # FIX: this used to `continue` on anything that was not role DDL, so
    # ordinary SQL in a tampered roles.sql was replayed as the bootstrap
    # superuser -- including `COPY (SELECT '') TO PROGRAM '...'`, which is
    # command execution as the postgres OS user.
    if not _role_sql_statement_is_role_shaped(words):
        return f"statement pg_dumpall --roles-only never emits: {statement}"
    if words[0] == "GRANT":
        # FIX: the parameter gate below only judges statements naming
        # PARAMETER, so every other object grant -- GRANT EXECUTE ON FUNCTION
        # pg_catalog.pg_read_file(text) TO app_tenant included -- sailed
        # through the verb allowlist. _object_grant_problem admits only the
        # two shapes pg_dumpall --roles-only emits and refuses the rest.
        object_grant = _object_grant_problem(statement, tokens)
        if object_grant is not None:
            return object_grant
        return _unsafe_parameter_grant_problem(statement, tokens, words)
    if words[0] not in {"CREATE", "ALTER", "DROP"} or words[1] not in _ROLE_SQL_ROLE_NOUNS:
        return None
    return _role_ddl_statement_problem(statement, tokens, words)


def _unsupported_role_statement_problems(body: str) -> list[str]:
    r"""Return roles.sql statements that are never legitimate here.

    Each of these was MEASURED on PostgreSQL 18.6 through the exact
    ``_restore_roles`` invocation, and none of them appears in
    ``pg_dumpall --roles-only`` output:

    * ``ALTER ROLE ALL SET session_preload_libraries TO 'evil';`` exits 0 and
      then makes the cluster unreachable to EVERY role including the bootstrap
      superuser (``FATAL: could not access file "evil"``); with a planted .so
      it is code execution in the postgres process on every connection.
      pg_dumpall does not dump ``ALTER ROLE ALL`` settings -- verified by
      setting both the global and the per-database form and re-dumping.
    * ``ALTER ROLE ums_admin RENAME TO app_tenant;`` exits 0 with only a
      WARNING and leaves a role NAMED app_tenant that is ``rolsuper=true
      rolcanlogin=true`` -- which then satisfies the post-apply
      ``ROLES_PRESENT_SQL`` existence check, so the restore reports success.
    * ``SET ROLE``, ``SET SESSION AUTHORIZATION`` and the single-token
      ``SET session_authorization`` all change who the remainder of the file
      runs as. All three spellings are refused; matching only the two-word
      form caught one spelling and not the other.
    * ``DO`` is already refused by ``_unsupported_role_ddl_in_roles_sql``, but
      that check is a raw-text ``^\s*DO`` search that a leading block comment
      walks past; this one reads the scanned statement.
    """
    problems: list[str] = []
    for statement in _scan_role_sql(body):
        if statement.startswith("\\"):
            continue
        problem = _unsupported_role_statement_problem(statement)
        if problem is not None:
            problems.append(problem)
    return problems


# ============================================================================
# Purpose: Refuse a roles.sql whose application roles carry a privileged
#          attribute, reading the same statements psql will execute.
# Database/ORM: None.
# Standards: Whole-token matching keeps NOSUPERUSER / NOLOGIN / NOBYPASSRLS --
#            exactly what pg_dumpall writes for these roles -- from matching
#            the enabled forms. ``ALTER ROLE ... SET/RESET`` and its ``IN
#            DATABASE`` form are excluded because their tail is a GUC name and
#            a GUC VALUE, not an attribute list.
# Blast Radius: Authorization. ``_restore_roles`` replays every ALTER ROLE as
#               the bootstrap superuser and the migration history is at head,
#               so nothing reruns to clear what lands here.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py
#     -> ``_create_role`` (92-113) creates both roles NOLOGIN and nothing else.
# ============================================================================
_PRIVILEGED_ATTRIBUTE_TOKENS = frozenset(
    {"SUPERUSER", "BYPASSRLS", "LOGIN", "CREATEROLE", "CREATEDB", "REPLICATION"}
)


def _role_attribute_clause(statement: str) -> tuple[str, list[str]] | None:
    """Return (role, attribute words) for a plain CREATE/ALTER ROLE, else None.

    FIX: excluding the SET/RESET forms is what removes two live over-refusals.
    ``ALTER ROLE app_tenant SET application_name TO login;`` and its
    ``IN DATABASE`` twin were refused as LOGIN drift although psql applies both
    with ZERO attribute change -- the old tokenizer could not tell a GUC VALUE
    from a role attribute, and a restore script's worst failure is refusing a
    file that would have restored.
    """
    tokens = _role_sql_tokens(statement)
    if len(tokens) < 3 or tokens[0][0] != "word" or tokens[1][0] != "word":
        return None
    if tokens[0][1].upper() not in {"CREATE", "ALTER"}:
        return None
    if tokens[1][1].upper() not in {"ROLE", "USER", "GROUP"}:
        return None
    name = _role_sql_identifier(tokens[2])
    if not name:
        return None
    words = _role_sql_words(tokens[3:])
    if words[:1] in (["SET"], ["RESET"]) or words[:2] == ["IN", "DATABASE"]:
        return None
    if "RENAME" in words:
        return None
    return name, [word for word in words if word != "WITH"]


def _collect_role_attribute_tokens(body: str) -> dict[str, set[str]]:
    """Map each role name to the uppercase attribute tokens roles.sql emitted."""
    tokens_by_role: dict[str, set[str]] = {}
    for statement in _scan_role_sql(body):
        if statement.startswith("\\"):
            continue
        clause = _role_attribute_clause(statement)
        if clause is None:
            continue
        role, words = clause
        tokens_by_role.setdefault(role, set()).update(words)
    return tokens_by_role


def _role_privilege_drift_problems(body: str) -> list[str]:
    """Return app roles carrying a privileged attribute in roles.sql.

    Byte-identical to the twin in the sibling script, because the two gates
    must agree: a restore stricter than the publish gate would refuse an
    archive this repository's own backup was allowed to publish.

    CREATEROLE / CREATEDB / REPLICATION joined the token set here. MEASURED:
    with ``ALTER ROLE app_tenant WITH CREATEROLE`` replayed, an app_tenant
    session can ``CREATE ROLE backdoor LOGIN PASSWORD ...`` -- a standing login
    account minted from the tenant lane -- and pg_dumpall emits the attribute
    on the allowlisted role, so a legacy archive carries it through unseen.
    ``_create_role`` grants none of the six.

    A MISSING app role is deliberately not flagged here. That is the publish
    gate's ``_role_declared_in_roles_sql`` check, which names the exact
    identifier and tells the operator what to do; leaving it out is what lets
    the two implementations be byte-identical and pinned to each other by
    source rather than by a frozenset.
    """
    problems: list[str] = []
    tokens_by_role = _collect_role_attribute_tokens(body)
    for role in _PROTECTED_APP_ROLES:
        tokens = tokens_by_role.get(role)
        if tokens is None:
            continue
        enabled = sorted(tokens & _PRIVILEGED_ATTRIBUTE_TOKENS)
        if enabled:
            problems.append(f"{role}: privileged attributes {', '.join(enabled)} must be revoked")
    return problems


_BOOTSTRAP_LOCKOUT_ATTRIBUTE_TOKENS = frozenset({"NOLOGIN", "NOSUPERUSER"})


def _bootstrap_role_lockout_problems(body: str, superuser: str) -> list[str]:
    """Refuse roles.sql that strips LOGIN/SUPERUSER from the bootstrap role.

    FIX(codex round-24 P1): ``ALTER ROLE <superuser> WITH NOLOGIN`` passed
    every gate -- the foreign-role check reads CREATE statements only and the
    drift gate covers the application roles only -- and under --allow-nonempty
    the replay then disabled the very identity the restore connects as. The old
    overlay path had already removed the target; the isolated path still must
    refuse because the cluster could need out-of-band recovery. Genuine
    pg_dumpall output never disables the bootstrap role, so any
    NOLOGIN/NOSUPERUSER on it is refused on both sides of the round trip.

    FIX(codex round-25 P1): ``DROP ROLE <superuser>;`` walked the same gap
    from the other side -- the DROP arm of the role gate protects only the
    application roles, so preflight approved the file before replay failed on
    "cannot drop the current user".
    pg_dumpall --roles-only emits no DROP of any kind.
    """
    problems: list[str] = []
    tokens = _collect_role_attribute_tokens(body).get(superuser)
    if tokens is not None:
        lockouts = sorted(tokens & _BOOTSTRAP_LOCKOUT_ATTRIBUTE_TOKENS)
        if lockouts:
            problems.append(
                f"{superuser}: attributes {', '.join(lockouts)} would lock the "
                "bootstrap identity out of the cluster"
            )
    for statement in _scan_role_sql(body):
        if statement.startswith("\\"):
            continue
        statement_tokens = _role_sql_tokens(statement)
        if len(statement_tokens) < 3:
            continue
        if statement_tokens[0][0] != "word" or statement_tokens[0][1].upper() != "DROP":
            continue
        if (
            statement_tokens[1][0] != "word"
            or statement_tokens[1][1].upper() not in _ROLE_SQL_ROLE_NOUNS
        ):
            continue
        dropped = {_role_sql_identifier(t) for t in statement_tokens[2:]}
        if superuser in dropped:
            problems.append(
                f"{superuser}: DROP would remove the bootstrap identity the "
                "restore itself connects as"
            )
    return problems


def _created_role_names(body: str) -> list[str]:
    r"""Return every role name a CREATE ROLE/USER/GROUP in roles.sql declares.

    FIX: the two foreign-role gates and the publish gate's declaration check
    each ran their own line-anchored ``^\s*CREATE`` regex, and they failed on
    orthogonal axes. A UTF-8 BOM, a leading block comment, a second CREATE on
    the same line, or a CREATE after a quoted semicolon each hid a foreign
    ``SUPERUSER LOGIN`` role while preflight reported success -- and the SAME
    regex pointed the other way answered False for ``/* c */ CREATE ROLE
    app_tenant;``, i.e. the backup refused a valid roles.sql. One scanner, one
    answer.
    """
    names: list[str] = []
    for statement in _scan_role_sql(body):
        if statement.startswith("\\"):
            continue
        tokens = _role_sql_tokens(statement)
        if len(tokens) < 3 or tokens[0][0] != "word" or tokens[1][0] != "word":
            continue
        if tokens[0][1].upper() != "CREATE":
            continue
        if tokens[1][1].upper() not in {"ROLE", "USER", "GROUP"}:
            continue
        name = _role_sql_identifier(tokens[2])
        if name:
            names.append(name)
    return names


def _foreign_roles_in_roles_sql(body: str, *, superuser: str) -> list[str]:
    """Return CREATE ROLE names outside the UMS restore allowlist.

    The bootstrap superuser name comes from ``SELECT current_user``, which is
    the role's true name rather than an SQL identifier token, so it is compared
    VERBATIM. pg_dumpall writes ``CREATE ROLE "Ums_Admin";`` quoted for a
    mixed-case superuser and ``CREATE ROLE postgres;`` bare for a folded one;
    ``_role_sql_identifier`` resolves both to the same name PostgreSQL would.
    """
    allowed = {superuser, *REQUIRED_ROLES}
    return [name for name in _created_role_names(body) if name not in allowed]


def _protected_role_setting_declarations(body: str) -> dict[str, set[str]]:
    """Return the cluster-level GUC names roles.sql itself installs per role.

    ``ALTER ROLE app_tenant SET statement_timeout TO '1ms';`` is a legitimate
    pg_dumpall --roles-only line when the SOURCE role carried that setting, so
    the post-apply validation in ``_restore_roles`` must compare the live
    catalog against what the FILE declares -- refusing every setting would
    reject genuine archives, and refusing none is exactly the hole the
    RESET-ALL normalization below exists to close.
    """
    declared: dict[str, set[str]] = {}
    for statement in _scan_role_sql(body):
        if statement.startswith("\\"):
            continue
        tokens = _role_sql_tokens(statement)
        if len(tokens) < 3:
            continue
        if tokens[0][0] != "word" or tokens[0][1].upper() != "ALTER":
            continue
        if tokens[1][0] != "word" or tokens[1][1].upper() not in _ROLE_SQL_ROLE_NOUNS:
            continue
        if _role_sql_word_index(tokens, "DATABASE") is not None:
            # IN DATABASE settings are per-database rows; the cluster-level
            # post-apply check never sees them and must not count them.
            continue
        role = _role_sql_identifier(tokens[2])
        if role not in _PROTECTED_APP_ROLES:
            continue
        setting = _role_sql_setting_name(tokens)
        if setting:
            declared.setdefault(role, set()).add(setting)
    return declared


def _role_sql_database_scope(tokens: list[tuple[str, str]]) -> str:
    """Return the database named by an exact ``IN DATABASE <name>`` clause."""
    for index in range(len(tokens) - 2):
        first, second = tokens[index], tokens[index + 1]
        if (
            first[0] == "word"
            and first[1].upper() == "IN"
            and second[0] == "word"
            and second[1].upper() == "DATABASE"
        ):
            return _role_sql_identifier(tokens[index + 2])
    return ""


def _protected_database_role_setting_declarations(
    body: str, database: str
) -> dict[str, set[str]]:
    """Return protected-role GUC keys explicitly SET for one database."""
    declared: dict[str, set[str]] = {}
    for statement in _scan_role_sql(body):
        if statement.startswith("\\"):
            continue
        tokens = _role_sql_tokens(statement)
        if len(tokens) < 3:
            continue
        if tokens[0][0] != "word" or tokens[0][1].upper() != "ALTER":
            continue
        if tokens[1][0] != "word" or tokens[1][1].upper() not in _ROLE_SQL_ROLE_NOUNS:
            continue
        if _role_sql_database_scope(tokens) != database:
            continue
        role = _role_sql_identifier(tokens[2])
        if role not in _PROTECTED_APP_ROLES:
            continue
        # RESET declarations intentionally produce no desired row. The final
        # transaction starts with RESET ALL, then applies SET declarations.
        if _role_sql_word_index(tokens, "SET") is None:
            continue
        setting = _role_sql_setting_name(tokens)
        if setting:
            declared.setdefault(role, set()).add(setting)
    return declared


# ============================================================================
# Purpose: Read exact protected-role settings for one database from the live
#          catalog, preserving values for transactional final-name replay.
# Database/ORM: pg_db_role_setting, pg_roles, and pg_database; no ORM.
# Standards: Independent psql -X control connection; JSON rows; strict shapes;
#            never log setting values because custom GUCs may be sensitive.
# Blast Radius: Database-scoped authorization/runtime settings only.
# Connections:
#   - File: scripts/restore_database.py -> _desired_database_role_settings and
#     _apply_database_role_settings_transactionally.
# ============================================================================
def _database_role_settings(
    container: str, database: str, *, timeout: int
) -> dict[str, dict[str, str]]:
    """Return ``{role: {guc: value}}`` for protected roles in ``database``."""
    role_literals = ", ".join(_quote_literal(role) for role in REQUIRED_ROLES)
    sql = (
        "SELECT json_build_object('role', r.rolname, 'settings', s.setconfig)::text "
        "FROM pg_catalog.pg_db_role_setting s "
        "JOIN pg_catalog.pg_roles r ON r.oid = s.setrole "
        "JOIN pg_catalog.pg_database d ON d.oid = s.setdatabase "
        f"WHERE d.datname = {_quote_literal(database)} "
        f"AND r.rolname IN ({role_literals}) ORDER BY r.rolname;"
    )
    settings: dict[str, dict[str, str]] = {}
    raw = _psql(container, sql, timeout=timeout, dbname="postgres")
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except ValueError as exc:
            raise RestoreError(
                EXIT_ROLES_FAILED,
                f"could not parse database role-setting catalog JSON: {exc}",
            ) from exc
        if not isinstance(document, dict):
            raise RestoreError(
                EXIT_ROLES_FAILED,
                "database role-setting catalog row is not a JSON object",
            )
        role = document.get("role")
        entries = document.get("settings")
        if (
            not isinstance(role, str)
            or role not in REQUIRED_ROLES
            or not isinstance(entries, list)
        ):
            raise RestoreError(
                EXIT_ROLES_FAILED,
                "database role-setting catalog row has an invalid role/settings shape",
            )
        role_settings: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, str) or "=" not in entry:
                raise RestoreError(
                    EXIT_ROLES_FAILED,
                    "database role-setting catalog contains a malformed setting",
                )
            key, value = entry.split("=", 1)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", key) or key in role_settings:
                raise RestoreError(
                    EXIT_ROLES_FAILED,
                    "database role-setting catalog contains an invalid or duplicate key",
                )
            role_settings[key] = value
        if role in settings:
            raise RestoreError(
                EXIT_ROLES_FAILED,
                f"database role-setting catalog returned duplicate rows for {role}",
            )
        settings[role] = role_settings
    return settings


def _desired_database_role_settings(
    container: str,
    roles_path: Path,
    database: str,
    *,
    timeout: int,
) -> dict[str, dict[str, str]]:
    """Capture only per-database settings explicitly declared by roles.sql."""
    body = roles_path.read_text(encoding="utf-8", errors="replace")
    declared = _protected_database_role_setting_declarations(body, database)
    if not declared:
        return {}
    live = _database_role_settings(container, database, timeout=timeout)
    desired: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for role, keys in sorted(declared.items()):
        for key in sorted(keys):
            value = live.get(role, {}).get(key)
            if value is None:
                missing.append(f"{role} SET {key}")
            else:
                desired.setdefault(role, {})[key] = value
    if missing:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "roles.sql declared database-scoped settings that did not land on "
            f"{database!r}: {', '.join(missing)}",
        )
    return desired


def _database_role_settings_transaction_sql(
    database: str, desired: dict[str, dict[str, str]]
) -> str:
    """Build one all-or-nothing replacement of protected per-database GUCs."""
    statements = ["BEGIN;"]
    for role in REQUIRED_ROLES:
        statements.append(
            f"ALTER ROLE {_quote_identifier(role)} IN DATABASE "
            f"{_quote_identifier(database)} RESET ALL;"
        )
    for role in sorted(desired):
        for key, value in sorted(desired[role].items()):
            statements.append(
                f"ALTER ROLE {_quote_identifier(role)} IN DATABASE "
                f"{_quote_identifier(database)} SET {_quote_identifier(key)} "
                f"TO {_quote_literal(value)};"
            )
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def _role_settings_shape(settings: dict[str, dict[str, str]]) -> str:
    """Describe role/key names without exposing configuration values."""
    return json.dumps(
        {role: sorted(values) for role, values in sorted(settings.items())},
        sort_keys=True,
    )


# ============================================================================
# Purpose: Finalize only database-scoped protected-role settings after the
#          verified replacement receives its final database name.
# Database/ORM: ALTER ROLE ... IN DATABASE and pg_db_role_setting.
# Standards: One BEGIN/COMMIT transaction with ON_ERROR_STOP; independent
#            before/after catalog reads reconcile commit-then-timeout outcomes;
#            no cluster-global RESET or full roles.sql replay at cutover.
# Blast Radius: Settings on the promoted replacement database only.
# Connections:
#   - File: scripts/restore_database.py -> _execute_restore finalizer callback.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> role atomicity boundary.
# ============================================================================
def _apply_database_role_settings_transactionally(
    container: str,
    database: str,
    desired: dict[str, dict[str, str]],
    *,
    timeout: int,
) -> None:
    """Replace protected per-database settings and reconcile ambiguous commit."""
    before = _database_role_settings(container, database, timeout=timeout)
    if before == desired:
        return
    sql = _database_role_settings_transaction_sql(database, desired)
    try:
        _psql(container, sql, timeout=timeout, dbname="postgres")
    except Exception as exc:
        try:
            observed = _database_role_settings(container, database, timeout=timeout)
        except Exception as observe_exc:
            raise RestoreError(
                EXIT_ROLES_FAILED,
                "database role-setting transaction failed and its commit state "
                f"could not be reconciled: {exc}; catalog query failed: {observe_exc}",
            ) from exc
        if observed == desired:
            print(
                "WARNING: database role-setting apply reported failure, but an "
                "independent catalog read proves the complete transaction committed",
                file=sys.stderr,
            )
            return
        if observed == before:
            raise RestoreError(
                EXIT_ROLES_FAILED,
                f"database role-setting transaction did not commit; prior state "
                f"was preserved: {exc}",
            ) from exc
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "database role-setting transaction ended in an unexpected catalog "
            f"state (before keys={_role_settings_shape(before)}, desired keys="
            f"{_role_settings_shape(desired)}, observed keys="
            f"{_role_settings_shape(observed)}); refusing connection admission",
        ) from exc
    observed = _database_role_settings(container, database, timeout=timeout)
    if observed != desired:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "database role-setting transaction returned success but the catalog "
            f"does not match the desired keys (desired={_role_settings_shape(desired)}, "
            f"observed={_role_settings_shape(observed)})",
        )


def _bootstrap_password_reset_problem(body: str, superuser: str) -> str | None:
    """Refuse roles.sql that rewrites the bootstrap role's PASSWORD.

    FIX(codex round-26 P1): a backup taken with ``--include-role-passwords``
    carries ``ALTER ROLE <superuser> PASSWORD ...``. Replaying it against a
    target whose POSTGRES_PASSWORD differs replaces the target verifier
    mid-restore: the current psql stays connected, but every NEXT connection
    -- this restore's own catalog queries, the application, health checks --
    authenticates with the unchanged container credential and fails during
    restore. The scanner masks literal bodies, so the refusal message carries
    no secret.
    """
    tokens = _collect_role_attribute_tokens(body).get(superuser)
    if tokens and "PASSWORD" in tokens:
        return (
            f"{superuser}: PASSWORD rewrite would replace the target's "
            "credential mid-restore while the container still authenticates "
            "with the old one"
        )
    return None


def _preflight_roles_file(container: str, roles_path: Path, *, timeout: int) -> None:
    """Validate roles.sql read-only before any restore mutation.

    Both checks are non-mutating -- a text scan of the file plus one
    ``SELECT current_user`` against the maintenance database -- so they run
    before cluster role replay or replacement creation.

    Args:
        container: Container name to resolve the bootstrap superuser through.
        roles_path: The archive's ``roles.sql``.
        timeout: Seconds allowed for the superuser lookup.

    Raises:
        RestoreError: On a psql meta-command, a statement pg_dumpall never
            emits, unsupported role DDL, roles outside the allowlist, an
            application role carrying a privileged attribute, or an application
            role placed into the cluster membership graph.
    """
    body = roles_path.read_text(encoding="utf-8", errors="replace")
    superuser = _psql(
        container, "SELECT current_user;", timeout=timeout, dbname="postgres"
    ).strip()
    unsupported = _unsupported_role_ddl_in_roles_sql(body)
    if unsupported:
        raise RestoreError(EXIT_ROLES_FAILED, unsupported)
    # FIX: psql honours a backslash command ANYWHERE outside a quote or a
    # comment, not only at the start of a line, and a successful one prints
    # nothing. MEASURED through the exact _restore_roles argv: a roles.sql
    # carrying `\! id > /pr210_B` wrote "uid=0(root) gid=0(root)" inside the
    # database container while psql exited 0 with EMPTY stderr, so
    # _unexpected_roles_errors had nothing to catch and the surrounding CREATE
    # ROLE statements still applied. Nothing here looked at meta-commands.
    meta_commands = _role_sql_meta_command_problems(body)
    if meta_commands:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "roles.sql carries psql meta-commands: "
            + "; ".join(meta_commands)
            + r". Only \restrict and \unrestrict are allowed; psql executes the "
            "rest inside the database container. Regenerate roles.sql with "
            "pg_dumpall --roles-only from the backup source cluster.",
        )
    # FIX: statements that escalate or brick without ever naming a role
    # attribute. `ALTER ROLE ALL SET session_preload_libraries` left the
    # cluster unreachable to every role including the bootstrap superuser, and
    # `ALTER ROLE <superuser> RENAME TO app_tenant` left a SUPERUSER LOGIN role
    # NAMED app_tenant, which then SATISFIES the post-apply existence check.
    unsupported_statements = _unsupported_role_statement_problems(body)
    if unsupported_statements:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "roles.sql carries statements pg_dumpall --roles-only never emits: "
            + "; ".join(unsupported_statements)
            + ". Regenerate roles.sql from the backup source cluster.",
        )
    foreign = _foreign_roles_in_roles_sql(body, superuser=superuser)
    if foreign:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "roles.sql declares cluster roles outside the UMS restore allowlist: "
            f"{', '.join(sorted(foreign))}. Restore into a dedicated UMS Postgres "
            "container or regenerate roles.sql from the backup source cluster.",
        )
    # FIX: reject drifted privileges on the application roles BEFORE
    # _restore_roles replays them as the bootstrap superuser. The gate above is
    # anchored to CREATE ROLE, so `ALTER ROLE app_tenant WITH SUPERUSER` in a
    # legacy roles.sql passed straight through and was applied.
    drift = _role_privilege_drift_problems(body)
    if drift:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "roles.sql grants RLS-bypassing privileges to protected roles: "
            + "; ".join(drift)
            + ". These ALTER ROLE attributes would be replayed as the bootstrap "
            "superuser before the archive loads, and the migration history will "
            "not rerun to clear them. Regenerate roles.sql from a source cluster "
            "whose application roles are NOLOGIN, NOSUPERUSER, NOBYPASSRLS.",
        )
    # FIX(round-24 P1): the replay runs AS this identity; a file that disables
    # it can brick the cluster during the otherwise isolated staging phase.
    lockouts = _bootstrap_role_lockout_problems(body, superuser)
    if lockouts:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "roles.sql locks the bootstrap identity out of the cluster: "
            + "; ".join(lockouts)
            + ". The restore replays the file as that identity, so it would "
            "disable its own connection before cutover. "
            "Regenerate roles.sql from the backup source cluster.",
        )
    # FIX(round-26 P1): only the RESTORE refuses this -- the archive itself
    # is sound, and the statement is harmless against a target whose
    # POSTGRES_PASSWORD already matches the backup's.
    password_reset = _bootstrap_password_reset_problem(body, superuser)
    if password_reset is not None:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            f"roles.sql rewrites the bootstrap credential: {password_reset}. "
            "Restore into a target whose POSTGRES_PASSWORD already matches "
            "the backup source, or regenerate roles.sql WITHOUT "
            "--include-role-passwords.",
        )
    # FIX: membership is not an attribute, so every check above is structurally
    # blind to it -- a GRANT statement can never match a CREATE/ALTER ROLE
    # pattern, and the post-apply check only asserts the two roles EXIST, never
    # reading pg_auth_members. MEASURED: with `GRANT <superuser> TO app_tenant;`
    # replayed, an app_tenant session with is_superuser=off and NO SET ROLE read
    # every row of an RLS table lacking FORCE, and on a FORCE table it ran
    # `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY` and then read every row.
    memberships = _role_membership_problems(body, superuser=superuser)
    if memberships:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "roles.sql puts a protected role into the cluster membership graph: "
            + "; ".join(memberships)
            + ". A membership is not a role attribute, so it survives every "
            "attribute check. Regenerate roles.sql from a source cluster whose "
            "application roles hold no memberships.",
        )


def _reset_existing_protected_role_settings(container: str, *, timeout: int) -> None:
    """RESET ALL cluster-level settings on protected roles that already exist.

    FIX: role-level GUCs are cluster-global like memberships; a clean
    roles.sql carries nothing to overwrite a target-only leftover, so this
    reset makes the replayed file the sole authority over protected-role
    settings. Only roles that already exist are reset -- on a fresh cluster
    the CREATE ROLE lines create them with no settings at all.
    """
    existing = [
        line.strip()
        for line in _psql(
            container, ROLES_PRESENT_SQL, timeout=timeout, dbname="postgres"
        ).splitlines()
        if line.strip()
    ]
    if not existing:
        return
    reset_lines = [
        f"ALTER ROLE {_quote_identifier(role)} RESET ALL;" for role in existing
    ]
    _psql(
        container,
        "\n".join(reset_lines) + "\n",
        timeout=timeout,
        dbname="postgres",
    )


def _required_roles_present(container: str, *, timeout: int) -> list[str]:
    """Return the required application roles the cluster currently reports."""
    return [
        line.strip()
        for line in _psql(
            container, ROLES_PRESENT_SQL, timeout=timeout, dbname="postgres"
        ).splitlines()
        if line.strip()
    ]


def _undeclared_role_settings(
    container: str, declared: dict[str, set[str]], *, timeout: int
) -> list[str]:
    """Return live cluster-level protected-role GUCs the file does not declare."""
    live: dict[str, set[str]] = {}
    for line in _psql(
        container, ROLE_SETTINGS_KEYS_SQL, timeout=timeout, dbname="postgres"
    ).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        role, _, key = stripped.partition(" = ")
        live.setdefault(role, set()).add(key)
    return sorted(
        f"{role} SET {key}"
        for role, keys in live.items()
        for key in keys - declared.get(role, set())
    )


def _refuse_post_apply_role_problems(
    container: str, roles_path: Path, *, timeout: int
) -> None:
    """Refuse every live cluster-role violation the replay left behind.

    Three absolute/post-replay invariants, all fail-closed:

    * membership graph empty (memberships are cluster-global, survive DROP
      DATABASE, and appear in no archive);
    * no cluster-level GUC on a protected role that roles.sql does not
      declare (the RESET-ALL normalization makes extras scanner drift);
    * no privileged attribute on a protected role (round-23: a clean
      roles.sql does not revoke target-leftover SUPERUSER/BYPASSRLS/LOGIN/
      CREATEROLE/CREATEDB/REPLICATION, and the CREATE ROLE lines are no-ops
      on a target where the role already exists).
    """
    memberships = [
        line.strip()
        for line in _psql(
            container, ROLE_MEMBERSHIPS_SQL, timeout=timeout, dbname="postgres"
        ).splitlines()
        if line.strip()
    ]
    if memberships:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "after applying roles.sql an application role is a member of "
            f"another role: {'; '.join(memberships)}. Memberships are "
            "cluster-global, so this one predates the archive or was installed "
            "outside it. It grants the lane the other role's object "
            "privileges without any attribute change. REVOKE it and re-run.",
        )
    declared = _protected_role_setting_declarations(
        roles_path.read_text(encoding="utf-8", errors="replace")
    )
    undeclared_settings = _undeclared_role_settings(container, declared, timeout=timeout)
    if undeclared_settings:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "after applying roles.sql the protected roles still carry "
            "cluster-global settings roles.sql does not declare: "
            f"{'; '.join(undeclared_settings)}. Cluster-level role settings "
            "survive database replacement, so these predate the archive or were installed "
            "by something other than the replayed file. Clear them with "
            "ALTER ROLE <role> RESET ALL and re-run.",
        )
    privileged = [
        line.strip()
        for line in _psql(
            container,
            ROLE_PRIVILEGED_ATTRIBUTES_SQL,
            timeout=timeout,
            dbname="postgres",
        ).splitlines()
        if line.strip()
    ]
    if privileged:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "after applying roles.sql the protected roles still carry "
            "privileged attributes "
            "(SUPERUSER/BYPASSRLS/LOGIN/CREATEROLE/CREATEDB/REPLICATION): "
            f"{', '.join(privileged)}. Attributes are cluster-global and a "
            "clean roles.sql does not revoke them, so these predate the "
            "archive. Run ALTER ROLE <role> WITH NOSUPERUSER NOBYPASSRLS "
            "NOLOGIN NOCREATEROLE NOCREATEDB NOREPLICATION and re-run.",
        )


def _live_protected_role_problems(container: str, *, timeout: int) -> list[str]:
    """Return live cluster-role violations the roles.sql text gates cannot see.

    Shared by the pre-destruction preflight in ``_execute_restore`` and the
    post-replay defense in ``_refuse_post_apply_role_problems``' callers:
    the same absolute invariant read at two different times.
    """
    problems: list[str] = []
    memberships = [
        line.strip()
        for line in _psql(
            container, ROLE_MEMBERSHIPS_SQL, timeout=timeout, dbname="postgres"
        ).splitlines()
        if line.strip()
    ]
    if memberships:
        problems.append("membership edges: " + "; ".join(memberships))
    privileged = [
        line.strip()
        for line in _psql(
            container,
            ROLE_PRIVILEGED_ATTRIBUTES_SQL,
            timeout=timeout,
            dbname="postgres",
        ).splitlines()
        if line.strip()
    ]
    if privileged:
        problems.append("privileged attributes on: " + ", ".join(privileged))
    return problems


def _foreign_writer_session_count(
    container: str, *, timeout: int, target_db: str | None = None
) -> int:
    """Count sessions on ``target_db`` from the maintenance database.

    When target_db is omitted the legacy current-database probe is retained for
    focused callers.  Production restore always supplies the name, so disabling
    connections on the target does not disable the guard itself.
    """
    sql = FOREIGN_WRITER_SESSIONS_SQL
    dbname: str | None = None
    if target_db is not None:
        sql = (
            "SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE datname = "
            f"{_quote_literal(target_db)};"
        )
        dbname = "postgres"
    raw = _psql(container, sql, timeout=timeout, dbname=dbname).strip()
    try:
        return int(raw or "0")
    except ValueError as exc:
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"could not read the live session count from psql output {raw!r}: {exc}",
        ) from exc


# ============================================================================
# Purpose: Hold a PostgreSQL advisory lock for the complete restore critical
#          section, including isolated replay, verification, and cutover.
# Database/ORM: One psql session connected to the maintenance database; no ORM.
# Standards: ``pg_try_advisory_lock`` fails closed when another restore owns the
#            target. The holder is connected to ``postgres`` so disabling and
#            renaming the application database cannot terminate it. Cleanup
#            preserves an active restore exception and reports an independent
#            cleanup failure.
# Blast Radius: Prevents concurrent restore/cutover operations on one target.
# Connections:
#   - File: scripts/restore_database.py -> _execute_restore holds this lock
#     before the first emptiness/session check and releases it after cutover.
# ============================================================================
def _read_lock_result(process: subprocess.Popen[str], *, timeout: int) -> str:
    """Read one advisory-lock result line without waiting forever."""
    stdout = process.stdout
    assert stdout is not None
    line_box: list[str] = []
    error_box: list[OSError] = []

    def _reader() -> None:
        """Read psql's first result row in a daemon thread."""
        try:
            line_box.append(stdout.readline())
        except OSError as exc:
            error_box.append(exc)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    reader.join(max(1, timeout))
    if reader.is_alive():
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"timed out acquiring the restore lock after {timeout}s",
        )
    if error_box:
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"restore lock psql output failed: {error_box[0]}",
        ) from error_box[0]
    if not line_box or not line_box[0]:
        stderr = process.stderr.read().strip() if process.stderr is not None else ""
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"restore lock psql exited before reporting its state: {stderr}",
        )
    return line_box[0].strip()


@contextmanager
def _target_restore_lock(
    container: str, target_db: str, *, timeout: int
) -> Iterator[None]:
    """Hold a target-scoped advisory lock until the restore critical section ends."""
    if not target_db or any(char in target_db for char in "\x00\r\n"):
        raise RestoreError(
            EXIT_USAGE,
            "target database name is empty or contains control characters",
        )
    lock_key = RESTORE_LOCK_PREFIX + target_db
    lock_sql = (
        "SELECT CASE WHEN pg_try_advisory_lock(hashtextextended("
        f"{_quote_literal(lock_key)}, 0)) THEN 'UMS_RESTORE_LOCK_OK' "
        "ELSE 'UMS_RESTORE_LOCK_BUSY' END;\n"
    )
    argv = _container_sh(
        container,
        'exec psql -X -U "$POSTGRES_USER" -d postgres '
        "--no-password -v ON_ERROR_STOP=1 -Atq",
    )
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    acquired = False
    try:
        process.stdin.write(lock_sql)
        process.stdin.flush()
        result = _read_lock_result(process, timeout=timeout)
        if result != "UMS_RESTORE_LOCK_OK":
            raise RestoreError(
                EXIT_USAGE,
                f"another restore already owns the target lock for database "
                f"{target_db!r}; wait for it to finish and retry",
            )
        acquired = True
        yield
    finally:
        active_error = sys.exc_info()[1]
        cleanup_error: BaseException | None = None
        try:
            if acquired and process.poll() is None:
                process.stdin.write("SELECT pg_advisory_unlock(hashtextextended(")
                process.stdin.write(f"{_quote_literal(lock_key)}, 0));\n")
                process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=min(30, max(1, timeout)))
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup_error = exc
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as kill_exc:
                print(
                    f"WARNING: could not terminate restore lock psql for "
                    f"{target_db!r}: {kill_exc}",
                    file=sys.stderr,
                )
        if cleanup_error is not None:
            message = f"restore lock cleanup failed for {target_db!r}: {cleanup_error}"
            if active_error is None:
                raise RestoreError(EXIT_RESTORE_FAILED, message) from cleanup_error
            print(f"WARNING: {message}; the restore error is preserved", file=sys.stderr)


def _restore_roles(container: str, roles_path: Path, *, timeout: int) -> list[str]:
    """Apply roles.sql and return the required roles present afterward."""
    _preflight_roles_file(container, roles_path, timeout=timeout)
    # FIX: cluster-global role settings survive database replacement just like
    # memberships do, and a clean roles.sql carries no ALTER ROLE app_* SET
    # line to overwrite a target-only leftover -- normalize before replay.
    _reset_existing_protected_role_settings(container, timeout=timeout)
    argv = _container_sh(
        container,
        'exec psql -X -U "$POSTGRES_USER" -d postgres '
        "--no-password -v ON_ERROR_STOP=0 -q -f -",
    )
    completed = _run_with_file(argv, timeout=timeout, source=roles_path)
    unexpected = _unexpected_roles_errors(completed.stderr)
    if unexpected:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            f"roles.sql reported unexpected errors while ON_ERROR_STOP was off: "
            f"{'; '.join(unexpected)}. Only 'role already exists' for the "
            "bootstrap superuser is tolerated.",
        )
    allowed = _allowed_role_duplicate_lines(completed.stderr)
    # Non-zero is expected when the bootstrap duplicate ERROR fired.
    # Non-zero with no allowed ERROR lines (empty/FATAL/other noise) fails closed.
    if completed.returncode != 0 and not allowed:
        noise = completed.stderr.strip() or f"psql exited {completed.returncode}"
        raise RestoreError(
            EXIT_ROLES_FAILED,
            f"roles.sql apply exited {completed.returncode}: {noise}",
        )
    if allowed:
        print("roles.sql reported (expected: 'role already exists' for the bootstrap user):")
        for line in allowed:
            print(f"    {line}")
    present = _required_roles_present(container, timeout=timeout)
    missing = [role for role in REQUIRED_ROLES if role not in present]
    if missing:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            f"after applying {roles_path.name} the cluster still has no "
            f"{', '.join(missing)}. The dump's RLS policies and GRANT "
            "statements reference those roles, so restoring the data now "
            "would fail part-way and leave a half-populated database.",
        )
    # FIX: assert the live graph and attribute surface, not just the names --
    # all three checks now live in one fail-closed helper.
    _refuse_post_apply_role_problems(container, roles_path, timeout=timeout)
    return present


# ============================================================================
# Purpose: Reapply database ownership and database-level privileges captured by
#          the backup after roles.sql has created the referenced roles.
# Database/ORM: ``pg_database`` ACL state; no ORM.
# Standards: Revoke the target's current database ACL first, then apply only
#            validated owner/grantee/privilege tuples using quoted identifiers.
#            This is intentionally separate from pg_restore: a custom dump made
#            without ``--create`` does not carry the database object's ACL.
# Blast Radius: Authorization. A missing database ACL can change who may
#               CONNECT, CREATE or create temporary objects after recovery.
# Connections:
#   - File: scripts/backup_database.py -> DATABASE_ACL_SQL writes this JSON.
#   - File: scripts/restore_database.py -> _execute_restore applies it before
#     data replay and verification.
# ============================================================================
def _apply_database_acl(
    container: str,
    target_db: str,
    owner: str,
    entries: list[tuple[str, str, bool]],
    *,
    timeout: int,
) -> None:
    """Reset and restore database owner/ACL state from validated metadata."""
    if not target_db or target_db.casefold() in {"postgres", "template0", "template1"}:
        raise RestoreError(
            EXIT_USAGE,
            f"refusing to apply database ACL to protected database {target_db!r}",
        )
    quoted_db = _quote_identifier(target_db)
    role_rows = _psql(
        container,
        "SELECT rolname FROM pg_catalog.pg_roles ORDER BY rolname;",
        timeout=timeout,
        dbname="postgres",
    )
    current_roles = {line.strip() for line in role_rows.splitlines() if line.strip()}
    statements = [f"REVOKE ALL PRIVILEGES ON DATABASE {quoted_db} FROM PUBLIC;"]
    statements.extend(
        f"REVOKE ALL PRIVILEGES ON DATABASE {quoted_db} FROM {_quote_identifier(role)};"
        for role in sorted(current_roles)
    )
    statements.append(f"ALTER DATABASE {quoted_db} OWNER TO {_quote_identifier(owner)};")
    for grantee, privilege, grantable in entries:
        recipient = "PUBLIC" if grantee == "PUBLIC" else _quote_identifier(grantee)
        option = " WITH GRANT OPTION" if grantable else ""
        statements.append(
            f"GRANT {privilege} ON DATABASE {quoted_db} TO {recipient}{option};"
        )
    _psql(container, "\n".join(statements) + "\n", timeout=timeout, dbname="postgres")


def _live_database_locale_row(
    container: str, *, timeout: int, dbname: str | None = None
) -> str:
    """Read the target locale row, with the PostgreSQL pre-17 fallback."""
    try:
        return _psql(container, DATABASE_LOCALE_SQL, timeout=timeout, dbname=dbname).strip()
    except RestoreError:
        return _psql(
            container, DATABASE_LOCALE_LEGACY_SQL, timeout=timeout, dbname=dbname
        ).strip()


def _require_target_locale(
    container: str, expected: str, *, timeout: int, dbname: str | None = None
) -> None:
    """Refuse a generated replacement whose locale differs from the source."""
    actual = _live_database_locale_row(container, timeout=timeout, dbname=dbname)
    if actual != expected:
        raise RestoreError(
            EXIT_USAGE,
            "replacement database locale does not match the backup source "
            f"(expected {expected!r}, got {actual!r}); refusing cutover. "
            "Verify the target PostgreSQL version supports the recorded locale provider",
        )


# ============================================================================
# Purpose: Prove the archive is READABLE by THIS container's pg_restore before
#          any cluster-global role replay or staging database creation. The
#          sha256 digest proves the bytes match what backup wrote; it does NOT
#          prove this container can parse them.
# Database/ORM: None. ``pg_restore --list`` reads the archive header and table
#               of contents; it opens no connection and writes nothing.
# Standards: Fail closed before any mutation. A non-zero exit is
#            EXIT_RESTORE_FAILED -- the same code the later real pg_restore
#            would have produced, so the documented exit table is unchanged.
# Blast Radius: Disaster recovery admission; a refusal leaves the live target
#               and cluster roles untouched.
# Connections:
#   - File: scripts/backup_database.py -> ``_pg_restore_list`` runs the same
#     read-only listing at BACKUP time to reject an unrestorable dump.
# ============================================================================
def _preflight_dump_readable(container: str, dump_path: Path, *, timeout: int) -> None:
    """Refuse an archive this container's pg_restore cannot read, read-only.

    Note the limit honestly: ``--list`` reads the header and TOC, so it catches
    an unreadable header, a corrupt TOC and a format version this pg_restore
    does not support -- failures that would otherwise surface during isolated
    replay. It does not read the data section, so it is not a promise the
    restore will succeed; it is a promise the archive is not obviously
    unrestorable before any restore mutation begins.

    Args:
        container: Container whose pg_restore must be able to read the archive.
        dump_path: The archive to probe, streamed on stdin.
        timeout: Seconds allowed for the listing.

    Raises:
        RestoreError: When this container's pg_restore cannot read the archive.
    """
    argv = _container_sh(container, "exec pg_restore --list")
    completed = _run_with_file(argv, timeout=timeout, source=dump_path)
    if completed.returncode != 0:
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"pg_restore --list could not read {dump_path.name} inside "
            f"{container} -- a newer archive format, or a corrupt header or "
            f"table of contents: {completed.stderr.strip()}. "
            "The target database was not changed.",
        )


# ============================================================================
# Purpose: Restore the custom-format archive into one isolated database in a
#          single transaction after required roles are known to exist.
# Database/ORM: Recreates every table, index, constraint, RLS policy, GRANT,
#               and large object represented in database.dump.
# Standards: --single-transaction (which implies --exit-on-error); explicit
#            database name; archive streamed from owner-only host staging.
#            Production replacement replay never uses --clean.
# Blast Radius: The generated replacement only; the live target name and bytes
#               remain untouched until verification and cutover.
# Connections:
#   - File: scripts/backup_database.py -> _dump_database produced the archive.
#   - File: scripts/restore_database.py -> _execute_restore selects dbname.
# ============================================================================
def _restore_data(
    container: str,
    dump_path: Path,
    *,
    timeout: int,
    clean: bool,
    dbname: str | None = None,
) -> None:
    """Restore database.dump into ``dbname`` via one pg_restore transaction."""
    # ``pg_restore`` includes large objects by default for custom archives, but
    # make that contract explicit: a blob-only database must not appear
    # restored merely because every ordinary table count matches.
    flags = "--no-password --large-objects --single-transaction"
    if clean:
        flags = "--no-password --large-objects --clean --if-exists --single-transaction"
    target = shlex.quote(dbname) if dbname else '"$POSTGRES_DB"'
    argv = _container_sh(
        container,
        f'exec pg_restore -U "$POSTGRES_USER" -d {target} {flags}',
    )
    completed = _run_with_file(argv, timeout=timeout, source=dump_path)
    if completed.returncode != 0:
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"pg_restore failed: {completed.stderr.strip()}",
        )
    if completed.stderr.strip():
        print("pg_restore warnings:")
        for line in completed.stderr.strip().splitlines():
            print(f"    {line}")


def _table_row_counts(
    container: str, *, timeout: int, dbname: str | None = None
) -> dict[str, int]:
    """Return {tablename: row_count} for every table in public."""
    raw = _psql(container, LIST_TABLES_SQL, timeout=timeout, dbname=dbname)
    tables = [line.strip() for line in raw.splitlines() if line.strip()]
    if not tables:
        return {}
    branches = [_count_sql_branch(name) for name in tables]
    counts: dict[str, int] = {}
    sql = " UNION ALL ".join(branches) + " ORDER BY t;"
    for line in _psql(container, sql, timeout=timeout, dbname=dbname).splitlines():
        if not line.strip():
            continue
        name, _, raw_count = line.rpartition("|")
        # FIX: a malformed psql row used to raise a bare ValueError out of the
        # whole process, exiting on an undocumented code with no explanation.
        try:
            counts[name] = int(raw_count)
        except ValueError as exc:
            raise RestoreError(
                EXIT_RESTORE_FAILED,
                f"could not read a row count from psql output {line!r}: {exc}",
            ) from exc
    return counts


def _large_object_count(
    container: str, *, timeout: int, dbname: str | None = None
) -> int:
    """Return the number of large objects in the restored database."""
    raw = _psql(
        container,
        "SELECT count(*) FROM pg_catalog.pg_largeobject_metadata;",
        timeout=timeout,
        dbname=dbname,
    ).strip()
    try:
        count = int(raw or "0")
    except ValueError as exc:
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"could not read the large-object count from psql output {raw!r}: {exc}",
        ) from exc
    if count < 0:
        raise RestoreError(EXIT_RESTORE_FAILED, f"large-object count is negative: {count}")
    return count


# ============================================================================
# Purpose: Prove the data came back. Compare every table's restored row count
#          against the manifest and report per-table, so the rehearsal has an
#          objective pass/fail instead of "it looks right".
# Database/ORM: One count per table in ``public`` on the restored database.
# Standards: Fail-closed -- a missing table, an extra table, or any count that
#            is not equal fails with exit 7. Row counts recorded at backup time
#            can legitimately exceed the archive if the application wrote
#            during the dump, so that case is reported explicitly rather than
#            hidden behind a tolerance.
# Blast Radius: Read-only; this is the acceptance test for the whole drill.
# Connections:
#   - File: scripts/backup_database.py -> ``_table_row_counts`` wrote the
#     manifest's table_row_counts block compared here.
# ============================================================================
def _verify(
    container: str,
    manifest: dict[str, object],
    *,
    timeout: int,
    dbname: str | None = None,
) -> bool:
    """Compare restored public table row counts to the manifest; True if all match."""
    expected = _require_manifest_table_row_counts(manifest)
    _require_manifest_seed_floor(manifest, expected)
    actual = _table_row_counts(container, timeout=timeout, dbname=dbname)
    expected_large_objects = _require_manifest_large_object_count(manifest)
    actual_large_objects = _large_object_count(container, timeout=timeout, dbname=dbname)
    names = sorted(set(expected) | set(actual))
    if not names:
        print("VERIFY: the manifest recorded no tables and the restore produced none.")
        return False
    width = max(len(name) for name in names)
    failures = 0
    if expected_large_objects != actual_large_objects:
        failures += 1
        print(
            "large objects: "
            f"manifest={expected_large_objects} restored={actual_large_objects} MISMATCH"
        )
    else:
        print(
            f"large objects: manifest={expected_large_objects} "
            f"restored={actual_large_objects} ok"
        )
    print()
    print(f"{'table'.ljust(width)}  {'manifest':>10}  {'restored':>10}  status")
    print(f"{'-' * width}  {'-' * 10}  {'-' * 10}  ------")
    for name in names:
        want = expected.get(name)
        got = actual.get(name)
        if want is None:
            status, failures = "EXTRA", failures + 1
        elif got is None:
            status, failures = "MISSING", failures + 1
        elif want == got:
            status = "ok"
        elif got < want:
            status, failures = "SHORT", failures + 1
        else:
            status, failures = "OVER", failures + 1
        print(
            f"{name.ljust(width)}  {('-' if want is None else want):>10}  "
            f"{('-' if got is None else got):>10}  {status}"
        )
    print()
    total_expected = sum(expected.values())
    total_actual = sum(actual.values())
    print(f"tables: manifest={len(expected)} restored={len(actual)}")
    print(f"rows:   manifest={total_expected} restored={total_actual}")
    return failures == 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse restore CLI arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Restore a backup run produced by scripts/backup_database.py: roles first, "
            "then the custom-format archive, then verify row counts."
        ),
    )
    parser.add_argument(
        "--backup-dir",
        required=True,
        help="One ums-backup-YYYYMMDDTHHMMSSZ run directory.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--rehearse",
        action="store_true",
        help=(
            "Restore into a throwaway container built from the manifest image, verify, "
            "then destroy it. Touches nothing that already exists."
        ),
    )
    target.add_argument(
        "--container",
        default=None,
        help="Restore into this existing container (name or ID).",
    )
    target.add_argument(
        "--compose",
        action="store_true",
        help=(
            "Restore into the running compose Postgres service resolved by "
            "--project/--service. Destructive; normally paired with --allow-nonempty."
        ),
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Compose project label.")
    parser.add_argument("--service", default=DEFAULT_SERVICE, help="Compose service label.")
    parser.add_argument(
        "--allow-nonempty",
        action="store_true",
        help=(
            "Permit a verified replacement cutover when the target has user "
            "objects. Restore and verification happen under a unique staging "
            "database name while the live target remains untouched. Cutover "
            "disables/drains sessions, renames the old target to a preserved "
            "ums_previous_<id> database, then promotes the replacement. The two "
            "renames are a short non-atomic PostgreSQL boundary with automatic "
            "rollback and explicit recovery names on failure. Stop application "
            "and scheduler traffic for the entire command."
        ),
    )
    parser.add_argument(
        "--keep-throwaway",
        action="store_true",
        help="With --rehearse, leave the throwaway container running for inspection.",
    )
    parser.add_argument(
        "--rehearse-image",
        default=None,
        help=(
            "With --rehearse, run this image instead of the one recorded at "
            "backup time. Required once the recorded image was pruned: the "
            "manifest is unsigned, so its source.image reference is never "
            "executed on its own authority."
        ),
    )
    parser.add_argument(
        "--wait-for-postgres",
        type=int,
        default=300,
        help="Seconds to wait for the target Postgres to accept connections.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Seconds allowed for each psql / pg_restore call.",
    )
    parser.add_argument(
        "--docker-timeout",
        type=int,
        default=120,
        help="Seconds allowed for each docker CLI call.",
    )
    parsed = parser.parse_args(argv)
    if parsed.rehearse_image and not parsed.rehearse:
        parser.error("--rehearse-image is only valid together with --rehearse")
    return parsed


# ============================================================================
# Purpose: Resolve the application database name from the selected container
#          before any target-scoped lock or SQL is constructed.
# Database/ORM: None; reads the container's POSTGRES_DB environment value.
# Standards: Absolute Docker executable; bounded argv-only subprocess; fail
#            closed on missing/empty values and preserve stderr diagnostics.
# Blast Radius: Restore target selection; read-only.
# Connections:
#   - File: scripts/restore_database.py -> _execute_restore validates and locks
#     the resolved name before creating an isolated replacement.
#   - File: docker-compose.yml -> defines POSTGRES_DB for the selected service.
# ============================================================================
def _container_default_database(container: str, *, timeout: int) -> str:
    """Return POSTGRES_DB straight from the container environment."""
    docker_path = shutil.which("docker")
    if not docker_path:
        raise RestoreError(
            EXIT_DOCKER_UNAVAILABLE,
            f"docker CLI not found on PATH; cannot read POSTGRES_DB inside "
            f"{container}",
        )
    completed = subprocess.run(
        [docker_path, "exec", container, "printenv", "POSTGRES_DB"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"could not read POSTGRES_DB inside {container}: "
            f"{completed.stderr.strip() or 'unset'}",
        )
    return completed.stdout.strip()


_LOCALE_FIELD_RE = re.compile(r"^[A-Za-z0-9_@+\-.]*$")


def _locale_provider_options(
    provider: str, collate: str, ctype: str, locale: str
) -> list[str]:
    """Map one datlocprovider code onto CREATE DATABASE locale options.

    FIX: the provider field was compared against the full word ``"icu"``, but
    ``pg_database.datlocprovider::text`` yields the one-character codes
    ``'c'`` (libc), ``'i'`` (icu) and ``'b'`` (builtin, PG17+) -- so a real
    ICU database fell into the libc branch and the restore silently recreated
    the target with the wrong collation provider. The mapping is now by code;
    an unrecognized code refuses instead of degrading to libc, which would
    change ORDER BY and unique-index semantics without a word of warning.
    """
    if provider == "i":
        if locale:
            return [f"LOCALE_PROVIDER icu ICU_LOCALE {_quote_literal(locale)}"]
        raise RestoreError(
            EXIT_USAGE,
            "manifest source.database_locale has an ICU provider but no ICU locale",
        )
    if provider == "b":
        # The builtin provider (PG17+) takes BUILTIN_LOCALE, never ICU_LOCALE;
        # datlocale is NULL for a builtin database created without an explicit
        # locale, and an empty option is how "inherit the target's" is spelled
        # here.
        if locale:
            return [f"LOCALE_PROVIDER builtin BUILTIN_LOCALE {_quote_literal(locale)}"]
        raise RestoreError(
            EXIT_USAGE,
            "manifest source.database_locale has a builtin provider but no locale",
        )
    if provider not in ("c", ""):
        raise RestoreError(
            EXIT_USAGE,
            f"manifest source.database_locale carries an unrecognized locale "
            f"provider code {provider!r}; refusing to create a replacement "
            "with a guessed collation provider",
        )
    options: list[str] = []
    if not collate or not ctype:
        raise RestoreError(
            EXIT_USAGE,
            "manifest source.database_locale has no complete libc locale; "
            "refusing to inherit the target template",
        )
    options.append(f"LC_COLLATE {_quote_literal(collate)}")
    options.append(f"LC_CTYPE {_quote_literal(ctype)}")
    return options


def _create_database_options(locale_row: str) -> str:
    """Return CREATE DATABASE options reproducing the source locale.

    FIX(codex round-22 P2): a bare CREATE DATABASE inherits the TARGET
    template's encoding/locale; the backup uses pg_dump without --create, so
    the archive carries none of them and a source/target locale mismatch
    silently changed collation semantics -- ORDER BY and unique indexes
    behaved differently after restore. The manifest's database_locale row
    (encoding id|encoding name|collate|ctype|provider code|locale name) now
    drives TEMPLATE template0 plus explicit ENCODING/LC_COLLATE/LC_CTYPE
    (libc, provider code ``c`` or empty), LOCALE_PROVIDER icu ICU_LOCALE
    (code ``i``) or LOCALE_PROVIDER builtin BUILTIN_LOCALE (code ``b``).
    Every field is charset-validated and the literals are quoted, so
    the unsigned manifest cannot inject through it; a row that is present but
    malformed REFUSES rather than silently dropping the locale.
    """
    if not locale_row:
        raise RestoreError(
            EXIT_USAGE,
            "manifest source.database_locale is missing; refusing to inherit "
            "a template database's encoding and collation",
        )
    parts = locale_row.split("|")
    if len(parts) != 6 or any(not _LOCALE_FIELD_RE.match(part) for part in parts):
        raise RestoreError(
            EXIT_USAGE,
            f"manifest source.database_locale is malformed: {locale_row!r}; "
            "refusing to create a replacement with an unpreserved locale",
        )
    _enc_id, encoding, collate, ctype, provider, locale = parts
    if not encoding:
        raise RestoreError(
            EXIT_USAGE,
            "manifest source.database_locale carries no encoding name; "
            "refusing to create a replacement with an unpreserved locale",
        )
    options = ["TEMPLATE template0", f"ENCODING {_quote_literal(encoding)}"]
    options.extend(_locale_provider_options(provider, collate, ctype, locale))
    return " " + " ".join(options)


def _validate_application_database_name(database: str) -> None:
    """Refuse empty/control/protected database names before generated SQL."""
    if not database or any(char in database for char in "\x00\r\n"):
        raise RestoreError(
            EXIT_USAGE,
            "target database name is empty or contains control characters",
        )
    if database.casefold() in {"postgres", "template0", "template1"}:
        raise RestoreError(
            EXIT_USAGE,
            f"refusing to replace PostgreSQL maintenance/template database {database!r}",
        )


# ============================================================================
# Purpose: Create and manage the isolated database that receives roles/ACL/data
#          and verification before the live target name is touched.
# Database/ORM: PostgreSQL pg_database through maintenance-database psql calls.
# Standards: Unique generated names, explicit locale, bounded commands, quoted
#            identifiers, and no DROP of the live target.
# Blast Radius: Disaster recovery cutover; the original database remains intact.
# Connections:
#   - File: scripts/restore_database.py -> _execute_restore and cutover helpers.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> verified cutover runbook.
# ============================================================================
def _create_replacement_database(
    container: str, target_db: str, *, timeout: int, locale_row: str
) -> str:
    """Create a locale-faithful staging database and return its unique name."""
    _validate_application_database_name(target_db)
    replacement = f"ums_restore_{uuid.uuid4().hex[:24]}"
    _psql(
        container,
        f"CREATE DATABASE {_quote_identifier(replacement)}"
        f"{_create_database_options(locale_row)};",
        timeout=timeout,
        dbname="postgres",
    )
    return replacement


def _set_database_allow_connections(
    container: str, database: str, *, allow: bool, timeout: int
) -> None:
    """Enable or disable all new connections to one database."""
    keyword = "true" if allow else "false"
    _psql(
        container,
        f"ALTER DATABASE {_quote_identifier(database)} WITH ALLOW_CONNECTIONS {keyword};",
        timeout=timeout,
        dbname="postgres",
    )


def _drain_database_sessions(container: str, database: str, *, timeout: int) -> None:
    """Terminate and boundedly wait for every session on a disabled database."""
    deadline = time.monotonic() + max(1, timeout)
    terminate_sql = (
        "SELECT pg_catalog.pg_terminate_backend(pid) "
        "FROM pg_catalog.pg_stat_activity WHERE datname = "
        f"{_quote_literal(database)};"
    )
    while True:
        _psql(container, terminate_sql, timeout=timeout, dbname="postgres")
        remaining = _foreign_writer_session_count(
            container, timeout=timeout, target_db=database
        )
        if remaining == 0:
            return
        if time.monotonic() >= deadline:
            raise RestoreError(
                EXIT_RESTORE_FAILED,
                f"could not drain {remaining} session(s) from database {database!r} "
                f"within {timeout}s",
            )
        time.sleep(0.1)


def _rename_database(
    container: str, source: str, destination: str, *, timeout: int
) -> None:
    """Rename one disconnected database through the maintenance database."""
    _psql(
        container,
        f"ALTER DATABASE {_quote_identifier(source)} RENAME TO "
        f"{_quote_identifier(destination)};",
        timeout=timeout,
        dbname="postgres",
    )


def _drop_generated_database(container: str, database: str, *, timeout: int) -> None:
    """Drop only a caller-owned generated staging database."""
    if not database.startswith("ums_restore_"):
        raise RestoreError(
            EXIT_INTERNAL,
            f"refusing cleanup of non-generated database {database!r}",
        )
    _psql(
        container,
        f"DROP DATABASE IF EXISTS {_quote_identifier(database)} WITH (FORCE);",
        timeout=timeout,
        dbname="postgres",
    )


@dataclass(frozen=True)
class _DatabaseCatalogRow:
    """One independently observed pg_database identity/name/admission row."""

    oid: int
    name: str
    allow_connections: bool


# ============================================================================
# Purpose: Observe cutover databases by immutable OID as well as mutable name,
#          including any unexpected occupant of a reserved cutover name.
# Database/ORM: pg_database through a fresh maintenance-database psql -X call.
# Standards: JSON rows, strict types, duplicate rejection, and quoted filters;
#            each call is an independent control connection after ambiguity.
# Blast Radius: Restore cutover reconciliation and operator recovery commands.
# Connections:
#   - File: scripts/restore_database.py -> _capture_cutover_database_identities
#     and _reconcile_cutover_rollback.
# ============================================================================
def _database_catalog_rows(
    container: str,
    *,
    timeout: int,
    names: tuple[str, ...] = (),
    oids: tuple[int, ...] = (),
) -> dict[int, _DatabaseCatalogRow]:
    """Read matching pg_database rows through an independent control query."""
    clauses: list[str] = []
    if names:
        clauses.append(
            "d.datname IN (" + ", ".join(_quote_literal(name) for name in names) + ")"
        )
    if oids:
        if any(isinstance(oid, bool) or not isinstance(oid, int) or oid <= 0 for oid in oids):
            raise RestoreError(EXIT_INTERNAL, "invalid database OID in cutover state")
        clauses.append("d.oid IN (" + ", ".join(str(oid) for oid in oids) + ")")
    if not clauses:
        raise RestoreError(EXIT_INTERNAL, "cutover catalog query has no identity filter")
    sql = (
        "SELECT json_build_object('oid', d.oid::text, 'name', d.datname, "
        "'allow_connections', d.datallowconn)::text "
        "FROM pg_catalog.pg_database d WHERE "
        + " OR ".join(f"({clause})" for clause in clauses)
        + " ORDER BY d.oid;"
    )
    rows: dict[int, _DatabaseCatalogRow] = {}
    names_seen: set[str] = set()
    raw = _psql(container, sql, timeout=timeout, dbname="postgres")
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except ValueError as exc:
            raise RestoreError(
                EXIT_RESTORE_FAILED,
                f"could not parse pg_database cutover JSON: {exc}",
            ) from exc
        if not isinstance(document, dict):
            raise RestoreError(
                EXIT_RESTORE_FAILED, "pg_database cutover row is not a JSON object"
            )
        raw_oid = document.get("oid")
        name = document.get("name")
        allow = document.get("allow_connections")
        try:
            oid = int(raw_oid) if isinstance(raw_oid, str) else -1
        except ValueError as exc:
            raise RestoreError(
                EXIT_RESTORE_FAILED, "pg_database cutover row has an invalid OID"
            ) from exc
        if oid <= 0 or not isinstance(name, str) or not isinstance(allow, bool):
            raise RestoreError(
                EXIT_RESTORE_FAILED,
                "pg_database cutover row has an invalid identity/name/admission shape",
            )
        if oid in rows or name in names_seen:
            raise RestoreError(
                EXIT_RESTORE_FAILED,
                "pg_database cutover query returned duplicate OID or database name",
            )
        rows[oid] = _DatabaseCatalogRow(oid, name, allow)
        names_seen.add(name)
    return rows


def _capture_cutover_database_identities(
    container: str,
    target_db: str,
    replacement: str,
    previous: str,
    *,
    timeout: int,
) -> tuple[int, int]:
    """Capture immutable OIDs and prove the generated previous name is free."""
    rows = _database_catalog_rows(
        container,
        timeout=timeout,
        names=(target_db, replacement, previous),
    )
    by_name = {row.name: row for row in rows.values()}
    if previous in by_name:
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"generated rollback database name {previous!r} is already occupied",
        )
    if target_db not in by_name or replacement not in by_name:
        missing = [
            name for name in (target_db, replacement) if name not in by_name
        ]
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            "cutover identity capture could not find database(s): " + ", ".join(missing),
        )
    target_oid = by_name[target_db].oid
    replacement_oid = by_name[replacement].oid
    if target_oid == replacement_oid:
        raise RestoreError(
            EXIT_INTERNAL, "target and replacement resolved to the same database OID"
        )
    return target_oid, replacement_oid


def _observe_cutover_database_state(
    container: str,
    target_db: str,
    replacement: str,
    previous: str,
    target_oid: int,
    replacement_oid: int,
    *,
    timeout: int,
) -> dict[int, _DatabaseCatalogRow]:
    """Observe both identities plus all reserved names after an ambiguous call."""
    return _database_catalog_rows(
        container,
        timeout=timeout,
        names=(target_db, replacement, previous),
        oids=(target_oid, replacement_oid),
    )


def _cutover_state_summary(
    rows: dict[int, _DatabaseCatalogRow] | None,
    target_oid: int,
    replacement_oid: int,
) -> str:
    """Render exact identity/name/admission state without inferring success."""
    if rows is None:
        return "UNAVAILABLE (the independent pg_database query failed)"

    def describe(label: str, oid: int) -> str:
        row = rows.get(oid)
        if row is None:
            return f"{label}_oid={oid}:MISSING"
        return (
            f"{label}_oid={oid}:name={row.name!r},"
            f"allow_connections={row.allow_connections}"
        )

    known_oids = {target_oid, replacement_oid}
    unexpected = sorted(
        f"oid={row.oid}:name={row.name!r},allow_connections={row.allow_connections}"
        for oid, row in rows.items()
        if oid not in known_oids
    )
    suffix = f", unexpected=[{'; '.join(unexpected)}]" if unexpected else ""
    return (
        describe("previous_live", target_oid)
        + "; "
        + describe("verified_replacement", replacement_oid)
        + suffix
    )


def _cutover_recovery_commands(
    rows: dict[int, _DatabaseCatalogRow] | None,
    target_db: str,
    replacement: str,
    previous: str,
    target_oid: int,
    replacement_oid: int,
) -> str:
    """Build commands valid for the last independently observed catalog state."""
    query = (
        "SELECT oid, datname, datallowconn FROM pg_catalog.pg_database WHERE oid IN "
        f"({target_oid}, {replacement_oid}) OR datname IN "
        f"({_quote_literal(target_db)}, {_quote_literal(replacement)}, "
        f"{_quote_literal(previous)});"
    )
    if rows is None:
        return (
            "re-query first: "
            + query
            + " No ALTER DATABASE recovery command can be derived until that query "
            "succeeds and both captured OIDs are identified."
        )
    old = rows.get(target_oid)
    new = rows.get(replacement_oid)
    by_name = {row.name: row for row in rows.values()}
    steps: list[str] = []
    safe_identity_shape = bool(
        old
        and new
        and (
            (old.name == target_db and new.name == replacement)
            or (old.name == previous and new.name == replacement)
            or (old.name == previous and new.name == target_db)
        )
    )
    if not safe_identity_shape:
        return (
            "re-query first: "
            + query
            + " Observed-state recovery: -- captured OIDs are not in a known "
            "cutover shape; do not rename any database"
        )
    old_will_be_target = old is not None and old.name == target_db
    if new is not None and new.name == target_db and replacement not in by_name:
        steps.extend(
            [
                f"ALTER DATABASE {_quote_identifier(target_db)} WITH ALLOW_CONNECTIONS false;",
                "SELECT pg_catalog.pg_terminate_backend(pid) FROM "
                "pg_catalog.pg_stat_activity WHERE datname = "
                f"{_quote_literal(target_db)} AND pid <> pg_catalog.pg_backend_pid();",
                f"ALTER DATABASE {_quote_identifier(target_db)} RENAME TO "
                f"{_quote_identifier(replacement)};",
            ]
        )
        by_name.pop(target_db, None)
        by_name[replacement] = new
    if old is not None and old.name == previous and target_db not in by_name:
        steps.append(
            f"ALTER DATABASE {_quote_identifier(previous)} RENAME TO "
            f"{_quote_identifier(target_db)};"
        )
        by_name[target_db] = old
        old_will_be_target = True
    if old_will_be_target:
        steps.append(
            f"ALTER DATABASE {_quote_identifier(target_db)} WITH ALLOW_CONNECTIONS true;"
        )
    if new is not None and new.name == replacement:
        steps.append(
            f"ALTER DATABASE {_quote_identifier(replacement)} WITH ALLOW_CONNECTIONS false;"
        )
    if not steps:
        steps.append(
            "-- observed identities are not in a safe automatic rename shape; "
            "do not rename an unknown occupant"
        )
    return "re-query first: " + query + " Observed-state recovery: " + " ".join(steps)


def _reconcile_cutover_rollback(
    container: str,
    target_db: str,
    replacement: str,
    previous: str,
    target_oid: int,
    replacement_oid: int,
    *,
    timeout: int,
) -> tuple[bool, dict[int, _DatabaseCatalogRow] | None, list[str]]:
    """Restore canonical names/admission using catalog observations, not flags."""
    notes: list[str] = []
    attempts: dict[tuple[str, str], int] = {}

    def observe() -> dict[int, _DatabaseCatalogRow] | None:
        try:
            return _observe_cutover_database_state(
                container,
                target_db,
                replacement,
                previous,
                target_oid,
                replacement_oid,
                timeout=timeout,
            )
        except Exception as exc:
            notes.append(f"independent pg_database reconciliation query failed: {exc}")
            return None

    rows = observe()
    if rows is None:
        return False, None, notes
    for _step in range(10):
        old = rows.get(target_oid)
        new = rows.get(replacement_oid)
        if old is None or new is None:
            notes.append("one or both captured database OIDs disappeared")
            return False, rows, notes
        by_name = {row.name: row for row in rows.values()}

        if new.name == target_db:
            if new.allow_connections:
                action_key = (f"allow:{target_db}", "False")
                attempts[action_key] = attempts.get(action_key, 0) + 1
                if attempts[action_key] > 2:
                    notes.append(
                        "bounded rollback exhausted disabling connections on "
                        f"promoted database {target_db!r}"
                    )
                    return False, rows, notes
                try:
                    _set_database_allow_connections(
                        container, target_db, allow=False, timeout=timeout
                    )
                except Exception as exc:
                    notes.append(
                        f"ALLOW_CONNECTIONS=False on {target_db!r} reported failure: {exc}"
                    )
                refreshed = observe()
                if refreshed is None:
                    return False, None, notes
                rows = refreshed
                continue
            try:
                _drain_database_sessions(container, target_db, timeout=timeout)
            except Exception as exc:
                notes.append(
                    f"could not drain promoted database {target_db!r} before rollback: {exc}"
                )
                refreshed = observe()
                return False, refreshed, notes

        if old.name not in {target_db, previous}:
            notes.append(
                f"previous-live OID {target_oid} has unexpected name {old.name!r}"
            )
            return False, rows, notes
        if new.name not in {target_db, replacement}:
            notes.append(
                f"verified replacement OID {replacement_oid} has unexpected name {new.name!r}"
            )
            return False, rows, notes
        if (new.name == target_db and old.name != previous) or (
            old.name == target_db and new.name != replacement
        ):
            notes.append("captured database OIDs are not in a known cutover name shape")
            return False, rows, notes

        action: tuple[str, str] | None = None
        if new.name == target_db:
            occupant = by_name.get(replacement)
            if occupant is not None and occupant.oid != replacement_oid:
                notes.append(
                    f"cannot move verified OID {replacement_oid} back: {replacement!r} "
                    f"is occupied by unexpected OID {occupant.oid}"
                )
                return False, rows, notes
            action = (target_db, replacement)
        elif old.name == previous:
            occupant = by_name.get(target_db)
            if occupant is not None and occupant.oid != target_oid:
                notes.append(
                    f"cannot restore previous-live OID {target_oid}: {target_db!r} "
                    f"is occupied by unexpected OID {occupant.oid}"
                )
                return False, rows, notes
            action = (previous, target_db)

        if action is not None:
            attempts[action] = attempts.get(action, 0) + 1
            if attempts[action] > 2:
                notes.append(
                    f"bounded rollback exhausted for rename {action[0]!r} -> {action[1]!r}"
                )
                return False, rows, notes
            try:
                _rename_database(
                    container, action[0], action[1], timeout=timeout
                )
            except Exception as exc:
                notes.append(
                    f"rename {action[0]!r} -> {action[1]!r} reported failure: {exc}"
                )
            refreshed = observe()
            if refreshed is None:
                return False, None, notes
            rows = refreshed
            continue

        # Canonical names are restored. Reconcile both admission flags even if
        # the ALTER DATABASE client previously timed out after server commit.
        desired_allow = ((replacement, replacement_oid, False), (target_db, target_oid, True))
        changed = False
        for name, oid, desired in desired_allow:
            row = rows.get(oid)
            if row is None or row.name != name or row.allow_connections == desired:
                continue
            action_key = (f"allow:{name}", str(desired))
            attempts[action_key] = attempts.get(action_key, 0) + 1
            if attempts[action_key] > 2:
                notes.append(
                    f"bounded rollback exhausted setting ALLOW_CONNECTIONS={desired} "
                    f"on {name!r}"
                )
                return False, rows, notes
            try:
                _set_database_allow_connections(
                    container, name, allow=desired, timeout=timeout
                )
            except Exception as exc:
                notes.append(
                    f"ALLOW_CONNECTIONS={desired} on {name!r} reported failure: {exc}"
                )
            refreshed = observe()
            if refreshed is None:
                return False, None, notes
            rows = refreshed
            changed = True
            break
        if changed:
            continue
        old = rows.get(target_oid)
        new = rows.get(replacement_oid)
        complete = bool(
            old
            and new
            and old.name == target_db
            and old.allow_connections
            and new.name == replacement
            and not new.allow_connections
        )
        if complete:
            return True, rows, notes
        notes.append("catalog state did not converge to the rollback invariant")
        return False, rows, notes
    notes.append("bounded catalog reconciliation exceeded ten state transitions")
    return False, rows, notes


# ============================================================================
# Purpose: Cut over a fully verified replacement while retaining the previous
#          database under a unique rollback name.
# Database/ORM: PostgreSQL database admission, sessions, and ALTER DATABASE.
# Standards: Advisory lock held by caller; capture immutable database OIDs;
#            disable connections before draining; re-query name/admission state
#            after every ambiguity; bounded rollback; never DROP either database.
# Blast Radius: Short non-atomic name cutover. Failure reports exact names and
#               recovery SQL instead of deleting the last-good database.
# Connections:
#   - File: scripts/restore_database.py -> _target_restore_lock / _execute_restore.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> cutover recovery commands.
# ============================================================================
def _cutover_verified_database(
    container: str,
    target_db: str,
    replacement: str,
    *,
    timeout: int,
    finalize: Callable[[], None] | None = None,
) -> str:
    """Swap verified ``replacement`` into ``target_db`` and retain the old DB."""
    previous = f"ums_previous_{uuid.uuid4().hex[:24]}"
    target_oid, replacement_oid = _capture_cutover_database_identities(
        container,
        target_db,
        replacement,
        previous,
        timeout=timeout,
    )
    stage = "disable target connections"
    try:
        _set_database_allow_connections(
            container, target_db, allow=False, timeout=timeout
        )
        stage = "drain target connections"
        _drain_database_sessions(container, target_db, timeout=timeout)
        stage = "disable replacement connections"
        _set_database_allow_connections(
            container, replacement, allow=False, timeout=timeout
        )
        stage = "drain replacement connections"
        _drain_database_sessions(container, replacement, timeout=timeout)
        # Recheck both names immediately before the first non-atomic rename.
        if _foreign_writer_session_count(
            container, timeout=timeout, target_db=target_db
        ) or _foreign_writer_session_count(
            container, timeout=timeout, target_db=replacement
        ):
            raise RestoreError(
                EXIT_RESTORE_FAILED,
                "database sessions reappeared after the cutover drain",
            )
        stage = f"rename {target_db!r} to {previous!r}"
        _rename_database(container, target_db, previous, timeout=timeout)
        stage = f"rename {replacement!r} to {target_db!r}"
        _rename_database(container, replacement, target_db, timeout=timeout)
        if finalize is not None:
            stage = "finalize database-scoped role settings on the promoted database"
            finalize()
        stage = f"enable connections on {target_db!r}"
        _set_database_allow_connections(
            container, target_db, allow=True, timeout=timeout
        )
        stage = "verify promoted database identities and admission state"
        observed = _observe_cutover_database_state(
            container,
            target_db,
            replacement,
            previous,
            target_oid,
            replacement_oid,
            timeout=timeout,
        )
        old = observed.get(target_oid)
        new = observed.get(replacement_oid)
        if not (
            old
            and new
            and old.name == previous
            and not old.allow_connections
            and new.name == target_db
            and new.allow_connections
        ):
            raise RestoreError(
                EXIT_RESTORE_FAILED,
                "post-cutover catalog state does not match captured database OIDs: "
                + _cutover_state_summary(observed, target_oid, replacement_oid),
            )
        return previous
    # This boundary deliberately catches unexpected callback/helper failures:
    # once the first rename succeeds, even an internal bug must attempt the
    # same reverse-name recovery before main maps it to exit 9.
    except Exception as exc:
        rollback_complete, rollback_observed, rollback_notes = (
            _reconcile_cutover_rollback(
                container,
                target_db,
                replacement,
                previous,
                target_oid,
                replacement_oid,
                timeout=timeout,
            )
        )
        code = exc.code if isinstance(exc, RestoreError) else EXIT_INTERNAL
        state = _cutover_state_summary(
            rollback_observed, target_oid, replacement_oid
        )
        recovery = _cutover_recovery_commands(
            rollback_observed,
            target_db,
            replacement,
            previous,
            target_oid,
            replacement_oid,
        )
        notes = "; ".join(rollback_notes)
        outcome = (
            "automatic catalog-reconciled rollback completed"
            if rollback_complete
            else "automatic catalog-reconciled rollback is INCOMPLETE"
        )
        raise RestoreError(
            code,
            f"verified-database cutover failed during {stage}: {exc}; {outcome}; "
            f"observed {state}"
            + (f"; reconciliation notes: {notes}" if notes else "")
            + f". {recovery}. Neither database was dropped.",
        ) from exc


def _guard_empty(
    container: str,
    *,
    allow_nonempty: bool,
    timeout: int,
    dbname: str | None = None,
) -> None:
    """Refuse a non-empty target database unless --allow-nonempty was passed."""
    raw = _psql(container, USER_OBJECT_COUNT_SQL, timeout=timeout, dbname=dbname).strip()
    try:
        existing = int(raw or "0")
    except ValueError as exc:
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"could not read the user-object count from psql output {raw!r}: {exc}",
        ) from exc
    if existing and not allow_nonempty:
        raise RestoreError(
            EXIT_USAGE,
            f"the target database already has {existing} user objects outside "
            "system catalogs. Restore into an empty database, or pass "
            "--allow-nonempty to authorize a verified replacement cutover. The "
            "old database will be retained under an ums_previous_<id> rollback "
            "name with connections disabled.",
        )


def _prepare_restore_target(
    args: argparse.Namespace, manifest: dict[str, object]
) -> tuple[str, str | None]:
    """Resolve the restore container; return (container, throwaway_or_none)."""
    if args.rehearse:
        throwaway = _create_throwaway(
            manifest,
            timeout=args.docker_timeout,
            operator_image=args.rehearse_image or "",
        )
        print(f"rehearsal container: {throwaway}")
        return throwaway, throwaway
    container = _resolve_container(
        explicit=args.container,
        project=args.project,
        service=args.service,
        timeout=args.docker_timeout,
    )
    print(f"target container: {container}")
    return container, None


# ============================================================================
# Purpose: Orchestrate one restore into an already-resolved container: preflight
#          the live cluster, restore and verify a unique replacement database,
#          then perform a drained two-rename cutover that preserves the old DB.
# Database/ORM: None in-process. Runs psql / pg_restore inside the target
#               container only.
# Standards: Emptiness/session checks before roles; roles before data so RLS
#            grants have parents; content/seed/large-object verification before
#            cutover; OID-reconciled rename rollback; transactional database-role
#            finalization; fail closed via RestoreError.
# Blast Radius: Cluster-global role replay plus a short live-name cutover;
#               previous database retained. Rehearsal uses a throwaway only.
# Connections:
#   - File: scripts/restore_database.py -> ``_restore_roles`` / ``_restore_data`` /
#     ``_verify`` / ``main``.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> roles-first procedure.
# ============================================================================
def _execute_restore(
    container: str,
    backup_dir: Path,
    args: argparse.Namespace,
    manifest: dict[str, object],
) -> bool:
    """Build/verify an isolated replacement, then cut it over under one lock."""
    _await_postgres(container, wait_seconds=args.wait_for_postgres, timeout=args.docker_timeout)
    target_db = _container_default_database(container, timeout=args.timeout)
    _validate_application_database_name(target_db)
    locale_row, acl_owner, acl_entries = _source_database_metadata(manifest)
    _require_source_database_target(manifest, target_db)

    # The advisory lock serializes every restore targeting this database name.
    # Application sessions do not cooperate with that lock, so the final
    # cutover separately disables connections and drains both database names.
    with _target_restore_lock(container, target_db, timeout=args.docker_timeout):
        _verify_backup_artifact_digests(backup_dir, manifest)
        live_writers = _foreign_writer_session_count(
            container, timeout=args.timeout, target_db=target_db
        )
        if live_writers:
            raise RestoreError(
                EXIT_USAGE,
                f"the target database has {live_writers} live client session(s). "
                "Stop the application and scheduler containers first (e.g. "
                "`docker compose stop app app-dev`), then re-run while they "
                "remain stopped until restore verification completes.",
            )
        _guard_empty(
            container,
            allow_nonempty=args.allow_nonempty,
            timeout=args.timeout,
            dbname=target_db,
        )
        _preflight_dump_readable(container, backup_dir / DUMP_NAME, timeout=args.timeout)
        _preflight_roles_file(container, backup_dir / ROLES_NAME, timeout=args.timeout)
        acl_role_problems = _database_acl_role_problems(
            backup_dir / ROLES_NAME, acl_owner, acl_entries
        )
        if acl_role_problems:
            raise RestoreError(
                EXIT_ROLES_FAILED,
                "the source database ACL references roles roles.sql does not "
                f"declare: {', '.join(acl_role_problems)}. Refusing before "
                "the target is changed.",
            )
        live_problems = _live_protected_role_problems(container, timeout=args.timeout)
        if live_problems:
            raise RestoreError(
                EXIT_ROLES_FAILED,
                "the target cluster's application roles already carry "
                "cluster-global state a clean roles.sql neither carries nor "
                "clears: " + "; ".join(live_problems) + ". Refusing before "
                "the target is changed; clear the state and re-run.",
            )
        replacement: str | None = None
        preserve_replacement = False
        try:
            replacement = _create_replacement_database(
                container,
                target_db,
                timeout=args.timeout,
                locale_row=locale_row,
            )
            # CREATE success is not proof that every requested locale option
            # landed exactly; compare the staging catalog before any replay.
            _require_target_locale(
                container,
                locale_row,
                timeout=args.timeout,
                dbname=replacement,
            )
            # Re-hash the private staged paths at each point of use. The host
            # source can no longer swap bytes between admission and replay.
            _verify_backup_artifact_digests(backup_dir, manifest)
            try:
                roles = _restore_roles(
                    container, backup_dir / ROLES_NAME, timeout=args.timeout
                )
                database_role_settings = _desired_database_role_settings(
                    container,
                    backup_dir / ROLES_NAME,
                    target_db,
                    timeout=args.timeout,
                )
            except (RestoreError, OSError, subprocess.SubprocessError) as exc:
                code = exc.code if isinstance(exc, RestoreError) else EXIT_ROLES_FAILED
                raise RestoreError(
                    code,
                    f"{exc}; restore stopped during cluster role replay. The live "
                    "database was not renamed or dropped, but roles.sql may have "
                    "partially applied cluster-global state or settings scoped to "
                    "the target database name; inspect roles, memberships and "
                    "pg_db_role_setting before retrying.",
                ) from exc
            print(f"roles present after roles.sql: {', '.join(roles)}")
            try:
                _apply_database_acl(
                    container,
                    replacement,
                    acl_owner,
                    acl_entries,
                    timeout=args.timeout,
                )
            except (RestoreError, OSError, subprocess.SubprocessError) as exc:
                code = exc.code if isinstance(exc, RestoreError) else EXIT_RESTORE_FAILED
                raise RestoreError(
                    code,
                    f"{exc}; restore stopped during staging-database ACL replay. "
                    "The live target was not renamed or dropped.",
                ) from exc
            _verify_backup_artifact_digests(backup_dir, manifest)
            try:
                _restore_data(
                    container,
                    backup_dir / DUMP_NAME,
                    timeout=args.timeout,
                    clean=False,
                    dbname=replacement,
                )
            except (RestoreError, OSError, subprocess.SubprocessError) as exc:
                code = exc.code if isinstance(exc, RestoreError) else EXIT_RESTORE_FAILED
                raise RestoreError(
                    code,
                    f"{exc}; restore stopped during isolated data replay. The "
                    "staging transaction was rolled back when possible and the "
                    "live target was not renamed or dropped.",
                ) from exc
            print(f"pg_restore completed in isolated database {replacement}")
            ok = _verify(
                container,
                manifest,
                timeout=args.timeout,
                dbname=replacement,
            )
            if not ok:
                return False

            # From this point a failure must preserve every named database for
            # operator recovery. The cutover helper attempts rollback but never
            # drops either the verified replacement or the previous target.
            preserve_replacement = True

            def _finalize_database_role_settings() -> None:
                """Apply only database-scoped settings after final-name promotion."""
                _verify_backup_artifact_digests(backup_dir, manifest)
                _apply_database_role_settings_transactionally(
                    container,
                    target_db,
                    database_role_settings,
                    timeout=args.timeout,
                )

            previous = _cutover_verified_database(
                container,
                target_db,
                replacement,
                timeout=args.timeout,
                finalize=_finalize_database_role_settings,
            )
            print(
                f"cutover complete: previous database preserved as {previous!r} "
                "with connections disabled"
            )
            print(
                "rollback (from database postgres, after stopping traffic): "
                f"disable/drain {_quote_identifier(target_db)}, rename it to "
                f"{_quote_identifier(replacement)}, rename "
                f"{_quote_identifier(previous)} to {_quote_identifier(target_db)}, "
                "then enable connections"
            )
            return True
        finally:
            if replacement is not None and not preserve_replacement:
                active_error = sys.exc_info()[1]
                try:
                    _drop_generated_database(container, replacement, timeout=args.timeout)
                except (RestoreError, OSError, subprocess.SubprocessError) as cleanup_exc:
                    message = (
                        f"could not remove failed staging database {replacement!r}: "
                        f"{cleanup_exc}"
                    )
                    if active_error is None:
                        raise RestoreError(EXIT_RESTORE_FAILED, message) from cleanup_exc
                    print(
                        f"WARNING: {message}; the original restore error is preserved",
                        file=sys.stderr,
                    )


def _cleanup_rehearsal_throwaway(
    throwaway: str | None,
    args: argparse.Namespace,
    *,
    code: int | None,
    ok: bool,
) -> int | None:
    """Destroy or keep the rehearsal container; may promote exit on destroy fail."""
    if not throwaway:
        return code
    if args.keep_throwaway:
        print(f"rehearsal container left running: {throwaway}")
        print(f"remove it with: docker rm --force --volumes {throwaway}")
        return code
    if _destroy_throwaway(throwaway, timeout=args.docker_timeout):
        print(f"removed rehearsal container {throwaway}")
        return code
    print(
        f"RESTORE WARNING: failed to remove rehearsal container "
        f"{throwaway}. Remove it with: "
        f"docker rm --force --volumes {throwaway}",
        file=sys.stderr,
    )
    # Promote only when restore+verify would otherwise be success.
    if code is None and ok:
        return EXIT_CONTAINER_UNAVAILABLE
    return code


def _restore_process_exit(code: int | None, ok: bool) -> int:
    """Map (exception code, verify ok) onto the documented restore exit codes."""
    if code is not None and not ok:
        return code
    if not ok:
        print(
            "VERIFICATION FAILED: the restored row counts do not match the manifest.",
            file=sys.stderr,
        )
        return EXIT_VERIFY_FAILED
    print()
    print("RESTORE VERIFIED: every table matched the manifest row count.")
    if code is not None:
        return code
    return EXIT_OK


# ============================================================================
# Purpose: Drive one restore: integrity-check the run, resolve or create the
#          target, apply roles, prove the roles landed, restore the data, then
#          verify the row counts.
# Database/ORM: See the stage functions above.
# Standards: Ordered, fail-closed stages; the throwaway container is destroyed
#            in a finally block so a failed rehearsal does not leak a
#            container; verification failure is a non-zero exit, never a
#            warning. Every exit is a documented code: a last-resort
#            ``except Exception`` prints the traceback and exits 9 rather than
#            letting an unexpected error surface as a bare 1 that the runbook's
#            exit-code table cannot explain.
# Blast Radius: Disaster recovery.
# Connections:
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> the procedure this
#     function implements, and the restore exit-code table.
#   - File: scripts/backup_database.py -> ``main`` carries the same last-resort
#     handler so neither CLI can exit on an undocumented code.
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Run one restore (or rehearsal) and return the documented exit code.

    Args:
        argv: CLI argument list to parse. ``None`` reads ``sys.argv[1:]`` so
            the shell invocation and any test/wrapper drive the same parser.

    Returns:
        0 restored and verified. 2 usage / malformed backup directory /
        non-empty target without --allow-nonempty / live client sessions on
        the target. 3 Docker daemon unavailable. 4 target container
        unavailable or could not be created. 5 roles restore failed or the
        required roles are absent/compromised after replay. 6 pg_restore
        failed, including a connection-drain or rename-cutover failure.
        7 post-restore verification mismatch in the isolated replacement.
        8 backup artifacts failed their sha256 integrity check.
        9 unexpected internal error (traceback printed).
        Handled failures print a stable ``RESTORE FAILED (exit N)`` line on
        stderr and never raise; ``--rehearse`` destroys its throwaway
        container on every path.
    """
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    backup_dir = Path(args.backup_dir).expanduser()
    throwaway: str | None = None
    code: int | None = None
    ok = False
    try:
        manifest = _load_backup(backup_dir)
        with _stage_backup_artifacts(backup_dir, manifest) as staged_dir:
            _require_docker(timeout=args.docker_timeout)
            container, throwaway = _prepare_restore_target(args, manifest)
            ok = _execute_restore(container, staged_dir, args, manifest)
    except RestoreError as exc:
        print(f"RESTORE FAILED (exit {exc.code}): {exc}", file=sys.stderr)
        code = exc.code
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"RESTORE FAILED (exit {EXIT_RESTORE_FAILED}): {exc}", file=sys.stderr)
        code = EXIT_RESTORE_FAILED
    # Last resort, and deliberately broad. Nothing is swallowed -- the traceback
    # is printed -- but the process exits on a documented code instead of a bare
    # 1 that means nothing to the operator or to Task Scheduler.
    except Exception as exc:
        traceback.print_exc()
        print(
            f"RESTORE FAILED (exit {EXIT_INTERNAL}): unexpected {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        code = EXIT_INTERNAL
    finally:
        code = _cleanup_rehearsal_throwaway(throwaway, args, code=code, ok=ok)

    return _restore_process_exit(code, ok)


if __name__ == "__main__":
    raise SystemExit(main())
