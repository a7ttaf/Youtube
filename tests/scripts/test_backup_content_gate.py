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
virgin state was 180 rows at the time (328 since P0.7), so total data loss with
the schema left standing published a green ``OK`` -- and that run then became
the reference for the next one. This file's fixtures are DERIVED from the
migrations and anchored to a container measurement, not assumed.
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
Defect 12: P0.7's roles/permissions seed migration put 148 rows into three
tables that ``SEED_TABLES`` did not list, so a VIRGIN database measured
``non_seed_rows=148`` and tier 3b stopped refusing an empty one. The test that
was supposed to catch it compared a literal against a literal.

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
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from ums_smart_revenue.auth.permissions import PERMISSION_DEFINITIONS
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS
from ums_smart_revenue.auth.seed import initial_role_permission_rows
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
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    def move_to(self, moment: datetime) -> None:
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
#     hard-coded 180 were correct until P0.7's roles/permissions seed migration
#     (20260825_0001) put 148 rows into three tables that were in neither. On a
#     VIRGIN database ``non_seed_rows`` went 0 -> 148, which is exactly the
#     input tier 3b keys on, so `docker compose down -v` + auto-migrate + one
#     `--establish-watermark` would have made an empty database the directory's
#     permanent reference. Meanwhile
#     `test_seed_tables_match_what_the_migrations_actually_seed` kept passing,
#     because it compared a literal against a literal.
#
# So the row counts below are computed from the SAME sources the migrations
# import, not re-typed. A registry that grows moves the fixture with it, and
# ``test_the_derived_virgin_state_still_matches_the_measured_one`` is what
# forces a re-measurement when it does.
#
# THE MEASUREMENT, which those derivations are checked against.
# `alembic upgrade head` (revision 20260825_0001) into a fresh
# postgres:18-alpine@sha256:96d56f7f container, measured 2026-08-25:
#
#     tables=38 rows=328
#       currencies                  178      permissions      26
#       role_permission_assignments 106      roles            16
#       alembic_version               1      tenants           1
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
    # 20260825_0001 seeds all three from the live auth registries.
    "permissions": len(PERMISSION_DEFINITIONS),
    "role_permission_assignments": len(initial_role_permission_rows()),
    "roles": len(ROLE_DEFINITIONS),
    # 20260516_0001 inserts the single bootstrap tenant.
    "tenants": 1,
}
#: The measured totals the derivation above is anchored to. Not the derivation's
#: source -- its cross-check, so a registry change forces a fresh measurement
#: rather than silently redefining what "virgin" means.
MEASURED_VIRGIN_TABLES = 38
MEASURED_VIRGIN_ROWS = 328
MEASURED_SEED_ROWS = {
    "alembic_version": 1,
    "currencies": 178,
    "permissions": 26,
    "role_permission_assignments": 106,
    "roles": 16,
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


#: A virgin `alembic upgrade head`: 38 tables, 328 rows, no application data.
VIRGIN = _database()
#: The documented reference database: the virgin state plus 7 application rows.
REAL = _database(monthly_channel_revenue_facts=3, org_units=2, youtube_channels=2)
#: Total loss of application data with the schema intact: 38 tables, 1 row.
GUTTED = {**dict.fromkeys(ALL_TABLES, 0), "alembic_version": 1}
#: `DROP SCHEMA public CASCADE`.
EMPTY: dict[str, int] = {}

NO_WATERMARK = backup.Watermark()


def _watermark(counts: dict[str, int], *, source: str = "test") -> backup.Watermark:
    return backup.Watermark(tables=dict(counts), source=source)


IDENTITY_A = backup.Identity(system_identifier="7677783453675450413", database="ums_smart_revenue")
IDENTITY_B = backup.Identity(system_identifier="7677783473962770477", database="ums_smart_revenue")


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
    if recorded_bytes is not None:
        body["artifacts"] = {name: {"bytes": size} for name, size in recorded_bytes.items()}
    if counts is not None:
        body["table_row_counts"] = counts
        body["content_gate"] = {
            "status": "rejected" if rejected else "accepted",
            "tables": len(counts),
            "rows": sum(counts.values()),
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
    counts = dict.fromkeys(ALL_TABLES, 0)
    verdict = backup._evaluate_content(counts, NO_WATERMARK, accept_drop=False, establish=False)
    assert verdict.accepted is False
    assert any("hold 0 rows" in reason for reason in verdict.failures)


def test_total_data_loss_with_the_schema_intact_is_rejected() -> None:
    """HOLE 1, the headline defect: 38 tables, 1 row, published as OK.

    Truncate every table but ``alembic_version`` and the old floor
    (MIN_TABLES = 1, MIN_ROWS = 1) saw "many tables, one stamp row" and called it
    a freshly migrated install. It is the opposite: a virgin install was 180 rows
    when that was measured and is 328 today.
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
# first prose to mention it -- a docstring in 20260825_0001 explaining this
# parser, written the same day -- registered a table called ``x``.
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
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bound = _sa_table_bindings(tree)
        prose = _docstring_constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in prose:
                    seeded.update(match.lower() for match in _INSERT_INTO.findall(node.value))
                continue
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "bulk_insert"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in bound:
                seeded.add(bound[first.id])
            elif (
                isinstance(first, ast.Call)
                and isinstance(first.func, ast.Attribute)
                and first.func.attr == "table"
                and first.args
                and isinstance(first.args[0], ast.Constant)
                and isinstance(first.args[0].value, str)
            ):
                seeded.add(first.args[0].value)
    return seeded


def test_seed_tables_match_what_the_migrations_actually_seed() -> None:
    """DERIVED, not re-typed: the previous version compared a literal to a literal.

    ``SEED_TABLES`` is application knowledge the backup deliberately takes on,
    and the cost of it being stale is not cosmetic -- ``_non_seed_rows`` counts
    everything OUTSIDE it, which is the single input tier 3b uses to refuse to
    make an empty database a directory's permanent reference. P0.7 added a
    seeding migration and this test kept passing, because it asserted the same
    three names it was supposed to be checking. It now reads the migrations.
    """
    scanned = _tables_seeded_by_migrations()
    # The scanner's own guard: these two ARE the two idioms it knows, so a
    # migration rewritten in a third one loses its table here rather than
    # silently going unnoticed.
    assert "currencies" in scanned, "the op.bulk_insert idiom is no longer recognised"
    assert "tenants" in scanned, "the INSERT INTO literal idiom is no longer recognised"
    expected = scanned | {"alembic_version"}  # written by Alembic itself, not a revision
    assert set(backup.SEED_TABLES) == expected, (
        "a migration seeds a table that SEED_TABLES does not list (or lists one it no "
        "longer seeds). Add it to scripts/backup_database.py::SEED_TABLES and to "
        "SEED_ROWS here, then re-measure the virgin state -- until both are updated "
        "the gate reads seeded rows as application data and tier 3b stops firing. "
        f"migrations seed {sorted(expected)}; SEED_TABLES holds {sorted(backup.SEED_TABLES)}"
    )
    assert set(backup.SEED_TABLES) <= set(VIRGIN)
    assert all(VIRGIN[name] > 0 for name in backup.SEED_TABLES)


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
    """Tier 3b's whole input. This is the property P0.7 silently broke.

    ``_evaluate_content`` refuses ``--establish-watermark`` over a first run only
    when ``non_seed == 0``. With the roles seed migration landed and SEED_TABLES
    left at three names, a virgin database measured ``non_seed_rows=148``, so the
    refusal never fired and `docker compose down -v` + auto-migrate + the flag
    the exit-8 message names would have published an empty database as the
    permanent reference.
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
    verdict = backup._evaluate_content(REAL, NO_WATERMARK, accept_drop=True, establish=False)
    assert verdict.accepted is False


def test_establishing_the_watermark_is_recorded_not_silent() -> None:
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
    verdict = backup._evaluate_content(
        VIRGIN, NO_WATERMARK, accept_drop=False, establish=False, accept_empty=True
    )
    assert verdict.accepted is False


def test_the_empty_acknowledgement_cannot_override_the_seed_floor() -> None:
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


def test_retiring_a_permission_costs_exactly_one_override_night(tmp_path: Path) -> None:
    """The price of widening SEED_TABLES, measured rather than asserted in prose.

    `roles`, `permissions` and `role_permission_assignments` joined SEED_TABLES to
    restore tier 3b, and that put them under the exact-mark seed-shrink rule too.
    Unlike a frozen ISO snapshot these DO change on ordinary work --
    `20260513_0002_retire_graph_permissions` is the precedent. Docs/22 promises
    the operator that this costs one `--accept-content-drop` run and no more, so
    that promise is a test: red, cleared, and green again with no flag.
    """
    _night(tmp_path, "20260801T020000", REAL, establish=True)
    retired = dict(REAL)
    retired["permissions"] -= 1

    red = _night(tmp_path, "20260802T020000", retired)
    assert red.accepted is False
    assert any("permissions" in reason for reason in red.failures)

    cleared = _night(tmp_path, "20260803T020000", retired, accept_drop=True)
    assert cleared.accepted is True

    assert _night(tmp_path, "20260804T020000", retired).accepted is True, (
        "one override night, not a standing flag in the scheduled task"
    )
    assert backup._load_watermark(tmp_path).tables["permissions"] == retired["permissions"]


def test_a_disappeared_table_is_caught() -> None:
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
    _write_run(tmp_path, "20260715T222143", counts=REAL)
    _write_run(tmp_path, "20260820T222143", counts=EMPTY)
    watermark = backup._load_watermark(tmp_path)
    assert watermark.total_rows == sum(REAL.values())
    assert watermark.table_count == 38


def test_the_watermark_is_empty_on_an_empty_output_directory(tmp_path: Path) -> None:
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
    """Either side missing means the check cannot run -- it must not mean "passed"."""
    for expected, observed in ((IDENTITY_A, None), (None, IDENTITY_B), (None, None)):
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
    impostor = tmp_path / "ums-backup-20250145T999999Z.rejected"
    impostor.mkdir()

    pruned = backup._prune(tmp_path, keep_days=0, keep_min=1, now=NOW)

    assert impostor.is_dir()
    assert impostor.name in pruned.skipped


def test_a_non_date_directory_cannot_poison_the_watermark(tmp_path: Path) -> None:
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
    plant = _write_run(tmp_path, "20990101T000000", counts=EMPTY, rejected=True)

    pruned = backup._prune(tmp_path, keep_days=0, keep_min=1, now=NOW)

    assert plant.is_dir()
    assert plant.name in pruned.future


# --------------------------------------------------------------------------
# Defect 6, second half -- the status file can never be stale-green
# --------------------------------------------------------------------------


def _last_run(out_dir: Path) -> dict[str, object]:
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


def _unwritable(path: Path) -> None:
    """Make one status file refuse writes, the way a share-mode lock does.

    Existing content is preserved on purpose: the defect is precisely that the
    PREVIOUS run's record survives, and a fixture that blanked it first would be
    testing something easier.
    """
    if not path.exists():
        path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o444)
    if os.access(path, os.W_OK):  # pragma: no cover - platform dependent
        pytest.skip("this filesystem ignores the read-only bit")


def test_an_unwritable_status_file_is_reported_not_swallowed(tmp_path: Path) -> None:
    backup._write_last_run(tmp_path, {"status": "OK", "exit_code": 0})
    _unwritable(tmp_path / backup.LAST_RUN_NAME)

    assert backup._write_last_run(tmp_path, {"status": "REJECTED", "exit_code": 8}) is False


def test_this_runs_record_lands_in_a_stamped_sidecar_when_the_file_is_locked(
    tmp_path: Path,
) -> None:
    """A NEW file name is the one write a lock on the canonical file cannot block."""
    _unwritable(tmp_path / backup.LAST_RUN_NAME)
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
) -> None:
    """The exit code is the one channel a file lock cannot block."""
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    _unwritable(tmp_path / backup.LAST_RUN_NAME)
    report.finalise("line", {"status": "OK", "exit_code": 0})

    assert report.status_durable is False
    assert report.escalate(backup.EXIT_OK) == backup.EXIT_BOOKKEEPING_FAILED


def test_escalation_never_overwrites_a_more_specific_failure_code(
    tmp_path: Path,
) -> None:
    """Turning 8 into 7 would hide TOTAL DATA LOSS behind a bookkeeping message."""
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    _unwritable(tmp_path / backup.LAST_RUN_NAME)
    report.finalise("line", {"status": "REJECTED", "exit_code": 8})

    assert report.status_durable is False
    assert report.escalate(backup.EXIT_NO_CONTENT) == backup.EXIT_NO_CONTENT


def test_a_run_whose_status_landed_normally_is_not_escalated(tmp_path: Path) -> None:
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    report.finalise("line", {"status": "OK", "exit_code": 0})

    assert report.status_durable is True
    assert report.escalate(backup.EXIT_OK) == backup.EXIT_OK


def test_an_unwritable_log_alone_is_enough_to_escalate(tmp_path: Path) -> None:
    """backup.log is half of the runbook's "every run writes" claim."""
    report = backup._RunReport(tmp_path, NOW)
    report.start()
    _unwritable(tmp_path / backup.LOG_NAME)
    report.finalise("line", {"status": "OK", "exit_code": 0})

    assert report.escalate(backup.EXIT_OK) == backup.EXIT_BOOKKEEPING_FAILED
    assert _last_run(tmp_path)["status"] == "OK", "the status file itself still landed"


def test_a_failure_to_clear_the_previous_green_is_carried_to_the_exit_code(
    tmp_path: Path,
) -> None:
    """The moment that matters most: until RUNNING lands, yesterday's OK stands."""
    backup._write_last_run(tmp_path, {"status": "OK", "exit_code": 0})
    _unwritable(tmp_path / backup.LAST_RUN_NAME)

    report = backup._RunReport(tmp_path, NOW)
    report.start()

    assert report.status_durable is False
    assert _last_run(tmp_path)["status"] == "OK", "it really is still the old record"
    assert report.escalate(backup.EXIT_OK) == backup.EXIT_BOOKKEEPING_FAILED


def test_a_transient_lock_is_retried_rather_than_failing_the_run(tmp_path: Path) -> None:
    """An AV scanner holds a file for seconds; that must not turn a run red."""
    target = tmp_path / backup.LAST_RUN_NAME
    attempts = {"n": 0}
    real_open = Path.open

    def flaky_open(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
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
    fresh = _write_run(tmp_path, "20260824T222105", counts=EMPTY, rejected=True)
    stale = _write_run(tmp_path, "20250101T000000", counts=EMPTY, rejected=True)

    pruned = backup._prune(tmp_path, keep_days=30, keep_min=7, now=NOW)

    assert stale.name in pruned.removed
    assert fresh.is_dir()


def test_prune_ignores_foreign_directories(tmp_path: Path) -> None:
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
    assert backup._run_has_content(_write_run(tmp_path, "20260101T000000", counts=REAL)) is True
    assert backup._run_has_content(_write_run(tmp_path, "20260102T000000", counts=EMPTY)) is False
    unknown = _write_run(tmp_path, "20260103T000000", counts=None, manifest=False)
    assert backup._run_has_content(unknown) is None


def test_a_manifest_claiming_acceptance_is_still_measured(tmp_path: Path) -> None:
    """A gate verdict of "accepted" over no data must not protect the run."""
    gutted = _write_run(tmp_path, "20260104T000000", counts=GUTTED)
    assert backup._run_has_content(gutted) is False


def test_partial_directories_still_expire(tmp_path: Path) -> None:
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
            }
        ),
        encoding="utf-8",
    )
    assert restore._load_backup(run)["schema"] == backup.MANIFEST_SCHEMA


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
    assert restore._unexpected_roles_errors(
        'ERROR:  role "ums" already exists\n'
    ) == []
    unexpected = restore._unexpected_roles_errors(
        'ERROR:  role "ums" already exists\n'
        'ERROR:  permission denied to alter role\n'
    )
    assert unexpected == ['ERROR:  permission denied to alter role']


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
        self.counts = dict(counts)
        self.identity = identity
        self.toc_entries = toc_entries

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backup, "_await_docker", lambda *a, **k: "29.5.3")
        monkeypatch.setattr(backup, "_resolve_container", lambda **k: "fake-postgres")
        monkeypatch.setattr(backup, "_await_postgres", lambda *a, **k: None)
        monkeypatch.setattr(backup, "_dump_roles", self._dump_roles)
        monkeypatch.setattr(backup, "_dump_database", self._dump_database)
        monkeypatch.setattr(backup, "_verify_dump_readable", lambda *a, **k: self.toc_entries)
        monkeypatch.setattr(backup, "_table_row_counts", lambda *a, **k: dict(self.counts))
        monkeypatch.setattr(backup, "_container_facts", self._facts)

    def _dump_roles(
        self, container: str, target: Path, *, timeout: int, include_passwords: bool
    ) -> list[str]:
        target.write_text("CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n", encoding="utf-8")
        return list(backup.REQUIRED_ROLES)

    def _dump_database(self, container: str, target: Path, *, timeout: int) -> None:
        target.write_bytes(backup.CUSTOM_FORMAT_MAGIC + b"-fake-archive")

    def _facts(self, container: str, *, timeout: int) -> dict[str, str]:
        return {
            "container": container,
            "database": self.identity.database,
            "superuser": "ums",
            "system_identifier": self.identity.system_identifier,
        }


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    out_dir: Path,
    counts: dict[str, int],
    *flags: str,
    identity: backup.Identity = IDENTITY_A,
    toc_entries: int = 17,
) -> int:
    _FakeContainer(counts, identity=identity, toc_entries=toc_entries).install(monkeypatch)
    return backup.main(["--out-dir", str(out_dir), *flags])


def _run_dirs(out_dir: Path) -> list[str]:
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


def test_the_cli_refuses_a_first_run_without_the_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    code = _run_cli(monkeypatch, tmp_path, REAL)

    assert code == backup.EXIT_NO_CONTENT
    quarantined = _run_dirs(tmp_path)
    assert len(quarantined) == 1, "the run happened; it was just not published"
    assert quarantined[0].endswith(backup.REJECTED_SUFFIX)
    assert not (tmp_path / backup.WATERMARK_NAME).exists()
    assert _last_run(tmp_path)["status"] == "REJECTED"


def test_the_cli_quarantines_a_dropped_schema_and_touches_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    """M53's kill. Every consequence of ``if not outcome.accepted:`` at once.

    A dropped schema, against a directory that already holds a good backup and
    an expired one. The run must exit 8, land under ``.rejected``, leave the
    watermark exactly where it was, and -- retention invariant 6 -- delete
    nothing, because a night that captured nothing gets no say over what is
    removed.
    """
    assert _run_cli(monkeypatch, tmp_path, REAL, "--establish-watermark") == backup.EXIT_OK
    good = _run_dirs(tmp_path)[0]
    expired = _write_run(tmp_path, "20260101T000000", counts=REAL)
    before = json.loads((tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8"))
    clock.advance(timedelta(days=1))

    code = _run_cli(monkeypatch, tmp_path, EMPTY, "--keep-days", "0", "--keep-min", "1")

    assert code == backup.EXIT_NO_CONTENT
    quarantined = [name for name in _run_dirs(tmp_path) if name.endswith(backup.REJECTED_SUFFIX)]
    assert len(quarantined) == 1, "the run must be quarantined, not published"
    after = json.loads((tmp_path / backup.WATERMARK_NAME).read_text(encoding="utf-8"))
    assert after == before, "a rejected run must not rewrite the watermark"
    assert (tmp_path / good).is_dir(), "the previous good backup must survive"
    assert expired.is_dir(), "retention invariant 6: a rejected run prunes nothing"
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
    _unwritable(tmp_path / backup.LAST_RUN_NAME)
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
    _unwritable(tmp_path / backup.LOG_NAME)

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
    _unwritable(tmp_path / backup.WATERMARK_NAME)
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
        target.write_text("CREATE ROLE app_tenant;\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(backup, "_run_to_file", _half_a_roles_file)

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
        target.write_text("CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(backup, "_run_to_file", _complete_roles_file)

    assert backup._dump_roles("fake", target, timeout=5, include_passwords=False) == list(
        backup.REQUIRED_ROLES
    )


def test_an_empty_roles_file_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """pg_dumpall exiting 0 having written nothing is still an unusable backup."""
    target = tmp_path / backup.ROLES_NAME

    def _nothing(argv: list[str], *, timeout: int, target: Path):
        target.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(backup, "_run_to_file", _nothing)

    with pytest.raises(backup.BackupError) as raised:
        backup._dump_roles("fake", target, timeout=5, include_passwords=False)

    assert raised.value.code == backup.EXIT_ARTIFACT_INVALID
