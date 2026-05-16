#!/usr/bin/env bash
set -Eeuo pipefail
# Generates docs/CHECKS.md from ci/checks/manifest.yml

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST="$ROOT_DIR/ci/checks/manifest.yml"

if [ ! -f "$MANIFEST" ]; then
  echo "ERROR: manifest not found: $MANIFEST" >&2
  exit 1
fi

cat <<'EOF'
# CI Gate Checks Reference

> Auto-generated from `ci/checks/manifest.yml`. Run `make docs` to regenerate.

## Check Catalog

| ID | Description | Languages | Tool | Severity | Phase | Pre-commit |
|----|-------------|-----------|------|----------|-------|------------|
EOF

awk '
/^  - id:/ {
  if (id != "") {
    pc = (precommit == "true") ? "yes" : "no"
    tool = (tool == "") ? "-" : tool
    langs = (langs == "") ? "all" : langs
    printf "| `%s` | %s | %s | `%s` | %s | %s | %s |\n", id, desc, langs, tool, severity, phase, pc
  }
  id = substr($0, index($0, ": ") + 2)
  desc = ""; langs = ""; tool = ""; severity = ""; phase = ""; precommit = ""
}
/^    description:/ {
  desc = substr($0, index($0, ": ") + 2)
  gsub(/^"|"$/, "", desc)
}
/^    languages: \[\]/ { langs = "all" }
/^    languages: \[/ { langs = substr($0, index($0, ": ") + 2); gsub(/[\[\]]/, "", langs) }
/^    requires_tool:/ { tool = substr($0, index($0, ": ") + 2) }
/^    severity:/ { severity = substr($0, index($0, ": ") + 2) }
/^    phase:/ { phase = substr($0, index($0, ": ") + 2) }
/^    pre_commit:/ { precommit = substr($0, index($0, ": ") + 2) }
END {
  if (id != "") {
    pc = (precommit == "true") ? "yes" : "no"
    tool = (tool == "") ? "-" : tool
    langs = (langs == "") ? "all" : langs
    printf "| `%s` | %s | %s | `%s` | %s | %s | %s |\n", id, desc, langs, tool, severity, phase, pc
  }
}
' "$MANIFEST"
