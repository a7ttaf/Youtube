"""Fix the runbook docker-run blocks: app image + path receipts."""
from pathlib import Path

d = Path("Docs/20_COMPOSE_STORAGE_RUNBOOK.md")
t = d.read_text(encoding="utf-8")

old = 'image_id="$(python -c "import json,sys; print(json.load(open(sys.argv[1]))[\'source\'][\'image_id\'])"   "$database_run/database-manifest.json")"\ntest -n "$image_id"\n'
new = (
    "# The root archive step below runs the APPLICATION image (the one the\n"
    "# compose stack builds and runs), not the database image the manifest\n"
    "# records -- archive-mounted is application tooling. Resolve it explicitly\n"
    "# and capture the canonical host path for the marker receipts.\n"
    'app_image_id="$(docker image inspect ums-smart-revenue:dev --format \'{{.Id}}\')"\n'
    'test -n "$app_image_id"\n'
    'canonical_host_path="$(realpath "$UMS_APP_DATA_HOST")"\n'
)
assert t.count(old) == 1, "capture"
t = t.replace(old, new, 1)

old = (
    "# The merged Compose model intentionally defines NO root-capable storage\n"
    "# service, so this root-operator step runs one explicit, operator-owned\n"
    "# docker container instead of a compose service. $image_id comes from the\n"
    "# database manifest captured above (`.source.image_id`).\n"
    'test -n "$image_id"\n'
    "docker run --rm --user 0:0 \\\n"
    '  --volume "$UMS_APP_DATA_HOST":/var/lib/ums \\\n'
    '  --volume "$bundle":/backup \\\n'
    '  "$image_id" \\\n'
)
new = (
    "# The merged Compose model intentionally defines NO root-capable storage\n"
    "# service, so this root-operator step runs one explicit, operator-owned\n"
    "# docker container instead of a compose service. Both path receipts the\n"
    "# mounted-marker contract requires are passed explicitly.\n"
    "docker run --rm --user 0:0 \\\n"
    '  --volume "$UMS_APP_DATA_HOST":/var/lib/ums \\\n'
    '  --volume "$bundle":/backup \\\n'
    '  --env UMS_APP_DATA_HOST="$UMS_APP_DATA_HOST" \\\n'
    '  --env UMS_APP_DATA_HOST_CANONICAL_CONTRACT="$canonical_host_path" \\\n'
    '  "$app_image_id" \\\n'
)
assert t.count(old) == 1, "archive run"
t = t.replace(old, new, 1)

# PowerShell recovery.
old = (
    "# The root ownership-adoption step below runs the exact image the backup\n"
    "# recorded; keep the archive and the database capture on one identity.\n"
    "$imageId = (Get-Content (Join-Path $dbRun 'database-manifest.json') | ConvertFrom-Json).source.image_id\n"
    "if (-not $imageId) { throw 'recovery manifest has no source image id' }\n"
)
new = (
    "# The root ownership-adoption step below runs the APPLICATION image the\n"
    "# compose stack builds (container-init is application tooling), plus the\n"
    "# canonical host path for the marker receipts.\n"
    "$appImageId = docker image inspect ums-smart-revenue:dev --format '{{.Id}}'\n"
    "if (-not $appImageId) { throw 'application image ums-smart-revenue:dev is not built' }\n"
    "$canonicalHostPath = (Resolve-Path $env:UMS_APP_DATA_HOST).Path\n"
)
assert t.count(old) == 1, "ps capture"
t = t.replace(old, new, 1)

old = (
    "# The merged Compose model has no root storage service; adopt restored\n"
    "# ownership with one explicit, operator-owned container. $imageId comes from\n"
    "# the recovery manifest read above.\n"
    "docker run --rm --user 0:0 `\n"
    "  --volume ${env:UMS_APP_DATA_HOST}:/var/lib/ums `\n"
    "  $imageId `\n"
)
new = (
    "# The merged Compose model has no root storage service; adopt restored\n"
    "# ownership with one explicit, operator-owned container. Both path\n"
    "# receipts the mounted-marker contract requires are passed explicitly.\n"
    "docker run --rm --user 0:0 `\n"
    "  --volume ${env:UMS_APP_DATA_HOST}:/var/lib/ums `\n"
    "  --env UMS_APP_DATA_HOST=$env:UMS_APP_DATA_HOST `\n"
    "  --env UMS_APP_DATA_HOST_CANONICAL_CONTRACT=$canonicalHostPath `\n"
    "  $appImageId `\n"
)
assert t.count(old) == 1, "ps run"
t = t.replace(old, new, 1)

# Bash recovery.
old = (
    "# The root ownership-adoption step below runs the exact image the backup\n"
    "# recorded; keep the archive and the database capture on one identity.\n"
    'image_id="$(python -c "import json,sys; print(json.load(open(sys.argv[1]))[\'source\'][\'image_id\'])"   "$database_run/database-manifest.json")"\n'
    'test -n "$image_id"'
)
new = (
    "# The root ownership-adoption step below runs the APPLICATION image the\n"
    "# compose stack builds (container-init is application tooling), plus the\n"
    "# canonical host path for the marker receipts.\n"
    'app_image_id="$(docker image inspect ums-smart-recovery:dev --format \'{{.Id}}\')"\n'
    'test -n "$app_image_id"\n'
    'canonical_host_path="$(realpath "$UMS_APP_DATA_HOST")"'
)
assert t.count(old) == 1, "bash recovery capture"
t = t.replace(old, new, 1)

old = (
    "# The merged Compose model has no root storage service; adopt restored\n"
    "# ownership with one explicit, operator-owned container. $image_id comes from\n"
    "# the recovery manifest read above.\n"
    'test -n "$image_id"\n'
    "docker run --rm --user 0:0 \\\n"
    '  --volume "$UMS_APP_DATA_HOST":/var/lib/ums \\\n'
    '  "$image_id" \\\n'
)
new = (
    "# The merged Compose model has no root storage service; adopt restored\n"
    "# ownership with one explicit, operator-owned container. Both path\n"
    "# receipts the mounted-marker contract requires are passed explicitly.\n"
    "docker run --rm --user 0:0 \\\n"
    '  --volume "$UMS_APP_DATA_HOST":/var/lib/ums \\\n'
    '  --env UMS_APP_DATA_HOST="$UMS_APP_DATA_HOST" \\\n'
    '  --env UMS_APP_DATA_HOST_CANONICAL_CONTRACT="$canonical_host_path" \\\n'
    '  "$app_image_id" \\\n'
)
assert t.count(old) == 1, "bash recovery run"
t = t.replace(old, new, 1)

d.write_bytes(t.encode("utf-8"))
print("all three docker-run blocks corrected")
