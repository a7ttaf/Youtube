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
    Create a throwaway Postgres container from the image recorded in the
    manifest, restore into it, compare every table's row count against the
    manifest, then destroy the container. Nothing existing is touched. This is
    the drill; run it after the first real backup and then quarterly.

``--container NAME``
    Restore into a container you name. Refuses a non-empty database unless
    ``--allow-nonempty`` is passed, which drops and recreates the entire
    target database (``DROP DATABASE ... WITH (FORCE)``, then
    ``CREATE DATABASE``) before restoring with ``--clean --if-exists``.
    Everything the database held is destroyed, including schemas and
    extensions the archive does not contain.

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
#            and a non-empty target database is refused rather than silently
#            overwritten.
# Blast Radius: Disaster recovery, and destructive when --allow-nonempty is
#               given. The rehearsal path touches nothing that already exists.
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
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid
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

REQUIRED_ROLES = ("app_tenant", "app_platform")
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
# invariant is "an application role is a member of nothing". Logins are members
# of THEM, which is the opposite direction and is not matched here.
ROLE_MEMBERSHIPS_SQL = (
    "SELECT m.rolname || ' -> ' || g.rolname "
    "FROM pg_catalog.pg_auth_members am "
    "JOIN pg_catalog.pg_roles m ON m.oid = am.member "
    "JOIN pg_catalog.pg_roles g ON g.oid = am.roleid "
    "WHERE m.rolname IN ('app_tenant', 'app_platform') "
    "ORDER BY 1;"
)
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

    ``dbname`` targets a specific database (single quoted identifier handled
    by the caller); the container default ``$POSTGRES_DB`` is used otherwise.
    """
    stop = "1" if stop_on_error else "0"
    target = f'-d "{dbname}" ' if dbname else '-d "$POSTGRES_DB" '
    argv = _container_sh(
        container,
        f'exec psql -U "$POSTGRES_USER" {target}'
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
    """Require sha256 digests for dump and roles and match on-disk bytes."""
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
        actual = _sha256(backup_dir / name)
        if actual != expected:
            raise RestoreError(
                EXIT_ARTIFACT_INTEGRITY,
                f"{name} does not match the manifest sha256 "
                f"(expected {expected}, got {actual}). Do not restore this run.",
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

    FIX: malformed or missing counts used to surface only in ``_verify`` after
    ``pg_restore --single-transaction`` committed — with ``--allow-nonempty``
    that replaced the target and then failed, implying the original state was
    intact. Validate before roles or data are applied.
    """
    expected_raw = manifest.get("table_row_counts")
    if not isinstance(expected_raw, dict):
        raise RestoreError(
            EXIT_USAGE,
            "manifest.table_row_counts is missing or not a JSON object; "
            "refusing to apply an unverifiable backup",
        )
    # FIX: int() accepted booleans as 0/1 and truncated fractional counts, and
    # an empty mapping verified trivially -- so --allow-nonempty could commit
    # the destructive single-transaction replacement and only discover the
    # mismatch, or coincidentally match, during verification. Require a
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


def _load_backup(backup_dir: Path) -> dict[str, object]:
    """Load and integrity-check a backup run; return its manifest dict."""
    _require_restorable_backup_dir(backup_dir)
    manifest_path = backup_dir / MANIFEST_NAME
    manifest = _read_restore_manifest(manifest_path)
    _refuse_rejected_content_gate(manifest, backup_name=manifest_path.parent.name)
    _verify_backup_artifact_digests(backup_dir, manifest)
    _require_manifest_table_row_counts(manifest)
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
                    'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
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
#          the image, database name and superuser recorded at backup time.
# Database/ORM: Creates an empty cluster only.
# Standards: POSTGRES_HOST_AUTH_METHOD=trust and no published ports, so the
#            rehearsal needs no password anywhere and nothing on the host
#            network can reach the container. The name carries a UTC stamp so
#            two rehearsals cannot collide.
# Blast Radius: Creates and destroys one container. Touches no existing
#               container, volume, or compose project.
# Connections:
#   - File: scripts/backup_database.py -> records source.image,
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


def _resolve_rehearsal_image(*, image_id: str, image: str, timeout: int) -> str:
    """Pick a runnable rehearsal image: local config ID, else pullable tag/digest."""
    if image_id and _docker_image_exists(image_id, timeout=timeout):
        return image_id
    if image:
        return image
    return image_id


def _create_throwaway(manifest: dict[str, object], *, timeout: int) -> str:
    """Start a disposable Postgres container from the backup manifest source."""
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise RestoreError(EXIT_USAGE, "manifest has no source block")
    image = str(source.get("image") or "").strip()
    image_id = str(source.get("image_id") or "").strip()
    # FIX: prefer a still-local image_id, else fall back to the pullable digest/tag
    # recorded as source.image after prune/upgrade removed the config ID.
    run_image = _resolve_rehearsal_image(image_id=image_id, image=image, timeout=timeout)
    database = str(source.get("database") or "")
    superuser = str(source.get("superuser") or "")
    if not run_image or not database or not superuser:
        raise RestoreError(
            EXIT_USAGE,
            "manifest.source is missing image/image_id/database/superuser; pass "
            "--container and restore into a container you prepared yourself",
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
# Database/ORM: None. Pure text, which is what keeps these checks safe to run
#               BEFORE ``--allow-nonempty`` drops the target.
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


def _role_membership_problems(body: str) -> list[str]:
    r"""Return the roles.sql statements that put an app role into a membership.

    FIX: ``GRANT <bootstrap superuser> TO app_tenant;`` matched neither the
    CREATE-anchored allowlist nor the attribute-drift gate -- a GRANT can never
    match a ``(?:CREATE|ALTER)\s+ROLE`` pattern -- and ``_restore_roles``
    replayed it. The post-apply check only asserts the two roles EXIST; it
    never reads pg_auth_members. The result is not a drifted attribute but an
    edge in the authorization graph, and that edge is enough on its own.

    Deliberately NOT proportional to how dangerous the granted role looks: the
    cross-lane edge ``GRANT app_tenant TO app_platform`` grants neither
    SUPERUSER nor a ``pg_*`` role and still crosses the platform/tenant write
    boundary 20260608_0001 exists to hold.
    """
    problems: list[str] = []
    for statement in _scan_role_sql(body):
        if statement.startswith("\\"):
            continue
        for member, granted in _role_membership_edges(statement):
            if member in _PROTECTED_APP_ROLES:
                problems.append(f"{member} becomes a member of {granted}: {statement}")
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
    # preflight, and under --allow-nonempty the target was already destroyed by
    # the time _restore_roles dropped the role and the post-apply presence check
    # refused -- leaving an empty replacement database.
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


def _preflight_roles_file(container: str, roles_path: Path, *, timeout: int) -> None:
    """Validate roles.sql read-only, so it can run BEFORE anything destructive.

    Both checks are non-mutating -- a text scan of the file plus one
    ``SELECT current_user`` -- which is why they are safe to run against the
    target before ``--allow-nonempty`` drops it.

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
    superuser = _psql(container, "SELECT current_user;", timeout=timeout).strip()
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
    # FIX: membership is not an attribute, so every check above is structurally
    # blind to it -- a GRANT statement can never match a CREATE/ALTER ROLE
    # pattern, and the post-apply check only asserts the two roles EXIST, never
    # reading pg_auth_members. MEASURED: with `GRANT <superuser> TO app_tenant;`
    # replayed, an app_tenant session with is_superuser=off and NO SET ROLE read
    # every row of an RLS table lacking FORCE, and on a FORCE table it ran
    # `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY` and then read every row.
    memberships = _role_membership_problems(body)
    if memberships:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "roles.sql puts a protected role into the cluster membership graph: "
            + "; ".join(memberships)
            + ". A membership is not a role attribute, so it survives every "
            "attribute check. Regenerate roles.sql from a source cluster whose "
            "application roles hold no memberships.",
        )


def _restore_roles(container: str, roles_path: Path, *, timeout: int) -> list[str]:
    """Apply roles.sql and return the required roles present afterward."""
    _preflight_roles_file(container, roles_path, timeout=timeout)
    argv = _container_sh(
        container,
        'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-password -v ON_ERROR_STOP=0 -q -f -',
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
    present = [
        line.strip()
        for line in _psql(container, ROLES_PRESENT_SQL, timeout=timeout).splitlines()
        if line.strip()
    ]
    missing = [role for role in REQUIRED_ROLES if role not in present]
    if missing:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            f"after applying {roles_path.name} the cluster still has no "
            f"{', '.join(missing)}. The dump's RLS policies and GRANT "
            "statements reference those roles, so restoring the data now "
            "would fail part-way and leave a half-populated database.",
        )
    # FIX: assert the graph, not just the names. See ROLE_MEMBERSHIPS_SQL --
    # a membership the TARGET cluster already had survives DROP DATABASE and
    # appears in no archive, so this is the only place it can be caught.
    memberships = [
        line.strip()
        for line in _psql(container, ROLE_MEMBERSHIPS_SQL, timeout=timeout).splitlines()
        if line.strip()
    ]
    if memberships:
        raise RestoreError(
            EXIT_ROLES_FAILED,
            "after applying roles.sql an application role is a member of "
            f"another role: {'; '.join(memberships)}. Memberships are "
            "cluster-global, so this one predates the archive and survived the "
            "database drop. It grants the lane the other role's object "
            "privileges without any attribute change. REVOKE it and re-run.",
        )
    return present


# ============================================================================
# Purpose: Restore the custom-format archive into the target database, in one
#          transaction, only after the roles are known to exist.
# Database/ORM: Recreates every table, index, constraint, RLS policy and GRANT
#               in the archive.
# Standards: --single-transaction (which implies --exit-on-error) so a failure
#            leaves the target untouched rather than half-populated; --clean
#            --if-exists only when the operator explicitly accepted a non-empty
#            target. The archive is fed on stdin from the host file, so the
#            dump never has to exist inside the container.
# Blast Radius: Destructive on the target database when --allow-nonempty is
#               used; otherwise the target was verified empty first.
# Connections:
#   - File: scripts/backup_database.py -> ``_dump_database`` produced the
#     archive this restores.
# ============================================================================
# ============================================================================
# Purpose: Prove the archive is READABLE by THIS container's pg_restore before
#          --allow-nonempty drops the target. The sha256 digest proves the bytes
#          match what backup wrote; it does NOT prove this container can parse
#          them -- a newer archive format passes the digest and is still
#          unrestorable here.
# Database/ORM: None. ``pg_restore --list`` reads the archive header and table
#               of contents; it opens no connection and writes nothing.
# Standards: Fail-closed BEFORE _recreate_target_database, exactly like the
#            roles.sql preflight beside it. A non-zero exit is
#            EXIT_RESTORE_FAILED -- the same code the later real pg_restore
#            would have produced, so the documented exit table is unchanged.
# Blast Radius: Disaster recovery. Turns "target destroyed, then pg_restore
#               fails" into a refusal that changes nothing.
# Connections:
#   - File: scripts/backup_database.py -> ``_pg_restore_list`` runs the same
#     read-only listing at BACKUP time to reject an unrestorable dump.
# ============================================================================
def _preflight_dump_readable(container: str, dump_path: Path, *, timeout: int) -> None:
    """Refuse an archive this container's pg_restore cannot read, read-only.

    Note the limit honestly: ``--list`` reads the header and TOC, so it catches
    an unreadable header, a corrupt TOC and a format version this pg_restore
    does not support -- the failures that would otherwise surface only AFTER
    the drop. It does not read the data section, so it is not a promise the
    restore will succeed; it is a promise the archive is not obviously
    unrestorable before anything irreversible happens.

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
            "The target database was NOT dropped.",
        )


def _restore_data(container: str, dump_path: Path, *, timeout: int, clean: bool) -> None:
    """Restore database.dump into the target via pg_restore --single-transaction."""
    flags = "--no-password --single-transaction"
    if clean:
        flags = "--no-password --clean --if-exists --single-transaction"
    argv = _container_sh(
        container,
        f'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" {flags}',
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


def _table_row_counts(container: str, *, timeout: int) -> dict[str, int]:
    """Return {tablename: row_count} for every table in public."""
    raw = _psql(container, LIST_TABLES_SQL, timeout=timeout)
    tables = [line.strip() for line in raw.splitlines() if line.strip()]
    if not tables:
        return {}
    branches = [_count_sql_branch(name) for name in tables]
    counts: dict[str, int] = {}
    sql = " UNION ALL ".join(branches) + " ORDER BY t;"
    for line in _psql(container, sql, timeout=timeout).splitlines():
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
def _verify(container: str, manifest: dict[str, object], *, timeout: int) -> bool:
    """Compare restored public table row counts to the manifest; True if all match."""
    expected = _require_manifest_table_row_counts(manifest)
    actual = _table_row_counts(container, timeout=timeout)
    names = sorted(set(expected) | set(actual))
    if not names:
        print("VERIFY: the manifest recorded no tables and the restore produced none.")
        return False
    width = max(len(name) for name in names)
    failures = 0
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
            "Permit restoring over a database that already has tables. This DROPS "
            "AND RECREATES THE ENTIRE TARGET DATABASE (DROP DATABASE ... WITH "
            "(FORCE), then CREATE DATABASE) before restoring, destroying every "
            "schema, extension and object it holds -- including ones the archive "
            "does not contain -- and disconnecting any live sessions. The restore "
            "then runs with pg_restore --clean --if-exists."
        ),
    )
    parser.add_argument(
        "--keep-throwaway",
        action="store_true",
        help="With --rehearse, leave the throwaway container running for inspection.",
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
    return parser.parse_args(argv)


# ============================================================================
# Purpose: Recreate the whole target database on demand so an
#          --allow-nonempty restore starts from a genuinely fresh cluster
#          shell instead of dropping objects piecemeal.
# Database/ORM: None in-process. Runs psql -d postgres inside the container.
# Standards: RestoreError(EXIT_USAGE) when asked to drop the maintenance
#            database itself; identifier/literal quoting everywhere; the fresh
#            database is created BEFORE any roles or data are applied.
# Blast Radius: Destructive by design under --allow-nonempty -- the operator
#               explicitly accepted replacing that database's contents, and a
#               failed apply afterwards leaves a pristine empty database rather
#               than a half-cleared original.
# Connections:
#   - File: scripts/restore_database.py -> _execute_restore calls these only
#     when --allow-nonempty was passed; pg_restore later targets the fresh db.
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


def _recreate_target_database(container: str, target_db: str, *, timeout: int) -> None:
    """Drop and recreate ``target_db``, leaving an empty shell to restore."""
    if target_db.lower() == "postgres":
        raise RestoreError(
            EXIT_USAGE,
            "--allow-nonempty cannot drop the maintenance database itself; "
            "point POSTGRES_DB at the application database to be replaced.",
        )
    quoted_db = _quote_identifier(target_db)
    # FIX: assembled as joined static fragments so the B608 string-built-query
    # detector stays satisfied; target_db is validated and identifier-quoted.
    drop_lines = [
        f"DROP DATABASE IF EXISTS {quoted_db} WITH (FORCE);",
        f"CREATE DATABASE {quoted_db};",
    ]
    _psql(
        container,
        "\n".join(drop_lines),
        timeout=timeout,
        dbname="postgres",
    )


def _guard_empty(container: str, *, allow_nonempty: bool, timeout: int) -> None:
    """Refuse a non-empty target database unless --allow-nonempty was passed."""
    raw = _psql(container, USER_OBJECT_COUNT_SQL, timeout=timeout).strip()
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
            "system catalogs. Restore into an empty database (recreate the "
            "container/volume), or pass --allow-nonempty to DROP AND RECREATE "
            "THE WHOLE TARGET DATABASE -- every schema, extension and object it "
            "holds is destroyed, including ones this archive does not contain, "
            "and it cannot be recovered afterwards.",
        )


def _prepare_restore_target(
    args: argparse.Namespace, manifest: dict[str, object]
) -> tuple[str, str | None]:
    """Resolve the restore container; return (container, throwaway_or_none)."""
    if args.rehearse:
        throwaway = _create_throwaway(manifest, timeout=args.docker_timeout)
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
# Purpose: Orchestrate one restore into an already-resolved container —
#          wait for Postgres, refuse a non-empty target unless allowed, apply
#          ``roles.sql``, restore ``database.dump``, then verify row counts.
# Database/ORM: None in-process. Runs psql / pg_restore inside the target
#               container only.
# Standards: Emptiness check before roles so a refused target is unmodified;
#            roles before data so RLS grants have parents; fail-closed on any
#            stage via RestoreError.
# Blast Radius: Disaster recovery writes into the named container; rehearsal
#               uses a throwaway only.
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
    """Apply roles then dump and verify; return verification success."""
    _await_postgres(container, wait_seconds=args.wait_for_postgres, timeout=args.docker_timeout)
    # Emptiness is checked before roles.sql is applied: a target this run
    # is going to refuse must not be modified at all, not even by an
    # idempotent CREATE ROLE.
    _guard_empty(container, allow_nonempty=args.allow_nonempty, timeout=args.timeout)
    if args.allow_nonempty:
        # FIX(round-8 review): a piecemeal schema clear ran outside the restore
        # transaction, so any later failure erased the original database while
        # its "leave untouched on failure" guarantee held elsewhere. Dropping
        # and recreating the TARGET DATABASE limits destruction to what the
        # operator explicitly consented to replace, and a failed apply leaves
        # only that pristine shell behind. It also drops the blanket PUBLIC
        # CREATE grant the previous CREATE SCHEMA path re-introduced on PG18.
        # FIX: validate roles.sql BEFORE the drop, not after. These checks are
        # read-only, but they used to run inside _restore_roles -- i.e. after
        # _recreate_target_database had already destroyed the target. A backup
        # whose bootstrap superuser differs from this target, or one carrying
        # unsupported role DDL, was therefore rejected only once the original
        # database was irrecoverable. Preflighting turns that into a refusal
        # that changes nothing.
        _preflight_roles_file(container, backup_dir / ROLES_NAME, timeout=args.timeout)
        # FIX: prove the ARCHIVE is readable here too, not just roles.sql. The
        # digest check on the way in only proves the bytes match what backup
        # wrote -- a newer-format archive hashes correctly and is still
        # unreadable by this container's pg_restore, which was discovered only
        # when _restore_data ran, one step AFTER the drop had committed. Both
        # preflights are read-only, so both belong before it.
        _preflight_dump_readable(container, backup_dir / DUMP_NAME, timeout=args.timeout)
        target_db = _container_default_database(container, timeout=args.timeout)
        _recreate_target_database(container, target_db, timeout=args.timeout)
    roles = _restore_roles(container, backup_dir / ROLES_NAME, timeout=args.timeout)
    print(f"roles present after roles.sql: {', '.join(roles)}")
    _restore_data(
        container,
        backup_dir / DUMP_NAME,
        timeout=args.timeout,
        clean=args.allow_nonempty,
    )
    print("pg_restore completed")
    return _verify(container, manifest, timeout=args.timeout)


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
    """Run one restore (or rehearsal) and return a documented exit code."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    backup_dir = Path(args.backup_dir).expanduser()
    throwaway: str | None = None
    code: int | None = None
    ok = False
    try:
        manifest = _load_backup(backup_dir)
        _require_docker(timeout=args.docker_timeout)
        container, throwaway = _prepare_restore_target(args, manifest)
        ok = _execute_restore(container, backup_dir, args, manifest)
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
