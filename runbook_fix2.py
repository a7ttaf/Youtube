"""Fix the runbook docker-run blocks: app image + path receipts."""
from pathlib import Path

d = Path("Docs/20_COMPOSE_STORAGE_RUNBOOK.md")
t = d.read_text(encoding="utf-8")

# --- bash backup: replace the postgres-image capture with the app image ---
old = '''# The root archive step below runs the exact image the database backup
# recorded, so the archive and the database capture share one image identity.
image_id="$(python -c "import json,sys; \\
print(json.load(open(sys.argv[1]))['source']['image_id'])" \\
  "$database_run/database-manifest.json")"
test -n "$image_id"'''
new = '''# The root archive step below runs the APPLICATION image (the one the
# compose stack builds and runs), not the database image the manifest
# records — archive-mounted is application tooling. Resolve it explicitly.
app_image_id="$(docker image inspect ums-smart-revenue:dev --format '{{.Id}}')"
test -n "$app_image_id"
canonical_host_path="$(realpath "$UMS_APP_DATA_HOST")"'''
assert t.count(old) == 1, "bash capture"
t = t.replace(old, new, 1)

# --- bash backup docker run: app image + receipts ---
old = '''# The merged Compose model intentionally defines NO root-capable storage
# service, so this root-operator step runs one explicit, operator-owned
# docker container instead of a compose service. $image_id comes from the
# database manifest captured above (`.source.image_id`).
test -n "$image_id"
docker run --rm --user 0:0 \\
  --volume "$UMS_APP_DATA_HOST":/var/lib/ums \\
  --volume "$bundle":/backup \\
  "$image_id" \\
  python /srv/app/scripts/compose_storage.py archive-mounted \\'''
new = '''# The merged Compose model intentionally defines NO root-capable storage
# service, so this root-operator step runs one explicit, operator-owned
# docker container instead of a compose service. Both path receipts the
# mounted-marker contract requires are passed explicitly.
docker run --rm --user 0:0 \\
  --volume "$UMS_APP_DATA_HOST":/var/lib/ums \\
  --volume "$bundle":/backup \\
  --env UMS_APP_DATA_HOST="$UMS_APP_DATA_HOST" \\
  --env UMS_APP_DATA_HOST_CANONICAL_CONTRACT="$canonical_host_path" \\
  "$app_image_id" \\
  python /srv/app/scripts/compose_storage.py archive-mounted \\'''
assert t.count(old) == 1, "bash archive run"
t = t.replace(old, new, 1)

# --- PowerShell recovery: app image + receipts ---
old = '''# The root ownership-adoption step below runs the exact image the backup
# recorded; keep the archive and the database capture on one identity.
$imageId = (Get-Content (Join-Path $dbRun 'database-manifest.json') | ConvertFrom-Json).source.image_id
if (-not $imageId) { throw 'recovery manifest has no source image id' }'''
new = '''# The root ownership-adoption step below runs the APPLICATION image the
# compose stack builds (container-init is application tooling), plus both
# path receipts the mounted-marker contract requires.
$appImageId = docker image inspect ums-smart-revenue:dev --format '{{.Id}}'
if (-not $appImageId) { throw 'application image ums-smart-revenue:dev is not built' }
$canonicalHostPath = (Resolve-Path $env:UMS_APP_DATA_HOST).Path'''
assert t.count(old) == 1, "ps capture"
t = t.replace(old, new, 1)

old = '''# The merged Compose model has no root storage service; adopt restored
# ownership with one explicit, operator-owned container. $imageId comes from
# the recovery manifest read above.
docker run --rm --user 0:0 `
  --volume ${env:UMS_APP_DATA_HOST}:/var/lib/ums `
  $imageId `
  python /srv/app/scripts/compose_storage.py container-init `
    --path /var/lib/ums --app-user app'''
new = '''# The merged Compose model has no root storage service; adopt restored
# ownership with one explicit, operator-owned container. Both path receipts
# the mounted-marker contract requires are passed explicitly.
docker run --rm --user 0:0 `
  --volume ${env:UMS_APP_DATA_HOST}:/var/lib/ums `
  --env UMS_APP_DATA_HOST=$env:UMS_APP_DATA_HOST `
  --env UMS_APP_DATA_HOST_CANONICAL_CONTRACT=$canonicalHostPath `
  $appImageId `
  python /srv/app/scripts/compose_storage.py container-init `
    --path /var/lib/ums --app-user app'''
assert t.count(old) == 1, "ps run"
t = t.replace(old, new, 1)

# --- bash recovery: app image + receipts ---
old = '''
# The root ownership-adoption step below runs the exact image the backup
# recorded; keep the archive and the database capture on one identity.
image_id="$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['source']['image_id'])"   "$database_run/database-manifest.json")"
test -n "$image_id"'''
new = '''
# The root ownership-adoption step below runs the APPLICATION image the
# compose stack builds (container-init is application tooling), plus both
# path receipts the mounted-marker contract requires.
app_image_id="$(docker image inspect ums-smart-recovery:dev --format '{{.Id}}')"
test -n "$app_image_id"
canonical_host_path="$(realpath "$UMS_APP_DATA_HOST")"'''
assert t.count(old) == 1, "bash recovery capture"
t = t.replace(old, new, 1)

old = '''# The merged Compose model has no root storage service; adopt restored
# ownership with one explicit, operator-owned container. $image_id comes from
# the recovery manifest read above.
test -n "$image_id"
docker run --rm --user 0:0 \\
  --volume "$UMS_APP_DATA_HOST":/var/lib/ums \\
  "$image_id" \\
  python /srv/app/scripts/compose_storage.py container-init \\
    --path /var/lib/ums --app-user app'''
new = '''# The merged Compose model has no root storage service; adopt restored
# ownership with one explicit, operator-owned container. Both path receipts
# the mounted-marker contract requires are passed explicitly.
docker run --rm --user 0:0 \\
  --volume "$UMS_APP_DATA_HOST":/var/lib/ums \\
  --env UMS_APP_DATA_HOST="$UMS_APP_DATA_HOST" \\
  --env UMS_APP_DATA_HOST_CANONICAL_CONTRACT="$canonical_host_path" \\
  "$app_image_id" \\
  python /srv/app/scripts/compose_storage.py container-init \\
    --path /var/lib/ums --app-user app'''
assert t.count(old) == 1, "bash recovery run"
t = t.replace(old, new, 1)

d.write_bytes(t.encode("utf-8"))
print("runbook docker-run blocks corrected")
