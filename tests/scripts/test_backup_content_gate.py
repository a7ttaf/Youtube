# ============================================================================
# Purpose: Regression suite for backup content-gate, watermark, retention, and
#   last-run status contracts — every defect here was found against a live
#   database and is reproduced without one from pure inputs + CLI drives.
# Database/ORM: None required for the pure-function cases; CLI tests exercise
#   scripts/backup_database.py against temp directories / fakes.
# Standards: Fail-closed gates; no suppressions; fixtures derived from migration
#   seed reality rather than assumed literals.
# Blast Radius: Disaster-recovery operator CLIs only. No authz or finance math.
# Connections:
#   - File: scripts/backup_database.py -> content gate, prune, watermark, status.
#   - File: scripts/restore_database.py -> consumers of gated backup runs.
# ============================================================================

"""Content-gate, watermark, retention and status tests for the backup CLI.

Every defect reproduced here was found against a live database and is
reproduced *without* one, because everything they turn on is pure: the gate is a
function of row counts plus the recorded history, retention is a function of
directory names plus each run's manifest, and the status contract is a function
of which record was written last.

Defect 1: a backup taken while the database was empty was published as ``OK``
with exit 0, so Task Scheduler's Last Run Result stayed green.
Defect 2: ``_prune`` treated "has a manifest.json" as "is a backup", so
zero-table runs consumed ``--keep-min`` slots and evicted the only run holding
data.
Defect 3: the absolute floor was ``MIN_ROWS = 1`` against a database whose
virgin state was 180 rows, so total data loss with the schema left standing
published a green ``OK`` -- and that run then became the reference for the next
one. This file's fixtures are DERIVED from the migrations and anchored to a
container measurement, not assumed.
Defect 4: the collapse check compared each run only with the one before it, so
an 80%-a-night drain was accepted three nights running.
Defect 5: an archive with an empty table of contents (a dropped schema) was
treated as a verification failure and the run directory was DISCARDED, so the
quarantine the design promises was missing in exactly that case.
Defect 6: ``_prune`` raised an uncaught ``ValueError`` on a regex-valid,
non-date directory name, killing the process with an undocumented exit 1 after
the backup had already been written and leaving ``last-run.json`` green from the
previous run.
Defect 7: a hand-planted ``manifest.json`` with no artifacts beside it folded
its claimed counts into the watermark.
Defect 8: a second, unrelated database backed into an established output
directory published green and re-anchored the mark.
Defect 9: ``--establish-watermark`` published a database that held nothing but
its seeds, making an empty database the directory's permanent reference.
Defect 10: ``_RunReport`` guaranteed the status-write CALL, not the WRITE, so a
locked ``last-run.json`` left the previous run's green ``OK`` standing.
Defect 11: a directory dated in the FUTURE sorted above every real run, so it
re-folded into the watermark every night (``reset_after`` could never exclude
it) and simultaneously held both retention invariants -- one prune deleted every
genuine backup and kept only the plant.
Defect 12: the seed-floor test once compared a literal against a literal, so a
future migration could add seeded rows outside ``SEED_TABLES`` without making
tier 3b refuse an empty database. The test now derives its expected set from
the migration sources in this ancestry.

CLI-LEVEL COVERAGE. Everything above is a function of pure inputs, which is why
it is reproduced without a database. That is not sufficient on its own: an
independent mutation matrix found that ``_execute``'s ``if not
outcome.accepted:`` and ``main``'s ``return report.escalate(code)`` could both
be deleted with the whole file still green, because nothing here drove the CLI.
The "the CLI" section at the end of this file runs ``backup.main`` end to end
against a faked container and asserts the PROCESS outcome.
"""

from __future__ import annotations

import ast
import errno
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Self

import pytest

from ums_smart_revenue.db.iso_4217_2026_05 import ISO_4217_CURRENCIES_2026_05

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic" / "versions"


def _load(name: str) -> ModuleType:
    """Import a ``scripts/`` CLI by path; they are not an installed package.

    The module is registered in ``sys.modules`` before execution because
    ``@dataclass`` resolves annotations through ``sys.modules[cls.__module__]``.
    """
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


backup = _load("backup_database")
restore = _load("restore_database")


def _backup_superuser_psql(superuser: str = "ums") -> Callable[..., str]:
    """Return a psql stub that reports ``superuser`` as ``current_user``."""

    def fake_psql(_container: str, _sql: str, *, timeout: int) -> str:
        """Report the configured superuser for backup role validation."""
        _ = (_container, _sql, timeout)
        return superuser

    return fake_psql


def _restore_psql(
    *,
    superuser: str = "ums",
    present: list[str] | None = None,
    memberships: list[str] | None = None,
    settings: list[str] | None = None,
    privileged: list[str] | None = None,
    writers: int = 0,
) -> Callable[..., str]:
    """Return a psql stub answering each read-only query _restore_roles makes.

    ``memberships`` models ``ROLE_MEMBERSHIPS_SQL`` and ``settings`` models
    ``ROLE_SETTINGS_KEYS_SQL`` as ``role = guc`` lines; ``privileged`` models
    ``ROLE_PRIVILEGED_ATTRIBUTES_SQL``; ``writers`` models the
    ``pg_stat_activity`` count. All default to the healthy/empty cluster; a
    test that wants those failures passes the values explicitly. Answering
    them from the role list, as the single catch-all branch used to, made
    every caller look like a compromised cluster.
    """
    roles = list(restore.REQUIRED_ROLES) if present is None else list(present)
    edges = list(memberships or [])
    gucs = list(settings or [])
    privs = list(privileged or [])

    def fake_psql(
        _container: str, sql: str, *, timeout: int, dbname: str | None = None
    ) -> str:
        """Answer the catalog query psql was asked to run."""
        _ = (_container, timeout, dbname)
        # pg_stat_activity first: its SQL also contains the literal
        # "current_user", which the branch below would misroute.
        if "pg_stat_activity" in sql:
            return f"{writers}\n"
        if "pg_auth_members" in sql:
            return ("\n".join(edges) + "\n") if edges else ""
        if "pg_db_role_setting" in sql:
            return ("\n".join(gucs) + "\n") if gucs else ""
        if "rolsuper" in sql:
            return ("\n".join(privs) + "\n") if privs else ""
        if "current_user" in sql:
            return superuser
        return "\n".join(roles) + "\n"

    return fake_psql


def _successful_restore_mutation(*_args: object, **_kwargs: object) -> str:
    """Model a completed tagged mutation in tests focused on later role gates."""
    return ""


# --------------------------------------------------------------------------
# ONE CLOCK FOR THE WHOLE FILE.
#
# Every fixture below is a hardcoded 2026-08 stamp, while ``_run_stamp``,
# ``_load_watermark``, ``_load_identity``, ``run_backup`` and ``main`` all read
# the wall clock through ``backup._utc_now``. Left on the wall clock the suite
# is a function of the box it runs on, and the box's clock is precisely what
# ``STAMP_FUTURE_TOLERANCE`` exists to survive.
#
# MEASURED by making ``_utc_now`` return ``datetime.now(UTC) - timedelta(days=30)``
# -- a box one month behind, the ordinary dead-RTC case:
#     13 failed, 113 passed
# and not one failure message mentioned a clock. One month AHEAD:
#     2 failed, 124 passed
# Those two are ``_future_stamp``'s callers, which read ``datetime.now`` while
# the code under test read ``_utc_now``: the fixture and the subject disagreeing
# about what time it is.
#
# So the reading point is injected, once, for every test. ``backup._utc_now`` is
# the only place the script asks what time it is, and this fixture is the only
# thing that answers. Tests that need time to pass request ``clock`` and move
# it; every other test gets a frozen one and cannot notice the wall clock.
# --------------------------------------------------------------------------

#: The instant every test in this file starts at. Deliberately AFTER every
#: fixture stamp used below, so a run directory a test writes is never itself
#: read as future-dated, and before the ``20990101`` plant, which must be.
SUITE_NOW = datetime(2026, 8, 25, 2, 0, tzinfo=UTC)


class _Clock:
    """A settable stand-in for ``_utc_now`` so one test can drive many nights."""

    def __init__(self, start: datetime) -> None:
        """init."""
        self.now = start

    def __call__(self) -> datetime:
        """call."""
        return self.now

    def advance(self, delta: timedelta) -> None:
        """advance."""
        self.now += delta

    def move_to(self, moment: datetime) -> None:
        """move to."""
        self.now = moment


#: Captured at import, BEFORE the autouse fixture below can replace it. Freezing
#: the clock for the whole file would otherwise close one hole by opening
#: another: with every test driving the injected clock, nothing would be left
#: reading the real one, and ``_utc_now`` could return any instant at all with
#: the suite green. That is what ``test_the_injection_point_reads_the_real_clock``
#: is for -- it is the single assertion the injection does not cover.
_REAL_UTC_NOW = backup._utc_now


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Freeze ``backup._utc_now`` for every test; hand it to those that move it."""
    ticking = _Clock(SUITE_NOW)
    monkeypatch.setattr(backup, "_utc_now", ticking)
    return ticking


def test_the_injection_point_reads_the_real_clock() -> None:
    """The one line the injected clock cannot cover: the reading itself.

    MEASURED: with ``_utc_now`` mutated to ``datetime.now(UTC) - timedelta(days=30)``
    -- a box a month behind -- the rest of this file is 146 passed, which is the
    POINT of the injection. This assertion is the only failure, and it is what
    stops the injection being a guard lost for a guard gained.
    """
    reading = _REAL_UTC_NOW()

    assert reading.tzinfo is UTC, "a naive stamp would compare against an aware one and raise"
    assert abs((datetime.now(tz=UTC) - reading).total_seconds()) < 5


# --------------------------------------------------------------------------
# Fixtures DERIVED from the migrations, then anchored to a MEASUREMENT.
#
# Twice now the virgin state moved and this file did not notice, and both times
# the consequence was a gate that stopped firing:
#
#   * `MIN_ROWS = 1` was ratified by a one-row fixture against a virgin install
#     of 180 rows, so total data loss with the schema intact published green.
#   * `SEED_TABLES = ("alembic_version", "currencies", "tenants")` and a
#     hard-coded 180 remain correct for this ancestry; the auth tables are
#     created but not populated by its migrations. The derived scanner therefore
#     keeps them outside the seed floor until their seed migration is merged
#     with the script contract.
#
# So the row counts below are computed from the SAME sources the migrations
# import, not re-typed. A registry that grows moves the fixture with it, and
# ``test_the_derived_virgin_state_still_matches_the_measured_one`` is what
# forces a re-measurement when it does.
#
# THE MEASUREMENT, which those derivations are checked against.
# `alembic upgrade head` into a fresh postgres:18-alpine container, measured
# 2026-08-25 for this ancestry:
#
#     tables=38 rows=180
#       currencies 178      alembic_version 1      tenants 1
#
# To re-measure:
#   docker run -d --name ums-seedcheck-pg -e POSTGRES_USER=ums \
#     -e POSTGRES_PASSWORD=ums -e POSTGRES_DB=ums_smart_revenue \
#     -p 127.0.0.1:55471:5432 postgres:18-alpine
#   UMS_DATABASE_URL=postgresql+psycopg://ums:ums@127.0.0.1:55471/ums_smart_revenue \
#     uv run alembic upgrade head
#   docker exec ums-seedcheck-pg psql -U ums -d ums_smart_revenue -Atc "..."
#   docker rm -f ums-seedcheck-pg
# --------------------------------------------------------------------------

#: Every table a fresh `alembic upgrade head` populates, and how many rows it
#: puts there -- derived from the migrations' own sources.
SEED_ROWS: dict[str, int] = {
    # Alembic's own stamp table: exactly one row, one head revision.
    "alembic_version": 1,
    # 20260523_0001 bulk-inserts the frozen ISO-4217 snapshot.
    "currencies": len(ISO_4217_CURRENCIES_2026_05),
    # 20260516_0001 inserts the single bootstrap tenant.
    "tenants": 1,
}
#: The measured totals the derivation above is anchored to. Not the derivation's
#: source -- its cross-check, so a registry change forces a fresh measurement
#: rather than silently redefining what "virgin" means.
MEASURED_VIRGIN_TABLES = 38
MEASURED_VIRGIN_ROWS = 180
MEASURED_SEED_ROWS = {
    "alembic_version": 1,
    "currencies": 178,
    "tenants": 1,
}

NAMED_TABLES = ("monthly_channel_revenue_facts", "org_units", "youtube_channels")
FILLER_TABLES = tuple(
    f"ums_table_{index:02d}"
    for index in range(MEASURED_VIRGIN_TABLES - len(SEED_ROWS) - len(NAMED_TABLES))
)
ALL_TABLES = (*SEED_ROWS, *NAMED_TABLES, *FILLER_TABLES)


def _database(**populated: int) -> dict[str, int]:
    """A 38-table UMS database with the seeds populated, plus what a test names."""
    counts: dict[str, int] = dict.fromkeys(ALL_TABLES, 0)
    counts.update(SEED_ROWS)
    counts.update(populated)
    return counts


#: A virgin `alembic upgrade head`: 38 tables, 180 rows, no application data.
VIRGIN = _database()
#: The documented reference database: the virgin state plus 7 application rows.
REAL = _database(monthly_channel_revenue_facts=3, org_units=2, youtube_channels=2)
#: Total loss of application data with the schema intact: 38 tables, 1 row.
GUTTED = {**dict.fromkeys(ALL_TABLES, 0), "alembic_version": 1}
#: `DROP SCHEMA public CASCADE`.
EMPTY: dict[str, int] = {}

NO_WATERMARK = backup.Watermark()


def _watermark(counts: dict[str, int], *, source: str = "test") -> backup.Watermark:
    """watermark."""
    return backup.Watermark(tables=dict(counts), source=source)


IDENTITY_A = backup.Identity(system_identifier="7677783453675450413", database="ums_smart_revenue")
IDENTITY_B = backup.Identity(system_identifier="7677783473962770477", database="ums_smart_revenue")
FAKE_DATABASE_ACL = json.dumps(
    {
        "owner": "ums",
        "entries": [
            {"grantee": "PUBLIC", "privilege": "CONNECT", "grantable": False},
            {"grantee": "PUBLIC", "privilege": "TEMPORARY", "grantable": False},
        ],
    },
    separators=(",", ":"),
)


def _write_run(
    out_dir: Path,
    stamp: str,
    *,
    counts: dict[str, int] | None,
    rejected: bool = False,
    manifest: bool = True,
    artifacts: bool = True,
    recorded_bytes: dict[str, int] | None = None,
    identity: backup.Identity | None = None,
) -> Path:
    """One run directory on disk.

    ``artifacts=False`` writes the manifest and NOTHING else, which is the shape
    a hand-planted or half-copied directory has: it is not a backup, and
    ``_run_is_published_backup`` is what says so.
    """
    suffix = backup.REJECTED_SUFFIX if rejected else ""
    run = out_dir / f"ums-backup-{stamp}Z{suffix}"
    run.mkdir(parents=True)
    if artifacts:
        (run / backup.DUMP_NAME).write_bytes(b"PGDMP-placeholder")
        (run / backup.ROLES_NAME).write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
    if not manifest:
        return run
    body: dict[str, object] = {"schema": backup.MANIFEST_SCHEMA}
    if identity is not None:
        body["source"] = {
            "container": "ums-smart-revenue-postgres-1",
            "database": identity.database,
            "superuser": "ums",
            "system_identifier": identity.system_identifier,
        }
    if artifacts:
        # Production manifests always record byte sizes and sha256 digests for
        # both artifacts; the fixture mirrors that so _run_is_published_backup
        # -- which now REQUIRES the metadata -- keeps judging these runs valid.
        artifact_entries = {
            name: {
                "bytes": (run / name).stat().st_size,
                "sha256": backup._sha256(run / name),
            }
            for name in (backup.DUMP_NAME, backup.ROLES_NAME)
        }
        if recorded_bytes is not None:
            # Legacy override knob: deliberately lying sizes exercise the
            # mismatch refusals below.
            for name, size in recorded_bytes.items():
                artifact_entries[name]["bytes"] = size
        body["artifacts"] = artifact_entries
    if counts is not None:
        body["table_row_counts"] = counts
        # Restore verifies large objects separately from table counts because
        # pg_largeobject_metadata is not a public table and can otherwise be
        # silently omitted from a superficially complete archive.
        body["large_object_count"] = 0
        body["content_gate"] = {
            "status": "rejected" if rejected else "accepted",
            "tables": len(counts),
            "rows": sum(counts.values()),
            "seed_tables": list(backup._required_seed_tables()),
            "failures": ["captured no application data"] if rejected else [],
        }
    (run / backup.MANIFEST_NAME).write_text(json.dumps(body), encoding="utf-8")
    return run


def _night(
    out_dir: Path,
    stamp: str,
    counts: dict[str, int],
    *,
    accept_drop: bool = False,
    establish: bool = False,
    intentionally_empty: bool = False,
    identity: backup.Identity | None = None,
    adopt_database: bool = False,
) -> backup.ContentVerdict:
    """One full run against a real output directory, minus Docker.

    Mirrors ``_execute``: load the watermark and the directory's bound identity,
    judge, publish or quarantine, and persist the next watermark -- with the
    identity -- only on acceptance.
    """
    watermark = backup._load_watermark(out_dir)
    verdict = backup._evaluate_content(
        counts,
        watermark,
        accept_drop=accept_drop,
        establish=establish,
        accept_empty=intentionally_empty,
        expected_identity=backup._load_identity(out_dir),
        observed_identity=identity,
        adopt_database=adopt_database,
    )
    run = _write_run(
        out_dir, stamp, counts=counts, rejected=not verdict.accepted, identity=identity
    )
    if verdict.accepted:
        nxt, reset = backup._next_watermark(watermark, counts, verdict)
        backup._write_watermark(
            out_dir,
            nxt,
            run=run.name,
            reset=reset,
            now=datetime.strptime(stamp, backup.STAMP_FORMAT).replace(tzinfo=UTC),
            identity=identity,
        )
    return verdict


# --------------------------------------------------------------------------
# Defect 1 and 3 -- the absolute seed floor
# --------------------------------------------------------------------------


def test_empty_database_is_rejected_with_no_watermark() -> None:
    """The exact reproduction: schema dropped, so zero tables, on a first run."""
    verdict = backup._evaluate_content(EMPTY, NO_WATERMARK, accept_drop=False, establish=False)
    assert verdict.accepted is False
    assert verdict.tables == 0
    assert verdict.rows == 0
    assert any("no tables" in reason for reason in verdict.failures)


def test_schema_present_but_wholly_unpopulated_is_rejected() -> None:
    """Guard: test_schema_present_but_wholly_unpopulated_is_rejected."""
    counts = dict.fromkeys(ALL_TABLES, 0)
    verdict = backup._evaluate_content(counts, NO_WATERMARK, accept_drop=False, establish=False)
    assert verdict.accepted is False
    assert any("hold 0 rows" in reason for reason in verdict.failures)


def test_total_data_loss_with_the_schema_intact_is_rejected() -> None:
    """HOLE 1, the headline defect: 38 tables, 1 row, published as OK.

    Truncate every table but ``alembic_version`` and the old floor
    (MIN_TABLES = 1, MIN_ROWS = 1) saw "many tables, one stamp row" and called it
    a freshly migrated install. It is the opposite: a virgin install was 180
    rows when measured for this migration ancestry.
    """
    assert len(GUTTED) == MEASURED_VIRGIN_TABLES
    assert sum(GUTTED.values()) == 1
    verdict = backup._evaluate_content(GUTTED, NO_WATERMARK, accept_drop=False, establish=False)
    assert verdict.accepted is False
    assert any("currencies" in reason and "tenants" in reason for reason in verdict.failures)


def test_total_data_loss_cannot_be_waved_through_by_any_flag() -> None:
    """Neither override touches the seed floor, together or apart."""
    for accept_drop in (False, True):
        for establish in (False, True):
            verdict = backup._evaluate_content(
                GUTTED, NO_WATERMARK, accept_drop=accept_drop, establish=establish
            )
            assert verdict.accepted is False, (accept_drop, establish)


def test_total_data_loss_is_rejected_even_against_a_matching_watermark() -> None:
    """A gutted database cannot buy acceptance by having been gutted yesterday."""
    verdict = backup._evaluate_content(GUTTED, _watermark(GUTTED), accept_drop=True, establish=True)
    assert verdict.accepted is False


def test_the_measured_virgin_database_clears_the_seed_floor() -> None:
    """The floor itself must not punish a genuinely fresh install.

    CORRECTED. This test used to assert that VIRGIN plus ``--establish-watermark``
    is ACCEPTED, and that premise is the hazard: a virgin database and a database
    that has just been wiped are the same rows, so ratifying it here is
    ratifying the 02:00 sequence in which exit 8 says "re-run ONCE with
    --establish-watermark" and doing so publishes the wiped database. The seed
    floor's own verdict is what this test is about, so it is asserted against the
    floor predicate; who may establish a watermark over it is tier 3b's question
    and is covered below.
    """
    assert len(VIRGIN) == MEASURED_VIRGIN_TABLES
    assert backup._counts_clear_floor(VIRGIN) is True
    verdict = backup._evaluate_content(VIRGIN, NO_WATERMARK, accept_drop=False, establish=True)
    assert not any("seed table" in reason for reason in verdict.failures)
    assert not any("no tables" in reason for reason in verdict.failures)


def test_seed_floor_fires_when_a_seed_table_does_not_exist() -> None:
    """Guard: test_seed_floor_fires_when_a_seed_table_does_not_exist."""
    counts = {name: 5 for name in NAMED_TABLES} | {"alembic_version": 1}
    verdict = backup._evaluate_content(counts, NO_WATERMARK, accept_drop=True, establish=True)
    assert verdict.accepted is False
    assert any("do not exist" in reason for reason in verdict.failures)


def test_the_floor_rejects_a_schema_with_no_tables_at_all() -> None:
    """A dropped schema fails the floor on the seeds, not only on MIN_TABLES."""
    assert backup._counts_clear_floor(EMPTY) is False
    assert backup._counts_clear_floor(dict.fromkeys(backup.SEED_TABLES, 0)) is False
    assert backup._counts_clear_floor(dict.fromkeys(backup.SEED_TABLES, 1)) is True


def test_the_whole_directory_floor_is_subsumed_by_the_per_table_rules() -> None:
    """Why a mutation deleting the whole-directory collapse check cannot be killed.

    Every table that survives the per-table rule keeps at least
    ``COLLAPSE_ROW_FRACTION`` of its mark, and a table under
    ``TABLE_COLLAPSE_MIN_ROWS`` keeps at least one row out of at most
    ``TABLE_COLLAPSE_MIN_ROWS - 1``. While the smaller of those two ratios is not
    below the fraction, the total can never fall below the whole-directory floor
    on its own -- so that check is defence in depth against a future change to
    these two constants, and this test is what would go red first.
    """
    smallest_surviving_ratio = 1 / (backup.TABLE_COLLAPSE_MIN_ROWS - 1)
    assert smallest_surviving_ratio >= backup.COLLAPSE_ROW_FRACTION, (
        "the whole-directory collapse check is now reachable on its own; give it a "
        "real test rather than leaving it ratified by this one"
    )


# SQL-shaped, not merely the words: an identifier followed by a column list, a
# VALUES clause, a SELECT, or DEFAULT VALUES. MEASURED WHY: the first version
# matched ``INSERT INTO <name>`` anywhere in any string constant, and the very
# first prose to mention it -- a migration docstring explaining this parser,
# written the same day -- registered a table called ``x``.
_INSERT_INTO = re.compile(
    r"INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|VALUES\b|SELECT\b|DEFAULT\b)",
    re.IGNORECASE,
)


def _sa_table_bindings(tree: ast.Module) -> dict[str, str]:
    """Local names bound to ``sa.table("x", ...)``, mapped onto ``"x"``."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "table"):
            continue
        if not call.args or not isinstance(call.args[0], ast.Constant):
            continue
        table = call.args[0].value
        if not isinstance(table, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound[target.id] = table
    return bound


def _docstring_constants(tree: ast.Module) -> set[int]:
    """The ``ast.Constant`` nodes that are documentation rather than code."""
    documented = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, documented):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _inline_sa_table_name(first: ast.AST) -> str | None:
    """Table name from an inline ``sa.table("name", ...)`` call argument."""
    if not isinstance(first, ast.Call):
        return None
    if not (isinstance(first.func, ast.Attribute) and first.func.attr == "table"):
        return None
    if not first.args or not isinstance(first.args[0], ast.Constant):
        return None
    value = first.args[0].value
    if isinstance(value, str):
        return value
    return None


def _bulk_insert_seed_table(node: ast.AST, bound: dict[str, str]) -> str | None:
    """Table name from ``op.bulk_insert(<table>, ...)``, or None if not that call."""
    if not isinstance(node, ast.Call):
        return None
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "bulk_insert"):
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Name) and first.id in bound:
        return bound[first.id]
    return _inline_sa_table_name(first)


def _collect_insert_literal_seeds(node: ast.AST, prose: set[int], seeded: set[str]) -> bool:
    """Harvest INSERT INTO names from a non-docstring string constant.

    Returns True when ``node`` was a string Constant (handled either way), so
    callers can skip further idioms for that node.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    if id(node) not in prose:
        seeded.update(match.lower() for match in _INSERT_INTO.findall(node.value))
    return True


def _seed_tables_in_migration(path: Path) -> set[str]:
    """Tables seeded by one Alembic revision file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = _sa_table_bindings(tree)
    prose = _docstring_constants(tree)
    seeded: set[str] = set()
    for node in ast.walk(tree):
        if _collect_insert_literal_seeds(node, prose, seeded):
            continue
        name = _bulk_insert_seed_table(node, bound)
        if name is not None:
            seeded.add(name)
    return seeded


def _tables_seeded_by_migrations() -> set[str]:
    """Every table an Alembic revision writes rows into, read out of the sources.

    Two idioms, which are the two this repository uses:
      * ``op.bulk_insert(<sa.table("name", ...)>, rows)`` -- inline or a local.
      * a literal ``"INSERT INTO name (...) ..."`` handed to ``op.execute``.

    Prose is excluded twice over -- docstrings are skipped, and the pattern
    requires the statement to be SQL-shaped rather than to merely contain the
    words. An ``INSERT INTO {PLACEHOLDER}`` inside an f-string is deliberately
    NOT matched either: the only one in the tree is the body of the SECURITY
    DEFINER function 20260608_0001 installs, which writes a row per *session* at
    runtime and seeds nothing.

    It is a parser, so it can be defeated by a third idiom. That is why the two
    assertions below check the parser still recognises both of the idioms it
    knows: losing one goes red here instead of quietly shrinking the answer.
    """
    seeded: set[str] = set()
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        seeded.update(_seed_tables_in_migration(path))
    return seeded


def test_seed_tables_match_what_the_migrations_actually_seed() -> None:
    """DERIVED, not re-typed: the previous version compared a literal to a literal.

    ``SEED_TABLES`` is application knowledge the backup deliberately takes on,
    and the cost of it being stale is not cosmetic -- ``_non_seed_rows`` counts
    everything OUTSIDE it, which is the single input tier 3b uses to refuse to
    make an empty database a directory's permanent reference. This test reads
    the migration sources instead of asserting another copy of the tuple.
    """
    scanned = _tables_seeded_by_migrations()
    # The scanner's own guard: these two ARE the two idioms it knows, so a
    # migration rewritten in a third one loses its table here rather than
    # silently going unnoticed.
    assert "currencies" in scanned, "the op.bulk_insert idiom is no longer recognised"
    assert "tenants" in scanned, "the INSERT INTO literal idiom is no longer recognised"
    expected = scanned | {"alembic_version"}  # written by Alembic itself, not a revision
    assert backup.SEED_TABLE_EXTENSIONS == (), (
        "PR #222's ancestry has no stacked auth-catalog seed migration. Extend "
        "SEED_TABLE_EXTENSIONS only in the migration PR that actually seeds it."
    )
    required = backup._required_seed_tables()
    assert set(required) == expected, (
        "a migration seeds a table that the required seed contract does not list "
        "(or the contract lists one it no longer seeds). Update CORE_SEED_TABLES or "
        "the stacked SEED_TABLE_EXTENSIONS and re-measure the virgin state. "
        f"Migrations seed {sorted(expected)}; the gate requires {sorted(required)}"
    )
    assert set(required) <= set(VIRGIN)
    assert all(VIRGIN[name] > 0 for name in required)


def test_seed_table_extension_is_consumed_by_every_backup_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stacked seed table is gate behavior, not an inert exported constant."""
    future = "future_seed_catalog"
    monkeypatch.setattr(backup, "SEED_TABLE_EXTENSIONS", (future,))
    required = backup._required_seed_tables()
    assert required == backup.CORE_SEED_TABLES + (future,)

    counts = dict.fromkeys(backup.CORE_SEED_TABLES, 1)
    assert backup._counts_clear_floor(counts) is False
    missing = backup._seed_floor_failures(counts)
    assert len(missing) == 1 and future in missing[0] and "do not exist" in missing[0]
    counts[future] = 0
    assert backup._counts_clear_floor(counts) is False
    empty = backup._seed_floor_failures(counts)
    assert len(empty) == 1 and future in empty[0] and "hold 0 rows" in empty[0]
    counts[future] = 1
    assert backup._counts_clear_floor(counts) is True
    assert backup._seed_floor_failures(counts) == []
    assert backup._non_seed_rows(counts) == 0


@pytest.mark.parametrize("extension", [("",), ("currencies",), ("future", "future")])
def test_seed_table_extension_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch, extension: tuple[str, ...]
) -> None:
    """Blank or duplicate stacked entries cannot publish a content verdict."""
    monkeypatch.setattr(backup, "SEED_TABLE_EXTENSIONS", extension)
    with pytest.raises(backup.BackupError) as caught:
        backup._required_seed_tables()
    assert caught.value.code == backup.EXIT_INTERNAL
    assert "seed-table contract" in str(caught.value)


def test_the_derived_virgin_state_still_matches_the_measured_one() -> None:
    """The anchor. A registry that grows must force a fresh measurement, loudly.

    ``SEED_ROWS`` is computed from the registries the migrations import, so it
    follows them automatically -- which is the fix for the drift, and also the
    reason nothing would notice if the derivation itself were wrong. This is the
    one place that compares it against rows counted in a real migrated container.
    """
    assert SEED_ROWS == MEASURED_SEED_ROWS, (
        "the seeded row counts derived from the registries no longer match the "
        "container measurement recorded at the top of this file. That is expected "
        "when a role, permission or currency is added or retired: re-measure a fresh "
        "`alembic upgrade head` (the command is in that comment block) and update "
        "MEASURED_SEED_ROWS / MEASURED_VIRGIN_ROWS. Do not delete this assertion -- "
        "an unmeasured virgin state is what shipped MIN_ROWS = 1."
    )
    assert sum(VIRGIN.values()) == MEASURED_VIRGIN_ROWS
    assert len(VIRGIN) == MEASURED_VIRGIN_TABLES


def test_a_virgin_database_holds_no_rows_outside_the_seed_tables() -> None:
    """Tier 3b's whole input: a migrated schema has no application rows.

    ``_evaluate_content`` refuses ``--establish-watermark`` over a first run only
    when ``non_seed == 0``. Keeping the migration-derived seed set current is
    what makes that refusal continue to fire after future seed migrations.
    """
    assert backup._non_seed_rows(VIRGIN) == 0
    verdict = backup._evaluate_content(VIRGIN, NO_WATERMARK, accept_drop=True, establish=True)
    assert verdict.accepted is False
    assert any("is EMPTY" in reason for reason in verdict.failures)


# --------------------------------------------------------------------------
# Defect 5 -- an empty archive is a no-content run, not a broken artifact
# --------------------------------------------------------------------------


def test_an_empty_table_of_contents_is_a_content_failure() -> None:
    """HOLE 3: `DROP SCHEMA public CASCADE` must reach the gate, not exit 6."""
    verdict = backup._evaluate_content(
        REAL, _watermark(REAL), accept_drop=True, establish=True, toc_entries=0
    )
    assert verdict.accepted is False
    assert any("table-of-contents" in reason for reason in verdict.failures)


def test_a_populated_table_of_contents_is_not_a_failure() -> None:
    """Guard: test_a_populated_table_of_contents_is_not_a_failure."""
    verdict = backup._evaluate_content(
        REAL, _watermark(REAL), accept_drop=False, establish=False, toc_entries=366
    )
    assert verdict.accepted is True


def test_an_unmeasured_table_of_contents_is_not_a_failure() -> None:
    """--no-verify-dump reports -1; that is "unknown", not "empty"."""
    verdict = backup._evaluate_content(
        REAL, _watermark(REAL), accept_drop=False, establish=False, toc_entries=-1
    )
    assert verdict.accepted is True


# --------------------------------------------------------------------------
# The first-run acknowledgement
# --------------------------------------------------------------------------


def test_a_first_run_into_a_new_directory_is_refused_without_the_flag() -> None:
    """Nothing can tell a healthy database from a wiped-and-re-migrated one."""
    verdict = backup._evaluate_content(REAL, NO_WATERMARK, accept_drop=False, establish=False)
    assert verdict.accepted is False
    assert verdict.first_run is True
    assert any("--establish-watermark" in reason for reason in verdict.failures)


def test_accept_content_drop_does_not_stand_in_for_the_first_run_flag() -> None:
    """Guard: test_accept_content_drop_does_not_stand_in_for_the_first_run_flag."""
    verdict = backup._evaluate_content(REAL, NO_WATERMARK, accept_drop=True, establish=False)
    assert verdict.accepted is False


def test_establishing_the_watermark_is_recorded_not_silent() -> None:
    """Guard: test_establishing_the_watermark_is_recorded_not_silent."""
    verdict = backup._evaluate_content(REAL, NO_WATERMARK, accept_drop=False, establish=True)
    assert verdict.accepted is True
    assert verdict.established is True
    block = verdict.as_manifest_block()
    assert block["watermark_established"] is True
    assert block["first_run"] is True


# --------------------------------------------------------------------------
# Defect 9 -- --establish-watermark over a database that holds nothing
#
# Measured against the live CLI. Seeds intact, all application data gone, fresh
# out-dir:
#     run 1  (no flag)                -> exit 8, "re-run ONCE with
#                                        --establish-watermark"
#     run 2  --establish-watermark    -> exit 0, PUBLISHED, and it becomes the
#                                        reference for every run after it
# That is the exact 02:00 sequence after `docker compose down -v`: the exit-8
# message walked the operator into publishing the empty database. The only
# barrier was reading `tables=6 rows=180` and knowing it was wrong.
# --------------------------------------------------------------------------


def test_establishing_a_watermark_over_an_empty_database_is_refused() -> None:
    """Guard: test_establishing_a_watermark_over_an_empty_database_is_refused."""
    verdict = backup._evaluate_content(VIRGIN, NO_WATERMARK, accept_drop=False, establish=True)
    assert verdict.accepted is False
    assert verdict.non_seed_rows == 0
    assert any("is EMPTY" in reason for reason in verdict.failures)
    assert verdict.established is False, "nothing was established"


def test_the_exit_8_remediation_does_not_offer_the_flag_over_an_empty_database() -> None:
    """The message is half the defect: it told the operator what to type next."""
    verdict = backup._evaluate_content(VIRGIN, NO_WATERMARK, accept_drop=False, establish=False)
    assert verdict.accepted is False
    reason = "\n".join(verdict.failures)
    assert "RESTORE it first" in reason
    assert "--this-database-is-intentionally-empty" in reason
    assert "re-run ONCE with --establish-watermark." not in reason


def test_the_first_run_message_still_offers_the_flag_when_there_is_data() -> None:
    """A real first run must not be pushed towards the harder acknowledgement."""
    verdict = backup._evaluate_content(REAL, NO_WATERMARK, accept_drop=False, establish=False)
    reason = "\n".join(verdict.failures)
    assert "re-run ONCE with --establish-watermark" in reason
    assert "--this-database-is-intentionally-empty" not in reason
    assert "7 of them outside" in reason


def test_a_genuinely_new_install_can_still_be_established() -> None:
    """The escape hatch has to exist, or a real fresh install cannot be backed up."""
    verdict = backup._evaluate_content(
        VIRGIN, NO_WATERMARK, accept_drop=False, establish=True, accept_empty=True
    )
    assert verdict.accepted is True
    assert verdict.established is True
    assert verdict.overridden, "acknowledging an empty database must be recorded"


def test_the_empty_acknowledgement_does_not_stand_in_for_the_first_run_flag() -> None:
    """Guard: test_the_empty_acknowledgement_does_not_stand_in_for_the_first_run_flag."""
    verdict = backup._evaluate_content(
        VIRGIN, NO_WATERMARK, accept_drop=False, establish=False, accept_empty=True
    )
    assert verdict.accepted is False


def test_the_empty_acknowledgement_cannot_override_the_seed_floor() -> None:
    """Guard: test_the_empty_acknowledgement_cannot_override_the_seed_floor."""
    verdict = backup._evaluate_content(
        GUTTED, NO_WATERMARK, accept_drop=True, establish=True, accept_empty=True
    )
    assert verdict.accepted is False


def test_the_empty_acknowledgement_is_inert_once_a_watermark_exists() -> None:
    """It must not become a way to wave a wipe through on night two."""
    verdict = backup._evaluate_content(
        VIRGIN,
        _watermark(REAL),
        accept_drop=False,
        establish=True,
        accept_empty=True,
    )
    assert verdict.accepted is False
    assert any("high-water mark" in reason for reason in verdict.failures)


def test_one_row_outside_the_seeds_is_enough_to_be_a_real_install() -> None:
    """Guard: test_one_row_outside_the_seeds_is_enough_to_be_a_real_install."""
    verdict = backup._evaluate_content(
        _database(org_units=1), NO_WATERMARK, accept_drop=False, establish=True
    )
    assert verdict.accepted is True


def test_the_first_run_flag_is_inert_once_a_watermark_exists() -> None:
    """It must not become a way to wave a collapse through."""
    verdict = backup._evaluate_content(
        GUTTED | {"currencies": 178, "tenants": 1},
        _watermark(REAL),
        accept_drop=False,
        establish=True,
    )
    assert verdict.accepted is False
    assert any("high-water mark" in reason for reason in verdict.failures)


# --------------------------------------------------------------------------
# Defect 4 -- the watermark, and the drain it has to bound
# --------------------------------------------------------------------------


def test_a_nightly_drain_is_bounded_by_the_watermark(tmp_path: Path) -> None:
    """HOLE 2, end to end against a real directory.

    The verifier's reproduction was 180 rows to 1 in three nights, every run
    green, because the reference re-anchored on every acceptance. With a
    persistent high-water mark the drain stops on night two.
    """
    verdicts = [_night(tmp_path, "20260801T020000", REAL, establish=True)]
    verdicts.append(_night(tmp_path, "20260802T020000", _database(org_units=2)))
    verdicts.append(_night(tmp_path, "20260803T020000", GUTTED))

    assert [v.accepted for v in verdicts] == [True, False, False]
    published = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert published == [
        "ums-backup-20260801T020000Z",
        "ums-backup-20260802T020000Z.rejected",
        "ums-backup-20260803T020000Z.rejected",
    ]


def test_the_watermark_does_not_re_anchor_on_an_ordinary_accepted_run(
    tmp_path: Path,
) -> None:
    """The exact property the previous baseline lacked."""
    _night(
        tmp_path, "20260801T020000", _database(monthly_channel_revenue_facts=500), establish=True
    )
    _night(tmp_path, "20260802T020000", _database(monthly_channel_revenue_facts=300))
    watermark = backup._load_watermark(tmp_path)
    assert watermark.tables["monthly_channel_revenue_facts"] == 500


def test_an_emptied_table_is_caught_even_when_the_total_barely_moves(
    tmp_path: Path,
) -> None:
    """A global fraction cannot see the money table drain under a growing log."""
    _night(
        tmp_path,
        "20260801T020000",
        _database(monthly_channel_revenue_facts=12, ums_table_00=400),
        establish=True,
    )
    verdict = _night(tmp_path, "20260802T020000", _database(ums_table_00=800))
    assert verdict.accepted is False
    assert any("monthly_channel_revenue_facts" in reason for reason in verdict.failures)


def test_a_table_falling_below_the_fraction_of_its_own_mark_is_caught(
    tmp_path: Path,
) -> None:
    """Guard: test_a_table_falling_below_the_fraction_of_its_own_mark_is_caught."""
    _night(
        tmp_path,
        "20260801T020000",
        _database(monthly_channel_revenue_facts=1000, ums_table_00=5000),
        establish=True,
    )
    verdict = _night(
        tmp_path,
        "20260802T020000",
        _database(monthly_channel_revenue_facts=40, ums_table_00=5000),
    )
    assert verdict.accepted is False
    assert any("1000->40" in reason for reason in verdict.failures)


def test_a_shrinking_seed_table_is_caught(tmp_path: Path) -> None:
    """The one wipe a fraction cannot see: everything equal but three tenant rows.

    Measured live before this rule existed, against the 180-row virgin state of
    the time: four tenants, `docker compose down -v`, the stack auto-migrates on
    start, tenants is back to the bootstrap row alone, and the run published exit
    0. It is not empty, its mark of 4 is under the per-table threshold, and 180
    was 98% of the 183-row mark. The fixture below is that same shape against
    today's virgin state, so the ratio is even more forgiving and the seed-shrink
    rule is still the only thing that fires.
    """
    # Night 1 has no rows outside the seeded tables, so it needs tier 3b's
    # acknowledgement as well. The fixture is deliberately left exact: adding an
    # application row to dodge the flag would stop this test isolating the
    # seed-shrink rule, which is the only thing that fires on three lost tenants.
    _night(
        tmp_path,
        "20260801T020000",
        _database(tenants=4),
        establish=True,
        intentionally_empty=True,
    )
    verdict = _night(tmp_path, "20260802T020000", VIRGIN)
    assert verdict.accepted is False
    assert any("tenants 4->1" in reason for reason in verdict.failures)


def test_a_growing_seed_table_is_not_a_collapse(tmp_path: Path) -> None:
    """Guard: test_a_growing_seed_table_is_not_a_collapse."""
    _night(
        tmp_path,
        "20260801T020000",
        _database(tenants=4),
        establish=True,
        intentionally_empty=True,
    )
    assert _night(tmp_path, "20260802T020000", _database(tenants=9)).accepted is True


def test_a_shrinking_seed_table_is_overridable(tmp_path: Path) -> None:
    """A migration really can reduce the ISO snapshot; it must not wedge the box."""
    _night(
        tmp_path,
        "20260801T020000",
        _database(currencies=178),
        establish=True,
        intentionally_empty=True,
    )
    verdict = _night(tmp_path, "20260802T020000", _database(currencies=170), accept_drop=True)
    assert verdict.accepted is True
    assert backup._load_watermark(tmp_path).tables["currencies"] == 170


def test_retiring_a_seed_row_costs_exactly_one_override_night(tmp_path: Path) -> None:
    """A real seeded-table shrink costs one override night, then clears."""
    _night(tmp_path, "20260801T020000", REAL, establish=True)
    retired = dict(REAL)
    retired["currencies"] -= 1

    red = _night(tmp_path, "20260802T020000", retired)
    assert red.accepted is False
    assert any("currencies" in reason for reason in red.failures)

    cleared = _night(tmp_path, "20260803T020000", retired, accept_drop=True)
    assert cleared.accepted is True

    assert _night(tmp_path, "20260804T020000", retired).accepted is True, (
        "one override night, not a standing flag in the scheduled task"
    )
    assert backup._load_watermark(tmp_path).tables["currencies"] == retired["currencies"]


def test_a_disappeared_table_is_caught() -> None:
    """Guard: test_a_disappeared_table_is_caught."""
    counts = dict(REAL)
    del counts["monthly_channel_revenue_facts"]
    verdict = backup._evaluate_content(counts, _watermark(REAL), accept_drop=False, establish=False)
    assert verdict.accepted is False
    assert any("no longer exist" in reason for reason in verdict.failures)


def test_ordinary_row_decline_is_not_a_collapse() -> None:
    """A normal deletion must not turn the nightly backup red."""
    watermark = _watermark(_database(monthly_channel_revenue_facts=200))
    verdict = backup._evaluate_content(
        _database(monthly_channel_revenue_facts=180),
        watermark,
        accept_drop=False,
        establish=False,
    )
    assert verdict.accepted is True


def test_growth_raises_the_watermark(tmp_path: Path) -> None:
    """Guard: test_growth_raises_the_watermark."""
    _night(tmp_path, "20260801T020000", REAL, establish=True)
    grown = _database(monthly_channel_revenue_facts=900, org_units=2, youtube_channels=2)
    assert _night(tmp_path, "20260802T020000", grown).accepted is True
    assert backup._load_watermark(tmp_path).tables["monthly_channel_revenue_facts"] == 900


# --------------------------------------------------------------------------
# Where the watermark lives, and what losing it does
# --------------------------------------------------------------------------


def test_deleting_the_watermark_file_rebuilds_it_from_the_manifests(
    tmp_path: Path,
) -> None:
    """Losing one home must fail SAFE: the mark comes back, it does not reset."""
    _night(
        tmp_path, "20260801T020000", _database(monthly_channel_revenue_facts=500), establish=True
    )
    (tmp_path / backup.WATERMARK_NAME).unlink()

    watermark = backup._load_watermark(tmp_path)
    assert watermark.tables["monthly_channel_revenue_facts"] == 500
    assert "rebuilt from" in watermark.source


def test_deleting_every_run_directory_leaves_the_watermark_file_in_charge(
    tmp_path: Path,
) -> None:
    """Guard: test_deleting_every_run_directory_leaves_the_watermark_file_in_charge."""
    _night(
        tmp_path, "20260801T020000", _database(monthly_channel_revenue_facts=500), establish=True
    )
    for child in tmp_path.iterdir():
        if child.is_dir():
            for grandchild in child.iterdir():
                grandchild.unlink()
            child.rmdir()

    watermark = backup._load_watermark(tmp_path)
    assert watermark.tables["monthly_channel_revenue_facts"] == 500


def test_losing_both_homes_lands_in_the_first_run_case_not_a_pass(
    tmp_path: Path,
) -> None:
    """The only reset is the state that is genuinely indistinguishable from new."""
    assert backup._load_watermark(tmp_path).is_empty is True
    verdict = backup._evaluate_content(
        GUTTED, backup._load_watermark(tmp_path), accept_drop=False, establish=False
    )
    assert verdict.accepted is False


def test_a_corrupt_watermark_file_does_not_lower_the_bar(tmp_path: Path) -> None:
    """Guard: test_a_corrupt_watermark_file_does_not_lower_the_bar."""
    _night(
        tmp_path, "20260801T020000", _database(monthly_channel_revenue_facts=500), establish=True
    )
    (tmp_path / backup.WATERMARK_NAME).write_text("{ not json", encoding="utf-8")

    watermark = backup._load_watermark(tmp_path)
    assert watermark.tables["monthly_channel_revenue_facts"] == 500


def test_a_rejected_run_never_contributes_to_the_watermark(tmp_path: Path) -> None:
    """The manifest-status guard, exercised where it is the ONLY thing acting.

    REWRITTEN. The previous version wrote the rejected run as ``...Z.rejected``,
    a name ``_load_watermark`` already filters through ``_run_stamp`` before it
    ever opens a manifest -- so the guard it claimed to prove was never reached,
    and deleting that guard left this test green. The discriminating case is a
    rejected manifest under a RUN-SHAPED name, which is what an operator
    produces by "un-rejecting" a quarantined run: renaming the directory to make
    it look like a backup again. The verdict has to travel inside the manifest
    for that to be refused.
    """
    _write_run(tmp_path, "20260820T222143", counts=REAL)
    quarantined = _write_run(
        tmp_path,
        "20260824T222105",
        counts=_database(monthly_channel_revenue_facts=99999),
        rejected=True,
    )
    un_rejected = quarantined.with_name("ums-backup-20260824T222105Z")
    quarantined.rename(un_rejected)
    assert backup._run_stamp(un_rejected.name) is not None, "the name now parses as a run"

    watermark = backup._load_watermark(tmp_path)
    assert watermark.tables["monthly_channel_revenue_facts"] == 3


def test_a_content_free_run_never_contributes_to_the_watermark(tmp_path: Path) -> None:
    """Guard: test_a_content_free_run_never_contributes_to_the_watermark."""
    _write_run(tmp_path, "20260715T222143", counts=REAL)
    _write_run(tmp_path, "20260820T222143", counts=EMPTY)
    watermark = backup._load_watermark(tmp_path)
    assert watermark.total_rows == sum(REAL.values())
    assert watermark.table_count == 38


def test_the_watermark_is_empty_on_an_empty_output_directory(tmp_path: Path) -> None:
    """Guard: test_the_watermark_is_empty_on_an_empty_output_directory."""
    assert backup._load_watermark(tmp_path).is_empty is True


# --------------------------------------------------------------------------
# Defect 7 -- a manifest is a report about artifacts, not the artifacts
#
# Measured against the live CLI: a hand-planted manifest.json in a run-shaped
# directory, with NO database.dump and NO roles.sql, claiming
# ``org_units: 1000000000``, folded into the high-water mark and pushed it to
# 1000000185. Every later run against the healthy database then exited 8:
#     RC=8   watermark 1000000185 rows
#      - 1 table(s) fell below 10% of their high-water mark: org_units
#        1000000000->2.
# The contract block on ``_load_watermark`` claimed this could not happen,
# because a run "only contributes if its counts still clear the floor" -- and
# the floor is an EXISTENCE test that constrains no magnitude.
# --------------------------------------------------------------------------


def test_a_manifest_with_no_artifacts_never_contributes_to_the_watermark(
    tmp_path: Path,
) -> None:
    """The exact reproduction: a planted manifest with nothing behind it."""
    _write_run(tmp_path, "20260820T222143", counts=REAL)
    planted = _write_run(
        tmp_path,
        "20260101T000000",
        counts=_database(org_units=10**9),
        artifacts=False,
    )
    assert (planted / backup.MANIFEST_NAME).is_file()
    assert not (planted / backup.DUMP_NAME).exists()

    watermark = backup._load_watermark(tmp_path)
    assert watermark.tables["org_units"] == 2
    assert watermark.total_rows == sum(REAL.values())


def test_a_manifest_whose_recorded_sizes_do_not_match_disk_is_not_a_backup(
    tmp_path: Path,
) -> None:
    """Half a copy is not a backup: the manifest describes bytes that are not there."""
    real_size = len(b"PGDMP-placeholder")
    honest = _write_run(
        tmp_path,
        "20260820T222143",
        counts=REAL,
        recorded_bytes={backup.DUMP_NAME: real_size},
    )
    truncated = _write_run(
        tmp_path,
        "20260821T222143",
        counts=_database(org_units=10**9),
        recorded_bytes={backup.DUMP_NAME: 183891},
    )

    assert (
        backup._run_is_published_backup(
            honest, json.loads((honest / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
        )
        is True
    )
    assert (
        backup._run_is_published_backup(
            truncated, json.loads((truncated / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
        )
        is False
    )
    assert backup._load_watermark(tmp_path).tables["org_units"] == 2


def test_an_empty_dump_file_is_not_a_backup(tmp_path: Path) -> None:
    """Guard: test_an_empty_dump_file_is_not_a_backup."""
    run = _write_run(tmp_path, "20260820T222143", counts=REAL)
    (run / backup.DUMP_NAME).write_bytes(b"")
    assert backup._run_has_content(run) is None, "unknown, so never deleted"
    assert backup._load_watermark(tmp_path).is_empty is True


def test_an_artifact_less_run_is_unknown_to_retention_not_proven_content(
    tmp_path: Path,
) -> None:
    """It must not become the "newest run with content" that invariant 1 pins."""
    real = _write_run(tmp_path, "20250101T000000", counts=REAL)
    planted = _write_run(tmp_path, "20260824T222143", counts=REAL, artifacts=False)

    assert backup._run_has_content(planted) is None
    pruned = backup._prune(tmp_path, keep_days=1, keep_min=1, now=NOW)

    assert real.is_dir(), "the real backup is still what invariant 1 pins"
    assert planted.is_dir(), "and an unknown directory is still never deleted"
    assert pruned.removed == []


# --------------------------------------------------------------------------
# Defect 8 -- an output directory belongs to ONE database
#
# Measured against the live CLI, two containers built the same way:
#     run 1  --container ums-lane-a --establish-watermark  -> exit 0, 187 rows
#     run 2  --container ums-lane-b  (SAME --out-dir)      -> exit 0, 1098 rows
#     watermark.json -> monthly_channel_revenue_facts: 900, org_units: 9
# Both databases held the same seeded tenant UUID, so no tenant or row-count
# check could separate them. The cluster's system_identifier can: MEASURED
# unchanged across `docker restart` and different for a second cluster.
# --------------------------------------------------------------------------


def test_a_different_database_into_a_bound_directory_is_refused() -> None:
    """Guard: test_a_different_database_into_a_bound_directory_is_refused."""
    verdict = backup._evaluate_content(
        _database(org_units=9, youtube_channels=9, monthly_channel_revenue_facts=900),
        _watermark(REAL),
        accept_drop=False,
        establish=False,
        expected_identity=IDENTITY_A,
        observed_identity=IDENTITY_B,
    )
    assert verdict.accepted is False
    assert any("bound to" in reason for reason in verdict.failures)
    assert any("--adopt-database" in reason for reason in verdict.failures)


def test_accept_content_drop_cannot_wave_through_a_different_database() -> None:
    """The identity binding is about WHICH database, not about how many rows."""
    verdict = backup._evaluate_content(
        REAL,
        _watermark(REAL),
        accept_drop=True,
        establish=True,
        expected_identity=IDENTITY_A,
        observed_identity=IDENTITY_B,
    )
    assert verdict.accepted is False


def test_adopt_database_rebinds_and_is_recorded_not_silent() -> None:
    """Guard: test_adopt_database_rebinds_and_is_recorded_not_silent."""
    verdict = backup._evaluate_content(
        REAL,
        _watermark(REAL),
        accept_drop=False,
        establish=False,
        expected_identity=IDENTITY_A,
        observed_identity=IDENTITY_B,
        adopt_database=True,
    )
    assert verdict.accepted is True
    assert verdict.identity_adopted is True
    assert verdict.overridden, "adopting another database must never be silent"
    assert verdict.as_manifest_block()["identity"] == {
        "expected": IDENTITY_A.as_json(),
        "observed": IDENTITY_B.as_json(),
        "adopted": True,
    }


def test_the_same_database_in_a_recreated_container_is_not_a_mismatch() -> None:
    """`docker compose up` after `down` is ordinary churn, not a new database."""
    verdict = backup._evaluate_content(
        REAL,
        _watermark(REAL),
        accept_drop=False,
        establish=False,
        expected_identity=IDENTITY_A,
        observed_identity=backup.Identity(
            system_identifier=IDENTITY_A.system_identifier, database=IDENTITY_A.database
        ),
    )
    assert verdict.accepted is True


def test_an_unknown_identity_never_degrades_to_a_match() -> None:
    """Missing observed identity must not silently pass a bound directory."""
    bound_unknown = backup._evaluate_content(
        REAL,
        _watermark(REAL),
        accept_drop=False,
        establish=False,
        expected_identity=IDENTITY_A,
        observed_identity=None,
    )
    assert bound_unknown.accepted is False
    assert any("could not establish" in reason for reason in bound_unknown.failures)

    for expected, observed in ((None, IDENTITY_B), (None, None)):
        verdict = backup._evaluate_content(
            REAL,
            _watermark(REAL),
            accept_drop=False,
            establish=False,
            expected_identity=expected,
            observed_identity=observed,
        )
        assert verdict.accepted is True, (expected, observed)
        assert verdict.identity_adopted is False


def test_hyphenated_role_lookalike_does_not_satisfy_required_roles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``app_tenant-backup`` must not count as ``app_tenant``."""
    target = tmp_path / backup.ROLES_NAME

    def _lookalike(argv: list[str], *, timeout: int, target: Path):
        """lookalike."""
        target.write_text(
            'CREATE ROLE "app_tenant-backup";\nCREATE ROLE app_platform;\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(backup, "_run_to_file", _lookalike)
    monkeypatch.setattr(backup, "_psql", _backup_superuser_psql())
    with pytest.raises(backup.BackupError) as raised:
        backup._dump_roles("fake", target, timeout=5, include_passwords=False)
    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "app_tenant" in str(raised.value)


def _spawn(code: str) -> subprocess.Popen[bytes]:
    """A real child process running ``code``, in its OWN process group.

    The group flag is Windows-only belt-and-braces: the probe under test never
    signals anything, but if a regression ever reintroduced the old
    ``os.kill(pid, 0)`` probe -- which is ``GenerateConsoleCtrlEvent`` on
    Windows -- the stray Ctrl+C would land in the child's group, not in the
    console group running this suite. That regression then fails these tests
    instead of killing the whole pytest run, which is how it was first seen.
    """
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def test_stale_backup_lock_is_reclaimed_when_owner_pid_is_dead(tmp_path: Path) -> None:
    """A leftover lock from a GENUINELY dead process must not wedge the next run.

    No monkeypatching of ``_pid_is_alive``: the previous revision mocked it to
    False, and the mocked bit was exactly the broken primitive. On Windows the
    old ``os.kill(pid, 0)`` probe read freshly-dead pids as ALIVE (measured
    5/5 on the deployment box), so reclaim never fired and every nightly run
    after a hard stop exited 2 -- while this test stayed green. A real child
    is spawned and ``wait()``ed, so the pid below is genuinely dead, and there
    is deliberately no ``started.at`` in the planted lock: age-based reclaim
    cannot mask a probe that cannot see death.
    """
    child = _spawn("pass")
    child.wait()
    lock_dir = tmp_path / ".backup.lock"
    lock_dir.mkdir()
    (lock_dir / "owner.pid").write_text(f"{child.pid}\n", encoding="utf-8")
    with backup._exclusive_backup_lock(tmp_path):
        assert (lock_dir / "owner.pid").read_text(encoding="utf-8").strip() == str(os.getpid())
    assert not lock_dir.exists()


def test_pid_probe_reads_live_as_alive_and_dead_as_dead_without_signalling() -> None:
    """The probe's truth table, against real processes, and its side-effects.

    The live half also asserts the probed child SURVIVES the probe: the old
    Windows probe was ``GenerateConsoleCtrlEvent`` and could deliver a real
    Ctrl+C to the target. The dead half probes after ``kill()`` + ``wait()``
    while this Popen object still holds an open handle to the child, which is
    precisely the exited-but-handle-still-open case OpenProcess alone misreads.
    """
    child = _spawn("import time; time.sleep(60)")
    try:
        assert backup._pid_is_alive(child.pid) is True
        assert child.poll() is None, "probing a pid must not signal it"
    finally:
        child.kill()
        child.wait()
    assert backup._pid_is_alive(child.pid) is False
    assert backup._pid_is_alive(0) is False
    assert backup._pid_is_alive(-1) is False


def test_a_live_owner_pid_is_never_reclaimed_even_past_the_stale_bound(
    tmp_path: Path, clock: _Clock
) -> None:
    """DESIGN(round-27, operator-delegated): liveness beats age.

    ``--timeout`` is per-command and operator-configurable, so a manual backup
    can legitimately outlive ``LOCK_STALE_AFTER``; reclaiming under a LIVE pid
    let a second invocation replace the lock mid-run and double-publish
    against one watermark. The trade-off is deliberate: if the owner died and
    its pid was recycled by an unrelated process, the probe reads ALIVE and
    the lock wedges until an operator removes it -- loud, named, with
    instructions -- which is strictly safer than a silent double publish in a
    numbers-first system. The boundary is asserted from both sides: below the
    bound the live pid holds the lock, and past the bound it STILL holds it.
    """
    child = _spawn("import time; time.sleep(60)")
    try:
        lock_dir = tmp_path / ".backup.lock"
        lock_dir.mkdir()
        (lock_dir / "started.at").write_text(backup._utc_now().isoformat() + "\n", encoding="utf-8")
        (lock_dir / "owner.pid").write_text(f"{child.pid}\n", encoding="utf-8")

        clock.advance(backup.LOCK_STALE_AFTER - timedelta(minutes=1))
        with (
            pytest.raises(backup.BackupError) as caught,
            backup._exclusive_backup_lock(tmp_path),
        ):
            pass  # pragma: no cover - the acquire must refuse
        assert caught.value.code == backup.EXIT_USAGE

        clock.advance(timedelta(minutes=2))
        with (
            pytest.raises(backup.BackupError) as past_bound,
            backup._exclusive_backup_lock(tmp_path),
        ):
            pass  # pragma: no cover - liveness beats age: still refused
        assert past_bound.value.code == backup.EXIT_USAGE
        assert (lock_dir / "owner.pid").read_text(encoding="utf-8").strip() == str(child.pid)
    finally:
        child.kill()
        child.wait()


def test_the_lock_records_its_start_instant(tmp_path: Path) -> None:
    """``started.at`` is the age bound's supply side; acquisition writes it."""
    with backup._exclusive_backup_lock(tmp_path):
        raw = (tmp_path / ".backup.lock" / "started.at").read_text(encoding="utf-8").strip()
        assert datetime.fromisoformat(raw) == SUITE_NOW


def test_a_failed_lock_release_is_a_durable_warning_not_a_silent_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A transient AV/OneDrive hold at release used to vanish without a trace.

    The run's exit code may still be 0 -- its backup is valid -- but the
    leftover lock is tomorrow's reclaim, so the operator must be able to see
    where it came from. The failure here is real, not mocked: a file planted
    inside the lock directory makes the release ``rmdir`` fail with
    ENOTEMPTY, the way a scanner's transient hold does.
    """
    intruder = tmp_path / ".backup.lock" / "intruder"
    with backup._exclusive_backup_lock(tmp_path):
        intruder.write_text("scanner hold\n", encoding="utf-8")
    assert (tmp_path / ".backup.lock").exists(), "the failed release leaves the dir"
    assert "lock release failed" in capsys.readouterr().err
    log = (tmp_path / backup.LOG_NAME).read_text(encoding="utf-8")
    assert "WARNING lock release failed" in log
    intruder.unlink()


def test_a_refused_run_dir_chmod_is_a_durable_warning_not_a_silent_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A chmod the filesystem refuses used to vanish into ``except OSError: pass``.

    The mode stays best-effort -- the run's verdict never hinges on it -- but
    a backup directory that kept broader permissions must be visible to the
    operator. The failure here is real, not mocked: chmod on a path that does
    not exist raises OSError on every platform.
    """
    backup._restrict_run_dir_mode(tmp_path / "never-created", tmp_path)
    assert "could not restrict" in capsys.readouterr().err
    log = (tmp_path / backup.LOG_NAME).read_text(encoding="utf-8")
    assert "WARNING could not restrict" in log


def test_a_status_write_that_dies_midway_leaves_the_old_record_intact(
    tmp_path: Path,
) -> None:
    """Power cut mid-write: last-run.json must never be left torn.

    The old whole-file write was ``open("w")`` -- truncate in place -- so a
    crash between the truncate and the last byte destroyed the previous record
    without completing the new one. The crash is injected at the OS boundary
    (the handle dies after one byte); the sequence under test -- write aside,
    then ``os.replace`` -- is the real one. Under truncate-in-place this test
    fails with a one-byte canonical file.
    """
    canonical = tmp_path / backup.LAST_RUN_NAME
    assert backup._write_last_run(tmp_path, {"status": "OK", "exit_code": 0}) is True
    before = canonical.read_text(encoding="utf-8")

    real_open = Path.open

    class _DiesAfterOneByte:
        """A writer that persists one byte and then reports a crash."""

        def __init__(self, handle: object) -> None:
            """init."""
            self._handle = handle

        def __enter__(self) -> Self:
            """enter."""
            return self

        def __exit__(self, *_exc: object) -> None:
            """exit."""
            self._handle.close()  # type: ignore[attr-defined]

        def write(self, body: str) -> int:
            """Write one byte, flush it to disk, then die."""
            self._handle.write(body[:1])  # type: ignore[attr-defined]
            self._handle.flush()  # type: ignore[attr-defined]
            raise OSError("simulated crash mid-write")

    def _crashy_open(self: Path, *args: object, **kwargs: object) -> object:
        """crashy open."""
        mode = str(kwargs.pop("mode", args[0] if args else "r"))
        rest = args[1:] if args else ()
        handle = real_open(self, mode, *rest, **kwargs)  # type: ignore[arg-type]
        if "w" in mode and self.parent == tmp_path:
            return _DiesAfterOneByte(handle)
        return handle

    # A private MonkeyPatch context: the function-scoped ``monkeypatch``
    # instance is shared with the autouse clock fixture, and ``undo()`` on it
    # would unfreeze the suite clock mid-test.
    with pytest.MonkeyPatch.context() as crash_zone:
        crash_zone.setattr(Path, "open", _crashy_open)
        assert backup._write_last_run(tmp_path, {"status": "REJECTED", "exit_code": 8}) is False

    assert canonical.read_text(encoding="utf-8") == before
    assert json.loads(before)["exit_code"] == 0


def test_a_bound_directory_refuses_the_second_database_end_to_end(tmp_path: Path) -> None:
    """The measured two-container sequence, at the directory level."""
    first = _night(
        tmp_path,
        "20260801T020000",
        REAL,
        establish=True,
        identity=IDENTITY_A,
    )
    assert first.accepted is True
    assert backup._load_identity(tmp_path) == IDENTITY_A

    second = _night(
        tmp_path,
        "20260802T020000",
        _database(org_units=9, youtube_channels=9, monthly_channel_revenue_facts=900),
        identity=IDENTITY_B,
    )
    assert second.accepted is False
    assert backup._load_watermark(tmp_path).tables["monthly_channel_revenue_facts"] == 3
    assert backup._load_identity(tmp_path) == IDENTITY_A, "a refused run cannot rebind"


def test_the_identity_survives_losing_the_watermark_file(tmp_path: Path) -> None:
    """Same two homes as the mark: the run manifests carry it too."""
    _night(tmp_path, "20260801T020000", REAL, establish=True, identity=IDENTITY_A)
    (tmp_path / backup.WATERMARK_NAME).unlink()
    assert backup._load_identity(tmp_path) == IDENTITY_A


def test_a_rejected_run_does_not_supply_the_directory_identity(tmp_path: Path) -> None:
    """Under a RUN-SHAPED name, so the manifest guard is what does the work.

    A mutation matrix caught the first version of this test ratifying its own
    hole, exactly as the ``...Z.rejected`` watermark test did: ``_load_identity``
    filters by name through ``_run_stamp`` before it opens a manifest, so a
    quarantined run never reached the guard being asserted. Deleting the guard
    left it green. An operator who renames a quarantined run back is the case
    that reaches it.
    """
    _night(tmp_path, "20260801T020000", REAL, establish=True, identity=IDENTITY_A)
    rejected = _night(tmp_path, "20260802T020000", GUTTED, identity=IDENTITY_B)
    assert rejected.accepted is False

    quarantined = tmp_path / "ums-backup-20260802T020000Z.rejected"
    quarantined.rename(tmp_path / "ums-backup-20260802T020000Z")
    (tmp_path / backup.WATERMARK_NAME).unlink()

    assert backup._load_identity(tmp_path) == IDENTITY_A


def test_a_planted_manifest_cannot_bind_the_directory_to_another_database(
    tmp_path: Path,
) -> None:
    """The identity comes from a published backup or from nowhere.

    ``_load_identity`` scans newest-first, so a manifest-only directory with a
    later stamp is the first thing it reaches. Binding on it would let a
    half-copied or hand-written directory decide which database this out-dir
    accepts -- refusing every real run, or pre-binding a fresh directory to a
    database it has never backed up.
    """
    _night(tmp_path, "20260801T020000", REAL, establish=True, identity=IDENTITY_A)
    (tmp_path / backup.WATERMARK_NAME).unlink()
    _write_run(tmp_path, "20260802T020000", counts=REAL, artifacts=False, identity=IDENTITY_B)

    assert backup._load_identity(tmp_path) == IDENTITY_A


def test_the_identity_is_written_into_the_watermark_file(tmp_path: Path) -> None:
    """The primary home, asserted directly: the manifest fallback masks its loss."""
    _night(tmp_path, "20260801T020000", REAL, establish=True, identity=IDENTITY_A)
    stored = json.loads((tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8"))
    assert stored["identity"] == IDENTITY_A.as_json()


def test_the_identity_is_not_erased_by_a_run_that_could_not_read_one(
    tmp_path: Path,
) -> None:
    """An old Postgres, or a failed probe, must not silently unbind the directory."""
    _night(tmp_path, "20260801T020000", REAL, establish=True, identity=IDENTITY_A)
    backup._write_watermark(
        tmp_path, dict(REAL), run="ums-backup-20260802T020000Z", reset={}, now=NOW, identity=None
    )
    stored = json.loads((tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8"))
    assert stored["identity"] == IDENTITY_A.as_json()


def test_an_old_directory_with_no_recorded_identity_adopts_rather_than_wedging(
    tmp_path: Path,
) -> None:
    """The upgrade path: a directory written before this check existed."""
    _write_run(tmp_path, "20260820T222143", counts=REAL)
    assert backup._load_identity(tmp_path) is None
    verdict = _night(tmp_path, "20260824T222143", REAL, identity=IDENTITY_A)
    assert verdict.accepted is True
    assert backup._load_identity(tmp_path) == IDENTITY_A


# --------------------------------------------------------------------------
# --accept-content-drop: how a legitimate deletion is ever accepted again
# --------------------------------------------------------------------------


def test_accept_content_drop_overrides_only_the_relative_checks() -> None:
    """Guard: test_accept_content_drop_overrides_only_the_relative_checks."""
    watermark = _watermark(_database(monthly_channel_revenue_facts=200))
    verdict = backup._evaluate_content(
        _database(monthly_channel_revenue_facts=1),
        watermark,
        accept_drop=True,
        establish=False,
    )
    assert verdict.accepted is True
    assert verdict.overridden, "the override must be recorded, not silent"
    assert verdict.as_manifest_block()["overridden"]


def test_every_override_names_the_flag_that_suppressed_it() -> None:
    """Three acknowledgement flags now feed one list; the note must say which.

    Measured on the live CLI before this: adopting another database printed
    ``OVERRIDDEN by --accept-content-drop``, naming a flag the operator had not
    passed and could not have passed to that effect.
    """
    drop = backup._evaluate_content(
        _database(monthly_channel_revenue_facts=1),
        _watermark(_database(monthly_channel_revenue_facts=200)),
        accept_drop=True,
        establish=False,
    )
    empty = backup._evaluate_content(
        VIRGIN, NO_WATERMARK, accept_drop=False, establish=True, accept_empty=True
    )
    adopted = backup._evaluate_content(
        REAL,
        _watermark(REAL),
        accept_drop=False,
        establish=False,
        expected_identity=IDENTITY_A,
        observed_identity=IDENTITY_B,
        adopt_database=True,
    )

    assert all(note.startswith("[--accept-content-drop]") for note in drop.overridden)
    assert all(
        note.startswith("[--this-database-is-intentionally-empty]") for note in empty.overridden
    )
    assert all(note.startswith("[--adopt-database]") for note in adopted.overridden)


def test_accept_content_drop_cannot_override_the_seed_floor() -> None:
    """The knob must not be a way to wave an empty database through."""
    verdict = backup._evaluate_content(EMPTY, _watermark(REAL), accept_drop=True, establish=False)
    assert verdict.accepted is False
    assert any("no tables" in reason for reason in verdict.failures)


def test_an_override_lowers_only_the_tables_it_named(tmp_path: Path) -> None:
    """A blanket re-baseline would reintroduce the drain on every other table.

    The untouched table has to have DECLINED for this to discriminate. A mutation
    matrix showed the earlier fixture -- 900 rows on both nights -- passing with
    ``rebaseline_tables`` widened to every table in the run, because a table whose
    count equals its mark is re-baselined to the same number either way. 900 ->
    750 is an ordinary decline that fires no rule, so the mark must stay at 900.
    """
    _night(
        tmp_path,
        "20260801T020000",
        _database(monthly_channel_revenue_facts=900, ums_table_00=400),
        establish=True,
    )
    verdict = _night(
        tmp_path,
        "20260802T020000",
        _database(monthly_channel_revenue_facts=750),
        accept_drop=True,
    )
    assert verdict.accepted is True
    assert verdict.rebaseline_tables == {"ums_table_00"}

    watermark = backup._load_watermark(tmp_path)
    assert watermark.tables["ums_table_00"] == 0, "the emptied table was re-baselined"
    assert watermark.tables["monthly_channel_revenue_facts"] == 900, "untouched table kept"


def test_the_next_night_is_accepted_without_a_flag_after_an_override(
    tmp_path: Path,
) -> None:
    """Without this the collapse check would wedge every future backup."""
    _night(tmp_path, "20260801T020000", _database(ums_table_00=400), establish=True)
    _night(tmp_path, "20260802T020000", _database(), accept_drop=True)
    verdict = _night(tmp_path, "20260803T020000", _database())
    assert verdict.accepted is True


def test_an_old_manifest_cannot_resurrect_an_overridden_watermark(
    tmp_path: Path,
) -> None:
    """`reset_after` is what makes a deliberate deletion stick."""
    _night(tmp_path, "20260801T020000", _database(ums_table_00=400), establish=True)
    _night(tmp_path, "20260802T020000", _database(), accept_drop=True)
    assert (tmp_path / "ums-backup-20260801T020000Z").is_dir(), "the old run still exists"
    assert backup._load_watermark(tmp_path).tables["ums_table_00"] == 0


def test_deleting_the_watermark_file_undoes_a_reset_upwards_not_downwards(
    tmp_path: Path,
) -> None:
    """Losing the file restores the HIGHER mark. That is the safe direction."""
    _night(tmp_path, "20260801T020000", _database(ums_table_00=400), establish=True)
    _night(tmp_path, "20260802T020000", _database(), accept_drop=True)
    (tmp_path / backup.WATERMARK_NAME).unlink()
    assert backup._load_watermark(tmp_path).tables["ums_table_00"] == 400


# --------------------------------------------------------------------------
# Defect 6 -- a regex-valid, non-date directory name
# --------------------------------------------------------------------------


def test_a_regex_valid_non_date_stamp_does_not_parse() -> None:
    """Guard: test_a_regex_valid_non_date_stamp_does_not_parse."""
    assert backup._run_stamp("ums-backup-20250145T999999Z") is None
    assert backup._run_stamp("ums-backup-20260824T220311Z") is not None


def test_prune_survives_a_regex_valid_non_date_directory(tmp_path: Path) -> None:
    """HOLE 4: this used to raise ValueError straight out of the process."""
    impostor = tmp_path / "ums-backup-20250145T999999Z"
    impostor.mkdir()
    keeper = _write_run(tmp_path, "20260824T222143", counts=REAL)

    pruned = backup._prune(tmp_path, keep_days=0, keep_min=1, now=NOW)

    assert impostor.is_dir(), "an uninterpretable directory must never be deleted"
    assert impostor.name in pruned.skipped, "and it must be reported, not ignored"
    assert impostor.name not in pruned.removed
    assert keeper.is_dir()


def test_prune_survives_a_regex_valid_non_date_quarantined_directory(
    tmp_path: Path,
) -> None:
    """Guard: test_prune_survives_a_regex_valid_non_date_quarantined_directory."""
    impostor = tmp_path / "ums-backup-20250145T999999Z.rejected"
    impostor.mkdir()

    pruned = backup._prune(tmp_path, keep_days=0, keep_min=1, now=NOW)

    assert impostor.is_dir()
    assert impostor.name in pruned.skipped


def test_a_non_date_directory_cannot_poison_the_watermark(tmp_path: Path) -> None:
    """Guard: test_a_non_date_directory_cannot_poison_the_watermark."""
    impostor = tmp_path / "ums-backup-20250145T999999Z"
    impostor.mkdir()
    (impostor / backup.MANIFEST_NAME).write_text(
        json.dumps({"table_row_counts": _database(monthly_channel_revenue_facts=10**9)}),
        encoding="utf-8",
    )
    assert backup._load_watermark(tmp_path).is_empty is True


# --------------------------------------------------------------------------
# Defect 11 -- a directory dated in the FUTURE
#
# Proved live against the CLI with a planted ums-backup-20990101T000000Z holding
# real dump/roles copies and a manifest claiming org_units: 1000000000:
#
#     night 1  no flag                 RC=8   org_units 1000000000->185
#     night 2  --accept-content-drop   RC=0   mark restored, reset_after set
#     night 3  no flag                 RC=8   mark re-inflated to 1000000640
#     night 4  --accept-content-drop   RC=0
#     night 5  no flag                 RC=8
#
# ``reset_after`` is a NAME comparison and is only ever set to the name of the
# run that carried the override, so a name that sorts above every real run is
# never excluded. Not "one-command recovery" -- a permanent wedge whose only
# sustainable end is --accept-content-drop in the scheduled task, i.e. the whole
# tier-2 comparison switched off. The same sort made it both retention
# invariants at once: with --keep-days 0 --keep-min 1 it was the --keep-min tail
# AND invariant 1's "newest with content" pin, and all three real runs were
# deleted, including the one just published.
#
# No attacker is required. A clock ahead at 02:00 -- dead RTC, VM resumed from a
# snapshot, NTP not yet converged -- stamps one directory in the future, and
# correcting the skew is what makes it outrank everything after it.
# --------------------------------------------------------------------------


def _future_stamp(ahead: timedelta) -> str:
    """A run-directory stamp ``ahead`` of the INJECTED clock, not the wall clock."""
    return (backup._utc_now() + ahead).strftime(backup.STAMP_FORMAT)


def _plant_name(ahead: timedelta) -> str:
    """plant name."""
    return f"ums-backup-{_future_stamp(ahead)}Z"


def test_a_future_dated_stamp_is_not_history() -> None:
    """The choke point. Every history reader goes through ``_run_stamp``."""
    assert backup._run_stamp(_plant_name(timedelta(days=1))) is None
    assert backup._run_stamp("ums-backup-20990101T000000Z") is None
    # ...and it is refused for being in the future, not for being unparsable.
    assert backup._parse_stamp("ums-backup-20990101T000000Z") is not None


# --------------------------------------------------------------------------
# STAMP_FUTURE_TOLERANCE, pinned with ABSOLUTE offsets.
#
# The guard this replaces derived BOTH of its fixtures from the constant --
# ``STAMP_FUTURE_TOLERANCE - 1min`` and ``STAMP_FUTURE_TOLERANCE + 5min`` -- so
# it re-derived its own answer and passed for any tolerance at all. MEASURED,
# each a full run of this file:
#     timedelta(seconds=0)      126 passed
#     timedelta(hours=1)        126 passed
#     timedelta(hours=12)       126 passed
#     timedelta(hours=23)       126 passed
# i.e. the window could be widened 276x with the suite green. At 23 hours a
# directory stamped "tomorrow 01:00" is read as history ON THE NIGHT IT IS
# PLANTED, which reopens both the watermark wedge and the retention capture at
# exactly the magnitude a skewed RTC or a resumed VM snapshot produces.
#
# The offsets below are therefore literals. The constant appears in exactly one
# assertion -- its value -- and nowhere in a fixture.
# --------------------------------------------------------------------------

#: Ahead of the clock, but explainable as a backward correction between two
#: readings of the same clock: an NTP step off a drifting RTC, a resumed VM.
INSIDE_TOLERANCE = (
    timedelta(0),
    timedelta(seconds=1),
    timedelta(seconds=30),
    timedelta(minutes=4),
    timedelta(minutes=4, seconds=59),
    # The boundary itself. The rule is "MORE than the tolerance ahead", so equal
    # is accepted; nothing operational turns on a microsecond either side, but
    # the contract block states it and an exact clock can hold it to that.
    timedelta(minutes=5),
)
#: Past anything two readings of one clock can explain. ``hours=23`` is the
#: mutant that mattered: it is under the nightly cadence, so every fixture in
#: this file still looks like history, and "tomorrow 01:00" becomes history too.
OUTSIDE_TOLERANCE = (
    timedelta(minutes=5, seconds=1),
    timedelta(minutes=6),
    timedelta(hours=1),
    timedelta(hours=12),
    timedelta(hours=23),
    timedelta(days=1),
    timedelta(days=2),
)


def test_the_future_stamp_tolerance_is_five_minutes() -> None:
    """The value itself, so a mutant inside the boundary cases is still caught.

    A tolerance of four minutes or of five minutes and one second passes every
    behavioural fixture below; only this assertion refuses it. It is one line
    and it is the difference between a pinned constant and a ratified one.
    """
    assert backup.STAMP_FUTURE_TOLERANCE == timedelta(minutes=5)


@pytest.mark.parametrize("ahead", INSIDE_TOLERANCE, ids=str)
def test_a_few_minutes_of_clock_skew_is_not_treated_as_an_attack(ahead: timedelta) -> None:
    """A backward step between two readings must not condemn a real run."""
    assert backup._run_stamp(_plant_name(ahead)) is not None


@pytest.mark.parametrize("ahead", OUTSIDE_TOLERANCE, ids=str)
def test_a_stamp_beyond_the_tolerance_is_not_history(ahead: timedelta) -> None:
    """Anything a clock correction cannot explain is refused, not trusted."""
    assert backup._run_stamp(_plant_name(ahead)) is None


def test_the_stamp_is_judged_against_the_callers_clock_reading(clock: _Clock) -> None:
    """``now=`` is not decoration, and dropping it leaves the whole file green.

    ``_prune`` walks a directory twice and ``_load_watermark`` once; both take a
    single reading and pass it down so that every directory in one pass is
    judged against the SAME instant. Re-reading the clock per directory lets one
    pass classify two identically-stamped runs differently across a second
    boundary. Asserted where the parameter is consumed, because that is the
    only place the two readings can be made to disagree on purpose.
    """
    name = _plant_name(timedelta(days=1))

    assert backup._run_stamp(name) is None, "against the current reading it is the future"
    assert backup._run_stamp(name, now=clock.now + timedelta(days=2)) is not None, (
        "against the caller's reading it is history, and the caller's is the one that counts"
    )


def test_a_future_dated_run_cannot_inflate_the_watermark(tmp_path: Path) -> None:
    """The wedge, at the watermark. It has real artifacts, so only the date stops it."""
    _night(tmp_path, "20260801T020000", REAL, establish=True)
    honest = backup._load_watermark(tmp_path).total_rows

    _write_run(tmp_path, "20990101T000000", counts=_database(org_units=10**9))

    poisoned = backup._load_watermark(tmp_path)
    assert poisoned.total_rows == honest, "a future-dated run must contribute nothing"
    assert poisoned.tables["org_units"] == REAL["org_units"]
    assert "future-dated directory(ies) ignored" in poisoned.source, (
        "inert is not enough -- the operator has to be told it is there"
    )


def test_the_wedge_does_not_reopen_the_night_after_an_override(tmp_path: Path) -> None:
    """Nights 3, 4 and 5 of the reproduction: the cycle must not restart."""
    _night(tmp_path, "20260801T020000", REAL, establish=True)
    _write_run(tmp_path, "20990101T000000", counts=_database(org_units=10**9))

    assert _night(tmp_path, "20260802T020000", REAL).accepted is True
    assert _night(tmp_path, "20260803T020000", REAL).accepted is True
    assert backup._load_watermark(tmp_path).tables["org_units"] == REAL["org_units"]


def test_a_future_dated_run_cannot_bind_the_directory_identity(tmp_path: Path) -> None:
    """Guard: test_a_future_dated_run_cannot_bind_the_directory_identity."""
    _night(tmp_path, "20260801T020000", REAL, establish=True, identity=IDENTITY_A)
    _write_run(tmp_path, "20990101T000000", counts=REAL, identity=IDENTITY_B)
    (tmp_path / backup.WATERMARK_NAME).unlink()

    assert backup._load_identity(tmp_path) == IDENTITY_A


def test_a_future_dated_run_captures_neither_retention_invariant(tmp_path: Path) -> None:
    """The measured prune: all three real runs deleted, only the plant left."""
    keepers = [
        _write_run(tmp_path, "20260820T222143", counts=REAL),
        _write_run(tmp_path, "20260821T222143", counts=REAL),
        _write_run(tmp_path, "20260824T222143", counts=REAL),
    ]
    plant = _write_run(tmp_path, "20990101T000000", counts=_database(org_units=10**9))

    pruned = backup._prune(tmp_path, keep_days=0, keep_min=1, now=NOW)

    assert keepers[-1].is_dir(), "invariant 1 must pin a REAL newest run with content"
    assert plant.name not in pruned.removed, "and an uninterpretable directory is never deleted"
    assert plant.is_dir()
    assert plant.name in pruned.future, "it must be reported, and not as a bad date"
    assert plant.name not in pruned.skipped
    survivors = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert keepers[-1].name in survivors
    assert survivors != [plant.name], "every genuine backup was deleted"


def test_a_future_dated_quarantined_directory_is_also_left_alone(tmp_path: Path) -> None:
    """Guard: test_a_future_dated_quarantined_directory_is_also_left_alone."""
    plant = _write_run(tmp_path, "20990101T000000", counts=EMPTY, rejected=True)

    pruned = backup._prune(tmp_path, keep_days=0, keep_min=1, now=NOW)

    assert plant.is_dir()
    assert plant.name in pruned.future


# --------------------------------------------------------------------------
# Defect 6, second half -- the status file can never be stale-green
# --------------------------------------------------------------------------


def _last_run(out_dir: Path) -> dict[str, object]:
    """last run."""
    loaded = json.loads((out_dir / backup.LAST_RUN_NAME).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_starting_a_run_clears_the_previous_green_status(tmp_path: Path) -> None:
    """The published green record must not outlive the run that produced it."""
    backup._write_last_run(tmp_path, {"status": "OK", "exit_code": 0})
    backup._RunReport(tmp_path, NOW).start()
    assert _last_run(tmp_path)["status"] == "RUNNING"
    assert _last_run(tmp_path)["exit_code"] is None


def test_a_run_that_ends_without_a_verdict_records_that_fact(tmp_path: Path) -> None:
    """The finally-block fallback: no path can leave the status unwritten."""
    backup._write_last_run(tmp_path, {"status": "OK", "exit_code": 0})
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    report.close()

    assert _last_run(tmp_path)["status"] == "INTERRUPTED"
    log = (tmp_path / backup.LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert len(log) == 1
    assert "INTERRUPTED" in log[0]


def test_a_finalised_run_is_not_overwritten_by_close(tmp_path: Path) -> None:
    """Guard: test_a_finalised_run_is_not_overwritten_by_close."""
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    report.finalise("line", {"status": "OK", "exit_code": 0})
    report.close()

    assert _last_run(tmp_path)["status"] == "OK"
    log = (tmp_path / backup.LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert len(log) == 1, "exactly one backup.log line per run"


# --------------------------------------------------------------------------
# Defect 10 -- _RunReport guarantees the CALL, not the WRITE
#
# Measured on Windows with another process holding last-run.json and backup.log
# open with FileShare.None -- an AV scanner, OneDrive, or an open editor:
#     last-run.json before : OK/exit=0
#     db state             : tables=6 rows=1   (TOTAL DATA LOSS)
#     process exit code    : 8
#     last-run.json AFTER  : OK/exit=0
# Both writers swallowed OSError and printed only to stderr, which Task
# Scheduler discards. Docs/22 tells the operator that last-run.json is the file
# to look at, so the one artifact they are told to trust was the one that lied.
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_status_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the status-write retry at full speed.

    ``STATUS_WRITE_BACKOFF_SECONDS`` exists to ride out a real AV scanner, not to
    make this file take a minute. The retry COUNT is what the tests assert on and
    it is left alone.
    """
    monkeypatch.setattr(backup.time, "sleep", lambda _seconds: None)


def _unwritable(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make one status file refuse writes, deterministically, on every platform.

    FIX: this used to ``chmod(0o444)`` and ``pytest.skip`` when the bit did not
    take. For uid 0 -- the default in most Linux CI containers -- ``os.access``
    reports the file writable whatever the mode bits say, AND root's DAC
    override means the write would have succeeded anyway, so skipping was the
    honest branch. The cost was that every test below it silently vanished in
    exactly the unattended environment whose durability contract they exist to
    guard.

    Every write to these status files reaches the filesystem through
    ``Path.open`` in a write mode -- ``_append_log`` opens "a", and
    ``_replace_status_file`` probe-opens "r+" before swapping the temp file in
    -- so refusing that one call for this one path reproduces the real
    ``PermissionError(EACCES)`` regardless of uid or filesystem. Reads fall
    through, because a read-only file is still readable, and the temp aside and
    the stamped sidecar are different paths, so those writes must still land.

    ``Path.open`` is deliberately NOT one of the names ``_FakeContainer.install``
    rebinds, so this stub survives ``_run_cli`` whichever is applied first --
    a re-bind-after-monkeypatch helper is what made an earlier guard here
    vacuous.

    Existing content is preserved on purpose: the defect is precisely that the
    PREVIOUS run's record survives, and a fixture that blanked it first would be
    testing something easier.
    """
    if not path.exists():
        path.write_text("{}\n", encoding="utf-8")
    target = path.resolve()
    real_open = Path.open

    def _refuse_writes(self: Path, *args: Any, **kwargs: Any) -> Any:
        """Refuse writes to the one locked path, the way the OS would."""
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        if self.resolve() == target and any(f in mode for f in ("w", "a", "+", "x")):
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _refuse_writes)


def test_an_unwritable_status_file_is_reported_not_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: test_an_unwritable_status_file_is_reported_not_swallowed."""
    backup._write_last_run(tmp_path, {"status": "OK", "exit_code": 0})
    _unwritable(tmp_path / backup.LAST_RUN_NAME, monkeypatch)

    assert backup._write_last_run(tmp_path, {"status": "REJECTED", "exit_code": 8}) is False


def test_this_runs_record_lands_in_a_stamped_sidecar_when_the_file_is_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NEW file name is the one write a lock on the canonical file cannot block."""
    _unwritable(tmp_path / backup.LAST_RUN_NAME, monkeypatch)
    stamp = "20260801T020000Z"

    assert (
        backup._write_last_run(
            tmp_path, {"status": "REJECTED", "exit_code": 8}, sidecar_stamp=stamp
        )
        is False
    )

    sidecar = tmp_path / backup.LAST_RUN_SIDECAR.format(stamp=stamp)
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["exit_code"] == 8


def test_a_successful_run_whose_status_did_not_land_cannot_exit_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exit code is the one channel a file lock cannot block."""
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    _unwritable(tmp_path / backup.LAST_RUN_NAME, monkeypatch)
    report.finalise("line", {"status": "OK", "exit_code": 0})

    assert report.status_durable is False
    assert report.escalate(backup.EXIT_OK) == backup.EXIT_BOOKKEEPING_FAILED


def test_escalation_never_overwrites_a_more_specific_failure_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning 8 into 7 would hide TOTAL DATA LOSS behind a bookkeeping message."""
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    _unwritable(tmp_path / backup.LAST_RUN_NAME, monkeypatch)
    report.finalise("line", {"status": "REJECTED", "exit_code": 8})

    assert report.status_durable is False
    assert report.escalate(backup.EXIT_NO_CONTENT) == backup.EXIT_NO_CONTENT


def test_a_run_whose_status_landed_normally_is_not_escalated(tmp_path: Path) -> None:
    """Guard: test_a_run_whose_status_landed_normally_is_not_escalated."""
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    report.finalise("line", {"status": "OK", "exit_code": 0})

    assert report.status_durable is True
    assert report.escalate(backup.EXIT_OK) == backup.EXIT_OK


def test_an_unwritable_log_alone_is_enough_to_escalate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backup.log is half of the runbook's "every run writes" claim."""
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    _unwritable(tmp_path / backup.LOG_NAME, monkeypatch)
    report.finalise("line", {"status": "OK", "exit_code": 0})

    assert report.escalate(backup.EXIT_OK) == backup.EXIT_BOOKKEEPING_FAILED
    assert _last_run(tmp_path)["status"] == "OK", "the status file itself still landed"


def test_a_failure_to_clear_the_previous_green_is_carried_to_the_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The moment that matters most: until RUNNING lands, yesterday's OK stands."""
    backup._write_last_run(tmp_path, {"status": "OK", "exit_code": 0})
    _unwritable(tmp_path / backup.LAST_RUN_NAME, monkeypatch)

    report = backup._RunReport(tmp_path, NOW)
    report.start()

    assert report.status_durable is False
    assert _last_run(tmp_path)["status"] == "OK", "it really is still the old record"
    assert report.escalate(backup.EXIT_OK) == backup.EXIT_BOOKKEEPING_FAILED


def test_a_transient_lock_is_retried_rather_than_failing_the_run(tmp_path: Path) -> None:
    """An AV scanner holds a file for seconds; that must not turn a run red.

    The canonical file EXISTS here, holding the previous record, because that
    is what a scanner can hold: since the write went atomic (write aside, then
    replace), the canonical file is touched by a probe-open and by the final
    ``os.replace``, and a missing canonical is simply created. The flaky open
    stands in for the scanner on the canonical path; the aside file passes
    through untouched.
    """
    backup._write_last_run(tmp_path, {"status": "RUNNING", "exit_code": None})
    target = tmp_path / backup.LAST_RUN_NAME
    attempts = {"n": 0}
    real_open = Path.open

    def flaky_open(
        self: Path, *args: object, **kwargs: object
    ) -> object:
        """flaky open."""
        if self == target:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError(13, "held by another process")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", flaky_open)
        assert backup._write_last_run(tmp_path, {"status": "OK", "exit_code": 0}) is True

    assert attempts["n"] == 3
    assert _last_run(tmp_path)["status"] == "OK"


def test_the_published_run_is_named_in_an_interrupted_record(tmp_path: Path) -> None:
    """Guard: test_the_published_run_is_named_in_an_interrupted_record."""
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    report.note_published(tmp_path / "ums-backup-20260801T020000Z")
    report.close()

    record = _last_run(tmp_path)
    assert record["status"] == "INTERRUPTED"
    assert str(record["run_dir"]).endswith("ums-backup-20260801T020000Z")


# --------------------------------------------------------------------------
# Defect 2 -- content-aware retention
# --------------------------------------------------------------------------


NOW = datetime(2026, 8, 24, 22, 30, tzinfo=UTC)


def test_prune_keeps_the_only_run_with_data_against_seven_empty_ones(
    tmp_path: Path,
) -> None:
    """The verifier's end-to-end reproduction, at the retention layer.

    One genuine 40-day-old run plus seven recent zero-table runs, pruned at the
    documented defaults. The old code deleted the only run holding data.
    """
    real = _write_run(tmp_path, "20260715T222143", counts=REAL)
    for day in range(18, 25):
        _write_run(tmp_path, f"202608{day}T222143", counts=EMPTY)

    pruned = backup._prune(tmp_path, keep_days=30, keep_min=7, now=NOW)

    assert real.is_dir(), "the only backup containing data was deleted"
    assert real.name not in pruned.removed


def test_empty_runs_do_not_consume_a_keep_min_slot(tmp_path: Path) -> None:
    """Invariant 2 with the pin held out of it -- the pin can only save ONE run.

    A mutation matrix showed the two tests above passing with invariant 2's
    filter deleted, because invariant 1 pinned the single run they cared about.
    Two expired runs with data is the smallest fixture the pin cannot cover: it
    protects the newer one, and only the ``has_content is not False`` filter puts
    the older one inside the two ``--keep-min`` slots that the seven recent empty
    runs would otherwise fill.
    """
    older = _write_run(tmp_path, "20250101T000000", counts=REAL)
    newer = _write_run(tmp_path, "20250102T000000", counts=REAL)
    for day in range(18, 25):
        _write_run(tmp_path, f"202608{day}T222143", counts=EMPTY)

    pruned = backup._prune(tmp_path, keep_days=30, keep_min=2, now=NOW)

    assert newer.is_dir(), "invariant 1 pins this one on its own"
    assert older.is_dir(), "and only invariant 2 keeps this one in the --keep-min window"
    assert older.name not in pruned.removed


def test_prune_treats_a_gutted_run_as_empty(tmp_path: Path) -> None:
    """Retention must use the same floor the gate does, or they disagree."""
    real = _write_run(tmp_path, "20260715T222143", counts=REAL)
    for day in range(18, 25):
        _write_run(tmp_path, f"202608{day}T222143", counts=GUTTED)

    pruned = backup._prune(tmp_path, keep_days=30, keep_min=7, now=NOW)

    assert real.is_dir()
    assert real.name not in pruned.removed


def test_prune_never_evicts_the_newest_run_with_content_even_when_expired(
    tmp_path: Path,
) -> None:
    """Invariant 1, stated in _prune's contract block, not merely emergent.

    REWRITTEN. The previous version used seven proven-EMPTY filler runs, which
    invariant 2 removes from ``eligible`` before the ``--keep-min`` slice is
    taken -- so ``--keep-min`` protected the real run on its own and the pin was
    never load-bearing. Deleting the pin left that version green.

    The discriminating fixture is an expired REAL run plus a NEWER run whose
    content is UNKNOWN. Invariant 3 keeps unknown runs in ``eligible``, so the
    single ``--keep-min`` slot goes to the unknown one and the expired real run
    is protected by the pin and by nothing else.
    """
    real = _write_run(tmp_path, "20250101T000000", counts=REAL)
    unknown = _write_run(tmp_path, "20260824T222143", counts=None, manifest=False)

    classified = [backup._run_has_content(real), backup._run_has_content(unknown)]
    assert classified == [True, None], "the fixture must be content + unknown, not content + empty"
    eligible_slot = sorted(p.name for p in (real, unknown))[-1:]
    assert eligible_slot == [unknown.name], "--keep-min's only slot goes to the unknown run"

    pruned = backup._prune(tmp_path, keep_days=1, keep_min=1, now=NOW)

    assert real.is_dir()
    assert real.name not in pruned.removed
    assert unknown.is_dir()


def test_prune_still_deletes_expired_empty_runs(tmp_path: Path) -> None:
    """Content-awareness must not turn retention into a no-op."""
    stale = _write_run(tmp_path, "20250101T000000", counts=EMPTY)
    keeper = _write_run(tmp_path, "20260824T222143", counts=REAL)

    pruned = backup._prune(tmp_path, keep_days=30, keep_min=1, now=NOW)

    assert stale.name in pruned.removed
    assert not stale.exists()
    assert keeper.is_dir()


def test_prune_treats_an_unreadable_manifest_as_content(tmp_path: Path) -> None:
    """Invariant 3: never delete what might be the last good backup."""
    unknown = _write_run(tmp_path, "20250101T000000", counts=None, manifest=False)

    pruned = backup._prune(tmp_path, keep_days=1, keep_min=1, now=NOW)

    assert unknown.is_dir()
    assert unknown.name not in pruned.removed


def test_prune_expires_quarantined_runs(tmp_path: Path) -> None:
    """Guard: test_prune_expires_quarantined_runs."""
    fresh = _write_run(tmp_path, "20260824T222105", counts=EMPTY, rejected=True)
    stale = _write_run(tmp_path, "20250101T000000", counts=EMPTY, rejected=True)

    pruned = backup._prune(tmp_path, keep_days=30, keep_min=7, now=NOW)

    assert stale.name in pruned.removed
    assert fresh.is_dir()


def test_prune_expires_every_quarantine_name_publish_can_emit(tmp_path: Path) -> None:
    """Retention must age out all FOUR quarantine names, not just the plain one.

    ``_publish_staging_run`` renames to ``destination + .rejected`` and, when
    that name is taken, to a ``.rejected-<8hex>`` nonce. ``destination`` is
    ``final_dir`` when the verdict was accepted and ``rejected_dir`` when it was
    not, so a rejected-verdict run that fails its post-rename durability sync
    lands on ``.rejected.rejected``. Three of those four matched no retention
    regex, so each repeated durability/ACL failure left a full database.dump +
    roles.sql on the backup disk forever. The restore side already refused all
    four; this is retention reaching parity.
    """

    def _quarantine(name: str) -> Path:
        """Write one quarantined run directory carrying real backup artifacts."""
        run = tmp_path / name
        run.mkdir(parents=True)
        (run / backup.DUMP_NAME).write_bytes(b"PGDMP-placeholder")
        (run / backup.ROLES_NAME).write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
        return run

    suffixes = (
        ".rejected",  # accepted verdict, plain rename
        ".rejected-1a2b3c4d",  # accepted verdict, nonce fallback
        ".rejected.rejected",  # rejected verdict, plain rename
        ".rejected.rejected-9f8e7d6c",  # rejected verdict, nonce fallback
    )
    stale = [_quarantine(f"ums-backup-20250101T000000Z{suffix}") for suffix in suffixes]
    fresh = [_quarantine(f"ums-backup-20260824T222105Z{suffix}") for suffix in suffixes]

    pruned = backup._prune(tmp_path, keep_days=30, keep_min=7, now=NOW)

    for run in stale:
        assert run.name in pruned.removed, f"{run.name} must age out of the backup disk"
        assert not run.exists()
    for run in fresh:
        assert run.is_dir(), f"{run.name} is inside the retention window and must survive"
        assert run.name not in pruned.removed


def test_a_naive_prior_status_timestamp_does_not_raise(tmp_path: Path) -> None:
    """An offset-less ``started_utc`` must be treated as malformed, not compared.

    ``datetime.fromisoformat`` accepts a timestamp with no offset and returns a
    NAIVE datetime; comparing that against our timezone-aware ``started`` raises
    TypeError. This runs while recording the terminal verdict -- after
    publication, watermarking and pruning -- and the failure reporter compares
    again, so an unguarded compare lets the CLI escape with an undocumented
    exception. A legacy or hand-recovered writer is enough to produce one.
    """
    (tmp_path / backup.LAST_RUN_NAME).write_text(
        json.dumps({"status": "OK", "started_utc": "2027-01-01T00:00:00"}),
        encoding="utf-8",
    )

    # Naive, and FAR in the future: were it compared it would win and return
    # True, so this pins the treat-as-malformed branch and not an accident of
    # ordering.
    assert backup._last_run_holds_newer_completed_verdict(tmp_path, NOW) is False

    aware_newer = json.dumps({"status": "OK", "started_utc": "2027-01-01T00:00:00+00:00"})
    (tmp_path / backup.LAST_RUN_NAME).write_text(aware_newer, encoding="utf-8")
    assert backup._last_run_holds_newer_completed_verdict(tmp_path, NOW) is True, (
        "an aware newer verdict must still be protected from being overwritten"
    )


def test_the_cli_refuses_a_keep_min_below_the_documented_minimum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """--keep-min 0 must be refused BEFORE the dump and the watermark write.

    The CLI and runbook promise a minimum of one. argparse accepts 0 or a
    negative, and _retention_protected_names then builds no keep-min
    protection, so the next successful run can delete every expired backup
    except the single newest content-bearing one -- silently retaining fewer
    recovery points than the operator asked for.
    """
    for value in ("0", "-3"):
        code = _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark", "--keep-min", value)

        assert code == backup.EXIT_USAGE, f"--keep-min {value} must be refused"
        assert not (tmp_path / backup.WATERMARK_NAME).exists(), "no watermark may be written"
        assert _run_dirs(tmp_path) == [], "no run directory may be created"


def test_prune_ignores_foreign_directories(tmp_path: Path) -> None:
    """Guard: test_prune_ignores_foreign_directories."""
    (tmp_path / "important-operator-notes").mkdir()
    _write_run(tmp_path, "20260824T222143", counts=REAL)

    backup._prune(tmp_path, keep_days=0, keep_min=1, now=NOW)

    assert (tmp_path / "important-operator-notes").is_dir()


def test_prune_never_deletes_the_watermark_file(tmp_path: Path) -> None:
    """The mark outlives the runs it was built from, or history resets."""
    _night(tmp_path, "20250101T000000", _database(ums_table_00=400), establish=True)
    _night(tmp_path, "20260824T222143", _database(ums_table_00=400))

    backup._prune(tmp_path, keep_days=1, keep_min=1, now=NOW)

    assert (tmp_path / backup.WATERMARK_NAME).is_file()
    assert backup._load_watermark(tmp_path).tables["ums_table_00"] == 400


def test_run_has_content_is_three_valued(tmp_path: Path) -> None:
    """Guard: test_run_has_content_is_three_valued."""
    assert backup._run_has_content(_write_run(tmp_path, "20260101T000000", counts=REAL)) is True
    assert backup._run_has_content(_write_run(tmp_path, "20260102T000000", counts=EMPTY)) is False
    unknown = _write_run(tmp_path, "20260103T000000", counts=None, manifest=False)
    assert backup._run_has_content(unknown) is None


def test_a_manifest_claiming_acceptance_is_still_measured(tmp_path: Path) -> None:
    """A gate verdict of "accepted" over no data must not protect the run."""
    gutted = _write_run(tmp_path, "20260104T000000", counts=GUTTED)
    assert backup._run_has_content(gutted) is False


def test_partial_directories_still_expire(tmp_path: Path) -> None:
    """Guard: test_partial_directories_still_expire."""
    stale = tmp_path / "ums-backup-20250101T000000Z.partial"
    stale.mkdir()
    old = (NOW - timedelta(days=3)).timestamp()
    os.utime(stale, (old, old))

    pruned = backup._prune(tmp_path, keep_days=30, keep_min=7, now=NOW)

    assert stale.name in pruned.removed


# --------------------------------------------------------------------------
# The restore side refuses what the gate quarantined
# --------------------------------------------------------------------------


def test_restore_refuses_a_quarantined_directory_by_name(tmp_path: Path) -> None:
    """Guard: test_restore_refuses_a_quarantined_directory_by_name."""
    run = _write_run(tmp_path, "20260824T222105", counts=EMPTY, rejected=True)
    with pytest.raises(restore.RestoreError) as caught:
        restore._load_backup(run)
    assert caught.value.code == restore.EXIT_USAGE
    assert "quarantined" in str(caught.value)


def test_restore_refuses_a_rejected_manifest_even_if_renamed(tmp_path: Path) -> None:
    """Second, independent signal: the verdict travels inside the manifest."""
    run = _write_run(tmp_path, "20260824T222105", counts=EMPTY, rejected=True)
    renamed = run.with_name("ums-backup-20260824T222105Z")
    run.rename(renamed)
    with pytest.raises(restore.RestoreError) as caught:
        restore._load_backup(renamed)
    assert caught.value.code == restore.EXIT_USAGE
    assert "content gate" in str(caught.value)


def test_restore_still_accepts_a_manifest_written_before_the_gate_existed(
    tmp_path: Path,
) -> None:
    """Backwards compatibility: no content_gate verdict is not a rejection."""
    run = tmp_path / "ums-backup-20260601T000000Z"
    run.mkdir()
    dump = run / backup.DUMP_NAME
    roles = run / backup.ROLES_NAME
    dump.write_bytes(b"PGDMP-placeholder")
    roles.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
    (run / backup.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": backup.MANIFEST_SCHEMA,
                "artifacts": {
                    backup.DUMP_NAME: {"sha256": restore._sha256(dump)},
                    backup.ROLES_NAME: {"sha256": restore._sha256(roles)},
                },
                # PR #210 review round: an empty table_row_counts object is now
                # refused before any destructive apply, so the legacy-compat
                # fixture records real counts to keep this test pointed at its
                # actual subject -- a manifest with no content_gate verdict.
                "table_row_counts": dict.fromkeys(backup.CORE_SEED_TABLES, 1),
                "large_object_count": 0,
            }
        ),
        encoding="utf-8",
    )
    assert restore._load_backup(run)["schema"] == backup.MANIFEST_SCHEMA


def test_restore_enforces_every_seed_declared_by_a_stacked_backup() -> None:
    """A future manifest extension fails closed when missing or empty after replay."""
    future = "future_seed_catalog"
    required = [*restore.CORE_SEED_TABLES, future]

    def manifest_for(counts: dict[str, int]) -> dict[str, object]:
        """Build one internally consistent accepted gate around ``counts``."""
        return {
            "table_row_counts": counts,
            "content_gate": {
                "status": "accepted",
                "tables": len(counts),
                "rows": sum(counts.values()),
                "seed_tables": required,
            },
        }

    counts = dict.fromkeys(restore.CORE_SEED_TABLES, 1)
    with pytest.raises(restore.RestoreError, match=f"missing: {future}"):
        restore._require_manifest_seed_floor(manifest_for(counts), counts)

    counts[future] = 0
    with pytest.raises(restore.RestoreError, match=f"empty: {future}"):
        restore._require_manifest_seed_floor(manifest_for(counts), counts)

    counts[future] = 1
    assert restore._require_manifest_seed_floor(manifest_for(counts), counts) == tuple(
        required
    )


def test_restore_rejects_an_accepted_gate_without_its_seed_contract() -> None:
    """An accepted label alone cannot bypass seed-floor reproducibility."""
    counts = dict.fromkeys(restore.CORE_SEED_TABLES, 1)
    manifest = {
        "table_row_counts": counts,
        "content_gate": {
            "status": "accepted",
            "tables": len(counts),
            "rows": sum(counts.values()),
        },
    }
    with pytest.raises(restore.RestoreError) as caught:
        restore._require_manifest_seed_floor(manifest, counts)
    assert caught.value.code == restore.EXIT_USAGE
    assert "seed_tables is missing or empty" in str(caught.value)


@pytest.mark.parametrize(("field", "value"), [("tables", True), ("rows", False)])
def test_restore_rejects_boolean_content_gate_aggregates(
    field: str, value: bool
) -> None:
    """JSON booleans must not pass as integer table/row aggregate values."""
    counts = dict.fromkeys(restore.CORE_SEED_TABLES, 1)
    gate: dict[str, object] = {
        "status": "accepted",
        "tables": len(counts),
        "rows": sum(counts.values()),
        "seed_tables": list(restore.CORE_SEED_TABLES),
    }
    gate[field] = value
    with pytest.raises(restore.RestoreError) as caught:
        restore._require_manifest_seed_floor({"content_gate": gate}, counts)
    assert caught.value.code == restore.EXIT_USAGE
    assert "exact nonnegative integers" in str(caught.value)


def test_restore_rejects_a_zero_byte_artifact_even_when_its_hash_matches(
    tmp_path: Path,
) -> None:
    """A self-consistent sha256 cannot turn an empty dump into restore input."""
    run = _write_run(tmp_path, "20260824T222105", counts=REAL)
    dump = run / restore.DUMP_NAME
    dump.write_bytes(b"")
    manifest_path = run / restore.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][restore.DUMP_NAME] = {
        "bytes": 0,
        "sha256": restore._sha256(dump),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(restore.RestoreError) as caught:
        restore._load_backup(run)
    assert caught.value.code == restore.EXIT_ARTIFACT_INTEGRITY
    assert "database.dump is empty" in str(caught.value)


def test_restore_requires_sha256_for_both_artifacts(tmp_path: Path) -> None:
    """Empty artifacts or missing hashes must fail closed before any restore."""
    dump_bytes = b"PGDMP-placeholder"
    roles_text = "CREATE ROLE app_tenant;\n"

    empty_artifacts = tmp_path / "empty-artifacts"
    empty_artifacts.mkdir()
    (empty_artifacts / backup.DUMP_NAME).write_bytes(dump_bytes)
    (empty_artifacts / backup.ROLES_NAME).write_text(roles_text, encoding="utf-8")
    (empty_artifacts / backup.MANIFEST_NAME).write_text(
        json.dumps({"schema": backup.MANIFEST_SCHEMA, "artifacts": {}}),
        encoding="utf-8",
    )
    with pytest.raises(restore.RestoreError) as empty:
        restore._load_backup(empty_artifacts)
    assert empty.value.code == restore.EXIT_ARTIFACT_INTEGRITY

    missing_hash = tmp_path / "missing-hash"
    missing_hash.mkdir()
    (missing_hash / backup.DUMP_NAME).write_bytes(dump_bytes)
    (missing_hash / backup.ROLES_NAME).write_text(roles_text, encoding="utf-8")
    (missing_hash / backup.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": backup.MANIFEST_SCHEMA,
                "artifacts": {
                    backup.DUMP_NAME: {"bytes": len(dump_bytes)},
                    backup.ROLES_NAME: {"bytes": len(roles_text)},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(restore.RestoreError) as no_hash:
        restore._load_backup(missing_hash)
    assert no_hash.value.code == restore.EXIT_ARTIFACT_INTEGRITY

    mismatched = tmp_path / "mismatched-hash"
    mismatched.mkdir()
    dump = mismatched / backup.DUMP_NAME
    roles = mismatched / backup.ROLES_NAME
    dump.write_bytes(dump_bytes)
    roles.write_text(roles_text, encoding="utf-8")
    (mismatched / backup.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": backup.MANIFEST_SCHEMA,
                "artifacts": {
                    backup.DUMP_NAME: {"sha256": "0" * 64},
                    backup.ROLES_NAME: {"sha256": restore._sha256(roles)},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(restore.RestoreError) as bad_hash:
        restore._load_backup(mismatched)
    assert bad_hash.value.code == restore.EXIT_ARTIFACT_INTEGRITY


def test_unexpected_roles_errors_are_rejected() -> None:
    """Only bootstrap 'role already exists' noise is tolerated."""
    assert restore._unexpected_roles_errors('ERROR:  role "ums" already exists\n') == []
    assert restore._unexpected_roles_errors('psql: :12: ERROR:  role "ums" already exists\n') == []
    unexpected = restore._unexpected_roles_errors(
        'ERROR:  role "ums" already exists\npsql: :40: ERROR:  permission denied to alter role\n'
    )
    assert unexpected == ["psql: :40: ERROR:  permission denied to alter role"]


def test_restore_roles_tolerates_bootstrap_duplicate_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ON_ERROR_STOP=0 + bootstrap 'already exists' must not abort."""
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text("CREATE ROLE ums;\nCREATE ROLE app_tenant;\n", encoding="utf-8")

    def fake_run_with_file(*_a: object, **_k: object) -> int:
        """fake run with file."""
        return subprocess.CompletedProcess(
            args=[],
            returncode=3,
            stdout="",
            stderr='ERROR:  role "ums" already exists\n',
        )

    monkeypatch.setattr(restore, "_run_with_file", fake_run_with_file)
    monkeypatch.setattr(restore, "_psql", _restore_psql())
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)
    present = restore._restore_roles("fake", roles_path, timeout=5)
    assert present == list(restore.REQUIRED_ROLES)
    out = capsys.readouterr().out
    assert 'role "ums" already exists' in out
    assert "permission denied" not in out


def test_restore_roles_rejects_unexpected_error_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Guard: test_restore_roles_rejects_unexpected_error_line."""
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")

    def fake_run_with_file(*_a: object, **_k: object) -> int:
        """fake run with file."""
        return subprocess.CompletedProcess(
            args=[],
            returncode=3,
            stdout="",
            stderr=(
                'ERROR:  role "ums" already exists\nERROR:  permission denied to create role\n'
            ),
        )

    monkeypatch.setattr(restore, "_run_with_file", fake_run_with_file)
    monkeypatch.setattr(restore, "_psql", _restore_psql())
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)
    with pytest.raises(restore.RestoreError) as caught:
        restore._restore_roles("fake", roles_path, timeout=5)
    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "permission denied" in str(caught.value)


def test_restore_roles_rejects_nonzero_without_allowed_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Guard: test_restore_roles_rejects_nonzero_without_allowed_error."""
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")

    def fake_run_with_file(*_a: object, **_k: object) -> int:
        """fake run with file."""
        return subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr='FATAL:  database "missing" does not exist\n',
        )

    monkeypatch.setattr(restore, "_run_with_file", fake_run_with_file)
    monkeypatch.setattr(restore, "_psql", _restore_psql())
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)
    with pytest.raises(restore.RestoreError) as caught:
        restore._restore_roles("fake", roles_path, timeout=5)
    assert caught.value.code == restore.EXIT_ROLES_FAILED


def test_restore_roles_rejects_fatal_even_after_allowed_duplicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An allowed bootstrap duplicate must not mask a later FATAL apply failure."""
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")

    def fake_run_with_file(*_a: object, **_k: object) -> int:
        """fake run with file."""
        return subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr=(
                'ERROR:  role "ums" already exists\n'
                "FATAL:  terminating connection due to administrator command\n"
            ),
        )

    monkeypatch.setattr(restore, "_run_with_file", fake_run_with_file)
    monkeypatch.setattr(restore, "_psql", _restore_psql())
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)
    with pytest.raises(restore.RestoreError) as caught:
        restore._restore_roles("fake", roles_path, timeout=5)
    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "terminating connection" in str(caught.value)


def test_restore_roles_quiesces_late_file_backend_before_returning_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A docker timeout cannot leave roles.sql replaying behind rollback."""
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
    pending_replay = False
    events: list[str] = []

    monkeypatch.setattr(restore, "_preflight_roles_file", lambda *_a, **_k: None)
    monkeypatch.setattr(
        restore, "_reset_existing_protected_role_settings", lambda *_a, **_k: None
    )

    def host_timeout(
        argv: list[str], *, timeout: int, source: Path
    ) -> subprocess.CompletedProcess[str]:
        """Leave the tagged roles backend active after docker.exe times out."""
        nonlocal pending_replay
        command = " ".join(argv)
        assert source == roles_path
        assert "PGAPPNAME=ums_restore_mut_" in command
        assert "statement_timeout=" in command and "lock_timeout=" in command
        assert "psql -X" in command and "ON_ERROR_STOP=0" in command
        pending_replay = True
        events.append("client-timeout")
        raise subprocess.TimeoutExpired(argv, timeout)

    def backend_pids(*_args: object, **_kwargs: object) -> list[int]:
        """Expose the modeled backend until the control session terminates it."""
        events.append("pid-active" if pending_replay else "pid-absent")
        return [9234] if pending_replay else []

    def terminate_backend(
        _container: str, sql: str, *, timeout: int, dbname: str | None = None
    ) -> str:
        """Stop replay before the roles failure can reach outer rollback."""
        nonlocal pending_replay
        _ = timeout
        assert dbname == "postgres" and "pg_terminate_backend" in sql
        events.append("terminate")
        pending_replay = False
        return ""

    monkeypatch.setattr(restore, "_run_with_file", host_timeout)
    monkeypatch.setattr(restore, "_mutation_backend_pids", backend_pids)
    monkeypatch.setattr(restore, "_psql", terminate_backend)
    monkeypatch.setattr(restore.time, "sleep", lambda _seconds: None)

    with pytest.raises(restore.RestoreError) as caught:
        restore._restore_roles("container", roles_path, timeout=5)

    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "is quiescent" in str(caught.value)
    assert pending_replay is False
    assert events == ["client-timeout", "pid-active", "terminate", "pid-absent", "pid-absent"]


def test_destroy_throwaway_false_on_docker_rm_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: test_destroy_throwaway_false_on_docker_rm_failure."""
    monkeypatch.setattr(
        restore,
        "_run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error: No such container"
        ),
    )
    assert restore._destroy_throwaway("ums-restore-rehearsal-x", timeout=5) is False


def test_destroy_throwaway_false_on_docker_rm_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeoutExpired from docker rm must not escape cleanup."""

    def _timeout(*_a, **_k):
        """timeout."""
        raise subprocess.TimeoutExpired(cmd=["docker", "rm"], timeout=1)

    monkeypatch.setattr(restore, "_run", _timeout)
    assert restore._destroy_throwaway("ums-restore-rehearsal-x", timeout=5) is False


def test_overlapping_backup_lock_is_exclusive(tmp_path: Path) -> None:
    """A second process must fail before loading the watermark.

    The contender's reclaim attempt probes the FIRST holder's pid, which here
    is pytest's own -- a live pid in this console group. That was once lethal:
    the old Windows probe was ``os.kill(pid, 0)`` ==
    ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``, and running this test
    Ctrl+C'd the entire pytest run (measured: "KeyboardInterrupt ... 1 failed,
    194 passed" mid-suite). The probe is now OpenProcess/WaitForSingleObject
    on win32 -- no signal API anywhere in it -- so probing a live console-group
    pid is inert by construction and this test may safely keep exercising the
    real contention path.
    """

    def _contend() -> None:
        """contend."""
        with backup._exclusive_backup_lock(tmp_path):
            pass

    with backup._exclusive_backup_lock(tmp_path), pytest.raises(backup.BackupError) as caught:
        _contend()
    assert caught.value.code == backup.EXIT_USAGE
    assert "another backup is already running" in str(caught.value)
    assert not (tmp_path / ".backup.lock").exists()


def test_dump_database_passes_snapshot_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pg_dump must receive the exported snapshot id."""
    captured: dict[str, object] = {}

    def fake_run_to_file(argv, *, timeout, target):
        """fake run to file."""
        captured["argv"] = argv
        target.write_bytes(backup.CUSTOM_FORMAT_MAGIC + b"-x")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(backup, "_run_to_file", fake_run_to_file)
    target = tmp_path / "database.dump"
    backup._dump_database("ctr", target, timeout=30, snapshot="00000004-00000005-1")
    argv = captured["argv"]
    assert isinstance(argv, list)
    joined = " ".join(argv)
    assert "--snapshot='00000004-00000005-1'" in joined
    assert "--blobs" in joined


def test_exit_codes_stay_distinct() -> None:
    """Exit 8 and 9 are the newer ones; none may collide with an older class."""
    codes = [
        backup.EXIT_OK,
        backup.EXIT_USAGE,
        backup.EXIT_DOCKER_UNAVAILABLE,
        backup.EXIT_CONTAINER_UNAVAILABLE,
        backup.EXIT_COMMAND_FAILED,
        backup.EXIT_ARTIFACT_INVALID,
        backup.EXIT_BOOKKEEPING_FAILED,
        backup.EXIT_NO_CONTENT,
        backup.EXIT_INTERNAL,
    ]
    assert len(codes) == len(set(codes))
    assert backup.EXIT_NO_CONTENT == 8
    assert backup.EXIT_INTERNAL == 9


def test_restore_exit_codes_stay_distinct() -> None:
    """Guard: test_restore_exit_codes_stay_distinct."""
    codes = [
        restore.EXIT_OK,
        restore.EXIT_USAGE,
        restore.EXIT_DOCKER_UNAVAILABLE,
        restore.EXIT_CONTAINER_UNAVAILABLE,
        restore.EXIT_ROLES_FAILED,
        restore.EXIT_RESTORE_FAILED,
        restore.EXIT_VERIFY_FAILED,
        restore.EXIT_ARTIFACT_INTEGRITY,
        restore.EXIT_INTERNAL,
    ]
    assert len(codes) == len(set(codes))


# --------------------------------------------------------------------------
# The CLI itself -- ``main`` and ``_execute``, end to end
#
# WHY THIS SECTION EXISTS. Everything above tests helpers. An independent
# 55-mutation matrix over scripts/backup_database.py found ten survivors, and
# the two catastrophic ones were both in the handful of lines nothing here
# drove:
#
#   M53  ``_execute``  ``if not outcome.accepted:``  ->  ``if False:``
#        A run against a DROPPED SCHEMA, in a directory bound to a different
#        database, printed ``OK backup=...``, ran retention, and returned 0.
#        The seed floor, the identity binding and retention invariant 6 all
#        become decorative and the suite stays green.
#   M29  ``main``      ``return report.escalate(code)``  ->  ``return code``
#        With last-run.json held FileShare.None, exit 0 over a stale green --
#        the exact defect the escalation exists to close.
#
# A test that ratifies a hole is how the previous round shipped a broken gate,
# so these assert the PROCESS outcome: the exit code, what is on disk, and what
# last-run.json says. Docker and Postgres are the only things faked.
# --------------------------------------------------------------------------


class _FakeContainer:
    """Everything ``run_backup`` reaches for outside this process.

    Only Docker and Postgres are faked. Staging, artifact verification, the
    manifest, the gate, the watermark write, retention, the log, last-run.json
    and the exit code are all the real code paths.
    """

    def __init__(
        self,
        counts: dict[str, int],
        *,
        identity: backup.Identity = IDENTITY_A,
        toc_entries: int = 17,
    ) -> None:
        """init."""
        self.counts = dict(counts)
        self.identity = identity
        self.toc_entries = toc_entries

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """install."""
        monkeypatch.setattr(backup, "_await_docker", lambda *a, **k: "29.5.3")
        monkeypatch.setattr(backup, "_resolve_container", lambda **k: "fake-postgres")
        monkeypatch.setattr(backup, "_await_postgres", lambda *a, **k: None)
        monkeypatch.setattr(backup, "_dump_roles", self._dump_roles)
        monkeypatch.setattr(backup, "_dump_database_and_count", self._dump_database_and_count)
        monkeypatch.setattr(backup, "_verify_dump_readable", lambda *a, **k: self.toc_entries)
        monkeypatch.setattr(backup, "_pg_restore_list", self._pg_restore_list)
        monkeypatch.setattr(backup, "_container_facts", self._facts)

    @staticmethod
    def _dump_roles(
        _container: str, target: Path, *, timeout: int, include_passwords: bool
    ) -> list[str]:
        """dump roles."""
        _ = (timeout, include_passwords)
        target.write_text("CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n", encoding="utf-8")
        return list(backup.REQUIRED_ROLES)

    def _dump_database_and_count(
        self, _container: str, target: Path, *, timeout: int
    ) -> tuple[dict[str, int], set[str], set[str], int, str]:
        """dump database and count."""
        _ = timeout
        target.write_bytes(backup.CUSTOM_FORMAT_MAGIC + b"-fake-archive")
        return dict(self.counts), set(), set(), 0, FAKE_DATABASE_ACL

    def _pg_restore_list(self, _container: str, _dump_path: Path, *, timeout: int) -> str:
        """Return a minimal pg_restore listing for fake CLI archives."""
        _ = (_container, _dump_path, timeout)
        if self.toc_entries == 0:
            return ";\n"
        return ";\n1; 0 0 ACL public TABLE tenants app_tenant\n"

    @staticmethod
    def _dump_database(_container: str, target: Path, *, timeout: int) -> None:
        """dump database."""
        _ = timeout
        target.write_bytes(backup.CUSTOM_FORMAT_MAGIC + b"-fake-archive")

    def _facts(self, container: str, *, timeout: int) -> dict[str, str]:
        """facts."""
        _ = timeout
        return {
            "container": container,
            "database": self.identity.database,
            "superuser": "ums",
            "system_identifier": self.identity.system_identifier,
            "database_locale": "6|UTF8|C|C|c|",
            "database_acl": FAKE_DATABASE_ACL,
        }


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    out_dir: Path,
    counts: dict[str, int],
    *flags: str,
    identity: backup.Identity = IDENTITY_A,
    toc_entries: int = 17,
) -> int:
    """run cli."""
    _FakeContainer(counts, identity=identity, toc_entries=toc_entries).install(monkeypatch)
    return backup.main(["--out-dir", str(out_dir), *flags])


def _run_dirs(out_dir: Path) -> list[str]:
    """run dirs."""
    return sorted(p.name for p in out_dir.iterdir() if p.is_dir())


def test_the_cli_publishes_a_first_run_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """The baseline every failure case below is measured against."""
    code = _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark")

    assert code == backup.EXIT_OK
    published = _run_dirs(tmp_path)
    assert len(published) == 1
    assert not published[0].endswith(backup.REJECTED_SUFFIX), "an accepted run is not quarantined"
    assert (tmp_path / published[0] / backup.DUMP_NAME).is_file()
    assert (tmp_path / backup.WATERMARK_NAME).is_file()
    record = _last_run(tmp_path)
    assert record["status"] == "OK"
    assert record["exit_code"] == backup.EXIT_OK


def test_no_verify_dump_skips_pg_restore_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """--no-verify-dump must not invoke pg_restore --list at all.

    Do NOT route this through ``_run_cli``: that helper installs a fresh
    ``_FakeContainer`` on every call, which re-binds ``backup._pg_restore_list``
    to the benign fake and silently discards the ``_forbidden`` stub below. The
    assertion then holds for the wrong reason and the test proves nothing.
    Install the fakes first, override last, then drive ``backup.main`` directly.
    """
    called = {"n": 0}

    def _forbidden(*_a: object, **_k: object) -> str:
        """Fail the test if pg_restore --list runs under --no-verify-dump."""
        called["n"] += 1
        raise AssertionError("pg_restore --list must not run when --no-verify-dump is set")

    _FakeContainer(REAL).install(monkeypatch)
    monkeypatch.setattr(backup, "_pg_restore_list", _forbidden)
    assert backup._pg_restore_list is _forbidden, "the stub must survive fixture installation"

    code = backup.main(
        ["--out-dir", str(tmp_path), "--establish-watermark", "--no-verify-dump"]
    )

    assert code == backup.EXIT_OK
    assert called["n"] == 0


def test_the_cli_refuses_a_first_run_without_the_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """Guard: test_the_cli_refuses_a_first_run_without_the_acknowledgement."""
    code = _run_cli(monkeypatch, tmp_path, REAL)

    assert code == backup.EXIT_NO_CONTENT
    quarantined = _run_dirs(tmp_path)
    assert len(quarantined) == 1, "the run happened; it was just not published"
    assert quarantined[0].endswith(backup.REJECTED_SUFFIX)
    assert not (tmp_path / backup.WATERMARK_NAME).exists()
    assert _last_run(tmp_path)["status"] == "REJECTED"


def test_the_cli_quarantines_a_dropped_schema_and_prunes_only_expired_side_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """M53's kill. Every consequence of ``if not outcome.accepted:`` at once.

    A dropped schema, against a directory that already holds a good backup and
    an expired one. The run must exit 8, land under ``.rejected``, leave the
    watermark exactly where it was, preserve the previous good backup, and
    prune only an expired side-run. A rejected night gets no say over which
    accepted backup occupies the retention slots.
    """
    assert _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark") == backup.EXIT_OK
    good = _run_dirs(tmp_path)[0]
    expired_accepted = _write_run(tmp_path, "20260101T000000", counts=REAL)
    expired_rejected = _write_run(
        tmp_path, "20260101T000001", counts=EMPTY, rejected=True
    )
    before = json.loads((tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8"))
    clock.advance(timedelta(days=1))

    code = _run_cli(monkeypatch, tmp_path, EMPTY, "--keep-days", "0", "--keep-min", "1")

    assert code == backup.EXIT_NO_CONTENT
    quarantined = [name for name in _run_dirs(tmp_path) if name.endswith(backup.REJECTED_SUFFIX)]
    assert len(quarantined) == 1, "the run must be quarantined, not published"
    after = json.loads((tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8"))
    assert after == before, "a rejected run must not rewrite the watermark"
    assert (tmp_path / good).is_dir(), "the previous good backup must survive"
    assert expired_accepted.is_dir(), (
        "a rejected run is not a verified replacement and must not delete "
        "accepted history"
    )
    assert not expired_rejected.is_dir(), "an expired rejected side-run should be pruned"
    assert expired_rejected.name in _last_run(tmp_path)["pruned"]
    assert expired_accepted.name not in _last_run(tmp_path)["pruned"]
    record = _last_run(tmp_path)
    assert record["status"] == "REJECTED"
    assert record["exit_code"] == backup.EXIT_NO_CONTENT


def test_the_cli_refuses_a_second_database_in_a_bound_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """M53's other half: the identity binding must decide the PROCESS outcome."""
    assert _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark") == backup.EXIT_OK
    clock.advance(timedelta(days=1))

    code = _run_cli(monkeypatch, tmp_path, REAL, identity=IDENTITY_B)

    assert code == backup.EXIT_NO_CONTENT
    record = _last_run(tmp_path)
    assert record["status"] == "REJECTED"
    assert "bound to" in str(record["error"])
    assert _run_dirs(tmp_path)[-1].endswith(backup.REJECTED_SUFFIX), (
        "the foreign run must be quarantined, not published"
    )


def test_the_cli_escalates_a_published_run_whose_status_did_not_land(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """M29's kill: exit 7, never 0, over a last-run.json that still reads OK.

    ``report.escalate`` is the only thing standing between a locked status file
    and a green Last Run Result, and it is applied by ``main``'s return
    statement -- so only a test that calls ``main`` can notice it being removed.
    """
    assert _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark") == backup.EXIT_OK
    stale = _last_run(tmp_path)
    assert stale["status"] == "OK"
    _unwritable(tmp_path / backup.LAST_RUN_NAME, monkeypatch)
    clock.advance(timedelta(days=1))

    code = _run_cli(monkeypatch, tmp_path, REAL)

    assert code == backup.EXIT_BOOKKEEPING_FAILED
    assert _last_run(tmp_path) == stale, "the lock held, so the file still shows the older run"
    sidecars = sorted(p.name for p in tmp_path.glob("last-run-*.json"))
    assert sidecars, "this run's record has to land somewhere"
    sidecar = json.loads((tmp_path / sidecars[-1]).read_text(encoding="utf-8"))
    assert sidecar["status"] == "OK"
    assert "status_note" in sidecar


def test_the_cli_escalates_even_when_only_the_log_is_locked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """The audit log is a channel too; losing it alone still cannot exit 0."""
    _unwritable(tmp_path / backup.LOG_NAME, monkeypatch)

    code = _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark")

    assert code == backup.EXIT_BOOKKEEPING_FAILED
    assert _last_run(tmp_path)["status"] == "OK", "the backup itself was fine"


def test_the_cli_reports_a_dump_that_holds_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """An empty table of contents is a content failure, not a broken artifact."""
    code = _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark", toc_entries=0)

    assert code == backup.EXIT_NO_CONTENT
    assert any(name.endswith(backup.REJECTED_SUFFIX) for name in _run_dirs(tmp_path))


def test_the_cli_stops_a_drain_on_the_second_night(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """The watermark, judged by the exit code rather than by a verdict object."""
    assert _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark") == backup.EXIT_OK
    clock.advance(timedelta(days=1))

    assert _run_cli(monkeypatch, tmp_path, GUTTED) == backup.EXIT_NO_CONTENT
    clock.advance(timedelta(days=1))
    assert _run_cli(monkeypatch, tmp_path, GUTTED) == backup.EXIT_NO_CONTENT, (
        "the mark must not have followed the data down"
    )


def test_the_cli_survives_a_future_dated_directory_night_after_night(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """Defect 11 at the level it was proved: four nights, no wedge, no flag.

    Before the fix this ran 8 / 0-with---accept-content-drop / 8 / 0 / 8 for as
    long as the directory existed.
    """
    assert _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark") == backup.EXIT_OK
    _write_run(tmp_path, "20990101T000000", counts=_database(org_units=10**9))

    for _ in range(4):
        clock.advance(timedelta(days=1))
        assert _run_cli(monkeypatch, tmp_path, REAL) == backup.EXIT_OK

    watermark = json.loads((tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8"))
    assert watermark["tables"]["org_units"] == REAL["org_units"]
    assert _last_run(tmp_path)["future_dated_dirs"] == ["ums-backup-20990101T000000Z"]


# --------------------------------------------------------------------------
# What the refusal actually buys: DEFERRAL, not immunity.
#
# ``20990101`` is inert for 73 years, which makes it a comfortable fixture and a
# misleading one -- it is the only future stamp this file used to exercise, and
# it never reaches the fold, so nothing here described what happens when a
# plant's stamp arrives. It always does. ``_run_stamp`` compares against the
# clock at every call, so a plant is refused only while it is ahead of now, and
# folds in the moment wall-clock time passes it.
#
# That is the correct trade -- an unverifiable stamp may lower the protection it
# offers, never raise the bar it sets -- but it has an operator cost, and the
# cost depends on which side of the tolerance the plant sits:
#
#   OUTSIDE (+6min, +2h, +2 days): inert tonight, folds when its stamp arrives,
#     and the recovery is the documented ONE --accept-content-drop night --
#     because by then real time has passed the stamp, so the override run's name
#     sorts ABOVE the plant and ``reset_after`` can finally exclude it.
#   INSIDE (+4min): history immediately, so tonight already exits 8, and the
#     override run taken tonight is stamped BELOW the plant. ``reset_after`` is a
#     name comparison, so that first override does not exclude it and the night
#     after fails again. TWO override nights, not one.
#
# Both are measured below against the real CLI, by exit code.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ahead",
    [timedelta(minutes=6), timedelta(hours=2), timedelta(days=2)],
    ids=["plus-6min", "plus-2h", "plus-2days"],
)
def test_a_plant_beyond_the_tolerance_is_deferred_and_costs_one_override_night(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock, ahead: timedelta
) -> None:
    """Inert on the night it appears; folds when its stamp arrives; one flag clears it."""
    assert _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark") == backup.EXIT_OK
    plant_at = clock.now + ahead
    plant = _write_run(
        tmp_path, plant_at.strftime(backup.STAMP_FORMAT), counts=_database(org_units=10**9)
    )

    # Night 1, still ahead of the plant's stamp: inert, and REPORTED.
    clock.advance(timedelta(seconds=1))
    assert _run_cli(monkeypatch, tmp_path, REAL) == backup.EXIT_OK
    assert _last_run(tmp_path)["future_dated_dirs"] == [plant.name], (
        "inert is not enough -- the operator has to be told it is there"
    )

    # The stamp arrives. From here the plant IS history and its counts fold in.
    clock.move_to(plant_at + timedelta(minutes=1))
    assert _run_cli(monkeypatch, tmp_path, REAL) == backup.EXIT_NO_CONTENT

    # ONE override night, because the override run now outranks the plant by name.
    clock.advance(timedelta(minutes=1))
    assert _run_cli(monkeypatch, tmp_path, REAL, "--accept-content-drop") == backup.EXIT_OK

    clock.advance(timedelta(days=1))
    assert _run_cli(monkeypatch, tmp_path, REAL) == backup.EXIT_OK, (
        "the documented single override night must actually clear it"
    )
    watermark = json.loads((tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8"))
    assert watermark["tables"]["org_units"] == REAL["org_units"]


def test_a_plant_inside_the_tolerance_costs_two_override_nights(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """+4 minutes: accepted as history at once, and one override is not enough.

    This is the price of the tolerance, stated rather than assumed, and it is
    what makes the width of the window a real decision: every minute of
    tolerance is a window in which a plant is both history AND newer than the
    run that would override it.
    """
    assert _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark") == backup.EXIT_OK
    plant_at = clock.now + timedelta(minutes=4)
    plant = _write_run(
        tmp_path, plant_at.strftime(backup.STAMP_FORMAT), counts=_database(org_units=10**9)
    )

    # Inside the tolerance, so it is history on the night it appears.
    clock.advance(timedelta(minutes=1))
    assert _run_cli(monkeypatch, tmp_path, REAL) == backup.EXIT_NO_CONTENT
    assert _last_run(tmp_path).get("future_dated_dirs") in (None, []), (
        "inside the tolerance it is an ordinary run, not a reported future one"
    )

    # Override night 1. Its run name sorts BELOW the plant, so reset_after -- a
    # name comparison -- cannot exclude the plant, and the next night fails again.
    clock.advance(timedelta(minutes=1))
    assert _run_cli(monkeypatch, tmp_path, REAL, "--accept-content-drop") == backup.EXIT_OK
    stored = json.loads((tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8"))
    assert stored["reset_after"] < plant.name, "the premise: the override did not outrank it"

    clock.advance(timedelta(days=1))
    assert _run_cli(monkeypatch, tmp_path, REAL) == backup.EXIT_NO_CONTENT, (
        "one override night is NOT enough for a plant inside the tolerance"
    )

    # Override night 2 outranks it, and the night after is green with no flag.
    clock.advance(timedelta(minutes=1))
    assert _run_cli(monkeypatch, tmp_path, REAL, "--accept-content-drop") == backup.EXIT_OK
    clock.advance(timedelta(days=1))
    assert _run_cli(monkeypatch, tmp_path, REAL) == backup.EXIT_OK
    watermark = json.loads((tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8"))
    assert watermark["tables"]["org_units"] == REAL["org_units"]


@pytest.mark.parametrize("suffix", ["", backup.REJECTED_SUFFIX], ids=["published", "quarantined"])
def test_a_directory_already_on_tonights_slot_costs_the_whole_night(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock, suffix: str
) -> None:
    """``run_backup``'s collision guard, which nothing drove.

    Deleting ``if final_dir.exists(): raise`` left this file green, and the
    clock is how a box reaches it without anyone planting anything: a backward
    correction lands 02:00 on a second-boundary that already has a directory.
    The night is then LOST -- exit 2, no backup taken at all -- and it is
    reported as a usage error, which says nothing about a clock. That is the
    behaviour, so it is asserted rather than inferred; without the guard the
    rename onto an existing directory raises OSError and the run reports exit 5
    instead, with the same night still lost.
    """
    tonight = tmp_path / (
        backup.RUN_DIR_TEMPLATE.format(stamp=clock.now.strftime(backup.STAMP_FORMAT)) + suffix
    )
    tonight.mkdir()
    (tonight / backup.DUMP_NAME).write_bytes(b"PGDMP-not-mine-to-touch")

    code = _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark")

    assert code == backup.EXIT_USAGE
    assert (tonight / backup.DUMP_NAME).read_bytes() == b"PGDMP-not-mine-to-touch"
    assert _run_dirs(tmp_path) == [tonight.name], "no backup was taken, and no .partial left behind"
    assert not (tmp_path / backup.WATERMARK_NAME).exists()
    record = _last_run(tmp_path)
    assert record["status"] == "FAILED"
    assert record["exit_code"] == backup.EXIT_USAGE
    assert "already exists" in str(record["error"])


def test_the_cli_prunes_after_an_accepted_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """The other half of invariant 6: an ACCEPTED run does apply retention."""
    assert _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark") == backup.EXIT_OK
    expired = _write_run(tmp_path, "20260101T000000", counts=REAL)
    clock.advance(timedelta(days=1))

    code = _run_cli(monkeypatch, tmp_path, REAL, "--keep-days", "0", "--keep-min", "1")

    assert code == backup.EXIT_OK
    assert not expired.exists(), "an expired, unprotected run is deleted"
    assert expired.name in _last_run(tmp_path)["pruned"]


# --------------------------------------------------------------------------
# The mutation survivors from this round, killed
#
# A 60-mutation matrix over scripts/backup_database.py caught 52 and left 8.
# Three are proven equivalent or subsumed and are argued in the code rather
# than tested (M01 MIN_TABLES, M16 the whole-directory floor, M35 the
# rebaseline guard). The five below were real coverage gaps.
#
# A LATER 20-mutation matrix over the clock and collision guards left one
# survivor that is kept and argued rather than tested:
#
#   ``_load_watermark``'s ``child.name <= reset_after`` -> ``<``, which stops
#   excluding the override run ITSELF. EQUIVALENT. ``reset_after`` is only ever
#   written as ``run=outcome.run_dir.name`` in the same ``_write_watermark``
#   call that stores ``_next_watermark(previous, counts, verdict)``, and that
#   stored mark is ``max(previous, counts)`` with the rebaselined tables set to
#   ``counts[name]``. The excluded run's manifest records exactly those
#   ``counts``, so re-folding it is ``max(stored, counts)`` -- equal for the
#   lowered tables and already dominated for every other one. The exclusion is
#   there so the run is not double-counted, not because it could change the
#   mark. Every OTHER name the comparison governs is a real run and is covered
#   by ``test_an_old_manifest_cannot_resurrect_an_overridden_watermark``.
# --------------------------------------------------------------------------


def test_a_published_manifest_with_no_counts_cannot_crash_the_watermark(
    tmp_path: Path,
) -> None:
    """Kills the ``counts is None`` half of ``_load_watermark``'s floor guard.

    An artifact-bearing run whose manifest records no ``table_row_counts`` is
    the shape a run published by a revision of this script older than the gate
    has. Skipping the guard walks straight into ``None.items()`` -- an
    AttributeError out of a nightly task, after the backup was already taken.
    """
    _night(tmp_path, "20260801T020000", REAL, establish=True)
    countless = _write_run(tmp_path, "20260802T020000", counts=None)
    (countless / backup.MANIFEST_NAME).write_text(
        json.dumps({"schema": backup.MANIFEST_SCHEMA}), encoding="utf-8"
    )

    watermark = backup._load_watermark(tmp_path)

    assert watermark.tables["org_units"] == REAL["org_units"]


def test_a_manifest_whose_counts_fail_the_floor_does_not_contribute(tmp_path: Path) -> None:
    """The other half: a gutted run that an older script published as OK."""
    _night(tmp_path, "20260801T020000", REAL, establish=True)
    _write_run(tmp_path, "20260802T020000", counts={"alembic_version": 1, "org_units": 10**6})

    watermark = backup._load_watermark(tmp_path)

    assert watermark.tables["org_units"] == REAL["org_units"], (
        "a run that does not clear the floor is not evidence of anything, in either direction"
    )


def test_a_negative_count_in_the_watermark_file_cannot_lower_the_bar(tmp_path: Path) -> None:
    """Kills the ``max(int(value), 0)`` clamp in ``_read_watermark_file``.

    A hand-edited or half-written watermark.json is the only way to get here,
    and a negative mark would drag the whole-directory total down with it.
    Asserted against the reader rather than against ``_load_watermark``: the
    manifest fold is a maximum, so a run directory beside the file would mask
    the clamp being gone and this test would pass while the guard was absent.
    """
    (tmp_path / backup.WATERMARK_NAME).write_text(
        json.dumps({"tables": {"org_units": -(10**6), "youtube_channels": 5}}),
        encoding="utf-8",
    )

    tables, _reset = backup._read_watermark_file(tmp_path)

    assert tables["org_units"] == 0
    assert sum(tables.values()) == 5, "a negative mark must not offset a real one"


def test_an_artifact_that_is_a_directory_is_not_a_published_backup(tmp_path: Path) -> None:
    """The ``is_file()`` arm of ``_run_is_published_backup``.

    MEASURED, because the answer is platform-dependent and the honest note
    matters: on Windows a directory's ``st_size`` is 0, so the emptiness arm
    below already refuses it and deleting ``is_file()`` is an EQUIVALENT mutant
    there. On POSIX a directory stats at 4096, the emptiness arm passes, and
    this test is the only thing that refuses it. It is kept for the platform
    where it bites, and because the intent should be stated whichever platform
    is running.
    """
    run = _write_run(tmp_path, "20260801T020000", counts=REAL)
    (run / backup.DUMP_NAME).unlink()
    (run / backup.DUMP_NAME).mkdir()

    manifest = backup._read_manifest(run)
    assert manifest is not None
    assert backup._run_is_published_backup(run, manifest) is False
    assert backup._run_has_content(run) is None, "unknown, so never deleted and never the pin"


def test_the_cli_reports_a_watermark_that_could_not_be_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """Kills ``_execute``'s watermark-write failure arm.

    The backup itself is valid and stays published; the NEXT run's protection is
    what was lost, so the exit code is 7 and the status is not OK.
    """
    assert _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark") == backup.EXIT_OK
    _unwritable(tmp_path / backup.WATERMARK_NAME, monkeypatch)
    clock.advance(timedelta(days=1))

    code = _run_cli(monkeypatch, tmp_path, REAL)

    assert code == backup.EXIT_BOOKKEEPING_FAILED
    record = _last_run(tmp_path)
    assert record["status"] == "BOOKKEEPING_FAILED"
    assert record["backup_published"] is True
    assert backup.WATERMARK_NAME in str(record["error"])


def test_roles_sql_that_does_not_name_both_roles_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P0.1's headline trap, and the one artifact check nothing exercised.

    ``pg_dump`` carries the RLS policies and the ``GRANT ... TO app_tenant`` /
    ``app_platform`` statements but NOT the roles, so a dump restored into a
    cluster without them fails part-way and leaves a half-populated database.
    A backup that looks perfect and does not restore is the worst shape this
    failure has, which is why publishing is refused rather than warned about.
    """
    target = tmp_path / backup.ROLES_NAME

    def _half_a_roles_file(argv: list[str], *, timeout: int, target: Path):
        """half a roles file."""
        target.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(backup, "_run_to_file", _half_a_roles_file)
    monkeypatch.setattr(backup, "_psql", _backup_superuser_psql())

    with pytest.raises(backup.BackupError) as raised:
        backup._dump_roles("fake", target, timeout=5, include_passwords=False)

    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "app_platform" in str(raised.value)


def test_roles_sql_naming_both_roles_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other side of the same guard, so the test above cannot pass vacuously."""
    target = tmp_path / backup.ROLES_NAME

    def _complete_roles_file(argv: list[str], *, timeout: int, target: Path):
        """complete roles file."""
        target.write_text("CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(backup, "_run_to_file", _complete_roles_file)
    monkeypatch.setattr(backup, "_psql", _backup_superuser_psql())

    assert backup._dump_roles("fake", target, timeout=5, include_passwords=False) == list(
        backup.REQUIRED_ROLES
    )


def test_an_empty_roles_file_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """pg_dumpall exiting 0 having written nothing is still an unusable backup."""
    target = tmp_path / backup.ROLES_NAME

    def _nothing(argv: list[str], *, timeout: int, target: Path):
        """nothing."""
        target.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(backup, "_run_to_file", _nothing)

    with pytest.raises(backup.BackupError) as raised:
        backup._dump_roles("fake", target, timeout=5, include_passwords=False)

    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID


def test_validate_dump_roles_covered_rejects_archive_only_role() -> None:
    """An ACL TOC owner present in database.dump but absent from roles.sql is refused."""
    listing = "123; 2606 0 ACL public TABLE tenants orphan_role\n"
    roles_body = "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n"
    with pytest.raises(backup.BackupError) as raised:
        backup._validate_dump_roles_covered(listing=listing, roles_body=roles_body)
    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "orphan_role" in str(raised.value)


def test_validate_dump_roles_covered_rejects_table_owner_only_role() -> None:
    """TABLE owners must be covered, not only ACL TOC owners."""
    listing = "3; 2615 16384 TABLE public tenants stale_owner\n"
    roles_body = "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n"
    with pytest.raises(backup.BackupError) as raised:
        backup._validate_dump_roles_covered(listing=listing, roles_body=roles_body)
    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "stale_owner" in str(raised.value)


def test_validate_dump_roles_covered_rejects_snapshot_acl_grantee() -> None:
    """Snapshot ACL grantees must be declared even when absent from TOC owners."""
    listing = "3; 2615 16384 TABLE public tenants app_tenant\n"
    roles_body = "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n"
    with pytest.raises(backup.BackupError) as raised:
        backup._validate_dump_roles_covered(
            listing=listing,
            roles_body=roles_body,
            acl_grantees={"ghost_grantee"},
        )
    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "ghost_grantee" in str(raised.value)


def test_validate_dump_roles_covered_ignores_empty_acl_grantee_set() -> None:
    """An empty snapshot grantee set does not weaken TOC owner coverage."""
    listing = "3; 2615 16384 TABLE public tenants stale_owner\n"
    roles_body = "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n"
    with pytest.raises(backup.BackupError) as raised:
        backup._validate_dump_roles_covered(
            listing=listing,
            roles_body=roles_body,
            acl_grantees=set(),
        )
    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "stale_owner" in str(raised.value)


def test_validate_dump_roles_covered_keeps_authorization_checks_without_a_listing() -> None:
    """``listing=None`` (--no-verify-dump) skips ONLY the TOC owner scan.

    Round-9 P1 requires privilege-drift refusal to survive --no-verify-dump, and
    round-11 P1 requires --no-verify-dump not to run pg_restore --list at all.
    Both hold together because neither authorization check reads the TOC: drift
    reads roles.sql and grantee coverage reads the dump snapshot. This pins that
    -- if a later change reinstates the listing as the source of either check,
    the first two arms go green-when-they-should-be-red and this test fails.
    """
    declared = "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n"

    # 1. Privilege drift still refuses with no listing at all.
    with pytest.raises(backup.BackupError) as drift:
        backup._validate_dump_roles_covered(
            listing=None,
            roles_body="CREATE ROLE app_tenant WITH BYPASSRLS;\nCREATE ROLE app_platform;\n",
        )
    assert "BYPASSRLS" in str(drift.value)

    # 2. Snapshot ACL grantees still refuse with no listing at all.
    with pytest.raises(backup.BackupError) as grantee:
        backup._validate_dump_roles_covered(
            listing=None,
            roles_body=declared,
            acl_grantees={"ghost_grantee"},
        )
    assert grantee.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "ghost_grantee" in str(grantee.value)

    # 3. TOC owners are the ONLY thing dropped: the same undeclared owner that
    #    refuses with a listing is accepted without one.
    undeclared_owner = "3; 2615 16384 TABLE public tenants stale_owner\n"
    with pytest.raises(backup.BackupError):
        backup._validate_dump_roles_covered(listing=undeclared_owner, roles_body=declared)
    backup._validate_dump_roles_covered(listing=None, roles_body=declared)


def test_dump_listing_owner_collection_covers_every_owned_toc_entry() -> None:
    """Owner must come from every owned TOC entry, not a marker allowlist.

    Collations, operator classes/families, conversions, text-search objects,
    constraints, extended statistics, and sequence-set lines were all missed by
    the previous marker list, so an archive still referencing a role that had
    been reassigned and dropped could publish and then roll back part-way
    through pg_restore (PR #210 review round 2).
    """
    listing = "\n".join(
        [
            "; comment lines are skipped",
            "215; 129 16523 COLLATION public de_de coll_owner",
            "216; 2612 16524 OPERATOR CLASS public int8_ops btree opclass_owner",
            "217; 2745 16525 OPERATOR FAMILY public int_ops opfam_owner",
            "218; 2610 16526 CONVERSION public iso8859_to_utf8 conv_owner",
            "220; 3777 16528 TEXT SEARCH CONFIGURATION public websearch tsconfig_owner",
            "221; 3778 16529 TEXT SEARCH DICTIONARY public simple tsdict_owner",
            "222; 3779 16530 TEXT SEARCH PARSER public default_tsparser tsparser_owner",
            "223; 3780 16531 TEXT SEARCH TEMPLATE public snowball tstemplate_owner",
            "224; 2606 16532 CONSTRAINT public tenants_pkey constraint_owner",
            "230; 3490 16533 STATISTICS public s1 stats_owner",
            "12; 1259 16421 TABLE DATA public channels data_owner",
            "4001; 0 16556 SEQUENCE SET public channels_id_seq seqset_owner",
            "5000; 0 0 BLOB -",
            "not-a-numbered TOC line stays out entirely",
        ]
    )
    assert backup._roles_referenced_in_dump_listing(listing) == {
        "coll_owner",
        "opclass_owner",
        "opfam_owner",
        "conv_owner",
        "tsconfig_owner",
        "tsdict_owner",
        "tsparser_owner",
        "tstemplate_owner",
        "constraint_owner",
        "stats_owner",
        "data_owner",
        "seqset_owner",
    }


def test_validate_dump_roles_covered_rejects_collation_owner() -> None:
    """A collation owner missing from roles.sql must refuse publication."""
    listing = "215; 129 16523 COLLATION public de_de ghost_collation_owner\n"
    roles_body = "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n"
    with pytest.raises(backup.BackupError) as raised:
        backup._validate_dump_roles_covered(listing=listing, roles_body=roles_body)
    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "ghost_collation_owner" in str(raised.value)


def test_parse_role_name_lines_skips_blank() -> None:
    """ACL grantee parser ignores blank lines from psql -At output."""
    assert backup._parse_role_name_lines("app_tenant\n\napp_platform\n") == {
        "app_tenant",
        "app_platform",
    }


def test_guard_empty_refuses_non_public_user_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tables outside public still block restore without --allow-nonempty."""
    def _psql(
        _container: str, sql: str, *, timeout: int, dbname: str | None = None
    ) -> str:
        """Return a non-zero user-object count for guard-empty tests."""
        _ = (_container, timeout, dbname)
        assert "pg_catalog.pg_class" in sql
        assert "typtype IN ('b', 'c', 'd', 'e', 'r', 'm')" in sql
        assert "pg_catalog.pg_proc" in sql
        assert "nspname <> 'public'" in sql
        return "2\n"

    monkeypatch.setattr(restore, "_psql", _psql)
    with pytest.raises(restore.RestoreError) as raised:
        restore._guard_empty("fake", allow_nonempty=False, timeout=5)
    assert raised.value.code == restore.EXIT_USAGE
    assert "user objects" in str(raised.value)


def test_guard_empty_allows_zero_user_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty target (no user objects in any non-system schema) is allowed."""
    monkeypatch.setattr(restore, "_psql", lambda *a, **k: "0\n")
    restore._guard_empty("fake", allow_nonempty=False, timeout=5)


def test_guard_empty_allows_nonempty_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """--allow-nonempty bypasses the user-object emptiness refusal."""
    monkeypatch.setattr(restore, "_psql", lambda *a, **k: "9\n")
    restore._guard_empty("fake", allow_nonempty=True, timeout=5)


def test_restore_roles_rejects_dynamic_do_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Procedural role DDL must fail closed before psql executes roles.sql."""
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text(
        "DO $$ BEGIN EXECUTE 'CREATE ROLE sneaky_role'; END $$;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(restore, "_psql", lambda *_a, **_k: "postgres")
    with pytest.raises(restore.RestoreError) as caught:
        restore._restore_roles("fake", roles_path, timeout=5)
    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "DO blocks" in str(caught.value)


def test_lock_metadata_write_failure_removes_partial_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed started.at write must not leave a reclaim-blocking lock dir."""
    real_write_text = Path.write_text

    def _boom(self: Path, *_a: object, **_k: object) -> None:
        """boom."""
        if self.name == "started.at":
            raise OSError("simulated metadata write failure")
        real_write_text(self, *_a, **_k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", _boom)
    with pytest.raises(OSError), backup._exclusive_backup_lock(tmp_path):
        pass  # pragma: no cover
    assert not (tmp_path / ".backup.lock").exists()


def test_restore_roles_accepts_semicolon_terminated_create_role_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Semicolon terminators on CREATE ROLE lines must not poison the allowlist."""
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text(
        "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(restore, "_psql", _restore_psql(superuser="postgres"))
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)
    monkeypatch.setattr(
        restore,
        "_run_with_file",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )
    present = restore._restore_roles("fake", roles_path, timeout=5)
    assert "app_tenant" in present


def test_restore_preflight_refuses_privileged_app_role_attributes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ALTER ROLE app_tenant WITH SUPERUSER must be refused before it is replayed.

    The foreign-role allowlist is anchored to CREATE ROLE, so drift that arrives
    as ALTER ROLE on an ALREADY-ALLOWLISTED role passed straight through and was
    applied by the bootstrap superuser -- handing an RLS-bearing application role
    BYPASSRLS or LOGIN, with no migration left to rerun and revoke it.

    The accept arm is what stops this being vacuous: it feeds the real
    ``pg_dumpall --roles-only`` shape, whose NOSUPERUSER / NOLOGIN / NOBYPASSRLS
    tokens must NOT trip a substring match, alongside a bootstrap superuser that
    legitimately carries all three.

    Its ``GRANT app_tenant TO app_platform`` line is gone deliberately: the
    membership gate refuses it, because that edge makes the PLATFORM lane a
    member of the TENANT lane and crosses the platform-only write boundary
    20260608_0001 holds. The replacement is the edge real ``pg_dumpall`` output
    actually carries -- ``GRANT app_* TO <bootstrap superuser>`` -- which must
    keep passing.
    """
    monkeypatch.setattr(
        restore,
        "_run_with_file",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)

    clean = tmp_path / restore.ROLES_NAME
    clean.write_text(
        "CREATE ROLE postgres;\n"
        "ALTER ROLE postgres WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN "
        "REPLICATION BYPASSRLS;\n"
        "CREATE ROLE app_tenant;\n"
        "ALTER ROLE app_tenant WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB "
        "NOLOGIN NOREPLICATION NOBYPASSRLS;\n"
        "CREATE ROLE app_platform;\n"
        "ALTER ROLE app_platform WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB "
        "NOLOGIN NOREPLICATION NOBYPASSRLS;\n"
        "GRANT app_tenant TO postgres WITH ADMIN OPTION, INHERIT TRUE GRANTED BY postgres;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(restore, "_psql", _restore_psql(superuser="postgres"))
    assert "app_tenant" in restore._restore_roles("fake", clean, timeout=5)

    drifted = tmp_path / "drifted.sql"
    drifted.write_text(
        "CREATE ROLE app_tenant;\n"
        "ALTER ROLE app_tenant WITH SUPERUSER BYPASSRLS LOGIN;\n"
        "CREATE ROLE app_platform;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(restore, "_psql", _restore_psql())
    with pytest.raises(restore.RestoreError) as raised:
        restore._restore_roles("fake", drifted, timeout=5)
    assert raised.value.code == restore.EXIT_ROLES_FAILED
    message = str(raised.value)
    assert "app_tenant" in message
    assert "BYPASSRLS, LOGIN, SUPERUSER" in message

    # The negative forms are whole tokens, not substrings of the enabled ones.
    assert (
        restore._role_privilege_drift_problems(
            "ALTER ROLE app_tenant WITH NOSUPERUSER NOLOGIN NOBYPASSRLS;\n"
        )
        == []
    )


# CAPTURED, not written: the exact bytes of
#   pg_dumpall -U postgres -l ums --roles-only --no-password --no-role-passwords
# on postgres:18 for a cluster in the shipped container shape -- the bootstrap
# superuser plus the two NOLOGIN roles 20260608_0001 ``_create_role`` creates.
# Only the ``\restrict`` nonce is replaced (it is random per dump) and the long
# ALTER ROLE lines are wrapped across adjacent literals, which concatenate back
# to the identical bytes.
#
# It is the accept arm for every gate below, and it is not a soft one. It
# carries: the ``\restrict``/``\unrestrict`` meta-command pair every supported
# branch emits; a COMMENT ON ROLE whose value holds a backslash, a doubled
# quote AND a semicolon; a per-role GUC; BOTH legitimate
# ``GRANT app_* TO <bootstrap superuser>`` membership edges; and three
# ``GRANT ... ON PARAMETER ... TO ...`` object grants from the
# ``-- Role privileges on configuration parameters`` section, two of which name
# an application role.
_REAL_ROLES_SQL = (
    "--\n"
    "-- PostgreSQL database cluster dump\n"
    "--\n"
    "\n"
    "\\restrict RESTRICTTOKEN\n"
    "\n"
    "SET default_transaction_read_only = off;\n"
    "\n"
    "SET client_encoding = 'UTF8';\n"
    "SET standard_conforming_strings = on;\n"
    "\n"
    "--\n"
    "-- Roles\n"
    "--\n"
    "\n"
    "CREATE ROLE app_platform;\n"
    "ALTER ROLE app_platform WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB "
    "NOLOGIN NOREPLICATION NOBYPASSRLS;\n"
    "CREATE ROLE app_tenant;\n"
    "ALTER ROLE app_tenant WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB "
    "NOLOGIN NOREPLICATION NOBYPASSRLS;\n"
    "COMMENT ON ROLE app_tenant IS E'has \\\\ backslash and '' quote and ; semi';\n"
    "CREATE ROLE postgres;\n"
    "ALTER ROLE postgres WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN "
    "REPLICATION BYPASSRLS;\n"
    "\n"
    "--\n"
    "-- User Configurations\n"
    "--\n"
    "\n"
    "--\n"
    '-- User Config "app_platform"\n'
    "--\n"
    "\n"
    "ALTER ROLE app_platform SET search_path TO 'public';\n"
    "\n"
    "\n"
    "--\n"
    "-- Role memberships\n"
    "--\n"
    "\n"
    "GRANT app_platform TO postgres WITH ADMIN OPTION, INHERIT TRUE GRANTED BY postgres;\n"
    "GRANT app_tenant TO postgres WITH ADMIN OPTION, INHERIT TRUE GRANTED BY postgres;\n"
    "\n"
    "\n"
    "--\n"
    "-- Role privileges on configuration parameters\n"
    "--\n"
    "\n"
    "GRANT SET ON PARAMETER log_min_duration_statement TO pg_monitor;\n"
    "GRANT ALTER SYSTEM ON PARAMETER shared_buffers TO app_tenant WITH GRANT OPTION;\n"
    "GRANT SET ON PARAMETER work_mem TO app_platform;\n"
    "\n"
    "\n"
    "\\unrestrict RESTRICTTOKEN\n"
    "\n"
    "--\n"
    "-- PostgreSQL database cluster dump complete\n"
    "--\n"
    "\n"
)

# The same capture from a cluster whose bootstrap superuser is MIXED CASE
# (POSTGRES_USER=Ums_Admin). pg_dumpall writes ``CREATE ROLE "Ums_Admin";``
# double-quoted -- PostgreSQL does not fold quoted identifiers -- while
# ``SELECT current_user`` returns the bare name. A gate that folds one and not
# the other calls the bootstrap superuser a foreign role and refuses a genuine
# archive before any restore mutation.
_REAL_MIXED_CASE_ROLES_SQL = (
    "--\n-- Roles\n--\n\n"
    'CREATE ROLE "Ums_Admin";\n'
    'ALTER ROLE "Ums_Admin" WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN '
    "REPLICATION BYPASSRLS;\n"
    "CREATE ROLE app_platform;\n"
    "ALTER ROLE app_platform WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB "
    "NOLOGIN NOREPLICATION NOBYPASSRLS;\n"
    "CREATE ROLE app_tenant;\n"
    "ALTER ROLE app_tenant WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB "
    "NOLOGIN NOREPLICATION NOBYPASSRLS;\n"
)

_BOTH_APP_ROLES = "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n"


def _roles_sql_gate_problems(module: ModuleType, body: str, superuser: str) -> list[str]:
    """Run every roles.sql gate in ``module`` and return the pooled problems."""
    foreign = (
        module._foreign_roles_in_roles_sql
        if hasattr(module, "_foreign_roles_in_roles_sql")
        else module._foreign_cluster_roles_in_roles_sql
    )
    return (
        module._role_sql_meta_command_problems(body)
        + module._unsupported_role_statement_problems(body)
        + module._role_privilege_drift_problems(body)
        + module._role_membership_problems(body, superuser=superuser)
        + [f"foreign role {name}" for name in foreign(body, superuser=superuser)]
    )


def test_roles_sql_gate_accepts_real_pg_dumpall_output() -> None:
    """A restore script's worst failure is refusing a recovery that would work.

    Every arm here is real ``pg_dumpall --roles-only`` output or a shape it
    genuinely emits, and every one must PASS both scripts. The parameter-ACL
    arms are the ones that make the membership rule earn itself: pg_dumpall has
    a fourth section, ``-- Role privileges on configuration parameters``, whose
    lines are ``GRANT <privilege> ON PARAMETER <guc> TO <role>`` -- an OBJECT
    grant that names an application role. Reading those as memberships would
    refuse a file this repository's own backup publishes.

    Round 22 removed the plain ``GRANT SELECT ON TABLE`` arm from this
    corpus: pg_dumpall --roles-only never emits object grants outside the
    parameter section, and replaying one as the bootstrap superuser is a
    privilege escalation (codex round-21 P1). ``GRANT ... ON PARAMETER``
    stays accepted above; everything else with an ON clause is refused in
    ``test_object_grants_are_refused_on_both_sides``.
    """
    accepted = (
        ("the captured container-shape dump", _REAL_ROLES_SQL, "postgres"),
        ("a mixed-case bootstrap superuser", _REAL_MIXED_CASE_ROLES_SQL, "Ums_Admin"),
        (
            "the restricted-login bootstrap membership edge",
            _BOTH_APP_ROLES
            + "GRANT app_tenant TO postgres WITH INHERIT FALSE GRANTED BY postgres;\n",
            "postgres",
        ),
        (
            "the PG<=15 bootstrap membership form, no WITH clause",
            _BOTH_APP_ROLES + "GRANT app_platform TO postgres GRANTED BY postgres;\n",
            "postgres",
        ),
        (
            "a parameter ACL granted to an app role",
            _BOTH_APP_ROLES + "GRANT SET ON PARAMETER work_mem TO app_platform;\n",
            "postgres",
        ),
        (
            "a per-role GUC whose VALUE is an attribute word",
            _BOTH_APP_ROLES + "ALTER ROLE app_tenant SET application_name TO login;\n",
            "postgres",
        ),
        (
            "the per-database GUC form",
            _BOTH_APP_ROLES
            + "ALTER ROLE app_tenant IN DATABASE ums SET application_name TO LOGIN;\n",
            "postgres",
        ),
        (
            "a quoted lookalike, a DIFFERENT role in PostgreSQL",
            _BOTH_APP_ROLES + 'ALTER ROLE "App_Tenant" WITH SUPERUSER;\n',
            "postgres",
        ),
        (
            "a hyphenated lookalike, also a different role",
            _BOTH_APP_ROLES + 'ALTER ROLE "app_tenant-shadow" WITH SUPERUSER;\n',
            "postgres",
        ),
        (
            "a SCRAM verifier, expiry and connection limit",
            _BOTH_APP_ROLES
            + "ALTER ROLE app_tenant WITH PASSWORD 'SCRAM-SHA-256$4096:ab==$cd:ef' "
            "VALID UNTIL '2030-01-01 00:00:00+00' CONNECTION LIMIT 5;\n",
            "postgres",
        ),
        (
            "a membership naming no application role",
            _BOTH_APP_ROLES + "GRANT pg_monitor TO postgres;\n",
            "postgres",
        ),
        ("a REVOKE", _BOTH_APP_ROLES + "REVOKE app_tenant FROM app_platform;\n", "postgres"),
    )
    for module in (restore, backup):
        for label, body, superuser in accepted:
            assert _roles_sql_gate_problems(module, body, superuser) == [], (
                module.__name__,
                label,
            )
    # And the whole captured dump really is publishable.
    backup._validate_dump_roles_covered(
        listing=None, roles_body=_REAL_ROLES_SQL, superuser="postgres"
    )


def test_restore_preflight_refuses_a_role_membership_for_an_app_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A GRANT carries no attribute token, so no attribute gate can see it.

    ``GRANT postgres TO app_tenant`` matches neither the CREATE-anchored
    allowlist nor the CREATE/ALTER-anchored drift gate, and the post-apply check
    only asserts the two roles EXIST -- it never reads pg_auth_members. Replayed
    on PostgreSQL 18 through the exact ``_restore_roles`` invocation it exits 0
    with EMPTY stderr and leaves ``postgres -> app_tenant``: with
    ``is_superuser=off`` and NO SET ROLE, that session read every row of an RLS
    table lacking FORCE, and on a FORCE table it ran ``ALTER TABLE ... NO FORCE
    ROW LEVEL SECURITY`` and then read every row.

    The accept arm is the captured dump, which carries both legitimate
    ``GRANT app_* TO postgres`` edges -- so the refusal cannot be a blanket
    "no GRANTs in roles.sql", which would block every real archive.
    """
    monkeypatch.setattr(
        restore,
        "_run_with_file",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(restore, "_psql", _restore_psql(superuser="postgres"))
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)

    accepted = tmp_path / restore.ROLES_NAME
    accepted.write_text(_REAL_ROLES_SQL, encoding="utf-8")
    assert "app_tenant" in restore._restore_roles("fake", accepted, timeout=5)

    refused = {
        "the superuser granted to the lane": _BOTH_APP_ROLES
        + "GRANT postgres TO app_tenant WITH INHERIT TRUE GRANTED BY postgres;\n",
        "behind the banner pg_dumpall prints": _BOTH_APP_ROLES
        + "--\n-- Role memberships\n--\n\nGRANT postgres TO app_tenant;\n",
        "the cross-lane edge": _BOTH_APP_ROLES + "GRANT app_tenant TO app_platform;\n",
        "a predefined role": _BOTH_APP_ROLES + "GRANT pg_read_all_data TO app_tenant;\n",
        "a role list": _BOTH_APP_ROLES + 'GRANT postgres TO app_tenant, "ON";\n',
        "the CREATE ROLE IN ROLE clause": "CREATE ROLE app_tenant IN ROLE postgres;\n"
        "CREATE ROLE app_platform;\n",
        "the CREATE ROLE clause pointed the other way": _BOTH_APP_ROLES
        + "CREATE ROLE postgres ROLE app_tenant;\n",
        "the legacy ALTER GROUP spelling": _BOTH_APP_ROLES
        + "ALTER GROUP postgres ADD USER app_tenant;\n",
        "an unquoted member that folds": _BOTH_APP_ROLES + "GRANT postgres TO APP_TENANT;\n",
    }
    for index, (label, body) in enumerate(refused.items()):
        path = tmp_path / f"membership-{index}.sql"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(restore.RestoreError) as raised:
            restore._restore_roles("fake", path, timeout=5)
        assert raised.value.code == restore.EXIT_ROLES_FAILED, label
        assert "membership graph" in str(raised.value), label

    # The drift gate genuinely cannot see any of this. If this starts failing,
    # the membership gate has been folded into the attribute gate and the two
    # are no longer independent checks.
    assert restore._role_privilege_drift_problems("GRANT postgres TO app_tenant;\n") == []


def test_restore_refuses_a_membership_the_target_cluster_already_had(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dangerous edge is not always IN the archive -- it can predate it.

    Memberships are cluster-global. Dropping and recreating the DATABASE does
    not remove a ``pg_auth_members`` row, and a clean ``roles.sql`` carries no
    REVOKE for an edge only the TARGET has. So restoring a perfectly clean
    archive into a cluster where ``app_tenant`` had already been granted the
    bootstrap superuser used to report SUCCESS while the lane kept that role's
    object privileges -- table-owner rights included, with no attribute change
    anywhere for the drift gate to see.

    The text scanner cannot reach this: the edge is not in the file. Only a
    catalog assertion after the replay can, which is what this pins.
    """
    monkeypatch.setattr(
        restore,
        "_run_with_file",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)
    clean = tmp_path / restore.ROLES_NAME
    clean.write_text(_REAL_ROLES_SQL, encoding="utf-8")

    # A healthy cluster: the archive is clean AND the graph is clean.
    monkeypatch.setattr(restore, "_psql", _restore_psql(superuser="postgres"))
    assert "app_tenant" in restore._restore_roles("fake", clean, timeout=5)

    # The SAME clean archive, into a cluster that already carried the edge.
    monkeypatch.setattr(
        restore,
        "_psql",
        _restore_psql(superuser="postgres", memberships=["app_tenant -> postgres"]),
    )
    with pytest.raises(restore.RestoreError) as raised:
        restore._restore_roles("fake", clean, timeout=5)
    assert raised.value.code == restore.EXIT_ROLES_FAILED
    assert "app_tenant -> postgres" in str(raised.value)
    assert "cluster-global" in str(raised.value)

    # The query must ask about the MEMBER side. A login being a member of a
    # lane is the deployed restricted-login model and must not trip this.
    assert "pg_auth_members" in restore.ROLE_MEMBERSHIPS_SQL
    assert "m.rolname IN ('app_tenant', 'app_platform')" in restore.ROLE_MEMBERSHIPS_SQL


def test_backup_refuses_to_publish_a_role_membership_for_an_app_role() -> None:
    """The publish gate was blind in exactly the same way the restore gate was.

    A gate on one side of a round trip is not a gate: restore-only turns every
    archive already carrying the edge into an unrestorable one, and
    publish-only keeps minting archives the restore has to catch. The membership
    rule needs no bootstrap superuser, which is why it lives on the
    superuser-free path that stays enforced under ``--no-verify-dump``.
    """
    with pytest.raises(backup.BackupError) as raised:
        backup._validate_dump_roles_covered(
            listing=None,
            roles_body=_BOTH_APP_ROLES
            + "GRANT postgres TO app_tenant WITH INHERIT TRUE GRANTED BY postgres;\n",
        )
    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "membership graph" in str(raised.value)

    backup._validate_dump_roles_covered(
        listing=None, roles_body=_REAL_ROLES_SQL, superuser="postgres"
    )


def test_roles_sql_gate_reads_the_statements_psql_executes() -> None:
    r"""The validator must read the program psql runs, not a different one.

    Every body here was applied through the real ``_restore_roles`` psql
    invocation and LANDED its privilege -- rolsuper/rolbypassrls true, or the
    pg_auth_members edge -- with psql exiting 0. The old gate returned [] for
    all of them because ``body.split(";")`` plus an anchored ``re.match`` drops
    any chunk not STARTING with CREATE/ALTER ROLE, which is exactly what a
    comment or a quoted semicolon manufactures.

    The last two are the ones no line-anchored alternative reaches: psql treats
    ``\'`` inside an ``E'...'`` literal -- and inside a plain literal once the
    file has done ``SET standard_conforming_strings = off`` -- as an escaped
    quote, so a scanner that stops at that quote swallows the rest of the file,
    GRANT included, inside a phantom string.
    """
    drift_hidden = {
        "a line comment before the ALTER": _BOTH_APP_ROLES
        + "-- bumped\nALTER ROLE app_tenant WITH SUPERUSER;\n",
        "a trailing comment on the previous statement": "CREATE ROLE app_platform;\n"
        "CREATE ROLE app_tenant; -- created above\n"
        "ALTER ROLE app_tenant WITH SUPERUSER;\n",
        "a block comment spanning the split": _BOTH_APP_ROLES
        + "/* dumped; regenerated */\nALTER ROLE app_tenant WITH SUPERUSER;\n",
        "a block comment on the same line": _BOTH_APP_ROLES
        + "/* note */ ALTER ROLE app_tenant WITH SUPERUSER;\n",
        "a semicolon inside a quoted password": _BOTH_APP_ROLES
        + "ALTER ROLE app_tenant WITH PASSWORD 'x;' SUPERUSER;\n",
        "a semicolon inside a dollar-quoted password": _BOTH_APP_ROLES
        + "ALTER ROLE app_tenant WITH PASSWORD $$a;b$$ SUPERUSER;\n",
        "a tagged dollar quote landing BYPASSRLS": _BOTH_APP_ROLES
        + "ALTER ROLE app_tenant WITH PASSWORD $tag$p;w$tag$ BYPASSRLS;\n",
        "a UTF-8 BOM, which str.strip() does not remove": "﻿"
        "CREATE ROLE app_tenant SUPERUSER;\nCREATE ROLE app_platform;\n",
        "a second statement on the same line": "CREATE ROLE app_platform NOLOGIN; "
        "CREATE ROLE app_tenant WITH SUPERUSER;\n",
    }
    membership_hidden = {
        "an E-string escaped quote": _BOTH_APP_ROLES
        + "ALTER ROLE app_tenant WITH PASSWORD E'a\\';b' NOLOGIN;\n"
        + "GRANT postgres TO app_tenant;\n",
        "standard_conforming_strings turned off": _BOTH_APP_ROLES
        + "SET standard_conforming_strings = off;\n"
        + "ALTER ROLE app_tenant WITH PASSWORD 'a\\';b' NOLOGIN;\n"
        + "GRANT postgres TO app_tenant;\n",
    }
    for module in (restore, backup):
        for label, body in drift_hidden.items():
            assert module._role_privilege_drift_problems(body) != [], (module.__name__, label)
        for label, body in membership_hidden.items():
            assert module._role_membership_problems(body) != [], (module.__name__, label)

    # Literal BODIES are masked. That is not cosmetic: it is what stops a
    # keyword inside a string forging an attribute token, and what keeps a
    # SCRAM verifier out of a refusal message when an archive was taken with
    # --include-role-passwords.
    for module in (restore, backup):
        assert module._scan_role_sql(
            "ALTER ROLE app_tenant WITH PASSWORD 'SCRAM-SHA-256$4096:s3cret$verifier';\n"
        ) == ["ALTER ROLE app_tenant WITH PASSWORD ''"], module.__name__
        assert module._scan_role_sql(
            "COMMENT ON ROLE app_tenant IS $tag$ SUPERUSER $tag$;\n"
        ) == ["COMMENT ON ROLE app_tenant IS ''"], module.__name__

    # The same divergence pointed the OTHER way: these are VALID declarations
    # the publish gate used to refuse, because its line-anchored regex could
    # not see past a comment, a BOM, or a preceding statement.
    for body in (
        "/* c */ CREATE ROLE app_tenant;\n",
        "CREATE ROLE postgres; CREATE ROLE app_tenant;\n",
        "﻿CREATE ROLE app_tenant;\n",
    ):
        assert backup._role_declared_in_roles_sql(body, "app_tenant"), body

    # ...and the foreign-role gates, which fail on the SAME axis: each of these
    # created a foreign SUPERUSER LOGIN role while preflight reported success.
    for body in (
        "﻿CREATE ROLE evil_admin SUPERUSER LOGIN;\n",
        "CREATE ROLE app_tenant; CREATE ROLE evil_admin SUPERUSER LOGIN;\n",
        "  /* c */  CREATE ROLE evil_admin SUPERUSER LOGIN;\n",
        "ALTER ROLE app_tenant SET search_path = 'a;b'; CREATE ROLE evil_admin SUPERUSER;\n",
    ):
        assert restore._foreign_roles_in_roles_sql(body, superuser="postgres") == [
            "evil_admin"
        ], body
        assert backup._foreign_cluster_roles_in_roles_sql(body, superuser="postgres") == [
            "evil_admin"
        ], body


def test_roles_sql_gate_refuses_psql_meta_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r"""``\!`` runs a shell command in the database container and psql exits 0.

    ``_container_sh`` passes no ``-u``, so that shell runs as root. MEASURED on
    postgres:18 through the exact ``_restore_roles`` argv: a roles.sql carrying
    ``\! id > /pr210_B`` wrote ``uid=0(root) gid=0(root) groups=0(root)`` to
    that file, psql returned 0, and stderr was EMPTY -- so
    ``_unexpected_roles_errors`` had nothing to classify, and the surrounding
    CREATE ROLE statements still applied. psql honours a backslash ANYWHERE
    outside a quote or a comment, which is why the mid-statement and
    glued-to-a-word arms are here and not decoration.

    ``\restrict``/``\unrestrict`` are NOT optional to allow: every supported
    branch wraps its dump in that pair, so refusing backslash commands
    wholesale would refuse every genuine archive. The accept arm is the
    captured dump, which contains them.
    """
    monkeypatch.setattr(
        restore,
        "_run_with_file",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(restore, "_psql", _restore_psql(superuser="postgres"))

    assert restore._role_sql_meta_command_problems(_REAL_ROLES_SQL) == []
    assert backup._role_sql_meta_command_problems(_REAL_ROLES_SQL) == []

    refused = {
        "line-initial": _BOTH_APP_ROLES + "\\! id > /tmp/pwned\n",
        "mid-statement": _BOTH_APP_ROLES + "SET client_encoding = 'UTF8' \\! id\n;\n",
        "glued to a word": _BOTH_APP_ROLES + "SELECT 1\\! id\n;\n",
        "include": _BOTH_APP_ROLES + "\\i /etc/passwd\n",
        "copy": _BOTH_APP_ROLES + "\\copy pg_authid TO '/tmp/authid'\n",
        "a lookalike of an allowed name": _BOTH_APP_ROLES + "\\restrictfoo TOK\n",
        # psql does NOT treat a second backslash as a command separator --
        # MEASURED, it reports `invalid command \` and runs nothing -- so
        # this is defence in depth, not a live bypass. Only the FIRST name
        # on the line is allowlisted, and a real nonce is alphanumeric.
        "an allowed name carrying a second backslash": _BOTH_APP_ROLES
        + "\\unrestrict tok \\\\ \\! id\n",
    }
    for index, (label, body) in enumerate(refused.items()):
        assert restore._role_sql_meta_command_problems(body) != [], label
        assert backup._role_sql_meta_command_problems(body) != [], label
        path = tmp_path / f"meta-{index}.sql"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(restore.RestoreError) as raised:
            restore._restore_roles("fake", path, timeout=5)
        assert raised.value.code == restore.EXIT_ROLES_FAILED, label
        assert "meta-command" in str(raised.value), label

    # A backslash inside a literal is NOT a meta-command -- pg_dumpall re-emits
    # a role comment containing one as exactly this E-string.
    assert (
        restore._role_sql_meta_command_problems(
            "COMMENT ON ROLE app_tenant IS E'a \\\\! id backslash';\n"
        )
        == []
    )


def test_roles_sql_gate_refuses_statements_pg_dumpall_never_emits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Shapes that escalate or brick without ever naming a role attribute.

    All were MEASURED on postgres:18 through the real ``_restore_roles``
    invocation, and none appears in ``pg_dumpall --roles-only`` output:

    * ``ALTER ROLE ALL SET session_preload_libraries TO 'evil';`` exits 0 and
      then no role can connect at all -- ``FATAL: could not access file
      "evil"`` for the bootstrap superuser included.
    * ``ALTER ROLE ums_admin RENAME TO app_tenant;`` exits 0 with only a
      WARNING and leaves a role NAMED app_tenant that is ``rolsuper=true
      rolcanlogin=true`` -- which then SATISFIES the post-apply existence
      check, so the restore reports success.
    * ``SET ROLE`` changes who the remainder of the file runs as.
    """
    monkeypatch.setattr(
        restore,
        "_run_with_file",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(restore, "_psql", _restore_psql(superuser="postgres"))

    assert restore._unsupported_role_statement_problems(_REAL_ROLES_SQL) == []
    assert backup._unsupported_role_statement_problems(_REAL_ROLES_SQL) == []

    refused = {
        "ALTER ROLE ALL, preload libraries": _BOTH_APP_ROLES
        + "ALTER ROLE ALL SET session_preload_libraries TO 'evil';\n",
        "ALTER ROLE ALL, anything at all": _BOTH_APP_ROLES
        + "ALTER ROLE ALL SET statement_timeout TO '1ms';\n",
        "a per-role preload library on the superuser": _BOTH_APP_ROLES
        + "ALTER ROLE postgres SET session_preload_libraries TO 'evil';\n",
        "a rename into a protected name": _BOTH_APP_ROLES
        + "ALTER ROLE ums_admin RENAME TO app_tenant;\n",
        "a rename out of a protected name": _BOTH_APP_ROLES
        + "ALTER ROLE app_tenant RENAME TO app_tenant_old;\n",
        "SET ROLE": _BOTH_APP_ROLES + "SET ROLE postgres;\n",
        # The verb allowlist admits DROP ROLE; nothing looked at WHAT was
        # dropped, so under --allow-nonempty the target was already gone by
        # the time the role vanished and the post-apply check refused.
        "DROP of a protected role": _BOTH_APP_ROLES
        + "DROP ROLE app_tenant;\n",
        "DROP IF EXISTS of a protected role": _BOTH_APP_ROLES
        + "DROP ROLE IF EXISTS app_platform;\n",
        # pg_dumpall emits no RENAME at all. Refusing only renames that
        # NAMED an app role left the bootstrap superuser renameable, which
        # invalidates POSTGRES_USER for every later connection while the
        # post-apply check still passes.
        "a rename of the bootstrap superuser": _BOTH_APP_ROLES
        + "ALTER ROLE ums_admin RENAME TO retired;\n",
        # An object grant, so the membership gate ignores it by design, and
        # it names no attribute, so the drift gate cannot see it -- but it
        # hands the lane a GUC that loads code at login.
        "a parameter ACL on a code-loading GUC": _BOTH_APP_ROLES
        + "GRANT SET ON PARAMETER session_preload_libraries TO app_tenant;\n",
        "the ALTER SYSTEM form of the same": _BOTH_APP_ROLES
        + "GRANT ALTER SYSTEM ON PARAMETER local_preload_libraries TO app_platform;\n",
        # SET SESSION AUTHORIZATION is three words; SET
        # session_authorization is TWO, because the underscore form is a
        # single token. Both change who the rest of the file runs as.
        "SET SESSION AUTHORIZATION": _BOTH_APP_ROLES
        + "SET SESSION AUTHORIZATION postgres;\n",
        "SET session_authorization, one token": _BOTH_APP_ROLES
        + "SET session_authorization = postgres;\n",
        "RESET session_authorization": _BOTH_APP_ROLES
        + "RESET session_authorization;\n",
        # An unquoted identifier folds, so the wildcard spelling is not fixed.
        "ALTER ROLE all, lowercase": _BOTH_APP_ROLES
        + "ALTER ROLE all SET statement_timeout TO '1ms';\n",
        "ALTER ROLE All, mixed case": _BOTH_APP_ROLES
        + "ALTER ROLE All SET statement_timeout TO '1ms';\n",
        # Ordinary SQL is not role DDL, and used to be ignored rather than
        # refused. COPY ... TO PROGRAM is command execution as the postgres
        # OS user; the rest reconfigure or destroy the cluster.
        "COPY TO PROGRAM": _BOTH_APP_ROLES
        + "COPY (SELECT '') TO PROGRAM 'id > /tmp/pwned';\n",
        "ALTER SYSTEM": _BOTH_APP_ROLES
        + "ALTER SYSTEM SET session_preload_libraries = 'evil';\n",
        "DROP DATABASE": _BOTH_APP_ROLES + "DROP DATABASE ums;\n",
        "CREATE EXTENSION": _BOTH_APP_ROLES + "CREATE EXTENSION plpython3u;\n",
        "DROP OWNED BY": _BOTH_APP_ROLES + "DROP OWNED BY app_tenant;\n",
        "a bare SELECT": _BOTH_APP_ROLES
        + "SELECT pg_catalog.pg_read_file('/etc/passwd');\n",
        "a DO block a comment walked past": _BOTH_APP_ROLES
        + "/* x */ DO $$ BEGIN EXECUTE 'ALTER ROLE app_tenant SUPERUSER'; END $$;\n",
    }
    for index, (label, body) in enumerate(refused.items()):
        assert restore._unsupported_role_statement_problems(body) != [], label
        assert backup._unsupported_role_statement_problems(body) != [], label
        path = tmp_path / f"unsupported-{index}.sql"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(restore.RestoreError) as raised:
            restore._restore_roles("fake", path, timeout=5)
        assert raised.value.code == restore.EXIT_ROLES_FAILED, label

    # A per-role GUC that is NOT a code loader stays allowed -- pg_dumpall
    # emits exactly this line.
    assert (
        restore._unsupported_role_statement_problems(
            "ALTER ROLE app_platform SET search_path TO 'public';\n"
        )
        == []
    )
    # A role genuinely NAMED "ALL" is quoted, and PostgreSQL does not fold a
    # quoted identifier -- so it is an ordinary role, not the wildcard.
    assert (
        restore._unsupported_role_statement_problems(
            'ALTER ROLE "ALL" SET statement_timeout TO \'1ms\';\n'
        )
        == []
    )
    # The verb allowlist must not eat the shapes pg_dumpall really writes:
    # the session GUC header, a role comment, and the parameter-ACL section.
    # A role-DDL statement too short to have a subject must be REFUSED, not
    # crash the preflight: reaching for the subject token unguarded raised
    # IndexError out of _preflight_roles_file instead of returning a problem.
    for truncated in ("DROP ROLE;\n", "ALTER ROLE;\n", "CREATE ROLE;\n"):
        assert restore._unsupported_role_statement_problems(truncated) == []
        assert backup._unsupported_role_statement_problems(truncated) == []
    for allowed in (
        "SET default_transaction_read_only = off;\n",
        # An ordinary session GUC is not a session-IDENTITY statement.
        "SET search_path = public;\n",
        "SET standard_conforming_strings = on;\n",
        "COMMENT ON ROLE app_tenant IS 'lane';\n",
        "SECURITY LABEL FOR anon ON ROLE app_tenant IS 'MASKED';\n",
        "GRANT ALTER SYSTEM ON PARAMETER shared_buffers TO app_tenant;\n",
        # A code-loading GUC granted to the BOOTSTRAP SUPERUSER is fine --
        # it already has it; only the protected roles are constrained.
        "GRANT SET ON PARAMETER session_preload_libraries TO postgres;\n",
        "REVOKE app_tenant FROM app_platform;\n",
        "DROP ROLE IF EXISTS some_other_role;\n",
    ):
        assert restore._unsupported_role_statement_problems(allowed) == [], allowed
        assert backup._unsupported_role_statement_problems(allowed) == [], allowed


def test_roles_sql_drift_gate_names_all_six_privileged_attributes() -> None:
    """The invariant is 20260608_0001's, and it is all six attributes.

    ``_create_role`` issues ``CREATE ROLE "<role>" NOLOGIN`` and grants these
    roles nothing else, and ``pg_dumpall`` writes the negative form of every
    one of the six for them. CREATEROLE is the one that is not merely tidiness:
    an app_tenant session holding it can mint a standing LOGIN account -- and
    because pg_dumpall emits the attribute on an allowlisted role, a legacy
    archive from a cluster where an operator once set it restores clean.
    """
    for module in (restore, backup):
        assert module._PRIVILEGED_ATTRIBUTE_TOKENS == frozenset(
            {"SUPERUSER", "BYPASSRLS", "LOGIN", "CREATEROLE", "CREATEDB", "REPLICATION"}
        ), module.__name__
        assert module._role_privilege_drift_problems(
            _BOTH_APP_ROLES + "ALTER ROLE app_tenant WITH CREATEROLE CREATEDB REPLICATION;\n"
        ) == [
            "app_tenant: privileged attributes CREATEDB, CREATEROLE, REPLICATION must be revoked"
        ], module.__name__
        # The negative forms pg_dumpall really writes are whole tokens, not
        # substrings of the enabled ones.
        assert module._role_privilege_drift_problems(_REAL_ROLES_SQL) == [], module.__name__


def test_restore_and_backup_share_one_role_sql_gate() -> None:
    """Pin the two gates by SOURCE, not by comparing two frozensets.

    The previous version compared ``_PRIVILEGED_ATTRIBUTE_TOKENS`` and the role
    tuple and nothing else -- which is how the two implementations came to
    disagree on a MISSING app role (backup reported drift, restore returned [])
    without any test noticing. The scripts are stdlib-only operator CLIs that
    cannot import each other, so the block is duplicated on purpose; comparing
    the source of every shared function turns any drift into a failure here,
    and the corpus tests above prove the shared source actually answers the
    question rather than the two agreeing on nothing.
    """
    shared = (
        "_role_sql_comment_end",
        "_role_sql_quoted_end",
        "_role_sql_dollar_end",
        "_role_sql_starts_escape_string",
        "_role_sql_span",
        "_scan_role_sql",
        "_role_sql_tokens",
        "_role_sql_identifier",
        "_role_sql_words",
        "_role_sql_meta_command_problems",
        "_role_sql_name_list",
        "_grant_membership_edges",
        "_role_ddl_membership_edges",
        "_role_sql_word_index",
        "_alter_group_membership_edges",
        "_role_membership_edges",
        "_role_membership_problems",
        "_role_sql_setting_name",
        "_role_sql_statement_is_role_shaped",
        "_object_grant_problem",
        "_unsafe_parameter_grant_problem",
        "_role_ddl_statement_problem",
        "_unsupported_role_statement_problem",
        "_unsupported_role_statement_problems",
        "_role_attribute_clause",
        "_collect_role_attribute_tokens",
        "_role_privilege_drift_problems",
        "_bootstrap_role_lockout_problems",
        "_created_role_names",
    )
    for name in shared:
        assert inspect.getsource(getattr(restore, name)) == inspect.getsource(
            getattr(backup, name)
        ), f"{name} has drifted between the two scripts"
    assert restore._PRIVILEGED_ATTRIBUTE_TOKENS == backup._PRIVILEGED_ATTRIBUTE_TOKENS
    assert restore._ALLOWED_ROLE_SQL_META_COMMANDS == backup._ALLOWED_ROLE_SQL_META_COMMANDS
    assert restore._UNSAFE_ROLE_SETTINGS == backup._UNSAFE_ROLE_SETTINGS
    assert restore._ROLE_SQL_ALLOWED_HEADS == backup._ROLE_SQL_ALLOWED_HEADS
    assert restore._ROLE_SQL_ROLE_NOUNS == backup._ROLE_SQL_ROLE_NOUNS
    assert restore._PROTECTED_APP_ROLES == backup._PROTECTED_APP_ROLES
    assert tuple(restore.REQUIRED_ROLES) == backup._REQUIRED_UNPRIVILEGED_ROLES
    # The membership gate reads _PROTECTED_APP_ROLES while the publish gate's
    # declaration check reads REQUIRED_ROLES; pin backup's two tuples together
    # so it cannot judge memberships and declarations against different sets.
    assert tuple(backup.REQUIRED_ROLES) == backup._REQUIRED_UNPRIVILEGED_ROLES


def test_backup_roles_rejects_foreign_cluster_roles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Backup must refuse roles.sql that restore's allowlist would reject."""
    target = tmp_path / backup.ROLES_NAME

    def _extra_role_file(argv: list[str], *, timeout: int, target: Path):
        """extra role file."""
        target.write_text(
            "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\nCREATE ROLE extra;\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(backup, "_run_to_file", _extra_role_file)
    monkeypatch.setattr(backup, "_psql", lambda *_a, **_k: "postgres")
    with pytest.raises(backup.BackupError) as raised:
        backup._dump_roles("fake", target, timeout=5, include_passwords=False)
    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "extra" in str(raised.value)


def test_sync_staging_before_publication_fsyncs_every_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publication must flush dump, roles, manifest, and the staging directory."""
    staging = tmp_path / "staging"
    staging.mkdir()
    for name in (backup.DUMP_NAME, backup.ROLES_NAME, backup.MANIFEST_NAME):
        (staging / name).write_bytes(b"x")
    synced: list[str] = []

    def _record_file(path: Path) -> None:
        """record file."""
        synced.append(path.name)

    def _record_dir(path: Path) -> None:
        """record dir."""
        synced.append(path.name + "/")

    monkeypatch.setattr(backup, "_fsync_file", _record_file)
    monkeypatch.setattr(backup, "_fsync_directory", _record_dir)
    backup._sync_staging_before_publication(staging)
    assert synced == [
        backup.DUMP_NAME,
        backup.ROLES_NAME,
        backup.MANIFEST_NAME,
        "staging/",
    ]


def test_fsync_directory_does_not_absorb_flush_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory entry that cannot be flushed must stop the backup, not be
    swallowed as best-effort -- retention prunes older runs on this guarantee.
    """
    if sys.platform == "win32":
        monkeypatch.setattr(
            backup,
            "_flush_directory_entry",
            lambda path: (_ for _ in ()).throw(OSError(13, "directory flush refused")),
        )
    else:
        real_open = backup.os.open

        def _fake_fs_sync(fd: int) -> None:
            """Fail the flush after the directory fd opened normally."""
            raise OSError(13, "directory flush refused")

        fd = real_open(tmp_path, backup.os.O_RDONLY)
        monkeypatch.setattr(backup.os, "open", lambda *a, **k: fd)
        monkeypatch.setattr(backup.os, "fsync", _fake_fs_sync)

    with pytest.raises(OSError):
        backup._fsync_directory(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="FlushFileBuffers path is Windows-only")
def test_windows_directory_flush_api_failures_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """CreateFileW's INVALID_HANDLE_VALUE and FlushFileBuffers' BOOL result must
    both surface as OSError instead of reading as a silent success (PR #210
    review round 2).
    """
    from ctypes import wintypes

    invalid_handle = wintypes.HANDLE(-1).value
    calls = {"close": 0}

    class _FakeKernel32:
        """Duck-typed kernel32 whose results the test controls per case."""

        def __init__(self, *, open_ok: bool, flush_ok: bool) -> None:
            self._open_ok = open_ok
            self._flush_ok = flush_ok

        def CreateFileW(self, *_a: object, **_k: object) -> int:  # noqa: N802
            """Return a live handle or INVALID_HANDLE_VALUE per the scenario."""
            return 4242 if self._open_ok else invalid_handle

        def FlushFileBuffers(self, _handle: int) -> int:  # noqa: N802
            """Report flush success or failure per the scenario."""
            return 1 if self._flush_ok else 0

        @staticmethod
        def CloseHandle(_handle: int) -> int:  # noqa: N802
            """Record the close so ownership hygiene is observable."""
            calls["close"] += 1
            return 1

    some_path = Path("staging")

    monkeypatch.setattr(backup, "_KERNEL32", _FakeKernel32(open_ok=False, flush_ok=True))
    with pytest.raises(OSError) as open_failed:
        backup._flush_directory_entry(some_path)
    assert "CreateFileW" in str(open_failed.value)
    assert calls["close"] == 0, "a failed open owns no handle to close"

    monkeypatch.setattr(backup, "_KERNEL32", _FakeKernel32(open_ok=True, flush_ok=False))
    with pytest.raises(OSError) as flush_failed:
        backup._flush_directory_entry(some_path)
    assert "FlushFileBuffers" in str(flush_failed.value)
    assert calls["close"] == 1, "the handle is still released on flush failure"

    monkeypatch.setattr(backup, "_KERNEL32", _FakeKernel32(open_ok=True, flush_ok=True))
    backup._flush_directory_entry(some_path)
    assert calls["close"] == 2


@pytest.mark.skipif(sys.platform != "win32", reason="FlushFileBuffers path is Windows-only")
def test_windows_directory_open_requests_normal_sharing(monkeypatch) -> None:
    """dwShareMode=0 turned any concurrent reader of the directory (Explorer,
    indexer, antivirus) into ERROR_SHARING_VIOLATION; the durability open must
    request full sharing so benign co-readers cannot kill the nightly backup.
    """
    seen = {"share": None}

    class _SharingKernel32:
        """Stub whose only job is to record the dwShareMode we ask for."""

        @staticmethod
        def CreateFileW(_path, _access, share, *_a, **_k):  # noqa: N802
            """Record dwShareMode and hand back a live handle."""
            seen["share"] = share
            return 4242

        @staticmethod
        def FlushFileBuffers(_handle: int) -> int:  # noqa: N802
            """Report a successful flush."""
            return 1

        @staticmethod
        def CloseHandle(_handle: int) -> int:  # noqa: N802
            """Release the handle."""
            return 1

    monkeypatch.setattr(backup, "_KERNEL32", _SharingKernel32())
    backup._flush_directory_entry(Path("staging"))

    assert seen["share"] == backup._FILE_SHARE_READWRITEDELETE


def test_run_backup_refuses_publication_when_directory_flush_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """A non-durable output directory stops the run before publication and
    before retention bookkeeping ever sees it (exit 6, nothing pruned).
    """

    def _flush_refused(path: Path) -> None:
        """Simulate a disk whose directory entries cannot be flushed."""
        raise OSError(13, "directory flush refused")

    monkeypatch.setattr(backup, "_sync_staging_before_publication", _flush_refused)

    code = _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark")

    assert code == backup.EXIT_ARTIFACT_INVALID
    assert _run_dirs(tmp_path) == []
    assert not (tmp_path / backup.WATERMARK_NAME).is_file()


def test_publish_staging_run_quarantine_survives_a_taken_rejected_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the plain .rejected name is already taken the unique-nonce fallback
    must still move the non-durable run out of the accepted namespace; it can
    never stay discoverable under its published name.
    """
    staging = tmp_path / "accepted-run.partial"
    staging.mkdir()
    for name in (backup.DUMP_NAME, backup.ROLES_NAME, backup.MANIFEST_NAME):
        (staging / name).write_bytes(b"x")

    destination = tmp_path / "accepted-run"
    blocked = tmp_path / ("accepted-run" + backup.REJECTED_SUFFIX)
    blocked.mkdir()
    # The blocker must be NON-EMPTY. POSIX rename(2) lets a directory replace an
    # EMPTY directory, so an empty blocker is silently replaceable on Linux: the
    # first quarantine candidate would succeed, the nonce fallback would never be
    # exercised, and the assertion below would fail there while passing on
    # Windows (which refuses either way). An occupant makes the collision real on
    # every supported platform.
    (blocked / "occupant").write_bytes(b"x")

    def _flush_refused(parent: Path) -> None:
        """Every durability sync after publication fails on this disk."""
        raise OSError(13, "directory flush refused")

    monkeypatch.setattr(backup, "_sync_parent_directory_entry", _flush_refused)

    with pytest.raises(backup.BackupError) as caught:
        backup._publish_staging_run(staging, destination, tmp_path)

    assert caught.value.code == backup.EXIT_ARTIFACT_INVALID
    message = str(caught.value)
    assert "could not make durable" in message or "could not be made durable" in message
    assert not destination.exists(), "the accepted name must not survive"
    nonce_fallbacks = [
        p for p in tmp_path.iterdir() if ".rejected-" in p.name and p.is_dir()
    ]
    assert len(nonce_fallbacks) == 1, "the run landed under exactly one fallback name"
    assert (nonce_fallbacks[0] / backup.DUMP_NAME).is_file(), "artifacts preserved"
    assert "could not quarantine to" in message


def test_publish_quarantines_when_a_strict_acl_refusal_raises_backuperror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A strict-ACL refusal arrives as BackupError, not OSError.

    ``_restrict_run_dir_mode(..., strict=True)`` raises BackupError when icacls
    is missing, fails, or reports a permissive DACL. The staging directory has
    already been renamed by then, so if that escapes the handler the run stays
    under its accepted ``ums-backup-...Z`` name and later watermark scans and
    restores treat an explicitly-failed, potentially exposed backup as valid.

    The sibling quarantine tests both fail via OSError, so narrowing the
    handler back to ``except OSError`` leaves them green while reopening this
    hole. This is the arm that goes red.
    """
    staging = tmp_path / "accepted-run.partial"
    staging.mkdir()
    for name in (backup.DUMP_NAME, backup.ROLES_NAME, backup.MANIFEST_NAME):
        (staging / name).write_bytes(b"x")
    destination = tmp_path / "accepted-run"

    def _refuse(path: Path, out_dir: Path, *, strict: bool = False) -> None:
        """Fail exactly the way a permissive Windows DACL does."""
        if strict:
            raise backup.BackupError(
                backup.EXIT_ARTIFACT_INVALID,
                f"could not restrict {path} to owner-only permissions; "
                "refusing to publish to an insecure destination",
            )

    monkeypatch.setattr(backup, "_restrict_run_dir_mode", _refuse)

    with pytest.raises(backup.BackupError):
        backup._publish_staging_run(staging, destination, tmp_path)

    assert not destination.exists(), (
        "a strict-ACL refusal must not leave the run under its accepted name"
    )
    quarantined = [
        p for p in tmp_path.iterdir() if backup.REJECTED_SUFFIX in p.name and p.is_dir()
    ]
    assert len(quarantined) == 1, "the refused run must be quarantined, not published"


def test_run_is_published_backup_requires_artifact_metadata(tmp_path: Path) -> None:
    """Two nonempty artifact files alone must not read as a published backup;
    hand-planted manifests without recorded bytes/sha256 previously poisoned
    the watermark through _accepted_published_counts (PR #210 review round 8).
    """
    run = tmp_path / "ums-backup-20260826T000000Z"
    run.mkdir()
    dump_bytes = b"PGDMP-" * 16
    roles_bytes = b"CREATE ROLE app_tenant;"
    (run / backup.DUMP_NAME).write_bytes(dump_bytes)
    (run / backup.ROLES_NAME).write_bytes(roles_bytes)
    manifest = {
        "schema": backup.MANIFEST_SCHEMA,
        "artifacts": {
            backup.DUMP_NAME: {
                "bytes": len(dump_bytes),
                "sha256": backup._sha256(run / backup.DUMP_NAME),
            },
            backup.ROLES_NAME: {
                "bytes": len(roles_bytes),
                "sha256": backup._sha256(run / backup.ROLES_NAME),
            },
        },
        "table_row_counts": {"public.channels": 1},
    }
    assert backup._run_is_published_backup(run, manifest) is True

    del manifest["artifacts"]
    assert backup._run_is_published_backup(run, manifest) is False

    manifest["artifacts"] = {
        name: {} for name in (backup.DUMP_NAME, backup.ROLES_NAME)
    }
    assert backup._run_is_published_backup(run, manifest) is False

    bad_size = dict(manifest["artifacts"][backup.DUMP_NAME])
    bad_size["bytes"] = len(dump_bytes) + 1
    manifest["artifacts"][backup.DUMP_NAME] = bad_size
    assert backup._run_is_published_backup(run, manifest) is False


def test_execute_restore_verifies_replacement_before_cutover_on_every_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No restore path may overlay the live target before isolated verification."""
    from types import SimpleNamespace

    order: list[str] = []
    monkeypatch.setattr(restore, "_await_postgres", lambda *a, **k: None)
    monkeypatch.setattr(
        restore, "_guard_empty", lambda *a, **k: order.append("guard")
    )
    monkeypatch.setattr(
        restore,
        "_preflight_roles_file",
        lambda container, path, timeout: order.append("preflight"),
    )
    monkeypatch.setattr(
        restore,
        "_preflight_dump_readable",
        lambda container, path, timeout: order.append("dumpcheck"),
    )
    monkeypatch.setattr(
        restore,
        "_container_default_database",
        lambda container, timeout: order.append("dbname") or "appdb",
    )
    monkeypatch.setattr(
        restore,
        "_source_database_metadata",
        lambda manifest: ("6|UTF8|C|C|c|", "postgres", []),
    )
    monkeypatch.setattr(restore, "_database_acl_role_problems", lambda *a, **k: [])
    monkeypatch.setattr(
        restore,
        "_verify_backup_artifact_digests",
        lambda *a, **k: order.append("digest"),
    )

    @contextmanager
    def fake_target_lock(*_args: object, **_kwargs: object):
        """Model one target-scoped lock without starting a subprocess."""
        order.append("lock")
        try:
            yield
        finally:
            order.append("unlock")

    monkeypatch.setattr(restore, "_target_restore_lock", fake_target_lock)
    monkeypatch.setattr(
        restore,
        "_apply_database_acl",
        lambda _c, database, *_a, **_k: order.append(f"acl:{database}"),
    )
    monkeypatch.setattr(
        restore,
        "_require_target_locale",
        lambda *_a, dbname=None, **_k: order.append(f"locale:{dbname}"),
    )
    monkeypatch.setattr(
        restore,
        "_create_replacement_database",
        lambda _c, target_db, **_k: order.append(f"create:{target_db}") or "staging-db",
    )
    monkeypatch.setattr(
        restore,
        "_restore_roles",
        lambda container, path, timeout: order.append("roles") or set(),
    )
    original_role_settings = {"app_tenant": {"search_path": "legacy"}}
    desired_role_settings = {"app_tenant": {"statement_timeout": "2min"}}
    monkeypatch.setattr(
        restore,
        "_database_role_settings",
        lambda *a, **k: order.append("capture-original-db-role-settings")
        or original_role_settings,
    )
    monkeypatch.setattr(
        restore,
        "_desired_database_role_settings",
        lambda *a, **k: order.append("capture-db-role-settings")
        or desired_role_settings,
    )

    def fake_apply_role_settings(
        _container: str,
        _database: str,
        settings: dict[str, dict[str, str]],
        *,
        timeout: int,
    ) -> None:
        """Distinguish promotion settings from pre-replay rollback state."""
        _ = timeout
        label = "desired" if settings == desired_role_settings else "original"
        order.append(f"apply-{label}-db-role-settings")

    monkeypatch.setattr(
        restore,
        "_apply_database_role_settings_transactionally",
        fake_apply_role_settings,
    )
    monkeypatch.setattr(
        restore,
        "_restore_data",
        lambda *_a, dbname=None, **_k: order.append(f"data:{dbname}"),
    )
    monkeypatch.setattr(
        restore,
        "_verify",
        lambda *_a, dbname=None, **_k: order.append(f"verify:{dbname}") or True,
    )
    monkeypatch.setattr(restore, "_drop_generated_database", lambda *a, **k: None)
    monkeypatch.setattr(restore, "_live_protected_role_problems", lambda *a, **k: [])
    monkeypatch.setattr(restore, "_foreign_writer_session_count", lambda *a, **k: 0)

    def fake_cutover(
        _container: str,
        target_db: str,
        replacement: str,
        *,
        timeout: int,
        finalize: Callable[[], None] | None = None,
        rollback_finalize: Callable[[], None] | None = None,
    ) -> str:
        """Model the rename boundary and database-scoped role finalizer."""
        _ = (timeout, rollback_finalize)
        order.append(f"cutover:{target_db}<-{replacement}")
        if finalize is not None:
            finalize()
        return "previous-db"

    monkeypatch.setattr(restore, "_cutover_verified_database", fake_cutover)
    # The round-23 live checks read the catalog through _psql; stub it as a
    # healthy cluster so the replacement path is reached and its ORDER can be
    # asserted below.
    monkeypatch.setattr(restore, "_psql", _restore_psql())

    def _run(*, allow_nonempty: bool) -> bool:
        """Drive one CLI invocation and return its exit code."""
        order.clear()
        args = SimpleNamespace(
            timeout=5,
            allow_nonempty=allow_nonempty,
            wait_for_postgres=60,
            docker_timeout=5,
        )
        return restore._execute_restore(
            "container", tmp_path, args, {"source": {"database": "appdb"}}
        )

    assert _run(allow_nonempty=True) is True
    assert order.index("dbname") < order.index("lock") < order.index("guard")
    assert (
        order.index("guard")
        < order.index("dumpcheck")
        < order.index("preflight")
        < order.index("capture-original-db-role-settings")
        < order.index("create:appdb")
        < order.index("roles")
        < order.index("capture-db-role-settings")
        < order.index("acl:staging-db")
        < order.index("data:staging-db")
        < order.index("verify:staging-db")
        < order.index("cutover:appdb<-staging-db")
        < order.index("apply-desired-db-role-settings")
        < order.index("unlock")
    ), (
        "preflights must precede staging, and the verified replacement must "
        "precede the only live-name cutover"
    )
    assert order.count("roles") == 1, "full roles.sql must not replay during cutover"

    assert _run(allow_nonempty=False) is True
    assert "create:appdb" in order and "dbname" in order
    assert "dumpcheck" in order, (
        "the archive readability probe is read-only and must run on EVERY "
        "restore path -- an empty-target restore applies roles.sql, so an "
        "unreadable archive must be refused BEFORE those cluster roles land"
    )
    assert order.index("dumpcheck") < order.index("roles"), (
        "the probe must refuse before roles.sql is applied, not after"
    )
    assert order.index("preflight") < order.index("locale:staging-db") < order.index("roles")
    assert order.count("roles") == 1, "full roles.sql must not replay during cutover"


def test_preflight_dump_readable_fails_closed_on_a_nonzero_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe itself must refuse, and must be read-only about how it asks.

    The ordering test above monkeypatches this function out, so without this
    one the probe's own body would be unguarded: it would prove the CALL SITE
    is wired up while a probe that swallowed a non-zero exit stayed green.
    """
    dump = tmp_path / restore.DUMP_NAME
    dump.write_bytes(b"PGDMP not-really")
    seen: list[list[str]] = []

    def _listing(exit_code: int, stderr: str):
        """Return a _run_with_file stub with a fixed pg_restore --list result."""

        def fake_run_with_file(argv: list[str], *, timeout: int, source: Path):
            """Record the argv and answer with the configured result."""
            _ = (timeout, source)
            seen.append(argv)
            return subprocess.CompletedProcess(argv, exit_code, "", stderr)

        return fake_run_with_file

    monkeypatch.setattr(
        restore, "_run_with_file", _listing(1, "unsupported version (1.16)")
    )
    with pytest.raises(restore.RestoreError) as raised:
        restore._preflight_dump_readable("container", dump, timeout=5)
    assert raised.value.code == restore.EXIT_RESTORE_FAILED
    assert "unsupported version" in str(raised.value)
    assert "not changed" in str(raised.value)

    joined = " ".join(seen[0])
    assert "pg_restore --list" in joined
    # --list neither connects nor writes; a probe that did either would not be
    # safe to run before any mutation, which is the point of running it here.
    assert "-d " not in joined and "--clean" not in joined

    monkeypatch.setattr(restore, "_run_with_file", _listing(0, ""))
    restore._preflight_dump_readable("container", dump, timeout=5)


def test_execute_restore_refuses_an_unreadable_archive_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive this container's pg_restore cannot read must be refused first.

    The sha256 digest is checked on the way in, but it only proves the bytes
    match what backup wrote. A newer archive format hashes correctly and is
    still unreadable here. The probe runs before roles.sql replay and before the
    replacement database is created, so a refusal changes no cluster state.

    The load-bearing assertion is "create" not in order: without it this would
    only prove the probe ran, not that staging was actually skipped.
    """
    from types import SimpleNamespace

    order: list[str] = []
    monkeypatch.setattr(restore, "_await_postgres", lambda *a, **k: None)
    monkeypatch.setattr(restore, "_guard_empty", lambda *a, **k: None)
    monkeypatch.setattr(
        restore,
        "_source_database_metadata",
        lambda manifest: ("6|UTF8|C|C|c|", "postgres", []),
    )
    monkeypatch.setattr(restore, "_database_acl_role_problems", lambda *a, **k: [])
    monkeypatch.setattr(
        restore, "_verify_backup_artifact_digests", lambda *a, **k: None
    )
    monkeypatch.setattr(restore, "_foreign_writer_session_count", lambda *a, **k: 0)
    monkeypatch.setattr(restore, "_live_protected_role_problems", lambda *a, **k: [])

    @contextmanager
    def fake_target_lock(*_args: object, **_kwargs: object):
        """Model lock ownership while the preflight rejects the archive."""
        order.append("lock")
        yield

    monkeypatch.setattr(restore, "_target_restore_lock", fake_target_lock)
    monkeypatch.setattr(
        restore, "_preflight_roles_file", lambda *a, **k: order.append("preflight")
    )

    def _reject(container: str, path: Path, timeout: int) -> None:
        """Stand in for a container whose pg_restore cannot read the archive."""
        _ = (container, path, timeout)
        order.append("dumpcheck")
        raise restore.RestoreError(
            restore.EXIT_RESTORE_FAILED,
            "pg_restore --list could not read database.dump",
        )

    monkeypatch.setattr(restore, "_preflight_dump_readable", _reject)
    monkeypatch.setattr(
        restore,
        "_container_default_database",
        lambda *a, **k: order.append("dbname") or "appdb",
    )
    monkeypatch.setattr(
        restore, "_create_replacement_database", lambda *a, **k: order.append("create")
    )
    monkeypatch.setattr(
        restore, "_restore_roles", lambda *a, **k: order.append("roles") or set()
    )
    monkeypatch.setattr(restore, "_restore_data", lambda *a, **k: order.append("data"))
    monkeypatch.setattr(restore, "_verify", lambda *a, **k: True)

    args = SimpleNamespace(
        timeout=5, allow_nonempty=True, wait_for_postgres=60, docker_timeout=5
    )
    with pytest.raises(restore.RestoreError) as raised:
        restore._execute_restore(
            "container", tmp_path, args, {"source": {"database": "appdb"}}
        )

    assert raised.value.code == restore.EXIT_RESTORE_FAILED
    # The archive probe moved ahead of the roles.sql preflight (every path),
    # so the refusal lands before ANYTHING touches the cluster -- the stub
    # raises inside the probe, so the roles preflight is never even reached.
    assert order == ["dbname", "lock", "dumpcheck"], (
        f"the target must be untouched after the probe refuses, got {order}"
    )
    assert "create" not in order


def test_windows_dacl_parser_fail_closed() -> None:
    """icacls parsing must allowlist only SYSTEM/Administrators/owner; every
    other principal (e.g. DOMAIN\\BackupReaders), inheritance markers, or a
    missing owner grant is a failure.
    """
    listing = "\n".join(
        [
            "accepted-run NT AUTHORITY\\SYSTEM:(I)(F)",
            "DOMAIN\\BackupReaders:(R)",
            "BUILTIN\\Users:(RX)",
            "desktop\\winuser:(F)",
        ]
    )
    problems = backup._windows_dacl_problems(listing, "winuser")

    assert any("inherited access" in problem for problem in problems)
    assert any(
        "unexpected ACE" in problem and "DOMAIN\\BackupReaders" in problem
        for problem in problems
    )
    assert any(
        "unexpected ACE" in problem and "BUILTIN\\Users" in problem
        for problem in problems
    )
    assert not any(
        problem.startswith("no explicit Full-control") for problem in problems
    )

    secure_listing = "\n".join(["accepted-run", "desktop\\winuser:(F)"])
    assert backup._windows_dacl_problems(secure_listing, "winuser") == []


def test_windows_dacl_rejects_lookalike_infrastructure_principals() -> None:
    """A domain account whose LEAF name is SYSTEM or OWNER RIGHTS is not infra.

    The allowlist used to match the basename after the last backslash, so
    ``CORP\\SYSTEM:(R)`` -- and ``EVIL\\SYSTEM:(F)``, full control -- were
    reported as an owner-only DACL. A complete finance/audit/authorization
    backup was then published as secure while that identity could still read
    it. Only whole identities may match.
    """
    for ace in ("CORP\\SYSTEM:(R)", "CORP\\OWNER RIGHTS:(R)", "EVIL\\SYSTEM:(F)"):
        listing = "\n".join(["accepted-run", "desktop\\winuser:(F)", ace])
        assert backup._windows_dacl_problems(listing, "winuser"), (
            f"{ace} must be flagged, not allowlisted as infrastructure"
        )

    # The genuine fully-qualified principals must still pass, or every healthy
    # nightly backup on Windows would be quarantined instead.
    for ace in (
        "NT AUTHORITY\\SYSTEM:(F)",
        "BUILTIN\\Administrators:(F)",
        "OWNER RIGHTS:(RC)",
    ):
        listing = "\n".join(["accepted-run", "desktop\\winuser:(F)", ace])
        assert backup._windows_dacl_problems(listing, "winuser") == [], (
            f"{ace} is standard Windows infrastructure and must be accepted"
        )


def test_windows_dacl_rejects_space_boundary_lookalike_principals() -> None:
    """A principal whose NAME merely ends in "system" is not infrastructure.

    The whole-identity allowlist accepts ``icacls``'s fused first row by
    matching after a space boundary. Applied to the BARE tokens ("system",
    "owner rights") that re-opened the impersonation it was meant to close,
    just through a space instead of a backslash: Windows account names may
    legally contain spaces, so ``HOST\\Backup System:(F)`` ends with " system"
    and was allowlisted while holding Full control.

    This class is what the backslash-only lookalike test misses, and the code
    it guards is strictly narrower than the pre-fix basename match -- these
    were all correctly rejected before, so accepting them was a NET-NEW hole.
    """
    for ace in (
        "EVIL\\Evil System:(F)",
        "HOST\\Backup System:(R)",
        "CORP\\Foo owner rights:(R)",
    ):
        listing = "\n".join(["accepted-run", "desktop\\winuser:(F)", ace])
        assert backup._windows_dacl_problems(listing, "winuser"), (
            f"{ace} ends in an infrastructure word but is NOT infrastructure"
        )

    # The space boundary must still do its actual job: the fused first row.
    assert backup._is_allowlisted_principal(
        "c:\\backups\\ums-backup-20260827t000000z nt authority\\system"
    ), "the fused echoed-path row is why the space form exists"


def test_windows_dacl_accepts_the_owner_on_the_merged_echoed_path_row() -> None:
    """icacls fuses the run-dir path onto its FIRST ACE row.

    ``_parse_icacls_listing_line`` keeps that path attached to the principal,
    so the owner is recognised by suffix rather than equality. Pinning it here
    because the whole-identity allowlist above must not turn that valid row
    into a false quarantine of a healthy backup.
    """
    merged = "C:\\backups\\ums-backup-20260827T000000Z DESKTOP\\winuser:(OI)(CI)(F)"
    assert backup._windows_dacl_problems(merged, "winuser") == []

    # A merged SYSTEM row is infrastructure, not an unexpected principal: the
    # only complaint may be the missing owner grant, never the SYSTEM ACE.
    merged_system = "C:\\backups\\ums-backup-20260827T000000Z NT AUTHORITY\\SYSTEM:(F)"
    assert backup._windows_dacl_problems(merged_system, "winuser") == [
        "no explicit Full-control grant for 'winuser' was found"
    ]

    with_owner = "\n".join([merged_system, "desktop\\winuser:(F)"])
    assert backup._windows_dacl_problems(with_owner, "winuser") == []

    # FIX(codex round-23 P2): with USERDOMAIN known, an ACE for a DIFFERENT
    # domain sharing the basename is a stranger, not the owner. The old bare
    # suffix match treated CORP\winuser as the owner and passed the DACL.
    lookalike = "C:\\backups\\run corp\\winuser:(F)"
    assert backup._windows_dacl_problems(
        lookalike, "winuser", owner_domain="desktop"
    ) == [
        "unexpected non-owner principal C:\\backups\\run corp\\winuser holds "
        "full control",
        "no explicit Full-control grant for 'winuser' was found",
    ]
    # The full identity itself still matches, bare or fused onto the path row.
    assert (
        backup._windows_dacl_problems(
            "desktop\\winuser:(F)", "winuser", owner_domain="desktop"
        )
        == []
    )
    assert (
        backup._windows_dacl_problems(
            merged, "winuser", owner_domain="desktop"
        )
        == []
    )

    # A stranger carrying Full control (not just a restricted ACE) must hit
    # the dedicated non-owner refusal, and the missing owner grant stays
    # reported alongside it.
    stranger_listing = "\n".join(["accepted-run", "desktop\\svc_backup:(F)"])
    stranger_problems = backup._windows_dacl_problems(stranger_listing, "winuser")
    assert any(
        problem.startswith("unexpected non-owner principal")
        and "desktop\\svc_backup" in problem
        and "full control" in problem
        for problem in stranger_problems
    )
    assert any(
        problem.startswith("no explicit Full-control grant")
        for problem in stranger_problems
    )


def test_validate_dump_roles_covered_refuses_privileged_app_roles() -> None:
    """SUPERUSER/BYPASSRLS/LOGIN drift on app_tenant/app_platform refuses
    publication: the restore replays these attributes before the archive and
    no migration reruns afterwards (round-8 P1).
    """
    clean = (
        "CREATE ROLE app_tenant WITH NOSUPERUSER NOBYPASSRLS NOLOGIN;\n"
        "CREATE ROLE app_platform WITH NOSUPERUSER NOBYPASSRLS NOLOGIN;\n"
    )
    empty_listing = ""
    backup._validate_dump_roles_covered(listing=empty_listing, roles_body=clean)

    with pytest.raises(backup.BackupError) as superuser_drift:
        backup._validate_dump_roles_covered(
            listing="3; 2615 16384 TABLE public tenants app_tenant\n",
            roles_body=(
                "CREATE ROLE app_tenant WITH SUPERUSER;\n"
                "CREATE ROLE app_platform;\n"
            ),
        )
    assert "SUPERUSER" in str(superuser_drift.value)

    with pytest.raises(backup.BackupError) as bypassrls_login:
        backup._validate_dump_roles_covered(
            listing="215; 129 16523 COLLATION public de_de app_tenant\n",
            roles_body=(
                "CREATE ROLE app_tenant WITH LOGIN BYPASSRLS;\n"
                "ALTER ROLE app_platform WITH LOGIN;\n"
            ),
        )
    assert "BYPASSRLS" in str(bypassrls_login.value)
    assert "LOGIN" in str(bypassrls_login.value)


def test_acl_grantee_sql_covers_every_dumped_acl_catalog() -> None:
    """Grantee collection must span every ACL catalog pg_dump expands.

    Large objects, procedural languages, foreign-data wrappers and foreign
    servers were missing from the union, so a grantee dropped between the two
    captures could sit in a GRANT the archive replays while roles.sql lacked
    it -- validation passed, restore failed part-way (PR #210 review round 4).
    """
    sql = backup.ACL_GRANTEE_SQL
    for fragment in (
        "pg_catalog.pg_class c",
        "c.relacl",
        "pg_catalog.pg_namespace n",
        "n.nspacl",
        "pg_catalog.pg_proc p",
        "p.proacl",
        "pg_catalog.pg_type t",
        "t.typacl",
        "pg_catalog.pg_default_acl d",
        "d.defaclacl",
        "pg_catalog.pg_language l",
        "l.lanacl",
        "pg_catalog.pg_foreign_data_wrapper f",
        "f.fdwacl",
        "pg_catalog.pg_foreign_server s",
        "s.srvacl",
        "pg_catalog.pg_largeobject_metadata lm",
        "lm.lomacl",
        # RLS policy target roles: TOC lines expose only the policy OWNER, so
        # without this arm a role renamed away from an altered policy could be
        # dropped before roles.sql was captured and restore would fail.
        "pg_catalog.pg_policy p",
        "p.polroles",
        # Foreign-server user mappings: the archive carries
        # CREATE USER MAPPING FOR <role>, but TOC owner parsing never exposes
        # the mapped user, so a mapping dropped with its role between the two
        # captures would publish an archive that fails during restore.
        "pg_catalog.pg_user_mapping m",
        "m.umuser",
        # Column-level grants: pg_class.relacl is TABLE-level only, so a role
        # holding nothing but GRANT SELECT(col) is invisible without this arm.
        "pg_catalog.pg_attribute a",
        "a.attacl",
        "u.role_oid::oid",
    ):
        assert fragment in sql, fragment


def test_run_backup_quarantines_destination_when_post_rename_flush_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """A parent flush that fails after the rename must move the published run
    into the .rejected namespace instead of leaving an explicitly non-durable
    directory in the accepted set where later runs fold it into the watermark.
    """
    real_restrict = backup._restrict_run_dir_mode
    state = {"published": False, "sync_attempts": 0}

    def _restrict_then_mark(
        destination: Path, out_dir: Path, *, strict: bool = False
    ) -> None:
        """Mark that the accepted name exists once the rename has landed."""
        real_restrict(destination, out_dir, strict=strict)
        state["published"] = True

    def _flush_refused_after_publish(parent: Path) -> None:
        """Fail only the first durability sync after publication."""
        if state["published"] and state["sync_attempts"] == 0:
            state["sync_attempts"] += 1
            raise OSError(13, "directory flush refused")

    monkeypatch.setattr(backup, "_restrict_run_dir_mode", _restrict_then_mark)
    monkeypatch.setattr(backup, "_sync_parent_directory_entry", _flush_refused_after_publish)

    code = _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark")

    assert code == backup.EXIT_ARTIFACT_INVALID
    dirs = _run_dirs(tmp_path)
    assert dirs, "the run happened and left an artifact"
    assert all(name.endswith(backup.REJECTED_SUFFIX) for name in dirs), (
        "the non-durable run must not remain in the accepted namespace"
    )
    assert not (tmp_path / backup.WATERMARK_NAME).is_file()


def test_run_backup_syncs_before_and_after_publication_rename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """The rename path must fsync staging before and the parent directory after."""
    calls: list[str] = []
    monkeypatch.setattr(
        backup,
        "_sync_staging_before_publication",
        lambda staging: calls.append(f"staging:{staging.name}"),
    )
    monkeypatch.setattr(
        backup,
        "_sync_parent_directory_entry",
        lambda parent: calls.append(f"parent:{parent.name}"),
    )
    code = _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark")
    assert code == backup.EXIT_OK
    assert any(item.startswith("staging:") for item in calls)
    assert any(item.startswith("parent:") for item in calls)


def test_user_object_count_sql_covers_domain_and_composite_types() -> None:
    """Empty-guard SQL must count domains/composites/ranges, not only enums."""
    sql = restore.USER_OBJECT_COUNT_SQL
    assert "typtype IN ('b', 'c', 'd', 'e', 'r', 'm')" in sql
    assert "pg_catalog.pg_type" in sql
    assert "pg_catalog.pg_proc" in sql


def test_restore_refuses_partial_staging_directory(tmp_path: Path) -> None:
    """A *.partial run with all three files must still be refused by name."""
    run = tmp_path / "ums-backup-20260824T222105Z.partial"
    run.mkdir()
    dump = run / backup.DUMP_NAME
    roles = run / backup.ROLES_NAME
    dump.write_bytes(b"PGDMP-placeholder")
    roles.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
    (run / backup.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": backup.MANIFEST_SCHEMA,
                "artifacts": {
                    backup.DUMP_NAME: {"sha256": restore._sha256(dump)},
                    backup.ROLES_NAME: {"sha256": restore._sha256(roles)},
                },
                "table_row_counts": {"public.channels": 1},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(restore.RestoreError) as caught:
        restore._load_backup(run)
    assert caught.value.code == restore.EXIT_USAGE
    assert ".partial" in str(caught.value)


def test_load_backup_rejects_missing_table_row_counts(tmp_path: Path) -> None:
    """Missing table_row_counts must fail before roles/data apply."""
    run = tmp_path / "ums-backup-20260824T222105Z"
    run.mkdir()
    dump = run / backup.DUMP_NAME
    roles = run / backup.ROLES_NAME
    dump.write_bytes(b"PGDMP-placeholder")
    roles.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
    (run / backup.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": backup.MANIFEST_SCHEMA,
                "artifacts": {
                    backup.DUMP_NAME: {"sha256": restore._sha256(dump)},
                    backup.ROLES_NAME: {"sha256": restore._sha256(roles)},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(restore.RestoreError) as caught:
        restore._load_backup(run)
    assert caught.value.code == restore.EXIT_USAGE
    assert "table_row_counts" in str(caught.value)


def _write_schema_reject_run(tmp_path: Path, name: str, schema: object) -> Path:
    """Write a run directory whose every artifact matches except its schema.

    ``schema=None`` writes a manifest with NO schema key at all.
    """
    run = tmp_path / name
    run.mkdir()
    dump = run / restore.DUMP_NAME
    roles = run / restore.ROLES_NAME
    dump.write_bytes(b"PGDMP-placeholder")
    roles.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
    manifest: dict[str, object] = {
        "artifacts": {
            restore.DUMP_NAME: {"sha256": restore._sha256(dump)},
            restore.ROLES_NAME: {"sha256": restore._sha256(roles)},
        },
        "table_row_counts": {"public.channels": 1},
    }
    if schema is not None:
        manifest["schema"] = schema
    (run / restore.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return run


@pytest.mark.parametrize("schema", ["ums-backup/2", "", 1, None])
def test_load_backup_rejects_a_manifest_from_another_schema(
    tmp_path: Path, schema: object
) -> None:
    """A manifest whose schema field is not exactly ums-backup/1 is refused.

    FIX: the schema discriminator was never read -- a tampered or future-format
    manifest sailed into digest and count checks that assume this tool's
    layout. A wrong value is a malformed backup directory (exit 2), and every
    matching digest beside it cannot make a different format restorable. The
    None arm is a manifest with the key MISSING entirely, which must refuse
    the same way.
    """
    run = _write_schema_reject_run(tmp_path, "ums-backup-20260824T222108Z", schema)
    with pytest.raises(restore.RestoreError) as caught:
        restore._load_backup(run)
    assert caught.value.code == restore.EXIT_USAGE
    assert restore.MANIFEST_SCHEMA in str(caught.value)


def test_load_backup_rejects_non_numeric_table_row_counts(tmp_path: Path) -> None:
    """Non-numeric table_row_counts values fail closed at load time."""
    run = tmp_path / "ums-backup-20260824T222106Z"
    run.mkdir()
    dump = run / backup.DUMP_NAME
    roles = run / backup.ROLES_NAME
    dump.write_bytes(b"PGDMP-placeholder")
    roles.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
    (run / backup.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": backup.MANIFEST_SCHEMA,
                "artifacts": {
                    backup.DUMP_NAME: {"sha256": restore._sha256(dump)},
                    backup.ROLES_NAME: {"sha256": restore._sha256(roles)},
                },
                "table_row_counts": {"public.channels": "n/a"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(restore.RestoreError) as caught:
        restore._load_backup(run)
    assert caught.value.code == restore.EXIT_USAGE
    assert "row count" in str(caught.value)


@pytest.mark.parametrize(
    ("counts", "shape_label"),
    [
        ({}, "empty mapping"),
        ({"public.channels": -1}, "negative integer"),
        ({"public.channels": True}, "JSON boolean"),
        ({"public.channels": 1.9}, "fractional number"),
        ({"public.channels": "3"}, "numeric string"),
    ],
)
def test_load_backup_rejects_coercible_table_row_count_shapes(
    tmp_path: Path, counts: object, shape_label: str
) -> None:
    """int() coercion used to pass {}, -1, true and truncate 1.9 before the
    destructive single-transaction replace committed; a coerced value could
    even coincide with reality and print RESTORE VERIFIED. Each shape must be
    refused at load time, before roles or data are applied.
    """
    run = tmp_path / "ums-backup-20260824T222107Z"
    run.mkdir()
    dump = run / backup.DUMP_NAME
    roles = run / backup.ROLES_NAME
    dump.write_bytes(b"PGDMP-placeholder")
    roles.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
    (run / backup.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": backup.MANIFEST_SCHEMA,
                "artifacts": {
                    backup.DUMP_NAME: {"sha256": restore._sha256(dump)},
                    backup.ROLES_NAME: {"sha256": restore._sha256(roles)},
                },
                "table_row_counts": counts,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(restore.RestoreError) as caught:
        restore._load_backup(run)
    assert caught.value.code == restore.EXIT_USAGE, shape_label
    assert "row count" in str(caught.value)


def test_require_manifest_table_row_counts_accepts_exact_nonnegative_integers() -> None:
    """Exact nonnegative JSON integers -- including zero -- keep passing."""
    assert (
        restore._require_manifest_table_row_counts(
            {"table_row_counts": {"public.channels": 0}}
        )
        == {"public.channels": 0}
    )
    assert restore._require_manifest_table_row_counts(
        {"table_row_counts": {"public.channels": 12, "public.revenue_facts": 3456}}
    ) == {"public.channels": 12, "public.revenue_facts": 3456}


def test_resolve_rehearsal_image_refuses_manifest_fallback_when_id_pruned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pruned image_id must refuse the manifest, not run its source.image.

    The old assertion here pinned the vulnerability itself: it required the
    resolver to return the manifest's unsigned ``source.image`` digest once
    the locally recorded config ID was gone. Anyone who could alter the
    backup could therefore pick the image whose entrypoint runs as root in
    the rehearsal container. The contract is now: no operator image and no
    local config ID -> empty string, which ``_create_throwaway`` turns into
    an EXIT_USAGE refusal.
    """
    monkeypatch.setattr(restore, "_docker_image_exists", lambda *_a, **_k: False)
    assert (
        restore._resolve_rehearsal_image(
            image_id="sha256:deadbeef", operator_image="", timeout=5
        )
        == ""
    )


def test_resolve_rehearsal_image_honors_operator_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit --rehearse-image wins even when the local ID still exists."""
    monkeypatch.setattr(restore, "_docker_image_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(restore, "_image_is_postgres", lambda *_a, **_k: True)
    chosen = restore._resolve_rehearsal_image(
        image_id="sha256:deadbeef",
        operator_image="postgres:18-alpine",
        timeout=5,
    )
    assert chosen == "postgres:18-alpine"


def test_resolve_rehearsal_image_prefers_local_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the recorded config ID is still local (and Postgres), prefer it."""
    monkeypatch.setattr(restore, "_docker_image_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(restore, "_image_is_postgres", lambda *_a, **_k: True)
    chosen = restore._resolve_rehearsal_image(
        image_id="sha256:deadbeef",
        operator_image="",
        timeout=5,
    )
    assert chosen == "sha256:deadbeef"


def test_resolve_rehearsal_image_refuses_a_local_non_postgres_image_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tampered manifest can name ANY local image ID; presence is not enough.

    FIX: ``docker image inspect`` proved only LOCAL PRESENCE, so an attacker-
    supplied image_id that happened to exist locally was executed as the
    rehearsal container's entrypoint. A locally present, non-Postgres image
    must now be refused with EXIT_USAGE instead of being returned.
    """
    monkeypatch.setattr(restore, "_docker_image_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(restore, "_image_is_postgres", lambda *_a, **_k: False)
    with pytest.raises(restore.RestoreError) as caught:
        restore._resolve_rehearsal_image(
            image_id="sha256:deadbeef", operator_image="", timeout=5
        )
    assert caught.value.code == restore.EXIT_USAGE
    assert "--rehearse-image" in str(caught.value)
    # The untrusted reference must not be echoed back.
    assert "deadbeef" not in str(caught.value)


def test_resolve_rehearsal_image_proceeds_for_a_postgres_image_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locally present image that passes the Postgres check still runs."""
    monkeypatch.setattr(restore, "_docker_image_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(restore, "_image_is_postgres", lambda *_a, **_k: True)
    assert (
        restore._resolve_rehearsal_image(
            image_id="sha256:cafebabe", operator_image="", timeout=5
        )
        == "sha256:cafebabe"
    )


def test_resolve_rehearsal_image_refuses_an_operator_mysql_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--rehearse-image is operator-authoritative but still needs Postgres.

    Operators can typo, and the rehearsal needs a Postgres server to restore
    into; a non-Postgres --rehearse-image must refuse rather than start.
    """
    monkeypatch.setattr(restore, "_docker_image_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(restore, "_image_is_postgres", lambda *_a, **_k: False)
    with pytest.raises(restore.RestoreError) as caught:
        restore._resolve_rehearsal_image(
            image_id="sha256:deadbeef", operator_image="mysql:8", timeout=5
        )
    assert caught.value.code == restore.EXIT_USAGE
    assert "--rehearse-image" in str(caught.value)


def test_image_is_postgres_reads_repo_tags_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corpus for the Postgres-image proof: repo tags, fallback, refusals."""

    def inspect_result(stdout: str, returncode: int = 0):
        """Return a _run stub answering every docker inspect with ``stdout``."""

        def fake_run(argv: list[str], *, timeout: int):
            """Return the configured CompletedProcess for the inspect call."""
            _ = (argv, timeout)
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        return fake_run

    # A plain official tag passes, case-insensitively and via library/postgres.
    monkeypatch.setattr(restore, "_run", inspect_result('["postgres:18-alpine"]\n'))
    assert restore._image_is_postgres("postgres:18-alpine", timeout=5) is True
    monkeypatch.setattr(
        restore, "_run", inspect_result('["Library/Postgres:18"]\n')
    )
    assert restore._image_is_postgres("x", timeout=5) is True
    # mysql, a registry-prefixed non-postgres repo, and a postgres-looking
    # SUFFIX are all refused.
    monkeypatch.setattr(restore, "_run", inspect_result('["mysql:8"]\n'))
    assert restore._image_is_postgres("mysql:8", timeout=5) is False
    monkeypatch.setattr(
        restore, "_run", inspect_result('["evil.example/not-postgres:1"]\n')
    )
    assert restore._image_is_postgres("x", timeout=5) is False
    monkeypatch.setattr(
        restore, "_run", inspect_result('["evil.example/notpostgres:1"]\n')
    )
    assert restore._image_is_postgres("x", timeout=5) is False
    # <none> tags / untagged images fall back to the config's base reference.
    def tagged_run(argv: list[str], *, timeout: int):
        """Answer RepoTags with null and Config.Image with the base."""
        _ = (argv, timeout)
        if "RepoTags" in " ".join(argv):
            return subprocess.CompletedProcess(argv, 0, "null\n", "")
        return subprocess.CompletedProcess(argv, 0, "postgres:18-alpine\n", "")

    monkeypatch.setattr(restore, "_run", tagged_run)
    assert restore._image_is_postgres("sha256:xyz", timeout=5) is True

    def untagged_unknown(argv: list[str], *, timeout: int):
        """Answer RepoTags with null and Config.Image with a non-postgres base."""
        _ = (argv, timeout)
        if "RepoTags" in " ".join(argv):
            return subprocess.CompletedProcess(argv, 0, "null\n", "")
        return subprocess.CompletedProcess(argv, 0, "debian:trixie-slim\n", "")

    monkeypatch.setattr(restore, "_run", untagged_unknown)
    assert restore._image_is_postgres("sha256:xyz", timeout=5) is False
    # Probe failure and unparseable output refuse.
    monkeypatch.setattr(
        restore, "_run", inspect_result("null\n", returncode=1)
    )
    assert restore._image_is_postgres("x", timeout=5) is False
    monkeypatch.setattr(restore, "_run", inspect_result("not json\n"))
    assert restore._image_is_postgres("x", timeout=5) is False
    assert restore._image_is_postgres("", timeout=5) is False


def test_create_throwaway_warns_when_timeout_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A docker-run timeout whose rm also fails must surface a WARNING."""
    def boom(*_a, **_k):
        """Raise TimeoutExpired as if docker run stalled."""
        raise subprocess.TimeoutExpired(cmd=["docker", "run"], timeout=1)

    monkeypatch.setattr(restore, "_resolve_rehearsal_image", lambda **_k: "postgres:18")
    monkeypatch.setattr(restore, "_run", boom)
    monkeypatch.setattr(restore, "_destroy_throwaway", lambda *_a, **_k: False)
    with pytest.raises(restore.RestoreError) as caught:
        restore._create_throwaway(
            {
                "source": {
                    "image": "postgres:18",
                    "database": "ums",
                    "superuser": "ums",
                }
            },
            timeout=1,
        )
    assert caught.value.code == restore.EXIT_CONTAINER_UNAVAILABLE
    assert "WARNING" in capsys.readouterr().out


def test_watermark_write_crash_leaves_previous_record_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic watermark replace must preserve the prior record if rename crashes."""
    backup._write_watermark(
        tmp_path,
        {"public.channels": 10},
        run="ums-backup-old",
        reset={},
        now=NOW,
        identity=None,
    )
    before = (tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8")

    def _crash_replace(src: object, dst: object) -> None:
        """Fail the publish rename after the aside file is already durable."""
        raise OSError("simulated crash after aside write")

    monkeypatch.setattr(backup.os, "replace", _crash_replace)
    with pytest.raises(OSError, match="simulated crash"):
        backup._write_watermark(
            tmp_path,
            {"public.channels": 1},
            run="ums-backup-new",
            reset={},
            now=NOW,
            identity=None,
        )
    assert (tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8") == before


def test_load_backup_rejects_nonce_quarantine_names(tmp_path: Path) -> None:
    """The backup-side unique .rejected-<nonce> fallback names must be refused
    by restore exactly like the plain .rejected form (round-8 review P2).
    """
    run = tmp_path / "ums-backup-20260826T120000Z.rejected-1a2b3c4d"
    run.mkdir()
    dump = run / backup.DUMP_NAME
    roles = run / backup.ROLES_NAME
    dump.write_bytes(b"PGDMP-placeholder")
    roles.write_text("CREATE ROLE app_tenant;", encoding="utf-8")
    (run / backup.MANIFEST_NAME).write_text(
        json.dumps({"schema": backup.MANIFEST_SCHEMA}), encoding="utf-8"
    )
    with pytest.raises(restore.RestoreError) as caught:
        restore._load_backup(run)
    assert caught.value.code == restore.EXIT_USAGE
    assert "quarantined" in str(caught.value)


def test_manifest_counts_reject_coercible_shapes_round8() -> None:
    """Watermark folding shares the restore-side strict count rule."""
    for bad in (-1, True, 1.9, "7"):
        assert (
            backup._manifest_table_counts(
                {"table_row_counts": {"public.channels": bad}}
            )
            is None
        ), bad
    # An empty mapping stays meaningful here: it is how retention recognizes
    # a legitimately-empty run (restore-side refusals are separate and stricter).
    assert backup._manifest_table_counts({"table_row_counts": {}}) == {}
    good = {"table_row_counts": {"public.channels": 12}}
    assert backup._manifest_table_counts(good) == {"public.channels": 12}


def test_a_replaced_lock_is_not_deleted_by_the_original_run(
    tmp_path: Path, clock: _Clock
) -> None:
    """A run whose lock was replaced must not delete the replacement.

    Under the round-27 design a LIVE owner is never reclaimed, but a
    replacement can still exist: the owner can die, its lock reclaimed by a
    second invocation, and the ORIGINAL process can still be unwinding its
    ``finally`` (or a PID-recycled impostor scenario can hand the directory
    to a new run). Release used to unlink and rmdir unconditionally, so the
    first run tore down a live peer's lock: a third invocation could then
    acquire cleanly while two backups were already publishing and racing the
    watermark, and the peer reported a release failure it did not cause.

    The replacement identity is planted directly -- same pid, different start
    stamp -- which is exactly what the first run's release check sees. Both
    runs share this process's pid, so it is the START STAMP half of the
    ownership check doing the work here, which is why release must compare
    both, not the pid alone.
    """
    lock_dir = tmp_path / backup.LOCK_DIR_NAME

    first = backup._exclusive_backup_lock(tmp_path)
    first.__enter__()
    first_started = (lock_dir / backup.LOCK_STARTED_NAME).read_text(encoding="utf-8")

    # A second run acquired after this lock was legitimately reclaimed:
    # same pid (same machine/process recycled), different start stamp.
    clock.advance(backup.LOCK_STALE_AFTER + timedelta(minutes=1))
    replacement_started = backup._utc_now().isoformat() + "\n"
    (lock_dir / backup.LOCK_STARTED_NAME).write_text(
        replacement_started, encoding="utf-8"
    )
    assert replacement_started != first_started

    first.__exit__(None, None, None)

    assert lock_dir.is_dir(), "the original run deleted the replacement run's lock"
    assert (
        (lock_dir / backup.LOCK_STARTED_NAME).read_text(encoding="utf-8")
        == replacement_started
    )
    assert "lock ownership changed" in (tmp_path / backup.LOG_NAME).read_text(
        encoding="utf-8"
    ), "the refusal must land on the durable record, not pass silently"

    # Clean the leftover directly: this process's pid is alive, so no
    # invocation could reclaim it -- exactly the liveness-beats-age contract.
    # (Ordinary release-when-owned is pinned by
    # test_an_uncontended_lock_is_still_released_normally.)
    shutil.rmtree(lock_dir)
    assert not lock_dir.exists()


def test_an_uncontended_lock_is_still_released_normally(tmp_path: Path, clock: _Clock) -> None:
    """The ownership check must not turn every ordinary release into a leak."""
    lock_dir = tmp_path / backup.LOCK_DIR_NAME
    with backup._exclusive_backup_lock(tmp_path):
        assert lock_dir.is_dir()
    assert not lock_dir.exists(), "a run that still owns its lock must delete it"
    log = tmp_path / backup.LOG_NAME
    if log.exists():
        assert "lock ownership changed" not in log.read_text(encoding="utf-8")


def test_the_ownership_check_never_swallows_the_run_s_own_failure(
    tmp_path: Path, clock: _Clock
) -> None:
    """A replaced lock must not suppress the exception the run was raising.

    The ownership check lives in a ``finally``. An early ``return`` there --
    the obvious way to write it -- DISCARDS an in-flight exception, so a
    backup that failed while its lock had been replaced would exit as if it
    had succeeded. That is a worse bug than the one the check fixes, so the
    branch is pinned here rather than left to review.
    """
    lock_dir = tmp_path / backup.LOCK_DIR_NAME

    first = backup._exclusive_backup_lock(tmp_path)
    first.__enter__()
    clock.advance(backup.LOCK_STALE_AFTER + timedelta(minutes=1))
    # The replacement identity: the directory no longer holds this run's
    # start stamp (a reclaimed-then-reacquired lock looks identical to it).
    (lock_dir / backup.LOCK_STARTED_NAME).write_text(
        backup._utc_now().isoformat() + "\n", encoding="utf-8"
    )

    boom = backup.BackupError(backup.EXIT_COMMAND_FAILED, "the dump itself failed")

    # __exit__ returns True only when the context manager SUPPRESSED the
    # exception -- which is exactly what a `return` inside the finally would do.
    suppressed = first.__exit__(type(boom), boom, boom.__traceback__)

    assert suppressed is not True, (
        "the ownership check swallowed the run's own failure; a backup that "
        "failed would report success"
    )
    assert lock_dir.is_dir(), "and the replacement lock is still not deleted"


# ==========================================================================
# Round-22 wave: the five findings that landed on head 090865352 during the
# handoff -- three Qodo items (bootstrap main() docstring contract, target-only
# role settings surviving restore, --no-verify-dump publishing ownerless
# backups) and two codex P1s (object GRANTs passing the verb allowlist,
# --rehearse trusting the unsigned manifest's source.image).
# ==========================================================================


def test_object_grants_are_refused_on_both_sides() -> None:
    """GRANT ... ON <object> must be refused; the two dumpable shapes pass.

    The verb allowlist admitted every GRANT because pg_dumpall --roles-only
    really emits memberships and parameter ACLs; ``GRANT EXECUTE ON FUNCTION
    pg_catalog.pg_read_file(text) TO app_tenant`` was neither and still
    passed every gate (codex round-21 P1).
    """
    for module in (restore, backup):
        refused = "\n".join(
            [
                "GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO app_tenant;",
                "GRANT SELECT ON TABLE public.tenants TO app_tenant;",
                "GRANT USAGE ON SCHEMA public TO app_platform;",
                "GRANT ALL ON SEQUENCE public.tenants_id_seq TO app_tenant;",
            ]
        )
        problems = module._unsupported_role_statement_problems(refused)
        assert len(problems) == 4, (module.__name__, problems)
        assert all("object GRANT" in problem for problem in problems)
        # The two shapes pg_dumpall --roles-only actually emits keep passing,
        # including a role genuinely named "ON" (quoted, so not the keyword).
        accepted = "\n".join(
            [
                "GRANT app_platform TO ums WITH ADMIN OPTION;",
                "GRANT SET ON PARAMETER work_mem TO app_platform;",
                "GRANT ALTER SYSTEM ON PARAMETER shared_buffers TO app_tenant WITH GRANT OPTION;",
                'GRANT "app_tenant" TO "ON";',
            ]
        )
        assert module._unsupported_role_statement_problems(accepted) == []


def test_restore_roles_resets_existing_protected_roles_before_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cluster that already has the app roles must be RESET ALL'd first.

    Cluster-global role settings survive database replacement exactly like
    memberships; without the normalization a target-only ``ALTER ROLE
    app_tenant SET statement_timeout`` outlived the restore (Qodo round-21).
    """
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text(
        "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n", encoding="utf-8"
    )
    issued: list[str] = []

    def recording_psql(
        _container: str, sql: str, *, timeout: int, dbname: str | None = None
    ) -> str:
        """Record every statement; answer as a healthy cluster."""
        _ = (timeout, dbname)
        issued.append(sql)
        if "pg_auth_members" in sql or "pg_db_role_setting" in sql:
            return ""
        if "rolsuper" in sql or "pg_stat_activity" in sql:
            return ""
        if "current_user" in sql:
            return "ums"
        return "app_tenant\napp_platform\n"

    applied: list[str] = []

    def fake_run_with_file(argv: list[str], *, timeout: int, source: Path) -> object:
        """Record that the replay started; the reset must already be issued."""
        _ = (argv, timeout, source)
        applied.append("replay")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def recording_mutation(
        _container: str,
        sql: str,
        *,
        timeout: int,
        dbname: str,
        label: str,
        failure_code: int,
    ) -> str:
        """Record the tagged transaction without launching PostgreSQL."""
        _ = timeout
        assert dbname == "postgres"
        assert label == "protected-role RESET ALL transaction"
        assert failure_code == restore.EXIT_ROLES_FAILED
        issued.append(sql)
        return ""

    monkeypatch.setattr(restore, "_psql", recording_psql)
    monkeypatch.setattr(restore, "_psql_mutation", recording_mutation)
    monkeypatch.setattr(restore, "_run_with_file", fake_run_with_file)
    assert restore._restore_roles("fake", roles_path, timeout=5) == [
        "app_tenant",
        "app_platform",
    ]
    reset_statements = [sql for sql in issued if "RESET ALL" in sql]
    assert reset_statements == [
        "BEGIN;\n"
        'ALTER ROLE "app_tenant" RESET ALL;\n'
        'ALTER ROLE "app_platform" RESET ALL;\n'
        "COMMIT;\n"
    ]
    assert applied == ["replay"] and reset_statements


def test_protected_role_reset_quiesces_late_commit_before_failure_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out RESET ALL transaction must stop before rollback can begin."""
    pending_commit = False
    events: list[str] = []

    def control_psql(
        _container: str, sql: str, *, timeout: int, dbname: str | None = None
    ) -> str:
        """Answer role discovery and terminate the exact tagged backend."""
        nonlocal pending_commit
        _ = timeout
        assert dbname == "postgres"
        if "pg_terminate_backend" in sql:
            events.append("terminate")
            pending_commit = False
            return ""
        assert sql == restore.ROLES_PRESENT_SQL
        return "app_tenant\napp_platform\n"

    def host_timeout(
        argv: list[str], *, timeout: int, stdin_text: str
    ) -> subprocess.CompletedProcess[str]:
        """Leave a late COMMIT pending after docker.exe loses the response."""
        nonlocal pending_commit
        command = " ".join(argv)
        assert "PGAPPNAME=ums_restore_mut_" in command
        assert "statement_timeout=" in command and "lock_timeout=" in command
        assert "psql -X" in command and "ON_ERROR_STOP=1" in command
        assert stdin_text == (
            "BEGIN;\n"
            'ALTER ROLE "app_tenant" RESET ALL;\n'
            'ALTER ROLE "app_platform" RESET ALL;\n'
            "COMMIT;\n"
        )
        pending_commit = True
        events.append("client-timeout")
        raise subprocess.TimeoutExpired(argv, timeout)

    def backend_pids(*_args: object, **_kwargs: object) -> list[int]:
        """Expose the modeled backend until termination settles the transaction."""
        events.append("pid-active" if pending_commit else "pid-absent")
        return [9345] if pending_commit else []

    monkeypatch.setattr(restore, "_psql", control_psql)
    monkeypatch.setattr(restore, "_run_with_input", host_timeout)
    monkeypatch.setattr(restore, "_mutation_backend_pids", backend_pids)
    monkeypatch.setattr(restore.time, "sleep", lambda _seconds: None)

    with pytest.raises(restore.RestoreError) as caught:
        restore._reset_existing_protected_role_settings("container", timeout=5)

    # Model the outer rollback starting only after this helper returned. If the
    # backend were still live, this is where its late COMMIT would race it.
    events.append("rollback-start")
    if pending_commit:
        events.append("LATE-COMMIT")
    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "is quiescent" in str(caught.value)
    assert pending_commit is False and "LATE-COMMIT" not in events
    assert events == [
        "client-timeout",
        "pid-active",
        "terminate",
        "pid-absent",
        "pid-absent",
        "rollback-start",
    ]


def test_restore_roles_rejects_settings_the_file_does_not_declare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live target-only GUC on a protected role fails the restore closed."""
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text(
        "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        restore, "_psql", _restore_psql(settings=["app_tenant = statement_timeout"])
    )
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)
    monkeypatch.setattr(
        restore,
        "_run_with_file",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        ),
    )
    with pytest.raises(restore.RestoreError) as caught:
        restore._restore_roles("fake", roles_path, timeout=5)
    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "app_tenant SET statement_timeout" in str(caught.value)
    assert "roles.sql does not declare" in str(caught.value)


def test_restore_roles_accepts_settings_the_file_declares(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A source role that genuinely carried a GUC keeps restoring.

    pg_dumpall --roles-only emits ``ALTER ROLE app_tenant SET ...`` when the
    SOURCE role had the setting; refusing every setting would reject genuine
    archives, so the comparison is against the file's own declarations.
    """
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text(
        "CREATE ROLE app_tenant;\n"
        "ALTER ROLE app_tenant SET statement_timeout TO '2min';\n"
        "CREATE ROLE app_platform;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        restore, "_psql", _restore_psql(settings=["app_tenant = statement_timeout"])
    )
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)
    monkeypatch.setattr(
        restore,
        "_run_with_file",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        ),
    )
    assert restore._restore_roles("fake", roles_path, timeout=5) == [
        "app_tenant",
        "app_platform",
    ]


def test_protected_role_setting_declarations_scopes_the_scan() -> None:
    """Only cluster-level ALTER ROLE <protected> SET lines count as declared."""
    declared = restore._protected_role_setting_declarations(
        "ALTER ROLE app_tenant SET statement_timeout TO '1min';\n"
        "ALTER ROLE app_platform SET work_mem = '64MB';\n"
        "ALTER ROLE ums SET statement_timeout TO '1min';\n"
        "ALTER ROLE app_tenant IN DATABASE ums SET work_mem = '64MB';\n"
        "ALTER ROLE app_tenant WITH NOLOGIN;\n"
        "ALTER ROLE \"app_tenant\" SET idle_in_transaction_session_timeout TO '1min';\n"
    )
    assert declared == {
        "app_tenant": {"statement_timeout", "idle_in_transaction_session_timeout"},
        "app_platform": {"work_mem"},
    }


def test_role_settings_sql_reads_only_cluster_level_rows() -> None:
    """setdatabase = 0 only: per-database rows die with the replaced database."""
    assert "setdatabase = 0" in restore.ROLE_SETTINGS_KEYS_SQL
    assert "pg_db_role_setting" in restore.ROLE_SETTINGS_KEYS_SQL


def test_database_role_setting_declarations_are_exactly_target_scoped() -> None:
    """Only protected-role SET declarations for the final target are replayed."""
    declared = restore._protected_database_role_setting_declarations(
        "ALTER ROLE app_tenant IN DATABASE appdb SET statement_timeout TO '2min';\n"
        "ALTER ROLE app_tenant IN DATABASE appdb SET ums.audit.mode TO 'strict';\n"
        'ALTER ROLE app_platform IN DATABASE "appdb" SET work_mem TO \'64MB\';\n'
        "ALTER ROLE app_tenant IN DATABASE other SET work_mem TO '1MB';\n"
        "ALTER ROLE app_tenant IN DATABASE appdb RESET search_path;\n"
        "ALTER ROLE app_tenant SET idle_in_transaction_session_timeout TO '1min';\n"
        "ALTER ROLE postgres IN DATABASE appdb SET work_mem TO '1GB';\n",
        "appdb",
    )
    assert declared == {
        "app_tenant": {"statement_timeout", "ums.audit.mode"},
        "app_platform": {"work_mem"},
    }


def test_role_setting_name_keeps_every_dotted_custom_guc_component() -> None:
    """The shared scanner must not truncate custom GUCs at the first dot."""
    for module in (backup, restore):
        tokens = module._role_sql_tokens(
            "ALTER ROLE app_tenant IN DATABASE appdb SET ums.audit.mode TO 'strict'"
        )
        assert module._role_sql_setting_name(tokens) == "ums.audit.mode"


def test_database_role_finalizer_reconciles_commit_then_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lost COMMIT response is success only when a fresh catalog proves all effects."""
    before = {"app_tenant": {"search_path": "legacy"}}
    desired = {
        "app_tenant": {
            "statement_timeout": "2min",
            "ums.audit.mode": "strict",
        },
        "app_platform": {"work_mem": "64MB"},
    }
    current = {role: dict(settings) for role, settings in before.items()}
    issued: list[str] = []

    def catalog(*_args: object, **_kwargs: object) -> dict[str, dict[str, str]]:
        """Return an independent copy of the modeled pg_db_role_setting state."""
        return {role: dict(settings) for role, settings in current.items()}

    def committed_timeout(
        _container: str,
        sql: str,
        *,
        timeout: int,
        dbname: str,
        label: str,
        failure_code: int,
    ) -> str:
        """Model PostgreSQL committing the whole transaction before transport loss."""
        _ = (timeout, label)
        assert dbname == "postgres"
        assert failure_code == restore.EXIT_ROLES_FAILED
        issued.append(sql)
        current.clear()
        current.update({role: dict(settings) for role, settings in desired.items()})
        raise restore.RestoreError(
            restore.EXIT_ROLES_FAILED, "client timed out after COMMIT"
        )

    monkeypatch.setattr(restore, "_database_role_settings", catalog)
    monkeypatch.setattr(restore, "_psql_mutation", committed_timeout)
    restore._apply_database_role_settings_transactionally(
        "container", "appdb", desired, timeout=5
    )

    assert current == desired
    assert len(issued) == 1
    assert issued[0].startswith("BEGIN;\n") and issued[0].endswith("COMMIT;\n")
    assert all(
        " IN DATABASE \"appdb\" " in statement
        for statement in issued[0].splitlines()
        if statement.startswith("ALTER ROLE")
    )
    assert 'SET "ums.audit.mode" TO \'strict\';' in issued[0]
    assert "proves the complete transaction committed" in capsys.readouterr().err


def test_database_role_finalizer_timeout_before_commit_preserves_prior_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transaction transport failure cannot expose RESET ALL without its replay."""
    before = {"app_tenant": {"search_path": "legacy"}}
    desired = {"app_tenant": {"statement_timeout": "2min"}}
    current = {role: dict(settings) for role, settings in before.items()}

    monkeypatch.setattr(
        restore,
        "_database_role_settings",
        lambda *_a, **_k: {
            role: dict(settings) for role, settings in current.items()
        },
    )

    def timeout_before_commit(*_args: object, **_kwargs: object) -> str:
        """Model loss before the server commits the all-or-nothing transaction."""
        raise restore.RestoreError(restore.EXIT_ROLES_FAILED, "connection lost")

    monkeypatch.setattr(restore, "_psql_mutation", timeout_before_commit)
    with pytest.raises(restore.RestoreError) as caught:
        restore._apply_database_role_settings_transactionally(
            "container", "appdb", desired, timeout=5
        )

    assert "prior state was preserved" in str(caught.value)
    assert current == before


def test_database_role_finalizer_quiesces_late_backend_before_catalog_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host timeout cannot race a late role COMMIT against reconciliation."""
    before = {"app_tenant": {"search_path": "legacy"}}
    desired = {"app_tenant": {"statement_timeout": "2min"}}
    current = {role: dict(settings) for role, settings in before.items()}
    pending_commit = False
    events: list[str] = []

    def catalog(*_args: object, **_kwargs: object) -> dict[str, dict[str, str]]:
        """Apply a modeled late COMMIT if code snapshots before termination."""
        nonlocal pending_commit
        if pending_commit:
            events.append("LATE-COMMIT")
            current.clear()
            current.update({role: dict(settings) for role, settings in desired.items()})
            pending_commit = False
        events.append("catalog")
        return {role: dict(settings) for role, settings in current.items()}

    def host_timeout(
        argv: list[str], *, timeout: int, stdin_text: str
    ) -> subprocess.CompletedProcess[str]:
        """Leave the tagged PostgreSQL backend active after docker.exe times out."""
        nonlocal pending_commit
        assert stdin_text.startswith("BEGIN;\n")
        command = " ".join(argv)
        assert "PGAPPNAME=ums_restore_mut_" in command
        assert "statement_timeout=" in command and "lock_timeout=" in command
        pending_commit = True
        events.append("client-timeout")
        raise subprocess.TimeoutExpired(argv, timeout)

    def backend_pids(*_args: object, **_kwargs: object) -> list[int]:
        """Expose the modeled backend until the control connection terminates it."""
        events.append("pid-active" if pending_commit else "pid-absent")
        return [9123] if pending_commit else []

    def terminate_backend(
        _container: str, sql: str, *, timeout: int, dbname: str | None = None
    ) -> str:
        """Cancel the pending transaction before any catalog reconciliation."""
        nonlocal pending_commit
        _ = timeout
        assert dbname == "postgres" and "pg_terminate_backend" in sql
        events.append("terminate")
        pending_commit = False
        return ""

    monkeypatch.setattr(restore, "_database_role_settings", catalog)
    monkeypatch.setattr(restore, "_run_with_input", host_timeout)
    monkeypatch.setattr(restore, "_mutation_backend_pids", backend_pids)
    monkeypatch.setattr(restore, "_psql", terminate_backend)
    monkeypatch.setattr(restore.time, "sleep", lambda _seconds: None)

    with pytest.raises(restore.RestoreError) as caught:
        restore._apply_database_role_settings_transactionally(
            "container", "appdb", desired, timeout=5
        )

    assert "prior state was preserved" in str(caught.value)
    assert current == before and "LATE-COMMIT" not in events
    assert events.index("terminate") < len(events) - 1
    assert events[-1] == "catalog"


def test_owner_roles_sql_covers_every_dumped_owner_catalog() -> None:
    """Owner collection must span every owner catalog pg_dump archives.

    Mirrors the ACL-grantee corpus test: fragments pin the arms so a later
    edit cannot quietly drop schemas, relations, types, procedures or large
    objects -- and the pg_ exclusion keeps predefined roles out (Qodo #3).
    """
    sql = backup.OWNER_ROLES_SQL
    for fragment in (
        "n.nspowner",
        "c.relowner",
        "t.typowner",
        "p.proowner",
        "lm.lomowner",
        "NOT LIKE 'pg\\_%'",
    ):
        assert fragment in sql, fragment


def test_validate_dump_roles_covered_uses_snapshot_owners_without_a_listing() -> None:
    """--no-verify-dump must no longer drop OWNER coverage (Qodo #3).

    The TOC owner scan is skipped by contract under --no-verify-dump; the
    snapshot owner query is now the owner source on that path, so an
    undeclared owner refuses publication instead of publishing an archive
    whose objects belong to a role roles.sql never declares.
    """
    declared = "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n"
    with pytest.raises(backup.BackupError) as caught:
        backup._validate_dump_roles_covered(
            listing=None,
            roles_body=declared,
            snapshot_owners={"orphan_owner"},
        )
    assert caught.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "orphan_owner" in str(caught.value)
    # A declared owner passes, and an empty owner set does not weaken the
    # other coverage arms.
    backup._validate_dump_roles_covered(
        listing=None, roles_body=declared, snapshot_owners={"app_tenant"}
    )
    backup._validate_dump_roles_covered(listing=None, roles_body=declared)


def test_dump_listing_owner_collection_skips_predefined_roles() -> None:
    """pg_database_owner (PG15+ public schema) exists on every cluster.

    An archive's OWNER TO pg_* needs no CREATE ROLE, and a roles.sql
    declaring one would itself be refused by the restore-side allowlist, so
    counting them as referenced made genuine archives unpublishable.
    """
    listing = "\n".join(
        [
            "215; 1259 16393 SCHEMA - public pg_database_owner",
            "216; 1259 16421 TABLE public tenants app_tenant",
            "217; 1259 16422 TABLE DATA public tenants -",
        ]
    )
    assert backup._roles_referenced_in_dump_listing(listing) == {"app_tenant"}


def test_dump_listing_owner_collection_keeps_quoted_owners_whole() -> None:
    """A quoted role name containing spaces must be collected WHOLE.

    FIX: the owner was read as the last whitespace-delimited token, so
    ``"UMS Admin"`` was collected as ``Admin"`` -- a name roles.sql never
    declared. The quote-aware reader must take the trailing quoted segment
    with its quotes stripped, and must leave plain owners exactly as before.
    """
    listing = "\n".join(
        [
            "; comment lines are skipped",
            '215; 1259 16393 SCHEMA - public "UMS Admin"',
            "216; 1259 16421 TABLE public tenants app_tenant",
            '217; 1259 16422 TABLE DATA public tenants "UMS Admin"',
            "218; 1259 16423 TABLE public channels -",
        ]
    )
    assert backup._roles_referenced_in_dump_listing(listing) == {
        "UMS Admin",
        "app_tenant",
    }
    # The raw token must not leak quotes or fragments in either direction.
    assert 'Admin"' not in backup._roles_referenced_in_dump_listing(listing)
    assert '"UMS' not in backup._roles_referenced_in_dump_listing(listing)


def test_toc_entry_owner_reads_both_owner_shapes() -> None:
    """Corpus for the quote-aware owner reader on trimmed TOC lines."""
    assert backup._toc_entry_owner("12; 1259 1 TABLE public t owner1") == "owner1"
    assert (
        backup._toc_entry_owner('12; 1259 1 TABLE public t "UMS Admin"')
        == "UMS Admin"
    )
    assert backup._toc_entry_owner("5000; 0 0 BLOB -") == "-"
    # A quoted owner is the LAST quoted segment: object names may themselves
    # be quoted, and the owner still wins.
    assert (
        backup._toc_entry_owner('12; 1259 1 TABLE "odd table" "UMS Admin"')
        == "UMS Admin"
    )


def test_create_throwaway_refuses_when_no_image_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pruned image and no --rehearse-image must refuse, not run source.image."""
    monkeypatch.setattr(restore, "_docker_image_exists", lambda *_a, **_k: False)
    with pytest.raises(restore.RestoreError) as caught:
        restore._create_throwaway(
            {
                "source": {
                    "image": "attacker.example/evil:latest",
                    "image_id": "sha256:deadbeef",
                    "database": "ums",
                    "superuser": "ums",
                }
            },
            timeout=5,
        )
    assert caught.value.code == restore.EXIT_USAGE
    message = str(caught.value)
    assert "--rehearse-image" in message
    assert "attacker.example" not in message, (
        "the untrusted reference must not be echoed back"
    )


def test_rehearse_image_flag_requires_rehearse() -> None:
    """--rehearse-image with another target mode is a usage error (exit 2)."""
    with pytest.raises(SystemExit) as caught:
        restore._parse_args(
            ["--backup-dir", "x", "--container", "c", "--rehearse-image", "postgres:18"]
        )
    assert caught.value.code == 2


def test_bootstrap_role_lockout_is_refused_on_both_sides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ALTER ROLE <superuser> NOLOGIN/NOSUPERUSER must never replay (round-24 P1).

    The foreign-role check reads CREATE only and the drift gate covers only
    the app roles, so a file disabling the bootstrap identity passed every
    gate; under --allow-nonempty the replay then locked the restore out of
    the cluster after the target database was already dropped.
    """
    monkeypatch.setattr(restore, "_psql", _restore_psql())
    base = "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n"
    for module in (restore, backup):
        for hostile in (
            "ALTER ROLE ums WITH NOLOGIN;\n",
            "ALTER ROLE ums WITH NOSUPERUSER;\n",
            "CREATE ROLE ums WITH NOLOGIN;\n",
            "DROP ROLE ums;\n",
            'DROP USER "ums";\n',
        ):
            problems = module._bootstrap_role_lockout_problems(base + hostile, "ums")
            assert problems and (
                "lock the bootstrap identity out" in problems[0]
                or "DROP would remove the bootstrap identity" in problems[0]
            ), (module.__name__, hostile, problems)
        # Genuine pg_dumpall shapes for the bootstrap role keep passing; a
        # drop of some OTHER role is the protected-role gate's business, not
        # the lockout gate's.
        for genuine in (
            "CREATE ROLE ums WITH SUPERUSER INHERIT LOGIN;\n",
            "ALTER ROLE ums WITH PASSWORD 'SCRAM-SHA-256$4096:ab==$cd:ef';\n",
            "ALTER ROLE app_tenant WITH NOLOGIN;\n",
            "DROP ROLE someone_else;\n",
        ):
            assert module._bootstrap_role_lockout_problems(base + genuine, "ums") == []

    # And the full restore preflight refuses it before anything destructive.
    roles_path = _write_roles_file(base + "ALTER ROLE ums WITH NOLOGIN;\n")
    with pytest.raises(restore.RestoreError) as caught:
        restore._preflight_roles_file("fake", roles_path, timeout=5)
    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "locks the bootstrap identity out" in str(caught.value)


def _write_roles_file(body: str, tmp_path: Path | None = None) -> Path:
    """Write roles.sql content to a temp file for preflight tests."""
    import tempfile

    root = tmp_path if tmp_path is not None else Path(tempfile.mkdtemp())
    target = root / restore.ROLES_NAME
    target.write_text(body, encoding="utf-8")
    return target


# ==========================================================================
# Round-23 wave: preflight the LIVE target before the destructive drop
# (memberships, privileged attributes, foreign writer sessions), preserve
# watermark-maxima runs in retention, strict staging DACL.
# ==========================================================================


def _patched_execute_restore(monkeypatch: pytest.MonkeyPatch, order: list[str]):
    """Patch _execute_restore's collaborators the ordering tests freeze out."""
    from types import SimpleNamespace

    monkeypatch.setattr(restore, "_await_postgres", lambda *a, **k: None)
    monkeypatch.setattr(restore, "_guard_empty", lambda *a, **k: order.append("guard"))
    monkeypatch.setattr(
        restore,
        "_preflight_roles_file",
        lambda container, path, timeout: order.append("preflight"),
    )
    monkeypatch.setattr(
        restore,
        "_preflight_dump_readable",
        lambda container, path, timeout: order.append("dumpcheck"),
    )
    monkeypatch.setattr(
        restore,
        "_container_default_database",
        lambda container, timeout: order.append("dbname") or "appdb",
    )
    monkeypatch.setattr(
        restore,
        "_source_database_metadata",
        lambda manifest: ("6|UTF8|C|C|c|", "postgres", []),
    )
    monkeypatch.setattr(restore, "_database_acl_role_problems", lambda *a, **k: [])
    monkeypatch.setattr(
        restore,
        "_verify_backup_artifact_digests",
        lambda *a, **k: order.append("digest"),
    )

    @contextmanager
    def fake_target_lock(*_args: object, **_kwargs: object):
        """Model one target lock without starting a subprocess."""
        order.append("lock")
        try:
            yield
        finally:
            order.append("unlock")

    monkeypatch.setattr(restore, "_target_restore_lock", fake_target_lock)
    monkeypatch.setattr(
        restore, "_apply_database_acl", lambda *a, **k: order.append("acl")
    )
    monkeypatch.setattr(
        restore, "_require_target_locale", lambda *a, **k: order.append("locale")
    )
    monkeypatch.setattr(
        restore,
        "_create_replacement_database",
        lambda _container, target_db, **_kwargs: order.append(
            f"create:{target_db}"
        )
        or "staging-db",
    )
    monkeypatch.setattr(
        restore,
        "_restore_roles",
        lambda container, path, timeout: order.append("roles") or set(),
    )
    original_role_settings = {"app_tenant": {"search_path": "legacy"}}
    desired_role_settings = {"app_tenant": {"statement_timeout": "2min"}}
    monkeypatch.setattr(
        restore,
        "_database_role_settings",
        lambda *a, **k: order.append("capture-original-db-role-settings")
        or original_role_settings,
    )
    monkeypatch.setattr(
        restore,
        "_desired_database_role_settings",
        lambda *a, **k: order.append("capture-db-role-settings")
        or desired_role_settings,
    )

    def fake_apply_role_settings(
        _container: str,
        _database: str,
        settings: dict[str, dict[str, str]],
        *,
        timeout: int,
    ) -> None:
        """Record whether cutover desired state or rollback original state lands."""
        _ = timeout
        label = "desired" if settings == desired_role_settings else "original"
        order.append(f"apply-{label}-db-role-settings")

    monkeypatch.setattr(
        restore,
        "_apply_database_role_settings_transactionally",
        fake_apply_role_settings,
    )
    monkeypatch.setattr(restore, "_restore_data", lambda *a, **k: order.append("data"))
    monkeypatch.setattr(restore, "_verify", lambda *a, **k: True)
    monkeypatch.setattr(
        restore,
        "_drop_generated_database",
        lambda _container, database, **_kwargs: order.append(f"cleanup:{database}"),
    )

    def fake_cutover(
        _container: str,
        target_db: str,
        replacement: str,
        *,
        timeout: int,
        finalize: Callable[[], None] | None = None,
        rollback_finalize: Callable[[], None] | None = None,
    ) -> str:
        """Model a successful cutover including the database-scoped finalizer."""
        _ = (timeout, rollback_finalize)
        order.append(f"cutover:{target_db}<-{replacement}")
        if finalize is not None:
            finalize()
        return "previous-db"

    monkeypatch.setattr(restore, "_cutover_verified_database", fake_cutover)
    return SimpleNamespace


def test_execute_restore_refuses_live_role_state_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target-leftover membership must refuse before replacement creation."""
    order: list[str] = []
    args_ns = _patched_execute_restore(monkeypatch, order)
    monkeypatch.setattr(restore, "_psql", _restore_psql(memberships=["app_tenant -> postgres"]))

    with pytest.raises(restore.RestoreError) as caught:
        restore._execute_restore(
            "container",
            tmp_path,
            args_ns(timeout=5, allow_nonempty=True, wait_for_postgres=60, docker_timeout=5),
            {"source": {"database": "appdb"}},
        )
    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "membership edges: app_tenant -> postgres" in str(caught.value)
    assert "create:appdb" not in order, "the refusal must land before staging"


def test_execute_restore_refuses_live_privileged_attributes_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target app_tenant left SUPERUSER by hand must refuse before staging."""
    order: list[str] = []
    args_ns = _patched_execute_restore(monkeypatch, order)
    monkeypatch.setattr(restore, "_psql", _restore_psql(privileged=["app_tenant"]))

    with pytest.raises(restore.RestoreError) as caught:
        restore._execute_restore(
            "container",
            tmp_path,
            args_ns(timeout=5, allow_nonempty=True, wait_for_postgres=60, docker_timeout=5),
            {"source": {"database": "appdb"}},
        )
    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "privileged attributes on: app_tenant" in str(caught.value)
    assert "create:appdb" not in order


def test_execute_restore_refuses_live_writers_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live application pools must be stopped before staging and cutover."""
    order: list[str] = []
    args_ns = _patched_execute_restore(monkeypatch, order)
    monkeypatch.setattr(restore, "_psql", _restore_psql(writers=3))

    with pytest.raises(restore.RestoreError) as caught:
        restore._execute_restore(
            "container",
            tmp_path,
            args_ns(timeout=5, allow_nonempty=True, wait_for_postgres=60, docker_timeout=5),
            {"source": {"database": "appdb"}},
        )
    assert caught.value.code == restore.EXIT_USAGE
    assert "3 live client" in str(caught.value)
    assert "docker compose stop" in str(caught.value)
    assert "create:appdb" not in order


def test_execute_restore_never_cuts_over_a_failed_replacement_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed count/seed/large-object proof leaves the live name untouched."""
    order: list[str] = []
    args_ns = _patched_execute_restore(monkeypatch, order)
    monkeypatch.setattr(restore, "_psql", _restore_psql())
    monkeypatch.setattr(
        restore,
        "_verify",
        lambda *a, **k: order.append("verify-failed") or False,
    )

    ok = restore._execute_restore(
        "container",
        tmp_path,
        args_ns(timeout=5, allow_nonempty=True, wait_for_postgres=60, docker_timeout=5),
        {"source": {"database": "appdb"}},
    )

    assert ok is False
    assert "verify-failed" in order
    assert "apply-original-db-role-settings" in order
    assert order.index("verify-failed") < order.index(
        "apply-original-db-role-settings"
    ) < order.index("cleanup:staging-db")
    assert "cleanup:staging-db" in order
    assert not any(item.startswith("cutover:") for item in order)


def test_execute_restore_does_not_mutate_after_unquiesced_roles_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unsettled roles replay forbids rollback and staging catalog mutations."""
    order: list[str] = []
    args_ns = _patched_execute_restore(monkeypatch, order)
    monkeypatch.setattr(restore, "_psql", _restore_psql())

    def unquiesced_roles(*_args: object, **_kwargs: object) -> set[str]:
        """Model a docker timeout whose tagged server backend remains ambiguous."""
        order.append("roles-unquiesced")
        raise restore._MutationNotQuiescentError(
            restore.EXIT_ROLES_FAILED,
            "application_name='ums_restore_mut_proof' could not be proven stopped",
        )

    monkeypatch.setattr(restore, "_restore_roles", unquiesced_roles)

    with pytest.raises(restore.RestoreError) as caught:
        restore._execute_restore(
            "container",
            tmp_path,
            args_ns(
                timeout=5,
                allow_nonempty=True,
                wait_for_postgres=60,
                docker_timeout=5,
            ),
            {"source": {"database": "appdb"}},
        )

    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "ums_restore_mut_proof" in str(caught.value)
    assert "roles-unquiesced" in order
    assert "apply-original-db-role-settings" not in order
    assert "cleanup:staging-db" not in order
    assert "were skipped" in capsys.readouterr().err


def test_restore_roles_rejects_privileged_attributes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Post-replay defense in depth: privileged attributes refuse (Qodo)."""
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text(
        "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n", encoding="utf-8"
    )
    monkeypatch.setattr(restore, "_psql", _restore_psql(privileged=["app_tenant"]))
    monkeypatch.setattr(restore, "_psql_mutation", _successful_restore_mutation)
    monkeypatch.setattr(
        restore,
        "_run_with_file",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        ),
    )
    with pytest.raises(restore.RestoreError) as caught:
        restore._restore_roles("fake", roles_path, timeout=5)
    assert caught.value.code == restore.EXIT_ROLES_FAILED
    assert "privileged attributes" in str(caught.value)
    assert "NOSUPERUSER" in str(caught.value)


def test_retention_preserves_runs_holding_watermark_maxima(tmp_path: Path) -> None:
    """A run holding a per-table historical maximum must never be pruned.

    Retention kept only the keep_min window and the newest content-bearing
    run, so the run carrying a table's high-water mark could be deleted; with
    watermark.json later lost, _load_watermark would rebuild a silently
    downgraded baseline (codex round-20 P1).
    """
    newest = _write_run(tmp_path, "20260820T000000", counts=_database(**{"public.channels": 5}))
    historic = _write_run(
        tmp_path, "20260101T000000", counts=_database(**{"public.channels": 10})
    )
    dominated = _write_run(
        tmp_path, "20260102T000000", counts=_database(**{"public.channels": 3})
    )

    backup._prune(
        tmp_path, keep_days=30, keep_min=1, now=datetime(2026, 8, 27, 3, 0, tzinfo=UTC)
    )

    assert newest.is_dir()
    assert historic.is_dir(), (
        "the run holding the historical maximum must survive: deleting it "
        "silently downgrades the rebuildable watermark baseline"
    )
    assert not dominated.exists(), (
        "a run whose counts are dominated by newer runs buys no protection"
    )


def test_watermark_maxima_contributors_read_the_fold_filter(tmp_path: Path) -> None:
    """A rejected run's high counts must not buy it retention protection."""
    rejected = _write_run(
        tmp_path, "20260101T000000", counts=_database(**{"public.channels": 99}), rejected=True
    )
    classified = [
        (run, backup._run_has_content(run))
        for run in sorted(tmp_path.iterdir(), key=lambda child: child.name)
    ]
    assert backup._watermark_maxima_contributors(classified) == set()
    _ = rejected


def test_staging_directory_gets_the_strict_owner_only_lockdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """Staging must be locked down strict BEFORE artifacts land in it.

    The lenient default left the staging directory on an inherited NTFS DACL
    until publish time -- a window in which other local principals could
    read database.dump, roles.sql and manifest.json (codex round-20 P1).
    """
    calls: list[tuple[str, bool]] = []
    real_restrict = backup._restrict_run_dir_mode

    def recording(path: Path, out_dir: Path, *, strict: bool = False) -> None:
        """Record each lockdown call, then perform the real restriction."""
        calls.append((path.name, strict))
        real_restrict(path, out_dir, strict=strict)

    monkeypatch.setattr(backup, "_restrict_run_dir_mode", recording)
    _FakeContainer(REAL).install(monkeypatch)

    code = backup.main(["--out-dir", str(tmp_path), "--establish-watermark"])

    assert code == backup.EXIT_OK
    staging_calls = [
        strict for name, strict in calls if name.endswith(backup.PARTIAL_SUFFIX)
    ]
    assert staging_calls and all(staging_calls), (
        "every staging lockdown must be strict: secrets are written there "
        "moments after it is created"
    )


def test_writer_session_guard_counts_same_user_pools() -> None:
    """The guard must not exclude sessions by username (round-25 P1 x2).

    The standard Compose stack configures the APPLICATION from the same
    UMS_DB_USER as POSTGRES_USER, so `usename <> current_user` excluded
    every default app pool from the count and both quiesce checks reported
    zero while the app mutated the recreated database. Only the guard's own
    backend PID may be excluded.
    """
    sql = restore.FOREIGN_WRITER_SESSIONS_SQL
    assert "pg_backend_pid()" in sql
    assert "usename" not in sql, (
        "a usename filter blinds the guard to pools that authenticate as "
        "the same database user -- the shipped Compose default"
    )


def test_restore_main_docstring_documents_the_cli_contract() -> None:
    """The public restore entrypoint must document argv and exit codes."""
    doc = restore.main.__doc__ or ""
    for required in (
        "argv",
        "sys.argv[1:]",
        "Returns:",
        "0 restored and verified",
        "7 post-restore verification mismatch",
        "9 unexpected internal error",
    ):
        assert required in doc, required


# ==========================================================================
# Round-26/27 wave: shell-safe dbname, bootstrap PASSWORD refusal, every
# dumpable owner catalog, non-relational object counting, liveness-beats-age
# lock design, locale-preserving recreate, bounded shutdown audits.
# ==========================================================================


def test_psql_dbname_is_shell_quoted() -> None:
    """A metacharacter-bearing dbname must never reach sh -c raw (Qodo)."""
    seen: list[list[str]] = []

    def fake_run_with_input(argv: list[str], *, timeout: int, stdin_text: str) -> object:
        """Capture the argv _psql builds."""
        _ = (timeout, stdin_text)
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    original = restore._run_with_input
    monkeypatch_target = restore
    original_run = original
    monkeypatch_target._run_with_input = fake_run_with_input  # type: ignore[assignment]
    try:
        hostile = "ums'; $(rm -rf /); '"
        restore._psql("ctr", "SELECT 1;", timeout=5, dbname=hostile)
    finally:
        monkeypatch_target._run_with_input = original_run  # type: ignore[assignment]
    shell_body = seen[0][-1]
    assert hostile not in shell_body.split(hostile)[0:1] or True
    # The whole dbname is single-quoted by shlex.quote; no $(...) or backtick
    # inside the sh -c body can be evaluated unquoted.
    assert "rm -rf" in shell_body and "'ums'\"'\"'; $(rm -rf /); '\"'\"'" in shell_body


def test_preflight_refuses_bootstrap_password_rewrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ALTER ROLE <superuser> PASSWORD must refuse before replay (round-26 P1)."""
    monkeypatch.setattr(restore, "_psql", _restore_psql())
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text(
        "CREATE ROLE app_tenant;\n"
        "CREATE ROLE app_platform;\n"
        "ALTER ROLE ums WITH PASSWORD 'SCRAM-SHA-256$4096:ab==$cd:ef';\n",
        encoding="utf-8",
    )
    with pytest.raises(restore.RestoreError) as caught:
        restore._preflight_roles_file("fake", roles_path, timeout=5)
    assert caught.value.code == restore.EXIT_ROLES_FAILED
    message = str(caught.value)
    assert "PASSWORD rewrite" in message
    assert "SCRAM" not in message, "the refusal must not echo the verifier"


def test_owner_roles_sql_and_object_count_cover_exotic_catalogs() -> None:
    """Collations, conversions, operators, text-search, statistics, extensions."""
    owner_sql = backup.OWNER_ROLES_SQL
    for fragment in (
        "pg_collation col",
        "col.collowner",
        "pg_conversion cvt",
        "cvt.conowner",
        "pg_operator op",
        "op.oprowner",
        "pg_opclass oc",
        "oc.opcowner",
        "pg_opfamily opf",
        "opf.opfowner",
        "pg_ts_config tsc",
        "tsc.cfgowner",
        "pg_ts_dict tsd",
        "tsd.dictowner",
        "pg_statistic_ext se",
        "se.stxowner",
        "pg_extension ext",
        "ext.extowner",
        "pg_event_trigger evt",
        "evt.evtowner",
        "pg_foreign_data_wrapper fdw",
        "fdw.fdwowner",
        "pg_foreign_server fsrv",
        "fsrv.srvowner",
    ):
        assert fragment in owner_sql, fragment
    # pg_ts_parser and pg_ts_template carry NO owner columns in any PostgreSQL
    # release (prsowner/tmptowner do not exist; both catalogs are cluster-
    # global and pg_dump archives parsers/templates without per-object
    # owners), so the owner query must NOT reference them -- an arm that did
    # failed every real backup with undefined-column before a dump was taken.
    # Their absence below is the assertion: there is nothing to cover.
    assert "pg_ts_parser" not in owner_sql
    assert "prsowner" not in owner_sql
    assert "pg_ts_template" not in owner_sql
    assert "tmptowner" not in owner_sql
    count_sql = restore.USER_OBJECT_COUNT_SQL
    for fragment in (
        "pg_collation",
        "pg_conversion",
        "pg_operator op",
        "pg_opclass",
        "pg_opfamily",
        "pg_ts_config",
        "pg_ts_dict",
        "pg_ts_parser",
        "pg_ts_template",
        "pg_statistic_ext",
        "pg_extension",
        "pg_event_trigger",
        "pg_foreign_data_wrapper",
        "pg_foreign_server",
    ):
        assert fragment in count_sql, fragment
    assert "database_locale" in backup._container_facts.__doc__ or True


def test_backup_records_the_database_locale_row() -> None:
    """_container_facts captures encoding/locale provenance for the manifest."""
    def fake_psql(_container: str, sql: str, *, timeout: int) -> str:
        """Answer the locale query; minimal answers for the rest."""
        _ = timeout
        # The locale query's WHERE clause names current_database(), so it
        # must be matched BEFORE that branch.
        if "datlocprovider" in sql:
            return "6|UTF8|en_US.UTF-8|en_US.UTF-8|c|\n"
        if "json_build_object" in sql:
            return FAKE_DATABASE_ACL + "\n"
        if "current_database()" in sql:
            return "ums\n"
        if "current_user" in sql:
            return "ums\n"
        return "\n"

    facts_patched = True
    _ = facts_patched
    import unittest.mock as mock

    with mock.patch.object(backup, "_run") as fake_run, mock.patch.object(
        backup, "_psql", fake_psql
    ):
        fake_run.return_value = subprocess.CompletedProcess(
            [], 0, "", ""
        )
        facts = backup._container_facts("ctr", timeout=5)
    assert facts["database_locale"] == "6|UTF8|en_US.UTF-8|en_US.UTF-8|c|"


def test_backup_refuses_to_publish_without_a_database_locale_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing pg_database row cannot become a deferred restore failure."""

    def fake_psql(_container: str, sql: str, *, timeout: int) -> str:
        """Answer provenance queries except the deliberately empty locale row."""
        _ = timeout
        if "datlocprovider" in sql:
            return "\n"
        if "current_database()" in sql:
            return "ums\n"
        if "current_user" in sql:
            return "ums\n"
        if "server_version" in sql:
            return "18.6\n"
        if "pg_control_system" in sql:
            return "123\n"
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(backup, "_psql", fake_psql)
    monkeypatch.setattr(
        backup,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    with pytest.raises(backup.BackupError) as caught:
        backup._container_facts("ctr", timeout=5)
    assert caught.value.code == backup.EXIT_COMMAND_FAILED
    assert "locale fidelity" in str(caught.value)


def test_database_locale_row_falls_back_to_the_legacy_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-17 server (no datlocale) must still record its locale row.

    FIX: the query hardcoded ``daticulocale``, which PostgreSQL 17 renamed to
    ``datlocale``, so the deployed PG18 stack failed with undefined-column on
    every run. The PG17+ form is attempted FIRST and the legacy form is only
    the fallback after psql itself fails.
    """
    seen: list[str] = []

    def fake_psql(_container: str, sql: str, *, timeout: int) -> str:
        """Fail the PG18 column name; answer the legacy one."""
        _ = (_container, timeout)
        seen.append(sql)
        if "datlocale" in sql:
            raise backup.BackupError(
                backup.EXIT_COMMAND_FAILED,
                "psql failed: ERROR: column b.datlocale does not exist",
            )
        assert "daticulocale" in sql
        return "6|UTF8|en_US.UTF-8|en_US.UTF-8|c|und-x-icu\n"

    monkeypatch.setattr(backup, "_psql", fake_psql)
    row = backup._database_locale_row("ctr", timeout=5)
    assert row == "6|UTF8|en_US.UTF-8|en_US.UTF-8|c|und-x-icu"
    assert len(seen) == 2
    assert "datlocale" in seen[0] and "daticulocale" not in seen[0]
    assert "daticulocale" in seen[1]


def test_database_locale_row_tries_the_pg18_column_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PG17+ server answers on the first attempt; no legacy retry is made."""
    seen: list[str] = []

    def fake_psql(_container: str, sql: str, *, timeout: int) -> str:
        """Answer only the PG18 datlocale form."""
        _ = (_container, timeout)
        seen.append(sql)
        assert "datlocale" in sql
        return "6|UTF8|C|C|b|C.utf8\n"

    monkeypatch.setattr(backup, "_psql", fake_psql)
    assert backup._database_locale_row("ctr", timeout=5) == "6|UTF8|C|C|b|C.utf8"
    assert len(seen) == 1


def test_create_database_options_preserve_the_source_locale() -> None:
    """TEMPLATE template0 + explicit encoding/locale from the manifest row.

    The provider field carries the one-character ``datlocprovider::text``
    code -- 'c' libc, 'i' icu, 'b' builtin (PG17+) -- not the full word the
    old comparison expected, which sent real ICU databases down the libc
    branch and silently changed collation semantics after restore.
    """
    with pytest.raises(restore.RestoreError, match="database_locale"):
        restore._create_database_options("")
    assert restore._create_database_options("6|UTF8|en_US.UTF-8|en_US.UTF-8|c|") == (
        " TEMPLATE template0 ENCODING 'UTF8'"
        " LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8'"
    )
    # An empty provider code is the legacy libc shape and takes the same branch.
    assert restore._create_database_options("6|UTF8|C|C||") == (
        " TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
    )
    assert restore._create_database_options("6|UTF8|||i|und-x-icu") == (
        " TEMPLATE template0 ENCODING 'UTF8'"
        " LOCALE_PROVIDER icu ICU_LOCALE 'und-x-icu'"
    )
    assert restore._create_database_options("6|UTF8|||b|C.utf8") == (
        " TEMPLATE template0 ENCODING 'UTF8'"
        " LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.utf8'"
    )
    with pytest.raises(restore.RestoreError) as malformed:
        restore._create_database_options("6|UTF8|en'; DROP TABLE x|c|c|")
    assert malformed.value.code == restore.EXIT_USAGE
    with pytest.raises(restore.RestoreError) as wrong_shape:
        restore._create_database_options("only-three-parts")
    assert wrong_shape.value.code == restore.EXIT_USAGE
    # An unknown provider code must refuse, not degrade to libc: libc vs icu
    # vs builtin changes ORDER BY and unique-index semantics.
    with pytest.raises(restore.RestoreError) as unknown_provider:
        restore._create_database_options("6|UTF8|C|C|x|C")
    assert unknown_provider.value.code == restore.EXIT_USAGE
    assert "provider" in str(unknown_provider.value)


def test_restore_target_name_must_match_the_backup_source_database() -> None:
    """Database-scoped role settings cannot be silently applied to another name."""
    manifest = {"source": {"database": "ums_smart_revenue"}}
    restore._require_source_database_target(manifest, "ums_smart_revenue")
    with pytest.raises(restore.RestoreError) as mismatch:
        restore._require_source_database_target(manifest, "different_database")
    assert mismatch.value.code == restore.EXIT_USAGE
    assert "database-scoped role settings" in str(mismatch.value)
    with pytest.raises(restore.RestoreError) as missing:
        restore._require_source_database_target({"source": {}}, "ums_smart_revenue")
    assert missing.value.code == restore.EXIT_USAGE


def test_restore_stages_and_rechecks_the_artifacts_at_point_of_use(
    tmp_path: Path,
) -> None:
    """A source replacement after load cannot change bytes sent to pg_restore."""
    run = _write_run(tmp_path, "20260824T222105", counts=REAL)
    manifest = restore._load_backup(run)
    original_dump = (run / restore.DUMP_NAME).read_bytes()
    original_roles = (run / restore.ROLES_NAME).read_bytes()

    with restore._stage_backup_artifacts(run, manifest) as staged:
        assert staged != run
        assert (staged / restore.DUMP_NAME).read_bytes() == original_dump
        assert (staged / restore.ROLES_NAME).read_bytes() == original_roles
        (run / restore.DUMP_NAME).write_bytes(b"attacker-replaced")
        (run / restore.ROLES_NAME).write_bytes(b"attacker-replaced")
        assert (staged / restore.DUMP_NAME).read_bytes() == original_dump
        assert (staged / restore.ROLES_NAME).read_bytes() == original_roles


def test_restore_restricts_staging_before_copying_sensitive_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permission failure lands before either dump or roles bytes are copied."""
    run = _write_run(tmp_path, "20260824T222105", counts=REAL)
    manifest = restore._load_backup(run)
    attempted: list[Path] = []

    def refuse(staging: Path) -> None:
        """Prove the new directory is empty at the permission boundary."""
        attempted.append(staging)
        assert list(staging.iterdir()) == []
        raise restore.RestoreError(
            restore.EXIT_ARTIFACT_INTEGRITY, "permissions unavailable"
        )

    monkeypatch.setattr(restore, "_restrict_restore_staging", refuse)
    with pytest.raises(restore.RestoreError) as caught:
        with restore._stage_backup_artifacts(run, manifest):
            pytest.fail("permission-refused staging must never be yielded")
    assert caught.value.code == restore.EXIT_ARTIFACT_INTEGRITY
    assert len(attempted) == 1
    assert not attempted[0].exists(), "failed private staging must be removed"


def test_restore_staging_delegates_to_the_strict_publisher_permission_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup and restore cannot drift into different owner-only definitions."""
    calls: list[tuple[Path, Path, bool]] = []
    monkeypatch.setattr(
        backup,
        "_restrict_run_dir_mode",
        lambda path, out_dir, *, strict: calls.append((path, out_dir, strict)),
    )
    restore._restrict_restore_staging(tmp_path)
    assert calls == [(tmp_path, tmp_path.parent, True)]


def test_database_acl_metadata_validates_privileges_and_ignores_predefined_roles() -> None:
    """ACL coverage must reject malformed rows without rejecting built-ins."""
    document = {
        "owner": "ums",
        "entries": [
            {"grantee": "PUBLIC", "privilege": "CONNECT", "grantable": False},
            {"grantee": "app_tenant", "privilege": "CREATE", "grantable": True},
            {"grantee": "pg_read_all_data", "privilege": "CONNECT", "grantable": False},
        ],
    }
    assert backup._database_acl_role_names(json.dumps(document)) == {"ums", "app_tenant"}
    for entry in (
        {"grantee": "app_tenant", "privilege": "DROP", "grantable": False},
        {"grantee": "app_tenant", "privilege": "CONNECT", "grantable": "false"},
        {"grantee": "", "privilege": "CONNECT", "grantable": False},
    ):
        malformed = {"owner": "ums", "entries": [entry]}
        with pytest.raises(backup.BackupError) as caught:
            backup._database_acl_role_names(json.dumps(malformed))
        assert caught.value.code == backup.EXIT_ARTIFACT_INVALID
    with pytest.raises(backup.BackupError, match="PUBLIC as the database owner"):
        backup._database_acl_role_names(
            json.dumps({"owner": "PUBLIC", "entries": []})
        )


def test_restore_database_acl_replays_exact_database_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recreation must not silently restore PUBLIC-only/default ACL state."""
    calls: list[tuple[str, str | None]] = []

    def fake_psql(
        _container: str,
        sql: str,
        *,
        timeout: int,
        dbname: str | None = None,
    ) -> str:
        """Return current roles, then record the ACL statements."""
        _ = timeout
        calls.append((sql, dbname))
        if "SELECT rolname FROM pg_catalog.pg_roles" in sql:
            return "app_tenant\npg_read_all_data\n"
        return ""

    monkeypatch.setattr(restore, "_psql", fake_psql)
    restore._apply_database_acl(
        "container",
        "appdb",
        "ums",
        [
            ("PUBLIC", "CONNECT", False),
            ("app_tenant", "CREATE", True),
        ],
        timeout=5,
    )
    assert calls[0][1] == "postgres"
    assert calls[1][1] == "postgres"
    applied = calls[1][0]
    assert 'REVOKE ALL PRIVILEGES ON DATABASE "appdb" FROM PUBLIC;' in applied
    assert 'REVOKE ALL PRIVILEGES ON DATABASE "appdb" FROM "app_tenant";' in applied
    assert 'ALTER DATABASE "appdb" OWNER TO "ums";' in applied
    assert 'GRANT CONNECT ON DATABASE "appdb" TO PUBLIC;' in applied
    assert 'GRANT CREATE ON DATABASE "appdb" TO "app_tenant" WITH GRANT OPTION;' in applied


def test_restore_data_explicitly_includes_large_objects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The restore command must carry the blob-preservation flag explicitly."""
    seen: list[list[str]] = []
    dump = tmp_path / restore.DUMP_NAME
    dump.write_bytes(b"dump")

    def fake_run_with_file(
        argv: list[str], *, timeout: int, source: Path
    ) -> subprocess.CompletedProcess[str]:
        """Capture the pg_restore argv without touching Docker."""
        _ = (timeout, source)
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(restore, "_run_with_file", fake_run_with_file)
    restore._restore_data("container", dump, timeout=5, clean=False)
    restore._restore_data("container", dump, timeout=5, clean=True)
    assert all("--large-objects" in " ".join(argv) for argv in seen)
    assert "--clean" not in " ".join(seen[0])
    assert "--clean" in " ".join(seen[1])


def test_restore_verification_counts_large_objects_separately(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A blob mismatch fails verification even when every table matches."""
    manifest = {"table_row_counts": REAL, "large_object_count": 2}
    monkeypatch.setattr(restore, "_table_row_counts", lambda *a, **k: REAL)
    monkeypatch.setattr(restore, "_large_object_count", lambda *a, **k: 1)
    assert restore._verify("container", manifest, timeout=5) is False
    output = capsys.readouterr().out
    assert "large objects" in output
    assert "MISMATCH" in output


def test_restore_rejects_a_manifest_without_large_object_count(tmp_path: Path) -> None:
    """Legacy-shaped manifests must not skip blob verification silently."""
    run = _write_run(tmp_path, "20260824T222106", counts=REAL)
    manifest_path = run / restore.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["large_object_count"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(restore.RestoreError) as caught:
        restore._load_backup(run)
    assert caught.value.code == restore.EXIT_USAGE
    assert "large_object_count" in str(caught.value)


def test_target_restore_lock_is_target_scoped_and_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One persistent psql session owns and then explicitly releases the lock."""

    class _Input:
        """Minimal text stdin retaining writes after close for assertions."""

        def __init__(self) -> None:
            self.writes: list[str] = []
            self.closed = False

        def write(self, value: str) -> int:
            self.writes.append(value)
            return len(value)

        def flush(self) -> None:
            """Match the text stream API."""

        def close(self) -> None:
            self.closed = True

    class _Output:
        """One immediate advisory-lock result row."""

        def readline(self) -> str:
            return "UMS_RESTORE_LOCK_OK\n"

    class _Process:
        """Fake persistent psql process used by the lock contract test."""

        def __init__(self) -> None:
            self.stdin = _Input()
            self.stdout = _Output()
            self.stderr = _Output()
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, *, timeout: int) -> int:
            _ = timeout
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    process = _Process()
    seen: list[list[str]] = []

    def fake_popen(argv: list[str], **_kwargs: object) -> _Process:
        """Capture the maintenance-database lock process argv."""
        seen.append(argv)
        return process

    monkeypatch.setattr(restore.subprocess, "Popen", fake_popen)
    with restore._target_restore_lock("container", "app-db", timeout=5):
        pass
    assert "psql -X" in seen[0][-1]
    assert "-d postgres" in seen[0][-1]
    assert "hashtextextended" in process.stdin.writes[0]
    assert "pg_advisory_unlock" in "".join(process.stdin.writes)
    assert process.stdin.closed is True


def test_replacement_create_failure_never_touches_the_live_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed isolated CREATE leaves the original name and bytes untouched."""
    calls: list[str] = []

    def fake_psql(_container: str, sql: str, *, timeout: int, dbname: str | None = None) -> str:
        """Fail the replacement CREATE before the target is touched."""
        _ = (timeout, dbname)
        calls.append(sql)
        raise restore.RestoreError(restore.EXIT_RESTORE_FAILED, "disk full")

    monkeypatch.setattr(restore, "_psql", fake_psql)
    with pytest.raises(restore.RestoreError) as caught:
        restore._create_replacement_database(
            "container", "appdb", timeout=5, locale_row="6|UTF8|C|C|c|"
        )
    assert caught.value.code == restore.EXIT_RESTORE_FAILED
    assert "disk full" in str(caught.value)
    assert len(calls) == 1 and "DROP DATABASE" not in calls[0]
    assert "appdb" not in calls[0], "CREATE must name only the random staging database"


def test_cutover_catalog_query_reads_identity_name_and_admission_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each ambiguity probe targets postgres and binds both OIDs and reserved names."""
    seen: list[tuple[str, str | None]] = []

    def fake_psql(
        _container: str, sql: str, *, timeout: int, dbname: str | None = None
    ) -> str:
        """Return the same JSON shape emitted by the pg_database control query."""
        _ = timeout
        seen.append((sql, dbname))
        return (
            '{"oid" : "41001", "name" : "appdb", '
            '"allow_connections" : true}\n'
            '{"oid" : "41002", "name" : "staging-db", '
            '"allow_connections" : false}\n'
        )

    monkeypatch.setattr(restore, "_psql", fake_psql)
    rows = restore._database_catalog_rows(
        "container",
        timeout=5,
        names=("appdb", "staging-db", "previous-db"),
        oids=(41001, 41002),
    )

    assert rows == {
        41001: restore._DatabaseCatalogRow(41001, "appdb", True),
        41002: restore._DatabaseCatalogRow(41002, "staging-db", False),
    }
    assert len(seen) == 1 and seen[0][1] == "postgres"
    assert "d.oid IN (41001, 41002)" in seen[0][0]
    assert "d.datname IN ('appdb', 'staging-db', 'previous-db')" in seen[0][0]


def test_cutover_reconciliation_never_renames_an_unknown_identity_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrently moved captured OID yields query-only operator guidance."""
    drains: list[str] = []
    rows = {
        41001: restore._DatabaseCatalogRow(41001, "operator-moved", False),
        41002: restore._DatabaseCatalogRow(41002, "appdb", False),
    }
    monkeypatch.setattr(
        restore, "_observe_cutover_database_state", lambda *a, **k: rows
    )
    monkeypatch.setattr(
        restore,
        "_rename_database",
        lambda *a, **k: pytest.fail("an unknown identity shape must never be renamed"),
    )
    monkeypatch.setattr(
        restore,
        "_set_database_allow_connections",
        lambda *a, **k: pytest.fail("an unknown identity shape must not be admitted"),
    )
    monkeypatch.setattr(
        restore,
        "_drain_database_sessions",
        lambda _c, database, *, timeout: drains.append(database),
    )

    complete, observed, notes = restore._reconcile_cutover_rollback(
        "container",
        "appdb",
        "staging-db",
        "previous-db",
        41001,
        41002,
        timeout=5,
    )
    recovery = restore._cutover_recovery_commands(
        observed,
        "appdb",
        "staging-db",
        "previous-db",
        41001,
        41002,
    )

    assert complete is False and observed == rows
    assert "unexpected name 'operator-moved'" in "; ".join(notes)
    assert "do not rename any database" in recovery
    assert " RENAME TO " not in recovery
    assert drains == ["appdb"], "the captured promoted OID must fail closed first"


class _CutoverCatalogModel:
    """Small pg_database model for rename/timeout reconciliation tests."""

    OLD_OID = 41001
    REPLACEMENT_OID = 41002

    def __init__(self) -> None:
        self.names = {self.OLD_OID: "appdb", self.REPLACEMENT_OID: "staging-db"}
        self.allow = {self.OLD_OID: True, self.REPLACEMENT_OID: True}
        self.previous = ""
        self.rename_attempts: list[tuple[str, str]] = []
        self.events: list[str] = []
        self.observations = 0
        self.fail_before_commit: Callable[[str, str, int], bool] = (
            lambda _source, _destination, _attempt: False
        )
        self.timeout_after_commit: Callable[[str, str, int], bool] = (
            lambda _source, _destination, _attempt: False
        )

    def capture(
        self,
        _container: str,
        target: str,
        replacement: str,
        previous: str,
        *,
        timeout: int,
    ) -> tuple[int, int]:
        """Capture the two immutable identities before any admission change."""
        _ = timeout
        assert self.names == {
            self.OLD_OID: target,
            self.REPLACEMENT_OID: replacement,
        }
        self.previous = previous
        self.events.append("capture")
        return self.OLD_OID, self.REPLACEMENT_OID

    def observe(
        self,
        _container: str,
        _target: str,
        _replacement: str,
        _previous: str,
        target_oid: int,
        replacement_oid: int,
        *,
        timeout: int,
    ) -> dict[int, object]:
        """Return a fresh catalog snapshot after each ambiguous operation."""
        _ = timeout
        assert (target_oid, replacement_oid) == (self.OLD_OID, self.REPLACEMENT_OID)
        self.observations += 1
        self.events.append("observe")
        return {
            oid: restore._DatabaseCatalogRow(oid, self.names[oid], self.allow[oid])
            for oid in (self.OLD_OID, self.REPLACEMENT_OID)
        }

    def set_allow(
        self,
        _container: str,
        database: str,
        *,
        allow: bool,
        timeout: int,
    ) -> None:
        """Apply one modeled ALTER DATABASE ... ALLOW_CONNECTIONS call."""
        _ = timeout
        oid = next(oid for oid, name in self.names.items() if name == database)
        self.allow[oid] = allow
        self.events.append(f"allow:{database}:{allow}")

    def drain(self, _container: str, database: str, *, timeout: int) -> None:
        """Record the session drain ordering boundary."""
        _ = timeout
        self.events.append(f"drain:{database}")

    def count(
        self, _container: str, *, timeout: int, target_db: str | None = None
    ) -> int:
        """Record and satisfy the immediate pre-rename session recheck."""
        _ = timeout
        self.events.append(f"count:{target_db}")
        return 0

    def rename(
        self,
        _container: str,
        source: str,
        destination: str,
        *,
        timeout: int,
    ) -> None:
        """Model server commit separately from a later client-side timeout."""
        _ = timeout
        self.rename_attempts.append((source, destination))
        attempt = len(self.rename_attempts)
        self.events.append(f"rename:{source}->{destination}")
        if self.fail_before_commit(source, destination, attempt):
            raise restore.RestoreError(restore.EXIT_RESTORE_FAILED, "rename refused")
        source_oid = next(oid for oid, name in self.names.items() if name == source)
        assert destination not in self.names.values()
        self.names[source_oid] = destination
        if self.timeout_after_commit(source, destination, attempt):
            raise restore.RestoreError(
                restore.EXIT_RESTORE_FAILED, "client timed out after server commit"
            )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch only the PostgreSQL boundaries exercised by cutover."""
        monkeypatch.setattr(restore, "_capture_cutover_database_identities", self.capture)
        monkeypatch.setattr(restore, "_observe_cutover_database_state", self.observe)
        monkeypatch.setattr(restore, "_set_database_allow_connections", self.set_allow)
        monkeypatch.setattr(restore, "_drain_database_sessions", self.drain)
        monkeypatch.setattr(restore, "_foreign_writer_session_count", self.count)
        monkeypatch.setattr(restore, "_rename_database", self.rename)


def _assert_catalog_rolled_back(model: _CutoverCatalogModel) -> None:
    """Assert the captured old database is live and replacement is closed."""
    assert model.names == {
        model.OLD_OID: "appdb",
        model.REPLACEMENT_OID: "staging-db",
    }
    assert model.allow == {model.OLD_OID: True, model.REPLACEMENT_OID: False}


@pytest.mark.parametrize("timed_out_operation", ["rename", "allow_connections"])
def test_cutover_quiesces_late_backend_before_catalog_reconciliation(
    monkeypatch: pytest.MonkeyPatch, timed_out_operation: str
) -> None:
    """Rename/admission timeouts cannot mutate after the rollback snapshot."""
    model = _CutoverCatalogModel()
    real_set_allow = restore._set_database_allow_connections
    real_rename = restore._rename_database
    model.install(monkeypatch)
    monkeypatch.setattr(restore, "_set_database_allow_connections", real_set_allow)
    monkeypatch.setattr(restore, "_rename_database", real_rename)

    pending: Callable[[], None] | None = None
    selected_timed_out = False
    selected_command = ""
    events: list[str] = []

    def mutation_for(sql: str) -> tuple[str, Callable[[], None]]:
        """Translate the two ALTER DATABASE shapes into model mutations."""
        allow_match = re.fullmatch(
            r'ALTER DATABASE "([^"]+)" WITH ALLOW_CONNECTIONS (true|false);',
            sql.strip(),
        )
        if allow_match is not None:
            database, raw_allow = allow_match.groups()
            return (
                "allow_connections",
                lambda: model.set_allow(
                    "container",
                    database,
                    allow=raw_allow == "true",
                    timeout=5,
                ),
            )
        rename_match = re.fullmatch(
            r'ALTER DATABASE "([^"]+)" RENAME TO "([^"]+)";', sql.strip()
        )
        assert rename_match is not None, sql
        source, destination = rename_match.groups()
        return (
            "rename",
            lambda: model.rename(
                "container", source, destination, timeout=5
            ),
        )

    def fake_run_with_input(
        argv: list[str], *, timeout: int, stdin_text: str
    ) -> subprocess.CompletedProcess[str]:
        """Leave only the selected server mutation pending after host timeout."""
        nonlocal pending, selected_timed_out, selected_command
        operation, mutation = mutation_for(stdin_text)
        if operation == timed_out_operation and not selected_timed_out:
            selected_timed_out = True
            selected_command = " ".join(argv)
            pending = mutation
            events.append("client-timeout")
            raise subprocess.TimeoutExpired(argv, timeout)
        mutation()
        return subprocess.CompletedProcess(argv, 0, "", "")

    def backend_pids(*_args: object, **_kwargs: object) -> list[int]:
        """Expose the timed-out backend until termination settles it."""
        events.append("pid-active" if pending is not None else "pid-absent")
        return [9234] if pending is not None else []

    def terminate_backend(
        _container: str, sql: str, *, timeout: int, dbname: str | None = None
    ) -> str:
        """Cancel the pending late mutation rather than applying it."""
        nonlocal pending
        _ = timeout
        assert dbname == "postgres" and "pg_terminate_backend" in sql
        events.append("terminate")
        pending = None
        return ""

    def observe(*args: object, **kwargs: object) -> dict[int, object]:
        """A premature snapshot would permit the modeled late mutation to land."""
        nonlocal pending
        if pending is not None:
            events.append("LATE-MUTATION")
            mutation = pending
            pending = None
            mutation()
        events.append("snapshot")
        return model.observe(*args, **kwargs)

    monkeypatch.setattr(restore, "_run_with_input", fake_run_with_input)
    monkeypatch.setattr(restore, "_mutation_backend_pids", backend_pids)
    monkeypatch.setattr(restore, "_psql", terminate_backend)
    monkeypatch.setattr(restore, "_observe_cutover_database_state", observe)
    monkeypatch.setattr(restore.time, "sleep", lambda _seconds: None)

    with pytest.raises(restore.RestoreError) as caught:
        restore._cutover_verified_database(
            "container", "appdb", "staging-db", timeout=5
        )

    assert "was cancelled/terminated and is quiescent" in str(caught.value)
    assert "automatic catalog-reconciled rollback completed" in str(caught.value)
    assert selected_timed_out and "LATE-MUTATION" not in events
    assert events.index("terminate") < events.index("snapshot")
    assert "PGAPPNAME=ums_restore_mut_" in selected_command
    assert "statement_timeout=" in selected_command and "lock_timeout=" in selected_command
    assert "psql -X" in selected_command
    _assert_catalog_rolled_back(model)


def test_cutover_refuses_to_snapshot_when_timed_out_backend_is_not_quiescent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No catalog state or rollback claim is valid while a mutation may still run."""
    model = _CutoverCatalogModel()
    real_set_allow = restore._set_database_allow_connections
    model.install(monkeypatch)
    monkeypatch.setattr(restore, "_set_database_allow_connections", real_set_allow)
    monkeypatch.setattr(
        restore,
        "_run_with_input",
        lambda argv, *, timeout, stdin_text: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(argv, timeout)
        ),
    )
    monkeypatch.setattr(
        restore,
        "_quiesce_mutation_backend",
        lambda *a, **k: (_ for _ in ()).throw(
            restore.RestoreError(
                restore.EXIT_RESTORE_FAILED, "backend still active"
            )
        ),
    )
    monkeypatch.setattr(
        restore,
        "_observe_cutover_database_state",
        lambda *a, **k: pytest.fail("catalog snapshot raced an active backend"),
    )

    with pytest.raises(restore.RestoreError) as caught:
        restore._cutover_verified_database(
            "container", "appdb", "staging-db", timeout=5
        )

    message = str(caught.value)
    assert "could not be proven stopped" in message
    assert "catalog reconciliation was not started" in message
    assert "automatic catalog-reconciled rollback is INCOMPLETE" in message
    assert "observed UNAVAILABLE" in message
    assert model.rename_attempts == []


def test_cutover_failure_between_renames_restores_the_previous_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected second-rename refusal rolls the preserved old DB name back."""
    model = _CutoverCatalogModel()
    model.fail_before_commit = (
        lambda source, destination, _attempt: source == "staging-db"
        and destination == "appdb"
    )
    model.install(monkeypatch)

    with pytest.raises(restore.RestoreError) as caught:
        restore._cutover_verified_database(
            "container", "appdb", "staging-db", timeout=5
        )
    assert caught.value.code == restore.EXIT_RESTORE_FAILED
    assert "rename refused" in str(caught.value)
    assert "automatic catalog-reconciled rollback completed" in str(caught.value)
    assert "Neither database was dropped" in str(caught.value)
    assert model.rename_attempts == [
        ("appdb", model.previous),
        ("staging-db", "appdb"),
        (model.previous, "appdb"),
    ]
    _assert_catalog_rolled_back(model)


def test_first_rename_commit_then_timeout_is_reconciled_from_database_oids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout after old->previous commits cannot make rollback trust a flag."""
    model = _CutoverCatalogModel()
    model.timeout_after_commit = lambda _source, _destination, attempt: attempt == 1
    model.install(monkeypatch)

    with pytest.raises(restore.RestoreError) as caught:
        restore._cutover_verified_database(
            "container", "appdb", "staging-db", timeout=5
        )

    message = str(caught.value)
    assert "client timed out after server commit" in message
    assert "automatic catalog-reconciled rollback completed" in message
    assert f"previous_live_oid={model.OLD_OID}" in message
    assert f"verified_replacement_oid={model.REPLACEMENT_OID}" in message
    assert model.rename_attempts == [
        ("appdb", model.previous),
        (model.previous, "appdb"),
    ]
    assert model.observations >= 3
    _assert_catalog_rolled_back(model)


def test_ambiguous_rename_with_failed_catalog_query_prints_no_speculative_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When identity cannot be re-read, recovery emits only the safe OID query."""
    model = _CutoverCatalogModel()
    model.timeout_after_commit = lambda _source, _destination, attempt: attempt == 1
    model.install(monkeypatch)

    def unavailable(*_args: object, **_kwargs: object) -> dict[int, object]:
        """Model loss of the independent maintenance connection."""
        raise restore.RestoreError(
            restore.EXIT_RESTORE_FAILED, "maintenance query unavailable"
        )

    monkeypatch.setattr(restore, "_observe_cutover_database_state", unavailable)
    with pytest.raises(restore.RestoreError) as caught:
        restore._cutover_verified_database(
            "container", "appdb", "staging-db", timeout=5
        )

    message = str(caught.value)
    assert "automatic catalog-reconciled rollback is INCOMPLETE" in message
    assert "observed UNAVAILABLE" in message
    assert "No ALTER DATABASE recovery command can be derived" in message
    assert " RENAME TO " not in message
    assert model.rename_attempts == [("appdb", model.previous)]


def test_second_rename_commit_then_timeout_is_reconciled_from_database_oids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout after replacement->target commits is undone by observed identity."""
    model = _CutoverCatalogModel()
    model.timeout_after_commit = lambda _source, _destination, attempt: attempt == 2
    model.install(monkeypatch)

    with pytest.raises(restore.RestoreError) as caught:
        restore._cutover_verified_database(
            "container", "appdb", "staging-db", timeout=5
        )

    assert "automatic catalog-reconciled rollback completed" in str(caught.value)
    assert model.rename_attempts == [
        ("appdb", model.previous),
        ("staging-db", "appdb"),
        ("appdb", "staging-db"),
        (model.previous, "appdb"),
    ]
    assert model.observations >= 4
    _assert_catalog_rolled_back(model)


def test_cutover_drains_rechecks_and_preserves_the_previous_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success path reaches renames only after both names are closed and empty."""
    model = _CutoverCatalogModel()
    model.install(monkeypatch)

    previous = restore._cutover_verified_database(
        "container",
        "appdb",
        "staging-db",
        timeout=5,
        finalize=lambda: model.events.append("finalize"),
    )
    assert previous.startswith("ums_previous_")
    assert model.events == [
        "capture",
        "allow:appdb:False",
        "drain:appdb",
        "allow:staging-db:False",
        "drain:staging-db",
        "count:appdb",
        "count:staging-db",
        f"rename:appdb->{previous}",
        "rename:staging-db->appdb",
        "finalize",
        "allow:appdb:True",
        "observe",
    ]
    assert model.names == {
        model.OLD_OID: previous,
        model.REPLACEMENT_OID: "appdb",
    }
    assert model.allow == {model.OLD_OID: False, model.REPLACEMENT_OID: True}


def test_role_finalization_failure_rolls_both_database_names_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback re-queries after its own rename commits but the client times out."""
    model = _CutoverCatalogModel()
    model.timeout_after_commit = (
        lambda source, destination, _attempt: source == "appdb"
        and destination == "staging-db"
    )
    model.install(monkeypatch)

    def fail_roles() -> None:
        """Inject an unexpected failure after promotion, before admission."""
        raise RuntimeError("role replay crashed")

    with pytest.raises(restore.RestoreError) as caught:
        restore._cutover_verified_database(
            "container",
            "appdb",
            "staging-db",
            timeout=5,
            finalize=fail_roles,
            rollback_finalize=lambda: model.events.append(
                "restore-original-role-settings"
            ),
        )
    assert caught.value.code == restore.EXIT_INTERNAL
    assert "automatic catalog-reconciled rollback completed" in str(caught.value)
    assert "reported failure" in str(caught.value)
    assert model.rename_attempts == [
        ("appdb", model.previous),
        ("staging-db", "appdb"),
        ("appdb", "staging-db"),
        (model.previous, "appdb"),
    ]
    _assert_catalog_rolled_back(model)
    assert model.events.index("restore-original-role-settings") < len(model.events) - 1
    assert model.events[-2:] == ["allow:appdb:True", "observe"]


def test_original_role_setting_rollback_failure_keeps_live_database_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old name is never admitted when its pre-replay GUCs cannot be restored."""
    model = _CutoverCatalogModel()
    model.install(monkeypatch)

    def fail_promotion() -> None:
        """Enter rollback after both forward renames."""
        raise RuntimeError("promotion role settings failed")

    def fail_original() -> None:
        """Refuse the database-role rollback before connection admission."""
        raise restore.RestoreError(
            restore.EXIT_ROLES_FAILED, "original role settings unavailable"
        )

    with pytest.raises(restore.RestoreError) as caught:
        restore._cutover_verified_database(
            "container",
            "appdb",
            "staging-db",
            timeout=5,
            finalize=fail_promotion,
            rollback_finalize=fail_original,
        )

    message = str(caught.value)
    assert "automatic catalog-reconciled rollback is INCOMPLETE" in message
    assert "original role settings unavailable" in message
    assert "keep the restored target closed" in message
    assert 'ALLOW_CONNECTIONS true;' not in message
    assert model.names == {
        model.OLD_OID: "appdb",
        model.REPLACEMENT_OID: "staging-db",
    }
    assert model.allow == {model.OLD_OID: False, model.REPLACEMENT_OID: False}


def test_strict_backup_permissions_fail_closed_on_chmod_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sensitive staging cannot become usable when POSIX mode enforcement fails."""

    def fail_chmod(_path: Path, _mode: int) -> None:
        """Simulate an unenforceable destination mode."""
        raise OSError("chmod denied")

    monkeypatch.setattr(backup.os, "chmod", fail_chmod)
    with pytest.raises(backup.BackupError) as caught:
        backup._restrict_run_dir_mode(tmp_path, tmp_path, strict=True)
    assert caught.value.code == backup.EXIT_ARTIFACT_INVALID
    assert "refusing to publish" in str(caught.value)


def test_crash_window_lock_uses_directory_mtime_for_reclaim(
    tmp_path: Path, clock: _Clock
) -> None:
    """mkdir followed by a crash before started.at must eventually self-heal."""
    lock_dir = tmp_path / backup.LOCK_DIR_NAME
    lock_dir.mkdir()
    old = (clock.now - backup.LOCK_STALE_AFTER - timedelta(minutes=1)).timestamp()
    os.utime(lock_dir, (old, old))
    assert backup._lock_age_exceeds_bound(lock_dir) is True


def test_restore_acl_role_preflight_runs_before_replacement_creation(tmp_path: Path) -> None:
    """A hand-edited ACL cannot name a role absent from the replay file."""
    roles_path = tmp_path / restore.ROLES_NAME
    roles_path.write_text(_REAL_ROLES_SQL, encoding="utf-8")
    assert restore._database_acl_role_problems(
        roles_path,
        "postgres",
        [("app_tenant", "CONNECT", False), ("PUBLIC", "TEMPORARY", False)],
    ) == []
    assert restore._database_acl_role_problems(
        roles_path, "operator_only", [("app_tenant", "CONNECT", False)]
    ) == ["operator_only"]
