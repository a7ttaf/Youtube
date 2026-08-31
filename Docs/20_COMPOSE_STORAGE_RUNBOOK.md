# Compose Storage Backup and Recovery Runbook

Status: operator contract for the single-box beta Compose deployment.

This runbook covers three state planes that must move together:

1. PostgreSQL roles (`app_tenant` and `app_platform` are cluster-wide).
2. The PostgreSQL database (the source of truth for finance, audit, tenancy,
   authorization, and artifact metadata).
3. The `UMS_APP_DATA_HOST` bind (export bytes and, for
   `UMS_BLOB_BACKEND=file-store`, connector evidence).

Redis is currently unused by backend code. Its named volume is not part of the
authoritative recovery set. If Redis becomes operational state, this runbook
must change before that feature ships.

## Safety facts

- `docker compose down` preserves `postgres-data`, `redis-data`, and the host
  bind.
- `docker compose down -v` deletes the two named volumes only. It does **not**
  delete or empty `UMS_APP_DATA_HOST`.
- Never run `down -v` until the complete bundle below passes checksum
  verification and a restore has been rehearsed in a unique Compose project.
- Never point `UMS_APP_DATA_HOST` at `/`, a drive root, a home directory, the
  repository, `.`, `..`, or a directory containing unrelated files.
- Compose uses `create_host_path: false`; a missing or misspelled bind source
  fails instead of being auto-created by Docker.
- The marker alone cannot prove which host directory Docker mounted. Run every
  `up` or `run` that can invoke `app-data-init` through the host-side
  `compose_storage.py compose` wrapper. It resolves and validates the actual
  host path, mounts that resolved source, then passes configured and canonical
  receipts. Accidental direct init without those receipts, a copied marker used
  through the wrapper, or a mismatched receipt fails before any root ownership
  or mode operation. The Compose `app` and `app-dev` service entrypoints also
  refuse an ordinary no-dependency start until init has published its readiness
  marker. The base image remains usable without a Compose storage mount.
  Never set `UMS_APP_DATA_HOST_CANONICAL` or `UMS_APP_DATA_HOST_CONFIGURED`
  manually.
- These receipts are an operator safety contract, not a security boundary
  against a Docker-capable local administrator. Such an administrator can forge
  environment values, reuse an existing container configuration, or override
  the Compose service entrypoint. Those actions are unsupported; no
  in-container process can independently prove the host bind's identity.
- POSIX `chmod` inside Docker Desktop does not prove a restrictive NTFS ACL.
  The path marker is a destructive-target contract, not a Windows privacy
  claim. Use the owner-only ACL block below for backup bundles. A multi-user
  Windows deployment also needs an operator-reviewed owner-only ACL on the live
  `UMS_APP_DATA_HOST` tree before sensitive data is written.

No command in this document deletes the live bind. Recovery is rehearsed in a
new directory and a unique Compose project.

## 1. Prepare the live bind once

PowerShell:

```powershell
$env:UMS_APP_DATA_HOST = '.\data\ums'
uv run python scripts/compose_storage.py prepare `
  --path $env:UMS_APP_DATA_HOST
uv run python scripts/compose_storage.py check `
  --path $env:UMS_APP_DATA_HOST
```

POSIX shell:

```bash
export UMS_APP_DATA_HOST=./data/ums
uv run python scripts/compose_storage.py prepare \
  --path "$UMS_APP_DATA_HOST"
uv run python scripts/compose_storage.py check \
  --path "$UMS_APP_DATA_HOST"
```

The default approved safe root is `<repository>/data`, and the target must be
a strict child such as `data/ums`. For external storage, explicitly name both
the dedicated safe root and its child:

```powershell
uv run python scripts/compose_storage.py prepare `
  --safe-root 'D:\UMS-Revenue-Data' `
  --path 'D:\UMS-Revenue-Data\live'
```

The target must be a direct child of the approved safe root. An existing
non-empty unmarked directory is refused; move or inspect it rather than planting
a marker manually.

Render Compose through the same host preflight before the first start:

```powershell
uv run python scripts/compose_storage.py compose `
  --path $env:UMS_APP_DATA_HOST -- config --quiet
```

## 2. Create one coordinated backup bundle

Use a UTC timestamp with seconds. A new bundle directory prevents silent
same-day overwrite. Record running services, then stop both possible bind
writers exactly once. Do not blindly start both afterward; doing so can start
two independent schedulers. Stop any operator script or direct database client
that can write through the loopback PostgreSQL port as part of the same quiesce
window.

### Windows PowerShell 5.1+

Create an owner-only directory using the current user's SID, not localized
account names:

```powershell
$ErrorActionPreference = 'Stop'
function Assert-NativeSuccess([string] $Step) {
  if ($LASTEXITCODE -ne 0) {
    throw "$Step failed with native exit code $LASTEXITCODE"
  }
}

$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$bundle = Join-Path (Split-Path -Parent $PWD) "ums-backup-$runId"
[IO.Directory]::CreateDirectory($bundle) | Out-Null

$ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$bundleAcl = New-Object Security.AccessControl.DirectorySecurity
$bundleAcl.SetOwner($ownerSid)
$bundleAcl.SetAccessRuleProtection($true, $false)
$bundleRule = [Security.AccessControl.FileSystemAccessRule]::new(
  $ownerSid,
  [Security.AccessControl.FileSystemRights]::FullControl,
  ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
   [Security.AccessControl.InheritanceFlags]::ObjectInherit),
  [Security.AccessControl.PropagationFlags]::None,
  [Security.AccessControl.AccessControlType]::Allow
)
$bundleAcl.AddAccessRule($bundleRule)
Set-Acl -LiteralPath $bundle -AclObject $bundleAcl

$running = @(docker compose ps --status running --services)
Assert-NativeSuccess 'record running Compose services'
[IO.File]::WriteAllLines(
  (Join-Path $bundle 'running-services.txt'),
  $running,
  (New-Object Text.UTF8Encoding($false))
)
[IO.File]::WriteAllText(
  (Join-Path $bundle 'git-revision.txt'),
  ((git rev-parse HEAD) + "`n"),
  (New-Object Text.UTF8Encoding($false))
)
Assert-NativeSuccess 'record Git revision'
docker compose stop app app-dev
Assert-NativeSuccess 'stop app and app-dev writers'
```

Create both PostgreSQL files inside the container with `umask 077`, suppress
role password verifiers, validate required roles, then copy files without using
PowerShell redirection:

```powershell
docker compose exec -T postgres sh -euc '
  umask 077
  rm -f /tmp/ums-roles.sql /tmp/ums-database.dump
  pg_dumpall --roles-only --no-role-passwords -U "$POSTGRES_USER" > /tmp/ums-roles.sql
  pg_dump --format=custom --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/ums-database.dump
  test -s /tmp/ums-roles.sql
  test -s /tmp/ums-database.dump
  grep -q "CREATE ROLE app_tenant" /tmp/ums-roles.sql
  grep -q "CREATE ROLE app_platform" /tmp/ums-roles.sql
  chmod 600 /tmp/ums-roles.sql /tmp/ums-database.dump
'
Assert-NativeSuccess 'create database and roles dumps'
docker compose cp postgres:/tmp/ums-roles.sql `
  (Join-Path $bundle 'ums-roles.sql')
Assert-NativeSuccess 'copy roles dump'
docker compose cp postgres:/tmp/ums-database.dump `
  (Join-Path $bundle 'ums-database.dump')
Assert-NativeSuccess 'copy database dump'
docker compose exec -T postgres rm -f `
  /tmp/ums-roles.sql /tmp/ums-database.dump
Assert-NativeSuccess 'remove container dump staging files'
```

Archive the stopped bind using Python rather than `tar`, `date`, or binary
shell pipelines:

```powershell
uv run python scripts/compose_storage.py archive `
  --path $env:UMS_APP_DATA_HOST `
  --output (Join-Path $bundle 'ums-app-data.tgz') `
  --writers-stopped
Assert-NativeSuccess 'archive artifact and blob bind'
```

Reapply an owner-only file ACL after Docker copies the dumps, then seal every
data member into one SHA-256 manifest:

```powershell
Get-ChildItem -LiteralPath $bundle -File | ForEach-Object {
  $fileAcl = New-Object Security.AccessControl.FileSecurity
  $fileAcl.SetOwner($ownerSid)
  $fileAcl.SetAccessRuleProtection($true, $false)
  $fileRule = [Security.AccessControl.FileSystemAccessRule]::new(
    $ownerSid,
    [Security.AccessControl.FileSystemRights]::FullControl,
    [Security.AccessControl.AccessControlType]::Allow
  )
  $fileAcl.AddAccessRule($fileRule)
  Set-Acl -LiteralPath $_.FullName -AclObject $fileAcl
}

uv run python scripts/compose_storage.py manifest `
  --output (Join-Path $bundle 'SHA256SUMS.json') `
  --profile compose-recovery `
  (Join-Path $bundle 'ums-roles.sql') `
  (Join-Path $bundle 'ums-database.dump') `
  (Join-Path $bundle 'ums-app-data.tgz') `
  (Join-Path $bundle 'running-services.txt') `
  (Join-Path $bundle 'git-revision.txt')
Assert-NativeSuccess 'create backup checksum manifest'
```

Protect the new manifest with the same file-ACL loop, then verify that every
allow ACE belongs to the current SID:

```powershell
$manifest = Join-Path $bundle 'SHA256SUMS.json'
$fileAcl = New-Object Security.AccessControl.FileSecurity
$fileAcl.SetOwner($ownerSid)
$fileAcl.SetAccessRuleProtection($true, $false)
$fileRule = [Security.AccessControl.FileSystemAccessRule]::new(
  $ownerSid,
  [Security.AccessControl.FileSystemRights]::FullControl,
  [Security.AccessControl.AccessControlType]::Allow
)
$fileAcl.AddAccessRule($fileRule)
Set-Acl -LiteralPath $manifest -AclObject $fileAcl

Get-ChildItem -LiteralPath $bundle -File | ForEach-Object {
  $acl = Get-Acl -LiteralPath $_.FullName
  if (-not $acl.AreAccessRulesProtected) {
    throw "Inherited ACL remains on $($_.FullName)"
  }
  foreach ($rule in $acl.Access) {
    $sid = $rule.IdentityReference.Translate(
      [Security.Principal.SecurityIdentifier]
    )
    if ($rule.AccessControlType -eq 'Allow' -and $sid.Value -ne $ownerSid.Value) {
      throw "Unexpected allow ACE $($sid.Value) on $($_.FullName)"
    }
  }
}

uv run python scripts/compose_storage.py verify `
  --manifest $manifest `
  --artifact-archive (Join-Path $bundle 'ums-app-data.tgz')
Assert-NativeSuccess 'verify complete backup bundle'
```

### Linux/POSIX shell

The live bind is owned by the image's numeric app uid and may be unreadable to
the host operator. Archive it from the root init image, then return only the
archive's ownership to the invoking host uid/gid. Numeric `0:0` is explicitly
supported when the host operator is root; the resulting archive remains mode
`0600` and root-owned:

```bash
set -eu
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)"
bundle="$(cd .. && pwd)/ums-backup-$run_id"
mkdir "$bundle"
docker compose ps --status running --services > "$bundle/running-services.txt"
git rev-parse HEAD > "$bundle/git-revision.txt"
docker compose stop app app-dev

docker compose exec -T postgres sh -euc '
  umask 077
  rm -f /tmp/ums-roles.sql /tmp/ums-database.dump
  pg_dumpall --roles-only --no-role-passwords -U "$POSTGRES_USER" > /tmp/ums-roles.sql
  pg_dump --format=custom --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/ums-database.dump
  test -s /tmp/ums-roles.sql
  test -s /tmp/ums-database.dump
  grep -q "CREATE ROLE app_tenant" /tmp/ums-roles.sql
  grep -q "CREATE ROLE app_platform" /tmp/ums-roles.sql
  chmod 600 /tmp/ums-roles.sql /tmp/ums-database.dump
'
docker compose cp postgres:/tmp/ums-roles.sql "$bundle/ums-roles.sql"
docker compose cp postgres:/tmp/ums-database.dump "$bundle/ums-database.dump"
docker compose exec -T postgres rm -f \
  /tmp/ums-roles.sql /tmp/ums-database.dump

host_uid="$(id -u)"
host_gid="$(id -g)"
uv run python scripts/compose_storage.py compose \
  --path "$UMS_APP_DATA_HOST" -- run --rm --no-deps --user 0:0 \
  --volume "$bundle:/backup" \
  app-data-init \
  python /srv/app/scripts/compose_storage.py archive-mounted \
    --path /var/lib/ums \
    --output /backup/ums-app-data.tgz \
    --writers-stopped \
    --output-uid "$host_uid" \
    --output-gid "$host_gid"

chmod 600 "$bundle"/*
uv run python scripts/compose_storage.py manifest \
  --output "$bundle/SHA256SUMS.json" \
  --profile compose-recovery \
  "$bundle/ums-roles.sql" \
  "$bundle/ums-database.dump" \
  "$bundle/ums-app-data.tgz" \
  "$bundle/running-services.txt" \
  "$bundle/git-revision.txt"
chmod 600 "$bundle/SHA256SUMS.json"
uv run python scripts/compose_storage.py verify \
  --manifest "$bundle/SHA256SUMS.json" \
  --artifact-archive "$bundle/ums-app-data.tgz"
```

If `UMS_BLOB_BACKEND=gcs`, the artifact archive contains local exports but not
GCS connector evidence. Export the provider inventory for the exact backup
point to `gcs-snapshot.json`. The file must use this validated schema with one
record per protected object (never invent generations or checksums):

```json
{
  "bucket": "ums-raw",
  "objects": [
    {
      "crc32c": "ImIEBA==",
      "generation": "123456789",
      "name": "connector/path/object.json"
    }
  ],
  "schema": "ums-gcs-snapshot-v1"
}
```

Include it in the manifest command and declare the backend on both gates:

```bash
export UMS_GCS_BUCKET=ums-raw
test -s "$bundle/gcs-snapshot.json"
uv run python scripts/compose_storage.py manifest \
  --output "$bundle/SHA256SUMS.json" \
  --profile compose-recovery \
  --blob-backend gcs \
  --gcs-bucket "$UMS_GCS_BUCKET" \
  "$bundle/ums-roles.sql" \
  "$bundle/ums-database.dump" \
  "$bundle/ums-app-data.tgz" \
  "$bundle/running-services.txt" \
  "$bundle/git-revision.txt" \
  "$bundle/gcs-snapshot.json"
uv run python scripts/compose_storage.py verify \
  --manifest "$bundle/SHA256SUMS.json" \
  --artifact-archive "$bundle/ums-app-data.tgz" \
  --blob-backend gcs \
  --gcs-bucket "$UMS_GCS_BUCKET"
```

The command rejects a GCS manifest without a non-empty, generation-pinned
snapshot record, a canonical base64 CRC32C that decodes to four bytes, or an
exact match between the snapshot bucket and `--gcs-bucket`. The flag defaults
to `UMS_GCS_BUCKET` (then the runtime default `ums-smart-revenue-raw`), but the
explicit form above makes the recovery boundary auditable. Recovery must
restore or prove those exact object generations; otherwise the coordinated
bundle is incomplete.

## 3. Rehearse coordinated recovery

Do this before any live `down -v`. Use a unique Compose project, ports, and bind
target. Never reuse the default project name or live bind.

1. Verify the bundle again.
2. Check out the exact commit recorded in `git-revision.txt`, then prepare a new
   empty bind under the same approved safe root.
3. Start only the recovery project's PostgreSQL service.
4. Pre-create and validate the two NOLOGIN RLS roles with
   `scripts/compose_restore_roles.sql`.
5. Restore the custom-format database with `--exit-on-error`.
6. Restore the artifact archive into the empty marked bind.
7. For GCS, restore or prove every exact object generation and CRC32C from
   `gcs-snapshot.json` before any application service starts.
8. Run `app-data-init` so restored POSIX ownership is adopted by the image's
   actual app uid, then start and validate the recovery app. Initialization
   retains `.ums-restore-pending` unless that uid can traverse every restored
   directory, read every restored file, and create files in both storage roots.

Artifact publication is journaled inside the marked target before its stage is
created. An ordinary publication error rolls both roots back to the empty state
while retaining the
verified stage and journal for retry. After an abrupt process or host
interruption, rerun the exact same `restore-artifacts` command with the same
verified archive, manifest, blob backend, and GCS bucket. A `staging` retry
discards only the journal-bound partial stage and re-extracts it; later states
revalidate every staged or published byte against the archive, infer the
publication state, and resume. The completed journal and
pending marker remain until `app-data-init` proves runtime access and publishes
the application-readiness marker. Do not edit `.ums-restore-journal.json`,
`.ums-restore-pending`, readiness markers, or stage paths.

The GCS prompts below are deliberate fail-stop acknowledgments, not provider
verification. Before typing the phrase, use provider tooling to prove every
generation and CRC32C in `gcs-snapshot.json`; retain that provider output with
the rehearsal evidence.

PowerShell skeleton:

```powershell
$ErrorActionPreference = 'Stop'
function Assert-NativeSuccess([string] $Step) {
  if ($LASTEXITCODE -ne 0) {
    throw "$Step failed with native exit code $LASTEXITCODE"
  }
}

$project = "ums-recovery-$runId"
$env:UMS_POSTGRES_PORT = '55433'
$env:UMS_REDIS_PORT = '56380'
$env:UMS_APP_PORT = '58000'
$env:UMS_APP_DATA_HOST = ".\data\recovery-$runId"
$env:UMS_BLOB_BACKEND = 'file-store' # Use 'gcs' when the manifest declares GCS.
$env:UMS_GCS_BUCKET = 'ums-smart-revenue-raw'

uv run python scripts/compose_storage.py verify `
  --manifest (Join-Path $bundle 'SHA256SUMS.json') `
  --artifact-archive (Join-Path $bundle 'ums-app-data.tgz') `
  --blob-backend $env:UMS_BLOB_BACKEND `
  --gcs-bucket $env:UMS_GCS_BUCKET
Assert-NativeSuccess 'verify recovery bundle'
uv run python scripts/compose_storage.py prepare `
  --path $env:UMS_APP_DATA_HOST
Assert-NativeSuccess 'prepare empty recovery bind'

docker compose -p $project up -d --wait --wait-timeout 120 postgres
Assert-NativeSuccess 'start recovery PostgreSQL'
docker compose -p $project cp scripts/compose_restore_roles.sql `
  postgres:/tmp/compose_restore_roles.sql
Assert-NativeSuccess 'copy required-role bootstrap SQL'
docker compose -p $project exec -T postgres sh -euc `
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -f /tmp/compose_restore_roles.sql'
Assert-NativeSuccess 'restore and validate required roles'
docker compose -p $project cp (Join-Path $bundle 'ums-database.dump') `
  postgres:/tmp/ums-database.dump
Assert-NativeSuccess 'copy recovery database dump'
docker compose -p $project exec -T postgres sh -euc `
  'pg_restore --exit-on-error --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/ums-database.dump'
Assert-NativeSuccess 'restore recovery database'

uv run python scripts/compose_storage.py restore-artifacts `
  --path $env:UMS_APP_DATA_HOST `
  --archive (Join-Path $bundle 'ums-app-data.tgz') `
  --manifest (Join-Path $bundle 'SHA256SUMS.json') `
  --blob-backend $env:UMS_BLOB_BACKEND `
  --gcs-bucket $env:UMS_GCS_BUCKET
Assert-NativeSuccess 'restore local artifact and blob-root bytes'
if ($env:UMS_BLOB_BACKEND -eq 'gcs') {
  $gcsRecoveryAck = Read-Host `
    'After proving every gcs-snapshot.json generation and CRC32C, type GCS-GENERATIONS-VERIFIED'
  if ($gcsRecoveryAck -cne 'GCS-GENERATIONS-VERIFIED') {
    throw 'GCS recovery proof was not acknowledged; refusing to start the application'
  }
}
uv run python scripts/compose_storage.py compose `
  --path $env:UMS_APP_DATA_HOST -- `
  -p $project run --rm --no-deps app-data-init
Assert-NativeSuccess 'adopt restored storage ownership'
uv run python scripts/compose_storage.py compose `
  --path $env:UMS_APP_DATA_HOST -- `
  -p $project up -d --wait --wait-timeout 180 app
Assert-NativeSuccess 'start recovery application'
```

POSIX commands are identical except for shell continuation and variable syntax:

```bash
set -eu
project="ums-recovery-$run_id"
export UMS_POSTGRES_PORT=55433 UMS_REDIS_PORT=56380 UMS_APP_PORT=58000
export UMS_APP_DATA_HOST="./data/recovery-$run_id"
export UMS_BLOB_BACKEND="${UMS_BLOB_BACKEND:-file-store}"
export UMS_GCS_BUCKET="${UMS_GCS_BUCKET:-ums-smart-revenue-raw}"

uv run python scripts/compose_storage.py verify \
  --manifest "$bundle/SHA256SUMS.json" \
  --artifact-archive "$bundle/ums-app-data.tgz" \
  --blob-backend "$UMS_BLOB_BACKEND" \
  --gcs-bucket "$UMS_GCS_BUCKET"
uv run python scripts/compose_storage.py prepare --path "$UMS_APP_DATA_HOST"

docker compose -p "$project" up -d --wait --wait-timeout 120 postgres
docker compose -p "$project" cp scripts/compose_restore_roles.sql \
  postgres:/tmp/compose_restore_roles.sql
docker compose -p "$project" exec -T postgres sh -euc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -f /tmp/compose_restore_roles.sql'
docker compose -p "$project" cp "$bundle/ums-database.dump" \
  postgres:/tmp/ums-database.dump
docker compose -p "$project" exec -T postgres sh -euc \
  'pg_restore --exit-on-error --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/ums-database.dump'

uv run python scripts/compose_storage.py restore-artifacts \
  --path "$UMS_APP_DATA_HOST" \
  --archive "$bundle/ums-app-data.tgz" \
  --manifest "$bundle/SHA256SUMS.json" \
  --blob-backend "$UMS_BLOB_BACKEND" \
  --gcs-bucket "$UMS_GCS_BUCKET"
if [ "$UMS_BLOB_BACKEND" = "gcs" ]; then
  printf '%s\n' \
    'After proving every gcs-snapshot.json generation and CRC32C, type GCS-GENERATIONS-VERIFIED'
  IFS= read -r gcs_recovery_ack
  if [ "$gcs_recovery_ack" != "GCS-GENERATIONS-VERIFIED" ]; then
    printf '%s\n' 'GCS recovery proof was not acknowledged; refusing application start' >&2
    exit 1
  fi
fi
uv run python scripts/compose_storage.py compose \
  --path "$UMS_APP_DATA_HOST" -- \
  -p "$project" run --rm --no-deps app-data-init
uv run python scripts/compose_storage.py compose \
  --path "$UMS_APP_DATA_HOST" -- \
  -p "$project" up -d --wait --wait-timeout 180 app
```

Validate at minimum:

- `docker compose -p <project> ps` reports PostgreSQL and app healthy.
- SQL counts for finance facts, audit events, tenants, users, export jobs, and
  connector raw files match the backup record.
- Both `app_tenant` and `app_platform` remain `NOLOGIN`, `NOBYPASSRLS`, and
  non-superuser.
- A known export downloads and matches its stored checksum.
- A known connector blob reads and matches its database checksum.
- Missing auth, insufficient permission, and tenant-scoped reads still fail
  closed.

The roles-only dump is a sealed cluster-role inventory. Do not blindly replay it
into Compose: the bootstrap `UMS_DB_USER` already exists on a fresh cluster.
The tracked SQL script restores the two roles referenced by database grants and
RLS policies idempotently. If `ums-roles.sql` contains additional application
login roles or memberships, stop and obtain a DBA-reviewed replay plan instead
of ignoring duplicate-role or grant errors.

After the rehearsal, `docker compose -p <unique-project> down -v` removes only
that recovery project's named volumes. The recovery bind still remains by
design. Record its exact canonical path before any later manual removal; never
delete the live bind as part of recovery-project cleanup.

## Migration and rollback impact

No migration/backfill required. PostgreSQL remains the sole source of truth.
This hardening changes Compose admission and operator recovery behavior only.
An empty existing PR 221 bind directory needs one explicit adoption step:

```powershell
uv run python scripts/compose_storage.py prepare --path $env:UMS_APP_DATA_HOST
```

The command refuses non-empty unmarked directories. For an existing populated
bind, first back it up and inspect it, then move the two known subdirectories
into a newly prepared target. Do not hand-create or copy the marker.
