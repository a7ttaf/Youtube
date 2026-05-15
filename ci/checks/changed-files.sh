#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/git.sh
source "$ROOT_DIR/ci/lib/git.sh"

cd "$ROOT_DIR"

ci::common::section "Check: changed files"

mkdir -p ci/reports

ci::git::changed_files > ci/reports/changed-files.txt
ci::git::staged_files > ci/reports/staged-files.txt
ci::git::untracked_files > ci/reports/untracked-files.txt

echo "Changed files: $(wc -l < ci/reports/changed-files.txt | tr -d '[:space:]')"
echo "Staged files: $(wc -l < ci/reports/staged-files.txt | tr -d '[:space:]')"
echo "Untracked files: $(wc -l < ci/reports/untracked-files.txt | tr -d '[:space:]')"

exit "$CI_RESULT_PASS"
