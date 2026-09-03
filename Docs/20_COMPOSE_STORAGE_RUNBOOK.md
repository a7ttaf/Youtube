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

Create one atomic semantic database package. The database tool counts/locks
every table in an exported PostgreSQL snapshot before `pg_dump`, copies only the
tracked password-free role SQL, and refuses a seed-only install. The explicit
bundle mode requires the owner-only P0-a directory created above:

```powershell
$dbContainer = (docker compose ps -q postgres).Trim()
if (-not $dbContainer) { throw 'Compose postgres container is not running' }
uv run python scripts/backup_database.py `
  --container $dbContainer `
  --out-dir $bundle `
  --coordinated-bundle `
  --confirm-writers-quiesced
Assert-NativeSuccess 'create database backup package'
$dbRuns = @(Get-ChildItem -LiteralPath $bundle -Directory |
  Where-Object { $_.Name -match '^ums-database-backup-\d{8}T\d{6}Z-[0-9a-f]{8}$' })
if ($dbRuns.Count -ne 1) {
  throw "Expected exactly one database package, found $($dbRuns.Count)"
}
$dbRun = $dbRuns[0].FullName
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
Get-ChildItem -LiteralPath $bundle -File -Recurse | ForEach-Object {
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
  (Join-Path $dbRun 'roles.sql') `
  (Join-Path $dbRun 'database.dump') `
  (Join-Path $dbRun 'database-manifest.json') `
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

Get-ChildItem -LiteralPath $bundle -File -Recurse | ForEach-Object {
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

db_container="$(docker compose ps -q postgres)"
test -n "$db_container"
uv run python scripts/backup_database.py \
  --container "$db_container" \
  --out-dir "$bundle" \
  --coordinated-bundle \
  --confirm-writers-quiesced
database_runs="$(find "$bundle" -mindepth 1 -maxdepth 1 -type d \
  -name 'ums-database-backup-????????T??????Z-????????' -print)"
test -n "$database_runs"
test "$(printf '%s\n' "$database_runs" | wc -l)" -eq 1
database_run="$database_runs"

# The root archive step below runs the APPLICATION image the compose stack
# builds (archive-mounted is application tooling, not database tooling).
app_image_id="$(docker image inspect ums-smart-revenue:dev --format '{{.Id}}')"
test -n "$app_image_id"
canonical_host_path="$(realpath "$UMS_APP_DATA_HOST")"

host_uid="$(id -u)"
host_gid="$(id -g)"
# The merged Compose model intentionally defines NO root-capable storage
# service, so this root-operator step runs one explicit, operator-owned
# docker container instead of a compose service. Both path receipts the
# mounted-marker contract requires are passed explicitly.
docker run --rm --user 0:0 \
  --volume "$UMS_APP_DATA_HOST":/var/lib/ums \
  --volume "$bundle":/backup \
  --env UMS_APP_DATA_HOST="$UMS_APP_DATA_HOST" \
  --env UMS_APP_DATA_HOST_CANONICAL_CONTRACT="$canonical_host_path" \
  "$app_image_id" \
  python /srv/app/scripts/compose_storage.py archive-mounted \
    --path /var/lib/ums \
    --output /backup/ums-app-data.tgz \
    --writers-stopped \
    --output-uid "$host_uid" \
    --output-gid "$host_gid"

find "$bundle" -type d -exec chmod 700 {} \;
find "$bundle" -type f -exec chmod 600 {} \;
uv run python scripts/compose_storage.py manifest \
  --output "$bundle/SHA256SUMS.json" \
  --profile compose-recovery \
  "$database_run/roles.sql" \
  "$database_run/database.dump" \
  "$database_run/database-manifest.json" \
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
  "$database_run/roles.sql" \
  "$database_run/database.dump" \
  "$database_run/database-manifest.json" \
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
4. Run `scripts/restore_database.py` against the clean PostgreSQL container.
   It verifies owner-only provenance and pinned artifact identities, the
   semantic manifest, current canonical NOLOGIN role SQL, archive TOC, exact
   image/bootstrap user, dedicated cluster, version/locale, zero-user-object
   target, table counts, and Alembic head. It applies roles before the
   single-transaction dump.
5. Treat any database verification difference as a failed rehearsal.
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
$dbRuns = @(Get-ChildItem -LiteralPath $bundle -Directory |
  Where-Object { $_.Name -match '^ums-database-backup-\d{8}T\d{6}Z-[0-9a-f]{8}$' })
if ($dbRuns.Count -ne 1) {
  throw "Expected exactly one database package, found $($dbRuns.Count)"
}
$dbRun = $dbRuns[0].FullName

# The root ownership-adoption step below runs the APPLICATION image the
# compose stack builds (container-init is application tooling).
$appImageId = docker image inspect ums-smart-revenue:dev --format '{{.Id}}'
if (-not $appImageId) { throw 'application image ums-smart-revenue:dev is not built' }
$canonicalHostPath = (Resolve-Path $env:UMS_APP_DATA_HOST).Path
uv run python scripts/compose_storage.py prepare `
  --path $env:UMS_APP_DATA_HOST
Assert-NativeSuccess 'prepare empty recovery bind'

docker compose -p $project up -d --wait --wait-timeout 120 postgres
Assert-NativeSuccess 'start recovery PostgreSQL'
$recoveryDbContainer = (docker compose -p $project ps -q postgres).Trim()
if (-not $recoveryDbContainer) { throw 'Recovery postgres container is unavailable' }
uv run python scripts/restore_database.py `
  --backup-dir $dbRun `
  --target-container $recoveryDbContainer `
  --confirm-clean-target
Assert-NativeSuccess 'restore and verify recovery database'

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
# The merged Compose model has no root storage service; adopt restored
# the recovery manifest read above.
docker run --rm --user 0:0 `
  --volume ${env:UMS_APP_DATA_HOST}:/var/lib/ums `
  --env UMS_APP_DATA_HOST=$env:UMS_APP_DATA_HOST `
  --env UMS_APP_DATA_HOST_CANONICAL_CONTRACT=$canonicalHostPath `
  $appImageId `
  python /srv/app/scripts/compose_storage.py container-init `
    --path /var/lib/ums --app-user app
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
database_runs="$(find "$bundle" -mindepth 1 -maxdepth 1 -type d \
  -name 'ums-database-backup-????????T??????Z-????????' -print)"
test -n "$database_runs"
test "$(printf '%s\n' "$database_runs" | wc -l)" -eq 1
database_run="$database_runs"

# The root ownership-adoption step below runs the APPLICATION image the
# compose stack builds (container-init is application tooling).
app_image_id="$(docker image inspect ums-smart-revenue:dev --format '{{.Id}}')"
test -n "$app_image_id"
canonical_host_path="$(realpath "$UMS_APP_DATA_HOST")"

docker compose -p "$project" up -d --wait --wait-timeout 120 postgres
recovery_db_container="$(docker compose -p "$project" ps -q postgres)"
test -n "$recovery_db_container"
uv run python scripts/restore_database.py \
  --backup-dir "$database_run" \
  --target-container "$recovery_db_container" \
  --confirm-clean-target

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
# The merged Compose model has no root storage service; adopt restored
# ownership with one explicit, operator-owned container. Both path
# receipts the mounted-marker contract requires are passed explicitly.
docker run --rm --user 0:0 \
  --volume "$UMS_APP_DATA_HOST":/var/lib/ums \
  --env UMS_APP_DATA_HOST="$UMS_APP_DATA_HOST" \
  --env UMS_APP_DATA_HOST_CANONICAL_CONTRACT="$canonical_host_path" \
  "$app_image_id" \
  python /srv/app/scripts/compose_storage.py container-init \
    --path /var/lib/ums --app-user app
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

The database package's `roles.sql` must remain byte-identical to the tracked
`scripts/compose_restore_roles.sql`. It restores only the two NOLOGIN roles
referenced by database grants and RLS policies, idempotently and with privileged
attribute drift checks. A cluster-wide `pg_dumpall --roles-only` inventory is
deliberately not accepted: it could replay unrelated logins, memberships, or
password verifiers.

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

## Cross-PR integration warning

PR 225 was authored against the pre-PR221 Compose and storage contract. Its
current branch overlaps `.env.example`, `README.md`, and `docker-compose.yml`;
the overlapping documentation still describes application artifact/blob files
as ephemeral, omits this coordinated backup and recovery contract, and uses
plain `docker compose up` lifecycle commands.

Re-author PR 225 after PR 221 lands. Do not merge or cherry-pick its overlapping
hunks as-is. The re-authored change must preserve the prepared host bind,
wrapper-issued invocation receipt, Compose-only readiness entrypoint,
`create_host_path: false`, and this coordinated database-plus-artifact recovery
runbook. Re-run Compose rendering and the storage contract tests after the
re-author.
