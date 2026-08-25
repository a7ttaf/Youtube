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
    ``--allow-nonempty`` is passed, which switches pg_restore to
    ``--clean --if-exists`` and is therefore destructive.

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
import subprocess
import sys
import time
import traceback
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

_SH_PREFIX = 'export PGPASSWORD="${POSTGRES_PASSWORD:-}"; '

ROLES_PRESENT_SQL = (
    "SELECT rolname FROM pg_catalog.pg_roles "
    "WHERE rolname IN ('app_tenant', 'app_platform') ORDER BY rolname;"
)
PUBLIC_TABLE_COUNT_SQL = "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public';"
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


def _psql(container: str, sql: str, *, timeout: int, stop_on_error: bool = True) -> str:
    """Run SQL via psql inside the container and return stdout."""
    stop = "1" if stop_on_error else "0"
    argv = _container_sh(
        container,
        'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
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
def _load_backup(backup_dir: Path) -> dict[str, object]:
    """Load and integrity-check a backup run; return its manifest dict."""
    if not backup_dir.is_dir():
        raise RestoreError(EXIT_USAGE, f"{backup_dir} is not a directory")
    if backup_dir.name.endswith(REJECTED_SUFFIX):
        raise RestoreError(
            EXIT_USAGE,
            f"{backup_dir.name} is a quarantined run, not a backup. The backup "
            "script rejected it because it captured no application data. "
            "Restoring it would replace the target with an empty database. Pick "
            "an ums-backup-...Z directory instead.",
        )
    manifest_path = backup_dir / MANIFEST_NAME
    for name in (MANIFEST_NAME, ROLES_NAME, DUMP_NAME):
        if not (backup_dir / name).is_file():
            raise RestoreError(
                EXIT_USAGE,
                f"{backup_dir} is not a backup run: {name} is missing. A "
                f"directory still named *.partial was never verified and must "
                f"not be restored.",
            )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RestoreError(EXIT_USAGE, f"cannot read {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RestoreError(EXIT_USAGE, f"{manifest_path} is not a JSON object")
    gate = manifest.get("content_gate")
    if isinstance(gate, dict) and gate.get("status") == "rejected":
        reasons = gate.get("failures")
        detail = "; ".join(str(item) for item in reasons) if isinstance(reasons, list) else ""
        raise RestoreError(
            EXIT_USAGE,
            f"{manifest_path.parent.name} failed the backup content gate "
            f"(tables={gate.get('tables')}, rows={gate.get('rows')}) and is not "
            f"restorable: {detail or 'it captured no application data'}",
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RestoreError(EXIT_USAGE, f"{manifest_path} has no artifacts block")
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
    return manifest


def _require_docker(*, timeout: int) -> None:
    """Fail closed unless the docker CLI can talk to a running daemon."""
    try:
        completed = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=timeout)
    except FileNotFoundError as exc:
        raise RestoreError(EXIT_DOCKER_UNAVAILABLE, "docker CLI not found on PATH") from exc
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
def _create_throwaway(manifest: dict[str, object], *, timeout: int) -> str:
    """Start a disposable Postgres container from the backup manifest source."""
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise RestoreError(EXIT_USAGE, "manifest has no source block")
    image = str(source.get("image") or "")
    database = str(source.get("database") or "")
    superuser = str(source.get("superuser") or "")
    if not image or not database or not superuser:
        raise RestoreError(
            EXIT_USAGE,
            "manifest.source is missing image/database/superuser; pass "
            "--container and restore into a container you prepared yourself",
        )
    name = THROWAWAY_PREFIX + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    completed = _run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "--env",
            f"POSTGRES_USER={superuser}",
            "--env",
            f"POSTGRES_DB={database}",
            image,
        ],
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RestoreError(
            EXIT_CONTAINER_UNAVAILABLE,
            f"could not start the throwaway container from {image}: {completed.stderr.strip()}",
        )
    return name


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
def _restore_roles(container: str, roles_path: Path, *, timeout: int) -> list[str]:
    """Apply roles.sql and return the required roles present afterward."""
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
def _restore_data(container: str, dump_path: Path, *, timeout: int, clean: bool) -> None:
    """Restore database.dump into the target via pg_restore --single-transaction."""
    flags = "--no-password --single-transaction --no-comments"
    if clean:
        flags = "--no-password --clean --if-exists --single-transaction --no-comments"
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
    branches = [
        f"SELECT {_quote_literal(name)} AS t, count(*) AS n FROM public.{_quote_identifier(name)}"
        for name in tables
    ]
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
    expected_raw = manifest.get("table_row_counts")
    expected: dict[str, int] = {}
    if isinstance(expected_raw, dict):
        # FIX: a manifest carrying a non-numeric count raised TypeError/ValueError
        # out of the process. A manifest this script cannot read is a usage
        # failure with an exit code, not a crash.
        try:
            expected = {str(k): int(v) for k, v in expected_raw.items()}
        except (TypeError, ValueError) as exc:
            raise RestoreError(
                EXIT_USAGE,
                f"manifest.table_row_counts holds a value that is not a row count: {exc}",
            ) from exc
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
            "Permit restoring over a database that already has tables. This switches "
            "pg_restore to --clean --if-exists and DROPS the existing objects."
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


def _guard_empty(container: str, *, allow_nonempty: bool, timeout: int) -> None:
    """Refuse a non-empty public schema unless --allow-nonempty was passed."""
    raw = _psql(container, PUBLIC_TABLE_COUNT_SQL, timeout=timeout).strip()
    try:
        existing = int(raw or "0")
    except ValueError as exc:
        raise RestoreError(
            EXIT_RESTORE_FAILED,
            f"could not read the public table count from psql output {raw!r}: {exc}",
        ) from exc
    if existing and not allow_nonempty:
        raise RestoreError(
            EXIT_USAGE,
            f"the target database already has {existing} tables in public. "
            "Restore into an empty database (recreate the container/volume), or "
            "pass --allow-nonempty to DROP and replace the existing objects.",
        )


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
        if args.rehearse:
            throwaway = _create_throwaway(manifest, timeout=args.docker_timeout)
            container = throwaway
            print(f"rehearsal container: {container}")
        else:
            container = _resolve_container(
                explicit=args.container,
                project=args.project,
                service=args.service,
                timeout=args.docker_timeout,
            )
            print(f"target container: {container}")
        _await_postgres(container, wait_seconds=args.wait_for_postgres, timeout=args.docker_timeout)
        # Emptiness is checked before roles.sql is applied: a target this run
        # is going to refuse must not be modified at all, not even by an
        # idempotent CREATE ROLE.
        _guard_empty(container, allow_nonempty=args.allow_nonempty, timeout=args.timeout)
        roles = _restore_roles(container, backup_dir / ROLES_NAME, timeout=args.timeout)
        print(f"roles present after roles.sql: {', '.join(roles)}")
        _restore_data(
            container,
            backup_dir / DUMP_NAME,
            timeout=args.timeout,
            clean=args.allow_nonempty,
        )
        print("pg_restore completed")
        ok = _verify(container, manifest, timeout=args.timeout)
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
        if throwaway and not args.keep_throwaway:
            if _destroy_throwaway(throwaway, timeout=args.docker_timeout):
                print(f"removed rehearsal container {throwaway}")
            else:
                print(
                    f"RESTORE WARNING: failed to remove rehearsal container "
                    f"{throwaway}. Remove it with: "
                    f"docker rm --force --volumes {throwaway}",
                    file=sys.stderr,
                )
                # Promote only when restore+verify would otherwise be success.
                if code is None and ok:
                    code = EXIT_CONTAINER_UNAVAILABLE
        elif throwaway:
            print(f"rehearsal container left running: {throwaway}")
            print(f"remove it with: docker rm --force --volumes {throwaway}")

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


if __name__ == "__main__":
    raise SystemExit(main())
