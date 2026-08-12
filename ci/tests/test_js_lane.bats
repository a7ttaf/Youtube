#!/usr/bin/env bats
#
# The JS lanes this PR activates are reachable only through a chain of config:
# checks.yml decides whether preflight schedules the `node` lane at all,
# affected.yml decides whether tests.sh considers any JavaScript test affected,
# and the result contract decides whether a failure is reported as a regression
# or as broken infrastructure. Each link failed open before; these pin them.

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  # The gate exports its own mode and push state, and these suites run
  # under it -- so a case inherited a range belonging to another tree.
  # shellcheck source=ci/tests/gate_env.bash
  source "$REPO_ROOT/ci/tests/gate_env.bash"
  ci::tests::clear_gate_env
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
  # git.sh too: node.sh sources it for the ship-mode push range, and a missing
  # source under `set -e` aborts the script with status 1 before a single
  # assertion runs. Every case here would then fail for a harness reason
  # wearing the costume of the behaviour it claims to test.
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" \
     "$REPO_ROOT/ci/lib/git.sh" "$NODE_SB/ci/lib/"
  printf 'it("x", () => {});\n' > "$NODE_SB/ws/tests/a.test.ts"
  printf '{}\n' > "$NODE_SB/ws/bun.lock"
  # Real wrapper scripts for the fixtures to name.
  #
  # The stub was `"test": "true"`, then `"test": "bash -c true"`, then a
  # `scripts/test.sh` containing `exit 0` — and the lane rejects all three now.
  # Each was the previous no-op wearing one more layer: a command that runs
  # nothing, a wrapper around a command that runs nothing, and a *file* whose
  # contents run nothing. The gate follows delegation to its end, so the
  # fixtures have to be wrappers that genuinely reach a runner.
  #
  # `scripts/vitest` stands in for the installed binary. That is the real
  # boundary: the gate can see that a script invokes something named `vitest`
  # and cannot see what that binary then does — so a fixture that names it is
  # modelling a true wrapper, not evading the rule. The exit codes the cases
  # rely on come from the wrapper, after the runner it names has been invoked.
  mkdir -p "$NODE_SB/ws/scripts"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/vitest"
  chmod +x "$NODE_SB/ws/scripts/vitest"
  printf '#!/usr/bin/env bash\n./scripts/vitest run "$@"\n' > "$NODE_SB/ws/scripts/test.sh"
  printf '#!/usr/bin/env bash\n./scripts/vitest run "$@"\nexit 1\n' > "$NODE_SB/ws/scripts/fail.sh"
  printf '#!/usr/bin/env bash\n./scripts/vitest run "$@"\nexit 10\n' > "$NODE_SB/ws/scripts/fail10.sh"
  # The other recognised runners, as stand-ins the workspace can actually
  # execute. A case that expects the lane to *accept* a script has to be able to
  # run it, or "the refusal message is absent" is satisfied by the script not
  # existing -- which asserts nothing about the rule under test.
  mkdir -p "$NODE_SB/ws/node_modules/.bin"
  local _wsb
  for _wsb in vitest jest mocha playwright deno tsx ts-node node cross-env env; do
    printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/node_modules/.bin/$_wsb"
    chmod +x "$NODE_SB/ws/node_modules/.bin/$_wsb"
  done
}

ws_manifest() {
  printf '%s\n' "$1" > "$NODE_SB/ws/package.json"
}

ws_seed_fingerprint() {
  # Mirrors _deps_fingerprint in node.sh: lockfile hash, then manifest hash.
  #
  # Hashed with the gate's own ci::common::hash_file rather than a hard-coded
  # sha256sum. hash_file falls back through several tools, so on a machine
  # where it picks a different backend a hand-rolled sha256sum seeds a value
  # node.sh will never compute — the sandbox would then attempt a real install
  # and these cases would fail for a reason that has nothing to do with what
  # they assert. Deriving the fixture from the code under test is the same rule
  # the extension-coverage case above follows.
  ( cd "$NODE_SB/ws" \
    && . "$REPO_ROOT/ci/lib/common.sh" \
    && printf '%s %s %s\n' \
      "$(ci::common::hash_file bun.lock)" \
      "$(ci::common::hash_file package.json)" \
      "$(ci::common::node_runtime_id)" \
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: test:unit also satisfies the requirement" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test:unit": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=999.0.0" }, "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"does not satisfy the engines.node range"* ]]
  [[ "$output" == *">=999.0.0"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a satisfied engines.node is not in the way" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=0.0.1" }, "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a range it cannot evaluate is reported, not assumed" {
  # Failing open here would be the whole finding again: an exotic range read as
  # "fine" is indistinguishable from no check at all.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": "1.2.3 - 2.3.4" }, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">=0.0.1 <${major}\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"does not satisfy"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a packageManager pin that the host misses stops the workspace" {
  command -v bun >/dev/null 2>&1 || skip "bun is not installed on this host"
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "packageManager": "bun@0.0.1", "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"pins packageManager bun@0.0.1"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: the declared packageManager must be the one the lockfile selects" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "packageManager": "pnpm@9.0.0", "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"packageManager\": \"bun@${bunv}+abc123\", \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a manifest declaring no toolchain is left alone" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=999.0.0" }, "scripts": { "test": "bash scripts/fail.sh" } }'
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"${major}\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: an X-range still excludes the neighbouring major" {
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"$((major + 1))\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=999.0.0 ||" }, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=0.0.1 ||" }, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=0.0.1 || 1.2.3 - 2.3.4" }, "scripts": { "test": "bash scripts/test.sh" } }'
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
  [[ "$output" == *"no package.json beside it"* ]]
  [[ "$output" == *"bun.lock"* ]]
  rm -rf "$NODE_SB"
}

@test "tests-js: a package manager's value-taking option is not the command" {
  # The sharpest fail-open this file has. `npx --package vitest jest --ci` is
  # the documented way to run a tool without installing it, and the reader
  # treated `--package` as valueless -- so it declared `vitest`, the name that
  # is the option's *value*. In a workspace that installs both runners this lane
  # then ran Vitest over a Jest suite: Vitest collects nothing it recognises,
  # exits 0, and tests-js reports PASS with every Jest test uncollected.
  #
  # ci/checks/node.sh's _pm_advance was keyed by manager in 0105adc8 for exactly
  # this reason; this copy kept the un-keyed list. Keyed here now, so `-p` can
  # mean `--package` to npx and `--parseable` to pnpm without one eating the
  # other's command.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib"
  cp "$REPO_ROOT/ci/checks/tests.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  _tj_declared() {
    printf '{ "name": "w", "scripts": { "test": %s } }\n' "$1" > "$sb/package.json"
    ( cd "$sb" && bash -c '. ci/lib/log.sh 2>/dev/null
                           . ci/lib/common.sh
                           eval "$(sed -n "/^_tests_js_declared_runner()/,/^}/p" ci/checks/tests.sh)"
                           _tests_js_declared_runner || printf ""' )
  }

  [ "$(_tj_declared '"npx --package vitest jest --ci"')" = "jest" ]
  [ "$(_tj_declared '"npx -p vitest jest --ci"')" = "jest" ]
  [ "$(_tj_declared '"npm --package vitest jest --ci"')" = "jest" ]

  # The joined spelling always worked, and is the tell that the value was being
  # read as a word rather than as the option's.
  [ "$(_tj_declared '"npx --package=vitest jest --ci"')" = "jest" ]

  # The control that keyed lists exist for: pnpm's `-p` is `--parseable`, a
  # boolean, so the word after it *is* the command. A shared list would have to
  # eat one of these two to satisfy the other.
  [ "$(_tj_declared '"pnpm -p vitest run"')" = "vitest" ]
  [ "$(_tj_declared '"pnpm --filter web vitest run"')" = "vitest" ]

  # And the plain forms are untouched.
  [ "$(_tj_declared '"vitest run"')" = "vitest" ]
  [ "$(_tj_declared '"jest --ci"')" = "jest" ]
  rm -rf "$sb"
}

@test "js lane: the two runner readers agree about the same test script" {
  # ci/checks/node.sh and ci/checks/tests.sh both answer "which runner does this
  # test script name", for different purposes: node.sh refuses a script that
  # runs no runner, tests.sh picks which runner to invoke when a workspace
  # installs both. The comment above the tests.sh reader has claimed since it
  # was written that "ci/tests/test_js_lane.bats pins the two readers against
  # the same spellings so they cannot drift apart about a wrapper".
  #
  # Nothing did. They drifted on two spellings at once, both of them fixed in
  # node.sh first and left standing in tests.sh:
  #
  #   true&&vitest run              node.sh: fixed here    tests.sh: read as one token
  #   npx --package vitest jest     node.sh: fixed in 0105adc8  tests.sh: declared vitest
  #
  # The second is the sharper one -- tests.sh would run Vitest over a Jest suite,
  # collect nothing, and report PASS. This case is that claim made true.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib"
  cp "$REPO_ROOT/ci/checks/tests.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"

  # The tests.sh reader, extracted and driven directly: it reads ./package.json
  # through node, so the fixture is a manifest.
  _declared() {
    printf '{ "name": "w", "scripts": { "test": %s } }\n' "$1" > "$sb/package.json"
    ( cd "$sb" && bash -c '. ci/lib/log.sh 2>/dev/null
                           . ci/lib/common.sh
                           eval "$(sed -n "/^_tests_js_declared_runner()/,/^}/p" ci/checks/tests.sh)"
                           _tests_js_declared_runner || printf ""' )
  }
  # The node.sh side is asked through the lane itself rather than through one of
  # its helpers. The two files do not share a function, so the property worth
  # pinning is the one a developer feels: for the same script, tests.sh names
  # the runner the shell would run, and node.sh does not refuse it for naming
  # none. Reaching into _command_runner asks a narrower question -- it answers
  # with the *first* command, so `true && vitest run` is `true` there and that
  # is correct -- and a case built on it fails for a reason no user has.
  ws_setup
  rm -f "$NODE_SB/ws/tsconfig.json"

  # The spellings, and what the shell would actually run for each.
  local spec truth got_t
  for spec in \
    'true&&vitest run|vitest' \
    'true && vitest run|vitest' \
    'true;vitest run|vitest' \
    'npx --package vitest jest --ci|jest' \
    'npx --package=vitest jest --ci|jest' \
    'pnpm -p vitest run|vitest' \
    'timeout 300 vitest run|vitest' \
    'env FOO=1 jest --ci|jest' \
    'jest --ci|jest'
  do
    truth="${spec##*|}"
    spec="${spec%|*}"
    got_t="$(_declared "\"$spec\"")"
    [ "$got_t" = "$truth" ] \
      || { echo "tests.sh read '$spec' as '${got_t:-<none>}', shell runs '$truth'" >&2; rm -rf "$sb"; return 1; }

    # node.sh's half. `;` throws the runner's status away and is refused on
    # those grounds, which is a different objection from "names no runner" --
    # the point here is that neither reader may fail to *find* the runner.
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"$spec\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"not appear to run a test runner"* ]] \
      || { echo "node.sh could not find the runner in '$spec'" >&2; echo "$output" >&2; rm -rf "$sb"; return 1; }
  done

  # And the control that keeps this from being "both readers say vitest to
  # everything": a script that names no runner is named by neither.
  got_t="$(_declared '"echo nothing"')"
  [ -z "$got_t" ]
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "echo nothing" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"not appear to run a test runner"* ]]
  rm -rf "$sb"
  rm -rf "$NODE_SB"
}

@test "js lane: every reader of 'is this a TypeScript project' knows the same names" {
  # Four places ask this one question, in three syntaxes:
  #
  #   ci/checks/node.sh   _ts_project_files  -- does this workspace need a
  #                                             typecheck script at all
  #   ci/checks/node.sh   the orphan scan    -- is a lost package.json noticed
  #   ci/checks/typecheck.sh                 -- which projects get compiled
  #   ci/lib/changeset.sh the classifier arm -- which lanes get scheduled
  #
  # Each disagreement has already happened once. The first three drifted when
  # tsconfig.app.json was added to two of them, and the orphan scan was still
  # on the original list a commit later -- so this case is the thing that
  # notices next time, rather than a fifth review round.
  #
  # Compared as sets of names with the syntax normalised away: `find -name`
  # globs, an ERE with escaped dots and `[^/]+`, and a shell case pattern.
  local expected
  expected="$(printf '%s\n' jsconfig.'*'.json jsconfig.json tsconfig.'*'.json tsconfig.json | sort)"

  # A guard on the extractor itself: a normaliser that matches nothing would
  # make every comparison below a comparison of two empty strings.
  [ -n "$expected" ]

  _tsnames() {
    # Backslashes (ERE escapes) removed, then `[^/]+` folded to `*`, so all
    # three syntaxes spell the same four names the same way.
    tr -d '\\' \
      | grep -oE '(ts|js)config\.(json|\*\.json|\[\^/\]\+\.json)' \
      | sed 's/\[\^\/\]+\.json/*.json/' \
      | sort -u
  }

  local got
  got="$(sed -n '/^_ts_project_files()/,/^}/p' "$REPO_ROOT/ci/checks/node.sh" | _tsnames)"
  [ "$got" = "$expected" ] \
    || { echo "node.sh _ts_project_files: $got" >&2; return 1; }

  got="$(sed -n '/ORPHAN_CFG="\$(find/,/-print 2>\/dev\/null/p' "$REPO_ROOT/ci/checks/node.sh" | _tsnames)"
  [ "$got" = "$expected" ] \
    || { echo "node.sh orphan find: $got" >&2; return 1; }

  # The ship-mode half of the same scan, which reads HEAD through a grep rather
  # than the filesystem through find -- two spellings of one list, and the place
  # a fix applied to only one of them would show up.
  got="$(grep -F 'npm-shrinkwrap' "$REPO_ROOT/ci/checks/node.sh" | grep -F 'grep -E' | _tsnames)"
  [ "$got" = "$expected" ] \
    || { echo "node.sh orphan grep: $got" >&2; return 1; }

  got="$(grep -n "name 'tsconfig" -A 2 "$REPO_ROOT/ci/checks/typecheck.sh" | _tsnames)"
  [ "$got" = "$expected" ] \
    || { echo "typecheck.sh: $got" >&2; return 1; }

  got="$(grep -F 'tsconfig.json|' "$REPO_ROOT/ci/lib/changeset.sh" | tr '|' '\n' | _tsnames)"
  [ "$got" = "$expected" ] \
    || { echo "changeset.sh: $got" >&2; return 1; }
}

@test "node lane: a compact && is the same composition as a spaced one" {
  # `&&` is the one composition this reader calls safe -- either the checker
  # runs or the thing before it failed and the script fails with it -- and the
  # separator normalisation above spaced out `;` and left it joined. So
  # `true&&vitest run` tokenized as `true&&vitest`, which sits in command
  # position and is not a runner, and a legitimate script was refused as "does
  # not appear to run a test runner".
  #
  # The mirror image of the `;` fix beside it: that one spaced its separator to
  # close a bypass, this one spaces its separator to stop inventing a failure.
  # Both are the tokenizer disagreeing with the shell.
  #
  # The runner is named bare here rather than as `./scripts/vitest`, which is
  # what the other cases in this file use. A path form was never affected: the
  # token `true&&./scripts/vitest` still ends in `/vitest`, and the basename is
  # what gets matched, so the joined separator was invisible. Only the bare name
  # reproduces -- my first draft of this case used the path and passed against
  # the unfixed tree.
  #
  # Asserted on what the lane says rather than on its exit status, because a
  # bare `vitest` is not installed in this sandbox: the script is reached and
  # then fails for a reason that has nothing to do with the rule. "Running
  # script: test" is printed before the script runs, which is exactly the
  # boundary this case is about.
  ws_setup
  rm -f "$NODE_SB/ws/tsconfig.json"

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true&&vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"not appear to run a test runner"* ]] \
    || { echo "a compact && was read as part of the command name" >&2; echo "$output" >&2; return 1; }
  [[ "$output" == *"Running script: test"* ]]

  # The spaced spelling is the same shell program and already worked; asserting
  # it here is what makes the case about the spacing rather than about the
  # fixture.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true && vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"not appear to run a test runner"* ]]
  [[ "$output" == *"Running script: test"* ]]

  # The controls, each checking that spacing the separator did not space past a
  # rule. A narrowing positional after a compact `&&` is still a narrowing
  # positional -- and refused in the same words as the spaced spelling, not by
  # the runner rule firing for the wrong reason, which is how the unfixed tree
  # happened to reach the right verdict here.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true&&vitest run tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  # A config redirect likewise.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true&&vitest --config other.ts run" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  # `||` is still refused, joined or not: it either never reaches the checker or
  # throws the checker's result away.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run||true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" != *"Running script: test"* ]]

  # A single `&` backgrounds the checker and is not `&&` with a character
  # missing -- the two-character match must not fire on it.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true&vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"backgrounds"* ]]

  # And a quoted `&&` is data, not a separator: the mask is what is scanned, so
  # an argument containing it is left where it is.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run --reporter=\"a&&b\"" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"Running script: test"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: the orphan scan knows every configuration name discovery does" {
  # The scan's list of what counts as workspace configuration was
  # `tsconfig.json|jsconfig.json`, which was the whole list until discovery
  # widened to tsconfig.*.json. So the `npm create vite` shape -- tsconfig.app.
  # json and tsconfig.node.json, no plain tsconfig.json -- was a workspace whose
  # manifest could go missing with nothing left behind that this scan recognised:
  # "No package.json found. Skipping Node lane.", exit 0, over a directory of
  # unchecked TypeScript.
  #
  # A name this scan does not know is a directory whose loss it cannot notice,
  # which is why the two lists have to agree. Found by sweeping the other readers
  # after the same disagreement was reported one commit over.
  ws_setup
  rm -rf "$NODE_SB/ws"
  mkdir -p "$NODE_SB/app/src"
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/app/tsconfig.app.json"
  printf '{ "compilerOptions": {} }\n' > "$NODE_SB/app/tsconfig.node.json"
  printf 'export const n: number = "no";\n' > "$NODE_SB/app/src/a.ts"

  # The premise: nothing else in the directory is on the old list -- no
  # lockfile, no vite config, no plain tsconfig.json -- so the scaffold configs
  # are the only thing that can report this directory.
  [ ! -f "$NODE_SB/app/tsconfig.json" ]
  run bash -c "ls '$NODE_SB/app'"
  [[ "$output" != *"lock"* ]]
  [[ "$output" != *"vite"* ]]

  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ] \
    || { echo "a lost manifest went unnoticed beside its scaffold configs" >&2; echo "$output" >&2; return 1; }
  [[ "$output" == *"no package.json beside it"* ]]
  [[ "$output" == *"tsconfig.app.json"* ]]
  [[ "$output" == *"tsconfig.node.json"* ]]
  # And not by the route this case is not about.
  [[ "$output" != *"No package.json found"* ]]

  # The control that keeps the rule: a manifest beside them settles it, in
  # exactly the shape the scan exists to distinguish.
  printf '{ "name": "app", "private": true }\n' > "$NODE_SB/app/package.json"
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [[ "$output" != *"no package.json beside it"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an orphaned workspace beside a healthy one still fails" {
  # The scan was nested under "the repository has no workspace at all", which
  # made an orphan a property of the repository rather than of the directory it
  # sits in. With two siblings, deleting b/package.json left a, discovery
  # succeeded, and b — lockfile and all — was never looked at.
  ws_setup
  mkdir -p "$NODE_SB/wb"
  printf '{ "name": "b", "private": true, "scripts": { "test": "bash scripts/test.sh" } }\n' > "$NODE_SB/wb/package.json"
  printf '{}\n' > "$NODE_SB/wb/bun.lock"
  printf '{}\n' > "$NODE_SB/wb/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  rm -f "$NODE_SB/wb/package.json"
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"no package.json beside it"* ]]
  [[ "$output" == *"wb/"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a workspace added only in the index is not an orphan" {
  # The control: a manifest that exists in the index but not yet on disk is a
  # workspace being added, not configuration left behind by a deletion.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  rm -f ws/package.json
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  # The manifest is gone from disk, so the workspace guard fires — but not the
  # orphan scan, which is what this case is about.
  [[ "$output" != *"no package.json beside it"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a manifest staged for deletion but restored on disk fails" {
  # Discovery reads the filesystem, so the restored worktree copy looked like a
  # healthy workspace and every check below read it. Both pre-commit and
  # pre-push passed for a commit carrying no manifest at all.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=banana" }, "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 30 ]
  [[ "$output" == *"Cannot evaluate"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a comparator with no operand is unverifiable" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "engines": { "node": ">=" }, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"~${major}\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"~${major}.$((minor + 1))\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  # Stage a manifest with no test script, then restore the healthy one on disk.
  printf '%s\n' '{ "name": "w", "private": true, "scripts": {} }' > ws/package.json
  git add ws/package.json >/dev/null 2>&1
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }' > ws/package.json
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">=20banana || >=${major}\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">=${major} || 1.2.3 - 2.3.4\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh", "build": "true" } }' > ws/package.json
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: an index that matches the worktree is not in the way" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/fail10.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"failed with status 10"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an ordinary script failure is still a new issue" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/fail.sh" } }'
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
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"${bad}\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"=${major}\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: an equality range that states every component still pins it" {
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"=$((major + 1)).0.0\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"<=${major}\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">${major}\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">${ver}\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  mkdir -p added
  printf '%s\n' '{ "name": "a", "private": true, "scripts": { "test": "bash scripts/fail.sh" } }' > added/package.json
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"no 'typecheck' script"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a typecheck script satisfies the TypeScript requirement" {
  # The fixture was `"typecheck": "true"`, chosen as a convenient stub — and
  # that stub is precisely the no-op the lane now rejects, so this case was
  # asserting that a typecheck which runs no compiler is acceptable. A real
  # command instead.
  #
  # The assertion is what this case can honestly claim: the declared-TypeScript
  # rules do not fire. It cannot assert an exit of 0, because the sandbox has no
  # tsc to run and the lane would then fail on a missing binary — a real result,
  # but a different question from the one in the title.
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"defines no 'typecheck' script"* ]]
  [[ "$output" != *"type checker"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a workspace with no TypeScript config needs no typecheck script" {
  # The negative control: the rule is keyed on the workspace declaring
  # TypeScript, not on every workspace everywhere.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">= ${major}\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a spaced comparator is still evaluated, not waved through" {
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \">= $((major + 1))\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  local elapsed
  elapsed="$(printf '%s\n' "$output" | sed -n 's/^elapsed=//p')"
  [ -n "$elapsed" ]

  # ci::runner::submit wraps the check only when `timeout` or `gtimeout` exists
  # and runs it bare otherwise, which is documented behaviour and not a defect.
  # Asserting rc=124 unconditionally therefore made this case fail on a machine
  # that has neither, for the one reason the runner is entitled to. Both
  # branches are asserted instead of skipping either: a skip on the machine that
  # lacks the tool is indistinguishable from a skip on the machine where the
  # feature regressed.
  if command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1; then
    # 124 is what `timeout` returns, and what preflight maps to FAIL_INFRA, so
    # a killed check is reported as one rather than as a mystery.
    [[ "$output" == *"rc=124"* ]]
    [ "$elapsed" -lt 5 ]
  else
    # No timeout tool: the check must still run to completion and report its own
    # result. What must never happen is the runner inventing a timeout it cannot
    # enforce.
    [[ "$output" == *"rc=0"* ]]
    [ "$elapsed" -ge 5 ]
  fi
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" }, "dependencies": { "x": "1.0.0" } }'
  run ws_run
  [[ "$output" != *"up to date"* ]]
  [[ "$output" == *"Installing dependencies"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an unchanged workspace still skips the install" {
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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

  # `tr -d $' \t'` rather than `tr -d ' \t'`: bash expands the escape itself, so
  # the set handed to tr is a real space and a real tab and does not depend on
  # tr interpreting `\t`. The failure mode of getting that wrong is silent —
  # a tr that took the backslash literally would delete every `t` and turn the
  # parsed `.ts` and `.cts` into `.s` and `.cs`, and the loop below would then
  # report the classifier disagreeing about extensions that do not exist.
  local exts
  exts="$(sed -n '/^  case "\$ext" in$/,/^  esac$/p' ci/lib/changeset.sh \
    | grep -E "printf 'javascript'" \
    | sed 's/).*//' \
    | tr '|' '\n' \
    | tr -d $' \t' \
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"~${major}.x\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"^${major}.x\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"~$((major - 1)).0\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
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
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }' > ws/package.json
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
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
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh", "build": "true" } }' > ws/package.json
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

@test "node lane: a comparator on an unstated major follows node-semver" {
  # node-semver resolves an X-range major before any comparator runs: ">=x" and
  # "<=x" become "*", "^x" and "~x" become "*", and ">x" and "<x" become
  # "<0.0.0-0". Reading the wildcard as 0 instead got four of the six backwards.
  ws_setup
  source "$REPO_ROOT/ci/lib/common.sh"
  local fns="$NODE_SB/semver.sh"
  sed -n '/^_semver_part()/,/^_semver_satisfies()/p' "$REPO_ROOT/ci/checks/node.sh" \
    | sed '$d' > "$fns"
  sed -n '/^_semver_satisfies()/,/^}/p' "$REPO_ROOT/ci/checks/node.sh" >> "$fns"
  bash -n "$fns"
  # shellcheck disable=SC1090
  . "$fns"

  local spec want rc bad=""
  # spec:expected, where expected is 0 satisfied / 1 not satisfied.
  for pair in '>=x:0' '<=x:0' '^x:0' '~x:0' '>x:1' '<x:1' \
              '>=X:0' '>=*:0' '>*:1' 'x:0' '*:0' '=x:0'; do
    spec="${pair%:*}"; want="${pair#*:}"
    rc=0
    _semver_token_ok 20.11.1 "$spec" || rc=$?
    [ "$rc" -eq "$want" ] || bad="${bad} ${spec}(want=${want} got=${rc})"
  done
  [ -z "$bad" ] || { echo "wildcard-major mismatches:${bad}" >&2; return 1; }
  rm -rf "$NODE_SB"
}

@test "node lane: a caret range below 0.1.0 pins the patch" {
  # "^0.0.3" is ">=0.0.3 <0.0.4": below 0.1.0 the patch carries the breaking
  # change, so constraining the minor alone admitted 0.0.9.
  ws_setup
  source "$REPO_ROOT/ci/lib/common.sh"
  local fns="$NODE_SB/semver.sh"
  sed -n '/^_semver_part()/,/^_semver_satisfies()/p' "$REPO_ROOT/ci/checks/node.sh" \
    | sed '$d' > "$fns"
  sed -n '/^_semver_satisfies()/,/^}/p' "$REPO_ROOT/ci/checks/node.sh" >> "$fns"
  bash -n "$fns"
  # shellcheck disable=SC1090
  . "$fns"

  local rc bad=""
  # version:spec:expected
  for triple in '0.0.9:^0.0.3:1' '0.0.3:^0.0.3:0' '0.0.2:^0.0.3:1' \
                '0.0.9:^0.0.x:0' '0.1.0:^0.0.x:1' \
                '0.2.9:^0.2.3:0' '0.3.0:^0.2.3:1' \
                '1.9.9:^1.2.3:0' '2.0.0:^1.2.3:1'; do
    local ver="${triple%%:*}" rest="${triple#*:}"
    local spec="${rest%:*}" want="${rest#*:}"
    rc=0
    _semver_token_ok "$ver" "$spec" || rc=$?
    [ "$rc" -eq "$want" ] || bad="${bad} ${spec}@${ver}(want=${want} got=${rc})"
  done
  [ -z "$bad" ] || { echo "caret mismatches:${bad}" >&2; return 1; }
  rm -rf "$NODE_SB"
}

@test "affected: a nested module-extension source maps to frontend tests" {
  # Reported in review as a gap: only "frontend/src/*.mts" is declared, so
  # frontend/src/lib/x.mts was said to match nothing. It matches — the matcher
  # collapses "**" to "*" and compares with `case`, where "*" crosses "/", so
  # the direct-child spelling already covers nested paths. This passes at
  # 98e3ecc8 too; it is here to hold the behaviour the disposition rests on,
  # because the reasoning depends on a matcher detail that could be changed by
  # someone tightening the globs with no idea this was load-bearing.
  source ci/lib/affected.sh
  local ext
  for ext in mts cts mjs cjs; do
    run ci::affected::get_affected_tests "frontend/src/lib/nested.$ext"
    [ "$status" -eq 0 ]
    [[ "$output" == *"frontend/tests/"* ]] \
      || { echo "no frontend test pattern for nested .$ext" >&2; return 1; }
  done
}

@test "preflight: a renamed .gitignore still un-filters the shell suite" {
  # _CI_CHANGESET_FILES_RAW holds STATUS<TAB>PATH records and a rename is
  # R100<TAB>old<TAB>new, so an end-of-line anchor matched only the destination.
  # Renaming .gitignore away therefore filtered out the suite that guards it.
  #
  # One occurrence now, not two. The second was the gate's fast exit, which used
  # this pattern to rescue a changeset whose paths named no language -- and that
  # exit no longer asks which paths it holds, only whether it holds any. A
  # renamed .gitignore is a path, so the fast exit cannot fire on it at all,
  # which is a stronger statement than the rescue was. The occurrence that
  # remains is the changeset filter, which is what decides whether this lane
  # runs once the gate has started.
  run grep -nE "gitignore\(\[\[:space:\]\]\|\\$\)" ci/preflight.sh
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | wc -l)" -eq 1 ]
  # And the exit that used to carry the other one now asks a different question.
  run grep -n '_pf_has_paths' ci/preflight.sh
  [ "$status" -eq 0 ]
  # And the pattern itself matches a rename record, not just the source text.
  run bash -c "printf 'R100\t.gitignore\t.gitignore.old\n' \
    | grep -qE '(^|[[:space:]])(ci/|\.githooks/|\.gitignore([[:space:]]|\$))'"
  [ "$status" -eq 0 ]
}

@test "node lane: a component stated after a wildcard is rejected, except after ^ or ~" {
  # This one turned over twice, so the evidence matters more than the rule.
  #
  # First reading: "20.*.3 should be rejected as malformed" — refuted, because
  # node-semver read any X as making everything to its right an X.
  # Second reading: accept it everywhere and truncate at the first wildcard.
  # Both were measured against the semver of the day, and the answer moved:
  # invalidXRangeOrder was added in 7.8.4 (confirmed by diffing classes/range.js
  # across 7.8.0, 7.8.3, 7.8.4 and 7.8.5 — absent in the first two, present in
  # the last two). From 7.8.4 on, `new Range("20.x.3")` throws.
  #
  # But it does not throw everywhere, and that is the part a rule stated as
  # "reject X-order" gets wrong. invalidXRangeOrder is reached from
  # replaceXRange only. replaceTilde and replaceCaret rewrite the operand
  # before any X-range pass sees it, and hyphenReplace never routes through it
  # at all. Measured on 7.8.5, over every operator crossed with every X-order
  # shape: the 30 that throw are bare and = >= > < <=, the 34 that construct
  # include every ^ and ~ form. Applying the rule uniformly made the gate
  # refuse "~22.x.1" — a range every published semver accepts.
  #
  # So: version-dependent for the comparator forms, and this gate refuses them
  # (fail-closed: npm >=7.8.4 will not install against them either); always
  # valid after ^ or ~, and evaluated by truncating at the first wildcard.
  ws_setup
  source "$REPO_ROOT/ci/lib/common.sh"
  local fns="$NODE_SB/semver.sh"
  sed -n '/^_semver_part()/,/^_semver_satisfies()/p' "$REPO_ROOT/ci/checks/node.sh" \
    | sed '$d' > "$fns"
  sed -n '/^_semver_satisfies()/,/^}/p' "$REPO_ROOT/ci/checks/node.sh" >> "$fns"
  bash -n "$fns"
  # shellcheck disable=SC1090
  . "$fns"

  local rc bad=""
  # version:spec:expected — 0 satisfied, 1 unsatisfied, 3 malformed operand.
  # The comparator forms are the ones 7.8.4+ throws on; ^ and ~ are the ones it
  # keeps, and there the wildcard still truncates so the trailing 3 is dropped
  # rather than compared against the runtime patch.
  for triple in '20.0.1:>=20.*.3:3' '19.9.9:>=20.*.3:3' \
                '20.11.1:<=20.*.3:3' '21.0.0:<=20.*.3:3' \
                '20.11.1:>20.*.3:3'  '21.0.0:>20.*.3:3' \
                '19.9.9:<20.*.3:3'   '20.0.0:<20.*.3:3' \
                '20.0.1:20.*.3:3'    '21.0.0:20.*.3:3' \
                '20.0.1:=20.*.3:3' \
                '20.11.1:^20.*.3:0'  '20.11.1:~20.*.3:0' \
                '21.0.0:^20.*.3:1'   '21.0.0:~20.*.3:1' \
                '20.11.1:~22.x.1:1'  '22.12.0:~22.x.1:0' \
                '20.11.1:20..1:3'    '20.11.1:>=20..1:3'; do
    local ver="${triple%%:*}" rest="${triple#*:}"
    local spec="${rest%:*}" want="${rest#*:}"
    rc=0
    _semver_token_ok "$ver" "$spec" || rc=$?
    [ "$rc" -eq "$want" ] || bad="${bad} ${spec}@${ver}(want=${want} got=${rc})"
  done
  # The grammar check still runs first, so a genuinely malformed operand is not
  # truncated into something legal.
  [ -z "$bad" ] || { echo "wildcard-truncation mismatches:${bad}" >&2; return 1; }
  rm -rf "$NODE_SB"
}

@test "node lane: an ignored recreation of a staged deletion is caught" {
  # `git ls-files --others` adds the standard exclusions, so a file recreated
  # after its deletion was staged shows as `!!` rather than `??` once .gitignore
  # covers it, and dropped out of the list the intersection was built from. The
  # rule now asks each staged path directly: whether a path is ignored has
  # nothing to do with whether the lane is about to read it.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  printf 'export const ok = true;\n' > "$NODE_SB/ws/app.js"
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  git rm --cached -q ws/app.js >/dev/null 2>&1
  rm -f ws/app.js
  printf 'ws/app.js\n' > .gitignore
  printf 'export const recreated = 1;\n' > ws/app.js
  ws_seed_fingerprint
  # The premise: git agrees the file is ignored, not merely untracked.
  run bash -c "cd '$NODE_SB' && git status --porcelain --ignored ws/app.js"
  [[ "$output" == *"!!"* ]]
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"ws/app.js"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: losing the whole suite fails even with a test script left behind" {
  # The suite-loss check was nested under "and no test script", which made
  # deleting every test safe as long as the runner survived —
  # `vitest run --passWithNoTests` exits 0 on an empty collection, so the lane
  # reported a pass over a suite that no longer exists.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  rm -rf ws/tests
  git add -A >/dev/null 2>&1
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"lost its entire test suite"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a workspace that never had tests is not accused of losing them" {
  # The control. Keyed on HEAD carrying tests, not on the tree lacking them.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "build": "true" } }'
  rm -rf "$NODE_SB/ws/tests"
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a tilde range with a stated patch after a wildcard covers the major" {
  # Reported after the wildcard-minor fix: `~20.x.1` is `>=20.0.0 <21.0.0-0`
  # to npm, and mapping the `x` to zero compared the runtime patch against the
  # trailing 1. Already cured by normalising the operand at the grammar check,
  # which truncates at the first wildcard — pinned here so it stays cured.
  ws_setup
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"engines\": { \"node\": \"~${major}.x.1\" }, \"scripts\": { \"test\": \"bash scripts/test.sh\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a test script carrying a persistent name filter fails" {
  # The layout guard rejects a persistent filter written into vitest.config.ts.
  # This is the same filter one layer out: `vitest run -t no-such-name` exits 0
  # with every collected test skipped, and the config the guard inspects is
  # untouched. "A test script exists" said nothing about whether it runs
  # anything, exactly as "a typecheck script exists" said nothing about tsc.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run -t definitely-no-such-test" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: every narrowing flag is rejected, not just -t" {
  ws_setup
  local flag
  for flag in '-t x' '--testNamePattern=x' '--shard=1/2' '--bail=1' '--changed' '--related src/a.ts'; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"vitest run ${flag}\" } }"
    ws_seed_fingerprint
    run ws_run
    [ "$status" -eq 20 ] || { echo "flag '${flag}' passed the guard" >&2; return 1; }
  done
  rm -rf "$NODE_SB"
}

@test "node lane: an ordinary test script is not read as a filter" {
  # The control. A path or a test name is not a flag, and matching on substrings
  # would fail every workspace whose script mentions one.
  #
  # A wrapper, not a runner: for a known runner that trailing path *is* a filter
  # and is rejected on purpose, which is the case above.
  #
  # The wrapper is ws_setup's, deliberately. This case used to write its own
  # `scripts/test.sh` containing `exit 0` — re-creating, one round later and in
  # a control, the no-op the rule above it exists to reject, and it passed
  # because delegation was accepted without the target ever being read. What is
  # under test here is the *argument* handling, so the wrapper only has to be
  # one the lane accepts.
  #
  # The positional this case used to carry -- `tests/no-t-here.test.ts` beside
  # the flags -- is gone from it, and that is a correction rather than a
  # weakening. ws_setup's wrapper is `vitest run "$@"`, so that path was
  # forwarded straight to the runner and collected one file: a filter, wearing a
  # wrapper. It is refused now, by `node lane: arguments handed to a delegated
  # wrapper are not ignored`. What this case still asserts is the property it
  # was written for -- that a flag *value* which happens to look like a path or
  # a test name is not matched by substring.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh --reporter=dot --outputFile=no-t-here.test.json" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: in ship mode an ignored replacement of a deleted path is caught" {
  # An outgoing commit deletes a workspace file and adds its path to .gitignore;
  # the worktree keeps a passing copy. HEAD does not carry the path so
  # `git diff HEAD` says nothing, and --exclude-standard is documented to drop
  # exactly that file — so the lane ran the replacement for a commit that
  # removes it. Same defect as the pre-commit branch had, one mode over.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  printf 'export const ok = true;\n' > "$NODE_SB/ws/app.js"
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  git rm -q ws/app.js >/dev/null 2>&1
  printf 'ws/app.js\n' > .gitignore
  git add .gitignore >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm "delete and ignore" >/dev/null 2>&1
  printf 'export const recreated = 1;\n' > ws/app.js
  ws_seed_fingerprint
  # The premise: neither list the guard used to consult sees this file.
  run bash -c "cd '$NODE_SB' && git diff --name-only HEAD -- ws; git ls-files --others --exclude-standard -- ws"
  [ -z "$output" ]
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"ws/app.js"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: in ship mode an ignored build directory is not drift" {
  # The control, and the reason the ignored list is pruned rather than taken
  # whole: node_modules and dist are ignored on purpose, and reporting them
  # would mean the ship gate never passes for any Node workspace.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  printf 'ws/node_modules/\nws/dist/\n' > .gitignore
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  mkdir -p ws/dist
  printf 'built\n' > ws/dist/bundle.js
  printf 'dep\n' > ws/node_modules/dep.js
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a workspace nested below the first directory level is discovered" {
  # Discovery looked one level down while ci::changeset::_in_node_workspace
  # walked up from any depth, so packages/app/src/x.ts scheduled the node lane
  # and node.sh then printed "No package.json found" and exited 0. Scheduling
  # and execution have to mean the same thing by "workspace".
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/packages/app" "$sb/node_modules/dep" "$sb/packages/app/dist"
  printf '{}\n' > "$sb/packages/app/package.json"
  printf '{}\n' > "$sb/node_modules/dep/package.json"
  printf '{}\n' > "$sb/packages/app/dist/package.json"
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' && ci::common::node_workspaces"
  [ "$status" -eq 0 ]
  [[ "$output" == *"packages/app"* ]]
  [[ "$output" != *"node_modules"* ]]
  [[ "$output" != *"dist"* ]]
  rm -rf "$sb"
}

@test "node lane: a fixture package under a test tree is not a workspace" {
  # The other half of recursive discovery. ci/tests/fixtures/node/package.json
  # is a real manifest with no lockfile, and treating it as a workspace made the
  # lane refuse the install and report FAIL_INFRA for a directory that exists to
  # be a fixture. Pruned only under a test tree, so packages/fixtures/ survives.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/tests/fixtures/node" "$sb/packages/fixtures" "$sb/app/testdata/pkg"
  printf '{}\n' > "$sb/ci/tests/fixtures/node/package.json"
  printf '{}\n' > "$sb/packages/fixtures/package.json"
  printf '{}\n' > "$sb/app/testdata/pkg/package.json"
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' && ci::common::node_workspaces"
  [ "$status" -eq 0 ]
  [[ "$output" == *"packages/fixtures"* ]]
  [[ "$output" != *"ci/tests/fixtures"* ]]
  [[ "$output" != *"testdata"* ]]
  rm -rf "$sb"
}

@test "changeset: scheduling uses the same workspace definition as discovery" {
  # One predicate, consulted by both. Two similar ones is how they disagreed.
  run bash -c "
    cd '$REPO_ROOT'
    . ci/lib/common.sh
    . ci/lib/changeset.sh
    ci::changeset::_in_node_workspace ci/tests/fixtures/node/src/a.ts && echo SCHEDULED || echo SKIPPED
  "
  [[ "$output" == *"SKIPPED"* ]]
}

@test "node lane: an unstaged file the staged one depends on stops the workspace" {
  # The rule was per-file and the lane is not: it installs, typechecks, tests
  # and builds the workspace as a unit. Stage a file, leave the helper it needs
  # untracked, and every staged path matched the index while the lane passed on
  # the strength of a file the commit does not contain.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  printf 'import { helper } from "./helper";\nexport const x = helper;\n' > ws/uses-helper.ts
  git add ws/uses-helper.ts >/dev/null 2>&1
  printf 'export const helper = 1;\n' > ws/helper.ts   # deliberately NOT staged
  ws_seed_fingerprint
  # The premise: the staged path itself matches the index exactly.
  run bash -c "cd '$NODE_SB' && git diff --name-only -- ws/uses-helper.ts"
  [ -z "$output" ]
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"ws/helper.ts"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a positional file filter in the test script is rejected" {
  # `vitest run [...filters]` is documented syntax, so a bare path narrows the
  # suite exactly as -t does while matching nothing in the flag deny-list. An
  # enumerated list of narrowing flags loses this race by construction.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run tests/one.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a wrapper script with a positional argument is not a filter" {
  # The control. A positional means "filter" only for a known test runner; for
  # `bash scripts/test.sh` it is the script being run, and rejecting it would
  # fail every workspace that wraps its suite.
  #
  # The fixture said `true scripts/test.sh --ci`, using `true` as a stand-in for
  # the wrapper, and then a `scripts/test.sh` of its own containing `exit 0`.
  # Both were the no-op this rule rejects, standing in for the wrapper it was
  # describing — the second one surviving only because delegation was accepted
  # without the target being read. ws_setup's wrapper is a real one, and what
  # this case is about is the positional argument beside it.
  #
  # `--ci` became `--coverage` for the same reason the case above lost its
  # positional: ws_setup's wrapper forwards `"$@"`, so `--ci` reached vitest,
  # which has no such flag, and an unknown flag arriving at a runner stops the
  # guard by design. `--coverage` is on the allow-list and cannot reduce the
  # run, so there is still an argument beside the target and the property under
  # test -- that `scripts/test.sh` is the script being run and not a filter --
  # is unchanged.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh --coverage" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: ship mode does not report ordinary ignored build output as drift" {
  # The prune-list attempt reported frontend/.env.local, a tsbuildinfo and
  # Playwright output as drift, which makes the ship gate unpassable during
  # ordinary development — and its printed remedy, "commit the rest", is the
  # exact thing git-safety.sh blocks. An ignored file is drift only where it
  # shadows a path the push removes.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  printf 'ws/.env.local\nws/*.tsbuildinfo\nws/test-results/\nws/src/build/\n' > .gitignore
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  printf 'VITE_API=http://localhost:8000\n' > ws/.env.local
  printf 'x\n' > ws/tsconfig.tsbuildinfo
  mkdir -p ws/test-results && printf 'x\n' > ws/test-results/report.xml
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: ship mode still catches an ignored file shadowing a deletion" {
  # And the case the ignored list exists for, which the deletion key preserves.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  printf 'export const ok = true;\n' > "$NODE_SB/ws/app.js"
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  git rm -q ws/app.js >/dev/null 2>&1
  printf 'ws/app.js\n' > .gitignore
  git add .gitignore >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm "delete and ignore" >/dev/null 2>&1
  printf 'export const recreated = 1;\n' > ws/app.js
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"ws/app.js"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a source file under a directory named build is not pruned away" {
  # The prune list matched build/ dist/ coverage/ at any depth, so a genuine
  # frontend/src/build/ was swallowed and the lane tested a replacement for a
  # file the push deletes.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  mkdir -p "$NODE_SB/ws/src/build"
  printf 'export const tokens = 1;\n' > "$NODE_SB/ws/src/build/tokens.js"
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  git rm -q ws/src/build/tokens.js >/dev/null 2>&1
  printf 'ws/src/build/\n' > .gitignore
  git add .gitignore >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm "delete and ignore" >/dev/null 2>&1
  mkdir -p ws/src/build && printf 'export const tokens = 2;\n' > ws/src/build/tokens.js
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"ws/src/build/tokens.js"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: an unreadable runtime is reported against the runtime" {
  # Nothing validated the runtime. _semver_part strips non-digits and defaults
  # to 0, so "banana" became 0.0.0; an empty string gave awk no record at all,
  # printed nothing, and every component compared equal — so a `>=` gate came
  # back satisfied for a node --version that emitted a shim banner or nothing.
  #
  # Status 4, not the generic 2. Both stop the lane, so this is not a
  # correctness change -- it is a message change, and the message was wrong in
  # a way that costs an operator real time: a broken `node --version` printed
  # "Cannot evaluate the engines.node range declared by <ws>/package.json",
  # sending someone to stare at a perfectly good manifest. A prerelease runtime
  # is readable but not orderable here, and gets its own status 5 for the same
  # reason.
  ws_setup
  source "$REPO_ROOT/ci/lib/common.sh"
  local fns="$NODE_SB/semver.sh"
  sed -n '/^_semver_part()/,/^_semver_satisfies()/p' "$REPO_ROOT/ci/checks/node.sh" \
    | sed '$d' > "$fns"
  sed -n '/^_semver_satisfies()/,/^}/p' "$REPO_ROOT/ci/checks/node.sh" >> "$fns"
  bash -n "$fns"
  # shellcheck disable=SC1090
  . "$fns"
  local rc bad=""
  for ver in '' 'v' 'banana' 'not-a-version' '20' '20.1'; do
    rc=0
    _semver_satisfies "$ver" ">=20" || rc=$?
    [ "$rc" -eq 4 ] || bad="${bad} '${ver}'(got=${rc})"
  done
  [ -z "$bad" ] || { echo "unreadable runtimes reported otherwise:${bad}" >&2; return 1; }
  # A prerelease runtime is readable, so it is a different report -- and it
  # must not be a pass. Measured against semver 7.8.5, ignoring the tail made
  # `20.1.0` come back satisfied by a 20.1.0-rc.1 runtime, which npm calls
  # false: the fail-open direction.
  for ver in '20.1.0-rc.1' '21.0.0-nightly' '1.2.3-alpha.1'; do
    rc=0
    _semver_satisfies "$ver" ">=20" || rc=$?
    [ "$rc" -eq 5 ] || bad="${bad} prerelease '${ver}'(got=${rc})"
  done
  [ -z "$bad" ] || { echo "prerelease runtimes reported otherwise:${bad}" >&2; return 1; }
  # And a real one still evaluates.
  rc=0; _semver_satisfies 20.11.1 ">=20" || rc=$?
  [ "$rc" -eq 0 ]
  rc=0; _semver_satisfies 19.9.9 ">=20" || rc=$?
  [ "$rc" -eq 1 ]
  rm -rf "$NODE_SB"
}

@test "node lane: comparator agreement with npm on the forms review found" {
  # One table for the eight root causes an oracle-backed audit against
  # node-semver 7.8.5 turned up. Statuses: 0 satisfied, 1 not, 2 unverifiable.
  ws_setup
  source "$REPO_ROOT/ci/lib/common.sh"
  local fns="$NODE_SB/semver.sh"
  sed -n '/^_semver_part()/,/^_semver_satisfies()/p' "$REPO_ROOT/ci/checks/node.sh" \
    | sed '$d' > "$fns"
  sed -n '/^_semver_satisfies()/,/^}/p' "$REPO_ROOT/ci/checks/node.sh" >> "$fns"
  # shellcheck disable=SC1090
  . "$fns"
  local rc bad="" spec ver want
  # A typo must not be rescued by a satisfied sibling; a merely unsupported
  # form must be. Leading zeros, oversized numbers and a number to the right of
  # a wildcard are all invalid operands to npm.
  #
  # Two rows here are deliberate divergences from npm, both fail-closed and
  # both measured rather than assumed:
  #   * An empty alternative from a stray "||" is ANY to node-semver -- 7.8.5
  #     gives `new Range(">=999.0.0 ||").range` == "" and .test("20.1.0") ==
  #     true. Copying that would let one keystroke nullify a declared
  #     constraint, so it is classed malformed here.
  #   * A prerelease on either side is unsupported, not guessed. Ignoring it
  #     returned SATISFIED for "1.2.3-alpha.1" @ 1.2.3 and "<=1.2.3-alpha.1" @
  #     1.2.3, both npm=false.
  # Everything else on this table agrees with 7.8.5 exactly; the full 585-case
  # sweep behind it has zero rows where this comparator says satisfied and npm
  # does not.
  for row in \
    'banana || >=20:20.1.0:2' '- || >=20:20.1.0:2' '>=20banana || >=20:20.1.0:2' \
    '1.2.3 - 2.3.4 || >=20:20.1.0:0' '1.2.3 - 2.3.4:20.1.0:2' \
    '>=020.1.0:20.1.0:2' '>=20.08.0:20.9.0:2' '>08:9.0.0:2' \
    '>=99999999999999999999.0.0:20.1.0:2' \
    '20.x.3:20.0.1:2' '>=20.*.3:20.0.1:2' \
    '>=999.0.0 ||:20.1.0:2' '|| >=20:20.1.0:2' '||:20.1.0:2'     '~22.x.1:22.12.0:0' '~22.x.1:20.1.0:1' '^20.*.3:20.11.1:0'     '>=9007199254740991:20.1.0:1' '>=20 || >=9007199254740991:20.1.0:0'     '= 20 - 22 || >=20:20.1.0:0' '20.x.3 - 22 || >=20:20.1.0:0'     '1.2.3-01 || >=20:20.1.0:2' '1.2.3-a..b || >=20:20.1.0:2'     '1.2.3+. || >=20:20.1.0:2' '>=20.1.0-rc.1 || >=20:20.1.0:0'     '1.2.3-alpha.1:1.2.3:2' '<=1.2.3-alpha.1:1.2.3:2' \
    '^20.x-alpha:20.11.1:2' '~20.1-alpha:20.11.1:2' '>=20-alpha:20.11.1:2' \
    '^20.x.x-alpha:20.11.1:0' '~20.x.1-rc.1:20.11.1:0' '20.x+b:20.11.1:0' \
    'x.x:20.1.0:0' '*.*.*:20.1.0:0' 'vx:20.1.0:0' 'v*:20.1.0:0' \
    '~>20:20.1.0:0' '~>20.1:20.1.9:0' '>==20:20.1.0:0' \
    '>=22.12.0:22.14.0:0' '>=22.12.0:20.1.0:1' '~20.x:20.11.1:0' \
    '^0.0.3:0.0.9:1' '^0.0.3:0.0.3:0' '20.x:21.0.0:1' '>= 20:20.1.0:0'; do
    spec="${row%%:*}"; ver="${row#*:}"; want="${ver#*:}"; ver="${ver%%:*}"
    rc=0
    _semver_satisfies "$ver" "$spec" || rc=$?
    [ "$rc" -eq "$want" ] || bad="${bad} '${spec}'@${ver}(want=${want} got=${rc})"
  done
  [ -z "$bad" ] || { echo "comparator disagrees with npm:${bad}" >&2; return 1; }
  rm -rf "$NODE_SB"
}

@test "node lane: a typecheck script that runs no compiler is rejected" {
  # The rule above it asked only whether a `typecheck` key exists. Changing its
  # command to a successful no-op satisfies that, exits 0, and never invokes a
  # compiler -- and `vite build` does not typecheck either, so a workspace
  # containing a type error passed the lane with no checker installed at all.
  # Editing the command reaches the same place as deleting the key.
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "typecheck": "true", "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"does not"* ]]
  [[ "$output" == *"type checker"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a real typecheck command is accepted" {
  # The control, and the shape this repository actually ships: `tsc --noEmit`.
  # A rule that rejected it would fail the workspace it was written to protect.
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "typecheck": "tsc --noEmit", "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"type checker"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a typecheck that delegates to another runner is accepted" {
  # Delegation is not evasion, but it is not taken on trust either: the script
  # it hands to is followed. `typecheck:all` has to exist in the manifest and
  # has to reach a checker, which is what npm would require of it at runtime
  # anyway — the earlier version of this case delegated to a script that was
  # never defined and still passed.
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "typecheck": "npm run typecheck:all", "typecheck:all": "tsc --noEmit", "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"type checker"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a root manifest beside nested ones is refused, not half-covered" {
  # A root package.json used to end discovery, so a repo with both a root and
  # child packages ran only the root and exited 0 while the children's failing
  # test and build were never invoked. Emitting the children instead is not the
  # fix: in a workspaces monorepo only the root carries a lockfile, and this
  # lane refuses to install a workspace that has none -- so that reading turns
  # every real monorepo red. The ambiguity is reported instead.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/packages/app"
  printf '{}' > "$sb/package.json"
  printf '{}' > "$sb/packages/app/package.json"
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' && ci::common::node_workspaces package.json"
  [ "$status" -ne 0 ]
  [[ "$output" == *"packages/app"* ]]
  [[ "$output" == *"coexists"* ]]

  # The control: a root manifest on its own is still the single workspace ".".
  rm -rf "$sb/packages"
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' && ci::common::node_workspaces package.json"
  [ "$status" -eq 0 ]
  [ "$output" = "." ]
  rm -rf "$sb"
}

@test "node lane: a directory whose name resembles the manifest is not filtered out" {
  # The root sentinel was dropped with `grep -v "^${manifest}$"`, and
  # `package.json` as a regex makes every `.` a wildcard -- so a workspace
  # directory named `package-json` matched the pattern and was skipped.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/package-json" "$sb/frontend"
  printf '{}' > "$sb/package-json/package.json"
  printf '{}' > "$sb/frontend/package.json"
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' && ci::common::node_workspaces package.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"package-json"* ]]
  [[ "$output" == *"frontend"* ]]
  rm -rf "$sb"
}

@test "node lane: a test script that runs no test runner is rejected" {
  # `"test": "true"` runs, exits 0 and collects nothing -- the whole suite
  # removed from the gate by a one-word manifest edit, with the lane still
  # reporting PASS over a workspace that still contains tests. The presence of
  # the key was never the property worth asserting, exactly as it was not for
  # `typecheck`.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"test runner"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: vitest flags that empty the suite are rejected" {
  # `--exclude 'tests/**' --passWithNoTests` makes vitest print "No test files
  # found", exit 0, and satisfy every earlier rule: neither is a name filter,
  # neither is a positional. Enumerating the narrowing flags lost this race
  # three times, so the flags are an allow-list now.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run --exclude tests/** --passWithNoTests" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: passWithNoTests alone is rejected" {
  # On its own it is the sharpest of them: it converts "collected nothing" into
  # success, which is the precise failure this lane exists to catch.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run --passWithNoTests" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: flags that do not narrow the suite are still accepted" {
  # The control, and the reason the allow-list needs to be generous: a rule
  # that rejected --coverage or --reporter would fail ordinary workspaces.
  # `vitest run` is what this repository ships.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run --coverage --reporter=verbose --silent" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]
  [[ "$output" != *"test runner"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a typecheck that only wraps a no-op is rejected" {
  # The first attempt at this rule accepted any token from a runner list as
  # evidence of delegation, so `bash -c true` satisfied it -- the exact no-op
  # the rule exists to reject, one wrapper out. Delegation counts only when it
  # names what it delegates to; an inline `-c` command names nothing and is
  # judged on its own contents.
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "typecheck": "bash -c true", "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"type checker"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a typecheck delegating to a named script is accepted" {
  # The control, and the reason delegation is allowed at all: a workspace that
  # wraps its checks in a shell script must keep working. What changed is that
  # the script has to actually reach a checker — the earlier version of this
  # case wrapped `exit 0` and passed, which is the hole it was meant to guard.
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\ntsc --noEmit\n' > "$NODE_SB/ws/scripts/tc.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "typecheck": "bash scripts/tc.sh", "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"type checker"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an inline shell command naming a checker is accepted" {
  # `bash -c` disqualifies *delegation*, not the command: if the inline text
  # names a checker, that is the checker being invoked.
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "typecheck": "bash -c tsc", "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"type checker"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a test script that only wraps a no-op is rejected" {
  # Same rule, same wrapper, the other script.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash -c true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"test runner"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a vendored manifest in the index is not a workspace" {
  # The two sources of workspaces have to agree in both directions. Discovery
  # drops ci/tests/fixtures/node through ci::common::is_vendored_path; the index
  # scan added it straight back, and being alphabetically first it was the
  # workspace the lane entered before any real one -- with no lockfile, so
  # `CI_GATE_MODE=full bash ci/checks/node.sh` exited FAIL_INFRA on a fixture and
  # never reached the workspace anybody meant. The round before this taught the
  # scan to look as deep as discovery and left it looking wider too.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  # A fixture manifest, tracked, with no lockfile beside it -- exactly the
  # shape that made the real lane exit 30.
  mkdir -p ci/tests/fixtures/node
  printf '%s\n' '{ "name": "fixture", "private": true }' > ci/tests/fixtures/node/package.json
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  ws_seed_fingerprint

  # The premise: it really is in the index, and filesystem discovery really
  # does drop it -- so anything the lane does with it came from the index scan.
  run bash -c "cd '$NODE_SB' && git ls-files -- '*/package.json' | grep -c 'fixtures/node/package.json'"
  [ "$output" -eq 1 ]
  run bash -c "cd '$NODE_SB' && source ci/lib/common.sh && ci::common::node_workspaces package.json"
  [[ "$output" != *"fixtures"* ]]

  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"fixtures/node"* ]]
  [[ "$output" != *"No lockfile"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a real staged workspace is still found through the index" {
  # The control. Pruning the index scan by the vendored rule must not prune the
  # thing that scan exists for -- a workspace that is in the commit and not on
  # disk, which is the case the scan was added for one round earlier.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  mkdir -p packages/app
  printf '%s\n' '{ "name": "a", "private": true, "scripts": { "test": "bash scripts/fail.sh" } }' > packages/app/package.json
  printf '{}\n' > packages/app/bun.lock
  git add packages >/dev/null 2>&1
  rm -rf packages
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"packages/app/package.json"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a delegated script that runs nothing is rejected" {
  # Delegation was accepted on the strength of a target *token*, without ever
  # reading the target -- so `bash scripts/noop.sh` passed while the script it
  # names is `exit 0`. That is the same no-op the rule rejects, one file out
  # rather than one wrapper out.
  ws_setup
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/noop.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/noop.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"not appear to run a test runner"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: ship mode refuses a push whose tip is not the checkout" {
  # The range is resolved from CI_GATE_PUSH_NEW_SHA, but everything that
  # actually runs -- the drift comparison and every script -- reads the
  # worktree. `git push origin other-branch` therefore gated the branch you are
  # standing on: a passing checkout vouched for an outgoing branch whose tests
  # fail, which is the confident-green this whole gate exists to remove.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm safe >/dev/null 2>&1
  local here
  here="$(git rev-parse --abbrev-ref HEAD)"
  git checkout -q -b other
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/fail.sh" } }'
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm bad >/dev/null 2>&1
  local other
  other="$(git rev-parse HEAD)"
  git checkout -q "$here"
  ws_seed_fingerprint

  # The premise: the two tips really do differ, and the checkout really is the
  # passing one -- so a PASS here is a report about the wrong tree.
  [ "$other" != "$(git rev-parse HEAD)" ]

  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_NEW_SHA='$other' \
    CI_GATE_NODE_WORKSPACE=ws bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"not the commit checked out"* ]]
  [[ "$output" == *"$other"* ]]
  # And it stopped before running anything, so it cannot have reported on the
  # checked-out branch's passing suite.
  [[ "$output" != *"Running script"* ]]

  # The control: pushing the branch you are standing on is the ordinary case
  # and must still run. Without this the fix would be satisfied by a check that
  # refuses every push.
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_NEW_SHA=\"\$(cd '$NODE_SB' && git rev-parse HEAD)\" \
    CI_GATE_NODE_WORKSPACE=ws bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"not the commit checked out"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a config-selecting flag is rejected" {
  # `--config` does not narrow what vitest collects; it changes which file
  # *declares* what it collects, and test-layout.sh validates
  # frontend/vitest.config.ts and nothing else. So `vitest run --config
  # vitest.narrow.config.ts` had the two checks reporting on two different
  # files: one confirming a broad include that is not in force, the other
  # running a config nobody inspected. The allow-list was only ever for flags
  # that cannot reduce the run.
  # Asserted on the message, not merely on the status. There is no vitest in
  # this sandbox, so `vitest run ...` exits 20 whatever the validator decides --
  # a status-only assertion here would pass for the wrong reason and keep
  # passing if the rule were deleted.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run --config vitest.narrow.config.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]
  [[ "$output" == *"--config"* ]]

  # `-c` is the same flag spelled short, and `--root` redirects resolution one
  # level further up. Enumerating one and not the others is how this rule lost
  # three times before.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run -c other.config.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run --root packages/other" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]

  # The control: flags that cannot reduce the run are still accepted, or the
  # allow-list would have become a ban on flags. Same shape as the assertions
  # above -- what is under test is the validator, and the sandbox cannot run
  # vitest either way.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run --reporter=dot --coverage" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a nested config under a workspace is not an orphan" {
  # A nested TypeScript project config is ordinary: frontend/e2e/tsconfig.json
  # extending ../tsconfig.json shares its package manager and dependencies with
  # frontend. Requiring a second package.json beside it made a full-mode run
  # exit 20 before reaching the workspace at all. The rule is meant to catch a
  # workspace that *lost* its manifest, and a config with an ancestor workspace
  # has lost nothing.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  mkdir -p "$NODE_SB/ws/e2e"
  printf '{ "extends": "../tsconfig.json" }\n' > "$NODE_SB/ws/e2e/tsconfig.json"
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=full bash ci/checks/node.sh 2>&1"
  # Asserted by the absence of the orphan refusal and by what the run reaches
  # instead, rather than by the lane's exit status. The status was a proxy for
  # "nothing refused it", and a nested config is now a TypeScript project the
  # workspace has to be able to check -- so this fixture is refused downstream,
  # correctly and by a different rule. A proxy assertion that starts answering
  # for a rule it was not written about is how a case passes for the wrong
  # reason, which this suite has had to fix more than once.
  [[ "$output" != *"no package.json beside it"* ]]
  [[ "$output" == *"declares TypeScript configuration but"* ]]
  [[ "$output" == *"e2e/tsconfig.json"* ]]

  # The control, and the case the rule was written for: a config whose walk
  # reaches the root without finding any manifest is still an orphan. Without
  # this, walking upward would have quietly deleted the rule.
  mkdir -p "$NODE_SB/stray"
  printf '{}\n' > "$NODE_SB/stray/tsconfig.json"
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=full bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"stray/tsconfig.json"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a checker named as an argument does not count as running one" {
  # The token scan accepted a tool name wherever it appeared, so `echo vitest`
  # satisfied the rule while running echo and collecting nothing. A name is
  # evidence that a checker runs only where a command starts.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "echo vitest" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"not appear to run a test runner"* ]]

  # Same shape one layer out: a delegation target reached only as an argument.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "echo bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"not appear to run a test runner"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a composition that cannot prove the checker runs is refused" {
  # `true || vitest run` never reaches the runner. `vitest run || true` reaches
  # it and throws the result away, which is worse -- the suite fails and the
  # script still exits 0. Neither can be vouched for.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true || vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"cannot be trusted"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run || true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"cannot be trusted"* ]]
  # And for the right reason. This was rejected before the fix too, by the
  # positional-filter rule, with "'true' selects a subset" -- a diagnosis that
  # describes a filter that is not there and sends the reader looking for one.
  [[ "$output" != *"selects a subset"* ]]

  # The control: `&&` is fine. Either the checker runs, or the thing before it
  # failed and the script fails with it -- no outcome where the suite is
  # silently skipped. Rejecting it would fail ordinary `tsc && vitest run`.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "echo start && bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: an unquoted checker token is not truncated by quote stripping" {
  # The tokens are unquoted before they are compared, because `sh -c 'tsc
  # --noEmit'` arrives with the quote clinging to the first one. Written as a
  # bracket expression that differs by a single backslash -- `[\"\']` against
  # `[\"']` -- the wrong form strips the first *character* of every token, so
  # `tsc` became `sc`, matched nothing, and every TypeScript workspace was told
  # it has no type checker. Silent, and in the direction that blocks correct
  # work rather than admitting bad work, which is why it is pinned here rather
  # than left to the cases that happened to catch it.
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "typecheck": "tsc --noEmit", "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"type checker"* ]]

  # And the case the stripping exists for: a quoted token still matches.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "typecheck": "'"'"'tsc'"'"' --noEmit", "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"type checker"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a clean deletion does not abort the lane" {
  # `[ -e "$_d" ] && printf ...` leaves the test's false status as the status of
  # the loop when the last deleted path is genuinely gone -- which is the
  # ordinary case -- and that propagated through the command substitution, the
  # assignment, and this script's `set -e`. The lane exited raw 1 with no
  # diagnostic, before install, typecheck, test or build had run.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  printf 'x\n' > ws/gone.ts
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  local base
  base="$(git rev-parse HEAD)"
  git rm -q ws/gone.ts >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm "delete it" >/dev/null 2>&1
  ws_seed_fingerprint

  # The premise: the deletion is clean -- the path really is gone from the tree.
  [ ! -e "$NODE_SB/ws/gone.ts" ]

  # Deliberately without CI_GATE_NODE_WORKSPACE. The drift scan lives in the
  # discovery pass, and setting that variable skips discovery entirely -- an
  # earlier version of this case did exactly that and passed against the broken
  # code, which is the whole reason a refutation is run before a case is kept.
  #
  # Refuting this one by swapping ci/checks/node.sh alone does not work and is
  # worth writing down: ws_setup copies the *current* ci/lib/git.sh beside it,
  # and the older node.sh calls a helper that has since been renamed, so the run
  # dies on a missing function and the case passes for the wrong reason. The
  # reproduction that stands behind it swaps both files together, and shows the
  # pre-fix lane exiting raw 1 with 39 bytes of output -- the section header and
  # nothing else.
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' \
    bash ci/checks/node.sh 2>&1"
  # Whatever the verdict, it must be one of the gate's own and must have a
  # diagnostic behind it. Raw 1 with no output is the bug.
  [ "$status" -ne 1 ]
  [ -n "$output" ]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a pipeline hides the checker's status and is refused" {
  # A pipeline reports its *last* command's status, so `tsc --noEmit | cat`
  # prints TS errors and exits 0. The checker is in command position, so the
  # command-position rule returned success without ever looking at the pipe.
  ws_setup
  printf '{}\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "typecheck": "tsc --noEmit | cat", "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"pipes or branches"* ]]
  rm -rf "$NODE_SB"

  # A fresh sandbox for the test script: leaving the tsconfig behind would make
  # the run fail on the missing typecheck script first, and this case would then
  # be asserting on a verdict it did not cause.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run | tee out.log" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"pipes or branches"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: || without spaces is the same operator and is refused" {
  # `case " $cmd " in *" || "*)` only recognised the spaced spelling, so the
  # guard was two keystrokes wide.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run||true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"pipes or branches"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "true||vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a runner the delegated script never reaches does not count" {
  # `unused() { node --test; }` followed by `exit 0` names a runner, in command
  # position, inside a function nobody calls. The line-by-line scan accepted it.
  ws_setup
  printf '#!/usr/bin/env bash\nunused() { node --test; }\nexit 0\n' > "$NODE_SB/ws/scripts/dead.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/dead.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"not appear to run a test runner"* ]]

  # The same one line lower, inside a conditional this reader cannot evaluate.
  printf '#!/usr/bin/env bash\nif [ -n "$NOPE" ]; then\n  vitest run\nfi\n' > "$NODE_SB/ws/scripts/cond.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/cond.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]

  # The control: a wrapper that plainly hands over is still accepted, including
  # via `exec`, which is how a wrapper normally does it.
  printf '#!/usr/bin/env bash\nset -e\nexec ./scripts/vitest run "$@"\n' > "$NODE_SB/ws/scripts/live.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/live.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a checker after a terminating command does not count" {
  # `"test": "exit 0 ; vitest run"` puts the runner in command position after a
  # separator, so every rule was satisfied by a token the shell never reaches.
  # A separator resets *where a command starts*, which is not the same question
  # as whether one runs.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "exit 0 ; vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"not appear to run a test runner"* ]]

  # The control, and the reason this is `break` and not a ban on `exit`: a
  # runner that has already been reached is not undone by exiting afterwards.
  #
  # The control used to be `bash scripts/test.sh ; exit 0`, and that was a
  # mistake -- it is itself the shape this lane must refuse, since the script's
  # failure is discarded by the `exit 0` after it. It passed because reaching a
  # checker ended the scan. `exit $?` makes the same point about separators
  # without throwing the result away.
  # Asserted on the guard's message, not the status: what this control is about
  # is that the scan still resolves the delegation across the separator, and
  # the script's own exit status here depends on how bun's runner spells `$?`.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh ; exit $?" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"not appear to run a test runner"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh ; exit 0" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a checker whose status is overwritten afterwards does not count" {
  # Reaching a checker ended the scan, so anything after it was unread. `tsc
  # --noEmit ; true` reaches the compiler and discards its result -- the shell
  # reports the last command's status -- and a delegated script running the
  # suite and then `true` exits 0 however the suite went. Both passed.
  ws_setup
  # The typecheck guard only runs where there is a tsconfig.json. Without one
  # the script is executed unguarded -- which is how this was confirmed
  # end to end: `tsc` failed to load and the lane still reported "Node lane
  # passed", because `; true` supplied the exit status.
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "typecheck": "tsc --noEmit ; true", "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"appear to run a type checker"* ]]
  rm -f "$NODE_SB/ws/tsconfig.json"

  # `;` binds to the token before it, so `run;` was never seen as a separator
  # at all -- the same bypass the `||` rule had before it stopped requiring
  # spaces around the operator.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run;true" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]

  printf '#!/usr/bin/env bash\n./scripts/vitest run\ntrue\n' > "$NODE_SB/ws/scripts/mask.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/mask.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]

  # The controls. `&&` short-circuits, so the checker's failure survives it;
  # `set -e` leaves on that failure before the trailing line runs; and `exit 1`
  # forces a failure, which cannot become a false pass. The rule is that
  # nothing may turn a failure into a pass -- not that nothing may follow.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run && echo done" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"not appear to run a test runner"* ]]

  printf '#!/usr/bin/env bash\nset -e\n./scripts/vitest run\ntrue\n' > "$NODE_SB/ws/scripts/ok1.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/ok1.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]

  printf '#!/usr/bin/env bash\n./scripts/vitest run\nexit $?\n' > "$NODE_SB/ws/scripts/ok2.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/ok2.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a quoted brace or keyword cannot make a line read as top level" {
  # The reachability rule counted braces and control keywords in raw line text,
  # so both could be spelled inside a string. `echo "}"` closed a function body
  # a line early and `echo "using profile"` closed an `if` block -- `profile`
  # contains `fi` -- and the unreachable runner below each of them then read as
  # top level, passing the lane on a suite that never runs.
  ws_setup

  # The premise: this exact runner line at top level is accepted, so every
  # rejection below is about the line being unreachable and not about the line
  # going unrecognised.
  printf '#!/usr/bin/env bash\n./scripts/vitest run\n' > "$NODE_SB/ws/scripts/x.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/x.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]

  # `fi` as a substring of a word inside a string, closing the block early.
  printf '#!/usr/bin/env bash\nif [ -n "$NOPE" ]; then\n  echo "using profile"\n  ./scripts/vitest run\nfi\n' \
    > "$NODE_SB/ws/scripts/x.sh"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"not appear to run a test runner"* ]]

  # `done` as a substring, closing a loop early.
  printf '#!/usr/bin/env bash\nfor f in a b; do\n  echo "well done"\n  ./scripts/vitest run\ndone\n' \
    > "$NODE_SB/ws/scripts/x.sh"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]

  # A quoted closing brace balancing a function body out early.
  printf '#!/usr/bin/env bash\nunused() {\n  echo "}"\n  ./scripts/vitest run\n}\nexit 0\n' \
    > "$NODE_SB/ws/scripts/x.sh"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]

  # A here-document body is data, not code; the runner named in one is text.
  printf '#!/usr/bin/env bash\ncat > /dev/null <<EOF\n./scripts/vitest run\nEOF\nexit 0\n' \
    > "$NODE_SB/ws/scripts/x.sh"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]

  # The controls, and the reason the rule is token-exact rather than a ban on
  # quotes: ordinary scripts that merely *contain* these spellings still pass.
  printf '#!/usr/bin/env bash\necho "modified files"\necho "abandoned"\n./scripts/vitest run\n' \
    > "$NODE_SB/ws/scripts/x.sh"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]

  printf '#!/usr/bin/env bash\nif [ -z "$SKIP" ]; then\n  echo "}"\nfi\ncat > /dev/null <<EOF\nnothing\nEOF\n./scripts/vitest run\n' \
    > "$NODE_SB/ws/scripts/x.sh"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 0 ]
  rm -rf "$NODE_SB"
}

@test "node lane: node counts as a runner only in test mode" {
  # `"test": "node"` was accepted. Bare node takes its program from stdin, and
  # under the gate stdin is at EOF: it runs an empty program and exits 0, and
  # with no further tokens every rule below the runner check was satisfied by
  # having nothing to inspect. The whole suite left the gate on a one-word
  # manifest edit.
  #
  # Asserted on the guard's own message rather than the exit status: an
  # accepted command goes on to actually run, and the sandbox's stand-in runner
  # is not on bun's path, so status 20 arrives either way.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "node" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"not appear to run a test runner"* ]]

  # The same one layer down, which is where a rule fixed only at the manifest
  # would still be wrong.
  printf '#!/usr/bin/env bash\nnode\n' > "$NODE_SB/ws/scripts/bare.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/bare.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"not appear to run a test runner"* ]]

  # The control, and the other half of the finding: `node --test` is the form
  # that collects anything, and it was being rejected -- `--test` was not on the
  # flag allow-list, so the gate refused the spelling that runs and accepted the
  # one that does not.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "node --test" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"not appear to run a test runner"* ]]
  [[ "$output" != *"narrows its own suite"* ]]

  printf '#!/usr/bin/env bash\nnode --test\n' > "$NODE_SB/ws/scripts/nt.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/nt.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"not appear to run a test runner"* ]]

  # And node's own narrowing flags are still refused, which is what keeps the
  # allow-list an allow-list.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "node --test --test-name-pattern=x" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: --allowOnly is narrowing and is not on the allow-list" {
  # Vitest defaults allowOnly to off under CI, so a committed `it.only` fails
  # the run. `--allowOnly` turns that back on, and an accidental `.only`
  # anywhere in the tree then reduces the suite to one test while the run exits
  # 0 -- narrowing applied to the whole suite by an edit somewhere else.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run --allowOnly" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]

  # The controls: asking for the CI default is not narrowing, and the ordinary
  # command is untouched.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run --no-allowOnly" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a path deleted and re-added in one push is not drift" {
  # The ship-mode scan asked `git log --diff-filter=D` over the whole outgoing
  # range, so a path any commit in it deleted counted -- including one a later
  # commit in the same push put back. A delete-then-re-add pair reported the
  # re-added file as drift and the lane exited 20 before running a check, over a
  # worktree matching HEAD exactly.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  printf 'export const a = 1;\n' > "$NODE_SB/ws/src.ts"
  printf '.ci-gate/\n' > "$NODE_SB/.gitignore"
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  local base
  base="$(git rev-parse HEAD)"
  git rm -q ws/src.ts >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm delete >/dev/null 2>&1
  printf 'export const a = 1;\n' > "$NODE_SB/ws/src.ts"
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm readd >/dev/null 2>&1
  ws_seed_fingerprint

  # The premises: the worktree matches HEAD, and the range really does contain a
  # deletion of that path -- so a commit-by-commit scan has something to find
  # and an endpoint one does not.
  run bash -c "cd '$NODE_SB' && git status --porcelain | wc -l"
  [ "$(echo "$output" | tr -d ' ')" -eq 0 ]
  run bash -c "cd '$NODE_SB' && git log --diff-filter=D --name-only --format= '$base..HEAD' | grep -c 'ws/src.ts'"
  [ "$output" -eq 1 ]

  # Not pinning CI_GATE_NODE_WORKSPACE: the drift scan lives in the discovery
  # pass, which node.sh skips entirely when the workspace is pinned.
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' \
    bash ci/checks/node.sh 2>&1"
  [[ "$output" != *"src.ts"* ]]

  # And with no push base at all, which is a different range shape through the
  # same code: `push_range` returns a bare tip there, so a fix written as an
  # endpoint diff would answer a different question -- and did, dropping this
  # whole scan on every push without a remote base. What decides it is that the
  # pushed tree carries the path, which does not depend on the shape.
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship bash ci/checks/node.sh 2>&1"
  [[ "$output" != *"src.ts"* ]]

  # The control: a push that genuinely removes the file, with an ignored copy
  # left on disk to shadow it, is still caught -- the case this scan is for.
  git rm -q ws/src.ts >/dev/null 2>&1
  printf '.ci-gate/\nws/src.ts\n' > "$NODE_SB/.gitignore"
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm "delete and ignore" >/dev/null 2>&1
  printf 'export const a = 999;\n' > "$NODE_SB/ws/src.ts"
  ws_seed_fingerprint
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' \
    bash ci/checks/node.sh 2>&1"
  [[ "$output" == *"src.ts"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: deleting the whole suite cannot pass by becoming the new HEAD" {
  # The lost-suite guard compared the worktree against HEAD. In ship mode the
  # deletion is already committed, so HEAD carries no tests either, nothing
  # looked missing, and a push that removes every test file and the test script
  # with them exited 0 -- the suite disappearing by becoming the new HEAD.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  cd "$NODE_SB"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  local base
  base="$(git rev-parse HEAD)"
  git rm -q -r ws/tests >/dev/null 2>&1
  printf '%s\n' '{ "name": "w", "private": true, "scripts": {} }' > ws/package.json
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm "remove the suite" >/dev/null 2>&1
  ws_seed_fingerprint

  # The premises: the base carried tests and HEAD carries none, so a comparison
  # against HEAD has nothing to notice.
  run bash -c "cd '$NODE_SB' && git ls-tree -r --name-only '$base' -- ws | grep -c '\.test\.'"
  [ "$output" -ge 1 ]
  run bash -c "cd '$NODE_SB' && git ls-tree -r --name-only HEAD -- ws | grep -c '\.test\.' || true"
  [ "$output" -eq 0 ]

  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_NODE_WORKSPACE=ws bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"lost its entire test suite"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a negated runner cannot satisfy the gate" {
  # `!` is a reserved word in command position and inverts the status of the
  # command it prefixes, so `! ./scripts/vitest run` leaves a failing suite as a
  # zero. The scan read `!` as a command separator, which made it look like an
  # empty command followed by a runner in command position -- every rule
  # satisfied by a token whose result the shell then reversed.
  ws_setup
  printf '#!/usr/bin/env bash\n./scripts/vitest run "$@"\nexit 77\n' > "$NODE_SB/ws/scripts/f77.sh"

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "! ./scripts/vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"negates a checker"* ]]

  # And one layer down, where the same rule has had to be re-applied four times.
  printf '#!/usr/bin/env bash\n! vitest run\n' > "$NODE_SB/ws/scripts/neg.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/neg.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"negates a checker"* ]]

  # A negated *delegation* is the same statement with a wrapper in front of it.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "! bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"negates a checker"* ]]

  # The controls. The negation binds to one command, so a negated *lookup*
  # followed by the runner is ordinary shell and must still pass -- refusing it
  # would be the tightening overshooting into correct work, which this lane has
  # already done twice.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "! command -v nosuchtool && vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"negates a checker"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"negates a checker"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a wrapper prefix does not hide the runner's own flags" {
  # Runner discovery and the checker resolver disagreed about `command`, `time`
  # and `nohup`: the resolver stepped over them and accepted vitest, while
  # discovery recorded the prefix as the runner, left is_test_runner at zero and
  # never applied the argument rules at all. `command vitest run
  # --exclude=tests/a.test.ts` exited 0 with the exclusion uninspected.
  ws_setup

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "command vitest run --exclude=tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "time vitest run --exclude=tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]

  # A positional filter behind a prefix is the same hole with the other rule.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "nohup vitest run tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]

  # The control: the prefix on its own is not a filter.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "command vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an EXIT trap that can replace the runner's status is refused" {
  # An EXIT handler runs after the runner and leaves with its own status if it
  # exits: `trap 'exit 0' EXIT` in front of a failing suite exits 0. The action
  # is quoted, so the quote blanker -- which exists precisely because quoted
  # text is data -- had already removed it, and the scan walked past to the
  # runner and reported a hit.
  ws_setup

  printf '#!/usr/bin/env bash\ntrap %s EXIT\n./scripts/vitest run\nexit 77\n' "'exit 0'" \
    > "$NODE_SB/ws/scripts/trap.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/trap.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"installs an EXIT trap"* ]]

  # Installed inside a block the structure reader refuses to judge, and still
  # installed at run time. Restricting the question to top-level lines would
  # have left this accepted -- the rule right on one spelling and absent on the
  # one beside it.
  printf '#!/usr/bin/env bash\nif [ -n "${CI:-}" ]; then trap %s EXIT; fi\n./scripts/vitest run\n' "'exit 0'" \
    > "$NODE_SB/ws/scripts/trapif.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/trapif.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"installs an EXIT trap"* ]]

  # And in the manifest itself, which runs in a shell like any other script.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "trap \"exit 0\" EXIT ; vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"installs an EXIT trap"* ]]

  # The controls. `trap - EXIT` removes a handler rather than installing one; a
  # handler on other signals cannot replace an exit status; and a trap inside a
  # string is text being printed, not a handler being installed -- the same
  # distinction the blanker draws for a checker named in a string.
  printf '#!/usr/bin/env bash\ntrap - EXIT\n./scripts/vitest run\n' > "$NODE_SB/ws/scripts/treset.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/treset.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"installs an EXIT trap"* ]]

  printf '#!/usr/bin/env bash\ntrap %s INT TERM\n./scripts/vitest run\n' "'echo bye'" \
    > "$NODE_SB/ws/scripts/tint.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/tint.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"installs an EXIT trap"* ]]

  printf '#!/usr/bin/env bash\necho "trap %s EXIT"\n./scripts/vitest run\n' "'exit 0'" \
    > "$NODE_SB/ws/scripts/techo.sh"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/techo.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"installs an EXIT trap"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a delegated typecheck is judged by the compiler's rules" {
  # Every resolved delegation was handed to the *test-runner* validator. A
  # typecheck script delegating to `tsc --noEmit` therefore met an allow-list
  # that has never contained `--noEmit`, and `tsc -p tsconfig.json` was reported
  # as pointing at "individual files" that are its project -- the gate refusing
  # the two most ordinary spellings of a correct typecheck. Worse, that
  # validator read `is_test_runner` out of its caller's scope, and on this path
  # the name does not exist: `set -u` aborted the lane.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/tsc"
  chmod +x "$NODE_SB/ws/scripts/tsc"
  printf '#!/usr/bin/env bash\ntsc -p tsconfig.json --noEmit\n' > "$NODE_SB/ws/scripts/tc.sh"

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "npm run tc", "tc": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]
  [[ "$output" != *"individual files"* ]]
  [[ "$output" != *"unbound variable"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "bash scripts/tc.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]
  [[ "$output" != *"individual files"* ]]
  [[ "$output" != *"unbound variable"* ]]

  # And the compiler's own rules do reach the delegated command, which is the
  # other half: dispatching by tool is not the same as skipping the check.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "npm run tc", "tc": "tsc --noEmit --noCheck" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"non-compiling tsc mode"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: tsc modes that do not typecheck are refused whatever their case" {
  # `--noCheck` is documented as processing the project without full type
  # checking, so `tsc --noEmit --noCheck` walks everything, reports nothing and
  # exits 0 over code that does not compile. The catch-all `-*` arm accepted it.
  #
  # And tsc matches its options case-insensitively, so every mode named by this
  # rule could be reached by changing a letter: `--showconfig` is the same
  # option to the compiler and was a different string to the guard.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/tsc"
  chmod +x "$NODE_SB/ws/scripts/tsc"

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --noEmit --noCheck" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"non-compiling tsc mode"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --noemit --showconfig" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"non-compiling tsc mode"* ]]

  # Watch mode never returns, so the lane is killed by the runner timeout and a
  # manifest edit is reported as broken infrastructure rather than a result --
  # the reason the same flag left the test-runner allow-list.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --noEmit --watch" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"non-compiling tsc mode"* ]]

  # The controls: the two correct spellings still pass, which is what keeps this
  # an enumeration of what cannot check rather than of what may run.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"non-compiling tsc mode"* ]]
  [[ "$output" != *"individual files"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -p tsconfig.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"non-compiling tsc mode"* ]]
  [[ "$output" != *"individual files"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: the build-mode operations that delete or plan are refused" {
  # `tsc --build` takes operations of its own, and two of them never typecheck:
  # `--clean` deletes the outputs of the referenced projects and `--dry` prints
  # what a build would do. Either exits 0 over code that does not compile, so
  # `tsc --build --clean` was a typecheck script that removed build output and
  # reported success. The rule enumerated single-command modes and did not know
  # the build operations existed.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/tsc"
  chmod +x "$NODE_SB/ws/scripts/tsc"

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --build --clean" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"non-compiling tsc mode"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --build --dry" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"non-compiling tsc mode"* ]]

  # The short spelling of the build flag reaches the same operations, and the
  # compiler folds their case like every other option.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -b --clean" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"non-compiling tsc mode"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --build --CLEAN" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"non-compiling tsc mode"* ]]

  # The control: `tsc --build` on its own is an ordinary project build and does
  # typecheck. Refusing it would be the other half of this rule going wrong.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --build" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"non-compiling tsc mode"* ]]
  [[ "$output" != *"individual files"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: listing the files it compiled is not a non-compiling mode" {
  # `--listFiles` and `--listFilesOnly` are one letter and a whole behaviour
  # apart: the first prints the files as part of a normal compile and still
  # reports every error, the second prints them *instead* of compiling. Naming
  # both refused `tsc --listFiles --noEmit`, an ordinary diagnostic spelling of
  # a real typecheck -- this rule's own false positive, and the shape that gets
  # a gate switched off.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/tsc"
  chmod +x "$NODE_SB/ws/scripts/tsc"

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --listFiles --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"non-compiling tsc mode"* ]]
  [[ "$output" != *"individual files"* ]]

  # And the mode that really does replace the compile is still refused, which is
  # what keeps this a correction rather than a relaxation.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --listFilesOnly --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"non-compiling tsc mode"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: the compiler's third spelling of help is refused, and no short flag with it" {
  # typescript registers the option as `{ name: "help", shortName: "?" }`, so
  # `tsc -?` prints the banner and exits 0. The list refused `--help` and `-h`
  # and missed the one spelling that is punctuation -- a one-character manifest
  # edit switched the typecheck lane off while it reported PASS.
  #
  # And the reason the pattern is quoted: an unquoted `?` in a case pattern
  # matches any single character, so `-?` written bare would have refused `-p`,
  # `-b`, `-w` and the rest of the short vocabulary at a stroke. The controls
  # below are that half of the rule.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/tsc"
  chmod +x "$NODE_SB/ws/scripts/tsc"

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -?" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"non-compiling tsc mode"* ]]

  local flag
  for flag in "-p tsconfig.json --noEmit" "-b" "--noEmit -i" "--noEmit -f"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"vitest run\", \"typecheck\": \"tsc ${flag}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"non-compiling tsc mode"* ]] || {
      echo "short flag swallowed by the -? pattern: ${flag}" >&2
      return 1
    }
  done
  rm -rf "$NODE_SB"
}

@test "node lane: a subshell or brace group does not switch off the argument rules" {
  # `_script_names_a_checker` steps over `(`, `)`, `{` and `}` when it looks for
  # a runner in command position, so it said the script runs vitest. Beside it
  # `_command_runner` had no arm for them at all: `(` became the program name,
  # `(` is in no tool list, and `_reject_tool_args_one` -- the only route to the
  # tsc rules, the narrowing rules and the positional-filter rule -- was never
  # called. One character in front of a command switched off every argument rule
  # at once, on both the test and the typecheck path.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/tsc"
  chmod +x "$NODE_SB/ws/scripts/tsc"

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "( vitest run --exclude=tests/x )", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  # No spaces are needed for a subshell, so the word is `(vitest`.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "(vitest run --exclude=tests/x)", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  # A brace group reaches the same place by the other spelling.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "{ vitest run tests/a.test.ts; }", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]

  # And the typecheck path, whose own rule this equally bypassed.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "( tsc --noEmit src/app.ts )" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"individual files"* ]]

  # The controls, and they are the reason the blanking is scoped to command
  # position: a wrapped full-suite run is an ordinary script and must pass, and
  # `{ts,tsx}` and `${VAR}` are arguments rather than groups.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "( vitest run )", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]
  [[ "$output" != *"does not appear to run"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "(cd . && vitest run)", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]
  [[ "$output" != *"does not appear to run"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a word spelled with a backslash escape is refused, not read past" {
  # `\trap` is the trap command to the shell and `rap` to this gate: an escaped
  # character is blanked, and it has to be, since keeping it would let `echo
  # \done` close a block the reader is standing inside. So the word vanished
  # from every rule that reads that mask at once -- the EXIT-trap rule added
  # this round was defeated by one character.
  ws_setup
  cat > "$NODE_SB/ws/scripts/esc.sh" <<'SH'
#!/usr/bin/env bash
\trap 'exit 0' EXIT
./scripts/vitest run
exit 77
SH

  # The premise: the shell really does run that as `trap`, and the handler
  # really does replace the failure.
  run bash -c "cd '$NODE_SB/ws' && bash scripts/esc.sh >/dev/null 2>&1"
  [ "$status" -eq 0 ]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/esc.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"backslash escape"* ]]

  # And in the manifest, where the same escape hides the runner itself.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "\\vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"backslash escape"* ]]

  # The controls, and they matter: a line continuation is a backslash at the end
  # of a line with no character after it to hide, and an escaped space is not a
  # word. Refusing either would break ordinary wrapper scripts.
  cat > "$NODE_SB/ws/scripts/cont.sh" <<'SH'
#!/usr/bin/env bash
./scripts/vitest \
  run
SH
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/cont.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"backslash escape"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/test.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"backslash escape"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an inline -c command is judged by the same argument rules" {
  # `bash -c 'vitest run tests/only.test.ts'` recursed into the predicate and
  # returned its answer, so the command inside the string reached a runner and
  # was accepted with nothing asked about its arguments. Four ways to spell one
  # delegation -- direct, shell script, package script, inline string -- and the
  # rule had been carried to three of them.
  ws_setup

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash -c \"vitest run tests/a.test.ts\"" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash -c \"vitest run --exclude=tests/a.test.ts\"" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]

  # The compiler's rules reach into the string too, by the same dispatcher.
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/tsc"
  chmod +x "$NODE_SB/ws/scripts/tsc"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "bash -c \"tsc --noEmit --noCheck\"" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"non-compiling tsc mode"* ]]

  # The control: an inline command that narrows nothing is still accepted.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "bash -c \"tsc --noEmit\"" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]
  [[ "$output" != *"non-compiling tsc mode"* ]]
  [[ "$output" != *"individual files"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: the argument rules stop at a command boundary" {
  # The rules were applied to the whole string, so `vitest run && echo done` had
  # `echo` read as a vitest argument and refused as a filter -- a script that
  # runs the full suite and whose status is the suite's, since `&&`
  # short-circuits, which is exactly why _reject_untrustworthy_composition
  # allows it. Two rules in one file giving contradictory answers about the same
  # composition.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/tsc"
  chmod +x "$NODE_SB/ws/scripts/tsc"

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run && echo done", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --noEmit && echo ok" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"individual files"* ]]
  [[ "$output" != *"non-compiling tsc mode"* ]]

  # And the other half, which is what stops this being a hole rather than a fix:
  # every command in the string is judged, not just the first. A filter before
  # the separator is still a filter, a second runner after it is still checked,
  # and a separator inside quotes is an argument rather than a boundary.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run --exclude=tests/a.test.ts && echo done", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run && jest --testPathPattern=x", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "echo hi && vitest run --exclude=tests/a.test.ts", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run \"a && b\"", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]

  # An environment assignment in front of the runner is not a filter either, and
  # it used to hide the runner from discovery exactly as `command` did.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "NODE_ENV=test vitest run", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "NODE_ENV=test vitest run --exclude=tests/a.test.ts", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" == *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an ignored file beside a staged one is not staging drift" {
  # The quick-mode scan added the whole ignored list, filtered only by a prune
  # list of directory names. So staging any workspace file at all failed the
  # commit gate over `frontend/.env.local` -- and the remedy it printed, stage
  # it or discard it, means committing a secrets file that git-safety.sh then
  # blocks. Ship mode was fixed by keying on deletions and this branch was not:
  # one rule, two trees.
  ws_setup
  cd "$NODE_SB"
  printf 'ci/\n.ci-gate/\n*.local\nnode_modules/\n' > .gitignore
  printf 'console.log(1)\n' > ws/app.js
  printf 'console.log(9)\n' > ws/doomed.js
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run" } }'
  git init -q -b main . >/dev/null 2>&1
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  ws_seed_fingerprint

  printf 'SECRET=1\n' > ws/.env.local
  printf 'console.log(2)\n' > ws/app.js
  git add ws/app.js >/dev/null 2>&1

  # The premises: the file really is ignored, and it really is the only thing
  # beside the staged change.
  run git check-ignore -q ws/.env.local
  [ "$status" -eq 0 ]
  run bash -c "cd '$NODE_SB' && git diff --cached --name-only"
  [ "$output" = "ws/app.js" ]

  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=quick bash ci/checks/node.sh 2>&1"
  [[ "$output" != *".env.local"* ]]
  [[ "$output" != *"staged but changed again"* ]]

  # And the case the ignored list is there for, which must still fire: a staged
  # deletion shadowed by an ignored file of the same name. The commit removes
  # the path, `git diff HEAD` is silent about it, --exclude-standard hides the
  # replacement, and the lane would run it.
  git rm -q --cached ws/doomed.js >/dev/null 2>&1
  printf 'doomed.js\n' >> .gitignore
  git add .gitignore >/dev/null 2>&1
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=quick bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"staged but changed again"* ]]
  [[ "$output" == *"ws/doomed.js"* ]]
  [[ "$output" != *".env.local"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: arguments handed to a delegated wrapper are not ignored" {
  # The wrapper reader accepts `vitest run "$@"`, and says why: what a caller
  # forwards is not visible from inside the script. It is visible at the call,
  # and the tokens after a delegated target were being dropped -- so
  # `bash scripts/test.sh tests/a.test.ts` over that wrapper collected one file
  # and the lane exited 0. Two halves of one question, one of them unasked.
  ws_setup
  printf '#!/usr/bin/env bash\n./scripts/vitest run "$@"\n' > "$NODE_SB/ws/scripts/fwd.sh"

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/fwd.sh tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"passes an argument into the"* ]]
  [[ "$output" == *"tests/a.test.ts"* ]]

  # A narrowing flag forwarded is the same statement in flag form, and meets
  # the allow-list the runner's own flags meet.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/fwd.sh --exclude=tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  # The controls: forwarding nothing is the ordinary wrapper, and a flag that
  # cannot reduce the run is not a filter.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/fwd.sh" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"passes an argument into the"* ]]
  [[ "$output" != *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/fwd.sh --coverage" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"passes an argument into the"* ]]
  [[ "$output" != *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an EXIT trap whose signal is quoted is still an EXIT trap" {
  # The first version of this rule split the blanked mask on whitespace, and a
  # quoted signal is blanked with everything else -- so `trap 'exit 0' 'EXIT'`,
  # a valid spelling, showed the loop a `trap` and no signal at all and was
  # accepted. The mask is length-preserving, so the arguments can be recovered
  # whole: a position holding a space in the mask *and* in the original is a
  # boundary, and a space inside a quoted span is marked so it is not mistaken
  # for one.
  ws_setup
  _trap_verdict() {
    printf '#!/usr/bin/env bash\n%s\n./scripts/vitest run\nexit 77\n' "$1" \
      > "$NODE_SB/ws/scripts/t.sh"
    ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "bash scripts/t.sh" } }'
    ws_seed_fingerprint
    case "$(ws_run)" in
      *"installs an EXIT trap"*) printf 'refused' ;;
      *) printf 'accepted' ;;
    esac
  }

  # The premise: the shell really does install that handler, and it really does
  # replace the 77 with a zero.
  printf '#!/usr/bin/env bash\ntrap %s EXIT\nexit 77\n' "'exit 0'" > "$NODE_SB/ws/scripts/p.sh"
  run bash -c "cd '$NODE_SB/ws' && bash scripts/p.sh"
  [ "$status" -eq 0 ]

  [ "$(_trap_verdict "trap 'exit 0' EXIT")" = refused ]
  [ "$(_trap_verdict "trap 'exit 0' 'EXIT'")" = refused ]
  [ "$(_trap_verdict 'trap "exit 0" "EXIT"')" = refused ]
  [ "$(_trap_verdict "trap 'exit 0' 0")" = refused ]
  [ "$(_trap_verdict "trap 'exit 0' EX'IT'")" = refused ]
  # A quoted command name is still that command.
  [ "$(_trap_verdict "'trap' 'exit 0' EXIT")" = refused ]

  # The controls. `trap - EXIT` removes a handler rather than installing one,
  # quoted or not; a handler on other signals cannot replace an exit status;
  # and a trap named inside a string is text being printed. That last one is
  # what the quoted-whitespace marking is for -- without it the words inside
  # the string read as separate arguments and `EXIT` was found in the middle
  # of one.
  [ "$(_trap_verdict 'trap - EXIT')" = accepted ]
  [ "$(_trap_verdict "trap - 'EXIT'")" = accepted ]
  [ "$(_trap_verdict "trap 'echo bye' INT TERM")" = accepted ]
  [ "$(_trap_verdict 'echo "trap fake EXIT"')" = accepted ]
  # A separator ends the trap command, and the words after it belong to the next
  # one. Applying that only to the piece in front of the separator, and leaving
  # the state where it was, had `trap 'echo bye' INT; echo EXIT` refused for a
  # signal two words past the end of the trap.
  [ "$(_trap_verdict "trap 'echo bye' INT; echo EXIT")" = accepted ]
  [ "$(_trap_verdict "trap 'echo bye' INT; trap 'exit 0' EXIT")" = refused ]
  rm -rf "$NODE_SB"
}

@test "node lane: the manifest check reads the pushed tree in ship mode" {
  # The index is right for pre-commit, where the commit is being assembled in
  # it, and it is not the tree a push carries: in ship mode the commit already
  # exists and the index can hold anything staged for the next one. A staged
  # deletion of the manifest, with the worktree copy kept and an unrelated HEAD
  # pushed, failed the push over a tree it is not sending -- a mandatory gate
  # blocking correct work.
  ws_setup
  cd "$NODE_SB"
  printf 'ci/\n.ci-gate/\n' > .gitignore
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run" } }'
  git init -q -b main . >/dev/null 2>&1
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  ws_seed_fingerprint
  git rm -q --cached ws/package.json >/dev/null 2>&1

  # The premises: HEAD carries the manifest, the index does not, and the
  # worktree copy is still there for discovery to find.
  run bash -c "cd '$NODE_SB' && git cat-file -e HEAD:ws/package.json"
  [ "$status" -eq 0 ]
  run bash -c "cd '$NODE_SB' && git cat-file -e :ws/package.json"
  [ "$status" -ne 0 ]
  [ -f "$NODE_SB/ws/package.json" ]

  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=ship bash ci/checks/node.sh 2>&1"
  [[ "$output" != *"exists on disk but not in"* ]]

  # And the defect the check exists for is unchanged where the index *is* the
  # commit: staging the deletion and committing nothing means the commit being
  # made carries no manifest, while every check below reads the worktree copy.
  run bash -c "cd '$NODE_SB' && CI_GATE_MODE=quick bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"exists on disk but not in the git index"* ]]
  cd "$REPO_ROOT"
  rm -rf "$NODE_SB"
}

@test "node lane: a newline in a package script is the separator the shell reads" {
  # A package.json script is a JSON string and may hold a real newline, which
  # the shell reads as `;`. `vitest run` then `exit 0` runs the suite and
  # replaces its status -- byte for byte the case the status rule refuses when
  # it is spelled with a semicolon. Nothing saw it: `;` is matched as a token by
  # the reader, and a newline arrives there as ordinary whitespace.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/scripts/tsc"
  chmod +x "$NODE_SB/ws/scripts/tsc"

  # Written through printf so the file carries the JSON escape, which is what a
  # real manifest holds; node turns it into the newline.
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "test": "vitest run\nexit 0", "typecheck": "tsc --noEmit" } }' \
    > "$NODE_SB/ws/package.json"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"does not become the script"* ]]

  # The control: an ordinary multi-line script, where the runner is the last
  # command and nothing after it replaces its status, is still accepted -- so
  # this is a newline read as the separator it is, not a newline refused.
  printf '%s\n' '{ "name": "w", "private": true, "scripts": { "test": "cd .\nvitest run", "typecheck": "tsc --noEmit" } }' \
    > "$NODE_SB/ws/package.json"
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"does not become the script"* ]]
  [[ "$output" != *"not appear to run a test runner"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: --test belongs to the command that runs node" {
  # `_nt` was computed by scanning every word of the whole script string, so the
  # `--test` belonging to `echo` licensed the bare `node` after the separator.
  # At runtime `echo --test` exits 0 and bare `node` reads an empty program from
  # stdin -- already at EOF under the gate -- and exits 0, so the lane reported
  # PASS with no suite behind it. The comment above the loop asserted the scope
  # and the code did not implement it.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "echo --test && node" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"not appear to run a test runner"* ]]

  # The control: `node --test` in one command is still a runner, and so is a
  # `--test` that follows the runner rather than preceding it.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "node --test" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"not appear to run a test runner"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a runner invoked bare runs nothing" {
  # `tsx`, `ts-node` and `deno` with no arguments drop into a REPL, which under
  # this gate reads EOF immediately and exits 0. That is the identical failure
  # the file documents for bare `node` -- "runs an empty program and exits 0" --
  # fixed for one name on the recognised list and absent on the three beside it.
  ws_setup
  local runner
  for runner in tsx ts-node deno; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${runner}\" } }"
    ws_seed_fingerprint
    run ws_run
    [ "$status" -eq 20 ] || { echo "bare ${runner} accepted" >&2; return 1; }
    [[ "$output" == *"not appear to run a test runner"* ]] \
      || { echo "bare ${runner} refused for the wrong reason" >&2; return 1; }
  done

  # The control: pointed at something, they are runners again.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "deno test" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"not appear to run a test runner"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an environment wrapper is not the command it wraps" {
  # `_command_runner` stepped over `command`, `nohup` and `time` and not over
  # `env`, `cross-env` or `timeout`, so the scan classified the wrapper as some
  # other command, cleared expect_cmd and dropped every token after it --
  # `vitest` included. `cross-env NODE_ENV=test vitest run` runs the complete
  # suite and was refused with "does not appear to run a test runner", and
  # cross-env is the portable way to set a variable in a package script on the
  # Windows host this gate supports.
  ws_setup
  local script
  for script in "cross-env NODE_ENV=test vitest run" "env NODE_ENV=test vitest run" \
                "timeout 300 vitest run"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${script}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"not appear to run a test runner"* ]] \
      || { echo "refused as running no runner: ${script}" >&2; return 1; }
    [[ "$output" != *"narrows its own suite"* ]] \
      || { echo "refused as narrowing: ${script}" >&2; return 1; }
  done

  # And the argument rules still reach through the wrapper, which is the other
  # half: stepping over a prefix is not the same as skipping the check.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "cross-env NODE_ENV=test vitest run --exclude=tests/x" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "timeout 300 vitest run --exclude=tests/x" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a runner's own vocabulary is not vitest's" {
  # The narrowing allow-list is vitest's and `node --test`'s, and it was applied
  # to every runner on the recognised list: `jest --ci` and `mocha --recursive`
  # were both refused for selecting a subset, and `--recursive` makes mocha
  # collect more. The positional-filter exemptions had the same shape -- they
  # carried `run`, vitest's and cypress's word, so `playwright test` and `deno
  # test`, the only invocation either tool has, were read as filters. Every
  # runner past vitest and `node --test` was unusable under a gate that names it
  # as recognised.
  ws_setup
  local script
  for script in "jest --ci" "mocha --recursive" "playwright test"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${script}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"narrows its own suite"* ]] \
      || { echo "refused as narrowing: ${script}" >&2; return 1; }
  done

  # The controls, and they are what keeps this an allow-list: a real filter, a
  # flag that stops a suite early, and a flag that redirects which config
  # declares the suite are all still refused -- and the vocabulary is per
  # runner, so vitest does not inherit jest's.
  for script in "jest -t somename" "mocha --bail" "jest --config other.json" \
                "vitest run --ci"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${script}\" } }"
    ws_seed_fingerprint
    run ws_run
    [ "$status" -eq 20 ] || { echo "accepted: ${script}" >&2; return 1; }
    [[ "$output" == *"narrows its own suite"* ]] \
      || { echo "refused for the wrong reason: ${script}" >&2; return 1; }
  done
  rm -rf "$NODE_SB"
}

@test "node lane: a quoted runner name is the same runner to every reader" {
  # `'vitest' run --exclude=tests/a.test.ts` executes vitest -- the shell
  # removes the quotes -- and _script_names_a_checker unquotes before it looks,
  # so the predicate accepted the runner. _command_runner did not: it returned
  # the token with its quotes still attached, matched no tool, and the whole
  # argument family was skipped for that script. Two readers of one token, and
  # only one of them was reading what the shell reads, so the persistent filter
  # went through.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "'"'"'vitest'"'"' run --exclude=tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  # The control: the same quoting without a filter is still an accepted runner,
  # so this is a rule about the arguments and not about the quotes.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "'"'"'vitest'"'"' run" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"narrows its own suite"* ]]
  [[ "$output" != *"not appear to run a test runner"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a quoted spelling of a non-checking tsc mode is still that mode" {
  # `tsc --no'Check'` is `tsc --noCheck` to the shell, which compiles without
  # checking anything. This scan lowercased the raw token to `--no'check'`,
  # matched none of the named modes, and fell through the generic `-*` arm that
  # accepts ordinary flags: the mandatory typecheck reported PASS with the
  # compiler told not to type-check. Quotes are removed wherever they sit in the
  # token, because that is where a bypass would put them.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --no'"'"'Check'"'"'" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"non-compiling tsc mode"* ]]

  # The control: the ordinary spelling of a mode that does check is admitted and
  # handed to the package manager.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"non-compiling tsc mode"* ]]
  [[ "$output" == *"Running script: typecheck"* ]]

  # And the control that says where the unquoting has to happen: the compiler's
  # own name quoted. Stripping below the runner comparison rather than above it
  # would leave `'tsc' --noEmit` falling through to the trailing arm and being
  # refused as a source file -- the same defect one line over from its own fix.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "'"'"'tsc'"'"' --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"individual files"* ]]
  [[ "$output" == *"Running script: typecheck"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: an explicit boolean is the value of the switch before it" {
  # `tsc --noEmit --pretty false` typechecks the whole project: tsc documents
  # `--pretty` as a boolean whose default is true, and passing it `false` only
  # changes how the diagnostics are formatted. The value-taking allow-list names
  # no boolean switch, so `false` reached the trailing arm and was refused as a
  # source file the compiler had been pointed at -- the gate blocking a valid
  # full-project typecheck, which is how a gate gets switched off.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --noEmit --pretty false" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"individual files"* ]]
  [[ "$output" == *"Running script: typecheck"* ]]

  # The control that keeps it narrow: only `true` and `false`, and only directly
  # after a flag. Any other bare word after a switch is still a source file, and
  # naming files is still what makes tsc ignore tsconfig.json.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --noEmit src/x.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"individual files"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: tsc is not pointed at a project this workspace does not have" {
  # The typecheck twin of `vitest --config`, and it sat on the value-taking
  # list -- so the argument was consumed and nothing asked which project it
  # selects. `-p <file>` compiles the project that file describes, and this
  # reader cannot open the file to see what that is, so a path discovery never
  # found is refused rather than assumed harmless.
  #
  # "Discovered" and not "named tsconfig.json", which is what it used to say:
  # widening discovery to tsconfig.*.json made the old spelling a requirement
  # with no way to satisfy it, and that half is asserted in the case below.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -p ../shared/tsconfig.narrow.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"a project this workspace does not have"* ]]
  [[ "$output" == *"tsconfig.narrow.json"* ]]
  # And it says which projects there are, so the refusal is actionable.
  [[ "$output" == *"tsconfig.json"* ]]

  # The joined spelling selects a project the same way and arrives as one
  # token, so it needs the rule written on it too -- otherwise the fix holds for
  # `-p x` and the option beside it walks straight through the generic flag arm.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --project=../shared/tsconfig.narrow.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"a project this workspace does not have"* ]]

  # A config that exists but sits outside the workspace is still outside it: the
  # enumeration is rooted at the workspace, so `../` cannot be reached by it and
  # the file being real does not make it discovered.
  mkdir -p "$NODE_SB/shared"
  printf '{ "compilerOptions": { "strict": true }, "files": ["src/ok.ts"] }\n' \
    > "$NODE_SB/shared/tsconfig.narrow.json"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"a project this workspace does not have"* ]]
  rm -rf "$NODE_SB/shared"

  # The controls: naming the workspace configuration explicitly is the same
  # compilation as omitting it, in either spelling, and stays accepted.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -p tsconfig.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"a project this workspace does not have"* ]]
  [[ "$output" == *"Running script: typecheck"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --project=tsconfig.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"a project this workspace does not have"* ]]
  [[ "$output" == *"Running script: typecheck"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: the only project a workspace has can be named as its project" {
  # A requirement with no way to satisfy it, created by the commit that widened
  # discovery to tsconfig.*.json and reported before anyone hit it.
  #
  # `npm create vite` leaves a workspace whose only configuration is
  # tsconfig.app.json. Discovery now sees it, so a `typecheck` script is
  # *required* -- and the only script that could typecheck that project was
  # refused by the rule above, because the rule read "the project" as the
  # literal name tsconfig.json. `tsc --noEmit` was accepted there, but with no
  # root configuration to read it checks nothing.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.app.json"

  # The premise: this workspace has a project, and it is not named tsconfig.json.
  [ -f "$NODE_SB/ws/tsconfig.app.json" ]
  [ ! -f "$NODE_SB/ws/tsconfig.json" ]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -p tsconfig.app.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"a project this workspace does not have"* ]] \
    || { echo "the workspace's own project was refused" >&2; echo "$output" >&2; return 1; }
  [[ "$output" == *"Running script: typecheck"* ]]

  # The `./` spelling names the same file and is what a hand-written script
  # tends to carry; matching it as a different string would refuse the project
  # for its punctuation.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -p ./tsconfig.app.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"a project this workspace does not have"* ]]
  [[ "$output" == *"Running script: typecheck"* ]]

  # The joined spelling has its own arm, and the comment above it has claimed
  # since it was written that both are "judged by the same test ... rather than
  # by a second rule that could drift away from it". They were two rules, and
  # they drifted the moment one was corrected: this case is what stops the split
  # spelling being fixed on its own again.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --project=tsconfig.app.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"a project"* ]] \
    || { echo "the joined spelling refused the workspace's own project" >&2; echo "$output" >&2; return 1; }
  [[ "$output" == *"Running script: typecheck"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -p=tsconfig.app.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"a project"* ]]
  [[ "$output" == *"Running script: typecheck"* ]]

  # A nested project is discovered too, and is likewise nameable.
  mkdir -p "$NODE_SB/ws/e2e"
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/e2e/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -p e2e/tsconfig.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"a project this workspace does not have"* ]]
  [[ "$output" == *"Running script: typecheck"* ]]

  # The control that keeps the rule from becoming "anything goes": a path that
  # looks like a project but is not one of this workspace's is still refused,
  # in a workspace that now has two real ones.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -p tsconfig.node.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"a project this workspace does not have"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: trap -p inspects the handlers and installs none" {
  # `trap -p EXIT && vitest run` prints the EXIT trap and returns; bash
  # documents `-p` as displaying the trap commands associated with each signal.
  # The state machine read `-p` as the handler and `EXIT` as the signal it was
  # installed for, and refused an ordinary diagnostic script before the suite
  # ran. It is the distinction `-` already makes in the same arm: inspection is
  # not installation.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "trap -p EXIT && vitest run", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"installs an EXIT trap"* ]]
  # The lane validates every script before it runs any of them, so reaching an
  # execution line at all is the evidence this script was admitted -- the
  # refusal below never prints one. Which script runs first is the lane's
  # business and not this case's.
  [[ "$output" == *"Running script:"* ]]

  # The control: a handler that does replace the runner's status is still
  # refused, which is the rule this one sits inside.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "trap true EXIT && vitest run", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"installs an EXIT trap"* ]]
  rm -rf "$NODE_SB"
}

@test "workspaces: an enumeration that could not run is not an empty repository" {
  # The producer ran inside process substitution, so `find` failing on an
  # unreadable subtree -- or `sort` failing to allocate -- delivered partial or
  # empty output and the loop reported its own success. This repository has no
  # root manifest, so an empty list reads as "no package.json found" and the
  # node lane passes having run nothing: one unreadable directory silently
  # removing every frontend check. "Could not look" is not "found nothing",
  # which is the rule config_sources and the git-safety path collector already
  # follow.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/bin" "$sb/frontend"
  printf '{ "name": "f", "private": true }\n' > "$sb/frontend/package.json"
  printf '#!/usr/bin/env bash\nexit 71\n' > "$sb/bin/find"
  chmod +x "$sb/bin/find"

  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' \
    && export PATH='$sb/bin:'\"\$PATH\" \
    && ci::common::node_workspaces package.json"
  [ "$status" -ne 0 ]
  [[ "$output" == *"Cannot enumerate"* ]]
  [[ "$output" != *"frontend"* ]]

  # The control: with a working enumeration the same tree answers normally, so
  # this is a rule about the producer's status and not about the layout.
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' \
    && ci::common::node_workspaces package.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"frontend"* ]]
  rm -rf "$sb"
}

# --- the `timeout` wrapper's own grammar --------------------------------------

@test "node lane: a timeout wrapper's options belong to timeout" {
  # `timeout [OPTION] DURATION COMMAND [ARG]...` is the documented grammar, and
  # every reader here knew only the two-word shape: the token after the wrapper
  # was taken as the duration if it looked numeric, and the state was cleared
  # either way. So `timeout --foreground 300 vitest run` left `300` in command
  # position, the lane reported that the script runs no test runner, and an
  # ordinary bounded full suite was refused. `-s SIGKILL` and `-k 10` bring a
  # value of their own, which is not the duration either.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/node_modules/.bin/timeout"
  chmod +x "$NODE_SB/ws/node_modules/.bin/timeout"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/node_modules/.bin/tsc"
  chmod +x "$NODE_SB/ws/node_modules/.bin/tsc"

  local spelling
  for spelling in \
    "timeout 300 vitest run" \
    "timeout --foreground 300 vitest run" \
    "timeout -s SIGKILL 300 vitest run" \
    "timeout --signal=SIGKILL 300 vitest run" \
    "timeout -k 10 300 vitest run" \
    "timeout --kill-after=10 300 vitest run" \
    "timeout --preserve-status 5m vitest run"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${spelling}\", \"typecheck\": \"tsc --noEmit\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"not appear to run a test runner"* ]] \
      || { echo "refused as no-runner: $spelling" >&2; return 1; }
    [[ "$output" != *"narrows its own suite"* ]] \
      || { echo "refused as narrowing: $spelling" >&2; return 1; }
    [[ "$output" == *"Running script:"* ]] \
      || { echo "never reached execution: $spelling" >&2; return 1; }
  done

  # The controls: the wrapper does not launder what it wraps. A filter behind it
  # is still a filter, and a positional behind it is still a positional.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "timeout --foreground 300 vitest run --exclude=tests/a.test.ts", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "timeout 300 vitest run tests/a.test.ts", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  rm -rf "$NODE_SB"
}

@test "node lane: the typecheck scan reads the same timeout wrapper" {
  # The test lane carried the wrapper rule in four places and the typecheck scan
  # beside it in none, so `timeout 300 tsc --noEmit` was refused for pointing
  # the compiler at a file named `300` -- and a `--noCheck` behind the wrapper
  # was never reached, because the wrong refusal came first.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/node_modules/.bin/timeout"
  chmod +x "$NODE_SB/ws/node_modules/.bin/timeout"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/node_modules/.bin/tsc"
  chmod +x "$NODE_SB/ws/node_modules/.bin/tsc"

  local spelling
  for spelling in \
    "timeout 300 tsc --noEmit" \
    "timeout --foreground 300 tsc --noEmit" \
    "timeout -s SIGKILL 300 tsc --noEmit"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"vitest run\", \"typecheck\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"individual files"* ]] \
      || { echo "refused as naming files: $spelling" >&2; return 1; }
    [[ "$output" == *"Running script:"* ]] \
      || { echo "never reached execution: $spelling" >&2; return 1; }
  done

  # The controls: a real source file behind the wrapper is still a source file,
  # and a non-compiling mode behind it is now reached instead of being hidden
  # behind the wrong refusal.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "timeout 300 tsc --noEmit src/x.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"individual files"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "timeout --foreground 300 tsc --noCheck" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"non-compiling tsc mode"* ]]
  rm -rf "$NODE_SB"
}

# --- which runner owns the suite ----------------------------------------------
#
# These drive ci/checks/tests.sh and ci/checks/typecheck.sh against a synthetic
# workspace, with CI_GATE_CHECK_ID scoping the run to the JS lane so the other
# languages' tools are not required.

lane_setup() {
  LANE_SB="$(mktemp -d)"
  mkdir -p "$LANE_SB/ci/checks" "$LANE_SB/ci/lib" "$LANE_SB/ci/config" \
           "$LANE_SB/ws/node_modules/.bin" "$LANE_SB/ws/tests"
  cp "$REPO_ROOT/ci/checks/tests.sh" "$REPO_ROOT/ci/checks/typecheck.sh" "$LANE_SB/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" \
     "$REPO_ROOT/ci/lib/junit.sh" "$LANE_SB/ci/lib/"
  printf 'it("x", () => {});\n' > "$LANE_SB/ws/tests/a.test.ts"
}

lane_runner() {
  # A stand-in that says which binary was invoked, which is the whole question.
  printf '#!/usr/bin/env bash\necho "INVOKED=%s"\nexit 0\n' "$2" \
    > "$LANE_SB/ws/node_modules/.bin/$1"
  chmod +x "$LANE_SB/ws/node_modules/.bin/$1"
}

lane_manifest() {
  printf '%s\n' "$1" > "$LANE_SB/ws/package.json"
}

lane_run_tests() {
  ( cd "$LANE_SB" && CI_GATE_CHECK_ID=tests-js bash ci/checks/tests.sh 2>&1 )
}

lane_run_typecheck() {
  ( cd "$LANE_SB" && CI_GATE_CHECK_ID=typecheck-js bash ci/checks/typecheck.sh 2>&1 )
}

@test "tests lane: the runner is the one the workspace declares" {
  # Presence and a fixed order decided this, so a workspace that declares Jest
  # and also has Vitest installed -- a migration, or a transitive dependency --
  # ran Vitest. Vitest collects nothing it recognises in a Jest suite, exits 0,
  # and tests-js reports PASS while every Jest test stays uncollected.
  lane_setup
  lane_runner vitest VITEST
  lane_runner jest JEST
  lane_manifest '{ "name": "w", "private": true, "scripts": { "test": "jest --ci" } }'
  run lane_run_tests
  [[ "$output" == *"INVOKED=JEST"* ]]
  [[ "$output" != *"INVOKED=VITEST"* ]]

  # The other direction, so this is about the declaration and not about a new
  # fixed order.
  lane_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run" } }'
  run lane_run_tests
  [[ "$output" == *"INVOKED=VITEST"* ]]
  [[ "$output" != *"INVOKED=JEST"* ]]
  rm -rf "$LANE_SB"
}

@test "tests lane: two runners and no declaration is refused, not ordered" {
  # The question this lane cannot answer. Running either one and reporting PASS
  # is the failure the case above describes, so it says so instead -- as
  # infrastructure, because no test failed.
  lane_setup
  lane_runner vitest VITEST
  lane_runner jest JEST
  lane_manifest '{ "name": "w", "private": true, "scripts": { "test": "npm run inner" } }'
  run lane_run_tests
  [ "$status" -eq 30 ]
  [[ "$output" == *"names neither"* ]]
  [[ "$output" != *"INVOKED="* ]]

  # The control: one runner installed needs no declaration to be unambiguous.
  rm -f "$LANE_SB/ws/node_modules/.bin/jest"
  run lane_run_tests
  [[ "$output" == *"INVOKED=VITEST"* ]]
  rm -rf "$LANE_SB"
}

@test "tests lane: a declared runner that is absent is not a licence for the other" {
  # Substituting the installed runner for the declared one is the same wrong
  # suite by another route, so a missing declared runner is the broken install
  # the no-runner branch already reports.
  lane_setup
  lane_runner vitest VITEST
  lane_manifest '{ "name": "w", "private": true, "scripts": { "test": "jest --ci" } }'
  run lane_run_tests
  [ "$status" -eq 30 ]
  [[ "$output" != *"INVOKED=VITEST"* ]]
  [[ "$output" == *"No workspace-local JS test runner"* ]]
  rm -rf "$LANE_SB"
}

@test "typecheck lane: a nested tsconfig is ordinary TypeScript" {
  # Discovery asked node_workspaces about tsconfig.json, which applies the
  # package-manager ambiguity rule -- a statement about lockfiles. A normal
  # `ws/e2e/tsconfig.json` extending its parent became "a workspace nested under
  # a workspace", the helper returned 1, and under this script's `set -e` the
  # substitution took the whole lane down: raw exit 1, outside the 0/10/20/30
  # contract, with the Python, Go and Rust typechecks never reached.
  lane_setup
  mkdir -p "$LANE_SB/ws/e2e" "$LANE_SB/ws/src"
  printf '{ "compilerOptions": { "strict": true }, "include": ["src"] }\n' > "$LANE_SB/ws/tsconfig.json"
  printf '{ "extends": "../tsconfig.json", "include": ["."] }\n' > "$LANE_SB/ws/e2e/tsconfig.json"
  printf 'export const ok = true;\n' > "$LANE_SB/ws/src/app.ts"
  lane_runner tsc TSC
  lane_manifest '{ "name": "w", "private": true }'
  run lane_run_typecheck
  [ "$status" -eq 0 ]
  [[ "$output" == *"INVOKED=TSC"* ]]
  [[ "$output" != *"nested under"* ]]
  rm -rf "$LANE_SB"
}

@test "typecheck lane: a package root with no TypeScript project says so" {
  # The counterpart of discovering package roots: a JavaScript-only package has
  # no project to compile, which is a fact about the workspace rather than a
  # compiler that could not be found -- and the two must not be reported as
  # each other.
  lane_setup
  lane_runner tsc TSC
  lane_manifest '{ "name": "w", "private": true }'
  run lane_run_typecheck
  [ "$status" -eq 0 ]
  [[ "$output" == *"no tsconfig.json in ws"* ]]
  [[ "$output" != *"INVOKED=TSC"* ]]

  # And with a project present, the same workspace is compiled.
  printf '{ "compilerOptions": { "strict": true } }\n' > "$LANE_SB/ws/tsconfig.json"
  run lane_run_typecheck
  [ "$status" -eq 0 ]
  [[ "$output" == *"INVOKED=TSC"* ]]
  rm -rf "$LANE_SB"
}

@test "lanes: a workspace enumeration that could not run is infrastructure" {
  # The producer's failure reached these two lanes through a command
  # substitution under `set -e`, which aborts the script where it stands -- raw
  # exit 1, no result line, and every language after JavaScript unrun. The
  # status is read and reported now, which is the same rule the enumeration
  # itself follows: could not look is not found nothing.
  lane_setup
  mkdir -p "$LANE_SB/bin"
  printf '#!/usr/bin/env bash\nexit 71\n' > "$LANE_SB/bin/find"
  chmod +x "$LANE_SB/bin/find"
  lane_runner vitest VITEST
  lane_runner tsc TSC
  lane_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run" } }'
  printf '{ "compilerOptions": { "strict": true } }\n' > "$LANE_SB/ws/tsconfig.json"

  run bash -c "cd '$LANE_SB' && PATH='$LANE_SB/bin:'\"\$PATH\" CI_GATE_CHECK_ID=tests-js bash ci/checks/tests.sh 2>&1"
  [ "$status" -eq 30 ]
  [[ "$output" == *"Could not enumerate JavaScript workspaces"* ]]
  [[ "$output" == *"Tests result:"* ]]

  run bash -c "cd '$LANE_SB' && PATH='$LANE_SB/bin:'\"\$PATH\" CI_GATE_CHECK_ID=typecheck-js bash ci/checks/typecheck.sh 2>&1"
  [ "$status" -eq 30 ]
  [[ "$output" == *"Could not enumerate JavaScript workspaces"* ]]
  [[ "$output" == *"Typecheck result:"* ]]
  rm -rf "$LANE_SB"
}

@test "node lane: an env prefix's options belong to env" {
  # `env [OPTION]... [NAME=VALUE]... [COMMAND [ARG]...]`, from `env --help`, and
  # `-u NAME` / `-C DIR` / `-S STRING` take a value as a separate word. The
  # wrapper was skipped by name with nothing reading the grammar after it, so
  # `env -u NODE_ENV vitest run` selected `NODE_ENV` as the program -- it is
  # neither a flag nor an assignment -- and the lane refused a full-suite script
  # for running no test runner. The same six readers the `timeout` wrapper
  # needed: two that discover the checker and three that judge its arguments,
  # plus the preamble that drops the first token before the loop sees it.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  local b
  for b in tsc env cross-env; do
    printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/node_modules/.bin/$b"
    chmod +x "$NODE_SB/ws/node_modules/.bin/$b"
  done

  local spelling
  for spelling in \
    "env vitest run" \
    "env NODE_ENV=test vitest run" \
    "env -u NODE_ENV vitest run" \
    "env --unset=NODE_ENV vitest run" \
    "env -i NODE_ENV=test vitest run" \
    "env -C . vitest run" \
    "cross-env -u NODE_ENV vitest run"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${spelling}\", \"typecheck\": \"tsc --noEmit\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"not appear to run a test runner"* ]] \
      || { echo "refused as no-runner: $spelling" >&2; return 1; }
    [[ "$output" != *"narrows its own suite"* ]] \
      || { echo "refused as narrowing: $spelling" >&2; return 1; }
    [[ "$output" == *"Running script:"* ]] \
      || { echo "never reached execution: $spelling" >&2; return 1; }
  done

  # The controls: the prefix does not launder what follows it. A filter is still
  # a filter, and a command that is not a runner is still not one.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "env -u NODE_ENV vitest run --exclude=tests/a.test.ts", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "env -u NODE_ENV echo hi", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"not appear to run a test runner"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: the typecheck scan reads the same env prefix" {
  # `env` was on no list in the compiler's argument scan at all, so
  # `env NODE_ENV=test tsc --noEmit` was refused for pointing tsc at a file
  # named `env`, and a non-checking mode behind the prefix was never reached
  # because the wrong refusal came first.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  local b
  for b in tsc env; do
    printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/node_modules/.bin/$b"
    chmod +x "$NODE_SB/ws/node_modules/.bin/$b"
  done

  local spelling
  for spelling in \
    "env NODE_ENV=test tsc --noEmit" \
    "env -u NODE_ENV tsc --noEmit"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"vitest run\", \"typecheck\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"individual files"* ]] \
      || { echo "refused as naming files: $spelling" >&2; return 1; }
    [[ "$output" == *"Running script:"* ]] \
      || { echo "never reached execution: $spelling" >&2; return 1; }
  done

  # The control: the mode behind the prefix is now reached instead of hidden.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "env -u NODE_ENV tsc --noCheck" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"non-compiling tsc mode"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: the dependency fingerprint covers the runtime it installed under" {
  # The lockfile and the manifest say what was asked for; they do not say what
  # was built. Native addons, optional packages and install-script output are
  # compiled against the Node ABI and for a platform and architecture, so moving
  # between two releases that both satisfy a broad `engines.node` range left
  # node_modules looking current -- the install was skipped and the lane
  # validated a tree a clean install would not produce.
  ws_setup
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run" } }'
  ws_seed_fingerprint

  # The premise: with the runtime it was seeded under, the install is skipped.
  run ws_run
  [[ "$output" == *"Skipping install"* ]]

  # And under a different runtime it is not. Written directly rather than by
  # running a second Node: what the rule asserts is that the recorded runtime is
  # part of the key, and a fixture that cannot vary it asserts nothing.
  ( cd "$NODE_SB/ws" \
    && . "$REPO_ROOT/ci/lib/common.sh" \
    && printf '%s %s %s\n' \
      "$(ci::common::hash_file bun.lock)" \
      "$(ci::common::hash_file package.json)" \
      "v0.0.0-otherplatform-otherarch" \
      > "$NODE_SB/.ci-gate/node_modules-ws.hash" )
  run ws_run
  [[ "$output" != *"Skipping install"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a nested TypeScript project still needs a typecheck script" {
  # The root-file test read "does this workspace declare TypeScript" as "is
  # there a tsconfig.json beside package.json", so a package whose only project
  # is `e2e/tsconfig.json` -- an ordinary shape -- answered no, required no
  # typecheck script, and reported PASS with that project's type errors never
  # looked for.
  ws_setup
  mkdir -p "$NODE_SB/ws/e2e"
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/e2e/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"declares TypeScript configuration but"* ]]
  [[ "$output" == *"e2e/tsconfig.json"* ]]

  # The control: a config belonging to a dependency is not the workspace's.
  rm -rf "$NODE_SB/ws/e2e"
  mkdir -p "$NODE_SB/ws/node_modules/dep"
  printf '{ }\n' > "$NODE_SB/ws/node_modules/dep/tsconfig.json"
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"declares TypeScript configuration but"* ]]
  rm -rf "$NODE_SB"
}

@test "typecheck lane: a nested project is compiled as its own project" {
  # `tsc --noEmit` at the root compiles what the root config includes, and a
  # config the root neither includes nor references is exactly the one whose
  # errors nobody would see. Compiling a referenced project twice reports the
  # same errors twice; not compiling an unreferenced one reports none at all,
  # and only one of those is a wrong answer.
  lane_setup
  mkdir -p "$LANE_SB/ws/e2e"
  lane_runner tsc TSC
  # The stand-in echoes its arguments, which is how the case can see which
  # projects were compiled rather than only that the compiler ran.
  printf '#!/usr/bin/env bash\necho "INVOKED=TSC $*"\nexit 0\n' \
    > "$LANE_SB/ws/node_modules/.bin/tsc"
  chmod +x "$LANE_SB/ws/node_modules/.bin/tsc"
  lane_manifest '{ "name": "w", "private": true }'
  printf '{ "compilerOptions": { "strict": true } }\n' > "$LANE_SB/ws/e2e/tsconfig.json"

  run lane_run_typecheck
  [ "$status" -eq 0 ]
  [[ "$output" == *"-p e2e/tsconfig.json --noEmit"* ]]

  # With a root project too, both are compiled and the root is not compiled
  # twice under its own name.
  printf '{ "compilerOptions": { "strict": true } }\n' > "$LANE_SB/ws/tsconfig.json"
  run lane_run_typecheck
  [ "$status" -eq 0 ]
  [[ "$output" == *"INVOKED=TSC --noEmit"* ]]
  [[ "$output" == *"-p e2e/tsconfig.json --noEmit"* ]]
  [[ "$output" != *"-p tsconfig.json --noEmit"* ]]

  # And a failing nested project fails the lane, so this is coverage and not
  # decoration.
  printf '#!/usr/bin/env bash\ncase "$*" in *e2e*) exit 2 ;; esac\nexit 0\n' \
    > "$LANE_SB/ws/node_modules/.bin/tsc"
  chmod +x "$LANE_SB/ws/node_modules/.bin/tsc"
  run lane_run_typecheck
  [ "$status" -eq 20 ]
  rm -rf "$LANE_SB"
}

# --- self-found: rules that existed in one reader and not in its sibling ------
#
# These came out of a sweep for the shape the reported findings kept having,
# rather than from a review thread. Each names the reader that had the rule and
# the one that did not.

ws_tools() {
  local _t
  for _t in "$@"; do
    printf '#!/usr/bin/env bash\nexit 0\n' > "$NODE_SB/ws/node_modules/.bin/$_t"
    chmod +x "$NODE_SB/ws/node_modules/.bin/$_t"
  done
}

@test "node lane: a tool reached through npx is judged by its own rules" {
  # _reject_tool_args_one picks the argument rules by the tool actually
  # resolved, and its own comment says why: falling through hands a type checker
  # the test-runner allow-list and refuses every ordinary invocation. The
  # forwarded path -- `npx tsc --noEmit`, `pnpm dlx vitest run`, where the
  # target IS the executable -- never reached it. _reject_forwarded_args called
  # _reject_narrowing_flags bare, with no runner, so vitest's vocabulary was
  # applied to everything: `npx tsc --noEmit` was refused for narrowing a suite
  # that does not exist, `npx jest --ci` and `npx mocha --recursive` for flags
  # the runner-keyed list names as harmless, and `npx playwright test` for the
  # only invocation playwright has.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  ws_tools tsc vue-tsc svelte-check npx jest mocha ava tap playwright deno

  # Every one of these passes when written directly, so it must pass through
  # npx: the spelling is not the question the rules are about.
  local spelling
  for spelling in \
    "npx tsc --noEmit" \
    "npx vue-tsc --noEmit" \
    "npx svelte-check --threshold error"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"vitest run\", \"typecheck\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"narrows its own suite"* ]] \
      || { echo "refused as narrowing: $spelling" >&2; return 1; }
    [[ "$output" == *"Running script:"* ]] \
      || { echo "never reached execution: $spelling" >&2; return 1; }
  done

  for spelling in \
    "npx jest --ci" \
    "npx mocha --recursive" \
    "npx playwright test" \
    "npx deno test" \
    "npx vitest run --reporter json"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${spelling}\", \"typecheck\": \"tsc --noEmit\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"narrows its own suite"* ]] \
      || { echo "refused as narrowing: $spelling" >&2; return 1; }
    [[ "$output" != *"passes an argument into the"* ]] \
      || { echo "refused as forwarded: $spelling" >&2; return 1; }
    [[ "$output" == *"Running script:"* ]] \
      || { echo "never reached execution: $spelling" >&2; return 1; }
  done

  # The controls, and the point of the whole rule: npx does not launder a
  # filter. Both spellings of a genuine narrowing are still refused.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "npx vitest run tests/a.test.ts", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "npx vitest run --exclude=tests/a.test.ts", "typecheck": "tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: tsc reads one dash the same way it reads two" {
  # TypeScript's command line strips the leading dashes before it looks an
  # option up, so `-noCheck` is `--noCheck` to the compiler. The non-compiling
  # list enumerated the two-dash spelling only, so the one-dash spelling matched
  # no arm and fell through the generic `-*` case that accepts ordinary flags:
  # the typecheck lane reported PASS with the compiler told not to type check,
  # reachable by deleting one character. A deny-list that loses to a spelling is
  # the shape this file keeps having to invert.
  ws_setup
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"
  ws_tools tsc

  local spelling
  for spelling in "tsc --noEmit -noCheck" "tsc --noEmit -showConfig" "tsc --noEmit -watch"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"vitest run\", \"typecheck\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [ "$status" -eq 20 ] || { echo "accepted: $spelling" >&2; return 1; }
    [[ "$output" == *"non-compiling tsc mode"* ]] \
      || { echo "refused for the wrong reason: $spelling" >&2; return 1; }
  done

  # The controls: a one-dash option that is not on the list is still an ordinary
  # flag, and the single-character flags keep their one-dash spelling -- turning
  # those into `--p` would take them out of the list they are already in.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"non-compiling tsc mode"* ]]
  [[ "$output" == *"Running script:"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -p tsconfig.json --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"non-compiling tsc mode"* ]]
  [[ "$output" != *"a project this workspace does not have"* ]]
  [[ "$output" == *"Running script:"* ]]

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc -w" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"non-compiling tsc mode"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a runner flag's value is not a filter" {
  # The flag allow-list is keyed by runner and says `mocha --timeout` cannot
  # reduce a run -- by name. The positional reader beside it carried vitest's
  # value-taking list and only that, so `5000` was refused as a test filter.
  # `mocha --require ts-node/register` is the standard mocha TypeScript setup
  # and went the same way. Two readers that had to agree about the same flag,
  # and did not.
  ws_setup
  ws_tools mocha ava tap playwright

  local spelling
  for spelling in \
    "mocha --timeout 5000" \
    "mocha --require ts-node/register" \
    "mocha --jobs 4" \
    "ava --concurrency 4" \
    "tap --jobs 4" \
    "playwright --workers 2"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"narrows its own suite"* ]] \
      || { echo "refused as narrowing: $spelling" >&2; return 1; }
    # And positively: the script was reached and run. Asserting only the absence
    # of a message is satisfied by the lane dying earlier for some unrelated
    # reason, which asserts nothing about the rule under test.
    [[ "$output" == *"Running script:"* ]] \
      || { echo "never reached execution: $spelling" >&2; return 1; }
  done

  # The control: a bare word that is NOT the value of a value-taking flag is
  # still a filter, so this exempts values and not positionals.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "mocha --recursive tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]
  rm -rf "$NODE_SB"
}

@test "node lane: a wrapper is the same wrapper quoted or spelled by path" {
  # `'timeout' 300 vitest run` and `/usr/bin/env NODE_ENV=test vitest run` are
  # ordinary scripts. Three readers unquoted before their wrapper arms and two
  # did not, and `timeout` was matched by path in one arm while `env` beside it
  # was not -- so the duration was read as a filter in the first case and the
  # interpreter path as one in the second.
  ws_setup
  ws_tools timeout env cross-env

  # The quoted spellings are written with plain single quotes: this is a bash
  # string, so `'timeout'` reaches package.json as `'timeout'` and the shell
  # that finally runs the script strips the quotes -- which is the whole point,
  # since the readers under test see the quoted form and the runtime does not.
  local spelling
  for spelling in \
    "'timeout' 300 vitest run" \
    "'env' NODE_ENV=test vitest run" \
    "/usr/bin/env NODE_ENV=test vitest run" \
    "timeout 300 /usr/bin/env NODE_ENV=test vitest run"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"narrows its own suite"* ]] \
      || { echo "refused as narrowing: $spelling" >&2; return 1; }
    [[ "$output" != *"not appear to run a test runner"* ]] \
      || { echo "refused as no-runner: $spelling" >&2; return 1; }
    [[ "$output" == *"Running script:"* ]] \
      || { echo "never reached execution: $spelling" >&2; return 1; }
  done

  # The control: the wrapper does not launder what it wraps, whichever way it
  # is spelled.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "/usr/bin/env NODE_ENV=test vitest run tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  rm -rf "$NODE_SB"
}

@test "node lane: a package manager's own options are not the command" {
  # `pnpm --filter web vitest run` and `yarn --cwd packages/web vitest run` put
  # a bare word after a flag, and every reader took that word as the program --
  # the third wrapper grammar with the same fault, after timeout and env. The
  # standard monorepo invocation was refused with "does not appear to run a test
  # runner" while the runner sat in the string.
  ws_setup
  ws_tools pnpm yarn npm bun npx

  local spelling
  for spelling in \
    "pnpm -F web vitest run" \
    "pnpm --filter web vitest run" \
    "yarn --cwd . vitest run" \
    "npm --prefix . vitest run"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"not appear to run a test runner"* ]] \
      || { echo "refused as no-runner: $spelling" >&2; return 1; }
    [[ "$output" != *"narrows its own suite"* ]] \
      || { echo "refused as narrowing: $spelling" >&2; return 1; }
    [[ "$output" == *"Running script:"* ]] \
      || { echo "never reached execution: $spelling" >&2; return 1; }
  done

  # The controls. A filter behind the prefix is still a filter -- the manager's
  # options are stepped over, not everything after them.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "pnpm -F web vitest run tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]

  # And delegating to another package's script is still something this gate
  # cannot follow, which is the honest answer rather than a guess.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "pnpm --filter web test" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"not appear to run a test runner"* ]]

  # The control the first draft of this fix failed. To most readers here a
  # manager is a prefix to step over, and putting the grammar in that same arm
  # everywhere stepped over `npm` in the one reader that treats it as the
  # delegating runner: `npm run inner` was left with no runner, no target and no
  # delegation to follow. The grammar belongs where that reader picks its
  # delegation target -- which is the token it was getting wrong anyway.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "npm run inner", "inner": "vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"not appear to run a test runner"* ]]
  [[ "$output" == *"Running script:"* ]]

  # And both halves at once: the manager's option takes a word, and the script
  # named after it is still the delegation target.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "npm --prefix . run inner", "inner": "vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"not appear to run a test runner"* ]]
  [[ "$output" == *"Running script:"* ]]
  rm -rf "$NODE_SB"
}

@test "workspaces: a root manifest with unreadable children is not a single-package repo" {
  # The cure above was applied to one branch. The branch taken when a root
  # manifest *does* exist still ran its producer inside process substitution, so
  # a find that fails delivers nothing, child_count stays 0, the
  # root-plus-nested ambiguity is never reported, and the function returns "."
  # and success.
  #
  # That is the fail-open direction of the same defect: node.sh, tests.sh and
  # typecheck.sh each treat a non-zero return as FAIL_INFRA on the stated
  # grounds that a lane cannot report on workspaces it could not determine.
  # Handed "." and a success they install, typecheck, test and build the root
  # alone and exit 0 -- which is precisely the harm the ambiguity report exists
  # to prevent.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/bin" "$sb/packages/app"
  printf '{ "name": "root", "private": true }\n' > "$sb/package.json"
  printf '{ "name": "app", "private": true }\n' > "$sb/packages/app/package.json"
  printf '#!/usr/bin/env bash\nexit 71\n' > "$sb/bin/find"
  chmod +x "$sb/bin/find"

  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' \
    && export PATH='$sb/bin:'\"\$PATH\" \
    && ci::common::node_workspaces package.json"
  [ "$status" -ne 0 ]
  [[ "$output" == *"Cannot enumerate"* ]]
  # And specifically not the answer that reads as "a single-package repo".
  [[ "$output" != "." ]]

  # The control: with a working find the same tree reports the ambiguity, so
  # this is about the producer's status and not about the layout.
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' \
    && ci::common::node_workspaces package.json"
  [ "$status" -ne 0 ]
  [[ "$output" == *"coexists with 1 nested one(s)"* ]]

  # And a genuine single-package repo still answers ".", which is what the
  # branch is for.
  rm -rf "$sb/packages"
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' \
    && ci::common::node_workspaces package.json"
  [ "$status" -eq 0 ]
  [[ "$output" == "." ]]
  rm -rf "$sb"
}

@test "node lane: a project is a project whatever its config is called" {
  # `tsconfig.app.json` and `tsconfig.node.json` are what `npm create vite`
  # produces, and discovery matched `tsconfig.json` and `jsconfig.json` only. A
  # workspace whose only project is one of those answered "no TypeScript here":
  # no typecheck script was required of it and the standalone typecheck lane
  # skipped it for the same reason, so a bundler build passed with nothing
  # having type checked anything.
  #
  # ci/lib/changeset.sh has classified `tsconfig.*.json` as TypeScript
  # configuration since it was written, so the scheduler already knew about a
  # file neither of the lanes it schedules could see.
  ws_setup
  ws_tools tsc

  local cfg
  for cfg in tsconfig.app.json tsconfig.node.json jsconfig.app.json; do
    rm -f "$NODE_SB/ws"/tsconfig*.json "$NODE_SB/ws"/jsconfig*.json
    printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/$cfg"
    ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run" } }'
    ws_seed_fingerprint
    run ws_run
    [ "$status" -eq 20 ] || { echo "accepted a workspace whose only project is $cfg" >&2; return 1; }
    [[ "$output" == *"defines no 'typecheck' script"* ]] \
      || { echo "refused for the wrong reason: $cfg" >&2; return 1; }
    [[ "$output" == *"$cfg"* ]] \
      || { echo "did not name $cfg" >&2; return 1; }

    # And it is satisfied the ordinary way, so this requires a check rather
    # than making the workspace unusable. Asserted positively as well: the
    # refusal's absence alone is satisfied by the lane stopping earlier for some
    # unrelated reason, and the status here is not discriminating -- against the
    # previous tree this fixture exits 20 from the test script itself.
    ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --noEmit" } }'
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"defines no 'typecheck' script"* ]] \
      || { echo "still refused with a typecheck script: $cfg" >&2; return 1; }
    [[ "$output" == *"Running script: typecheck"* ]] \
      || { echo "typecheck never ran for: $cfg" >&2; return 1; }
  done

  # The control: a workspace with no TypeScript configuration at all is not
  # asked for a typecheck script, which is the rule this widens and not one it
  # replaces.
  rm -f "$NODE_SB/ws"/tsconfig*.json "$NODE_SB/ws"/jsconfig*.json
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run" } }'
  ws_seed_fingerprint
  run ws_run
  [[ "$output" != *"defines no 'typecheck' script"* ]]
  rm -rf "$NODE_SB"
}

@test "js config names: the three readers of that question agree" {
  # Discovery in node.sh, discovery in typecheck.sh and the changeset
  # classifier all answer "is this a TypeScript project", and a spelling in one
  # and not the others is how tsconfig.app.json came to be scheduled by the
  # classifier and then found by neither lane. Compared by reading the three
  # lists rather than by restating them here, so this fails when they drift.
  local name
  for name in 'tsconfig.json' 'tsconfig.*.json' 'jsconfig.json' 'jsconfig.*.json'; do
    grep -qF -- "-name '${name}'" "$REPO_ROOT/ci/checks/node.sh" \
      || { echo "node.sh discovery does not match ${name}" >&2; return 1; }
    grep -qF -- "-name '${name}'" "$REPO_ROOT/ci/checks/typecheck.sh" \
      || { echo "typecheck.sh discovery does not match ${name}" >&2; return 1; }
    grep -qF -- "${name}" "$REPO_ROOT/ci/lib/changeset.sh" \
      || { echo "changeset.sh does not classify ${name}" >&2; return 1; }
  done

  # And the classifier really returns javascript for them, rather than merely
  # containing the text.
  for name in tsconfig.app.json jsconfig.app.json; do
    run bash -c ". '$REPO_ROOT/ci/lib/common.sh' && . '$REPO_ROOT/ci/lib/changeset.sh' \
      && ci::changeset::classify_file 'frontend/${name}'"
    [ "$status" -eq 0 ]
    [ "$output" = "javascript" ] || { echo "${name} classified as '${output}'" >&2; return 1; }
  done
}

@test "node lane: a pipe inside quotes is data, not a pipeline" {
  # The composition rule read the raw string, so `PATTERN='foo|bar' vitest run`
  # was refused as a pipeline -- the shell treats that `|` as part of the
  # assignment's value and then runs the whole suite. _blank_quoted is in this
  # file for exactly this and three readers already went through it.
  #
  # Two readers had the fault, not one: _reject_untrustworthy_composition and
  # the copy of the same test inside _script_names_a_checker. Fixing the first
  # alone only changed the message -- the second returned "not a checker", and
  # since a runner *is* named the lane then reported the runner's status lost to
  # a `;` the script does not contain. That is how the second one was found.
  ws_setup
  ws_tools tee

  local spelling
  for spelling in \
    "PATTERN='foo|bar' vitest run" \
    "MSG='a&&b' vitest run" \
    "MSG='a&b' vitest run" \
    "vitest run --reporter='a|b'"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"pipes or branches"* ]] \
      || { echo "refused as a pipeline: $spelling" >&2; return 1; }
    [[ "$output" != *"does not become the script"* ]] \
      || { echo "refused as status-lost: $spelling" >&2; return 1; }
    [[ "$output" != *"backgrounds"* ]] \
      || { echo "refused as backgrounded: $spelling" >&2; return 1; }
    [[ "$output" == *"Running script:"* ]] \
      || { echo "never reached execution: $spelling" >&2; return 1; }
  done

  # The controls, and the point: masking the quotes must not mask a real
  # operator, including one that follows a quoted span.
  for spelling in \
    "vitest run | tee out.log" \
    "vitest run || true" \
    "PATTERN='x' vitest run | tee out.log"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [ "$status" -eq 20 ] || { echo "accepted: $spelling" >&2; return 1; }
    [[ "$output" == *"pipes or branches"* ]] \
      || { echo "refused for the wrong reason: $spelling" >&2; return 1; }
  done

  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run &" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"backgrounds"* ]]

  # And a quote that never closes is refused rather than masked past: the mask
  # blanks to end of string once inside an unclosed quote, so an operator after
  # it would be invisible to both tests above.
  ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"PATTERN='foo vitest run | tee out.log\" } }"
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"leaves a quote open"* ]]
  rm -rf "$NODE_SB"
}

# --- which runner a workspace declares ----------------------------------------
#
# _tests_js_declared_runner decides which of two installed runners owns a suite,
# and arrived with no case of its own. These are it.

# Print _tests_js_declared_runner's answer for a `test` script, or "<none>".
#
# The function is extracted and sourced rather than reached through the lane,
# because the lane installs and runs a real suite to get there and the question
# here is one line of parsing. `bash -n` first, so a mis-extraction fails loudly
# instead of quietly defining nothing.
declared_runner_for() {
  local sb fn
  sb="$(mktemp -d)"
  fn="$(mktemp)"
  awk '/^_tests_js_declared_runner\(\) \{/,/^\}/' "$REPO_ROOT/ci/checks/tests.sh" > "$fn"
  bash -n "$fn" || { rm -rf "$sb" "$fn"; echo "<extraction failed>"; return 1; }
  printf '{ "name": "w", "private": true, "scripts": { "test": "%s" } }\n' "$1" > "$sb/package.json"
  (
    cd "$sb" || exit 1
    # shellcheck disable=SC1090
    . "$REPO_ROOT/ci/lib/common.sh" >/dev/null 2>&1
    # shellcheck disable=SC1090
    . "$fn"
    local _r
    _r="$(_tests_js_declared_runner)"
    printf '%s' "${_r:-<none>}"
  )
  rm -rf "$sb" "$fn"
}

@test "tests-js: the declared runner is the one in command position" {
  # The scan read every word, so a runner named as an *argument* declared the
  # suite: `echo vitest && jest` reported vitest, the lane ran Vitest over a
  # Jest suite, Vitest collected nothing it recognised and exited 0, and
  # tests-js reported PASS with every Jest test uncollected -- the exact failure
  # the declared-runner rule exists to prevent, reached by a spelling it did not
  # read.
  run declared_runner_for "echo vitest && jest"
  [ "$output" = "jest" ]

  run declared_runner_for "echo 'runs vitest' && jest --ci"
  [ "$output" = "jest" ]

  # A runner named among another command's arguments is not what runs.
  run declared_runner_for "bash scripts/test.sh vitest"
  [ "$output" = "<none>" ]

  # The controls: the ordinary spellings still resolve, or this would refuse
  # every workspace that installs both runners.
  local spelling
  for spelling in "vitest run" "cross-env NODE_ENV=test vitest run" \
                  "timeout 300 vitest run" "env -u NODE_ENV vitest run" \
                  "pnpm --filter web vitest run" "npx vitest run"; do
    run declared_runner_for "$spelling"
    [ "$output" = "vitest" ] || { echo "'$spelling' declared '$output'" >&2; return 1; }
  done
  for spelling in "jest --ci" "timeout -k 10 300 jest --ci"; do
    run declared_runner_for "$spelling"
    [ "$output" = "jest" ] || { echo "'$spelling' declared '$output'" >&2; return 1; }
  done

  # And saying nothing is still the honest answer where it cannot tell. The
  # caller refuses a workspace that installs both and declares neither, so
  # silence fails closed.
  run declared_runner_for "npm run jest:ci"
  [ "$output" = "<none>" ]
}

# Print node.sh's _command_runner answer for a command string, or "<none>".
#
# The function and the five helpers it calls are extracted and sourced. That is
# more machinery than the extraction above, and it is the point: this case is
# the only thing that compares the two readers, so it has to ask the real one
# rather than a restatement of what it is believed to do.
node_lane_runner_for() {
  local fn _f
  fn="$(mktemp)"
  for _f in _blank_quoted _unquote_tok _normalize_command _timeout_advance \
            _env_advance _pm_advance _command_runner; do
    awk -v n="^${_f}\\\\(\\\\) \\\\{" '$0 ~ n, /^\}/' "$REPO_ROOT/ci/checks/node.sh" >> "$fn"
  done
  bash -n "$fn" || { rm -f "$fn"; echo "<extraction failed>"; return 1; }
  (
    # shellcheck disable=SC1090
    . "$fn"
    local _cr="" _ng_out="" _bq_state="" _bq_out="" _bq_cont=0 _bq_esc=0
    _command_runner "$1"
    printf '%s' "${_cr:-<none>}"
  )
  rm -f "$fn"
}

@test "tests-js: this reader and the node lane's agree about wrappers" {
  # Two readers of "what does this command run", in two files, and the only
  # reason this one was wrong is that it was written without reference to the
  # other. Compared by running both rather than by restating what either is
  # believed to do.
  #
  # Scoped to single commands, because that is the question both answer.
  # node.sh's _command_runner is applied by its caller to one segment at a time,
  # so asked for a whole composed script it answers about the first segment --
  # `echo vitest && vitest run` is `echo` to it and `vitest` to this reader, and
  # neither is wrong. The wrappers are where they have to agree, and where this
  # one had drifted.
  local spelling ours theirs
  for spelling in \
    "vitest run" \
    "jest --ci" \
    "cross-env NODE_ENV=test vitest run" \
    "timeout 300 vitest run" \
    "timeout -k 10 300 vitest run" \
    "env -u NODE_ENV vitest run" \
    "pnpm --filter web vitest run" \
    "npx vitest run" \
    "pnpm --filter jest vitest run" \
    "env -u vitest jest --ci"; do
    run declared_runner_for "$spelling"
    ours="$output"
    run node_lane_runner_for "$spelling"
    theirs="$output"
    [ "$ours" = "$theirs" ] \
      || { echo "'$spelling': tests.sh says '$ours', node.sh says '$theirs'" >&2; return 1; }
  done

  # And the difference above is asserted rather than assumed, so a change that
  # makes node.sh scan across separators shows up here instead of quietly making
  # the scoping comment false.
  run node_lane_runner_for "echo vitest && vitest run"
  [ "$output" = "echo" ]
  run declared_runner_for "echo vitest && vitest run"
  [ "$output" = "vitest" ]

  run declared_runner_for "npm run jest:ci"
  [ "$output" = "<none>" ]
}

@test "node lane: a package manager's value-taking options are its own, not one list" {
  # The grammar that steps over a manager's options carried one list for all
  # five of them, so `npx --package typescript tsc --noEmit` -- the documented
  # way to run a tool without installing it -- selected `typescript` as the
  # command and the lane refused the script before tsc could run. The joined
  # `--package=typescript` spelling worked, which is the tell: the value was
  # being read as a word rather than as the option's.
  #
  # Keyed by manager rather than widened, for the reason the flag allow-list is
  # keyed by runner: one list cannot describe five vocabularies. `-p` is
  # `--package` to npx and `--parseable` to pnpm, so a shared list either misses
  # npx's value or eats pnpm's command.
  ws_setup
  ws_tools npx pnpm yarn npm bun tsc
  printf '{ "compilerOptions": { "strict": true } }\n' > "$NODE_SB/ws/tsconfig.json"

  local spelling
  for spelling in \
    "npx --package typescript tsc --noEmit" \
    "npx -p typescript tsc --noEmit" \
    "npx --package=typescript tsc --noEmit" \
    "npx tsc --noEmit"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"vitest run\", \"typecheck\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" == *"Running script: typecheck"* ]] \
      || { echo "never reached the compiler: $spelling" >&2; return 1; }
  done

  # And the other direction, which is why this is keyed and not widened: pnpm's
  # `-p` is a boolean, so consuming a word after it would swallow the runner.
  #
  # The tsconfig goes first: with one present the lane requires a typecheck
  # script and then runs it, and `tsc` in this sandbox resolves to a real shim
  # that wants the typescript package -- so the case would fail at the compiler
  # over a question it is not asking.
  rm -f "$NODE_SB/ws/tsconfig.json"
  for spelling in "pnpm -p vitest run" "pnpm --filter web vitest run"; do
    ws_manifest "{ \"name\": \"w\", \"private\": true, \"scripts\": { \"test\": \"${spelling}\" } }"
    ws_seed_fingerprint
    run ws_run
    [[ "$output" != *"not appear to run a test runner"* ]] \
      || { echo "refused as no-runner: $spelling" >&2; return 1; }
    [[ "$output" == *"Running script: test"* ]] \
      || { echo "never reached the runner: $spelling" >&2; return 1; }
  done

  # The control that keeps the rule: a filter behind the option's value is
  # still a filter, so the option is stepped over and not everything after it.
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "npx -p vitest vitest run tests/a.test.ts" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"narrows its own suite"* ]]

  # And `--call`, which is a value-taking option of a different shape: it takes
  # a whole command line as one string, so `npx --call tsc --noEmit` hands `tsc`
  # to the option and leaves `--noEmit` as the command. Refused, and correctly:
  # the gate word-splits, so even the quoted `npx -c 'tsc --noEmit'` arrives as
  # separate tokens and there is no command it can follow. My first draft of
  # this case expected `--call tsc` to reach the compiler, which was a
  # misreading of npx's grammar rather than a defect in the reader.
  printf '{ "compilerOptions": { "strict": true } }
' > "$NODE_SB/ws/tsconfig.json"
  ws_manifest '{ "name": "w", "private": true, "scripts": { "test": "vitest run", "typecheck": "npx --call tsc --noEmit" } }'
  ws_seed_fingerprint
  run ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"does not"* ]]
  [[ "$output" == *"type checker"* ]]
  rm -rf "$NODE_SB"
}

# --- which tree this lane's discovery stands behind ---------------------------
#
# Three readers here answered "is there a workspace" from the worktree and the
# index in every mode, while the manifest check a screen below them already
# switched to `HEAD:` in ship mode. These are the three.

# A sandbox laid out as a real repository, with a frontend workspace committed.
ship_ws_setup() {
  SHIP_SB="$(mktemp -d)"
  mkdir -p "$SHIP_SB/ci/checks" "$SHIP_SB/ci/lib" "$SHIP_SB/frontend/tests" "$SHIP_SB/.ci-gate"
  cp "$REPO_ROOT/ci/checks/node.sh" "$SHIP_SB/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$REPO_ROOT/ci/lib/git.sh" \
     "$SHIP_SB/ci/lib/"
  printf 'it("x", () => {});\n' > "$SHIP_SB/frontend/tests/a.test.ts"
  printf '{}\n' > "$SHIP_SB/frontend/bun.lock"
  printf '{ "compilerOptions": {} }\n' > "$SHIP_SB/frontend/tsconfig.json"
  printf '{ "name": "f", "private": true, "scripts": { "test": "vitest run", "typecheck": "tsc --noEmit" } }\n' \
    > "$SHIP_SB/frontend/package.json"
  ( cd "$SHIP_SB" && git init -q -b main . \
    && printf '.ci-gate/\nci/\n' > .gitignore \
    && git add -A && git -c user.email=t@t -c user.name=t commit -qm init ) >/dev/null 2>&1
}

ship_ws_run() {
  ( cd "$SHIP_SB" \
    && CI_GATE_MODE=ship CI_GATE_PUSH_NEW_SHA="$(git rev-parse HEAD)" \
       bash ci/checks/node.sh 2>&1 )
}

@test "node lane: ship-mode workspace discovery is the pushed tree, not a union" {
  # The first discovery in this file stats the filesystem, and in ship mode its
  # result was unioned with HEAD's rather than replaced by it. So an untracked
  # zz_review_pkg/package.json joined the list and the HEAD manifest check below
  # then failed the push for a workspace the pushed commit does not contain --
  # and git will not send that directory, so there was no remedy but deleting
  # it.
  #
  # Sixth reader of "which tree" corrected across this file and
  # ci/checks/test-layout.sh.
  ship_ws_setup
  mkdir -p "$SHIP_SB/zz_review_pkg"
  printf '{ "name": "zz", "private": true }\n' > "$SHIP_SB/zz_review_pkg/package.json"

  # The premise: untracked, in neither git tree.
  run bash -c "cd '$SHIP_SB' && git ls-files -- zz_review_pkg"
  [ -z "$output" ]

  run ship_ws_run
  [[ "$output" != *"zz_review_pkg"* ]] \
    || { echo "ship discovered a workspace the push does not carry" >&2; echo "$output" >&2; return 1; }
  # And positively: the workspace this push is about was still discovered.
  [[ "$output" == *"frontend"* ]]

  # The control that keeps the rule: the pre-commit gate stands behind the
  # worktree, where the package really is, and still sees it.
  run bash -c "cd '$SHIP_SB' && CI_GATE_MODE=quick bash ci/checks/node.sh 2>&1"
  [[ "$output" == *"zz_review_pkg"* ]]

  # And a workspace the pushed tree does carry is still discovered in ship mode,
  # which is the direction the HEAD source was added for.
  ( cd "$SHIP_SB" && git add zz_review_pkg/package.json \
    && git -c user.email=t@t -c user.name=t commit -qm zz ) >/dev/null 2>&1
  rm -rf "$SHIP_SB/zz_review_pkg"
  run ship_ws_run
  [[ "$output" == *"zz_review_pkg"* ]]
  rm -rf "$SHIP_SB"
}

@test "node lane: the orphan scan reads one tree, not the union of two" {
  # The other direction of the case below, reported one round after it. Adding
  # HEAD beside the filesystem walk fixed the miss and opened a false red: an
  # untracked scratch/tsconfig.json -- a local experiment with no package.json
  # beside it, which git will not send -- failed a ship run over a directory the
  # push has nothing to do with.
  #
  # Which tree a run vouches for chooses the source; it does not add to it. The
  # same correction as candidate_files in ci/checks/test-layout.sh, and the same
  # one this scan's sibling readers took two commits ago.
  ship_ws_setup
  mkdir -p "$SHIP_SB/scratch"
  printf '{ "compilerOptions": {} }\n' > "$SHIP_SB/scratch/tsconfig.json"

  # The premise: untracked, and in no git tree.
  run bash -c "cd '$SHIP_SB' && git ls-files -- scratch"
  [ -z "$output" ]
  run bash -c "cd '$SHIP_SB' && git ls-tree -r --name-only HEAD -- scratch"
  [ -z "$output" ]

  run ship_ws_run
  [[ "$output" != *"no package.json beside it"* ]] \
    || { echo "ship reported an orphan the push does not carry" >&2; echo "$output" >&2; return 1; }
  # And positively: it reached the workspace this push is actually about.
  [[ "$output" == *"frontend"* ]]

  # The control that keeps the rule: the pre-commit gate stands behind the
  # worktree, where the stray really is, and still reports it.
  run bash -c "cd '$SHIP_SB' && CI_GATE_MODE=quick bash ci/checks/node.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"no package.json beside it"* ]]
  [[ "$output" == *"scratch/tsconfig.json"* ]]

  # And once it is in the pushed tree, ship is the gate that must catch it --
  # which is what the case below covers from the other side.
  ( cd "$SHIP_SB" && git add -f scratch/tsconfig.json \
    && git -c user.email=t@t -c user.name=t commit -qm scratch ) >/dev/null 2>&1
  rm -rf "$SHIP_SB/scratch"
  run ship_ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"scratch/tsconfig.json"* ]]
  rm -rf "$SHIP_SB"
}

@test "node lane: in ship mode the orphan scan reads the pushed tree" {
  # This scan is what turns "no manifest" into "a workspace lost its manifest",
  # and it walked the worktree alone -- so the repair that hides the fault hid
  # it here too. Delete frontend/package.json in a commit, then delete the
  # lockfile and the tsconfig on disk only, and push: discovery finds nothing,
  # the scan finds nothing to be orphaned, and the lane prints "No package.json
  # found" and exits 0. Install, typecheck, tests and build all skipped for a
  # pushed tree that is broken, by the check written to notice exactly that.
  ship_ws_setup
  ( cd "$SHIP_SB" && git rm -q frontend/package.json \
    && git -c user.email=t@t -c user.name=t commit -qm drop ) >/dev/null 2>&1
  rm -f "$SHIP_SB/frontend/bun.lock" "$SHIP_SB/frontend/tsconfig.json"

  # The premise: HEAD still carries the configuration, the disk does not.
  run bash -c "cd '$SHIP_SB' && git ls-tree -r --name-only HEAD -- frontend"
  [[ "$output" == *"bun.lock"* ]]
  [[ "$output" == *"tsconfig.json"* ]]
  [ ! -f "$SHIP_SB/frontend/bun.lock" ]

  run ship_ws_run
  [ "$status" -eq 20 ]
  [[ "$output" == *"no package.json beside it"* ]]
  [[ "$output" != *"No package.json found"* ]]

  # The control: with the configuration genuinely gone from the pushed tree too,
  # there is no orphan and nothing to report -- this rule is about a workspace
  # that lost its manifest, not about any repository without one.
  ( cd "$SHIP_SB" && git rm -q -r --ignore-unmatch frontend/bun.lock frontend/tsconfig.json \
    && git -c user.email=t@t -c user.name=t commit -qm "drop the rest" ) >/dev/null 2>&1
  run ship_ws_run
  [ "$status" -eq 0 ]
  [[ "$output" == *"No package.json found"* ]]
  rm -rf "$SHIP_SB"
}

@test "node lane: in ship mode a workspace staged for a later commit is not this push's" {
  # The index is the commit being made; HEAD is the commit being pushed. Folding
  # the index in for every mode meant a workspace staged for next time joined
  # the list, and the HEAD manifest check then reported the pushed commit as
  # missing a manifest for a directory this push has nothing to do with -- a
  # clean HEAD refused because of work staged for later.
  ship_ws_setup
  mkdir -p "$SHIP_SB/added"
  printf '{ "name": "added", "private": true }\n' > "$SHIP_SB/added/package.json"
  ( cd "$SHIP_SB" && git add added/package.json ) >/dev/null 2>&1
  rm -rf "$SHIP_SB/added"

  # The premise: staged, absent from HEAD and from disk.
  run bash -c "cd '$SHIP_SB' && git ls-files -- added/package.json"
  [[ "$output" == *"added/package.json"* ]]
  run bash -c "cd '$SHIP_SB' && git ls-tree -r --name-only HEAD -- added"
  [ -z "$output" ]

  # Asserted as "this push is not about `added`" rather than as an exit status.
  # A ship run that gets past discovery goes on to install and to run the
  # scripts, and this sandbox has neither a real lockfile nor real binaries --
  # so the status would answer a question about the fixture rather than about
  # the rule, and the case would pass or fail for the wrong reason.
  run ship_ws_run
  [[ "$output" != *"added"* ]] \
    || { echo "ship enumerated a workspace that only the index carries" >&2; echo "$output" >&2; return 1; }
  # And positively: it did reach the workspace the push is actually about.
  [[ "$output" == *"frontend"* ]]

  # The control that keeps the rule: the pre-commit gate does stand behind the
  # index, and it still objects to a staged workspace with no manifest on disk.
  run bash -c "cd '$SHIP_SB' && CI_GATE_MODE=quick bash ci/checks/node.sh 2>&1"
  [[ "$output" == *"added"* ]]
  rm -rf "$SHIP_SB"
}

@test "node lane: in ship mode a workspace only the pushed tree carries is still found" {
  # The other direction of the same line. Filesystem discovery cannot see a
  # workspace that exists only in HEAD, the index no longer lists it once its
  # removal is staged, and the ship drift scan iterates the workspaces this list
  # already contains -- so it was contributed by nobody, and its test, typecheck
  # and build scripts were never run and never reported on.
  ship_ws_setup
  mkdir -p "$SHIP_SB/pkg"
  printf '{ "name": "pkg", "private": true, "scripts": { "test": "exit 1" } }\n' \
    > "$SHIP_SB/pkg/package.json"
  ( cd "$SHIP_SB" && git add pkg/package.json \
    && git -c user.email=t@t -c user.name=t commit -qm "add pkg" ) >/dev/null 2>&1
  ( cd "$SHIP_SB" && git rm -q -r --cached pkg ) >/dev/null 2>&1
  rm -rf "$SHIP_SB/pkg"

  # The premise: HEAD carries it, the index and the disk do not.
  run bash -c "cd '$SHIP_SB' && git ls-tree -r --name-only HEAD -- pkg"
  [[ "$output" == *"pkg/package.json"* ]]
  run bash -c "cd '$SHIP_SB' && git ls-files -- pkg"
  [ -z "$output" ]
  [ ! -d "$SHIP_SB/pkg" ]

  run ship_ws_run
  [ "$status" -ne 0 ] || { echo "ship passed over a workspace only HEAD carries" >&2; return 1; }
  [[ "$output" == *"pkg"* ]]
  rm -rf "$SHIP_SB"
}
