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
  unset CI_GATE_NODE_WORKSPACE CI_GATE_USE_LANES

  unset CI_GATE_TRUST_TRACKING_REFS
  unset CI_GATE_PUSH_REMOTE_TIPS CI_GATE_PUSH_REMOTE_TIPS_FOR

  # CI_GATE_CHECK_ID selects which language a check runs, and four checks read
  # it: ci/checks/format.sh, lint.sh, tests.sh and typecheck.sh, each defaulting
  # to `all`. Its value is ambient in every case in these files during a real
  # gate run -- ci/checks/tests-shell.sh sets it, and it reaches grandchildren:
  #
  #   env -u CI_GATE_CHECK_ID bash -c \
  #     'exec env CI_GATE_CHECK_ID=tests-shell bash mid.sh'   # mid.sh runs inner.sh
  #   -> grandchild sees: CI_GATE_CHECK_ID='tests-shell'
  #
  # and tests-shell.sh's grandchildren are `bats ci/tests/` and everything a
  # case invokes. So a case running a check bare narrows it to the shell arm
  # without saying so, which for typecheck.sh is the difference between
  #
  #   bash ci/checks/typecheck.sh                       -> runs mypy, fails here
  #   CI_GATE_CHECK_ID=typecheck-js bash .../typecheck.sh -> tsc only, passes
  #
  # No case relies on the ambient value today; every one of them pins it on the
  # command, which is why nothing has broken yet. That is the state the rest of
  # this list was also in until the day it wasn't.
  unset CI_GATE_CHECK_ID

  # CI_GATE_CACHE_DIR is *set* here, not unset, and that is the whole point.
  #
  # ci/lib/cache.sh defaults it to "${HOME}/.cache/ci-gate", so unsetting it did
  # not isolate a case from the gate's cache -- it pointed every case at the
  # developer's shared one. A sandbox these files build is a fresh directory
  # with a synthetic tree, but the cache key is the lane plus tool versions plus
  # the changed files, and two runs of the same case build byte-identical
  # sandboxes: the second run gets a hit from the first, run_phase prints
  # "[cache hit] <lane>" and restores the result without executing anything.
  #
  # Every case here that asserts on which lanes ran reads execution markers, so
  # a hit reads as "the lane was not scheduled". Found by the gate rather than
  # by these suites: `preflight: the standalone typecheck lane runs` passed on
  # every direct run and failed under ci/preflight.sh --mode ship, with 669
  # entries in ~/.cache/ci-gate by then. The sandbox output says it plainly:
  #
  #   RAN:node
  #     [cache hit] typecheck-js
  #   Result [typecheck-js]: PASS
  #
  # The lane was scheduled correctly the whole time. Only the marker was gone.
  #
  # BATS_TEST_TMPDIR is per-test and bats removes it, so each case starts cold
  # and the caching path itself still runs -- which is what ci/tests/
  # test_cache.bats asserts, and it keeps setting its own directory after this
  # call for exactly that reason. This is the same pattern, applied to the cases
  # that never meant to involve the cache at all.
  CI_GATE_CACHE_DIR="${BATS_TEST_TMPDIR:-${BATS_TMPDIR:-/tmp}}/ci-gate-cache"
  export CI_GATE_CACHE_DIR
}

# Make a sandbox's destination genuinely carry a commit.
#
# `git update-ref refs/remotes/origin/main <sha>` was how these fixtures said
# "the destination already has this". It is not a statement about the
# destination -- it writes this clone's *memory* of one -- and
# ci::git::published_to_destination stopped accepting that memory as proof, for
# the reason a force-push makes it wrong: the ref can still reach a commit the
# remote has dropped.
#
# So a fixture that means "published" now has to publish. A bare repository
# beside the sandbox is a real remote: `ls-remote` answers from it, the answer
# is authoritative, and no network is involved.
#
# ci::tests::publish <sandbox> <remote> <branch> <sha>
ci::tests::publish() {
  local sb="$1" remote="$2" branch="$3" sha="$4"
  local bare="${sb}/.remotes/${remote}.git"
  if [ ! -d "$bare" ]; then
    mkdir -p "${sb}/.remotes"
    git init -q --bare "$bare" || return 1
  fi
  ( cd "$sb"     && { git remote add "$remote" "$bare" 2>/dev/null || git remote set-url "$remote" "$bare"; }     && git push -q "$bare" "+${sha}:refs/heads/${branch}" ) || return 1
  # The tracking ref too, so a fixture that also reads it still sees what it
  # expects. It is no longer what decides the answer.
  ( cd "$sb" && git update-ref "refs/remotes/${remote}/${branch}" "$sha" ) || return 1
}
