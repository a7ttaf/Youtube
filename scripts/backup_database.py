#!/usr/bin/env python
"""Host-side ``pg_dump`` backup for the UMS Smart Revenue Postgres container.

Writes one timestamped run directory per invocation to a **host** path::

    <out-dir>/ums-backup-YYYYMMDDTHHMMSSZ/
        database.dump   pg_dump --format=custom of the application database
        roles.sql       pg_dumpall --roles-only (app_tenant / app_platform)
        manifest.json   provenance, sha256 digests, and per-table row counts

``pg_dump`` does not dump roles. The custom dump *does* carry the RLS policies
and the ``GRANT ... TO app_tenant`` / ``app_platform`` statements installed by
``db/alembic/versions/20260608_0001_tenant_rls_enforcement.py``, so restoring
it into a cluster where those two roles do not exist fails part-way through and
leaves a half-populated database. ``roles.sql`` is what makes the dump
restorable, and this script refuses to publish a run whose ``roles.sql`` does
not name both roles.

Credentials: the host process never learns a database password. Every database
command runs inside the Postgres container via ``docker exec``, and the
password is expanded from the container's own ``POSTGRES_PASSWORD`` by the
container's shell -- so it never reaches this process's argv, this process's
environment, the host process listing, or ``backup.log``.

Stdlib only, no repository imports: the backup has to keep working while the
checkout is mid-rebase or the application image will not build.

Shell: none. Runs under Windows PowerShell, cmd.exe, and bash/zsh on macOS.
Requires Python 3.11+ and the ``docker`` CLI on PATH. It deliberately does not
read ``docker-compose.yml``: compose would refuse to interpolate that file
without ``UMS_DB_USER`` and friends, and a scheduled task inherits almost no
environment.

Content gate: a backup that captured no data is a failure, not a success. Four
tiers decide, and they have different authority:

* **The seed floor** is absolute and has no override. Every UMS migration path
  ends with the six tables in ``SEED_TABLES`` populated, so any of them empty
  means the dump is not of a working UMS database. A bare row count cannot do
  this job: a virgin ``alembic upgrade head`` measures 38 tables / **328** rows,
  so the old ``MIN_ROWS = 1`` accepted a database truncated to nothing but its
  Alembic stamp.
* **The identity binding** is overridable only by ``--adopt-database``. An
  output directory holds the history of exactly ONE database, and nothing about
  a row count can notice that tonight's rows came from a different one. The
  Postgres cluster's ``system_identifier`` plus the database name are recorded
  on the first accepted run and re-checked on every run after it.
* **The watermark** is a persistent per-table high-water mark kept in
  ``watermark.json``, not a comparison against last night. A reference that
  re-anchors on every accepted run bounds nothing: 80% loss a night, three
  nights running, is three green runs and 180 rows gone.
* **The first-run acknowledgement.** With no watermark at all -- a brand new
  output directory -- nothing can tell a healthy database from one that was
  wiped and re-migrated, because those two databases are byte-for-byte the same
  shape. That run is therefore refused until the operator confirms the printed
  numbers with ``--establish-watermark`` -- and if every table outside
  ``SEED_TABLES`` is empty, that flag alone is refused too, because "wiped and
  re-migrated" is then not a possibility but the measured state.

A run that fails is *quarantined*: renamed ``...Z.rejected`` rather than
``...Z``, so it can never be mistaken for a backup, never protects itself from
retention, never contributes to the watermark, and is refused by
``restore_database.py``. The process then exits 8, which is what the operator
sees in Task Scheduler's Last Run Result.

Status durability: ``last-run.json`` is rewritten to ``RUNNING`` before any work
starts and to a terminal verdict on the way out, including from a ``finally``
block. A crashed, killed or internally-failed run therefore cannot leave the
previous run's green ``OK`` standing as if it were this run's result. When the
file itself cannot be replaced -- another process holding it with
``FileShare.None`` is the ordinary Windows cause -- the write is retried, then
this run's record is written to a stamped ``last-run-<stamp>.json`` beside it,
and an otherwise-successful run exits 7 instead of 0 so the one channel that
always works, the process exit code, still says something is wrong.

Exit codes (surfaced verbatim as Task Scheduler's "Last Run Result"):

    0  backup written and verified
    2  usage / output directory unusable
    3  Docker daemon unavailable (see --wait-for-docker)
    4  Postgres container not running or not accepting connections
    5  pg_dump / pg_dumpall / psql failed
    6  an artifact failed verification -- the run directory was discarded
    7  the backup IS published, but watermark, retention or status bookkeeping
       failed -- including "last-run.json could not be replaced"
    8  the backup captured no application data -- the run was quarantined
    9  unexpected internal error -- backup.log names it; see also last-run.json

Usage::

    python scripts/backup_database.py --out-dir D:/UMS-Backups
    python scripts/backup_database.py --out-dir D:/UMS-Backups --wait-for-docker 900
"""

# ============================================================================
# Purpose: Operator CLI that takes a verified, timestamped, restorable backup
#          of the beta Postgres container to a host directory, judges what it
#          captured against a seed floor and a persistent watermark, prunes
#          expired runs, and fails loudly rather than leaving an artifact that
#          looks healthy and is not.
# Database/ORM: None in-process. Reads the live database only through
#               ``pg_dump``/``pg_dumpall``/``psql`` executed inside the
#               Postgres container; no SQLAlchemy, no ORM, no repository.
# Standards: Stdlib only; no secret enters argv, this process's environment or
#            the log; every external command is an argv list, never
#            ``shell=True``; artifacts are staged under ``.partial`` and
#            renamed only after verification; one exit code per failure class.
#            "Verified" includes the payload, not only its container: a run that
#            captured no application data is quarantined as ``.rejected`` and
#            exits 8 rather than being published as OK. Every exit path writes
#            exactly one ``backup.log`` line and one terminal ``last-run.json``,
#            enforced by ``_RunReport`` rather than by remembering to call it.
# Blast Radius: Disaster recovery. A silent failure here is unrecoverable data
#               loss, so every check is fail-closed. Writes nothing to the
#               database; the only deletions are its own expired run
#               directories beneath --out-dir.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py
#     -> ``_create_role`` (lines 92-113) creates app_tenant/app_platform, and
#     the grant block (lines 300-333) is why roles.sql is a prerequisite.
#   - File: backend/ums_smart_revenue/db/iso_4217_2026_05.py -> the 178-entry
#     snapshot migration 20260523_0001 seeds into ``currencies``, which is why
#     ``currencies`` is a seed table.
#   - File: backend/ums_smart_revenue/db/alembic/versions/20260516_0001_tenants_foundation.py
#     -> the bootstrap tenant insert is why ``tenants`` is a seed table.
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260825_0001_security_role_permission_seed.py -> seeds ``roles``,
#     ``permissions`` and ``role_permission_assignments`` from the live auth
#     registries, which is why those three are seed tables.
#   - File: scripts/restore_database.py -> Consumes this layout, roles first.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> Operator runbook,
#     rehearsal procedure, and Windows Task Scheduler wiring.
# ============================================================================
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DOCKER_UNAVAILABLE = 3
EXIT_CONTAINER_UNAVAILABLE = 4
EXIT_COMMAND_FAILED = 5
EXIT_ARTIFACT_INVALID = 6
EXIT_BOOKKEEPING_FAILED = 7
EXIT_NO_CONTENT = 8
EXIT_INTERNAL = 9

DEFAULT_PROJECT = "ums-smart-revenue"
DEFAULT_SERVICE = "postgres"

DUMP_NAME = "database.dump"
ROLES_NAME = "roles.sql"
MANIFEST_NAME = "manifest.json"
LOG_NAME = "backup.log"
LAST_RUN_NAME = "last-run.json"
WATERMARK_NAME = "watermark.json"
# Written only when LAST_RUN_NAME itself could not be replaced. A NEW file name is
# the one write that a share-mode lock on the canonical file cannot block, and the
# stamp means it is never itself stale.
LAST_RUN_SIDECAR = "last-run-{stamp}.json"
# A share-mode lock from an AV scanner, OneDrive or an open editor is measured in
# seconds, so a bounded retry converts most of them into a normal run. It is
# deliberately short: this runs inside the 02:00 task's time budget.
STATUS_WRITE_ATTEMPTS = 5
STATUS_WRITE_BACKOFF_SECONDS = 0.5

MANIFEST_SCHEMA = "ums-backup/1"
WATERMARK_SCHEMA = "ums-backup-watermark/1"

# The two cluster roles created by 20260608_0001. Both are NOLOGIN, which is
# why --no-role-passwords costs nothing here: it keeps SCRAM verifiers for the
# login roles out of a plaintext file sitting on the operator's disk without
# losing anything the restore needs.
REQUIRED_ROLES = ("app_tenant", "app_platform")

# pg_dump --format=custom archives start with this magic. A cheap truncation
# and wrong-format guard; the authoritative check is pg_restore --list.
CUSTOM_FORMAT_MAGIC = b"PGDMP"

RUN_DIR_TEMPLATE = "ums-backup-{stamp}Z"
RUN_DIR_RE = re.compile(r"^ums-backup-(\d{8}T\d{6})Z$")
PARTIAL_SUFFIX = ".partial"
PARTIAL_RE = re.compile(r"^ums-backup-\d{8}T\d{6}Z\.partial$")
# A run that failed the content gate keeps its artifacts for diagnosis but is
# renamed out of RUN_DIR_RE's namespace. That single fact does most of the work:
# it is not a backup to ``_prune``, not a watermark contribution to the next run,
# and not restorable to ``restore_database.py``.
REJECTED_SUFFIX = ".rejected"
REJECTED_RE = re.compile(r"^ums-backup-(\d{8}T\d{6})Z\.rejected$")
STAMP_FORMAT = "%Y%m%dT%H%M%S"
# How far ahead of now a run directory's own stamp may sit and still be read as
# history. A stamp is written by THIS script from THIS box's clock, so one in
# the future did not come from a run that already happened. Both processes read
# the same clock, so the only honest source of a positive difference is a
# BACKWARD correction between the two readings -- NTP stepping a drifting RTC,
# or a VM resumed from a snapshot. Those are seconds to a couple of minutes;
# five is comfortably above them and still 288x below the nightly cadence, so
# "tomorrow" can never be inside it. See ``_run_stamp``.
STAMP_FUTURE_TOLERANCE = timedelta(minutes=5)

# ---------------------------------------------------------------------------
# The absolute floor. Deliberately NOT operator-tunable and with no override
# flag, because there is no legitimate UMS backup that fails it.
#
# SEED_TABLES is "every table a fresh `alembic upgrade head` populates", and it
# has TWO jobs. The floor is the visible one. The load-bearing one is that
# ``_non_seed_rows`` -- everything OUTSIDE this tuple -- is how tier 3b decides
# that a first run is looking at a database with no application data in it at
# all. A table that the migrations seed but that is MISSING from this tuple
# therefore reads as application data, and tier 3b stops firing.
#
# MEASURED, not assumed. `alembic upgrade head` against
# postgres:18-alpine@sha256:96d56f7f (the compose pin), head revision
# 20260825_0001, measured 2026-08-25: 38 tables, 328 rows, and exactly six
# tables hold any of them --
#     currencies 178   role_permission_assignments 106   permissions 26
#     roles 16         alembic_version 1                 tenants 1
#
# It has been wrong twice, in the same direction both times, so the direction is
# the thing to watch:
#   * MIN_ROWS = 1 was justified by "a freshly migrated database has one stamp
#     row". It was 180. That let a database truncated to nothing but its Alembic
#     stamp publish a green OK.
#   * The three-name tuple was correct until P0.7's roles/permissions seed
#     migration (20260825_0001) landed 148 rows into three tables that were not
#     in it. ``non_seed_rows`` went 0 -> 148 on a VIRGIN database, so tier 3b's
#     "every table outside SEED_TABLES is EMPTY" stopped being true of an empty
#     database, and `docker compose down -v` + auto-migrate + one
#     --establish-watermark would have made an empty database the directory's
#     permanent reference -- the exact self-perpetuating failure tier 3b exists
#     to stop. tests/scripts/test_backup_content_gate.py derives the seeded set
#     from the migration sources so the next one goes red here instead.
#
# Non-empty rather than a hardcoded count, on purpose. Coupling to 178 would
# make an ordinary refresh of the frozen ISO-4217 snapshot
# (backend/ums_smart_revenue/db/iso_4217_2026_05.py -- the name carries its own
# expiry date) break every backup on the box. Row-count regressions are the
# watermark's job; existence is this floor's job, and they do not overlap.
#
# WHAT THIS COSTS, stated because it is a real operator event and not a
# hypothetical: these six are also held to their EXACT high-water mark by the
# seed-shrink rule in ``_evaluate_content``. Retiring a role or a permission
# (precedent: 20260513_0002_retire_graph_permissions) shrinks one of them, so
# the first night after that deploy exits 8 and the operator clears it with ONE
# --accept-content-drop run, exactly as an ISO-4217 refresh would. That is
# bounded and self-clearing -- ``reset_after`` means the night after needs no
# flag -- and it is the same cost the tuple already carried for `currencies`.
# Proved, not reasoned: tests/scripts/test_backup_content_gate.py has
# ``test_retiring_a_permission_costs_exactly_one_override_night``, which walks
# red -> one override night -> green again with no flag.
#
# KNOWN, UNCLOSED, AND UNREACHABLE HERE -- widening this tuple reclassifies runs
# that were published BEFORE the widening. ``_run_has_content`` re-tests a run's
# recorded counts against this floor, and a manifest written before
# 20260825_0001 has no ``roles`` key at all, so ``counts.get(name, 0) > 0``
# reads it as 0 and the run becomes PROVEN EMPTY: it stops consuming a
# --keep-min slot and becomes eligible for deletion by age. That is the wrong
# direction on the one destructive path in this script.
#   * Unreachable in this deployment: no backup directory predates this script's
#     first release, which is the same release as 20260825_0001.
#   * NOT fixed here on purpose. The obvious fix -- "a seed table absent from a
#     manifest is schema drift, so return None (unknown) rather than False" --
#     also swallows the ``{}`` case, which is a dropped schema and the exact
#     fixture retention invariant 2 is built on
#     (``test_empty_runs_do_not_consume_a_keep_min_slot``). Trading that
#     invariant away to close a hazard nothing can reach today would be a guard
#     lost for a guard gained.
#   * FOR THE NEXT WIDENING: decide this deliberately, with a reject->accept
#     matrix over ``_prune``, before adding a seventh name. The likely shape is a
#     schema-generation stamp in the manifest rather than a heuristic over which
#     keys are present.
# ---------------------------------------------------------------------------
MIN_TABLES = 1
SEED_TABLES: tuple[str, ...] = (
    "alembic_version",
    "currencies",
    "permissions",
    "role_permission_assignments",
    "roles",
    "tenants",
)

# Relative collapse, measured against the PERSISTENT watermark rather than
# against last night's run. See ``_load_watermark``.
COLLAPSE_ROW_FRACTION = 0.10
# Below this, a per-table fraction is noise: a table whose high-water mark is 9
# rows cannot lose 90% without being caught by the emptied-table rule anyway.
TABLE_COLLAPSE_MIN_ROWS = 10
# How many watermark resets to keep in watermark.json for forensics.
WATERMARK_RESET_HISTORY = 20
# How many table names to name in one grouped failure message before summarising.
MAX_NAMED_TABLES = 6

# Everything that touches the database runs behind this prefix, so the password
# is expanded by the container's shell and never appears on the host.
# --no-password on each client is not decoration: without it a misconfigured
# pg_hba turns an unattended 02:00 task into a process blocked forever on a
# password prompt, which reads as "still running" rather than "failed".
_SH_PREFIX = 'export PGPASSWORD="${POSTGRES_PASSWORD:-}"; '

LIST_TABLES_SQL = (
    "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public' ORDER BY tablename;"
)

# The cluster's own identity, written into pg_control by initdb. MEASURED against
# postgres:18-alpine: unchanged across `docker restart` (same volume, same value
# 7677783130042306599), and different for a second container built the same way
# (7677783226065739815). That is exactly the discrimination this needs -- ordinary
# container churn is not a change of database, and a second cluster is. Available
# since PostgreSQL 9.6.
IDENTITY_SQL = "SELECT system_identifier FROM pg_control_system();"

ROW_COUNT_NOTE = (
    "Counted on the same REPEATABLE READ snapshot that pg_dump used "
    "(pg_export_snapshot / --snapshot). Live writes during the dump do not "
    "inflate these counts past what the archive holds."
)


class BackupError(Exception):
    """Fatal backup failure carrying the exit code the operator should see."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run one external command with no stdin, capturing both streams."""
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
    """Run one external command, feeding it SQL on stdin."""
    return subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_to_file(
    argv: list[str], *, timeout: int, target: Path
) -> subprocess.CompletedProcess[str]:
    """Stream a command's stdout straight to a host file, keeping stderr."""
    with target.open("wb") as sink:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )


def _container_sh(container: str, body: str) -> list[str]:
    return ["docker", "exec", "-i", container, "sh", "-c", _SH_PREFIX + body]


def _psql(container: str, sql: str, *, timeout: int) -> str:
    """Run SQL in the container and return raw ``psql -At`` output."""
    argv = _container_sh(
        container,
        'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
        "--no-password -v ON_ERROR_STOP=1 -Atq -f -",
    )
    completed = _run_with_input(argv, timeout=timeout, stdin_text=sql)
    if completed.returncode != 0:
        raise BackupError(
            EXIT_COMMAND_FAILED,
            f"psql failed inside {container}: {completed.stderr.strip()}",
        )
    return completed.stdout


def _parse_stamp(name: str, pattern: re.Pattern[str] = RUN_DIR_RE) -> datetime | None:
    """The calendar half of ``_run_stamp``: does this name carry a real date?

    Separate from ``_run_stamp`` only so ``_prune`` can tell a name that is not a
    date from a name that is a date in the future, and warn about the right one.
    Nothing else should call it -- reading history is ``_run_stamp``'s job.
    """
    # FIX: parse-and-validate instead of regex-and-assume. The regex cannot
    # express "is a calendar date", so the parse is the validation.
    match = pattern.match(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


# ============================================================================
# Purpose: Turn a run directory name into the instant it claims, or None when
#          the name matches the shape but cannot be a run that already happened
#          -- it is not a real date, or it is dated in the future.
# Database/ORM: None.
# Standards: THE single gate every history-reading path goes through, which is
#            why the future rule lives here rather than in each caller.
#
#            1. ``RUN_DIR_RE`` accepts any ``\\d{8}T\\d{6}``, so
#               ``ums-backup-20250145T999999Z`` matches and ``strptime`` raises
#               ValueError on it. An uncaught ValueError here used to take the
#               whole process down with an undocumented exit 1 *after* the
#               backup had already been written.
#            2. A stamp more than ``STAMP_FUTURE_TOLERANCE`` ahead of now is
#               refused. MEASURED, and the reason this rule exists: a single
#               planted ``ums-backup-20990101T000000Z`` used to be permanent.
#               ``_load_watermark`` excludes runs at or older than
#               ``reset_after``, and ``reset_after`` is only ever set to the
#               name of the run that carried the override -- so a name that
#               sorts ABOVE every real run is never excluded and re-folds its
#               inflated counts into the mark every night. The measured cycle
#               was exit 8 / exit 0 with --accept-content-drop / exit 8 again,
#               forever, whose only sustainable end is that flag living in the
#               scheduled task -- i.e. the tier-2 comparison switched off for
#               good. It also captured BOTH retention invariants at once: the
#               lexicographic sort made it the ``--keep-min`` tail AND the
#               invariant-1 "newest with content" pin, and one prune with
#               ``--keep-days 0 --keep-min 1`` deleted all three real runs and
#               kept only the plant.
#            3. No attacker is needed for either. A clock ahead at 02:00 -- a
#               dead RTC, a VM restored from a snapshot, NTP not yet converged
#               -- stamps one directory in the future, and correcting the skew
#               is what makes it outrank every run after it.
#            4. The refusal costs an ignored directory, never a deleted one:
#               None means "not a run this script produced", and ``_prune``
#               treats that as untouchable. A GENUINE run stamped while the
#               clock was ahead therefore stops contributing to the watermark
#               until real time passes its stamp, then contributes again. That
#               direction is deliberate: an unverifiable stamp may lower the
#               protection it offers, never raise the bar it sets.
# Blast Radius: Disaster recovery. Decides what retention may delete and what
#               may raise the watermark.
# Connections:
#   - File: scripts/backup_database.py -> ``_prune``, ``_load_watermark`` and
#     ``_load_identity`` are the three history readers it guards.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> retention section.
# ============================================================================
def _run_stamp(
    name: str,
    pattern: re.Pattern[str] = RUN_DIR_RE,
    *,
    now: datetime | None = None,
) -> datetime | None:
    stamped = _parse_stamp(name, pattern)
    if stamped is None:
        return None
    reference = _utc_now() if now is None else now
    if stamped - reference > STAMP_FUTURE_TOLERANCE:
        return None
    return stamped


# ============================================================================
# Purpose: Refuse to proceed until the Docker daemon answers, because Docker
#          Desktop starts at user login rather than at boot; a task that fires
#          before it is up must fail loudly, not quietly do nothing.
# Database/ORM: None.
# Standards: Bounded polling against an explicit deadline; exit code 3 so
#            "Docker was down" is distinguishable from "the dump failed" in
#            Task Scheduler's Last Run Result column.
# Blast Radius: Disaster recovery -- a false success here is precisely the
#               failure mode this script exists to prevent.
# Connections:
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> records why the task
#     passes --wait-for-docker instead of trusting the clock.
# ============================================================================
def _await_docker(wait_seconds: int, *, timeout: int) -> str:
    deadline = time.monotonic() + max(wait_seconds, 0)
    last_error = "docker CLI not found on PATH"
    while True:
        completed: subprocess.CompletedProcess[str] | None
        try:
            completed = _run(
                ["docker", "version", "--format", "{{.Server.Version}}"], timeout=timeout
            )
        except FileNotFoundError:
            completed = None
        except subprocess.TimeoutExpired:
            completed = None
            last_error = "docker version timed out"
        if completed is not None:
            if completed.returncode == 0:
                return completed.stdout.strip()
            last_error = completed.stderr.strip() or "docker daemon did not respond"
        if time.monotonic() >= deadline:
            raise BackupError(
                EXIT_DOCKER_UNAVAILABLE,
                f"Docker daemon is not available, so NO backup was taken: {last_error}",
            )
        time.sleep(5)


def _resolve_container(
    *,
    explicit: str | None,
    project: str,
    service: str,
    wait_seconds: int,
    timeout: int,
) -> str:
    """Find the running Postgres container by compose label, or by name."""
    if explicit:
        completed = _run(
            ["docker", "inspect", "--format", "{{.State.Running}}", explicit],
            timeout=timeout,
        )
        if completed.returncode != 0 or completed.stdout.strip() != "true":
            raise BackupError(
                EXIT_CONTAINER_UNAVAILABLE,
                f"container {explicit!r} is not running: {completed.stderr.strip()}",
            )
        return explicit
    deadline = time.monotonic() + max(wait_seconds, 0)
    last_error = ""
    while True:
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
        last_error = completed.stderr.strip() or last_error
        ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if len(ids) > 1:
            raise BackupError(
                EXIT_CONTAINER_UNAVAILABLE,
                f"{len(ids)} running containers match project={project} "
                f"service={service}; pass --container to disambiguate",
            )
        if ids:
            return ids[0]
        if time.monotonic() >= deadline:
            raise BackupError(
                EXIT_CONTAINER_UNAVAILABLE,
                f"no running container matches project={project} service={service}, "
                f"so NO backup was taken. {last_error}".strip(),
            )
        time.sleep(5)


# ============================================================================
# Purpose: Wait for the *real* server, not the initdb bootstrap server.
# Database/ORM: Connection probe plus one ``SELECT 1`` on the target database.
# Standards: Readiness means two things in the same iteration -- the server
#            answers on TCP AND a real query against the target database
#            succeeds. A socket-only ``pg_isready`` is not enough: the official
#            postgres image runs a temporary server during first-boot
#            initialisation that listens on the unix socket only, so
#            ``pg_isready`` reports ready inside a window where the very next
#            command dies with "the database system is shutting down".
#            Observed while rehearsing this script against a fresh container.
# Blast Radius: A false ready here aborts a scheduled backup, or worse aborts a
#               restore part-way, so the probe is deliberately strict.
# Connections:
#   - File: scripts/restore_database.py -> ``_await_postgres`` is the same probe
#     on the restore side and must stay in step with this one.
# ============================================================================
def _await_postgres(container: str, *, wait_seconds: int, timeout: int) -> None:
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
            raise BackupError(
                EXIT_CONTAINER_UNAVAILABLE,
                f"Postgres in {container} is not accepting connections: {last_error}",
            )
        time.sleep(5)


# ============================================================================
# Purpose: Collect the non-secret provenance recorded in ``manifest.source``,
#          including the two fields that identify WHICH database this run read.
# Database/ORM: Reads the container's environment and pg_control via psql; no
#               application table is touched.
# Standards: Nothing here is a secret -- image, database name, superuser name,
#            server version and the cluster's system_identifier. The identity
#            pair is (system_identifier, database): the first is written by
#            initdb and survives container recreation, the second separates two
#            databases inside one cluster. Neither is derivable from row counts,
#            which is why the content gate could not see a wrong-database run
#            before this was wired to it.
# Blast Radius: Disaster recovery. ``_evaluate_content`` refuses on a mismatch,
#               so a fact that is silently absent degrades to "unknown identity"
#               and the check cannot run -- it never degrades to "matches".
# Connections:
#   - File: scripts/backup_database.py -> ``_identity_from_source`` reads these
#     keys back out of a manifest; ``_load_identity`` rebuilds from them.
#   - File: scripts/restore_database.py -> ``_create_throwaway`` reads
#     ``source.image`` / ``database`` / ``superuser`` from the same block.
# ============================================================================
def _container_facts(container: str, *, timeout: int) -> dict[str, str]:
    """Collect non-secret provenance: image, database, superuser, identity."""
    facts: dict[str, str] = {"container": container}
    inspected = _run(
        ["docker", "inspect", "--format", "{{.Name}}|{{.Config.Image}}|{{.Image}}", container],
        timeout=timeout,
    )
    if inspected.returncode == 0:
        parts = inspected.stdout.strip().split("|")
        for key, value in zip(("container_name", "image", "image_id"), parts, strict=False):
            facts[key] = value.strip().lstrip("/")
    # FIX: Prefer the connected session over optional container env. POSTGRES_DB
    # can be unset while psql still connects to a default database; publishing
    # without a real current_database() identity would skip the mismatch gate.
    connected_db = _psql(container, "SELECT current_database();", timeout=timeout).strip()
    if connected_db:
        facts["database"] = connected_db
    connected_user = _psql(container, "SELECT current_user;", timeout=timeout).strip()
    if connected_user:
        facts["superuser"] = connected_user
    else:
        for env_name, key in (("POSTGRES_USER", "superuser"),):
            probe = _run(
                ["docker", "exec", container, "sh", "-c", f'printf "%s" "${env_name}"'],
                timeout=timeout,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                facts[key] = probe.stdout.strip()
    server_version = _psql(container, "SHOW server_version;", timeout=timeout).strip()
    if server_version:
        facts["server_version"] = server_version
    system_identifier = _psql(container, IDENTITY_SQL, timeout=timeout).strip()
    if system_identifier:
        facts["system_identifier"] = system_identifier
    return facts


# ============================================================================
# Purpose: Exact row counts for every table in ``public``, recorded in the
#          manifest so the rehearsal can compare restored data against a
#          concrete number instead of "check it looks right", and so the
#          content gate has something to judge.
# Database/ORM: Reads pg_catalog.pg_tables plus one count per public table.
# Standards: Table names come from the catalog and are still quoted with
#            doubled quotes before interpolation; the connection is the
#            container superuser, which bypasses RLS, so the counts are the
#            full table and not one tenant's slice. A row psql returns in an
#            unexpected shape raises BackupError rather than a bare ValueError:
#            an unhandled exception here would exit 1 with no log line.
# Blast Radius: Read-only. The manifest it feeds is the restore acceptance
#               test and the content gate's input, so an under-count here would
#               weaken both.
# Connections:
#   - File: scripts/restore_database.py -> compares these counts after
#     pg_restore and fails the rehearsal on a mismatch.
#   - File: scripts/backup_database.py -> ``_evaluate_content`` judges them.
# ============================================================================
def _table_row_counts(container: str, *, timeout: int) -> dict[str, int]:
    raw = _psql(container, LIST_TABLES_SQL, timeout=timeout)
    tables = [line.strip() for line in raw.splitlines() if line.strip()]
    if not tables:
        return {}
    return _parse_counts_output(_psql(container, _count_sql_for_tables(tables), timeout=timeout))


# ============================================================================
# Purpose: Stream pg_dump from the container straight to a host file, then
#          prove the file is a readable custom-format archive before it is
#          allowed to count as a backup.
# Database/ORM: Reads the whole application database through pg_dump; writes
#               nothing.
# Standards: No --no-privileges and no --no-owner. The ACLs and RLS policies
#            ARE the payload this backup exists to preserve, and stripping
#            them would produce an archive that restores into a database with
#            tenant isolation silently switched off.
# Blast Radius: Disaster recovery. A truncated dump that passed here would be
#               an unrestorable backup that looks healthy.
# Connections:
#   - File: scripts/restore_database.py -> ``_restore_data`` is the pg_restore
#     consumer of exactly this artifact.
# ============================================================================
def _dump_database(
    container: str, target: Path, *, timeout: int, snapshot: str | None = None
) -> None:
    snapshot_flag = f" --snapshot={_shell_single_quote(snapshot)}" if snapshot else ""
    argv = _container_sh(
        container,
        'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
        f"--format=custom --compress=6 --no-password{snapshot_flag}",
    )
    completed = _run_to_file(argv, timeout=timeout, target=target)
    if completed.returncode != 0:
        raise BackupError(EXIT_COMMAND_FAILED, f"pg_dump failed: {completed.stderr.strip()}")
    if not target.exists() or target.stat().st_size == 0:
        raise BackupError(EXIT_ARTIFACT_INVALID, "pg_dump produced an empty file")
    with target.open("rb") as handle:
        magic = handle.read(len(CUSTOM_FORMAT_MAGIC))
    if magic != CUSTOM_FORMAT_MAGIC:
        raise BackupError(
            EXIT_ARTIFACT_INVALID,
            f"{target.name} does not start with the pg_dump custom-format magic",
        )


def _shell_single_quote(value: str) -> str:
    """Quote a value for embedding inside the container ``sh -c`` single-quoted body."""
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise BackupError(EXIT_INTERNAL, "snapshot id contains illegal control characters")
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _parse_counts_output(raw: str) -> dict[str, int]:
    """Parse ``name|count`` psql rows into a table-to-row-count mapping."""
    counts: dict[str, int] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        name, _, raw_count = line.rpartition("|")
        try:
            counts[name] = int(raw_count)
        except ValueError as exc:
            raise BackupError(
                EXIT_COMMAND_FAILED,
                f"could not read a row count from psql output {line!r}: {exc}",
            ) from exc
    return counts


def _count_sql_for_tables(tables: list[str]) -> str:
    """Build a UNION ALL SELECT that counts rows for each public table name."""
    branches = [
        f"SELECT {_quote_literal(name)} AS t, count(*) AS n FROM public.{_quote_identifier(name)}"
        for name in tables
    ]
    return " UNION ALL ".join(branches) + " ORDER BY t;"


# ============================================================================
# Purpose: Hold one REPEATABLE READ session, export its snapshot for pg_dump
#          --snapshot, then count every public table on that same session so
#          the manifest cannot disagree with the archive under live writes.
# Database/ORM: One long-lived psql inside the Postgres container.
# Standards: Fail closed if snapshot export, dump, or counts fail; always
#            COMMIT/close the holder so idle-in-transaction sessions do not
#            accumulate.
# Blast Radius: Manifest table_row_counts and restore verification.
# ============================================================================
def _readline_with_deadline(stream, deadline: float) -> str:
    """Read one line from ``stream`` or raise when the monotonic deadline passes."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BackupError(
            EXIT_COMMAND_FAILED,
            "snapshot holder timed out waiting for psql output",
        )
    box: list[str] = []
    errors: list[OSError] = []

    def _reader() -> None:
        try:
            box.append(stream.readline())
        except OSError as exc:
            errors.append(exc)

    worker = threading.Thread(target=_reader, daemon=True)
    worker.start()
    worker.join(remaining)
    if worker.is_alive():
        raise BackupError(
            EXIT_COMMAND_FAILED,
            "snapshot holder timed out waiting for psql output",
        )
    if errors:
        raise BackupError(
            EXIT_COMMAND_FAILED,
            f"snapshot holder read failed: {errors[0]}",
        ) from errors[0]
    return box[0] if box else ""


@contextmanager
def _held_repeatable_read_session(container: str, *, timeout: int):
    """Yield ``(snapshot_id, run_sql)`` for one held REPEATABLE READ transaction."""
    argv = _container_sh(
        container,
        'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
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
    deadline = time.monotonic() + max(1, timeout)

    def run_sql(sql: str) -> str:
        if process.poll() is not None:
            err = process.stderr.read()
            raise BackupError(
                EXIT_COMMAND_FAILED,
                f"snapshot holder psql exited early: {err.strip()}",
            )
        process.stdin.write(sql if sql.endswith("\n") else sql + "\n")
        process.stdin.flush()
        # Each statement's -At output ends when psql prints the row(s). For
        # transactional control statements with no rows, we still need a
        # marker. Append a sentinel SELECT so we can drain one line.
        process.stdin.write("SELECT 'UMS_SNAP_OK';\n")
        process.stdin.flush()
        lines: list[str] = []
        while True:
            # FIX: Bound every readline against --timeout. process.wait() alone
            # never runs while a blocked snapshot/count query holds stdout.
            line = _readline_with_deadline(process.stdout, deadline)
            if line == "":
                err = process.stderr.read()
                raise BackupError(
                    EXIT_COMMAND_FAILED,
                    f"snapshot holder psql closed stdout: {err.strip()}",
                )
            text = line.rstrip("\n")
            if text == "UMS_SNAP_OK":
                break
            lines.append(text)
        return "\n".join(lines)

    try:
        # Fail closed on locked catalog / slow counts inside the holder session.
        timeout_ms = max(1, int(timeout * 1000))
        run_sql(f"SET statement_timeout = {timeout_ms};")
        run_sql("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;")
        snapshot = run_sql("SELECT pg_export_snapshot();").strip()
        if not snapshot:
            raise BackupError(EXIT_COMMAND_FAILED, "pg_export_snapshot returned empty")
        yield snapshot, run_sql
        run_sql("COMMIT;")
    except Exception:
        try:
            if process.poll() is None:
                process.stdin.write("ROLLBACK;\n")
                process.stdin.flush()
        except OSError:
            pass
        raise
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=min(30, timeout))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _dump_database_and_count(container: str, target: Path, *, timeout: int) -> dict[str, int]:
    """Dump the database and return row counts from the same snapshot."""
    with _held_repeatable_read_session(container, timeout=timeout) as (snapshot, run_sql):
        _dump_database(container, target, timeout=timeout, snapshot=snapshot)
        raw_tables = run_sql(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname = 'public' ORDER BY tablename;"
        )
        tables = [line.strip() for line in raw_tables.splitlines() if line.strip()]
        if not tables:
            return {}
        return _parse_counts_output(run_sql(_count_sql_for_tables(tables)))


# ============================================================================
# Purpose: Pipe the written host file back through ``pg_restore --list`` and
#          return how many table-of-contents entries it holds.
# Database/ORM: None. Reads the archive header and TOC only.
# Standards: This is what separates "a file exists" from "a restorable archive
#            exists": pg_restore reads the whole table of contents, so a
#            truncated or corrupted transfer fails now rather than during the
#            incident. It reads the file that actually landed on disk, not
#            pg_dump's stdout.
#
#            An EMPTY table of contents is deliberately NOT an error here. A
#            non-zero pg_restore exit means the archive is unreadable, which is
#            a verification failure (exit 6, run discarded). A zero exit with
#            zero entries means pg_restore read the archive perfectly and it
#            describes nothing -- that is a faithful dump of a database with no
#            content, which is the content gate's question, not this function's.
#            Raising here made `DROP SCHEMA public CASCADE` exit 6 and DISCARD
#            the run directory, so the quarantine-for-diagnosis the design
#            promises was missing in exactly the case it was designed for, and
#            the operator was pointed at a roles problem that did not exist.
# Blast Radius: Disaster recovery. Routes the dropped-schema case to the gate.
# Connections:
#   - File: scripts/backup_database.py -> ``_evaluate_content`` receives the
#     returned count and rejects a zero-entry archive as a no-content run.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> exit-code 6 vs 8 rows.
# ============================================================================
def _verify_dump_readable(container: str, dump_path: Path, *, timeout: int) -> int:
    with dump_path.open("rb") as source:
        completed = subprocess.run(
            _container_sh(container, "exec pg_restore --list"),
            stdin=source,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    if completed.returncode != 0:
        raise BackupError(
            EXIT_ARTIFACT_INVALID,
            f"pg_restore --list rejected the written dump: {completed.stderr.strip()}",
        )
    entries = [
        line for line in completed.stdout.splitlines() if line.strip() and not line.startswith(";")
    ]
    return len(entries)


# ============================================================================
# Purpose: Dump the cluster roles the custom-format archive cannot carry, and
#          refuse the whole backup if app_tenant / app_platform are absent.
# Database/ORM: Reads pg_authid / pg_auth_members via pg_dumpall --roles-only.
# Standards: --no-role-passwords by default keeps SCRAM verifiers out of a
#            plaintext file on the operator's disk; both app roles are NOLOGIN
#            so nothing the restore needs is lost, and
#            --include-role-passwords exists for clusters where a login role's
#            password is not recoverable from the environment. Fail-closed: a
#            roles.sql missing either role fails the run rather than shipping
#            an unrestorable backup.
# Blast Radius: Disaster recovery. This is the specific trap that turns an
#               otherwise perfect-looking backup into an unrestorable one.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py
#     -> ``_create_role`` (lines 92-113); grants at lines 300-333.
# ============================================================================
def _role_declared_in_roles_sql(body: str, role: str) -> bool:
    """Return True when ``roles.sql`` declares exact CREATE ROLE ``role``.

    Word-boundary search is insufficient: ``app_tenant-backup`` matches
    ``\\bapp_tenant\\b`` because ``-`` is a non-word character.
    """
    quoted = rf'(?im)^\s*CREATE\s+(?:ROLE|USER)\s+"{re.escape(role)}"\s*(?:WITH\b|;|$)'
    bare = rf"(?im)^\s*CREATE\s+(?:ROLE|USER)\s+{re.escape(role)}\s*(?:WITH\b|;|$)"
    return re.search(quoted, body) is not None or re.search(bare, body) is not None


def _dump_roles(
    container: str, target: Path, *, timeout: int, include_passwords: bool
) -> list[str]:
    flags = "--roles-only --no-password"
    if not include_passwords:
        flags += " --no-role-passwords"
    argv = _container_sh(
        container,
        f'exec pg_dumpall -U "$POSTGRES_USER" -l "$POSTGRES_DB" {flags}',
    )
    completed = _run_to_file(argv, timeout=timeout, target=target)
    if completed.returncode != 0:
        raise BackupError(
            EXIT_COMMAND_FAILED,
            f"pg_dumpall --roles-only failed: {completed.stderr.strip()}",
        )
    if not target.exists() or target.stat().st_size == 0:
        raise BackupError(EXIT_ARTIFACT_INVALID, "pg_dumpall --roles-only produced an empty file")
    body = target.read_text(encoding="utf-8", errors="replace")
    # FIX: Require exact CREATE ROLE identifiers, not substring/word-boundary hits
    # that accept hyphenated lookalikes such as app_tenant-backup.
    missing = [role for role in REQUIRED_ROLES if not _role_declared_in_roles_sql(body, role)]
    if missing:
        raise BackupError(
            EXIT_ARTIFACT_INVALID,
            f"{target.name} does not declare CREATE ROLE for {', '.join(missing)}. "
            "The dump carries RLS policies and grants that reference those roles, "
            "so this backup would not restore. Refusing to publish it.",
        )
    return list(REQUIRED_ROLES)


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


@dataclass(frozen=True)
class Identity:
    """Which database an output directory belongs to.

    ``system_identifier`` is the Postgres cluster's own id, written once by
    initdb: stable across restarts and container recreation, different for every
    freshly initialised cluster. ``database`` separates two databases inside one
    cluster. Row counts cannot express either, which is the whole point.
    """

    system_identifier: str
    database: str

    def describe(self) -> str:
        return f"database {self.database!r} in cluster {self.system_identifier}"

    def as_json(self) -> dict[str, str]:
        return {"system_identifier": self.system_identifier, "database": self.database}


def _identity_from_source(source: object) -> Identity | None:
    """Read an identity out of a ``manifest.source`` block, or None if incomplete.

    Incomplete means *unknown*, never *matching*: a manifest written before this
    check existed has no ``system_identifier``, and the caller must treat that as
    "nothing to compare" rather than as a pass.
    """
    if not isinstance(source, dict):
        return None
    system_identifier = str(source.get("system_identifier") or "").strip()
    database = str(source.get("database") or "").strip()
    if not system_identifier or not database:
        return None
    return Identity(system_identifier=system_identifier, database=database)


@dataclass(frozen=True)
class Watermark:
    """Per-table high-water row counts, carried across every run."""

    tables: dict[str, int] = field(default_factory=dict)
    source: str = "none"
    reset_after: str | None = None

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def total_rows(self) -> int:
        return sum(self.tables.values())

    @property
    def is_empty(self) -> bool:
        return not self.tables

    def as_manifest_block(self) -> dict[str, object] | None:
        if self.is_empty:
            return None
        return {
            "source": self.source,
            "tables": self.table_count,
            "rows": self.total_rows,
            "reset_after": self.reset_after,
        }


@dataclass(frozen=True)
class ContentVerdict:
    """The content gate's decision about one run, recorded in its manifest."""

    accepted: bool
    tables: int
    rows: int
    watermark: Watermark
    first_run: bool
    established: bool = False
    failures: tuple[str, ...] = ()
    overridden: tuple[str, ...] = ()
    rebaseline_tables: frozenset[str] = frozenset()
    non_seed_rows: int = 0
    expected_identity: Identity | None = None
    observed_identity: Identity | None = None
    identity_adopted: bool = False

    def as_manifest_block(self) -> dict[str, object]:
        return {
            "status": "accepted" if self.accepted else "rejected",
            "tables": self.tables,
            "rows": self.rows,
            "non_seed_rows": self.non_seed_rows,
            "min_tables": MIN_TABLES,
            "seed_tables": list(SEED_TABLES),
            "collapse_row_fraction": COLLAPSE_ROW_FRACTION,
            "table_collapse_min_rows": TABLE_COLLAPSE_MIN_ROWS,
            "watermark": self.watermark.as_manifest_block(),
            "watermark_source": self.watermark.source,
            "first_run": self.first_run,
            "watermark_established": self.established,
            "identity": {
                "expected": self.expected_identity.as_json() if self.expected_identity else None,
                "observed": self.observed_identity.as_json() if self.observed_identity else None,
                "adopted": self.identity_adopted,
            },
            "failures": list(self.failures),
            "overridden": list(self.overridden),
            "rebaselined_tables": sorted(self.rebaseline_tables),
        }


@dataclass(frozen=True)
class PruneOutcome:
    """What retention deleted, and what it refused to interpret.

    ``skipped`` and ``future`` are both "left alone", and they are separate
    because the operator's fix differs: a name that is not a date is junk to
    rename, a name dated ahead of now is either a plant or the fingerprint of a
    clock that was wrong at 02:00, and the second one needs the clock checked.
    """

    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    future: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BackupOutcome:
    """What one invocation produced: a published run, or a quarantined one."""

    run_dir: Path
    manifest: dict[str, object]
    verdict: ContentVerdict
    counts: dict[str, int]
    next_watermark: dict[str, int]
    watermark_reset: dict[str, object]
    identity: Identity | None = None

    @property
    def accepted(self) -> bool:
        return self.verdict.accepted


def _read_manifest(run: Path) -> dict[str, object] | None:
    """Load one run's manifest, or None if it is absent, unreadable or not JSON."""
    try:
        loaded = json.loads((run / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _manifest_table_counts(manifest: dict[str, object]) -> dict[str, int] | None:
    """Return a manifest's per-table counts, or None if it records none usable."""
    raw = manifest.get("table_row_counts")
    if not isinstance(raw, dict):
        return None
    counts: dict[str, int] = {}
    for name, value in raw.items():
        try:
            counts[str(name)] = int(value)
        except (TypeError, ValueError):
            return None
    return counts


# ============================================================================
# Purpose: The absolute floor, as one predicate: does this set of per-table row
#          counts look like a working UMS database at all?
# Database/ORM: None. Operates on counts already read from a database or a
#               manifest.
# Standards: Every UMS migration path ends with all of SEED_TABLES populated,
#            which is what makes "non-empty" decidable without any history. It
#            is deliberately an existence test, not a count test -- see the
#            SEED_TABLES block for why coupling to 178 currencies would be
#            brittle. WHAT BREAKS IF A MIGRATION CHANGES THE SEED: if a future
#            migration drops, renames, or empties one of these tables, EVERY
#            backup on the box starts failing closed with exit 8 until this
#            tuple is updated. That direction is the intended one -- a gate that
#            degrades to "accept everything" when the schema moves is not a
#            gate -- and the fix is a one-line edit here plus the matching
#            fixture in tests/scripts/test_backup_content_gate.py.
# Blast Radius: Disaster recovery. This is the only check that is correct on a
#               first-ever run with no history to compare against.
# Connections:
#   - File: scripts/backup_database.py -> ``_evaluate_content`` (live counts)
#     and ``_run_has_content`` / ``_load_watermark`` (recorded counts).
# ============================================================================
def _counts_clear_floor(counts: dict[str, int]) -> bool:
    # The MIN_TABLES arm cannot change the answer while SEED_TABLES is non-empty:
    # an empty mapping fails the seed test anyway. It is kept as the floor's own
    # statement of "a UMS database has tables", and a mutation matrix confirmed no
    # test can distinguish removing it -- which is what an equivalent mutant is,
    # not a gap in the tests.
    if len(counts) < MIN_TABLES:
        return False
    return all(counts.get(name, 0) > 0 for name in SEED_TABLES)


def _non_seed_rows(counts: dict[str, int]) -> int:
    """Rows outside the seeded lookup/bootstrap tables -- the application's data."""
    return sum(value for name, value in counts.items() if name not in SEED_TABLES)


# ============================================================================
# Purpose: Decide whether a run directory is a backup this script published, as
#          opposed to a directory that merely contains a ``manifest.json``.
# Database/ORM: None. Stats two files and compares against the manifest.
# Standards: A manifest is a REPORT about artifacts, not the artifacts. Both
#            artifacts must exist as non-empty files, and any size the manifest
#            records for them must match what is on disk. Sizes rather than
#            sha256 on purpose: this runs over every run directory on every
#            night, and re-hashing multi-hundred-megabyte dumps to decide a
#            watermark contribution would trade a nightly cost against a check
#            the restore side already performs on the one run being restored.
#
#            MEASURED, which is why this exists: a hand-planted manifest.json in
#            a run-shaped directory -- no database.dump, no roles.sql -- claiming
#            ``org_units: 1000000000`` folded straight into the high-water mark
#            and pushed it to 1000000185, after which every run against the
#            healthy database exited 8.
# Blast Radius: Disaster recovery. Gates what may raise the watermark and what
#               retention treats as proven content.
# Connections:
#   - File: scripts/backup_database.py -> ``_load_watermark`` and
#     ``_run_has_content`` both require it before trusting a manifest.
#   - File: scripts/restore_database.py -> re-verifies both sha256 digests
#     before restoring, which is the stronger check at the point it matters.
# ============================================================================
def _run_is_published_backup(run: Path, manifest: dict[str, object]) -> bool:
    raw_artifacts = manifest.get("artifacts")
    artifacts = raw_artifacts if isinstance(raw_artifacts, dict) else {}
    for name in (DUMP_NAME, ROLES_NAME):
        path = run / name
        try:
            if not path.is_file():
                return False
            size = path.stat().st_size
        except OSError:
            return False
        if size == 0:
            return False
        entry = artifacts.get(name)
        if isinstance(entry, dict) and "bytes" in entry:
            try:
                recorded = int(entry["bytes"])
            except (TypeError, ValueError):
                return False
            if recorded != size:
                return False
    return True


# ============================================================================
# Purpose: Classify one existing run directory as proven-to-have-content,
#          proven-empty, or unknown, so retention can protect data rather than
#          protect directory entries.
# Database/ORM: None. Reads only manifest.json from a run directory.
# Standards: Three-valued on purpose. ``None`` (no manifest, unreadable
#            manifest, or a manifest with no counts block) means *unknown*, and
#            every caller must treat unknown as content -- deleting a run that
#            might be the last good backup is unrecoverable, keeping one that is
#            not costs disk. A run whose manifest says the content gate rejected
#            it is proven empty regardless of what else it records. A manifest
#            that merely CLAIMS ``accepted`` is not taken at its word: the
#            recorded counts are re-tested against the floor, and the directory
#            has to actually hold the artifacts the manifest describes
#            (``_run_is_published_backup``) before those counts mean anything.
#            A directory that fails that test is *unknown*, not proven-empty:
#            never deleted, but never allowed to be the "newest run with
#            content" that invariant 1 pins either.
# Blast Radius: Disaster recovery. This predicate decides what ``_prune`` is
#               allowed to delete.
# Connections:
#   - File: scripts/backup_database.py -> ``_prune`` protects on this verdict,
#     and ``run_backup`` writes the content_gate block it reads.
# ============================================================================
def _run_has_content(run: Path) -> bool | None:
    manifest = _read_manifest(run)
    if manifest is None:
        return None
    gate = manifest.get("content_gate")
    if isinstance(gate, dict) and gate.get("status") == "rejected":
        return False
    if not _run_is_published_backup(run, manifest):
        return None
    counts = _manifest_table_counts(manifest)
    if counts is None:
        return None
    return _counts_clear_floor(counts)


def _read_watermark_file(out_dir: Path) -> tuple[dict[str, int], str | None]:
    """Load the stored watermark, tolerating absence and corruption."""
    try:
        raw = json.loads((out_dir / WATERMARK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, None
    if not isinstance(raw, dict):
        return {}, None
    tables: dict[str, int] = {}
    stored = raw.get("tables")
    if isinstance(stored, dict):
        for name, value in stored.items():
            try:
                tables[str(name)] = max(int(value), 0)
            except (TypeError, ValueError):
                continue
    reset_after = raw.get("reset_after")
    return tables, reset_after if isinstance(reset_after, str) else None


# ============================================================================
# Purpose: Build the persistent per-table high-water mark this run is judged
#          against, so cumulative loss is bounded instead of being re-anchored
#          every night.
# Database/ORM: None. Reads watermark.json and the manifests of accepted runs.
# Standards: The watermark is a MAXIMUM over history, never "the previous run".
#            Comparing each run only with the one before it bounds nothing --
#            losing 80% a night for three nights is three green runs and a
#            database drained from 180 rows to 1, each step individually under
#            the collapse fraction.
#
#            It has two independent homes, and the merge is a max, so LOSING one
#            fails safe rather than open:
#              * ``watermark.json``. Delete it and the mark is rebuilt from the
#                run manifests, which can only push it back UP.
#              * Every accepted run's manifest. Prune them all and the file
#                still holds the mark.
#              Only losing BOTH resets it, and that state -- no watermark file
#              and no accepted run -- is indistinguishable from a genuinely new
#              output directory, which ``_evaluate_content`` refuses without
#              --establish-watermark rather than waving through.
#
#            ``reset_after`` is what makes --accept-content-drop stick: runs at
#            or older than the reset are excluded from the rebuild, so a
#            deliberate deletion is not resurrected by last week's manifest.
#            Deleting watermark.json also deletes the reset, which restores the
#            higher mark -- again, the safe direction.
#
#            ``reset_after`` IS A NAME COMPARISON, and that is the whole reason
#            ``_run_stamp`` refuses a future stamp. The reset is only ever set to
#            the name of the run that carried the override, so a directory whose
#            name sorts ABOVE every real run -- any future date -- was never
#            excluded and re-folded its counts in every single night. MEASURED
#            before the fix, with a planted ``ums-backup-20990101T000000Z``: exit
#            8, then exit 0 under --accept-content-drop, then exit 8 again on the
#            very next night, forever. Not "one-command recovery": a wedge whose
#            only sustainable end is --accept-content-drop living in the
#            scheduled task, which is the tier-2 comparison switched off for
#            good. The refusal is in ``_run_stamp`` so that this path, retention
#            and ``_load_identity`` all get it from the same place.
#
#            A run contributes only if it is a STRUCTURALLY PUBLISHED backup --
#            ``_run_is_published_backup``: the directory holds a non-empty
#            database.dump and roles.sql, and any artifact size its manifest
#            records matches the file on disk -- AND its gate verdict is not
#            "rejected" AND its recorded counts still clear the floor.
#
#            WHAT THIS DOES *NOT* GUARANTEE, stated plainly because the previous
#            revision of this block asserted the opposite. Nothing here bounds
#            the MAGNITUDE of a contribution. ``_counts_clear_floor`` is an
#            existence test; it constrains no number. The fold is a maximum, so
#            a manifest that overstates what its run held raises the bar rather
#            than lowering it -- the failure mode is exit 8, an availability
#            failure, and never a silently weakened gate.
#
#            MEASURED both halves: a planted manifest.json in a run-shaped
#            directory with no artifacts at all claiming ``org_units:
#            1000000000`` pushed the mark to 1000000185 and every later run
#            against the healthy database exited 8. It no longer contributes.
#            A directory that *does* carry matching artifacts and an inflated
#            count still would, and the recovery is one run with
#            --accept-content-drop (measured: exit 0, mark back to the real
#            numbers, and the night after that is green with no flag) or
#            deleting the offending run directory. That recovery claim is only
#            true because ``reset_after`` can actually exclude the directory,
#            which is exactly what a future stamp defeated; it holds for every
#            stamp ``_run_stamp`` now accepts, and a future-stamped one never
#            reaches the fold at all.
# Blast Radius: Disaster recovery. This is the reference the collapse checks
#               use, so a mark that is too low silently weakens every run.
# Connections:
#   - File: scripts/backup_database.py -> ``_evaluate_content`` consumes it;
#     ``_write_watermark`` persists the next one;
#     ``_run_is_published_backup`` decides what may be folded in.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> "what a green run does
#     and does not guarantee".
# ============================================================================
# ============================================================================
# Purpose: Serialize overlapping backup invocations on one --out-dir so two
#          processes cannot load the same watermark, both publish, and race the
#          final watermark write.
# Database/ORM: None. Host filesystem lock directory only.
# Standards: Atomic ``mkdir`` (works on Windows without flock). Fail closed on
#            contention with EXIT_USAGE. Always release in finally.
# Blast Radius: Backup watermark / identity bookkeeping only.
# ============================================================================
def _pid_is_alive(pid: int) -> bool:
    """Return True when ``pid`` appears to be a live process on this host."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is not killable by this uid.
        return True
    except OSError:
        return False
    return True


def _reclaim_stale_backup_lock(lock_dir: Path) -> bool:
    """Remove an abandoned ``.backup.lock`` when its owner pid is dead or missing."""
    owner = lock_dir / "owner.pid"
    try:
        raw = owner.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except FileNotFoundError:
        # Pre-owner.pid locks, or crash between mkdir and pid write: reclaim.
        pid = None
    except (OSError, ValueError):
        return False
    if pid is not None and _pid_is_alive(pid):
        return False
    try:
        if owner.exists():
            owner.unlink()
        lock_dir.rmdir()
    except OSError:
        return False
    return True


@contextmanager
def _exclusive_backup_lock(out_dir: Path):
    """Hold an exclusive lock directory under ``out_dir`` for one backup run."""
    lock_dir = out_dir / ".backup.lock"
    owner = lock_dir / "owner.pid"

    def _acquire() -> None:
        lock_dir.mkdir()
        owner.write_text(f"{os.getpid()}\n", encoding="utf-8")

    try:
        _acquire()
    except FileExistsError as exc:
        # FIX: Recover abandoned locks left by kill -9 / host reboot so the
        # next scheduled run is not wedged until an operator deletes the dir.
        if _reclaim_stale_backup_lock(lock_dir):
            try:
                _acquire()
            except FileExistsError as retry_exc:
                raise BackupError(
                    EXIT_USAGE,
                    f"another backup is already running (lock {lock_dir}). "
                    "If no backup is running, remove that directory and retry.",
                ) from retry_exc
        else:
            raise BackupError(
                EXIT_USAGE,
                f"another backup is already running (lock {lock_dir}). "
                "If no backup is running, remove that directory and retry.",
            ) from exc
    try:
        yield
    finally:
        try:
            owner.unlink(missing_ok=True)
            lock_dir.rmdir()
        except OSError:
            pass


def _restrict_run_dir_mode(path: Path) -> None:
    """Best-effort owner-only mode for backup run directories (POSIX)."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        # Windows ignores mkdir mode and may reject chmod on some paths.
        pass


def _load_watermark(out_dir: Path) -> Watermark:
    stored, reset_after = _read_watermark_file(out_dir)
    merged = dict(stored)
    folded = 0
    ignored_future = 0
    now = _utc_now()
    try:
        children = sorted(out_dir.iterdir(), key=lambda child: child.name)
    except OSError:
        children = []
    for child in children:
        if not child.is_dir():
            continue
        if _run_stamp(child.name, now=now) is None:
            # Counted, not silently dropped: a future-dated directory is inert
            # now, but the operator still has to know it is there -- and on a
            # night that gets no further than the gate, ``_prune`` never runs to
            # say so. See ``_run_stamp`` for why it is refused at all.
            if _parse_stamp(child.name) is not None:
                ignored_future += 1
            continue
        if reset_after is not None and child.name <= reset_after:
            continue
        manifest = _read_manifest(child)
        if manifest is None:
            continue
        gate = manifest.get("content_gate")
        if isinstance(gate, dict) and gate.get("status") == "rejected":
            continue
        if not _run_is_published_backup(child, manifest):
            continue
        counts = _manifest_table_counts(manifest)
        if counts is None or not _counts_clear_floor(counts):
            continue
        folded += 1
        for name, value in counts.items():
            merged[name] = max(merged.get(name, 0), value)
    if not merged:
        source = f"none: no {WATERMARK_NAME} and no accepted run in this directory"
    elif stored and folded:
        source = f"{WATERMARK_NAME} merged with {folded} accepted run manifest(s)"
    elif stored:
        source = WATERMARK_NAME
    else:
        source = f"rebuilt from {folded} accepted run manifest(s); {WATERMARK_NAME} was absent"
    if ignored_future:
        source += f"; {ignored_future} future-dated directory(ies) ignored"
    return Watermark(tables=merged, source=source, reset_after=reset_after)


# ============================================================================
# Purpose: Recover which database this output directory belongs to, so a run
#          against a different one can be refused instead of published.
# Database/ORM: None. Reads watermark.json and the run manifests.
# Standards: Same two-homes design as the watermark, and the same fail-safe
#            direction. ``watermark.json`` carries it; if that file is lost the
#            identity is rebuilt from the NEWEST structurally published,
#            non-rejected run manifest, because that is the run whose data the
#            directory most recently accepted. Losing BOTH yields None, which
#            means *unknown* -- the check cannot run, and it is recorded on the
#            next accepted run. Unknown never degrades to "matches": every
#            comparison in ``_evaluate_content`` requires two known identities.
#
#            An out-dir written by a revision of this script that predates the
#            check has no identity in either home. Its first run under this
#            revision therefore adopts silently; there is nothing to compare
#            against and refusing every existing directory would be worse than
#            the hole. That is a one-time, documented upgrade path.
#
#            The rebuild walks names in REVERSE order, so this is the path a
#            future-dated directory reaches first: its name outranks every real
#            run, and it would hand the whole binding to whatever manifest it
#            carried. ``_run_stamp`` refuses it here for the same reason it does
#            in the watermark and in retention.
# Blast Radius: Disaster recovery. A wrongly-recovered identity would refuse
#               every run (exit 8, loud and recoverable), never accept a wrong
#               database silently.
# Connections:
#   - File: scripts/backup_database.py -> ``_container_facts`` produces the
#     observed side; ``_write_watermark`` persists the adopted one.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> exit-code 8 cause 4.
# ============================================================================
def _load_identity(out_dir: Path) -> Identity | None:
    try:
        raw = json.loads((out_dir / WATERMARK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict):
        stored = _identity_from_source(raw.get("identity"))
        if stored is not None:
            return stored
    now = _utc_now()
    try:
        children = sorted(out_dir.iterdir(), key=lambda child: child.name, reverse=True)
    except OSError:
        return None
    for child in children:
        # Reverse-sorted, so a future-dated directory is the FIRST thing this
        # loop sees -- the newest name wins, and any future date outranks every
        # real one. One clock reading for the whole pass.
        if not child.is_dir() or _run_stamp(child.name, now=now) is None:
            continue
        manifest = _read_manifest(child)
        if manifest is None:
            continue
        gate = manifest.get("content_gate")
        if isinstance(gate, dict) and gate.get("status") == "rejected":
            continue
        if not _run_is_published_backup(child, manifest):
            continue
        recovered = _identity_from_source(manifest.get("source"))
        if recovered is not None:
            return recovered
    return None


# ============================================================================
# Purpose: Persist the watermark this run leaves behind, including any
#          deliberate downward reset and the identity of the database this
#          output directory is bound to, with enough provenance to audit both.
# Database/ORM: None.
# Standards: Raises OSError to its caller rather than swallowing it. Unlike
#            ``backup.log``, this file is load-bearing for the NEXT run's
#            protection, so a failed write is reported as exit 7 -- "the backup
#            is published, the bookkeeping is not" -- instead of being logged
#            and forgotten. It is written BEFORE retention runs, because
#            retention can delete the manifests the rebuild path depends on.
# Blast Radius: Disaster recovery. Sets the bar for every subsequent run.
# Connections:
#   - File: scripts/backup_database.py -> ``_load_watermark`` reads it back;
#     ``main`` turns a write failure into EXIT_BOOKKEEPING_FAILED.
# ============================================================================
def _write_watermark(
    out_dir: Path,
    tables: dict[str, int],
    *,
    run: str,
    reset: dict[str, object],
    now: datetime,
    identity: Identity | None = None,
) -> None:
    previous_resets: list[object] = []
    try:
        raw = json.loads((out_dir / WATERMARK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict) and isinstance(raw.get("resets"), list):
        previous_resets = list(raw["resets"])
    reset_after: str | None = None
    if isinstance(raw, dict) and isinstance(raw.get("reset_after"), str):
        reset_after = raw["reset_after"]
    if reset:
        previous_resets.append({"utc": now.isoformat(), "run": run, **reset})
        reset_after = run
    # An identity is only ever recorded from a run the gate ACCEPTED, so a
    # refused wrong-database run cannot rebind the directory to itself. A run
    # that could not read one (an old manifest, a probe that failed) leaves
    # whatever was already there rather than erasing it.
    bound = identity
    if bound is None and isinstance(raw, dict):
        bound = _identity_from_source(raw.get("identity"))
    payload = {
        "schema": WATERMARK_SCHEMA,
        "updated_utc": now.isoformat(),
        "source_run": run,
        "reset_after": reset_after,
        "identity": bound.as_json() if bound is not None else None,
        "tables": dict(sorted(tables.items())),
        "resets": previous_resets[-WATERMARK_RESET_HISTORY:],
    }
    (out_dir / WATERMARK_NAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _name_tables(names: list[str]) -> str:
    """Render a bounded, deterministic list of table names for one message."""
    ordered = sorted(names)
    head = ", ".join(ordered[:MAX_NAMED_TABLES])
    extra = len(ordered) - MAX_NAMED_TABLES
    return head if extra <= 0 else f"{head} (+{extra} more)"


# ============================================================================
# Purpose: Compute the watermark this run leaves behind: a per-table maximum,
#          lowered only for the tables an operator override explicitly named.
# Database/ORM: None.
# Standards: An override lowers ONLY the tables that appeared in the failures it
#            suppressed. A blanket "re-baseline everything to tonight" would
#            reintroduce the exact defect being fixed -- accepting a drop on one
#            table would silently lower the bar protecting every other one.
# Blast Radius: Disaster recovery.
# Connections:
#   - File: scripts/backup_database.py -> ``_evaluate_content`` decides
#     ``rebaseline_tables``; ``_write_watermark`` persists the result.
# ============================================================================
def _next_watermark(
    previous: Watermark, counts: dict[str, int], verdict: ContentVerdict
) -> tuple[dict[str, int], dict[str, object]]:
    merged = dict(previous.tables)
    for name, value in counts.items():
        merged[name] = max(merged.get(name, 0), value)
    lowered: dict[str, int] = {}
    removed: list[str] = []
    for name in sorted(verdict.rebaseline_tables):
        current = counts.get(name)
        if current is None:
            if merged.pop(name, None) is not None:
                removed.append(name)
            continue
        if merged.get(name, 0) != current:
            merged[name] = current
            lowered[name] = current
    reset: dict[str, object] = {}
    if lowered or removed:
        reset = {"reason": "--accept-content-drop", "lowered": lowered, "removed": removed}
    return merged, reset


# ============================================================================
# Purpose: Decide whether what this run captured is a backup at all, using an
#          absolute seed floor that is always right, a persistent watermark that
#          bounds cumulative loss, and an explicit acknowledgement for the one
#          case neither can judge.
# Database/ORM: None; operates on the row counts already read from the database.
# Standards: Four tiers with different authority.
#
#            1. THE SEED FLOOR has NO override. A schema with no tables, an
#               archive whose table of contents is empty, or any of
#               ``SEED_TABLES`` missing or empty means this is not a dump of a
#               working UMS database, and no flag can wave that through.
#            1b. THE IDENTITY BINDING is overridable by --adopt-database and by
#               nothing else -- in particular NOT by --accept-content-drop, which
#               is about magnitude and would otherwise wave through a whole
#               different database. An output directory holds the history of one
#               database: its watermark, its retention decisions and its restore
#               set all describe that one. MEASURED: a second, unrelated UMS
#               database backed into an established directory published exit 0
#               and moved the mark 187 -> 1098, after which ``_prune``'s
#               invariant 1 protected the foreign run as "newest with content".
#               Both databases held the same seeded tenant UUID, so no tenant or
#               row-count check could have separated them; the cluster's
#               ``system_identifier`` can.
#            2. THE WATERMARK CHECKS are overridable by --accept-content-drop,
#               because a deliberate operator wipe or a migration that drops a
#               table is a real event and without an escape hatch the comparison
#               would reject every subsequent run forever. They compare against
#               the persistent high-water mark, so a slow drain cannot walk the
#               reference down with it. They are deliberately PER TABLE as well as
#               whole-directory: a wipe that restores a database to its virgin
#               state moves the total by 2% on a small install, which no global
#               fraction can catch, but it empties every application table.
#            3. THE FIRST-RUN CASE is overridable by --establish-watermark and
#               by nothing else. With no history, a healthy database and one
#               that was wiped and re-migrated are THE SAME DATABASE -- both are
#               38 tables, 180 rows, seeds intact. No amount of inspection
#               separates them, so the honest gate asks the operator to confirm
#               the printed numbers once per output directory. That is also what
#               stops the failure being self-perpetuating: previously the first
#               green run became the reference for every run after it.
#            3b. THE EMPTY FIRST RUN needs --this-database-is-intentionally-empty
#               ON TOP of --establish-watermark. Tier 3 says the two databases
#               cannot be told apart; that is true of the general case and FALSE
#               of the specific one where every table outside ``SEED_TABLES``
#               holds zero rows, which is decidable right here. MEASURED as the
#               exact 02:00 sequence after `docker compose down -v`: the run
#               exits 8, the message told the operator to "re-run ONCE with
#               --establish-watermark", and doing so published the empty database
#               and made it the reference. The second flag is deliberately long
#               and unmistakable so it cannot be reached by copying the
#               remediation line or by muscle memory, and the tier-3 message no
#               longer offers --establish-watermark at all in that state.
#
#            Any override that actually suppressed something is recorded in the
#            manifest and in last-run.json, so it is auditable rather than
#            invisible.
# Blast Radius: Disaster recovery. This function is the difference between a
#               green "OK" over an empty database and a red Last Run Result.
# Connections:
#   - File: scripts/backup_database.py -> ``run_backup`` publishes or
#     quarantines on this verdict; ``_load_watermark`` supplies the reference.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> exit-code 8 row and the
#     "what a green run does not guarantee" section.
# ============================================================================
def _evaluate_content(
    counts: dict[str, int],
    watermark: Watermark,
    *,
    accept_drop: bool,
    establish: bool,
    toc_entries: int = -1,
    accept_empty: bool = False,
    expected_identity: Identity | None = None,
    observed_identity: Identity | None = None,
    adopt_database: bool = False,
) -> ContentVerdict:
    tables = len(counts)
    rows = sum(counts.values())
    non_seed = _non_seed_rows(counts)
    seed_names = ", ".join(SEED_TABLES)
    absolute: list[str] = []
    relative: list[str] = []
    rebaseline: set[str] = set()

    if tables < MIN_TABLES:
        absolute.append(
            "schema public has no tables, so this run captured no application "
            "data at all. A migrated UMS database always has tables. This is what "
            "a backup fired against a dropped schema -- or against a container "
            "brought up empty for a restore -- produces."
        )
    else:
        absent = [name for name in SEED_TABLES if name not in counts]
        drained = [name for name in SEED_TABLES if name in counts and counts[name] == 0]
        if absent:
            absolute.append(
                f"seed table(s) {', '.join(absent)} do not exist in schema public. "
                "Every UMS migration path creates and populates them, so this dump "
                "is not of a migrated UMS database."
            )
        if drained:
            absolute.append(
                f"seed table(s) {', '.join(drained)} exist but hold 0 rows. A virgin "
                "'alembic upgrade head' leaves alembic_version, currencies and "
                "tenants populated, so an empty one means this database was "
                "truncated or restored from nothing -- total loss of application "
                "data with the schema left standing."
            )
    if toc_entries == 0:
        absolute.append(
            "pg_restore read the archive successfully and it holds no "
            "table-of-contents entries at all, so the dump describes an empty "
            "database. The archive is not corrupt; there was nothing to dump."
        )

    first_run = watermark.is_empty
    if not first_run:
        disappeared = [
            name for name, mark in watermark.tables.items() if mark > 0 and name not in counts
        ]
        if disappeared:
            relative.append(
                f"{len(disappeared)} table(s) that held rows at their high-water mark "
                f"no longer exist: {_name_tables(disappeared)}. Tables do not "
                "disappear on their own."
            )
            rebaseline.update(disappeared)
        emptied = [
            name
            for name, mark in watermark.tables.items()
            if mark > 0 and counts.get(name, 0) == 0 and name in counts
        ]
        if emptied:
            relative.append(
                f"{len(emptied)} table(s) that held rows at their high-water mark are "
                f"now empty: {_name_tables(emptied)}."
            )
            rebaseline.update(emptied)
        # Seeded lookup and bootstrap tables do not shrink in normal operation:
        # currencies is a frozen ISO snapshot, alembic_version is a single stamp
        # row, and a tenant row is referenced ON DELETE RESTRICT from everywhere.
        # Holding them to their exact high-water mark closes the one case a
        # fraction cannot see -- a wipe whose entire lost dataset was extra rows in
        # a seeded table leaves the total within 2% of the mark and every other
        # table untouched, so nothing else fires. Measured: tenants 4 -> 1 after
        # `down -v` plus an auto-migrate used to publish exit 0.
        seeds_shrunk = [
            name
            for name in SEED_TABLES
            if watermark.tables.get(name, 0) > 0
            and 0 < counts.get(name, 0) < watermark.tables[name]
        ]
        if seeds_shrunk:
            relative.append(
                "seed table(s) fell below their high-water mark: "
                + "; ".join(
                    f"{name} {watermark.tables[name]}->{counts.get(name, 0)}"
                    for name in seeds_shrunk
                )
                + ". Seeded lookup and bootstrap tables do not shrink on their own."
            )
            rebaseline.update(seeds_shrunk)
        shrunk = [
            name
            for name, mark in watermark.tables.items()
            if mark >= TABLE_COLLAPSE_MIN_ROWS
            and 0 < counts.get(name, 0) < mark * COLLAPSE_ROW_FRACTION
        ]
        if shrunk:
            relative.append(
                f"{len(shrunk)} table(s) fell below {COLLAPSE_ROW_FRACTION:.0%} of their "
                f"high-water mark: "
                + "; ".join(
                    f"{name} {watermark.tables[name]}->{counts.get(name, 0)}"
                    for name in sorted(shrunk)[:MAX_NAMED_TABLES]
                )
                + "."
            )
            rebaseline.update(shrunk)
        # SUBSUMED, and deliberately kept. Under the current constants this can
        # never be the SOLE reason a run is refused: a table that escapes the
        # per-table rule keeps at least COLLAPSE_ROW_FRACTION of its mark, and a
        # table below TABLE_COLLAPSE_MIN_ROWS keeps at least one row of at most
        # nine, which is 11%. Summing those minima gives rows >= floor whenever
        # every per-table rule passed. It stays because that subsumption is a
        # property of the two constants, not of the design -- raise
        # COLLAPSE_ROW_FRACTION above 1/(TABLE_COLLAPSE_MIN_ROWS - 1) and this
        # becomes the only rule that sees the loss. A mutation matrix found no
        # test could kill it; ``test_the_whole_directory_floor_is_subsumed``
        # pins the arithmetic instead of pretending otherwise.
        floor = watermark.total_rows * COLLAPSE_ROW_FRACTION
        if watermark.total_rows > 0 and rows < floor:
            relative.append(
                f"the row count is {rows}, below the {COLLAPSE_ROW_FRACTION:.0%} floor "
                f"of {floor:.0f} for this directory's {watermark.total_rows}-row "
                f"high-water mark ({watermark.source}). The mark is a maximum over "
                "history, not last night's run, so a drain cannot walk it down."
            )
            rebaseline.update(
                name for name, mark in watermark.tables.items() if mark > counts.get(name, 0)
            )

    failures = list(absolute)
    overridden: list[str] = []
    if first_run:
        if not establish:
            failures.append(_first_run_refusal(tables, rows, non_seed, seed_names))
        elif non_seed == 0:
            # Tier 3b. The one first-run state that IS decidable, and the one the
            # tier-3 remediation line used to walk the operator straight into.
            empty_note = (
                "--establish-watermark was passed, but every table outside "
                f"{seed_names} is EMPTY: {tables} tables, {rows} rows, all of them "
                "seeded lookup and bootstrap data. Establishing here would make an "
                "empty database this directory's permanent reference, and every later "
                "run would be judged against nothing. This is the exact state left by "
                "'docker compose down -v' plus an auto-migrate. If the database was "
                "lost, RESTORE it first (Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md) and "
                "back up the restored database. If this really is a brand-new install "
                "with nothing entered yet, re-run with "
                "--this-database-is-intentionally-empty as well."
            )
            if accept_empty:
                overridden.append(f"[--this-database-is-intentionally-empty] {empty_note}")
            else:
                failures.append(empty_note)
    elif relative:
        if accept_drop:
            overridden.extend(f"[--accept-content-drop] {note}" for note in relative)
        else:
            failures.extend(relative)

    # Tier 1b, outside the first-run branch: an established directory can be
    # pointed at a different database on any night, not only the first.
    identity_note = _identity_refusal(expected_identity, observed_identity)
    if identity_note is not None:
        if adopt_database:
            overridden.append(f"[--adopt-database] {identity_note}")
        else:
            failures.append(identity_note)

    return ContentVerdict(
        accepted=not failures,
        tables=tables,
        rows=rows,
        watermark=watermark,
        first_run=first_run,
        established=bool(first_run and establish and not failures),
        failures=tuple(failures),
        overridden=tuple(overridden),
        # EQUIVALENT UNDER MUTATION, and kept as a statement of intent rather
        # than pretended otherwise. ``rebaseline`` is only ever populated by a
        # relative check firing, and a relative check that fired WITHOUT
        # --accept-content-drop lands in ``failures``, so that run is rejected
        # and ``_execute`` never reaches ``_write_watermark``. The guard
        # therefore cannot change any reachable accepted run -- but it is what
        # makes "only an override lowers the mark" true by construction instead
        # of by tracing three call sites.
        rebaseline_tables=frozenset(rebaseline) if accept_drop and relative else frozenset(),
        non_seed_rows=non_seed,
        expected_identity=expected_identity,
        observed_identity=observed_identity,
        identity_adopted=bool(identity_note is not None and adopt_database),
    )


def _first_run_refusal(tables: int, rows: int, non_seed: int, seed_names: str) -> str:
    """Tier 3's message, which must not nudge an empty database into tier 3b."""
    head = (
        f"this output directory has no watermark: {WATERMARK_NAME} is absent and it "
        "holds no accepted run to rebuild one from, so only the seed floor could run. "
    )
    if non_seed == 0:
        return (
            head + f"tables={tables} rows={rows}, and EVERY table outside {seed_names} "
            "is EMPTY -- this database holds no application data at all, so there is "
            "nothing here worth making a reference out of. If the database was lost, "
            "RESTORE it first (Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md); "
            "--establish-watermark on its own is refused in this state. Only if this "
            "really is a brand-new install with nothing entered yet, re-run with "
            "--establish-watermark --this-database-is-intentionally-empty."
        )
    return (
        head + "A healthy database and one that was wiped and re-migrated are the same "
        f"shape, so tables={tables} rows={rows} ({non_seed} of them outside "
        f"{seed_names}) cannot be judged here. Confirm those numbers are what this "
        "database should hold, then re-run ONCE with --establish-watermark. Do not put "
        "that flag in the scheduled task."
    )


def _identity_refusal(expected: Identity | None, observed: Identity | None) -> str | None:
    """Tier 1b's message, or None when there is nothing to compare or no mismatch."""
    # FIX: A bound directory with an unknown observed identity must fail closed;
    # previously None observed skipped the gate and accepted foreign dumps.
    if expected is not None and observed is None:
        return (
            f"this output directory is bound to {expected.describe()}, but this run "
            "could not establish the connected database identity "
            "(current_database/system_identifier). Refusing to publish without a "
            "comparable identity."
        )
    if expected is None or observed is None or expected == observed:
        return None
    return (
        f"this output directory is bound to {expected.describe()}, but this run read "
        f"{observed.describe()}. A backup directory holds the history of ONE database: "
        "its watermark, its retention decisions and its restore set all describe that "
        "one, so publishing another database's rows here moves the high-water mark to "
        "numbers the real database has never held and puts a foreign run at the front "
        "of the retention queue. Check --container / --project / --service. If this "
        "database really was rebuilt -- a restore into a new volume, a re-initialised "
        "cluster -- re-run ONCE with --adopt-database."
    )


def _classify_unusable(
    name: str,
    pattern: re.Pattern[str],
    *,
    skipped: list[str],
    future: list[str],
) -> None:
    """Record WHY a run-shaped directory could not be read as history.

    ``_run_stamp`` collapses "not a date" and "dated ahead of now" into the same
    None, because every caller's action is the same: leave it alone. The
    operator's action is not the same, so retention re-asks with the calendar
    half and reports them separately.
    """
    if _parse_stamp(name, pattern) is None:
        skipped.append(name)
    else:
        future.append(name)


# ============================================================================
# Purpose: Delete expired run directories while guaranteeing that backups which
#          actually contain data survive, and never touch anything that does not
#          match this script's own naming.
# Database/ORM: None. Reads each run's manifest.json to decide what it holds.
# Standards: Retention is content-aware because a directory is not evidence of a
#            backup. Five invariants, in force regardless of --keep-days and
#            --keep-min:
#              1. THE NEWEST RUN PROVEN TO HAVE CONTENT IS NEVER DELETED. Not by
#                 age, not by arithmetic, not when every other run is expired.
#              2. --keep-min protects the newest runs that are not proven empty.
#                 A run whose manifest records no seeded data does not consume a
#                 protection slot, so seven empty nights can no longer push the
#                 only run with data out of the window.
#              3. A run whose content cannot be determined counts as content.
#                 Deleting the last good backup is unrecoverable; keeping a
#                 directory that turns out to be junk costs disk. "Cannot be
#                 determined" now includes a directory that does not hold the
#                 artifacts its manifest describes -- so a planted or half-copied
#                 manifest is protected from deletion but cannot become the
#                 "newest run with content" that invariant 1 pins.
#              4. Deletion is restricted to immediate children of --out-dir whose
#                 names match RUN_DIR_RE / PARTIAL_RE / REJECTED_RE, anchored at
#                 both ends, AND whose timestamp actually parses.
#              5. A name that matches the shape but is not a run this box could
#                 have produced -- not a date, or dated in the FUTURE -- is
#                 REPORTED and left alone. The non-date case used to raise
#                 ValueError straight out of the process, which killed a run
#                 whose backup had already been written and left last-run.json
#                 green from the run before. The future case used to be far
#                 worse than an exception: a directory stamped
#                 ``ums-backup-20990101T000000Z`` sorts above every real run, so
#                 it was simultaneously the ``--keep-min`` tail AND invariant
#                 1's "newest run with content" pin. MEASURED with
#                 ``--keep-days 0 --keep-min 1``: all THREE real runs were
#                 deleted, including the one just published, and only the plant
#                 survived -- every genuine backup destroyed by the invariant
#                 whose stated purpose is that the newest run with content is
#                 never deleted. ``_run_stamp`` now refuses it, so it enters
#                 neither the protection list nor the deletion list.
#            The caller adds a sixth: retention runs only after a backup that
#            passed the content gate, and only after the watermark is durable
#            (see ``main``).
# Blast Radius: Destructive, on the backup directory only, and the one place in
#               this script that can lose history. Invariant 1 is enforced by
#               pinning that run before any age comparison happens rather than
#               emerging from the ordering of the other rules.
# Connections:
#   - File: scripts/backup_database.py -> ``_run_has_content`` supplies the
#     three-valued classification; ``_run_stamp`` supplies invariants 4 and 5.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> retention defaults.
# ============================================================================
def _prune(out_dir: Path, *, keep_days: int, keep_min: int, now: datetime) -> PruneOutcome:
    skipped: list[str] = []
    future: list[str] = []
    runs: list[tuple[Path, datetime]] = []
    for child in sorted(out_dir.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue
        if RUN_DIR_RE.match(child.name) is not None:
            # ``now`` rather than the wall clock, so every decision this pass
            # makes is judged against ONE clock reading.
            stamped = _run_stamp(child.name, now=now)
            if stamped is None:
                _classify_unusable(child.name, RUN_DIR_RE, skipped=skipped, future=future)
                continue
            runs.append((child, stamped))
    classified = [(run, _run_has_content(run)) for run, _ in runs]
    # Invariant 2: proven-empty runs are filtered out BEFORE the --keep-min slice
    # is taken, so they cannot occupy a protection slot. Slicing first and
    # filtering after would leave the original defect intact -- seven empty runs
    # would still fill the window and evict the run that holds the data.
    # Invariant 3 keeps `None` (unknown) in the eligible list.
    eligible = [run.name for run, has_content in classified if has_content is not False]
    protected = set(eligible[-max(keep_min, 1) :])
    # Invariant 1: pin the newest run that proves it holds data, whatever its age
    # and whatever --keep-min worked out to.
    newest_with_content = next(
        (run.name for run, has_content in reversed(classified) if has_content is True), None
    )
    if newest_with_content is not None:
        protected.add(newest_with_content)

    cutoff = now - timedelta(days=max(keep_days, 0))
    removed: list[str] = []
    for run, stamped in runs:
        if run.name in protected or stamped >= cutoff:
            continue
        shutil.rmtree(run)
        removed.append(run.name)
    for child in out_dir.iterdir():
        if not child.is_dir():
            continue
        if REJECTED_RE.match(child.name) is not None:
            # Quarantined runs are tiny and are kept for the same window as real
            # runs so a week of empty nights stays visible in the directory.
            stamped_or_none = _run_stamp(child.name, REJECTED_RE, now=now)
            if stamped_or_none is None:
                _classify_unusable(child.name, REJECTED_RE, skipped=skipped, future=future)
            elif stamped_or_none < cutoff:
                shutil.rmtree(child)
                removed.append(child.name)
        elif PARTIAL_RE.match(child.name):
            age = now - datetime.fromtimestamp(child.stat().st_mtime, tz=UTC)
            if age > timedelta(days=1):
                shutil.rmtree(child)
                removed.append(child.name)
    return PruneOutcome(removed=removed, skipped=sorted(set(skipped)), future=sorted(set(future)))


# ============================================================================
# Purpose: Write one status file, absorbing the transient share-mode lock that
#          is the ordinary Windows reason a status write fails.
# Database/ORM: None.
# Standards: Bounded retry, then raise. It deliberately does NOT swallow: the
#            caller decides what an undeliverable status means, and for this
#            script it means the run may not exit 0. An AV scanner, OneDrive or
#            an open editor holding the file with FileShare.None releases it in
#            seconds; ~2s of retry converts that into a normal run, and anything
#            longer is a real problem the operator has to see.
# Blast Radius: Operator contract.
# Connections:
#   - File: scripts/backup_database.py -> ``_append_log`` and
#     ``_write_last_run`` are its only callers.
# ============================================================================
def _write_status_file(path: Path, body: str, *, append: bool) -> None:
    mode = "a" if append else "w"
    for attempt in range(1, STATUS_WRITE_ATTEMPTS + 1):
        try:
            with path.open(mode, encoding="utf-8") as handle:
                handle.write(body)
            return
        except OSError:
            if attempt == STATUS_WRITE_ATTEMPTS:
                raise
            time.sleep(STATUS_WRITE_BACKOFF_SECONDS)


def _append_log(out_dir: Path, line: str) -> bool:
    """Append one audit line to the durable log. True only if it landed."""
    try:
        _write_status_file(out_dir / LOG_NAME, line + "\n", append=True)
    except OSError as exc:
        print(f"WARNING: could not write {LOG_NAME}: {exc}", file=sys.stderr)
        return False
    return True


# ============================================================================
# Purpose: Replace the at-a-glance status file an unattended box is judged by,
#          and when it cannot be replaced, refuse to let the previous run's
#          record pass for this one.
# Database/ORM: None.
# Standards: Returns whether the canonical file landed; it never raises, because
#            a failure to report must not become a second failure. On failure it
#            writes this run's record to a STAMPED sidecar instead. A new file
#            name is the one write a share-mode lock on the canonical file
#            cannot block, and the stamp means the sidecar is never itself stale
#            -- a fixed second name could be locked, or left behind, and become
#            the same lie in a different file. MEASURED before this existed:
#            with last-run.json held FileShare.None, a run that ended in TOTAL
#            DATA LOSS exited 8 while last-run.json still read OK/exit=0, and
#            the only complaint went to stderr, which Task Scheduler discards.
# Blast Radius: Operator contract. Docs/22 tells the operator to read this file.
# Connections:
#   - File: scripts/backup_database.py -> ``_RunReport`` escalates an otherwise
#     successful run to EXIT_BOOKKEEPING_FAILED when this returns False.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> last-run.json contract.
# ============================================================================
def _write_last_run(
    out_dir: Path, payload: dict[str, object], *, sidecar_stamp: str | None = None
) -> bool:
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        _write_status_file(out_dir / LAST_RUN_NAME, body, append=False)
    except OSError as exc:
        print(f"WARNING: could not write {LAST_RUN_NAME}: {exc}", file=sys.stderr)
        if sidecar_stamp is not None:
            sidecar = out_dir / LAST_RUN_SIDECAR.format(stamp=sidecar_stamp)
            # The sidecar says why it exists. Its ``exit_code`` is the backup's
            # verdict; the PROCESS exits 7 on top of it, because the bookkeeping
            # failed, and a reader comparing the two would otherwise see a
            # contradiction with no explanation.
            annotated = dict(payload)
            annotated["status_note"] = (
                f"{LAST_RUN_NAME} could not be replaced, so this file is this run's "
                "record. The process exit code is 7 unless the run itself failed with "
                "something more specific."
            )
            try:
                _write_status_file(
                    sidecar, json.dumps(annotated, indent=2, sort_keys=True) + "\n", append=False
                )
            except OSError as sidecar_exc:
                print(f"WARNING: could not write {sidecar.name}: {sidecar_exc}", file=sys.stderr)
            else:
                print(f"WARNING: this run's record was written to {sidecar.name}", file=sys.stderr)
        return False
    return True


# ============================================================================
# Purpose: Make it impossible for one invocation to leave a stale green status
#          behind unnoticed -- either the record is replaced, or the run does not
#          exit 0 and says so.
# Database/ORM: None. Owns backup.log, last-run.json and the stamped sidecar.
# Standards: Two-phase, and the phases are ordered so that no later step can
#            defeat an earlier verdict.
#              * ``start`` overwrites last-run.json with RUNNING before any work
#                begins, so the previous run's OK stops standing the moment this
#                one starts. A power cut now reads as "did not finish", not as
#                "succeeded".
#              * exactly one terminal record is written per invocation, and
#                ``close`` writes an INTERRUPTED one from a finally block if
#                nothing else did -- which covers KeyboardInterrupt, SystemExit
#                and anything the except clauses do not name.
#              * ``_RunReport`` guarantees the CALL. It cannot guarantee the
#                WRITE, because another process can hold either file with
#                FileShare.None and the OS will refuse. So an undeliverable
#                record is escalated instead of absorbed: the write is retried,
#                this run's record goes to a stamped ``last-run-<stamp>.json``
#                beside the locked file, the operator is told in words that
#                last-run.json is showing the PREVIOUS run, and an otherwise
#                successful run returns EXIT_BOOKKEEPING_FAILED rather than 0.
#                The exit code is the one channel a lock cannot block, and it is
#                the channel Task Scheduler actually records.
#            The runbook's claim is therefore precisely: every run writes one
#            backup.log line and one terminal last-run.json, and a run that
#            could not do so does not exit 0.
# Blast Radius: Operator contract. This file and this exit code ARE how an
#               unattended machine is judged.
# Connections:
#   - File: scripts/backup_database.py -> ``main`` drives every transition and
#     passes the final code through ``escalate``.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> last-run.json contract.
# ============================================================================
class _RunReport:
    def __init__(self, out_dir: Path, started: datetime) -> None:
        self._out_dir = out_dir
        self._started = started
        self._final = False
        self._published: str | None = None
        self._undelivered: list[str] = []

    @property
    def _stamp(self) -> str:
        return self._started.strftime(STAMP_FORMAT) + "Z"

    @property
    def status_durable(self) -> bool:
        return not self._undelivered

    def start(self) -> None:
        written = _write_last_run(
            self._out_dir,
            {
                "status": "RUNNING",
                "exit_code": None,
                "started_utc": self._started.isoformat(),
                "note": (
                    "A run is in progress, or one ended without recording a verdict. "
                    "This is never a successful backup."
                ),
            },
            sidecar_stamp=self._stamp,
        )
        if not written:
            # The moment that matters most: until this lands, the PREVIOUS run's
            # verdict is still the one standing in the file.
            self._undelivered.append(f"{LAST_RUN_NAME} could not be cleared to RUNNING")
            print(
                f"WARNING: {LAST_RUN_NAME} still shows the PREVIOUS run and could not "
                "be replaced. Do not read it as this run's result.",
                file=sys.stderr,
            )

    def note_published(self, run_dir: Path) -> None:
        self._published = str(run_dir)

    def finalise(self, line: str, payload: dict[str, object]) -> None:
        self._final = True
        if not _append_log(self._out_dir, line):
            self._undelivered.append(f"{LOG_NAME} could not be appended to")
        if not _write_last_run(self._out_dir, payload, sidecar_stamp=self._stamp):
            self._undelivered.append(f"{LAST_RUN_NAME} could not be replaced")

    def escalate(self, code: int) -> int:
        """Turn an undeliverable status into a visible exit code.

        Only 0 is escalated. A run that already failed carries a more specific
        code, and overwriting it with 7 would hide what went wrong.
        """
        if not self._undelivered:
            return code
        print(
            "WARNING: this run's status could not be recorded: "
            + "; ".join(self._undelivered)
            + f". {LAST_RUN_NAME} may still show an OLDER run.",
            file=sys.stderr,
        )
        if code != EXIT_OK:
            return code
        print(
            f"BACKUP PUBLISHED, STATUS NOT RECORDED (exit {EXIT_BOOKKEEPING_FAILED}): the "
            "backup itself is valid, but nothing on this box would have shown that this "
            "run happened.",
            file=sys.stderr,
        )
        return EXIT_BOOKKEEPING_FAILED

    def close(self) -> None:
        if self._final:
            return
        detail = "the process ended without recording a verdict"
        self.finalise(
            f"{self._started.isoformat()} INTERRUPTED exit=unknown {detail}",
            {
                "status": "INTERRUPTED",
                "exit_code": None,
                "started_utc": self._started.isoformat(),
                "run_dir": self._published,
                "error": detail,
            },
        )


# ============================================================================
# Purpose: One backup run end to end -- wait for Docker, locate the container,
#          dump roles and data into a .partial directory, verify both, judge the
#          payload against the content gate, then either publish the run or
#          quarantine it under a name that is not a backup.
# Database/ORM: Read-only against the application database.
# Standards: Nothing reaches its final name until every check has passed, so a
#            crashed, killed or timed-out run leaves a .partial directory that
#            can never be mistaken for a backup. A run that fails the content
#            gate is renamed ``...Z.rejected`` instead: the artifacts survive for
#            diagnosis, but the name keeps it out of RUN_DIR_RE, so it is not a
#            backup to retention, not a watermark contribution, and not
#            restorable. The caller turns that into exit 8. The watermark is
#            read BEFORE the dump so the comparison is against runs that existed
#            independently of this one.
# Blast Radius: Disaster recovery; no application state is touched.
# Connections:
#   - File: scripts/restore_database.py -> reads manifest.json, roles.sql and
#     database.dump from this layout, and refuses a rejected run.
#   - File: scripts/backup_database.py -> ``_evaluate_content`` supplies the
#     verdict this function acts on; ``main`` persists ``next_watermark``.
# ============================================================================
def run_backup(args: argparse.Namespace, out_dir: Path) -> BackupOutcome:
    started = _utc_now()
    docker_version = _await_docker(args.wait_for_docker, timeout=args.docker_timeout)
    container = _resolve_container(
        explicit=args.container,
        project=args.project,
        service=args.service,
        wait_seconds=args.wait_for_docker,
        timeout=args.docker_timeout,
    )
    _await_postgres(container, wait_seconds=args.wait_for_docker, timeout=args.docker_timeout)

    final_dir = out_dir / RUN_DIR_TEMPLATE.format(stamp=started.strftime(STAMP_FORMAT))
    rejected_dir = out_dir / (final_dir.name + REJECTED_SUFFIX)
    staging = out_dir / (final_dir.name + PARTIAL_SUFFIX)
    if final_dir.exists():
        raise BackupError(EXIT_USAGE, f"{final_dir} already exists")
    if rejected_dir.exists():
        raise BackupError(EXIT_USAGE, f"{rejected_dir} already exists")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, mode=0o700)
    _restrict_run_dir_mode(staging)

    watermark = _load_watermark(out_dir)
    expected_identity = _load_identity(out_dir)

    moved = False
    try:
        roles = _dump_roles(
            container,
            staging / ROLES_NAME,
            timeout=args.timeout,
            include_passwords=args.include_role_passwords,
        )
        counts = _dump_database_and_count(
            container, staging / DUMP_NAME, timeout=args.timeout
        )
        toc_entries = -1
        if args.verify_dump:
            toc_entries = _verify_dump_readable(
                container, staging / DUMP_NAME, timeout=args.timeout
            )
        # Provenance is collected BEFORE the verdict, not after: the identity it
        # carries is one of the gate's inputs, and the manifest has to record the
        # same facts the gate judged.
        facts = _container_facts(container, timeout=args.docker_timeout)
        observed_identity = _identity_from_source(facts)
        if observed_identity is None:
            raise BackupError(
                EXIT_COMMAND_FAILED,
                "could not establish database identity via current_database()/"
                "system_identifier; refusing to publish without a comparable identity",
            )
        verdict = _evaluate_content(
            counts,
            watermark,
            accept_drop=bool(args.accept_content_drop),
            establish=bool(args.establish_watermark),
            toc_entries=toc_entries,
            accept_empty=bool(args.this_database_is_intentionally_empty),
            expected_identity=expected_identity,
            observed_identity=observed_identity,
            adopt_database=bool(args.adopt_database),
        )
        next_watermark, reset = _next_watermark(watermark, counts, verdict)
        manifest: dict[str, object] = {
            "schema": MANIFEST_SCHEMA,
            "created_utc": started.isoformat(),
            "docker_server_version": docker_version,
            "source": facts,
            "roles_required": list(REQUIRED_ROLES),
            "roles_verified": roles,
            "roles_passwords_included": bool(args.include_role_passwords),
            "artifacts": {
                name: {
                    "bytes": (staging / name).stat().st_size,
                    "sha256": _sha256(staging / name),
                }
                for name in (DUMP_NAME, ROLES_NAME)
            },
            "dump_toc_entries": toc_entries,
            "table_row_counts": counts,
            "table_row_counts_note": ROW_COUNT_NOTE,
            "content_gate": verdict.as_manifest_block(),
            "watermark_after": next_watermark if verdict.accepted else None,
            "completed_utc": _utc_now().isoformat(),
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # The manifest is written either way. A quarantined run keeps its
        # artifacts and its verdict so the operator can see what the database
        # looked like at 02:00, but lands outside RUN_DIR_RE.
        destination = final_dir if verdict.accepted else rejected_dir
        staging.rename(destination)
        _restrict_run_dir_mode(destination)
        moved = True
    finally:
        if not moved:
            shutil.rmtree(staging, ignore_errors=True)
    return BackupOutcome(
        run_dir=destination,
        manifest=manifest,
        verdict=verdict,
        counts=counts,
        next_watermark=next_watermark,
        watermark_reset=reset,
        identity=observed_identity,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Back up the UMS Smart Revenue Postgres container to a host directory "
            "(pg_dump --format=custom plus pg_dumpall --roles-only)."
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help=(
            "Host directory holding the timestamped run directories. Falls back to "
            "UMS_BACKUP_DIR. There is deliberately no default inside the repository: "
            "a backup that lives in the working tree dies with the working tree."
        ),
    )
    parser.add_argument(
        "--container",
        default=None,
        help="Container name or ID to dump. Overrides the --project/--service lookup.",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Compose project label.")
    parser.add_argument("--service", default=DEFAULT_SERVICE, help="Compose service label.")
    parser.add_argument(
        "--wait-for-docker",
        type=int,
        default=0,
        help=(
            "Seconds to wait for the Docker daemon, the container and Postgres. "
            "Docker Desktop starts at user login, not at boot, so the scheduled "
            "task should pass a generous value such as 900."
        ),
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=30,
        help="Delete run directories older than this many days (default 30).",
    )
    parser.add_argument(
        "--keep-min",
        type=int,
        default=7,
        help="Always keep this many most recent complete runs (default 7, minimum 1).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Seconds allowed for each pg_dump / pg_dumpall / psql call.",
    )
    parser.add_argument(
        "--docker-timeout",
        type=int,
        default=60,
        help="Seconds allowed for each docker CLI probe.",
    )
    parser.add_argument(
        "--include-role-passwords",
        action="store_true",
        help=(
            "Dump role password verifiers too. Off by default: app_tenant and "
            "app_platform are NOLOGIN, and the beta login role is recreated from "
            "POSTGRES_PASSWORD, so the default loses nothing and keeps verifiers "
            "out of a plaintext file on disk."
        ),
    )
    parser.add_argument(
        "--no-verify-dump",
        dest="verify_dump",
        action="store_false",
        help=(
            "Skip piping the written dump back through pg_restore --list. Only use "
            "this if that check proves unreliable on the host: it is the only thing "
            "distinguishing a readable archive from a file of the right size."
        ),
    )
    parser.add_argument(
        "--no-prune",
        dest="prune",
        action="store_false",
        help="Take the backup but do not apply the retention policy.",
    )
    parser.add_argument(
        "--accept-content-drop",
        action="store_true",
        help=(
            "Accept a run whose tables or rows fell below this directory's "
            "high-water mark, and lower the mark for the tables that were named. "
            "For ONE deliberate re-baseline after the operator really did delete "
            "data or roll back a migration -- without it the comparison would "
            "reject every future run against a number that can no longer be met. "
            "It lowers ONLY the tables named by the checks it suppressed, never "
            "the whole mark -- though suppressing the whole-directory collapse "
            "check necessarily re-baselines every table that shrank, because that "
            "check is about the total. It does NOT override the seed floor: a "
            "database with no "
            "tables, or with alembic_version/currencies/tenants empty, is rejected "
            "with or without this flag. Its use is recorded in manifest.json, "
            "watermark.json and last-run.json. Do not put it in the scheduled task."
        ),
    )
    parser.add_argument(
        "--establish-watermark",
        action="store_true",
        help=(
            "Permit the FIRST run into an output directory that has no watermark "
            "yet. Without a watermark nothing can tell a healthy database from one "
            "that was wiped and re-migrated -- both are 38 tables of seeded lookup "
            "data -- so that run is refused with exit 8 and the numbers printed. "
            "Read them, confirm they are what this database should hold, then re-run "
            "once with this flag. It is NOT enough on its own when every table "
            "outside the seeded ones is empty: that case additionally requires "
            "--this-database-is-intentionally-empty. Inert once a watermark exists. "
            "Do not put it in the scheduled task: it would re-arm the hole every "
            "time the output directory changed."
        ),
    )
    parser.add_argument(
        "--this-database-is-intentionally-empty",
        action="store_true",
        help=(
            "Only meaningful with --establish-watermark, and only on the first run "
            "into a new output directory. Confirms that a database whose every table "
            "outside alembic_version/currencies/tenants is EMPTY is a brand-new "
            "install rather than one you have just lost. Without it that combination "
            "is refused, because 'exit 8, so re-run with --establish-watermark' is "
            "exactly the sequence that publishes a wiped database and makes it the "
            "reference. Deliberately long: it must not be reachable by copying a "
            "remediation line. If you are here after losing data, RESTORE first."
        ),
    )
    parser.add_argument(
        "--adopt-database",
        action="store_true",
        help=(
            "Rebind this output directory to the database this run actually read. "
            "An out-dir records the cluster's system_identifier and the database "
            "name of the first run it accepted, and refuses a later run against a "
            "different one -- that is how a --container/--project typo backing up "
            "somebody else's Postgres is caught, which no row count can see. Pass "
            "this ONCE after a legitimate rebuild (a restore into a new volume, a "
            "re-initialised cluster). Recorded in manifest.json, watermark.json and "
            "last-run.json. Do not put it in the scheduled task."
        ),
    )
    parser.set_defaults(verify_dump=True, prune=True)
    return parser.parse_args(argv)


def _resolve_out_dir(raw: str | None) -> Path:
    value = raw or os.environ.get("UMS_BACKUP_DIR") or ""
    if not value.strip():
        raise BackupError(
            EXIT_USAGE,
            "No output directory. Pass --out-dir or set UMS_BACKUP_DIR to a host "
            "path outside the repository; a different physical disk is better.",
        )
    out_dir = Path(value).expanduser()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(EXIT_USAGE, f"cannot create {out_dir}: {exc}") from exc
    if not out_dir.is_dir():
        raise BackupError(EXIT_USAGE, f"{out_dir} is not a directory")
    return out_dir


# ============================================================================
# Purpose: CLI entrypoint. Resolve the host output directory first so that even
#          a total failure leaves a durable timestamped trace, then run the
#          backup and report a distinct exit code per failure class.
# Database/ORM: None directly.
# Standards: Every exit path ATTEMPTS backup.log and last-run.json, enforced by
#            ``_RunReport`` -- ``start`` before any work, exactly one terminal
#            record after it, and a finally-block fallback for anything that
#            escapes. Whether the write lands is the OS's decision, not this
#            script's, so ``escalate`` runs last: a run whose record did not land
#            cannot return 0. A bare ``except Exception`` is the last-resort arm:
#            nothing is swallowed, the traceback goes to stderr and a one-line
#            summary goes to the log, and exit 9 is documented. Before this,
#            ANY unexpected exception -- a ValueError out of _prune's strptime,
#            for instance -- killed the process with an undocumented exit 1
#            AFTER the backup had been published, wrote no log line, and left
#            last-run.json reading OK from the previous run.
#
#            Ordering is load-bearing: watermark first, retention second.
#            Retention deletes manifests, and the manifests are the watermark's
#            recovery path, so pruning before persisting the mark could lower it.
#            Retention invariant 6, which belongs here rather than in ``_prune``:
#            pruning runs ONLY after a backup that passed the content gate. A
#            night that captured nothing deletes nothing, so a run of empty
#            nights cannot age out the last good backup no matter how long it
#            lasts.
# Blast Radius: Operator contract -- Task Scheduler reads the exit code and the
#               runbook reads last-run.json.
# Connections:
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> exit-code table.
#   - File: scripts/backup_database.py -> ``_prune`` holds invariants 1-5.
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    started = _utc_now()
    try:
        out_dir = _resolve_out_dir(args.out_dir)
    except BackupError as exc:
        print(f"BACKUP FAILED (exit {exc.code}): {exc}", file=sys.stderr)
        return exc.code

    report = _RunReport(out_dir, started)
    report.start()
    code = EXIT_INTERNAL
    try:
        code = _execute(args, out_dir, report, started)
    except BackupError as exc:
        _record_failure(report, started, exc.code, str(exc))
        code = exc.code
    except (OSError, subprocess.SubprocessError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        _record_failure(report, started, EXIT_COMMAND_FAILED, detail)
        code = EXIT_COMMAND_FAILED
    # Last resort, and deliberately broad. Nothing is swallowed: the traceback
    # goes to stderr, a one-line summary goes to backup.log, last-run.json stops
    # reading green, and exit 9 is documented. The alternative -- letting an
    # unexpected exception escape -- is the defect being fixed.
    except Exception as exc:
        traceback.print_exc()
        detail = f"unexpected {type(exc).__name__}: {exc}"
        _record_failure(report, started, EXIT_INTERNAL, detail)
        code = EXIT_INTERNAL
    finally:
        report.close()
    # FIX: the status files are written by ``_RunReport``, not guaranteed by it.
    # A run whose record could not land must not be able to report success, so
    # the escalation happens after ``close`` -- which is itself a writer.
    return report.escalate(code)


def _execute(args: argparse.Namespace, out_dir: Path, report: _RunReport, started: datetime) -> int:
    with _exclusive_backup_lock(out_dir):
        outcome = run_backup(args, out_dir)
        report.note_published(outcome.run_dir)

        if not outcome.accepted:
            # Retention invariant 6. A run that captured nothing gets no say over
            # what is deleted, so the previous good backups are untouched however
            # many empty nights follow one another.
            _record_rejected(report, outcome, started)
            return EXIT_NO_CONTENT

        try:
            _write_watermark(
                out_dir,
                outcome.next_watermark,
                run=outcome.run_dir.name,
                reset=outcome.watermark_reset,
                now=_utc_now(),
                identity=outcome.identity,
            )
        except OSError as exc:
            _record_bookkeeping_failure(
                report, outcome, started, f"could not write {WATERMARK_NAME}: {exc}"
            )
            return EXIT_BOOKKEEPING_FAILED

        pruned = PruneOutcome()
        if args.prune:
            try:
                pruned = _prune(
                    out_dir, keep_days=args.keep_days, keep_min=args.keep_min, now=_utc_now()
                )
            except OSError as exc:
                _record_bookkeeping_failure(report, outcome, started, f"retention failed: {exc}")
                return EXIT_BOOKKEEPING_FAILED

        _record_success(report, outcome, started, pruned)
        return EXIT_OK


def _record_failure(report: _RunReport, started: datetime, code: int, detail: str) -> None:
    report.finalise(
        f"{started.isoformat()} FAILED exit={code} {detail}",
        {
            "status": "FAILED",
            "exit_code": code,
            "started_utc": started.isoformat(),
            "error": detail,
        },
    )
    print(f"BACKUP FAILED (exit {code}): {detail}", file=sys.stderr)


def _total_artifact_bytes(manifest: dict[str, object]) -> tuple[dict[str, object], int]:
    raw_artifacts = manifest.get("artifacts")
    artifacts = raw_artifacts if isinstance(raw_artifacts, dict) else {}
    total = 0
    for entry in artifacts.values():
        if isinstance(entry, dict):
            total += int(entry.get("bytes", 0))
    return artifacts, total


def _record_success(
    report: _RunReport,
    outcome: BackupOutcome,
    started: datetime,
    pruned: PruneOutcome,
) -> None:
    verdict = outcome.verdict
    artifacts, total_bytes = _total_artifact_bytes(outcome.manifest)
    run_dir = outcome.run_dir
    watermark_rows = sum(outcome.next_watermark.values())
    report.finalise(
        f"{started.isoformat()} OK run={run_dir.name} bytes={total_bytes} "
        f"tables={verdict.tables} rows={verdict.rows} "
        f"watermark_rows={watermark_rows} "
        f"watermark={verdict.watermark.source} "
        f"overrides={len(verdict.overridden)} pruned={len(pruned.removed)}",
        {
            "status": "OK",
            "exit_code": EXIT_OK,
            "started_utc": started.isoformat(),
            "run_dir": str(run_dir),
            "bytes": total_bytes,
            "tables": verdict.tables,
            "rows": verdict.rows,
            "content_gate": verdict.as_manifest_block(),
            "watermark_after": {
                "tables": len(outcome.next_watermark),
                "rows": watermark_rows,
                "reset": outcome.watermark_reset or None,
            },
            "pruned": pruned.removed,
            "unparsable_dirs": pruned.skipped,
            "future_dated_dirs": pruned.future,
        },
    )
    print(f"OK backup={run_dir}")
    print(f"   artifacts={', '.join(sorted(artifacts))} bytes={total_bytes}")
    print(f"   tables={verdict.tables} rows={verdict.rows} non_seed_rows={verdict.non_seed_rows}")
    if verdict.observed_identity is not None:
        print(f"   {verdict.observed_identity.describe()}")
    if verdict.established:
        # Say it out loud. This run is the number every later run is judged
        # against, and the operator asked for that by passing the flag.
        print(
            "   content gate: seed floor only - this run ESTABLISHES the watermark "
            f"at {verdict.tables} tables / {verdict.rows} rows"
        )
    else:
        print(
            f"   content gate: watermark {verdict.watermark.total_rows} rows across "
            f"{verdict.watermark.table_count} tables ({verdict.watermark.source})"
        )
    # FIX: every override used to be printed as "OVERRIDDEN by
    # --accept-content-drop", which became a lie the moment a second and a third
    # acknowledgement flag existed. The note now names the flag that suppressed
    # it, so the operator reading the console sees which one they passed.
    for note in verdict.overridden:
        print(f"   OVERRIDDEN {note}")
    if outcome.watermark_reset:
        print(f"   watermark lowered for: {outcome.watermark_reset}")
    if pruned.removed:
        print(f"   pruned={', '.join(pruned.removed)}")
    for name in pruned.skipped:
        print(
            f"   WARNING: {name} matches a run directory name but its timestamp is "
            "not a date. It was left untouched; rename or remove it by hand.",
            file=sys.stderr,
        )
    for name in pruned.future:
        print(
            f"   WARNING: {name} is dated in the FUTURE, so it is not a run that has "
            "happened. It contributes nothing to the watermark, protects nothing from "
            "retention, and was left untouched. Check this box's clock, then delete it.",
            file=sys.stderr,
        )


# ============================================================================
# Purpose: Report a run whose artifacts are good and published but whose
#          post-backup bookkeeping -- the watermark, or retention -- failed.
# Database/ORM: None.
# Standards: The exit code is 7 and the status is not OK, because the NEXT run's
#            protection is degraded when the watermark did not land. The record
#            still names the published run directory, so the operator can see
#            that the backup itself is valid and usable.
# Blast Radius: Operator contract.
# Connections:
#   - File: scripts/backup_database.py -> ``_execute`` calls this for both the
#     watermark write and the retention pass.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> exit-code 7 row.
# ============================================================================
def _record_bookkeeping_failure(
    report: _RunReport, outcome: BackupOutcome, started: datetime, detail: str
) -> None:
    verdict = outcome.verdict
    report.finalise(
        f"{started.isoformat()} BOOKKEEPING-FAILED exit={EXIT_BOOKKEEPING_FAILED} "
        f"run={outcome.run_dir.name} tables={verdict.tables} rows={verdict.rows} {detail}",
        {
            "status": "BOOKKEEPING_FAILED",
            "exit_code": EXIT_BOOKKEEPING_FAILED,
            "started_utc": started.isoformat(),
            "run_dir": str(outcome.run_dir),
            "backup_published": True,
            "tables": verdict.tables,
            "rows": verdict.rows,
            "error": detail,
        },
    )
    print(
        f"BACKUP PUBLISHED, BOOKKEEPING FAILED (exit {EXIT_BOOKKEEPING_FAILED}): {detail}",
        file=sys.stderr,
    )
    print(f"   the backup itself is valid: {outcome.run_dir}", file=sys.stderr)


# ============================================================================
# Purpose: Report a run that was quarantined by the content gate, in the three
#          places an unattended machine is judged by: the process exit code, the
#          durable log, and last-run.json.
# Database/ORM: None.
# Standards: status is REJECTED and exit_code is 8, never OK/0 -- the whole
#            defect being fixed here was a green Last Run Result over an empty
#            database. The reasons are written verbatim so the operator does not
#            have to reconstruct them, and the quarantined directory is named so
#            it can be inspected.
# Blast Radius: Operator contract. This is the red light.
# Connections:
#   - File: scripts/backup_database.py -> ``_evaluate_content`` produced the
#     verdict; ``_execute`` skips retention entirely on this path.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> exit-code 8 row.
# ============================================================================
def _record_rejected(report: _RunReport, outcome: BackupOutcome, started: datetime) -> None:
    verdict = outcome.verdict
    _, total_bytes = _total_artifact_bytes(outcome.manifest)
    reasons = "; ".join(verdict.failures)
    report.finalise(
        f"{started.isoformat()} REJECTED exit={EXIT_NO_CONTENT} "
        f"run={outcome.run_dir.name} bytes={total_bytes} "
        f"tables={verdict.tables} rows={verdict.rows} {reasons}",
        {
            "status": "REJECTED",
            "exit_code": EXIT_NO_CONTENT,
            "started_utc": started.isoformat(),
            "quarantined_dir": str(outcome.run_dir),
            "bytes": total_bytes,
            "tables": verdict.tables,
            "rows": verdict.rows,
            "content_gate": verdict.as_manifest_block(),
            "error": reasons,
        },
    )
    print(
        f"BACKUP REJECTED (exit {EXIT_NO_CONTENT}): this run captured no usable "
        "application data, so it was NOT published as a backup.",
        file=sys.stderr,
    )
    print(f"   quarantined={outcome.run_dir}", file=sys.stderr)
    print(
        f"   tables={verdict.tables} rows={verdict.rows} non_seed_rows={verdict.non_seed_rows}",
        file=sys.stderr,
    )
    print(f"   watermark={verdict.watermark.source}", file=sys.stderr)
    if verdict.observed_identity is not None:
        print(f"   read {verdict.observed_identity.describe()}", file=sys.stderr)
    for reason in verdict.failures:
        print(f"   - {reason}", file=sys.stderr)
    print(
        "   Retention was skipped, so earlier backups were left alone.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
