#!/usr/bin/env bats
#
# The JS lanes this PR activates are reachable only through a chain of config:
# checks.yml decides whether preflight schedules the `node` lane at all,
# affected.yml decides whether tests.sh considers any JavaScript test affected,
# and the result contract decides whether a failure is reported as a regression
# or as broken infrastructure. Each link failed open before; these pin them.

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  cd "$REPO_ROOT"
}

# --- checks.yml gates the whole node lane -------------------------------------
#
# ci/preflight.sh skips a lane when _all_related_checks_disabled reports every
# related check disabled. With lint-js, typecheck-js, format-js and tests-js all
# `enabled: false`, the node lane never ran — so activating the workspace
# runners was not enough on its own.

_enabled_value() {
  # Echo the `enabled:` value of the given check id from checks.yml.
  awk -v want="  $1:" '
    $0 == want { found = 1; next }
    found && $0 ~ /^[[:space:]]*enabled:/ {
      sub(/^[[:space:]]*enabled:[[:space:]]*/, "")
      sub(/[[:space:]]*#.*$/, "")
      print
      exit
    }
    found && $0 ~ /^[[:space:]]*[a-z-]+:$/ { exit }
  ' ci/config/checks.yml
}

@test "js lane: tests-js is enabled" {
  [ "$(_enabled_value tests-js)" = "true" ]
}

@test "js lane: typecheck-js is enabled" {
  [ "$(_enabled_value typecheck-js)" = "true" ]
}

@test "js lane: not every JS check is disabled, so preflight schedules node" {
  local any_enabled=0 id
  for id in lint-js typecheck-js format-js tests-js; do
    [ "$(_enabled_value "$id")" = "true" ] && any_enabled=1
  done
  [ "$any_enabled" -eq 1 ]
}

# --- affected.yml maps the frontend workspace ---------------------------------

@test "affected: a frontend source change maps to frontend test patterns" {
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests frontend/src/lib/api/useGroups.ts
  [ "$status" -eq 0 ]
  [[ "$output" == *"frontend/tests/"* ]]
}

@test "affected: a frontend tsx change maps to frontend test patterns" {
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests frontend/src/components/srcc/OutcomeTable.tsx
  [ "$status" -eq 0 ]
  [[ "$output" == *"frontend/tests/"* ]]
}

@test "affected: frontend patterns end in .ts/.tsx so the javascript filter keeps them" {
  # ci/checks/tests.sh classifies a pattern as JavaScript purely by suffix. A
  # pattern the filter drops is the same as no pattern: the lane reports
  # "skipped: no affected JavaScript tests".
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests frontend/src/lib/api/useGroups.ts
  [ "$status" -eq 0 ]
  local kept=0 line
  while IFS= read -r line; do
    case "$line" in
      *.ts | *.tsx | *.js | *.jsx) kept=1 ;;
    esac
  done <<< "$output"
  [ "$kept" -eq 1 ]
}

@test "affected: a mixed frontend+python changeset still yields javascript patterns" {
  # The reported failure mode: python contributes a pattern, so AFFECTED_TESTS
  # is non-empty, and an empty javascript slice reads as "nothing to run".
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests backend/app/main.py frontend/src/lib/api/useGroups.ts
  [ "$status" -eq 0 ]
  [[ "$output" == *"frontend/tests/"* ]]
  [[ "$output" == *"test_"*".py"* ]]
}

@test "affected: a file directly under frontend/src maps to frontend test patterns" {
  # The matcher collapses "**" to "*", so "frontend/src/**/*.ts" alone requires
  # a subdirectory and misses frontend/src/test-setup.ts — a real file, and the
  # one vitest loads as setupFiles.
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests frontend/src/test-setup.ts
  [ "$status" -eq 0 ]
  [[ "$output" == *"frontend/tests/"* ]]
}

@test "affected: a change to the vitest config runs the suite" {
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests frontend/vitest.config.ts
  [ "$status" -eq 0 ]
  [[ "$output" == *"frontend/tests/"* ]]
}

# --- child exit codes keep their meaning --------------------------------------

@test "result contract: a package script exiting 1 is a new issue, not infra" {
  source ci/lib/common.sh
  [ "$(ci::common::normalize_result 1)" -eq "$CI_RESULT_FAIL_NEW_ISSUE" ]
  # Without normalization this merge yields 1, which result_severity ranks at
  # the FAIL_INFRA level.
  [ "$(ci::common::merge_results "$CI_RESULT_PASS" "$(ci::common::normalize_result 1)")" -eq "$CI_RESULT_FAIL_NEW_ISSUE" ]
}

@test "result contract: contract codes pass through normalize unchanged" {
  source ci/lib/common.sh
  [ "$(ci::common::normalize_result "$CI_RESULT_PASS")" -eq "$CI_RESULT_PASS" ]
  [ "$(ci::common::normalize_result "$CI_RESULT_PASS_WITH_KNOWN_DEBT")" -eq "$CI_RESULT_PASS_WITH_KNOWN_DEBT" ]
  [ "$(ci::common::normalize_result "$CI_RESULT_FAIL_NEW_ISSUE")" -eq "$CI_RESULT_FAIL_NEW_ISSUE" ]
  [ "$(ci::common::normalize_result "$CI_RESULT_FAIL_INFRA")" -eq "$CI_RESULT_FAIL_INFRA" ]
}

@test "result contract: an infra failure in one workspace still outranks a pass" {
  source ci/lib/common.sh
  [ "$(ci::common::merge_results "$CI_RESULT_FAIL_NEW_ISSUE" "$CI_RESULT_FAIL_INFRA")" -eq "$CI_RESULT_FAIL_INFRA" ]
}

@test "node lane: the workspace loop normalizes child results before merging" {
  run grep -n "ci::common::normalize_result" ci/checks/node.sh
  [ "$status" -eq 0 ]
}

@test "node lane: a failed dependency install stays an infra result" {
  # normalize_result maps off-contract codes to FAIL_NEW_ISSUE, which is right
  # for a failing package script and wrong for a registry outage. Every install
  # pins its own exit so provisioning failures survive that mapping.
  local mgr
  for mgr in "pnpm install --frozen-lockfile" "npm ci --quiet" \
             "yarn install --frozen-lockfile" "yarn install --immutable" \
             "bun install --frozen-lockfile"; do
    run grep -F "$mgr || exit \"\$CI_RESULT_FAIL_INFRA\"" ci/checks/node.sh
    [ "$status" -eq 0 ] || { echo "install not pinned to FAIL_INFRA: $mgr" >&2; return 1; }
  done
}

# --- package metadata triggers the lane ---------------------------------------
#
# preflight filters lanes by ci/lib/changeset.sh check ids, not affected.yml. A
# manifest classified as `json` and a lockfile as `unknown` emit no JavaScript
# check ids, so a dependency bump or a changed script skipped the node lane
# entirely — no install, tests, typecheck or build.

@test "changeset: a package manifest classifies as javascript" {
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  [ "$(ci::changeset::classify_file frontend/package.json)" = "javascript" ]
}

@test "changeset: every supported lockfile classifies as javascript" {
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  local f
  for f in bun.lock bun.lockb package-lock.json npm-shrinkwrap.json pnpm-lock.yaml yarn.lock; do
    [ "$(ci::changeset::classify_file "frontend/$f")" = "javascript" ] \
      || { echo "$f did not classify as javascript" >&2; return 1; }
  done
}

@test "changeset: a javascript classification emits the node lane check ids" {
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  run ci::changeset::_checks_for_language javascript
  [ "$status" -eq 0 ]
  [[ "$output" == *"tests-js"* ]]
  [[ "$output" == *"typecheck-js"* ]]
}

@test "changeset: an unrelated json file is still json" {
  # The manifest rule must key on the basename, not on the extension.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  [ "$(ci::changeset::classify_file Docs/example.json)" = "json" ]
}
