#!/usr/bin/env bash
# The gate's own environment, cleared before a case builds its own.
#
# These suites run *under* the gate they test: ci/checks/tests.sh invokes
# `bats ci/tests/`, and by then ci/preflight.sh has exported CI_GATE_MODE and,
# on a push, ci/hook-dispatch.sh has exported the whole push state. Every case
# here builds a sandbox and runs a copied check inside it, and those inherited
# values reached the copy -- so `bash ci/checks/node.sh` in a sandbox that is
# not this repository took the ship path, resolved a push range belonging to
# another tree, and answered a question the case never asked.
#
# Measured, not assumed. With every suite green one file at a time, a real
# `ci/preflight.sh --mode ship` run reported tests-shell FAIL_NEW_ISSUE with 148
# failures across five files, and a single case flips on the environment alone:
#
#   bats test_js_lane.bats -f 'an orphaned test path with a space'
#     -> ok 1
#   CI_GATE_MODE=ship CI_GATE_PUSH_NEW_SHA=<sha> CI_GATE_PUSH_OLD_SHA=<sha> \
#   CI_GATE_PUSH_REMOTE=origin bats test_js_lane.bats -f '<same>'
#     -> not ok 1 (line 264) `[[ "$output" == *"my component.test.ts"* ]]' failed
#
# So the gate could not pass its own shell suite in the mode that matters, and
# the reason had nothing to do with any of the 148 cases.
#
# A case that wants a mode sets it on the command it runs -- which is how every
# ship-mode case in these files is already written. Nothing here needs the
# ambient value, and a test whose result depends on who invoked it is not
# testing what it says it tests.
#
# Same rule and the same reason as the unset at the top of the pre-push arm in
# ci/hook-dispatch.sh: derived state, cleared before it is derived. That one was
# about a stray value in a developer's shell; this one is about the gate handing
# its own state to the suite that checks it.
#
# CI_CHECKS_SECRET_PATTERN is deliberately NOT cleared. ci/checks/security.sh
# has no definition of its own -- unlike ci/checks/git-safety.sh, which sources
# ci/checks/common.sh -- so it reads that variable from the environment and
# exits early without it. Clearing it here would turn a case that runs security
# into an infrastructure error instead of the thing it asserts. That divergence
# between the two checks is worth closing on its own; it is not this file's to
# close, and papering over it here would hide it.
ci::tests::clear_gate_env() {
  unset CI_GATE_MODE CI_GATE_HOOK CI_GATE_VERBOSE CI_GATE_FIX
  unset CI_GATE_PUSH_OLD_SHA CI_GATE_PUSH_NEW_SHA CI_GATE_PUSH_REMOTE
  unset CI_GATE_PUSH_DELETIONS_ONLY CI_GATE_PUSH_REMOTE_REFS
  unset CI_GATE_PUSH_BRANCH_TIPS CI_GATE_PUSH_TAG_TIPS CI_GATE_PUSH_OTHER_TIPS
  unset CI_GATE_CHANGED_FILES CI_GATE_INCREMENTAL CI_GATE_CACHE_ENABLED
  unset CI_GATE_PARALLEL CI_GATE_TIMEOUT CI_GATE_FAIL_FAST
  unset CI_GATE_PRE_COMMIT_BUDGET CI_GATE_PRE_PUSH_BUDGET
  unset CI_GATE_COVERAGE_MIN CI_GATE_COMPLEXITY_MAX CI_GATE_BUNDLE_SIZE_MAX
  unset CI_GATE_NODE_WORKSPACE CI_GATE_USE_LANES CI_GATE_CACHE_DIR
}
