# 22 — Database Backup, Restore, and the Restore Rehearsal

**Implements:** [`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md) item
**P0.1** — *"the only item in this plan I would refuse to skip."*
**Evidence:** [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md) finding
**B3** — real revenue data has no backup, and a documented command destroys it.
**Related:** [`17_MULTI_TENANT_ARCHITECTURE.md`](17_MULTI_TENANT_ARCHITECTURE.md) records the
approach as *"single physical backup"*; this document is that backup.

Everything below was executed against a container running the repository's real
Alembic head — 38 tables, 26 RLS policies, both app roles — not reasoned about.
The measured numbers are in [Evidence](#evidence--what-was-actually-run).

> **Read [What a green run does not guarantee](#what-a-green-run-does-not-guarantee)
> before you rely on the nightly task.** The roles trap — the failure that makes
> backups look perfect and be unrestorable — is closed and rehearsed. The gate that
> decides *"was there any data in this backup"* was not finished in the first two
> rounds: its absolute floor was one row against a virgin install of 180, and its
> collapse check re-anchored every night, so total data loss and an unbounded drain
> both published a green `OK`. **Both are now closed and re-measured**, and the
> section named above states exactly what the replacement does and does not promise.
>
> Round 4 closed four more, each measured against the live CLI rather than argued:
> a manifest with no artifacts behind it could inflate the high-water mark until no
> real run could ever clear it; **a second, unrelated database could be backed up into
> an established directory and publish exit `0`**; a status file another process was
> holding left the previous run's green `OK` standing while the run itself failed; and
> `--establish-watermark`, the flag the exit-`8` message told the operator to use,
> would publish a wiped database and make it the reference. See
> [Round 4](#round-4--five-smaller-findings-re-measured-against-the-fixed-script).
>
> Round 5 closed four more, three of them proved with live CLI runs by an adversarial
> verifier and one of them caused by a *different* piece of work landing in the same
> release: **a single directory dated in the future permanently wedged the watermark
> and captured both retention invariants at once**; the tests never drove the CLI, so
> two catastrophic mutations survived a 55-mutation matrix; and the migration-derived
> seed-floor test now fails closed if a later auth-seed migration is added without a
> matching script update. See [Round 5](#round-5--the-future-dated-directory-the-untested-cli-and-a-cross-lane-regression).

---

## The one thing to understand before anything else

`pg_dump` does not dump roles, and this schema cannot be restored without them.

The custom-format archive *does* carry the RLS policies and every
`GRANT ... TO app_tenant` / `app_platform` statement installed by
`backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py`
(`_create_role`, lines 92–113; the grant block, lines 300–333). Those two roles are
**cluster-global objects**. They live outside the database, so they are not in the
dump, and a fresh container does not have them.

Restoring a roles-less backup was measured, twice, in both of its shapes:

| | `pg_restore --single-transaction` | `pg_restore` (the default) |
|---|---|---|
| Exit code | 1 | 1 |
| First error | `role "app_tenant" does not exist` | `role "app_tenant" does not exist` |
| Tables restored | **0** | **38** |
| Revenue rows restored | 0 | **3 of 3** |
| RLS policies | 0 | **26 of 26** |
| RLS *enabled* on tables | 0 | **26 of 26** |
| Grants to `app_tenant` | 0 | **0 of 110** |
| Grants to `app_platform` | 0 | **0 of 129** |

The right-hand column is the dangerous one, and it is the shape an operator gets by
typing the obvious command. The data comes back. The tables come back. The policies
come back. Row-level security is switched **on**. And the entire 239-grant privilege
surface is silently absent, along with the two roles the application's sessions
`SET ROLE` into. `pg_restore` prints `warning: errors ignored on restore: 42` and
exits 1 — one line, at the end, after several screens of successful output.

That is why `scripts/backup_database.py` runs `pg_dumpall --roles-only` alongside the
dump and **refuses to publish a backup whose `roles.sql` does not name both roles**,
and why `scripts/restore_database.py` applies `roles.sql` first and then re-queries
`pg_roles` before it will start the data restore.

---

## What a backup is

One directory per run, on a **host** path — never a container path, never a Docker
volume, and deliberately never inside this repository:

```text
<UMS_BACKUP_DIR>/
    backup.log                          one appended line per run, success or failure
    last-run.json                       the at-a-glance status of the most recent run
    last-run-<stamp>.json               only when last-run.json could not be replaced
    watermark.json                      high-water row counts + this directory's identity
    ums-backup-20260824T220311Z/
        database.dump                   pg_dump --format=custom --compress=6
        roles.sql                       pg_dumpall --roles-only --no-role-passwords
        manifest.json                   provenance, sha256 digests, per-table row counts
```

`watermark.json` is what stops a slow drain from being accepted one night at a time.
It records, per table, the **largest** row count this directory has ever backed up —
not the previous run's — plus any deliberate downward reset and which run performed
it, plus the `identity` of the database this directory belongs to. Deleting it is safe:
both are rebuilt from the run manifests, and the mark can only be pushed back up. See
[What a green run does not guarantee](#what-a-green-run-does-not-guarantee).

`manifest.json` is what makes the rehearsal objective. It records the exact row count
of **every** table in `public` at backup time, so a restore is checked against numbers
rather than against a feeling. It also records the image, the server version, the
database name and the superuser name, which is what lets the rehearsal build a
throwaway container that matches the source.

A run directory only ever appears under its final name after every check has passed.
While a backup is in progress it is `ums-backup-...Z.partial`, so a crashed, killed or
power-cut run leaves something that can never be mistaken for a backup.

### What is *not* in it

- **Export artifacts and connector blobs.** Those are plan item P0.2. They now have a
  named compose volume (`app-data`). The volume comment in `docker-compose.yml` states
  that `scripts/backup_database.py` does **not** cover it today (Postgres-only) —
  `docker compose down -v` still destroys it. Closing that gap is a separate piece of
  work; see [Open items](#open-items-this-does-not-close).
- **Role passwords.** `--no-role-passwords` is the default so SCRAM verifiers never
  land in a plaintext file on the operator's disk. Nothing is lost: `app_tenant` and
  `app_platform` are `NOLOGIN`, and the beta's login role is recreated by the Postgres
  image from `POSTGRES_PASSWORD`. If your deployment ever has a login role whose
  password exists nowhere else, pass `--include-role-passwords` and treat the backup
  directory as a secret.
- **The Redis volume.** It holds no source-of-truth state.

---

## Where these files live, and which shell each needs

| File | What it is | Shell required |
|---|---|---|
| `scripts/backup_database.py` | Backup + verify + retention | **None.** Python |
| `scripts/restore_database.py` | Restore + rehearsal + verification | **None.** Python |

They are in `scripts/` because that is where this repository already keeps operational
CLIs (`run_google_connector.py`, `run_adsense_payment_sync.py`, `seed_demo_month.py`,
`smoke_mvp.py`). `ci/checks/` is the CI gate lane directory — a backup is not a gate —
and inventing a new top-level directory for two files would be worse than either.
Putting them in `scripts/` also means `uv run ruff check backend tests scripts`, which
`AGENTS.md` already requires, covers them.

They are **Python, not shell, on purpose**:

- The target host is Windows. A `.sh` file needs Git Bash or WSL in the scheduled
  task's environment; `python.exe` needs neither.
- The same file then works unchanged from the Mac.
- They are **stdlib only and import nothing from this repository**, so a backup still
  runs while the checkout is mid-rebase or the application image will not build.

Consequently there is no shellcheck/shfmt surface to satisfy here
(`ci/checks/tests-shell.sh` and `ci/checks/lint.sh` glob `*.sh`).

### Prerequisites on the host

- **Python 3.11 or newer** on `PATH`. No packages, no virtualenv, no `uv`.
- The **`docker` CLI** on `PATH`, with Docker Desktop running.
- That is all. The scripts never read `docker-compose.yml`, so they do **not** need
  `UMS_DB_USER` / `UMS_DB_PASSWORD_URLENC` / `UMS_DB_NAME` to be set — which matters,
  because compose refuses to interpolate that file without them and a scheduled task
  inherits almost no environment.

### How credentials are handled

The host process never learns a database password.

Every database command is run *inside* the Postgres container, and the password is
expanded there from the container's own `POSTGRES_PASSWORD` by the container's shell:

```text
docker exec -i <container> sh -c 'export PGPASSWORD from the container POSTGRES_PASSWORD env; exec pg_dump ...'
```

So no password reaches the script's argv, the script's environment, the host process
listing, `backup.log`, `last-run.json`, or `manifest.json`. Inside the container the
value is in `pg_dump`'s environment, not its argv, so it is not in the container's
`ps` output either. Every client is invoked with `--no-password`, which turns a
misconfigured `pg_hba` into an immediate failure instead of an unattended 02:00 task
blocked forever on a password prompt — a hang reads as "still running", which is
exactly the silence this whole document exists to prevent.

The rehearsal's throwaway container is created with `POSTGRES_HOST_AUTH_METHOD=trust`
and **no published ports**, so it has no password at all and nothing on the host
network can reach it.

---

## Taking a backup

Pick a host directory first. Put it **outside the repository** — a backup that lives
in the working tree dies with the working tree (`git clean`, a bad rebase, a reinstall)
— and on a different physical disk if there is one. There is deliberately no default.

```powershell
# Windows PowerShell, from anywhere
[Environment]::SetEnvironmentVariable('UMS_BACKUP_DIR', 'D:\UMS-Backups', 'User')
```

```powershell
# take one now
python C:\path\to\repo\scripts\backup_database.py --out-dir D:\UMS-Backups
```

```bash
# macOS / Linux
python3 scripts/backup_database.py --out-dir ~/UMS-Backups
```

**The very first run into a brand new directory is refused on purpose.** There is no
history in it yet, and without history a healthy database and one that was wiped and
re-migrated are the same 38 tables of seeded lookup data — nothing can tell them apart.
So the first run prints what it found and stops:

```text
BACKUP REJECTED (exit 8): this run captured no usable application data, so it was NOT
published as a backup.
   quarantined=D:\UMS-Backups\ums-backup-20260825T003356Z.rejected
   tables=38 rows=187 non_seed_rows=7
   read database 'ums_smart_revenue' in cluster 7677783453675450413
   - this output directory has no watermark: watermark.json is absent and it holds no
     accepted run to rebuild one from, so only the seed floor could run. […]
     tables=38 rows=187 (7 of them outside alembic_version, currencies, tenants) cannot
     be judged here. Confirm those numbers are what this database should hold, then
     re-run ONCE with --establish-watermark.
```

> The current PR ancestry has no auth-seed migration: a virgin install is 180 rows and
> `SEED_TABLES` holds the three tables actually populated by these migrations. If a
> later migration adds seeded rows, update the script and its measurement in the same
> change; do not manufacture rows in this fixture or silently widen the floor.

Read `rows=` and, above all, **`non_seed_rows=`** — the rows outside the three seed tables
(`alembic_version`, `currencies`, `tenants`). That number is your data; the other 180 rows
are what every UMS database has on the day it is created in this ancestry. If it is what
this database should hold, run it again with the
flag — **once, by hand, never in the scheduled task**:

```powershell
python C:\path\to\repo\scripts\backup_database.py --out-dir D:\UMS-Backups --establish-watermark
```

Expected output from then on:

```text
OK backup=D:\UMS-Backups\ums-backup-20260824T220311Z
   artifacts=database.dump, roles.sql bytes=184669
   tables=38 rows=187 non_seed_rows=7
   database 'ums_smart_revenue' in cluster 7677783453675450413
   content gate: watermark 187 rows across 38 tables (watermark.json)
```

> ⚠️ **If `non_seed_rows=0`, `--establish-watermark` is refused, and that is deliberate.**
> A database whose every table outside the three seeded ones is empty is not a database
> whose numbers you can confirm — it is a freshly migrated one, or one you have just
> lost. This was measured as a live sequence: exit `8` told the operator to *"re-run ONCE
> with `--establish-watermark`"*, doing so **published the empty database**, and it then
> became the reference every later run was judged against. The remediation line no longer
> offers that flag in this state; it says to restore first. A genuinely brand-new install
> — nothing entered yet, and you know it — passes
> `--this-database-is-intentionally-empty` alongside it. It is long on purpose: it must
> not be reachable by copying a remediation line.
>
> **`non_seed_rows` is exactly as good as `SEED_TABLES` is complete**, which is why the
> migration-derived test is required whenever a seed migration lands. A later auth-seed
> migration must update the tuple and measured fixture in the same change; do not
> manufacture rows here.

**The directory also binds itself to one database.** The first accepted run records the
Postgres cluster's `system_identifier` and the database name in `watermark.json`, and a
later run against a *different* database is refused with exit `8` rather than published.
Measured: a second, unrelated UMS database backed into an established directory used to
publish exit `0` and move the high-water mark from 187 rows to 1098, after which
retention protected the foreign run as the newest one holding data. Both databases held
the same seeded tenant UUID, so nothing about the rows could have separated them. If the
database really was rebuilt — a restore into a new volume, a re-initialised cluster —
re-run **once** with `--adopt-database`. See [exit `8`](#exit-codes) cause 4.

### Exit codes

Task Scheduler shows these verbatim in **Last Run Result** (as hex: `0x2`, `0x3`, …).

| Code | Meaning | What to do |
|---|---|---|
| `0` | Backup written and verified | Nothing — but read [what a green run does not guarantee](#what-a-green-run-does-not-guarantee) once |
| `2` | No/unusable output directory, or a run directory of that name already exists | Fix `--out-dir` / `UMS_BACKUP_DIR` |
| `3` | **Docker daemon unavailable — no backup was taken** | See the Task Scheduler section |
| `4` | Postgres container not running or not accepting connections | Start the stack |
| `5` | `pg_dump` / `pg_dumpall` / `psql` failed | Read `backup.log` |
| `6` | An artifact failed verification — **the run was discarded** | Read `backup.log`; do not ignore |
| `7` | The backup **is** published, but the watermark, retention, or the status files could not be written | Check disk/permissions and what is holding the file; the run directory itself is valid |
| `8` | **The backup captured no application data — the run was quarantined** | See below |
| `9` | Unexpected internal error | Read `backup.log` and the traceback; report it |

**Exit `6` covers every artifact-verification failure, not only the roles trap.**
`EXIT_ARTIFACT_INVALID` has five raise sites in `scripts/backup_database.py`, grouped
here by cause:

- **The roles trap** — `roles.sql` is empty, or does not name **both** `app_tenant`
  and `app_platform` (`_dump_roles`). This is the case worth panicking about.
- **A malformed dump** — `database.dump` is empty, or does not start with the `PGDMP`
  custom-format magic (`_dump_database`).
- **An unreadable archive** — `pg_restore --list` **failed** on the written file
  (`_verify_dump_readable`).

An archive `pg_restore` reads perfectly and that contains nothing is deliberately
**not** in that list:

> ✅ **A dropped schema exits `8` and is quarantined, at default flags.** An earlier
> revision raised inside `_verify_dump_readable` when the table of contents was empty,
> so `DROP SCHEMA public CASCADE` exited `6` and the run directory was **discarded** —
> the quarantine-for-diagnosis this design promises was missing in exactly the case it
> was built for, and the exit code sent the operator looking at `roles.sql` for a
> problem that had nothing to do with roles. A zero TOC is not a broken archive; it is
> a faithful dump of a database with nothing in it, which is the content gate's
> question. `pg_restore` failing is still exit `6`. Measured both ways: with the schema
> dropped and not recreated, `dump_toc_entries` is `0` and the run exits `8`; with it
> dropped and recreated empty, `dump_toc_entries` is `3` and the run also exits `8`.

**Exit `8` is the content gate firing.** It has five causes, and the message names
which one:

1. **The seed floor** — `public` has no tables, or any of the three `SEED_TABLES`
   (`alembic_version`, `currencies`, `tenants`) is missing or empty. No flag overrides this. It is what a backup
   fired against a dropped schema, an unmigrated container, a truncated database, or
   somebody else's Postgres produces.
2. **The watermark** — a table that once held rows is gone or empty, a table fell below
   10% of its own high-water mark, or the whole directory's row count fell below 10% of
   its high-water mark. (That last one is *subsumed* by the per-table rules under the
   current constants and cannot fire on its own; it is kept as defence in depth against
   a future change to `COLLAPSE_ROW_FRACTION` / `TABLE_COLLAPSE_MIN_ROWS`. See
   `test_the_whole_directory_floor_is_subsumed_by_the_per_table_rules`.)
3. **No watermark at all** — the first run into a new output directory. See
   [Taking a backup](#taking-a-backup). Overridable **once** by `--establish-watermark`.
4. **A different database** — this output directory is bound to one Postgres cluster and
   one database name, and this run read another. Overridable **once** by
   `--adopt-database`, and by nothing else — in particular **not** by
   `--accept-content-drop`, which is about how many rows there are, not about which
   database they came from.
5. **An empty database on a first run** — `--establish-watermark` was passed but every
   table outside the three seeded ones holds zero rows. Overridable by
   `--this-database-is-intentionally-empty` on top of `--establish-watermark`, and by
   nothing else.

The run is **quarantined, not published**: it is renamed `ums-backup-…Z.rejected`,
which keeps its artifacts for diagnosis while making it invisible to retention, unusable
as a watermark contribution, and refused by the restore script. Retention is skipped
entirely on this path, so earlier good backups are untouched no matter how many empty
nights follow each other. `last-run.json` reads `"status": "REJECTED"`,
`"exit_code": 8`.

If the drop was deliberate — you really did delete the data, or rolled a migration back
— re-run **once** with `--accept-content-drop`. It lowers the high-water mark for the
tables the failure named and leaves every other table's mark alone, so accepting a
deletion in one table cannot quietly lower the bar protecting the rest. It can never
wave through the seed floor. Its use is recorded in `manifest.json`, `watermark.json`
(with the run that did it and a timestamp) and `last-run.json`. Do not put it in the
scheduled task.

**Every run *attempts* exactly one `backup.log` line and one terminal `last-run.json`** —
`OK`, `FAILED`, `REJECTED`, `BOOKKEEPING_FAILED` and `INTERRUPTED` alike. The *call* is
structural rather than remembered: `last-run.json` is overwritten with `RUNNING` *before*
any work starts, a terminal record replaces it on the way out, and a `finally` block
writes an `INTERRUPTED` record if nothing else did.

**The call is not the write, and this document used to conflate them.** Another process
can hold either file open with `FileShare.None` — an AV scanner, OneDrive, an editor you
left open on the backup directory — and the OS will simply refuse. Both writers used to
swallow that and print to **stderr**, which under Task Scheduler goes nowhere. Measured:

```text
last-run.json before : OK/exit=0
db state             : seeds only, every application table empty   (TOTAL DATA LOSS)
process exit code    : 8
last-run.json AFTER  : OK/exit=0
```

Three things changed, and the guarantee is now stated as what it actually is:

- **The write is retried** (5 attempts, 0.5 s apart). A share-mode lock is measured in
  seconds, so the ordinary case now just succeeds.
- **This run's record is written to `last-run-<stamp>.json`** beside the locked file. A
  *new* file name is the one write a lock on `last-run.json` cannot block, and the stamp
  means the sidecar is never itself stale. It carries a `status_note` saying why it
  exists.
- **A run whose record did not land cannot exit `0`.** It exits `7` and says so. The exit
  code is the one channel a file lock cannot block, and it is the channel Task Scheduler
  records. A run that already failed keeps its own, more specific code — turning `8` into
  `7` would hide data loss behind a bookkeeping message.

Measured after the change, same lock, a run that otherwise succeeded:

```text
WARNING: could not write last-run.json: [Errno 13] Permission denied: ...
WARNING: this run's record was written to last-run-20260825T022158Z.json
WARNING: last-run.json still shows the PREVIOUS run and could not be replaced. Do not
         read it as this run's result.
BACKUP PUBLISHED, STATUS NOT RECORDED (exit 7): the backup itself is valid, but nothing
on this box would have shown that this run happened.
process exit code = 7
```

So: **read `LastTaskResult` first and `last-run.json` second.** If they disagree, the
exit code is right and there is a `last-run-<stamp>.json` next to the stale file. On an
unattended machine `last-run.json` is still the file to look at — it just is not the
only one:

```json
{
  "status": "OK",
  "exit_code": 0,
  "run_dir": "D:\\UMS-Backups\\ums-backup-20260824T220311Z",
  "tables": 38,
  "rows": 335,
  "content_gate": { "non_seed_rows": 7, "identity": { "adopted": false } },
  "watermark_after": { "tables": 38, "rows": 335, "reset": null },
  "pruned": [],
  "unparsable_dirs": [],
  "future_dated_dirs": []
}
```

`unparsable_dirs` and `future_dated_dirs` name run-shaped directories retention refused
to interpret and **left alone**: the first is a name that is not a date, the second is a
name dated ahead of now. A non-empty `future_dated_dirs` means either a planted directory
or a clock that was wrong at 02:00 — check the clock first, then delete it. Neither list
can ever be a directory this script wrote while its clock was right.

`rows` shown here is the reference database — a virgin `alembic upgrade head` (180) plus
seven application rows.

> ✅ **A stale green status is now structurally impossible.** An earlier revision
> parsed a run directory's timestamp with `datetime.strptime` after matching
> `RUN_DIR_RE`, which accepts any `\d{8}T\d{6}`, and `main` caught only `OSError`
> around the prune call. A directory named `ums-backup-20250145T999999Z` — regex-valid,
> not a date — killed the process with an **undocumented exit 1 after the backup had
> already succeeded**: no `backup.log` line for that run, and `last-run.json` left
> showing green from the *previous* run. Three things changed. Timestamps are now
> parse-validated, and a name that matches the shape but is not a date is **left
> untouched and reported** rather than deleted or fatal. A last-resort handler turns any
> unexpected exception into exit `9` with a log line and a written status. And the
> `RUNNING` / terminal / `INTERRUPTED` sequence above means the previous run's `OK`
> stops standing the moment a new run begins. Measured: with the impostor directory
> present, the run exits `0`, prunes normally, logs its line, and prints
> `WARNING: ums-backup-20250145T999999Z matches a run directory name but its timestamp
> is not a date. It was left untouched; rename or remove it by hand.`

> ⚠️ **A directory dated in the FUTURE is refused the same way, and round 5 is why.**
> A run stamp is written by this script from this box's clock, so one ahead of now did
> not come from a run that has happened. A single planted
> `ums-backup-20990101T000000Z` used to be permanent: `reset_after` is a *name*
> comparison and is only ever set to the name of the run that carried the override, so a
> name sorting above every real run was never excluded and re-folded its counts into the
> watermark every single night. The measured cycle was exit `8` / exit `0` with
> `--accept-content-drop` / exit `8` again, for as long as the directory existed — not
> one-command recovery, but a wedge whose only sustainable end is that flag living in
> the scheduled task, which is the whole tier-2 comparison switched off. The same
> lexicographic sort made it **both** retention invariants at once: with `--keep-days 0
> --keep-min 1` it was the `--keep-min` tail *and* invariant 1's "newest run with
> content" pin, and all three real runs were deleted — including the one just published
> — leaving only the plant.
>
> **No attacker is needed.** A clock ahead at 02:00 — a dead RTC, a VM restored from a
> snapshot, NTP not yet converged — stamps one directory in the future, and *correcting*
> the skew is what makes it outrank everything after it.
>
> Such a directory is now ignored by the watermark, by the identity binding and by
> retention, which never deletes it either. Up to `STAMP_FUTURE_TOLERANCE` (5 minutes)
> of skew is treated as ordinary drift rather than as a plant. The operator is told
> twice: the `content gate: watermark …` line gains
> `; N future-dated directory(ies) ignored`, and a run that reaches retention prints
> `WARNING: <name> is dated in the FUTURE, so it is not a run that has happened … Check
> this box's clock, then delete it.` and records it under `future_dated_dirs` in
> `last-run.json`.
>
> One consequence, stated because it is a real trade: a *genuine* run stamped while the
> clock was ahead also stops contributing to the watermark until real time passes its
> stamp. It is never deleted, and it starts counting again afterwards. An unverifiable
> stamp is allowed to lower the protection it offers, never to raise the bar it sets.

### What a green run does not guarantee

Exit `0` means: the dump is a readable custom-format archive, `roles.sql` names both
RLS roles, both sha256 digests are recorded, and the payload cleared the content gate.

It does **not** mean the database was full. Here is exactly what the gate promises,
what it cannot promise, and why.

#### The three tiers, and what each one can actually see

**1. The seed floor — no override, correct with no history.** The migrations in this
PR ancestry populate **three** tables, and `SEED_TABLES` is exactly that list:

| Table | Rows | Seeded by |
|---|---:|---|
| `currencies` | 178 | `20260523_0001` — the frozen `ISO_4217_CURRENCIES_2026_05` snapshot |
| `alembic_version` | 1 | Alembic itself |
| `tenants` | 1 | `20260516_0001` — the bootstrap `ums` tenant |

A virgin `alembic upgrade head` therefore measures **38 tables / 180 rows** (measured
2026-08-25 against `postgres:18-alpine`), and exactly those three hold any of them. Any of
them missing or empty means the dump is not of a working UMS database, and no flag waves
it through.

That measurement is why the previous floor was wrong. `MIN_ROWS = 1` was justified in
the code by *"a freshly migrated, never-used database passes it (many tables, one stamp
row)"* — false by a factor of 180 at the time, and it is what let a database truncated to
nothing but `alembic_version` (38 tables, 1 row, total loss with the schema intact)
publish a green `OK`. That case now exits `8` with `seed table(s) currencies, tenants
exist but hold 0 rows`, with or without either override flag.

The floor is an **existence** test, not a count test, on purpose: hardcoding 178 would
turn an ordinary refresh of the frozen ISO-4217 snapshot into a box-wide backup outage,
and row-count regressions are the watermark's job.

> **If a future migration changes what is seeded, backups start failing closed.** That
> is the intended direction — a gate that degrades to "accept everything" when the
> schema moves is not a gate — but it does mean a migration that drops, renames or
> empties one of those three tables requires a matching edit to `CORE_SEED_TABLES` in
> `scripts/backup_database.py` and to `SEED_ROWS` in
> `tests/scripts/test_backup_content_gate.py`. The symptom is every run exiting `8`
> naming the table.
>
> A stacked migration that adds new seed catalogs uses the deliberately narrow
> `SEED_TABLE_EXTENSIONS` tuple instead. It is empty in PR #222 because this ancestry
> does not seed additional catalogs. `_required_seed_tables()` is consumed dynamically
> by the content gate, watermark/retention classification and manifest writer; restore
> then enforces the exact declared list. Tests prove an added required table is refused
> when missing or empty. The stacked migration must also extend the migration-derived
> measurement in the same commit—do not name catalogs here before their migration lands.
>
> The migration-derived test is the guard against the other direction: if a future
> migration adds rows outside the required seed tuple, `non_seed_rows` would misclassify a virgin
> database and tier 3b could stop firing. `tests/scripts/test_backup_content_gate.py`
> parses the migration sources for `op.bulk_insert(sa.table("x", …), …)` and literal
> `INSERT INTO x (…)` statements and fails if the required tuple and that set disagree, so
> the next seeding migration goes red instead of quiet.
>
> **The tuple is also held to its exact high-water mark by the seed-shrink rule.** If a
> future migration retires or changes a seeded row, the first night after that deploy
> exits `8` naming the table; **one** run with `--accept-content-drop` clears it and the
> night after needs no flag. The migration-derived test and measured fixture must be
> updated with any intentional seed change.

**1b. The identity binding — the one thing row counts cannot see.** An output directory
holds the history of exactly *one* database: its watermark, its retention decisions and
its restore set all describe that one. Nothing about tonight's row counts can notice that
they came from somewhere else. So the first accepted run records the Postgres cluster's
`system_identifier` — written once by `initdb`, **measured** unchanged across a
`docker restart` and different for a second container built the same way — together with
the database name, and every later run is checked against it.

That check exists because the failure was measured, not imagined: pointing `--container`
at a second, unrelated UMS database published exit `0` into an established directory and
moved the mark from 187 rows to 1098. Both databases carried the same seeded tenant UUID,
so a tenant check would not have separated them. It is not reachable from the default
scheduled task — the compose-label lookup errors when more than one container matches —
but `--container` / `--project` misdirection reaches it, and so does an operator with two
stacks.

The binding lives in `watermark.json` and, as a fallback, in the `source` block of the
newest published run manifest — the same two homes as the mark, with the same fail-safe
direction. A directory written before this check existed has neither, so its first run
under this version adopts silently; there is nothing to compare against, and refusing
every existing directory would be worse than the hole. Only `--adopt-database` rebinds a
directory that *does* have an identity.

**2. The watermark — a maximum over history, not last night.** `watermark.json` records
the largest row count this directory has ever seen for each table. A run is refused if
a table that once held rows is gone or empty, if a **seed** table holds fewer rows than
its mark at all, if any table with a mark of 10 or more rows falls below 10% of that
mark, or if the whole directory's row count falls below 10% of its total mark.

The per-table half is not decoration. `docker compose down -v` followed by the migrate
service on next start leaves a database that is 98% of the row count it had — 180
against a 184-row mark — with every seed intact and not one table missing. No global
percentage can see that. The emptied-table rule catches it in one line, measured:
`1 table(s) that held rows at their high-water mark are now empty: org_units`. The
seed-table rule catches the last corner of it, where the only data lost was extra rows
in a seeded table (`tenants 4->1`, 183 rows to 180), which was measured publishing exit
`0` before that rule existed.

Because the reference never moves down on its own, cumulative loss is bounded —
measured live, one probe table draining 80% a night:

```text
night   probe rows   total   watermark   verdict
    1         1000    1180           -   ACCEPTED (exit 0, establishes the watermark)
    2          200     380        1180   ACCEPTED (exit 0)
    3           40     220        1180   REJECTED (exit 8) "revenue_probe 1000->40"
    4            8     188        1180   REJECTED (exit 8) "revenue_probe 1000->8"
```

The previous single-step baseline accepted all four — 1000 → 200 → 40 → 8 is 20% each
night, over the 10% floor every time, with the reference re-anchoring after each one.
That is the shape that took 180 rows to 1 in three nights, all green.

**3. The first-run acknowledgement — the one thing that cannot be decided.** With no
watermark and no accepted run, a healthy database and one that was wiped and
re-migrated are *the same database*: 38 tables, 180 rows, seeds intact. No amount of
inspection separates them (there are no sequences in this schema to compare against —
every primary key is a UUID). So that run is refused and the numbers are printed, and
the operator confirms them once per directory with `--establish-watermark`. That also
removes the self-perpetuation: previously the first green run silently became the
reference for every run after it.

**3b. …except when the database is empty, which *is* decidable.** Tier 3's "nothing can
tell them apart" is true of the general case and false of the specific one where every
table outside `alembic_version` / `currencies` / `tenants` holds zero rows. That is
visible right there in the counts, and it was the sharpest edge left in the design:

```text
exit 8  ->  "re-run ONCE with --establish-watermark"  ->  exit 0, empty database
            published and made the reference for every run after it
```

which is exactly the 02:00 sequence after `docker compose down -v`. Two things changed.
`--establish-watermark` now **refuses** when `non_seed_rows` is `0`, and the tier-3
remediation line no longer mentions it in that state — it says to restore first. A
genuine brand-new install still has a way through, but it has to type
`--this-database-is-intentionally-empty`, which is not a flag anyone reaches by muscle
memory or by copying an error message. Every use is recorded in `manifest.json` and
`last-run.json` under `content_gate.overridden`, tagged with the flag that suppressed it.

**What still cannot be decided:** a first run with `non_seed_rows=7` against a database
that *should* hold 700. The flag remains an acknowledgement, and the printed
`non_seed_rows=` is what the operator is acknowledging.

#### What is still not guaranteed

- **A single night can lose up to 90% of the peak and stay green** (night 2 above).
  What is bounded is the *total*: the mark does not follow the loss down, so the run
  after it is refused. Ninety percent of peak is the floor, once, not per night.
- **`--establish-watermark` passed blindly over a *partially* populated database
  publishes it.** The wholly-empty case is now refused outright (tier 3b), but a database
  holding 7 rows where it should hold 700 clears every check on a first run. The flag
  remains an acknowledgement of the printed `non_seed_rows=`, not a check.
- **`--accept-content-drop`, `--adopt-database` and
  `--this-database-is-intentionally-empty` in the scheduled task would each re-arm the
  failure they exist to expose.** They are refused as an idea, not by code. Every use is
  written into `manifest.json`, `watermark.json` (`resets`, with the run and timestamp)
  and `last-run.json`, tagged with the flag responsible, so it is auditable after the
  fact.
- **Nothing bounds the *magnitude* of a watermark contribution.** The mark is a maximum
  over the counts recorded in published run manifests, and the seed floor those counts
  are re-tested against is an *existence* test. A manifest that overstates what its run
  held therefore raises the bar. That direction is fail-closed — the symptom is exit `8`,
  never a silently weakened gate — and it is recoverable in one run with
  `--accept-content-drop`. Manifests with no artifacts behind them no longer contribute
  at all; see [Retention](#retention).

  > **Round 5 corrected the "one run" half of that claim.** Recovery works because
  > `--accept-content-drop` writes `reset_after`, and `reset_after` excludes runs whose
  > *name* sorts at or below it. A directory dated in the future sorts above every real
  > run, so it was never excluded, re-folded every night, and the recovery never stuck —
  > measured as `8 / 0 / 8 / 0 / 8` indefinitely. Such directories are now refused
  > outright. For every stamp that is still read as history, the one-run recovery holds.
- **A partially restored database can land above every floor** and be published. See
  [the restore window](#the-restore-window-and-what-the-scheduled-backup-does-inside-it).

**What this means for you in practice.**

- **Keep one backup directory and do not repoint it casually.** A new `--out-dir` has
  neither a watermark nor an identity, so it needs `--establish-watermark` and a look at
  the numbers — and it is the one state in which a run against the wrong database cannot
  be caught.
- **Read `non_seed_rows=` on the OK line, at least weekly.** `rows=` includes the 180
  seeded rows every UMS database has on day one; `non_seed_rows=` is your data, and it is
  printed precisely so a human can notice a number that should not have moved.
- **Read `LastTaskResult` before `last-run.json`.** If they disagree, the exit code is
  the one that could not be blocked, and there is a `last-run-<stamp>.json` next to the
  stale file.
- **Do not delete `watermark.json` to "reset" anything.** Deleting it rebuilds the mark
  from the run manifests, which pushes it back *up* and un-does any deliberate reset.
  That is the safe direction, and it is not a reset.

### Verification the script performs on every run

1. `roles.sql` is non-empty and names **both** `app_tenant` and `app_platform`.
2. `database.dump` is non-empty and starts with the `PGDMP` custom-format magic.
3. The written host file is piped **back into the container** through
   `pg_restore --list`, which reads the archive header and the whole table of
   contents. This is the check that separates *"a file of the right size exists"* from
   *"a readable archive exists"*, and it inspects the bytes that actually landed on
   disk rather than `pg_dump`'s stdout. `pg_restore` failing is exit `6`; the entry
   count goes into the manifest (`dump_toc_entries`; 366 on the reference schema) and
   an entry count of **zero** is handed to the content gate as a no-content signal
   rather than treated as a broken artifact.
4. sha256 of both artifacts is recorded, and re-checked by the restore script.
5. The content gate: the seed floor, the watermark, and the first-run acknowledgement.

If `pg_restore --list` ever proves unreliable on your host, `--no-verify-dump` turns
it off — but understand you are removing the only completeness check, and the
rehearsal becomes the only thing standing between you and a bad backup.

### Retention

```text
--keep-days 30   delete run directories older than this   (default 30)
--keep-min 7     always keep this many most recent runs   (default 7, minimum 1)
                 that hold content — empty runs do not fill the window
```

`--keep-min` wins over `--keep-days`, so a machine that has been off for two months
does not wake up and delete its own last backup.

Retention counts **content, not directories**, and it uses the same seed floor the
gate does. A run whose manifest does not show all three `SEED_TABLES` populated does not
consume a `--keep-min` slot, and the newest run that does hold content is never deleted
— not by age, not by arithmetic, not when every other run has expired. Without that,
seven nights of a silently-empty database would fill the seven-run window and push the
last backup that actually held data out of it. A run whose manifest cannot be read counts
as content, because deleting the last good backup is unrecoverable while keeping a junk
directory only costs disk. A manifest that merely *claims* the gate accepted it is not
taken at its word: the counts it records are re-tested.

A directory dated in the **future** is outside both lists entirely — it can neither be
the pinned "newest run with content" nor be deleted. That is round 5's fix, and the
measured reason for it is that a single planted `ums-backup-20990101T000000Z` was
simultaneously the `--keep-min` tail *and* the invariant-1 pin, so one prune with
`--keep-days 0 --keep-min 1` deleted all three real runs, including the one just
published, and kept only the plant.

**And a manifest is not the artifacts it describes.** A run directory counts as a
published backup — for retention *and* for the watermark — only if it actually holds a
non-empty `database.dump` and `roles.sql`, and any artifact size its manifest records
matches the file on disk. Without that, a hand-planted `manifest.json` in a run-shaped
directory, with no dump behind it, folded straight into the high-water mark. Measured:
one claiming `org_units: 1000000000` pushed the mark to `1000000185`, after which every
run against the healthy database exited `8` naming a table that had never held those
rows. A directory failing this test is treated as **unknown** — never deleted, but never
allowed to be the "newest run with content" that retention pins either.

Sizes rather than sha256, on purpose: this runs over every run directory on every night,
and re-hashing multi-hundred-megabyte dumps to decide a watermark contribution would
trade a nightly cost against a check the restore script already performs, in full, on the
one run being restored.

Retention for accepted runs runs **only after a backup that passed the content gate and
only after `watermark.json` has been written**. A rejected run does not prune accepted
history or contribute to the watermark, but it does age-prune its own expired
`.rejected`/`.partial` siblings so repeated failed nights cannot fill the backup disk.
Pruning never runs ahead of the watermark for an accepted run, because pruning deletes
the manifests the watermark is rebuilt from.

Deletion only ever touches immediate children of the output directory whose names
match `ums-backup-YYYYMMDDTHHMMSSZ` exactly **and whose timestamp parses as a real date
that is not in the future**, plus `*.partial` directories older than a day and expired
`*.rejected` quarantined runs. Two kinds of name are left untouched and reported as a
`WARNING` on the run's output, in separate lists so the remedy is clear:

| Recorded as | Example | What it means | What to do |
|---|---|---|---|
| `unparsable_dirs` | `ums-backup-20250145T999999Z` | matches the shape, is not a date | rename or remove it by hand |
| `future_dated_dirs` | `ums-backup-20990101T000000Z` | dated ahead of now by more than `STAMP_FUTURE_TOLERANCE` (5 min) | **check this box's clock first**, then delete it |

`backup.log`, `last-run.json`, `watermark.json` and anything else you keep in that
directory are never touched. `--no-prune` skips retention entirely.

Sizing: on the reference schema (38 tables, 187 rows at the time it was measured,
nearly empty) a run is ~185 KB. Budget from your own first month of real data, not from
that number.

---

## Windows Task Scheduler

**The constraint that shapes this:** Docker Desktop starts at **user login**, not at
boot (`20_DEPLOYMENT_READINESS_AUDIT.md:469-475`). A task registered to "run whether
the user is logged on or not" will therefore fire at 02:00 on a rebooted, locked
machine into a world with no Docker daemon.

**The decision:** the script does **not** treat a missing daemon as "nothing to do".
It exits `3`, writes a `FAILED` line to `backup.log`, and rewrites `last-run.json`
with the reason. A loud failure that Task Scheduler records is strictly better than a
silent no-op that looks like a successful backup. `--wait-for-docker` then gives the
daemon a chance to come up first rather than failing on a race.

Register it with the Docker Desktop lifecycle rather than against it:

```powershell
# Run once, in a normal (non-elevated) PowerShell, on the target PC.
$repo   = 'C:\path\to\ums-smart-revenue-specs'
$python = (Get-Command python).Source          # e.g. C:\Python314\python.exe
$outDir = 'D:\UMS-Backups'

$action = New-ScheduledTaskAction `
  -Execute $python `
  -Argument "`"$repo\scripts\backup_database.py`" --out-dir `"$outDir`" --wait-for-docker 900" `
  -WorkingDirectory $repo

# Two triggers: the nightly one, and one after login for the day the PC was off.
$daily  = New-ScheduledTaskTrigger -Daily -At 02:00
$logon  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$logon.Delay = 'PT10M'                          # let Docker Desktop finish starting

# Interactive == "run only when the user is logged on". No password is stored,
# and it is the only principal under which Docker Desktop is guaranteed to exist.
$principal = New-ScheduledTaskPrincipal `
  -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15) `
  -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
  -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName 'UMS Smart Revenue - nightly database backup' `
  -Action $action -Trigger @($daily, $logon) -Principal $principal -Settings $settings
```

Why each piece:

- **`-LogonType Interactive`** — the task runs only while the operator is logged on,
  which is the only state in which Docker Desktop is running. It also means Windows
  stores no password for the task, and no elevation is needed to register it.
- **`-StartWhenAvailable`** — if the PC was off or asleep at 02:00, Windows runs the
  task as soon as it can instead of skipping the day.
- **`-AtLogOn` with a 10-minute delay** — covers "the PC rebooted overnight and nobody
  logged in until 09:00". `-MultipleInstances IgnoreNew` stops that colliding with the
  daily run.
- **`--wait-for-docker 900`** — up to 15 minutes of polling at **each** of three
  stages (the daemon answering, the container running, Postgres genuinely ready)
  before giving up with exit `3` or `4`. Worst case is therefore 45 minutes, which is
  why `-ExecutionTimeLimit` is 2 hours and not 30 minutes. "Genuinely ready" means the
  *real* server: the Postgres image runs a temporary socket-only server during
  first-boot initialisation and a naive `pg_isready` returns success inside that
  window (see [Evidence](#evidence--what-was-actually-run)).
- **`-RestartCount 3 -RestartInterval 15m`** — three more attempts if the run fails,
  which absorbs a slow Docker Desktop start without a false alarm.

Check on it:

```powershell
Get-ScheduledTaskInfo 'UMS Smart Revenue - nightly database backup' |
  Select-Object LastRunTime, LastTaskResult, NextRunTime
Get-Content D:\UMS-Backups\last-run.json
```

`LastTaskResult` of `0` is success; `3` (`0x3`) is "Docker was down"; anything else,
read `backup.log`.

> **The backup is not verified until it has been restored.** Registering the task is
> not the end of P0.1 — the rehearsal below is.

---

## The rehearsal

Do this **once, before any real CMS revenue is entered**, and then quarterly and after
any Postgres image bump. It takes about two minutes, restores into a container that is
created and destroyed for the purpose, and touches nothing that already exists.

### Step 1 — take a backup and note the numbers

```powershell
python scripts\backup_database.py --out-dir D:\UMS-Backups
```

Write down the `tables=` and `rows=` line it prints. If this is the first run into
that directory it will exit `8` and ask you to confirm those numbers — read them, then
re-run once with `--establish-watermark`. That is the only time you should ever pass
that flag for a given directory.

### Step 2 — restore it into a throwaway container

```powershell
python scripts\restore_database.py --backup-dir D:\UMS-Backups\ums-backup-20260824T220311Z --rehearse
```

This will:

1. re-hash `database.dump` and `roles.sql` against `manifest.json` and stop if either
   has rotted (exit `8`); a `manifest.json` whose `schema` field is not
   `ums-backup/1` is refused here too (exit `2`) — it was not written by a
   compatible backup run, so none of its other fields are interpreted;
2. `docker run` a disposable Postgres from the image recorded at backup time —
   verified to still exist locally first, **and verified to be a Postgres
   image** (a `postgres` / `library/postgres` repository tag, falling back to
   the image config's base reference when the image is untagged) — with
   `POSTGRES_HOST_AUTH_METHOD=trust` and no published ports. Local presence
   alone proves nothing about what an image is: a tampered manifest can name
   any local image ID, so a locally present non-Postgres image is refused
   (exit `2`). Once that image has been pruned the rehearsal refuses to start
   (exit `2`): pull a Postgres image yourself and pass
   `--rehearse-image <reference>` — that reference is yours, but it must pass
   the same Postgres check (operators can typo; the rehearsal needs a Postgres
   server). The manifest is unsigned, so its `source.image` reference is never
   executed on its own authority;
3. wait for the *real* server, not the initdb bootstrap server;
4. prove `pg_restore --list` inside the target container can actually read
   `database.dump` — a read-only probe of the header and table of contents
   that runs on **every** restore path, before anything is applied (exit `6`
   if it cannot). The sha256 check in step 1 proves the bytes match what
   backup wrote; only this probe proves *this container* can parse them;
5. apply **`roles.sql` first**, tolerating the expected
   `ERROR: role "ums" already exists` (`pg_dumpall` emits `CREATE ROLE` for the
   bootstrap superuser, which the Postgres image has already created);
6. **re-query `pg_roles` and refuse to continue** unless `app_tenant` *and*
   `app_platform` are now present (exit `5`);
7. `pg_restore --single-transaction` the archive, so a failure leaves the throwaway
   empty rather than half-populated;
8. print a per-table comparison against `manifest.json`;
9. destroy the container.

### Step 3 — read the verification table

```text
table                               manifest    restored  status
--------------------------------  ----------  ----------  ------
alembic_version                            1           1  ok
currencies                               178         178  ok
monthly_channel_revenue_facts              3           3  ok
org_units                                  2           2  ok
tenants                                    1           1  ok
youtube_channels                           2           2  ok
...
tables: manifest=38 restored=38
rows:   manifest=187 restored=187

RESTORE VERIFIED: every table matched the manifest row count.
```

Exit `0` and that final line is the pass. Anything else — `MISSING`, `EXTRA`, `SHORT`,
`OVER`, or exit `7` — is a failed rehearsal. Do not enter real data until it passes.

Repeatable-read snapshot counts and manifest verification share one holder transaction
during backup, so a restored count lower than the manifest is a failed rehearsal, not a
benign race. Everything at zero, or a whole table missing, is never acceptable.

### Step 4 — the concrete verification query

The row-count table proves the rows came back. Run this to prove the *privilege and
isolation surface* came back too — the part the roles trap destroys silently. Rehearse
with `--keep-throwaway`, then:

```powershell
$c = 'ums-restore-rehearsal-20260824T220617'   # printed by the rehearsal
```

```sql
-- Run against the restored container. The container already knows its own
-- credentials, so let it supply them rather than typing any in:
--   docker exec -i $c sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -'
SELECT 'grants_to_app_tenant',   count(*) FROM information_schema.role_table_grants
  WHERE grantee = 'app_tenant'
UNION ALL
SELECT 'grants_to_app_platform', count(*) FROM information_schema.role_table_grants
  WHERE grantee = 'app_platform'
UNION ALL
SELECT 'rls_policies',           count(*) FROM pg_policies WHERE schemaname = 'public'
UNION ALL
SELECT 'rls_enabled_tables',     count(*) FROM pg_tables
  WHERE schemaname = 'public' AND rowsecurity
UNION ALL
SELECT 'app_roles',              count(*) FROM pg_roles
  WHERE rolname IN ('app_tenant', 'app_platform')
UNION ALL
SELECT 'tenant_ctx_functions',   count(*) FROM pg_proc
  WHERE proname IN ('app_current_tenant_id', 'set_app_current_tenant_id',
                    'clear_app_current_tenant_id')
ORDER BY 1;
```

Run the identical query against the **live** container and compare. They must match
exactly. On the reference schema both sides return:

```text
app_roles              | 2
grants_to_app_platform | 129
grants_to_app_tenant   | 110
rls_enabled_tables     | 26
rls_policies           | 26
tenant_ctx_functions   | 3
```

A roles-less restore returns `0 / 0 / 0` for the first three and still returns
`26 / 26 / 3` for the rest. That is the signature of the trap, and it is why this
query is here rather than "check it looks right".

And the money query, on the restored container:

```sql
SELECT month, count(*) AS facts, sum(gross_revenue_usd) AS gross_usd
FROM monthly_channel_revenue_facts
GROUP BY month
ORDER BY month;
```

Then clean up:

```powershell
docker rm --force --volumes $c
```

---

## Real disaster recovery

If the database is gone or corrupted — including the case this whole document exists
for, someone running `docker compose down -v`, which deletes the `postgres-data` volume
and every revenue fact in it. (The audit found that command documented as ordinary
teardown; the compose header now carries an explicit warning instead.)

**Preferred: restore into a clean database.**

```powershell
# 0. Stop the nightly task for the duration. See "the restore window" below.
Disable-ScheduledTask -TaskName 'UMS Smart Revenue - nightly database backup'

# 1. Stop the stack and remove ONLY the Postgres volume.
docker compose down
docker volume rm ums-smart-revenue_postgres-data

# 2. Bring Postgres back up empty. Do NOT run migrations - the dump carries the schema.
docker compose up -d postgres

# 3. Restore, roles first. --compose finds the running container by its compose labels.
python scripts\restore_database.py --backup-dir D:\UMS-Backups\ums-backup-...Z --compose

# 4. Start the rest of the stack.
docker compose up -d

# 5. Take ONE backup by hand. Step 1 destroyed the volume, so this is a NEW Postgres
#    cluster with a new system_identifier, and the backup directory is still bound to
#    the old one - it will exit 8 naming both. Read the message, confirm the restored
#    row counts are right, then rebind the directory ONCE:
python scripts\backup_database.py --out-dir D:\UMS-Backups --adopt-database

# 6. Re-enable the task.
Enable-ScheduledTask -TaskName 'UMS Smart Revenue - nightly database backup'
```

Step 5 is not optional bookkeeping: until the directory is rebound, every nightly run
exits `8` and no new backup is published. It is also the moment to check the restore
worked — `--adopt-database` prints the row counts it is about to make the new reference,
and it will still refuse if the seed floor fails.

Step 2's "do not run migrations" matters: the archive contains the full schema at its
own Alembic revision, and `alembic_version` is restored with it. Running `alembic
upgrade head` first would leave a non-empty database that the restore then refuses.

### The restore and cutover window

The live target is no longer the database receiving `pg_restore`. Every restore,
including an empty-target restore, creates a unique `ums_restore_<id>` database with
the source encoding and locale, applies the recorded database owner/ACL, restores the
archive there in one transaction, and verifies exact table counts, the complete
manifest-declared seed floor, and the large-object count. Until those checks pass, the
live target name and its data are untouched. A failed staging restore removes only the
generated staging database; it never deletes accepted backup history or the live target.

`--allow-nonempty` now authorizes only the final verified replacement cutover. Without
it, a target that already has user objects is refused (exit `2`). It does **not**
authorize `DROP DATABASE`, `pg_restore --clean`, or an unverified overlay.

The operator must stop application and scheduler traffic for the whole command, for
example `docker compose stop app app-dev`. The script refuses if any target session is
present before staging. One target-scoped advisory lock, held from the maintenance
database, serializes restore operators. Immediately before cutover the script also:

1. sets `ALLOW_CONNECTIONS false` on the target and verified replacement;
2. terminates and boundedly drains every session on both names;
3. rechecks both session counts;
4. renames the target to `ums_previous_<id>`;
5. renames the verified `ums_restore_<id>` database to the target name;
6. applies the protected roles' settings scoped to the final database name in one
   `BEGIN`/`COMMIT` transaction; and
7. enables connections on the promoted target.

PostgreSQL cannot make steps 4 and 5 one transaction. That is the exact short
non-atomic cutover window. Failure at or after either rename triggers automatic reverse
renames while connections remain disabled. Before changing admission, the script records
the target and replacement databases' immutable `pg_database.oid` values. After **every**
rename exception — including a client timeout after the server already committed — and
after every rollback attempt, it first proves the mutating PostgreSQL backend has stopped,
then opens a fresh `psql -X` maintenance connection and re-reads each OID's current name
and `datallowconn`. Rename, `ALLOW_CONNECTIONS`, and final database-role transactions run
with a unique `PGAPPNAME` plus server-side `statement_timeout` and `lock_timeout` values
shorter than the host timeout. If `docker exec` times out, the script finds that exact
`pg_stat_activity` backend, cancels and terminates it, and requires two absent observations
before taking a catalog snapshot. Recovery decisions therefore come from settled server
state, not from whether the Docker client returned. The protected roles' cluster-level
`RESET ALL` normalization runs in one tagged `BEGIN`/`COMMIT` transaction, and the initial
`roles.sql` replay uses a separate tagged, bounded backend. Outer role rollback and staging
cleanup proceed only after either backend is proven stopped; otherwise the error identifies
its exact `application_name` and leaves state untouched for operator quiescence. The file
replay itself remains non-transactional. The error prints both captured OIDs, their exact
last-observed names/admission flags, any unexpected occupant of a reserved
name, and whether catalog-reconciled rollback completed. If backend quiescence or the
independent query fails, state is explicitly `UNAVAILABLE` and no speculative rename
command is printed; neither database is dropped.

If automatic rollback also fails, connect to the maintenance database with `psql -X`,
inspect `pg_database` first, keep application traffic stopped, and recover according to
the names printed by the error. The post-promotion recovery shape is:

```sql
SELECT oid, datname, datallowconn
FROM pg_catalog.pg_database
WHERE oid IN (<previous-live-oid>, <verified-replacement-oid>)
   OR datname IN ('<target>', 'ums_restore_<id>', 'ums_previous_<id>');

ALTER DATABASE "<target>" WITH ALLOW_CONNECTIONS false;
SELECT pg_catalog.pg_terminate_backend(pid)
FROM pg_catalog.pg_stat_activity
WHERE datname = '<target>';
ALTER DATABASE "<target>" RENAME TO "ums_restore_<id>";
ALTER DATABASE "ums_previous_<id>" RENAME TO "<target>";
ALTER DATABASE "<target>" WITH ALLOW_CONNECTIONS true;
```

If the second rename never succeeded, `<target>` is absent and the verified replacement
still has its `ums_restore_<id>` name; skip the first rename and rename only
`ums_previous_<id>` back to `<target>`. A timeout is not proof that a rename failed: run
the OID query first **only after** the error confirms the printed mutation
`application_name` is quiescent, then use only the observed-state recovery commands
printed by the error. If automatic quiescence failed, find that exact application name in
`pg_stat_activity`, cancel/terminate it, and wait until it is absent before the OID query.
If an expected OID is missing or a reserved name has an unexpected occupant, do not rename
anything until that discrepancy is understood.

On success, the previous database remains under `ums_previous_<id>` with connections
disabled. Keep it until the promoted database has passed application acceptance and a
fresh accepted backup has been published. Only then may an operator explicitly drop
that exact previous name from the maintenance database. Restore never prunes or drops
it automatically.

`roles.sql` is validated before apply, but the initial PostgreSQL role creation,
membership and cluster-global role-setting replay is **not transactionally coupled** to
the database renames. A failure there is fail-closed before cutover, but may still require
inspection and cleanup of partially applied cluster role state before retrying. Do not
describe the database-plus-roles operation as atomic.

The finalizer deliberately does **not** replay the full `roles.sql` a second time. After
capturing the live target's exact protected-role database settings, the script performs
the initial replay and records only settings explicitly scoped to the target database.
Once the verified replacement has the target name, it replaces
those database-scoped settings with `RESET ALL` plus the recorded `SET` statements inside
one transaction. A fresh catalog read follows both success and failure: a lost COMMIT
response is accepted only when the complete desired state is observed; the unchanged
prior state proves a pre-commit failure; any other state refuses connection admission and
triggers the OID-based database rollback. On a staging failure before cutover, the script
transactionally restores the captured original database settings before cleanup. On a
cutover failure, rollback keeps both databases closed, restores those original settings
only after the previous live OID is back under the target name, and enables it afterward.
Dotted custom GUC names are preserved as one complete setting name rather than truncated
at the first dot. Thus a cutover finalizer cannot leave a bare cluster-global `RESET ALL`
side effect, but this does not make the earlier full role replay atomic.

The two host artifacts are copied into an owner-only temporary directory and re-hashed
there before Docker is contacted and again at each point of use. All `psql` calls use
`-X`; live sessions, source locale, database owner/ACL, required roles, protected-role
memberships/attributes/settings, table counts, seed populations, and large objects all
fail closed. The target `POSTGRES_DB` must exactly match the source database name in the
manifest; otherwise `ALTER ROLE ... IN DATABASE` settings could be replayed against the
wrong name, so restore refuses instead of translating or guessing. Rehearsal uses the
same isolated replacement and cutover logic inside its disposable container.

Restore exit codes:

| Code | Meaning |
|---|---|
| `0` | Restored and verified |
| `2` | Bad/malformed backup metadata (including content-gate or seed-floor drift), the target is non-empty without `--allow-nonempty`, target sessions are still live, the run was quarantined (`…Z.rejected`), or the resolved rehearsal image is not Postgres |
| `3` | Docker daemon unavailable |
| `4` | Target container unavailable or could not be created |
| `5` | Role preflight/replay/finalization failed, or protected roles are missing/unsafe; inspect possibly partial cluster role state before retrying |
| `6` | Archive preflight, `pg_restore`, connection drain, staging cleanup, or database cutover failed; inspect the named database state and recovery message |
| `7` | Replacement table counts, seed populations, or large-object count did not match the manifest; no cutover occurred |
| `8` | An artifact failed its sha256 check — do not restore that run |
| `9` | Unexpected internal error — the traceback is printed above the summary |

---

## Evidence — what was actually run

### Current PR #222 repair validation — 2026-08-31

The final four-file repair was validated without creating another PostgreSQL or Docker
container because the workstation had only 12.44 GB free and the existing multi-PG run
had already exhausted Docker storage. Do not read the fault-injection tests as a claim
that a live two-rename cutover was rehearsed on this final snapshot.

| Command / check | Final result |
|---|---|
| `uv sync --extra dev --extra test --extra lint` | success — 89 packages resolved, 86 checked |
| `uv run ruff check backend tests scripts` | `All checks passed!` |
| `uv run mypy scripts/backup_database.py scripts/restore_database.py` | `Success: no issues found in 2 source files` |
| `uv run pytest -q tests/scripts/test_backup_content_gate.py` | **316 passed** — includes missing/empty stacked-seed refusal, rejected-run retention isolation, private restore staging, replacement-before-cutover ordering, late-backend quiescence for protected-role `RESET ALL`, `roles.sql`, rename, `ALLOW_CONNECTIONS`, and role COMMIT timeouts, no cleanup mutation when quiescence is unavailable, OID-based commit-then-timeout reconciliation, query-only guidance when state cannot be observed, rollback of original database-scoped role settings before admission, dotted custom GUC preservation, ACL/locale/roles and large-object gates |
| `git diff --check` | success |
| `uv run pytest -q` | **not a pass** — `UMS_TEST_DATABASE_URL` was unset, the suite reached its explicit real-Postgres setup errors, and the non-actionable run was stopped at 65% when the repair owner requested immediate closeout |

To close the remaining environment gate, provision one operator-owned disposable
PostgreSQL database, set `UMS_TEST_DATABASE_URL` to that database, then run
`uv run pytest -q`. A separate live rehearsal should inject a failure between the two
`ALTER DATABASE ... RENAME` statements and confirm the catalog state matches the unit
fault test before this becomes the production restore procedure.

Against `postgres:18-alpine` (PostgreSQL 18.4) migrated to this repository's real
Alembic head, seeded with 2 org units, 2 channels and 3
`monthly_channel_revenue_facts` rows, on Docker Engine 29.5.3.

> **Dated note — 2026-08-25: historical seed-floor snapshots.** Every figure in the
> rounds 1–4 tables below was measured against the then-current ancestry, where a
> virgin `alembic upgrade head` was **38 tables / 180 rows**. A later P0.7 snapshot
> temporarily added 148 auth-seed rows and Round 5 recorded that separate state;
> that migration is not present in the current PR #222 ancestry.
>
> **The old numbers are left exactly as they were run.** They are the evidence for what
> those rounds actually proved, and rewriting them would misrepresent when each hole was
> found and closed. Read `180` in any rounds 1–4 row as *"the virgin baseline at the time
> of that run"*. For the current PR #222 snapshot, the expected virgin floor remains
> `180` rows in the three migration-populated tables. The derived percentages in
> those rows (for example "180 rows is 98% of a 184-row mark") are likewise historical
> arithmetic, not a claim about today's database. The historical `148` non-seed-row
> reading belongs only to that later snapshot; the migration-derived test now fails if
> a future seed migration is not reflected in `SEED_TABLES`.

| Check | Result |
|---|---|
| Backup of the real migrated schema | exit `0`, 183,891 B dump + 778 B roles.sql, 38 tables / 187 rows |
| `roles.sql` content | `CREATE ROLE app_platform;` and `CREATE ROLE app_tenant;` present; **no password verifiers** |
| `pg_restore --list` pipe-back verification | 366 TOC entries |
| Restore without roles, `--single-transaction` | exit `1`, `role "app_tenant" does not exist`, **0 tables** |
| Restore without roles, default | exit `1`, `errors ignored on restore: 42`, 38 tables, 3 revenue rows, 26 policies, **0 grants** |
| Full rehearsal (`--rehearse`) | exit `0`, 38/38 tables, 187/187 rows, every table `ok` |
| Privilege surface, source vs restored | `129 / 110 / 26 / 26 / 2 / 3` on both sides |
| Backup, Docker daemon unreachable | exit `3`, `FAILED` line in `backup.log`, no artifacts |
| Backup, unknown container | exit `4` |
| Backup, no output directory | exit `2` |
| Backup against a cluster with no app roles | exit `6`, **run directory discarded**, nothing published |
| Backup against a schema that exists and is empty, no watermark | exit `8` — the seed floor fires with nothing to compare against |
| `--accept-content-drop` against an empty database | still exit `8` — the seed floor has no override |
| One override, then the next night with no flag | exit `0` — a deliberate wipe re-baselines rather than wedging every future run |
| Restore of a quarantined (`…Z.rejected`) run | exit `2`, refused |
| Retention, content-aware (`--keep-days 30 --keep-min 7`) | 1 real 40-day run + 7 recent content-free runs → `pruned=[]`; the data run survives. The pre-fix `_prune` deleted it. |
| Retention, age/`--keep-min` arithmetic (`--keep-days 30 --keep-min 2`) | 90/60/40-day runs and a stale `.partial` deleted; 20-day, 2-day and new runs kept; an unrelated directory untouched |
| Restore, `roles.sql` stripped of both roles | exit `5`, refused **before** any data restore |
| Restore, corrupted `database.dump` | exit `8`, refused before touching the target |
| Restore into a non-empty target without the flag | exit `2`, target unmodified |
| Repair of a half-populated database (`--allow-nonempty`) | exit `0`, privilege surface back to `129 / 110 / 26 / 26 / 2` |
| Task Scheduler registration | the `Action`/`Trigger`/`Principal`/`Settings` object was **built and validated in memory**; it was **not registered** |

#### Round 3 — the four holes, re-measured against the fixed script

Same container, PostgreSQL 18.4, Docker Engine 29.5.3, 2026-08-25. Every row below is
the CLI's own exit code and its own files, not a unit test.

| Check | Before | After |
|---|---|---|
| Virgin `alembic upgrade head`, measured twice | — | **38 tables / 180 rows**; only `currencies` 178, `alembic_version` 1, `tenants` 1 |
| 38 tables / 1 row (`alembic_version` only), fresh out-dir, default flags | exit `0`, `"status": "OK"`, **published** | exit `8`, quarantined — `seed table(s) currencies, tenants exist but hold 0 rows` |
| Same, with `--establish-watermark` **and** `--accept-content-drop` | — | exit `8`. The seed floor has no override |
| Same again, second run over the same gutted database | exit `0` (the first green run had become the baseline) | exit `8`. No green run exists to become one |
| Virgin database, fresh out-dir, no flag | exit `0` | exit `8` — `this output directory has no watermark … re-run ONCE with --establish-watermark` |
| Virgin database, fresh out-dir, `--establish-watermark` | — | exit `0`, `content gate: seed floor only - this run ESTABLISHES the watermark at 38 tables / 180 rows` |
| Drain 80%/night: 1000 → 200 → 40 → 8 rows in one table | all accepted (single-step baseline re-anchors: 20% each night) | `0`, `0`, **`8`**, **`8`** — `1 table(s) fell below 10% of their high-water mark: revenue_probe 1000->40` |
| `watermark.json` after that drain | — | `revenue_probe: 1000` — the mark did not follow the loss down |
| `DROP SCHEMA public CASCADE`, not recreated, **default flags** | exit `6`, run directory **discarded** | exit **`8`**, quarantined with artifacts; `dump_toc_entries = 0` |
| Same, schema recreated empty | exit `6` | exit **`8`**, quarantined; `dump_toc_entries = 3` |
| Same, with `--no-verify-dump` | exit `8` | exit `8` — the two paths now agree |
| `ums-backup-20250145T999999Z` present, retention **enabled** | uncaught `ValueError` → exit `1`, no `backup.log` line, `last-run.json` stale at the previous `OK` | exit `0`, pruned normally, one log line, fresh `last-run.json`, impostor **untouched** and reported as a `WARNING` |
| That impostor carrying a manifest claiming 1,000,000,000 rows | — | ignored; `watermark.json` still reads `revenue_probe: 1000` |
| `docker compose down -v` + auto-migrate, against a directory with history | — | exit `8` — `1 table(s) that held rows at their high-water mark are now empty: org_units` (180 rows vs a 184-row mark: **98%**, which no global fraction can catch) |
| Same, where the only lost data was extra `tenants` rows (183 → 180) | exit `0` **during this round**, which is why the seed-shrink rule exists | exit `8` — `seed table(s) fell below their high-water mark: tenants 4->1` |
| Backup pointed at a **different** Postgres (not UMS), both override flags | — | exit `6` (no app roles), and exit `8` once the roles were created — `seed table(s) alembic_version, currencies, tenants do not exist` |
| `--accept-content-drop` on a real collapse | — | exit `0`; `watermark.json` lowered `revenue_probe` to `5` and left `currencies` at `178`; `reset_after` and a `resets` entry recorded |
| The night after that override, no flag | — | exit `0`. A deliberate deletion does not wedge the box |
| The night after *that*, probe table emptied | — | exit `8`. The drain cannot resume silently from the new mark |
| `watermark.json` deleted after an override, then a run | — | exit `8` — `rebuilt from 4 accepted run manifest(s); watermark.json was absent`, mark back at `1000`. Losing the file un-does the reset **upwards** |
| Full rehearsal against a run from the fixed script | — | exit `0`, 39/39 tables, 185/185 rows, every table `ok` |
| `uv run pytest -q` (with `UMS_TEST_DATABASE_URL` set) | — | **2966 passed**, 0 failed |

#### Round 4 — five smaller findings, re-measured against the fixed script

Docker Engine 29.5.3, `postgres:18-alpine`, 2026-08-25. Three throwaway clusters were
used, each holding a minimal UMS-shaped schema (the three seed tables plus `org_units`,
`youtube_channels`, `monthly_channel_revenue_facts`, both app roles) rather than the full
Alembic head, so the `tables=` figures below are `6`, not `38`. Every row is the CLI's
own exit code and its own files.

| Check | Before | After |
|---|---|---|
| A planted `manifest.json` (no dump, no `roles.sql`) claiming `org_units: 1000000000`, then a run against the healthy database | mark inflated to `1000000185`; **exit `8` from then on** — `org_units 1000000000->2` | **exit `0`**; the planted directory contributes nothing; `watermark.json` still reads `org_units: 2` |
| Recovering a directory whose mark *was* inflated | — | one run with `--accept-content-drop` → exit `0`, mark back to the real numbers; the night after, no flag → exit `0`. **⚠️ Round 5 corrected the generality of this row**: it holds because `reset_after` can exclude the offending directory, and it was FALSE for a directory dated in the future, which `reset_after` could never exclude. See [Round 5](#round-5--the-future-dated-directory-the-untested-cli-and-a-cross-lane-regression) |
| `--container` pointed at a **second, unrelated** UMS database, same `--out-dir` | **exit `0`**, published, mark moved 187 → 1098 rows | **exit `8`**, quarantined — `this output directory is bound to database 'ums_smart_revenue' in cluster 7677783453675450413, but this run read … 7677783473962770477` |
| The original database again, next run | — | **exit `0`** — a container ID that changed is not a database that changed |
| Same second database with `--adopt-database` | — | **exit `0`**, rebound, and recorded as `OVERRIDDEN [--adopt-database] …` |
| Same second database with `--accept-content-drop` instead | — | **exit `8`** — the identity binding is not a magnitude check |
| `system_identifier` across `docker restart` (same volume) | — | `7677783130042306599` both times |
| `system_identifier` of a second container built identically | — | `7677783226065739815` — different cluster, different value |
| Seeds intact, all application data gone, fresh out-dir, **no flag** | exit `8` → *"re-run ONCE with `--establish-watermark`"* | exit `8` → *"EVERY table outside … is EMPTY … RESTORE it first … `--establish-watermark` on its own is refused in this state"* |
| The operator does exactly what the old message said | **exit `0`, the empty database published and made the reference** | **exit `8`** — the flag is refused |
| A genuine fresh install: both flags | — | **exit `0`**, recorded as `OVERRIDDEN [--this-database-is-intentionally-empty] …` |
| `last-run.json` + `backup.log` held `FileShare.None`, run ends in total data loss | exit `8`, `last-run.json` **still `OK`/`exit=0`**, complaint only on stderr | exit `8`, `last-run-20260825T022131Z.json` written with the `REJECTED` record and a `status_note` |
| Same lock, run otherwise **succeeds** | **exit `0`** over a stale green | **exit `7`** — `BACKUP PUBLISHED, STATUS NOT RECORDED`, plus `last-run.json still shows the PREVIOUS run … Do not read it as this run's result` |
| Console label on a suppressed check | every override printed as `OVERRIDDEN by --accept-content-drop`, including ones that flag cannot suppress | `OVERRIDDEN [--adopt-database] …` / `[--this-database-is-intentionally-empty] …` / `[--accept-content-drop] …` |
| Mutation matrix over `tests/scripts/test_backup_content_gate.py` (44 single-guard regressions) | 15 mutations, **13** caught | 44 mutations, **42** caught; the two survivors are proven equivalent/subsumed and are documented in the code |
| `uv run ruff check backend tests scripts` | — | `All checks passed!` |
| `uv run mypy backend` | — | `Success: no issues found in 217 source files` |
| `uv run mypy scripts/backup_database.py scripts/restore_database.py` | — | `Success: no issues found in 2 source files`. `uv run mypy scripts` as a whole is red on nine **other** scripts (57 pre-existing module-path errors, none in these two) |
| `uv run pytest tests/scripts/test_backup_content_gate.py -q` | 62 tests | **98 passed** |
| `uv run pytest -q` (with `UMS_TEST_DATABASE_URL` set) | — | **3068 passed**, 0 failed, in 10m24s |

#### Round 5 — historical snapshot: the future-dated directory, the untested CLI, and a cross-lane regression

**Provenance, because it matters here.** This subsection preserves a historical review
of a different branch snapshot; the P0.7 migration named below is not in PR #222's
current ancestry. Findings 1–3 below were found and proved with
**live CLI runs by an adversarial verifier**, not by the author of the fix; the run
transcripts quoted are theirs. Finding 4 was found by the same pass reading the P0.7
migration against `SEED_TABLES`. The measurements in the *After* column, including the 328-row
virgin state, and the mutation matrix are the fix author's own, run on 2026-08-25 against
Docker Engine 29.5.3 and `postgres:18-alpine@sha256:96d56f7f`.

**Finding 1 — a future-dated directory permanently wedged the watermark.** `reset_after`
is a *name* comparison and is only ever set to the name of the run that carried the
override, so a directory sorting above every real run could never be excluded and
re-folded its counts every night. Proved with a planted `ums-backup-20990101T000000Z`
holding real dump/roles copies and a manifest claiming `org_units: 1000000000`:

```text
night 1  no flag                 RC=8   org_units 1000000000->185
night 2  --accept-content-drop   RC=0   mark restored, reset_after set
night 3  no flag                 RC=8   mark re-inflated to 1000000640
night 4  --accept-content-drop   RC=0
night 5  no flag                 RC=8
```

The operator's only sustainable move is `--accept-content-drop` in the scheduled task —
the one flag the docs say must never go there, and the flag that disables the whole
tier-2 comparison. **A guard is lost, not just availability.** Reachable with no
attacker: a clock ahead at 02:00 stamps one directory in the future, and correcting the
skew is what makes it outrank every run afterwards.

**Finding 2 — the same directory captured BOTH retention invariants.** Proved with
`--keep-days 0 --keep-min 1`:

```text
before: 3 real runs + ums-backup-20990101T000000Z
pruned: all THREE real runs, including the one just published
after:  ums-backup-20990101T000000Z          <- only the junk survives
```

Every genuine backup deleted, protected by the invariant whose stated purpose is that
the newest run with content is never deleted.

**Finding 3 — the tests never drove the CLI.** An independent 55-mutation matrix caught
42 and left **10** survivors, two of them catastrophic:
`_execute`'s `if not outcome.accepted:` → `if False:` published a run against a **dropped
schema**, in a directory bound to a **different database**, as `OK backup=…` with `rc=0`;
and `main`'s `return report.escalate(code)` → `return code` returned `rc=0` over a stale
green with `last-run.json` held `FileShare.None`. Nothing in the test file called
`backup.main`, `_execute` or `run_backup`.

**Finding 4 — a cross-lane regression from the P0.7 roles seed.** See the seed-floor
section above. `non_seed_rows` went `0` → `148` on a virgin database, which switched off
tier 3b's refusal.

| Check | Before | After |
|---|---|---|
| Virgin `alembic upgrade head` (head `20260825_0001`), counted per table | **38 tables / 180 rows** (pre-P0.7) | **38 tables / 328 rows** — `currencies` 178, `role_permission_assignments` 106, `permissions` 26, `roles` 16, `alembic_version` 1, `tenants` 1 |
| `_non_seed_rows` on that virgin database | **148** — tier 3b silently disabled | **0** — the refusal fires again |
| `--establish-watermark` over a virgin database | would have **published** it as the permanent reference | exit `8`, `EVERY table outside … is EMPTY` |
| `_run_stamp("ums-backup-20990101T000000Z")` | a valid run timestamp | `None` — refused as history by the one function every history reader goes through |
| Five nights beside a planted `ums-backup-20990101T000000Z`, no flag | `8 / 0 / 8 / 0 / 8` forever | `0 / 0 / 0 / 0 / 0` — the plant contributes nothing and the mark stays at the real numbers. **Scope: this is the `20990101` case, and only that case.** The stamp is 73 years out, so it never becomes history within any run of this test. A stamp that wall-clock *does* reach behaves differently — see [the deferral note](#the-future-stamp-refusal-is-a-deferral-not-an-immunity) |
| `--keep-days 0 --keep-min 1` beside the same plant | all three real runs deleted, plant kept | the newest real run is pinned, nothing is deleted that should not be, and the plant is left alone and **reported** |
| Clock skew inside `STAMP_FUTURE_TOLERANCE` (5 min) | — | still read as history — a backward NTP step is not treated as an attack |
| Operator visibility of a future-dated directory | none | `; N future-dated directory(ies) ignored` on the watermark line, a `WARNING:` naming it, and `future_dated_dirs` in `last-run.json` |
| `_execute` `if not outcome.accepted:` → `if False:` | **SURVIVED** | **CAUGHT** — `test_the_cli_quarantines_a_dropped_schema_and_touches_nothing_else` |
| `main` `return report.escalate(code)` → `return code` | **SURVIVED** | **CAUGHT** — `test_the_cli_escalates_a_published_run_whose_status_did_not_land` |
| `roles.sql` missing `app_platform` (P0.1's headline trap) | no test at all | **CAUGHT** — `test_roles_sql_that_does_not_name_both_roles_is_refused` |
| Mutation matrix over `scripts/backup_database.py` — a **new, independent 60-mutation set**, not a re-run of the verifier's 55 | first pass with the code fixes in but no new tests: **52 caught, 8 survived** | after the CLI section and five targeted tests: **56 caught, 4 survived** — all four proven equivalent or platform-equivalent and argued in the code (`MIN_TABLES` in `_counts_clear_floor`; the subsumed whole-directory floor; the `rebaseline` guard; `is_file()`, which a Windows directory's `st_size` of 0 already refuses — the test that kills it on POSIX is present) |
| The five real survivors of that first pass | `_execute`'s watermark-write failure arm returning 0; `roles.sql` missing `app_platform`; an empty `roles.sql`; a `None` counts block crashing `_load_watermark`; a negative count in `watermark.json` | all five now **CAUGHT** by named tests |
| `uv run ruff check backend tests scripts` | — | `All checks passed!` |
| `uv run mypy backend` | — | `Success: no issues found in 217 source files` |
| `uv run mypy scripts/backup_database.py scripts/restore_database.py` | — | `Success: no issues found in 2 source files` |
| `uv run pytest tests/scripts/test_backup_content_gate.py -q` | 98 tests | **126 passed** |
| `uv run ruff format --check` on the three lane files | 1 of 3 failed (pre-existing reflow) | `3 files already formatted` |
| `uv run pytest -q` (with `UMS_TEST_DATABASE_URL` set, fresh `postgres:18-alpine`) | — | **3112 passed**, 0 failed, in 11m51s, on the final tree. Point-in-time: other beta lanes were landing tests in the same working tree during this round (3110 → 3111 → 3112 across three runs an hour apart), so the total is not attributable to this lane alone; this lane's own contribution is `tests/scripts/test_backup_content_gate.py` 98 → 126 |

**What round 5 did NOT do.** `scripts/restore_database.py` was re-read and needed no
change: it scans no directory, parses no run stamp, and has no seed-table coupling — a
future-dated directory cannot reach it because a restore target is always named
explicitly. And the five-night reproduction above was re-proved as a **test against the
real `main`**, with Docker and Postgres faked; it was not re-run against a live container
by the author of the fix.

#### The future-stamp refusal is a deferral, not an immunity

**Read this before treating the row above as "future-dated directories are handled".**
It is an accepted limit for the beta, not a closed hole.

`_run_stamp` returns `None` only while `stamped - now > STAMP_FUTURE_TOLERANCE`
(5 minutes). That is a comparison against *the wall clock at the moment of the run*,
not a permanent mark on the directory. Nothing is written to the directory, nothing
quarantines it, and its name never changes. So the refusal expires on its own:

- **While the stamp is still in the future**, the directory is inert. It cannot raise
  the watermark, it cannot be pruned, and it is reported as
  `; N future-dated directory(ies) ignored`. This is the tested state.
- **Once real time passes the stamp**, the very same directory is ordinary history
  again. Its manifest counts fold into the watermark on the next run, and if those
  counts are inflated the run exits `8`.
- **Recovery is one `--accept-content-drop` night** — because by then the directory's
  name sorts *below* tonight's run, so the `reset_after` that override writes finally
  excludes it. This is the case the permanent `8 / 0 / 8 / 0 / 8` wedge could never
  reach.
- **Two nights if the stamp sits inside the 5-minute tolerance.** There the directory
  is already read as history (so it folds in) while its name still sorts *above*
  tonight's run (so `reset_after` does not exclude it). Tonight's override does not
  stick; the night after, wall-clock has passed the stamp and it does.

The practical shape on the target PC: a clock that is hours or days ahead at 02:00 —
a dead RTC, a VM resumed from a snapshot, NTP not yet converged — costs **one exit-8
night plus one `--accept-content-drop` night**, once, when the clock is corrected. That
is bounded and recoverable, which is why it is accepted rather than fixed. What it is
*not* is immunity, and a reader who stops at the `0 / 0 / 0 / 0 / 0` row will believe
it is.

**A related one-line edge, same cause, different exit.** If a planted or clock-skewed
directory happens to occupy **tonight's exact run name**, the run does not reach the
content gate at all — `_execute` refuses at
`raise BackupError(EXIT_USAGE, f"{final_dir} already exists")`, before any dump is
taken. That is **exit `2`, reported as a usage error**, with nothing in the message
about clocks or stamps. The cost is one missed night; the risk is that the operator
reads "usage error" as a mistake in their own command line and looks in the wrong
place. Deleting or renaming the colliding directory clears it.

### The bug the rehearsal found

The first rehearsal failed, and the failure was in this tooling, not in Postgres:

```text
RESTORE FAILED (exit 6): psql failed ...: FATAL: the database system is shutting down
    roles.sql reported: FATAL: database "ums_smart_revenue" does not exist
```

The official Postgres image runs a **temporary server during first-boot
initialisation** that listens on the unix socket only, then shuts it down and starts
the real one. A socket-based `pg_isready` returns success inside that window, and the
very next command dies. Both scripts now treat readiness as two things in the same
poll: the server answers on **TCP** (the bootstrap server never does — it is started
with `listen_addresses=''`) *and* a real `SELECT 1` against the target database
succeeds. This is exactly the class of bug a rehearsal exists to find, and it would
otherwise have surfaced during a real incident on a freshly recreated container.

### What has *not* been verified

- **Nothing here has run against the live compose stack.** The `--compose` label
  lookup and `--project`/`--service` defaults match `docker-compose.yml` by reading it,
  and the equivalent `--container` path is proven, but the label lookup itself is
  unexercised.

  > **Update.** The compose stack *has* now been started — on the development machine,
  > under throwaway project names, never on the target PC. Starting it for the first
  > time is also what exposed the blocker recorded as **B0** in
  > [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md): until
  > `docker-compose.yml:226` was fixed, Postgres restart-looped and the database would
  > have lived in the container's writable layer. That is relevant to this document in
  > a direct way — for the whole period in which "there is no backup" was the recorded
  > risk, **there was also nothing durable to back up.** Both halves are now closed.
- **No scheduled task was registered**, on this machine or the target PC. The
  PowerShell above was validated by constructing the task object, not by running
  `Register-ScheduledTask`.
- **No real CMS revenue has been backed up.** The reference database is a seeded
  schema, not production data, so restore *duration* on a real month is unknown.
- **Windows-specific host behaviour** — Docker Desktop's start timing, WSL2 file
  behaviour on the backup path, the reboot path — remains unverified on the target
  hardware, exactly as the audit records.

---

## Open items this does not close

- **`--establish-watermark` remains the sharpest edge in this tool.** Tier 3b closed the
  wholly-empty case — the flag is now refused when `non_seed_rows` is `0` — but the flag
  is still an *acknowledgement of a printed number*, not a check. A database holding 7
  rows where it should hold 700 clears every gate on a first run and is published as the
  permanent reference. The only barrier is the operator reading the `rows=` /
  `non_seed_rows=` figures on the OK line and knowing they are wrong.

  **The current number follows the migrations actually deployed.** In PR #222's
  ancestry a virgin `alembic upgrade head` is **180 rows** in the three seeded tables.
  A future auth-seed migration must update the script, migration-derived test and
  measured fixture together. So `rows=180, non_seed_rows=0` is the *expected* reading
  for a genuine fresh install in this snapshot. Read
  **`non_seed_rows=`**, not `rows=`: it excludes the seeded rows entirely and is
  therefore the figure that does not move when a future migration seeds more. If it is
  not the number of rows you believe you have, do not pass the flag.

- **A single night can lose up to 90% of the high-water mark and stay green** if it
  empties no table, shrinks no seed table, and leaves every table with a mark of 10+
  rows above 10% of it. The *cumulative* loss is bounded — the mark does not follow the
  drain down, so the next run is refused — but one large deletion inside those bounds is
  accepted. `COLLAPSE_ROW_FRACTION` is the knob; tightening it trades false alarms
  against this.
- **The three acknowledgement flags are acknowledgements, not checks.**
  `--establish-watermark`, `--accept-content-drop`, `--adopt-database` and
  `--this-database-is-intentionally-empty` all print what they are suppressing, are all
  recorded in `manifest.json` / `watermark.json` / `last-run.json` tagged with the flag
  responsible, and none of them can touch the seed floor. Nothing stops an operator
  putting one in the scheduled task. `--this-database-is-intentionally-empty` is long
  enough to be conspicuous there; the others are not.
- **`last-run.json` can still be stale, it just cannot be stale *and* green-looking
  unnoticed.** If another process holds the file, the OS refuses the write and no amount
  of retrying changes that. What is guaranteed is that the run exits `7` (or its own
  failure code), says so on stderr, and leaves `last-run-<stamp>.json` beside it. An
  operator who reads only `last-run.json` and ignores `LastTaskResult` can still be
  misled.
- **The identity binding does not survive an operator who deletes both homes.** Removing
  `watermark.json` *and* every run manifest leaves a directory with no identity, which
  adopts silently on its next accepted run — the same state as a directory created before
  this check existed. That is the same fail-safe direction as the watermark, and it is
  the reason "keep one backup directory and do not repoint it casually" is in the
  practice list above.
- **A partially restored database can land above every floor** and be published as a
  backup of a half-populated database. See
  [the restore window](#the-restore-window-and-what-the-scheduled-backup-does-inside-it).
- **The seed floor is coupled to what the migrations seed.** A future migration that
  drops, renames or empties any of the three `SEED_TABLES` turns every backup red until
  the core/extension tuple and test fixture are updated. That direction is deliberate,
  but it is a maintenance obligation. Any intentional shrink of a required seeded
  catalog costs one `--accept-content-drop` run on the night after it deploys.
- **The migration parser that keeps `SEED_TABLES` honest is a parser.** It recognises
  `op.bulk_insert(sa.table("x", …), …)` and literal `INSERT INTO x (…)` statements, and
  a revision that seeds through a third idiom (`bind.execute(_ROLES.insert(), rows)` is
  the tempting one) drops out of its view. It fails **loudly** when that happens — two
  assertions check it still recognises both known idioms — but it cannot see an idiom
  nobody taught it.
- **A run stamped while this box's clock was ahead stops counting toward the watermark
  until real time passes its stamp — and then starts counting again.** It is never
  deleted, and while the stamp is still ahead the direction is safe (a lower bar, never
  a deleted backup). But the refusal is a comparison against the wall clock on each
  run, not a permanent property of the directory, so **it expires by itself.** When
  real time passes the stamp the directory becomes ordinary history, its counts fold
  into the watermark, and an inflated manifest exits `8` — recovered by one
  `--accept-content-drop` night, or two if the stamp sits inside the 5-minute
  tolerance. Bounded and recoverable, therefore accepted for the beta; it is **not**
  immunity, and the round-5 evidence row only covers the far-future (`20990101`) case.
  Full mechanics, including the exit-`2` collision when a plant lands on tonight's exact
  run name, are in
  [the deferral note](#the-future-stamp-refusal-is-a-deferral-not-an-immunity). The fix
  for the underlying condition is the clock, not this script.
- **Widening `SEED_TABLES` reclassifies backups taken before the widening — and the
  direction is wrong.** Retention re-tests each run's *recorded* counts against the
  current floor, and a manifest written before a table existed has no key for it, which
  reads as "empty". Such a run stops consuming a `--keep-min` slot and becomes eligible
  for deletion by age. It remains a maintenance hazard if a future release widens the
  tuple, and it is deliberately not patched here, because the obvious fix
  ("an absent seed table is drift, so call the run *unknown*") also swallows the
  dropped-schema case that retention invariant 2 is built on.

  **The mechanism, so the next person does not have to re-derive it.** Retention
  re-tests each run's recorded counts through `all(counts.get(name, 0) > 0 for name in
  SEED_TABLES)`. `counts.get(name, 0)` cannot distinguish *"this table held zero rows"*
  from *"this manifest was written before the table existed"* — both arrive as `0`. A
  manifest predating a newly seeded table has no key for it at all, so widening
  `SEED_TABLES` retroactively reclassifies it as **proven empty**. It then stops
  consuming a `--keep-min` slot and becomes deletable by age. The direction is wrong:
  widening the safety net deletes old backups.

  **The recommendation on record** is a **schema-generation stamp written into
  `manifest.json`** — a run declares which seed generation it was taken under, and
  retention compares against *that* rather than inferring intent from which keys are
  present. Absence then means "older generation", while a present-and-zero key still
  means "empty", and the dropped-schema case keeps its teeth.

  **How to decide it, not just what to build.** This is a retention-deletion path, so
  reason it through as a **reject→accept matrix over `_prune`**: for each candidate
  rule, enumerate the run states it must REJECT (dropped schema, truncated database, a
  genuinely empty run) and the states it must ACCEPT (a healthy old run written under
  an earlier generation), and confirm the rule separates them *before* writing it.
  Round 5 recorded two tests that passed with their guard deleted; a rule chosen
  without that matrix is how a test ends up ratifying the hole it was written to close.
  Do this before **any** name is added to `SEED_TABLE_EXTENSIONS`.
- **The `app-data` volume is not backed up.** P0.2 gave export artifacts and connector
  blobs a named volume. Compose and the README warn that `-v` destroys it and that the
  database backup does not protect it. It is not in this backup set, and no rehearsal
  covers it. The clean extension is a `--include-volume` streaming `tar` beside the
  dump, plus the matching restore side and its own rehearsal; that is a piece of work,
  not a flag, and it is deliberately not smuggled into P0.1. **Until it exists, an
  artifact re-request is the only recovery** — `request_export` has no dedup on
  scope+month (`reports/exports.py:383-433`), so the operator can simply ask for the
  export again.
- **Off-machine copy.** This writes to a host directory on the same PC. A disk failure
  or a ransomware event takes the database and its backups together. Point
  `UMS_BACKUP_DIR` at a second physical disk at minimum, and copy run directories to
  external or cloud storage. A run directory is self-contained and safe to copy — the
  restore script re-verifies both sha256 digests before it uses one.
- **Point-in-time recovery.** There is none. The recovery point is the last nightly
  run, so up to 24 hours of manual imports can be lost. For a one-operator beta that is
  an accepted risk; take a manual backup before and after any large import.
