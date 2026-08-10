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

@test "affected: a root-level frontend test maps to frontend test patterns" {
  # The declared vitest glob permits frontend/tests/App.test.tsx, but
  # "frontend/tests/**/*.tsx" cannot match it once ** collapses to *.
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests frontend/tests/App.test.tsx
  [ "$status" -eq 0 ]
  [[ "$output" == *"frontend/tests/"* ]]
}

@test "affected: a root-level frontend test in a mixed changeset still yields js patterns" {
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests backend/app/main.py frontend/tests/App.test.tsx
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

# --- the node lane cannot lose its own suite ----------------------------------
#
# These drive ci/checks/node.sh against a synthetic workspace. The install is
# short-circuited by pre-seeding the dependency fingerprint, so the cases
# exercise the lane's decisions rather than a package manager.

ws_setup() {
  NODE_SB="$(mktemp -d)"
  mkdir -p "$NODE_SB/ci/checks" "$NODE_SB/ci/lib" \
           "$NODE_SB/ws/tests" "$NODE_SB/ws/node_modules" "$NODE_SB/.ci-gate"
  cp "$REPO_ROOT/ci/checks/node.sh" "$NODE_SB/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$NODE_SB/ci/lib/"
  printf 'it("x", () => {});\n' > "$NODE_SB/ws/tests/a.test.ts"
  printf '{}\n' > "$NODE_SB/ws/bun.lock"
}

ws_manifest() {
  printf '%s\n' "$1" > "$NODE_SB/ws/package.json"
}

ws_seed_fingerprint() {
  # Mirrors _deps_fingerprint in node.sh: lockfile hash, then manifest hash.
  ( cd "$NODE_SB/ws" && printf '%s %s\n' \
      "$(sha256sum bun.lock | cut -d' ' -f1)" \
      "$(sha256sum package.json | cut -d' ' -f1)" \
      > "$NODE_SB/.ci-gate/node_modules-ws.hash" )
}

ws_run() {
  ( cd "$NODE_SB" && CI_GATE_NODE_WORKSPACE=ws bash ci/checks/node.sh 2>&1 )
}

@test "node lane: a workspace shipping tests without a test script fails closed" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "build": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"ships tests but defines no"* ]]
  [[ "$output" == *"a.test.ts"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an orphaned test path with a space is reported as one file" {
  ws_setup
  printf 'it("x", () => {});\n' > "$NODE_SB/ws/tests/my component.test.ts"
  rm -f "$NODE_SB/ws/tests/a.test.ts"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "build": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"my component.test.ts"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a test script satisfies the requirement" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: test:unit also satisfies the requirement" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test:unit": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

# --- the declared toolchain gates the workspace -------------------------------
#
# Accepting whatever node and the package manager resolve to means a green lane
# vouches for nothing: the same commit can pass on one machine and fail on a
# conforming one. These pin that the manifest's own declarations are enforced
# before anything is installed or run, and that a manifest declaring nothing is
# left alone.

@test "node lane: an unsatisfied engines.node stops the workspace" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=999.0.0" }, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"does not satisfy the engines.node range"* ]]
  [[ "$output" == *">=999.0.0"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a satisfied engines.node is not in the way" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=0.0.1" }, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a range it cannot evaluate is reported, not assumed" {
  # Failing open here would be the whole finding again: an exotic range read as
  # "fine" is indistinguishable from no check at all.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": "1.2.3 - 2.3.4" }, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"Cannot evaluate the engines.node range"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: engines ranges are evaluated, not pattern-matched" {
  # The comparator has to treat an absent component as 0. Reading "23" as
  # "23.23.23" would make this upper bound accept the version it excludes.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">=0.0.1 <${major}\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"does not satisfy"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a packageManager pin that the host misses stops the workspace" {
  command -v bun >/dev/null 2>&1 || skip "bun is not installed on this host"
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "packageManager": "bun@0.0.1", "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"pins packageManager bun@0.0.1"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: the declared packageManager must be the one the lockfile selects" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "packageManager": "pnpm@9.0.0", "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"selects bun"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a matching packageManager pin is not in the way" {
  command -v bun >/dev/null 2>&1 || skip "bun is not installed on this host"
  ws_setup
  local bunv
  bunv="$(bun --version)"
  # The integrity suffix corepack appends must not be compared as part of the
  # version, or every real-world manifest would fail this check.
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"packageManager\": \"bun@${bunv}+abc123\", \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a manifest declaring no toolchain is left alone" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  [[ "$output" != *"engines.node"* ]]
  [[ "$output" != *"packageManager"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: the version gate runs before any install or script" {
  # "Before invoking the workspace" is the point: an install under an undeclared
  # toolchain has already produced the tree the run would be judged on.
  ws_setup
  rm -f "$NODE_SB/.ci-gate/node_modules-ws.hash"
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=999.0.0" }, "scripts": { "test": "exit 1" } }'
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" != *"Installing dependencies"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: engines X-ranges admit the versions npm admits" {
  # "20" is >=20.0.0 <21.0.0, not 20.0.0 exactly. Defaulting an unstated
  # component to 0 rejected a conforming runtime — a false infra failure, which
  # is how a fail-closed check gets switched off.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"${major}\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: an X-range still excludes the neighbouring major" {
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"$((major + 1))\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"does not satisfy"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a malformed range cannot come back satisfied" {
  # A trailing "||" leaves an empty alternative. Counting tokens across all
  # alternatives let the empty one inherit the previous alternative's count and
  # its untouched ok flag, so ">=999.0.0 ||" reported satisfied — the
  # enforcement boundary switched off by a typo.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=999.0.0 ||" }, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"Cannot evaluate"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a malformed range is rejected even when an alternative matches" {
  # Malformed input is unverifiable however well one alternative matches. This
  # is the case an early return on the first satisfied alternative would hide.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=0.0.1 ||" }, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"Cannot evaluate"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an unsupported range form does not fail a version another alternative admits" {
  # The converse of the case above, and the reason the two are distinguished: a
  # range form this comparator does not implement must not reject a runtime that
  # a sibling alternative plainly accepts.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=0.0.1 || 1.2.3 - 2.3.4" }, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a deleted manifest with workspace config left behind fails" {
  # Discovery finds nothing, so the lane used to report PASS having run no
  # install, typecheck, test or build — while the tracked frontend was left
  # uninstallable and test-layout still passed on the surviving vitest config.
  ws_setup
  rm -f "$NODE_SB/ws/package.json"
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"still carries workspace configuration"* ]]
  [[ "$output" == *"bun.lock"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a manifest staged for deletion but restored on disk fails" {
  # Discovery reads the filesystem, so the restored worktree copy looked like a
  # healthy workspace and every check below read it. Both pre-commit and
  # pre-push passed for a commit carrying no manifest at all.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  git rm --cached -q ws/package.json >/dev/null 2>&1
  [ -f "$NODE_SB/ws/package.json" ]
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"not in the git index"* ]]
  [[ "$output" == *"ws/package.json"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: the index check is inert where there is no index" {
  # The lane also runs from a plain export with no .git. Failing there would be
  # a false positive, not a finding.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  if git -C "$NODE_SB" rev-parse --git-dir >/dev/null 2>&1; then
    rm -rf "$NODE_SB"
    skip "the sandbox temp dir is itself inside a repository on this host"
  fi
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a malformed comparator operand is unverifiable, not satisfied" {
  # _semver_part strips non-numeric text and defaults to 0, so ">=banana" and a
  # bare ">=" both became >=0.0.0 and admitted every version there is.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=banana" }, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"Cannot evaluate"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a comparator with no operand is unverifiable" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=" }, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"Cannot evaluate"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a partial tilde range covers its whole major" {
  # "~20" is >=20.0.0 <21.0.0. Comparing the minor unconditionally read the
  # omitted one as 0 and rejected 20.20.2 — a conforming runtime.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"~${major}\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a tilde range that states a minor still pins it" {
  # The converse: narrowing must survive the fix, or "~20.1" would admit 20.20.
  ws_setup
  local major minor
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  minor="$(node --version | sed 's/^v//' | cut -d. -f2)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"~${major}.$((minor + 1))\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"does not satisfy"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a partially staged manifest stops the workspace" {
  # Existing in the index is not enough: every check below reads the worktree
  # copy, so staging the removal of the test script and restoring the healthy
  # manifest on disk ran the restored script and exited 0 for a commit that
  # ships tests with no way to run them.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  # Stage a manifest with no test script, then restore the healthy one on disk.
  printf '%s\n' '{ "name": "w", "private": true, "scripts": {} }' > ws/package.json
  git add ws/package.json >/dev/null 2>&1
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "test": "true" } }' > ws/package.json
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"staged but changed again in the worktree"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a partially staged source file stops the workspace" {
  # The manifest is not the only file the lane consumes: it installs,
  # typechecks, tests and builds the worktree. Staging a failing source file and
  # restoring the passing copy on disk reported "Node lane passed" for a commit
  # whose code fails once checked out.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  printf 'export const ok = true;\n' > "$NODE_SB/ws/app.js"
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  printf 'throw new Error("fail");\n' > ws/app.js
  git add ws/app.js >/dev/null 2>&1
  printf 'export const ok = true;\n' > ws/app.js
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"ws/app.js"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a staged deletion recreated as an untracked file is caught" {
  # git reports `D  app.js` and `?? app.js`, but `git diff` compares tracked
  # content only, so the intersection stayed empty and the lane tested the
  # recreated file for a commit that deletes it.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  printf 'export const ok = true;\n' > "$NODE_SB/ws/app.js"
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  git rm --cached -q ws/app.js >/dev/null 2>&1
  rm -f ws/app.js
  printf 'export const recreated = 1;\n' > ws/app.js
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"ws/app.js"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: an ordinary untracked file is not partial staging" {
  # The rule still has to stay on files that are part of this commit. A new
  # file nobody has staged is the normal way work starts.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  printf 'export const brand_new = 1;\n' > ws/newfile.js
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: an invalid operand beats a satisfied alternative" {
  # A malformed operand is a typo, not a range form this comparator lacks, and
  # no sibling should rescue it: ">=20banana || >=<major>" came back satisfied
  # while _semver_is_version was rejecting the first operand outright.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">=20banana || >=${major}\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"Cannot evaluate"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an unsupported form still loses to a satisfied alternative" {
  # The distinction that makes the case above meaningful: a hyphen range is
  # valid syntax this comparator does not implement, and it must not fail a
  # runtime a sibling plainly admits.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">=${major} || 1.2.3 - 2.3.4\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: an ordinary dirty worktree is not partial staging" {
  # The rule has to stay on files that are part of this commit. Running the gate
  # by hand on an edited-but-unstaged tree is the normal case, and failing it
  # makes the lane unusable rather than stricter. Uses the manifest on purpose:
  # the first version of this check compared package.json against the index
  # unconditionally, so every uncommitted manifest edit failed the lane.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "test": "true", "build": "true" } }' > ws/package.json
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: an index that matches the worktree is not in the way" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: an established workspace cannot lose its whole suite" {
  # Deleting every test file AND both scripts leaves nothing to be orphaned,
  # test-layout reports "0 file(s)" quite happily, and a successful build
  # carries the gate to exit 0 after the suite has disappeared.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  rm -rf ws/tests
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "build": "true" } }' > ws/package.json
  git add -A >/dev/null 2>&1
  # Seeded after the mutation: the fingerprint covers the manifest, so seeding
  # it earlier would leave the install to run against a stub lockfile.
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"lost its entire test suite"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a workspace that never had tests is unaffected by the suite rule" {
  # The negative control: "had tests at HEAD" is the whole trigger, so a
  # genuinely test-free workspace must still pass.
  ws_setup
  rm -rf "$NODE_SB/ws/tests"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "build": "true" } }'
  ws_seed_fingerprint
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a script exiting 10 is a failure, not known debt" {
  # 10 is the gate's PASS_WITH_KNOWN_DEBT. A package script does not implement
  # that contract, so vitest — or any tool it wraps — exiting 10 was recorded as
  # a passing lane, the remaining scripts were skipped, and preflight exited 0.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "exit 10" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"failed with status 10"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an ordinary script failure is still a new issue" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "exit 1" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a numeric-prefixed malformed operand is unverifiable" {
  # ">=20banana" begins with a digit and carries only legal characters, so a
  # first-character-and-charset check accepted it and _semver_part then parsed
  # it as >=20.0.0. node-semver rejects it outright.
  ws_setup
  local bad
  for bad in '>=20banana' '>=20..1' '>=20.1.2.3'; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"${bad}\" }, \"scripts\": { \"test\": \"true\" } }"
    ws_seed_fingerprint
    run ws_run
    [ "$status" -eq 30 ] || { echo "accepted malformed range: $bad" >&2; return 1; }
    [[ "$output" == *"Cannot evaluate"* ]] || { echo "wrong message for: $bad" >&2; return 1; }
  done
  rm -rf "$NODE_SB"
}

@test "node lane: an equality-prefixed partial range keeps X-range semantics" {
  # node-semver normalises "=20" to >=20.0.0 <21.0.0-0. Routing it through
  # exact comparison defaulted the omitted components to zero and rejected a
  # conforming runtime — the same defaulting the bare form was fixed for.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"=${major}\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: an equality range that states every component still pins it" {
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"=$((major + 1)).0.0\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"does not satisfy"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a comparator on a partial range covers the whole major" {
  # node-semver expands "<=20" to <21.0.0-0. Comparing against a zero-filled
  # 20.0.0 rejected 20.20.2, a conforming runtime.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"<=${major}\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a strict comparator on a partial range excludes the whole major" {
  # The other side of the same bound: ">20" requires 21 or later, and against a
  # zero-filled 20.0.0 it wrongly admitted 20.20.2.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">${major}\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"does not satisfy"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a fully stated comparator is unaffected by the expansion" {
  # ">=20.0.0" and "<20.0.0" already read correctly; the bound must only apply
  # where the operand leaves components unstated.
  ws_setup
  local ver
  ver="$(node --version | sed 's/^v//')"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">${ver}\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a workspace staged but missing from disk stops the gate" {
  # Discovery stats the filesystem, so a workspace that exists only in the index
  # never entered NODE_WORKSPACES: the commit adds it, with a failing test
  # script, and the lane never looks at it.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  mkdir -p added
  printf '%s\n' '{ "name": "a", "private": true, "scripts": { "test": "exit 1" } }' > added/package.json
  printf '{}\n' > added/bun.lock
  git add added >/dev/null 2>&1
  rm -rf added
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"staged but missing from the worktree"* ]]
  [[ "$output" == *"added/package.json"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a workspace declaring TypeScript must be able to typecheck it" {
  # vite build does not run tsc, so with the typecheck script gone run_script
  # logs "Skipping missing script" and the lane exits 0 having checked no types.
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"no 'typecheck' script"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a typecheck script satisfies the TypeScript requirement" {
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true", "typecheck": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a workspace with no TypeScript config needs no typecheck script" {
  # The negative control: the rule is keyed on the workspace declaring
  # TypeScript, not on every workspace everywhere.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: whitespace between a comparator and its operand is not malformed" {
  # npm reads ">= 20" as ">=20". Splitting on whitespace alone left a bare ">=",
  # which the grammar check correctly calls malformed — so a conforming
  # environment was blocked by an infrastructure failure over a space.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">= ${major}\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a spaced comparator is still evaluated, not waved through" {
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">= $((major + 1))\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"does not satisfy"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a repository with no JavaScript at all still skips" {
  # The negative control the check is keyed on: no manifest AND no workspace
  # configuration is a repo without a Node lane, not a deleted manifest.
  ws_setup
  rm -f "$NODE_SB/ws/package.json" "$NODE_SB/ws/bun.lock"
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Skipping Node lane"* ]]
  rm -rf "$NODE_SB"
}

# --- the result cache must not outlive the toolchain it vouched for -----------

# --- a blocking lane that cannot finish is a lane that does not run ----------

@test "tests-shell: the suites are given longer than the gate default" {
  # Found by running the gate rather than by reading it: at the 1200s default
  # the lane was killed and reported FAIL_INFRA, so the blocking shell suites
  # silently stopped executing in ship mode.
  source ci/lib/runner.sh
  local declared default
  declared="$(ci::runner::_declared_timeout tests-shell)"
  default="$(awk '/^default_timeout_sec:/ { gsub(/[^0-9]/, ""); print; exit }' ci/config/gate.yml)"
  [ -n "$declared" ]
  [ -n "$default" ]
  [ "$declared" -gt "$default" ]
}

@test "tests-shell: the timeout applies in sequential mode too" {
  # CI_GATE_PARALLEL=0, or a single-worker pool, runs the check directly. With
  # the timeout applied only on the background path, a supported mode ignored
  # both the declared value and the global one and could hang indefinitely.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/ci/config"
  cp "$REPO_ROOT/ci/lib/runner.sh" "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf 'checks:\n  slowcheck:\n    enabled: true\n    timeout_sec: 1\n' > "$sb/ci/config/checks.yml"
  printf '#!/usr/bin/env bash\nsleep 6\nexit 0\n' > "$sb/slow.sh"
  chmod +x "$sb/slow.sh"

  run bash -c "
    set -Eeuo pipefail
    cd '$sb'
    source ci/lib/common.sh 2>/dev/null || true
    source ci/lib/runner.sh
    CI_GATE_PARALLEL=0
    ci::runner::init
    start=\$(date +%s)
    ci::runner::submit slowcheck ./slow.sh || true
    end=\$(date +%s)
    echo \"elapsed=\$((end-start))\"
    echo \"rc=\$(ci::runner::get_result slowcheck 2>/dev/null || echo unknown)\"
  "
  rm -rf "$sb"
  [ "$status" -eq 0 ]
  # 124 is what `timeout` returns, and what preflight maps to FAIL_INFRA.
  [[ "$output" == *"rc=124"* ]]
  local elapsed
  elapsed="$(printf '%s\n' "$output" | sed -n 's/^elapsed=//p')"
  [ -n "$elapsed" ]
  [ "$elapsed" -lt 5 ]
}

@test "tests-shell: a declared timeout is actually applied, not documentation" {
  # checks.yml has carried timeout_sec since before this PR and nothing read it;
  # the runner used the global value for every check.
  run bash -c "sed -n '/# Determine timeout/,/^  fi\$/p' ci/lib/runner.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"_declared_timeout"* ]]
  [[ "$output" == *"check_timeout"* ]]
}

@test "cache key: the node lane is excluded from the result cache" {
  # The lane runs the workspace's complete test, typecheck and build scripts,
  # so its result depends on every file in that workspace -- not just the ones
  # the branch touches. A PASS cached for one frontend change, rebased onto a
  # base carrying a regression in another frontend file, has an identical key.
  # Keying on the package managers was the first fix and covered only the
  # toolchain half.
  run bash -c "sed -n '/^_check_is_cacheable()/,/^}/p' ci/preflight.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"node) return 1"* ]]
}

@test "cache key: the shell suite is excluded from the result cache" {
  # tests-shell asserts on the whole ci/ tree and on files a changeset need not
  # mention. A PASS cached for a .gitignore-only branch, rebased onto a base
  # carrying a regression in ci/checks/node.sh, has an identical key: same
  # tools, same changed files, same checks.yml.
  run bash -c "sed -n '/^_check_is_cacheable()/,/^}/p' ci/preflight.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"tests-shell) return 1"* ]]
  [[ "$output" == *"test-layout) return 1"* ]]
}

@test "cache key: a broken package manager cannot abort the gate" {
  # Deriving a cache key must never end the run. Under `set -Eeuo pipefail` a
  # bare $(tool --version | head -1) aborts preflight the moment a
  # present-but-broken executable exits non-zero — before a single check has
  # run, with a message about nothing the commit touched.
  local sb
  sb="$(mktemp -d)"
  printf '#!/usr/bin/env bash\necho boom >&2\nexit 1\n' > "$sb/bun"
  chmod +x "$sb/bun"

  run bash -c "
    set -Eeuo pipefail
    cd '$REPO_ROOT'
    source ci/lib/cache.sh
    $(sed -n '/^_tool_fingerprint()/,/^}/p' "$REPO_ROOT/ci/preflight.sh")
    PATH='$sb':\$PATH
    printf 'FINGERPRINT=%s\n' \"\$(_tool_fingerprint bun)\"
    echo REACHED_END
  "
  rm -rf "$sb"
  [ "$status" -eq 0 ]
  [[ "$output" == *"REACHED_END"* ]]
  # A tool that cannot report its version is not the same state as no tool.
  [[ "$output" == *"bun-unknown"* ]]
}

@test "cache key: an uninstalled tool is recorded, not skipped" {
  # Recording only the tools that happen to be installed would leave the
  # original finding intact: uninstalling bats has to change the key.
  run bash -c "
    set -Eeuo pipefail
    cd '$REPO_ROOT'
    source ci/lib/cache.sh
    $(sed -n '/^_tool_fingerprint()/,/^}/p' "$REPO_ROOT/ci/preflight.sh")
    _tool_fingerprint definitely-not-a-real-tool-9de32131
  "
  [ "$status" -eq 0 ]
  [[ "$output" == *"-absent"* ]]
}

@test "cache key: an uncached lane keeps no key branch to go stale" {
  # Keying on the bats and package-manager versions was the first fix for both
  # lanes. Excluding them from the cache supersedes it and covers inputs a key
  # never could, so their branches must not linger: they are unreachable --
  # _compute_cache_key is only called when _check_is_cacheable passes -- and an
  # unreachable cache key reads like the lane is still cached.
  # Comments are stripped first: this block explains why those lanes are not
  # here, and the explanation must not be what satisfies the assertion.
  run bash -c "sed -n '/^_compute_cache_key()/,/^}/p' ci/preflight.sh | grep -v '^[[:space:]]*#'"
  [ "$status" -eq 0 ]
  [[ "$output" != *"tests-shell)"* ]]
  [[ "$output" != *"node)"* ]]
}

@test "node lane: a workspace with no tests at all is unaffected" {
  ws_setup
  rm -rf "$NODE_SB/ws/tests"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "build": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: an edited manifest invalidates the install cache" {
  # Fingerprinting the lockfile alone lets a package.json/lockfile mismatch
  # reuse a stale node_modules, so the frozen install that would catch it never
  # runs.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" }, "dependencies": { "x": "1.0.0" } }'
  run ws_run
  [[ "$output" != *"up to date"* ]]
  [[ "$output" == *"Installing dependencies"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an unchanged workspace still skips the install" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  [[ "$output" == *"up to date"* ]]
  rm -rf "$NODE_SB"
}

# --- the JS test runner must be the pinned one --------------------------------

@test "tests lane: no PATH fallback to a global jest or vitest" {
  # An unpinned runner against a different Vite produced ~160 phantom failures
  # in this checkout. Reporting the runner unavailable is recoverable; a green
  # run of the wrong binary is not.
  run grep -nE '^[[:space:]]*(jest|vitest)[[:space:]]' ci/checks/tests.sh
  [ "$status" -ne 0 ]
  run grep -nE 'command_exists (jest|vitest)$' ci/checks/tests.sh
  [ "$status" -ne 0 ]
}

@test "tests lane: the JS runner resolves only inside the workspace" {
  # Every invocation must come from node_modules/.bin of the workspace.
  run grep -c 'node_modules/.bin/' ci/checks/tests.sh
  [ "$status" -eq 0 ]
  [ "$output" -ge 2 ]
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

@test "changeset: frontend build inputs schedule the node lane" {
  # These are the only lane that can validate them. Left `unknown`/`json` they
  # schedule nothing at all — no typecheck, no tests, no vite build.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  local f
  for f in frontend/src/styles.css frontend/index.html frontend/tsconfig.json; do
    [ "$(ci::changeset::classify_file "$f")" = "javascript" ] \
      || { echo "$f classified as $(ci::changeset::classify_file "$f"), not javascript" >&2; return 1; }
  done
}

@test "affected: a lockfile change maps to the frontend suite" {
  # A lockfile resolves different code into node_modules, so the suite has to
  # run. Staged next to a Python change it otherwise leaves the JavaScript
  # slice empty and tests.sh skips the lane.
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests frontend/bun.lock backend/app/main.py
  [ "$status" -eq 0 ]
  [[ "$output" == *"frontend/tests/"* ]]
}

@test "affected: a stylesheet change maps to the frontend suite" {
  # Kept in step with the `javascript` classification in changeset.sh: the lane
  # being scheduled is useless if the affected filter then finds nothing.
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests frontend/src/styles.css backend/app/main.py
  [ "$status" -eq 0 ]
  [[ "$output" == *"frontend/tests/"* ]]
}

@test "affected: classifier and mapping agree on every javascript extension" {
  # Derived from the classifier, not a hand-kept list: adding an extension to
  # ci/lib/changeset.sh without a matching affected.yml rule fails here rather
  # than silently scheduling a lane that then finds nothing to run. This gap has
  # recurred three times in review, which is why the list is computed.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  source ci/lib/affected.sh

  local exts
  exts="$(sed -n '/^  case "\$ext" in$/,/^  esac$/p' ci/lib/changeset.sh \
    | grep -E "printf 'javascript'" \
    | sed 's/).*//' \
    | tr '|' '\n' \
    | tr -d ' \t' \
    | grep -vE '^$|^#')"
  [ -n "$exts" ]

  local ext missing=""
  while IFS= read -r ext; do
    [ -z "$ext" ] && continue
    [ "$(ci::changeset::classify_file "frontend/src/probe.$ext")" = "javascript" ] \
      || { echo "classifier disagrees for .$ext" >&2; return 1; }
    case "$(ci::affected::get_affected_tests "frontend/src/probe.$ext")" in
      *frontend/tests/*) ;;
      *) missing="${missing} .${ext}" ;;
    esac
  done <<< "$exts"

  if [ -n "$missing" ]; then
    echo "classified javascript but unmapped in affected.yml:${missing}" >&2
    return 1
  fi
}

@test "affected: classifier and mapping agree on frontend manifests and lockfiles" {
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  source ci/lib/affected.sh
  local f
  for f in frontend/package.json frontend/tsconfig.json frontend/bun.lock frontend/index.html; do
    [ "$(ci::changeset::classify_file "$f")" = "javascript" ] \
      || { echo "$f not classified javascript" >&2; return 1; }
    case "$(ci::affected::get_affected_tests "$f")" in
      *frontend/tests/*) ;;
      *) echo "$f classified javascript but maps to no frontend tests" >&2; return 1 ;;
    esac
  done
}

@test "typecheck lane: no PATH fallback to a global tsc" {
  # Same reasoning as the JS test runner: an unpinned compiler gives a
  # non-reproducible red or green.
  run grep -nE 'bin="tsc"' ci/checks/typecheck.sh
  [ "$status" -ne 0 ]
  run grep -c 'node_modules/.bin/tsc' ci/checks/typecheck.sh
  [ "$status" -eq 0 ]
  [ "$output" -ge 1 ]
}

@test "changeset: mts and cts classify as javascript" {
  # Module-specific TypeScript extensions are production source, and
  # test-layout.sh only covers test files, so nothing else would catch them.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  local ext
  for ext in mts cts mjs cjs; do
    [ "$(ci::changeset::classify_file "frontend/src/client.$ext")" = "javascript" ] \
      || { echo ".$ext did not classify as javascript" >&2; return 1; }
  done
}

@test "affected: an mts source change maps to the frontend suite" {
  source ci/lib/affected.sh
  run ci::affected::get_affected_tests frontend/src/client.mts
  [ "$status" -eq 0 ]
  [[ "$output" == *"frontend/tests/"* ]]
}

# --- the gate runs its own test suites ----------------------------------------
#
# The suites in this directory cover the layout guard and the node lane. Until
# they were scheduled they ran only by hand — the same "registered but never
# runs" failure they exist to catch.

@test "tests-shell: scheduled by preflight full and ship modes" {
  run bash -c "sed -n '/^run_full_or_ship_checks()/,/^}/p' ci/preflight.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"tests-shell:./ci/checks/tests-shell.sh"* ]]
}

@test "tests-shell: registered as a blocking lane" {
  run grep -E '^tests-shell\|' ci/config/lanes.conf
  [ "$status" -eq 0 ]
  [[ "$output" == *"./ci/checks/tests-shell.sh|yes|"* ]]
}

@test "tests-shell: every input these suites assert on un-filters the lane" {
  # The suites test the gate's own inputs, and those are not all classifiable by
  # language: ci/config/*.yml emits lint-yaml only, and .gitignore emits nothing
  # at all. Either would filter the lane that tests it.
  run bash -c "sed -n '/bats suites assert on the/,/^    fi\$/p' ci/preflight.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"tests-shell"* ]]
  local dep
  for dep in 'ci/' '.githooks/' '.gitignore' 'frontend/README'; do
    [[ "$output" == *"$dep"* ]] || { echo "dependency path not covered: $dep" >&2; return 1; }
  done
}

@test "tests-shell: the README the suites assert on schedules them" {
  # test_test_layout.bats validates frontend/README.md's prose about which modes
  # run the guard. A README-only change classifies as markdown, schedules
  # lint-markdown and nothing else, and would let a false coverage claim through
  # without running the case written to reject it.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  # The reason the path exception is needed: the language route schedules
  # lint-markdown and nothing else.
  [ "$(ci::changeset::classify_file frontend/README.md)" = "markdown" ]
  run ci::changeset::_checks_for_language markdown
  [ "$status" -eq 0 ]
  [[ "$output" != *"tests-shell"* ]]

  # Feed a README-only changeset line through preflight's OWN pattern, lifted
  # from the file rather than copied — a copy would pass whatever preflight
  # actually contains.
  local pattern
  pattern="$(sed -n "/bats suites assert on the/,/^    fi\$/p" ci/preflight.sh \
    | sed -n "s/.*grep -qE '\(.*\)'.*/\1/p")"
  [ -n "$pattern" ]
  run bash -c "printf 'M\tfrontend/README.md\n' | grep -qE '$pattern'"
  [ "$status" -eq 0 ]
}

@test "tests-shell: a .gitignore-only change is not classified into any lane" {
  # The reason the path exception exists: without it, the changeset for a
  # .gitignore edit yields no check ids whatsoever.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  [ "$(ci::changeset::classify_file .gitignore)" = "unknown" ]
  run ci::changeset::_checks_for_language unknown
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "layout guard: excluded from the result cache" {
  # test-layout reads the git index and walks the whole frontend tree, but the
  # cache key is derived from the changed-file list and worktree contents. A
  # staged config change with an unchanged worktree copy would serve a cached
  # PASS for a commit the guard never inspected.
  run bash -c "sed -n '/^_check_is_cacheable()/,/^}/p' ci/preflight.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"test-layout"* ]]
  # Both the read and the write side must consult it, or a stale entry is
  # stored now and served later.
  run grep -c '_check_is_cacheable "' ci/preflight.sh
  [ "$status" -eq 0 ]
  [ "$output" -ge 2 ]
}

@test "gitignore: a nested lib directory under frontend/tests is trackable" {
  # The tests tree mirrors src/, so a lib/ segment can appear at any depth. The
  # guard and vitest both see such a file; only `git add` quietly omits it.
  run git check-ignore -q frontend/tests/features/lib/widget.test.ts
  [ "$status" -ne 0 ]
  run git check-ignore -q frontend/tests/a/b/lib/deep.test.ts
  [ "$status" -ne 0 ]
}

@test "gitignore: a nested lib directory under frontend/src is trackable" {
  run git check-ignore -q frontend/src/features/lib/util.ts
  [ "$status" -ne 0 ]
}

@test "gitignore: a new file under ci/lib is trackable" {
  # The Python-convention `lib/` rule shadows the gate's own library directory.
  # The files already there are tracked and so unaffected; a newly added helper
  # would be invisible to `git add`, shipping a check with half its code.
  run git check-ignore -q --no-index ci/lib/newthing.sh
  [ "$status" -ne 0 ]
}

@test "gitignore: the lib negations do not re-include ignored artifacts" {
  # A `!<dir>/**` companion re-includes every descendant outright, overriding
  # the artifact and secret rules above it. Un-excluding the directory alone is
  # enough, because git only skips rule evaluation *inside* an excluded
  # directory — everything under it is then matched normally.
  local p
  for p in \
    ci/lib/__pycache__/x.pyc \
    ci/lib/x.pyc \
    ci/lib/x.so \
    ci/lib/.env \
    ci/lib/.vscode/settings.json \
    frontend/src/lib/.env \
    frontend/src/lib/secret.pem \
    frontend/tests/lib/x.pyc \
    frontend/tests/features/lib/.env
  do
    run git check-ignore -q --no-index "$p"
    [ "$status" -eq 0 ] || { echo "leaked back in: $p" >&2; return 1; }
  done
}

@test "gitignore: the example env files stay trackable" {
  # The `.env.*` rule carries `!.env.example` negations that sit *above* this
  # block. Re-stating the artifact rules after the negations, rather than
  # narrowing them, would have re-ignored those.
  run git check-ignore -q --no-index .env.example
  [ "$status" -ne 0 ]
}

@test "gitignore: vendored lib directories are still ignored" {
  # The negations must not reach into node_modules.
  run git check-ignore -q frontend/node_modules/pkg/lib/index.js
  [ "$status" -eq 0 ]
}

@test "tests-shell: a shell change emits the tests-shell check id" {
  # Without this the lane is scheduled and then filtered straight back out.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  run ci::changeset::_checks_for_language shell
  [ "$status" -eq 0 ]
  [[ "$output" == *"tests-shell"* ]]
}

@test "tests-shell: the wrapper is executable in the index, not just on disk" {
  # run_phase executes the path directly, so a wrapper committed 100644 exits
  # 126 Permission denied and the gate reports infra failure before any test
  # runs. core.fileMode=false means a local chmod is not what git records, so
  # this asserts the index mode — checking [ -x ] on the worktree passes while
  # the committed file is still non-executable.
  run git ls-files -s ci/checks/tests-shell.sh
  [ "$status" -eq 0 ]
  [[ "$output" == 100755* ]]
  bash -n ci/checks/tests-shell.sh
}

@test "tests-shell: every ci/checks script is executable in the index" {
  run bash -c "git ls-files -s ci/checks/ | grep -v '\.yml\$' | awk '\$1 != \"100755\"'"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "tests-shell: the wrapper dispatches to tests.sh as tests-shell" {
  run grep -c 'CI_GATE_CHECK_ID=tests-shell' ci/checks/tests-shell.sh
  [ "$status" -eq 0 ]
  [ "$output" -ge 1 ]
}

@test "tests-shell: a missing bats runner is infra failure, not a green skip" {
  # tests.sh logs "skipped: bats not installed" and returns 0, which would make
  # this enabled blocker pass having executed nothing. uv sync does not
  # provision bats, so a fresh environment hits exactly that.
  # A PATH that still has a shell and coreutils but no bats: emptying PATH
  # entirely would break bash before the check under test ever runs.
  if PATH=/usr/bin:/bin command -v bats >/dev/null 2>&1; then
    skip "bats resolves from /usr/bin on this host; cannot simulate its absence"
  fi
  run bash -c 'PATH=/usr/bin:/bin bash ci/checks/tests-shell.sh'
  [ "$status" -eq 30 ]
  [[ "$output" == *"bats is not installed"* ]]
}

@test "changeset: a rename classifies both of its paths" {
  # The entry was reduced to its destination before classification, so
  # R100 frontend/src/x.ts -> Docs/x.md yielded lint-markdown alone — and a
  # rename that removes a module the bundle imports scheduled neither the tests,
  # the typecheck nor the build that would surface the broken import.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  _CI_CHANGESET_FILES_RAW="$(printf 'R100\tfrontend/src/x.ts\tDocs/x.md')"
  ci::changeset::_populate_state_from_raw
  [[ "$_CI_CHANGESET_LANGUAGES" == *javascript* ]]
  [[ "$_CI_CHANGESET_LANGUAGES" == *markdown* ]]
  [[ "$_CI_CHANGESET_CHECKS" == *tests-js* ]]
  [[ "$_CI_CHANGESET_CHECKS" == *lint-markdown* ]]
}

@test "changeset: a recognised type inside a workspace still schedules the node lane" {
  # Workspace membership was consulted only in classify_file's unknown fallback,
  # so a recognised type never reached it: frontend/src/data.json classifies as
  # json and emitted no node ids, while this workspace enables resolveJsonModule.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  _CI_CHANGESET_FILES_RAW="$(printf 'M\tfrontend/src/data.json')"
  ci::changeset::_populate_state_from_raw
  [[ "$_CI_CHANGESET_CHECKS" == *tests-js* ]]
  [[ "$_CI_CHANGESET_CHECKS" == *typecheck-js* ]]
}

@test "changeset: the report generator does not overwrite scheduler state" {
  # emit_json recomputed the language and check sets from its own collapsed view
  # of a rename and wrote them back, so classifying both rename paths in the
  # scheduler was undone one call later — and preflight calls emit_json
  # immediately after detect.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  local tmp
  tmp="$(mktemp -d)"
  CI_CHANGESET_JSON="$tmp/changeset.json"
  _CI_CHANGESET_MODE="test"
  _CI_CHANGESET_FILES_RAW="$(printf 'R100\tfrontend/src/x.ts\tDocs/x.md')"
  ci::changeset::_populate_state_from_raw
  [[ "$_CI_CHANGESET_CHECKS" == *tests-js* ]]
  ci::changeset::emit_json
  [[ "$_CI_CHANGESET_CHECKS" == *tests-js* ]] \
    || { echo "emit_json dropped tests-js from the scheduler" >&2; rm -rf "$tmp"; return 1; }
  # And the report describes the same change set it is reporting on.
  grep -q 'tests-js' "$tmp/changeset.json"
  grep -q 'frontend/src/x.ts' "$tmp/changeset.json"
  grep -q 'Docs/x.md' "$tmp/changeset.json"
  rm -rf "$tmp"
}

@test "changeset: workspace membership does not reach outside a workspace" {
  # The negative control: a json file elsewhere still schedules no node lane.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  _CI_CHANGESET_FILES_RAW="$(printf 'M\tDocs/example.json')"
  ci::changeset::_populate_state_from_raw
  [[ "$_CI_CHANGESET_CHECKS" != *tests-js* ]]
}

@test "changeset: the JSON report and the scheduler agree on workspace membership" {
  # emit_json duplicates the per-file check computation. The two disagreeing is
  # how the report starts describing a gate that is not the one being run.
  run bash -c "sed -n '/^ci::changeset::emit_json()/,/^}/p' ci/lib/changeset.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"_workspace_checks"* ]]
}

@test "changeset: an unrelated json file is still json" {
  # The manifest rule must key on the basename, not on the extension.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  [ "$(ci::changeset::classify_file Docs/example.json)" = "json" ]
}

# --- the fast exit must not front-run the path exception ----------------------
#
# _check_should_skip carries an exception so a change to ci/, .githooks/ or
# .gitignore still runs tests-shell. That exception is dead code if preflight
# returns before run_mode ever calls it, which is exactly what happens for those
# paths: none of them classifies into a language.

@test "preflight: a gate-input-only change does not hit the fast exit" {
  # Read the guard rather than the whole gate: driving preflight end to end
  # would run every scheduled check. What has to hold is that the condition
  # names the same paths the exception does.
  run bash -c "sed -n '/Fast-exit if no relevant files changed/,/^  fi\$/p' ci/preflight.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"_CI_CHANGESET_FILES_RAW"* ]]
  local dep
  for dep in 'ci/' '.githooks/' '.gitignore'; do
    [[ "$output" == *"$dep"* ]] || { echo "fast exit still swallows: $dep" >&2; return 1; }
  done
}

@test "preflight: the fast exit still fires for a genuinely irrelevant change" {
  # The exception must stay narrow. A Docs-only commit carries no language and
  # touches no gate input, so it must still skip rather than run the full gate.
  run bash -c "
    _CI_CHANGESET_FILES_RAW='M	Docs/15_DELIVERY_BACKLOG.md'
    printf '%s\n' \"\$_CI_CHANGESET_FILES_RAW\" \
      | grep -qE '(^|[[:space:]])(ci/|\.githooks/|\.gitignore\$)'
  "
  [ "$status" -ne 0 ]
}

@test "preflight: the gate-input pattern matches the changeset's own line format" {
  # _CI_CHANGESET_FILES_RAW holds STATUS<TAB>PATH lines, so a pattern anchored
  # only at ^ would never match. Feed the real shape through the real pattern.
  local raw
  raw="$(printf 'M\tci/preflight.sh\nA\t.gitignore\nM\t.githooks/pre-commit\n')"
  run bash -c "printf '%s\n' \"\$1\" | grep -cE '(^|[[:space:]])(ci/|\.githooks/|\.gitignore\$)'" _ "$raw"
  [ "$status" -eq 0 ]
  [ "$output" -eq 3 ]
}

# --- workspace build inputs that no extension list can enumerate --------------

@test "changeset: a frontend dotfile classifies into the node lane" {
  # frontend/.env supplies the VITE_ variables the bundle is compiled against.
  # Left `unknown` it emits no check ids at all, so changing what ships would
  # run no install, no typecheck, no tests and no build.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  [ "$(ci::changeset::classify_file frontend/.env)" = "javascript" ]
}

@test "changeset: a frontend static asset classifies into the node lane" {
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  [ "$(ci::changeset::classify_file frontend/public/fonts/inter.woff2)" = "javascript" ]
  [ "$(ci::changeset::classify_file frontend/public/favicon.ico)" = "javascript" ]
}

@test "changeset: the workspace rule does not reach outside a package.json tree" {
  # Anchoring on package.json is what keeps this from swallowing the repo: a
  # backend asset or a root dotfile has no JavaScript to check.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  [ "$(ci::changeset::classify_file .gitignore)" = "unknown" ]
  [ "$(ci::changeset::classify_file src/ums/static/logo.woff2)" = "unknown" ]
}

@test "changeset: an explicit language still wins inside the workspace" {
  # The workspace rule is the last resort, after the extension table and both
  # sniffs. A shell script or a markdown file under frontend/ keeps its own lane.
  source ci/lib/common.sh
  source ci/lib/changeset.sh
  [ "$(ci::changeset::classify_file frontend/scripts/build.sh)" = "shell" ]
  [ "$(ci::changeset::classify_file frontend/README.md)" = "markdown" ]
}

@test "affected: every path the workspace rule classifies maps to a test pattern" {
  # Scheduling the lane without a mapping just moves the silence: tests.sh
  # reports "no affected JavaScript tests" and the suite still does not run.
  source ci/lib/affected.sh
  local f
  for f in frontend/.env frontend/public/fonts/inter.woff2 frontend/public/favicon.ico; do
    run ci::affected::get_affected_tests "$f"
    [ "$status" -eq 0 ]
    [[ "$output" == *"frontend/tests/"* ]] \
      || { echo "$f maps to no frontend test pattern" >&2; return 1; }
  done
}

@test "node lane: a tilde range on a wildcard minor covers the whole major" {
  # `~20.x` states a minor textually and none semantically. Testing the operand
  # for a non-empty second component treated the `x` as a stated minor, pinned
  # the upper bound at 20.1.0, and failed every runtime above 20.0.x — a range
  # npm reads as the whole of major 20.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"~${major}.x\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a caret range on a wildcard minor covers the whole major" {
  # A control, not a second reproduction: the caret branch reads the same
  # predicate but only consults the minor when the major is 0, so `^20.x` was
  # already correct. It is here so the shared fix is pinned on both branches.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"^${major}.x\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a tilde range on a stated minor still pins that minor" {
  # The control that keeps the case above from being a hole: with a real number
  # in the minor position the tilde bound is still enforced.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"~$((major - 1)).0\" }, \"scripts\": { \"test\": \"true\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"does not satisfy"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: in ship mode a commit repaired only on disk is caught" {
  # The partial-staging rule is written against the index, and in ship mode the
  # index matches HEAD: `git diff --cached` is empty and the rule never fires.
  # So a workspace committed without a way to run its tests, then repaired in
  # the worktree, passed the pre-push gate on the strength of the repair.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": {} }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  # Repair on disk only. HEAD still ships a workspace with no test script.
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "test": "true" } }' > ws/package.json
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"differ between HEAD and the worktree"* ]]
  [[ "$output" == *"ws/package.json"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: in ship mode an untracked workspace file is caught too" {
  # A file that exists only on disk is not in the commits being pushed either,
  # and `git diff HEAD` says nothing about it.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  printf 'export const only_on_disk = 1;\n' > ws/extra.js
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"ws/extra.js"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: in ship mode a worktree matching HEAD passes" {
  # The control: the rule must fail a divergent tree, not every ship run.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: outside ship mode an uncommitted edit is still allowed" {
  # The reference tree follows the gate, so the stricter HEAD rule must not leak
  # into the pre-commit gate, where working on a dirty tree is the normal case.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "test": "true", "build": "true" } }' > ws/package.json
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=quick bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: preflight exports the gate mode the reference tree depends on" {
  # node.sh reads CI_GATE_MODE, and nothing else sets it. Without the export the
  # ship rule above is dead code in the real gate — the shape of failure this PR
  # has hit repeatedly.
  run grep -nE '^export CI_GATE_MODE="\$MODE"$' ci/preflight.sh
  [ "$status" -eq 0 ]
}
