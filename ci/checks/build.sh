#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"

cd "$ROOT_DIR"

ci::common::section "Check: build and script validation"

script_globs='ci/*.sh ci/checks/*.sh ci/lib/*.sh .githooks/pre-push'
root_script_targets='install.sh deploy.sh'

collect_script_targets() {
  local pattern=""
  local candidate=""
  for pattern in $script_globs; do
    for candidate in $pattern; do
      if [ -f "$candidate" ]; then
        printf '%s\n' "$candidate"
      fi
    done
  done
  for candidate in $root_script_targets; do
    if [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
    fi
  done
}

syntax_targets=()
while IFS= read -r script; do
  syntax_targets+=("$script")
done < <(collect_script_targets)

for script in "${syntax_targets[@]}"; do
  echo "bash -n $script"
  bash -n "$script"
done

if ci::common::command_exists shellcheck; then
  echo "Running: shellcheck on CI scripts (advisory; hard-fail enforcement is in the CI workflow)"
  if [ "${#syntax_targets[@]}" -gt 0 ]; then
    shellcheck -x -e SC1090,SC1091,SC1094 "${syntax_targets[@]}" || true
  else
    echo "No shellcheck targets found."
  fi
else
  echo "Optional tool skipped: shellcheck not installed."
fi

echo "Build/script checks passed."
exit "$CI_RESULT_PASS"
