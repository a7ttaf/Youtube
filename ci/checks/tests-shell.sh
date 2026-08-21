#!/usr/bin/env bash
# Runs the bats suites under ci/tests/ as a scheduled gate lane.
#
# ci/checks/tests.sh dispatches on CI_GATE_CHECK_ID, but preflight's run_phase
# executes a script path directly ("$script"), so a phase entry cannot carry an
# environment variable of its own. This wrapper is that variable.
#
# It exists because the suites guarding this gate were themselves unscheduled:
# a change to ci/checks/*.sh emitted only lint-shell and format-shell, so the
# tests covering the layout guard and the node lane ran only when invoked by
# hand — the same "registered but never runs" failure those suites exist to
# catch.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"

cd "$ROOT_DIR"

# The worktree-local bats, if one has been provisioned.
#
# This lane is a blocker that refuses with FAIL_INFRA when it cannot find its
# runner -- correct, because a lane reporting PASS without running these suites
# is worse than no lane. What was missing was the other half: nothing installed
# bats. `uv sync` does not, and neither did ci/install-hooks.sh, so a fresh clone
# of a supported machine had every push touching ci/ blocked with no in-repo way
# to unblock it. `ci/scripts/install-bats.sh` provisions a pinned copy here, and
# this is what makes it take effect without anyone exporting PATH by hand.
#
# Appended rather than prepended: a bats the developer or the image already has
# stays the one that runs, and this is only the fallback.
if [ -x "$ROOT_DIR/.ci-gate/bats/bin/bats" ]; then
  PATH="${PATH}:${ROOT_DIR}/.ci-gate/bats/bin"
  export PATH
fi

# The suites run from the worktree, which is the tree this lane is entitled to
# report on only in the pre-commit gate. In ship mode the commit already exists,
# so a gate script, a bats file or .gitignore broken in an outgoing commit and
# repaired only on disk is validated in its repaired form while the broken
# version is pushed. The node lane had the same defect against its own inputs;
# this is the same rule over the inputs these suites actually read.
#
# Scoped to those inputs rather than the whole tree: an unrelated dirty file is
# not something this lane's result claims anything about.
if [ "${CI_GATE_MODE:-}" = "ship" ] \
  && command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1 \
  && git rev-parse --verify HEAD >/dev/null 2>&1; then
  # One list of inputs, consulted by all three commands.
  #
  # The tracked scan covered .gitignore and frontend/README.md; the untracked
  # and ignored scans were scoped to ci and .githooks alone. So a commit that
  # deleted .gitignore and left a worktree-only copy behind was invisible to
  # all three — the tracked scan sees a deletion, and the scan that would have
  # seen the replacement was not looking there. Three scopes for one question
  # is how they came to disagree.
  # A scan that cannot read its reference tree has not found a clean one.
  # `|| true` made those the same answer, so an unreadable HEAD blob left the
  # drift list empty and these suites ran the worktree against a reference
  # nothing had compared them to. The sentinel survives the pipeline and is
  # checked before the output is read as a list of paths.
  SCAN_UNREADABLE="__ci_gate_unreadable__"
  # `Makefile` is in this list for the reason the other four are: the suites read
  # it. `ci/tests/test_preflight.bats` asserts the `bats-install` target exists
  # and matches the refusal message this lane prints -- against the *worktree*
  # copy, since that is the file bats opens. So a push whose HEAD carries a
  # broken Makefile, with an unstaged local repair, ran that assertion against
  # the repair and approved a commit that fails its own provisioning check. The
  # comparison scope and what the suites actually read have to be the same set,
  # which is the lesson the three-scopes-for-one-question note above records.
  SHELL_INPUTS=(ci .githooks .gitignore Makefile frontend/README.md)
  # The entries above that carry no file extension, named once because the
  # ignored-file scan below filters by suffix and cannot see them otherwise.
  #
  # Adding `Makefile` to SHELL_INPUTS without adding it here left exactly the
  # fail-open that addition was meant to close, one scan over: a commit that
  # deletes `Makefile` and ignores the path leaves a worktree replacement that
  # the tracked scan cannot see (the path is untracked now), that
  # --exclude-standard drops (it is ignored), and that this filter discarded
  # for having no recognised suffix. Measured before the fix: exit 0, "Shell
  # suites passed", with the bats run reading a `bats-install` target the push
  # does not carry. Two lists for one question is what round after round here
  # has been about, so the scope and the filter now share this definition.
  SHELL_INPUT_BASENAMES='\.gitignore|Makefile|VERSION'
  SHELL_DRIFT="$( {
    git diff --name-only HEAD -- "${SHELL_INPUTS[@]}" 2>/dev/null || printf '%s\n' "$SCAN_UNREADABLE"
    git ls-files --others --exclude-standard -- "${SHELL_INPUTS[@]}" 2>/dev/null || printf '%s\n' "$SCAN_UNREADABLE"
    # And the ignored ones. A commit that deletes a bats file and adds its path
    # to .gitignore leaves the worktree replacement invisible to both lists
    # above — HEAD does not carry the path, and --exclude-standard drops
    # exactly this file — so the suites ran the replacement for a commit that
    # removes their own coverage. Nothing under ci/ or .githooks/ is
    # FIX: macOS /bin/bash 3.2 misparses a possessive apostrophe inside any
    # comment that sits in a double-quoted command substitution block. The
    # same file is accepted by bash 5. Possessives in comments below use
    # hyphenation so the system Bash the gate targets can parse this scan.
    # legitimately ignored except the gate-owned generated state.
    #
    # Which is more than `.ci-gate`. `ci/.gitignore` also declares `reports/*`
    # and `artifacts/*`, and the parallel runner writes its job state and
    # per-check logs into `ci/reports/` *before* this lane runs -- so on every
    # ship run this scan reported the gate-owned output as drift and exited 20
    # before a single suite executed. The pre-push gate blocking itself, and
    # the remedy it printed -- "commit the rest" -- is one git-safety.sh
    # forbids. Anchored at `ci/`, so only these two directories are pruned and
    # not any directory that happens to share their name.
    #
    # The listing status is taken before the output is shaped. `| grep ... ||
    # true` puts the `|| true` on the *pipeline*, so it answers for grep -- which
    # exits 1 whenever it filters everything out, the ordinary case -- and never
    # for the enumeration in front of it. An unreadable index therefore produced
    # an empty list that reads exactly like "nothing is ignored here", and this
    # is the one of the three scans that can see a worktree replacement for a
    # path the pushed commits delete: the suites would then validate code that
    # is not in the tree being pushed. Third instance of this shape in the same
    # sweep, and the two before it were also hidden behind a pipeline.
    #
    # `-z`, through the shared NUL reader. git quotes by default, so an ignored
    # `ci/tests/café.bats` arrived as `"ci/tests/caf\303\251.bats"` -- ending in
    # a quote character rather than in `.bats`, so the suffix filter below
    # discarded it and this scan reported nothing. A commit that deletes and
    # ignores that suite while the local copy remains then leaves bats executing
    # coverage the pushed tree does not have, which is the precise fail-open
    # this scan exists to close. Fourth reader in this repository with the same
    # quoting fault, and the first three were found by sweeping for it.
    _ts_ig_rc=0
    _ts_ig="$(git ls-files -z --others --ignored --exclude-standard -- "${SHELL_INPUTS[@]}" 2>/dev/null \
              | ci::common::nul_to_lines
              exit "${PIPESTATUS[0]}")" \
      || _ts_ig_rc=$?
    if [ "$_ts_ig_rc" -ne 0 ]; then
      printf '%s\n' "$SCAN_UNREADABLE"
    else
      # Narrowed to the files these suites actually read, which is what the
      # scan is for: an ignored path only matters here if it could stand in for
      # one the pushed commits delete.
      #
      # Which is not only shell and bats, and reading it that way left the
      # config these suites are mostly *about* outside the scan. `test_js_lane`
      # asserts on ci/config/checks.yml and ci/config/affected.yml,
      # `test_preflight` on ci/config/lanes.conf, the layout suite on
      # ci/checks/manifest.yml, and the language lanes run the fixtures under
      # ci/tests/fixtures/ as real projects. A commit that deleted
      # ci/config/affected.yml and ignored the path left a worktree copy that
      # all three scans missed -- the tracked diff sees no change because the
      # path is untracked now, --exclude-standard drops it, and this filter
      # dropped it for having the wrong suffix -- so the suites validated a
      # configuration the pushed tree does not contain and the lane reported
      # PASS. The suffix set below is every input type these directories carry.
      #
      # Pruning by name did not scale. `.ci-gate`, `ci/reports/` and
      # `ci/artifacts/` were each added after the gate blocked itself on its own
      # output, and the list was still short by every cache this repository
      # own tooling writes under ci/ -- all of these are ignored, all were
      # reported as drift, and each one exits 20 before a single suite runs:
      #
      #   ci/__pycache__/x.pyc          ci/.ruff_cache/z
      #   ci/lib/__pycache__/y.pyc      ci/tests/.pytest_cache/w
      #
      # A deny-list has to name every generated directory that will ever exist,
      # including the ones a tool added last week. What the suites read is a
      # closed set, so it is the set that is named.
      #
      # The prune stays, and it is now written by shape rather than by name:
      # every one of those four is `__pycache__` or a dot-directory with
      # `cache` in its name, and so is the next tool. Naming the shape is
      # what stops the widened suffix set below from reporting a cached `.json`
      # or `.py` as drift and blocking the push on the gate-owned scratch
      # output -- the failure this prune was added for, and one a wider
      # allow-list would otherwise have brought straight back.
      printf '%s\n' "$_ts_ig" \
        | grep -Ev '(^|/)(\.ci-gate|node_modules|__pycache__|\.venv|\.[^/]*cache[^/]*)/|^ci/(reports|artifacts)/' \
        | grep -E '\.(sh|bats|bash|yml|yaml|conf|toml|json|py|js|cjs|mjs|ts|go|mod|md)$|(^|/)('"$SHELL_INPUT_BASENAMES"')$|(^|/)\.githooks/' || true
    fi
  } | sort -u | sed '/^$/d')"
  case "$SHELL_DRIFT" in
    *"$SCAN_UNREADABLE"*)
      echo "Cannot read the tree these suites are being compared against."
      echo "  Refusing to report on the worktree alone: a reference that could"
      echo "  not be read is not a reference that matches."
      exit "$CI_RESULT_FAIL_INFRA"
      ;;
  esac
  if [ -n "$SHELL_DRIFT" ]; then
    echo "Gate inputs differ between HEAD and the worktree:"
    # FIX: bash 3.2 has no <<< here-string; pipe the list instead so the
    # drift report path works on the same Bash the gate targets.
    while IFS= read -r _p; do
      [ -n "$_p" ] || continue
      echo "    $_p"
    done <<EOF
$SHELL_DRIFT
EOF
    echo "  These suites read the worktree, so the run would report on files the"
    echo "  pushed commits do not contain. Commit the rest, stash it, or discard it."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi
fi

# tests.sh logs "skipped: bats not installed" and returns 0, which is right for
# a generic lane that may run in a repo with no shell tests. It is wrong here:
# this lane exists *because* these suites must run, and `uv sync` does not
# provision bats, so a fresh environment would report PASS having executed
# nothing. An enabled blocker that cannot find its runner is broken
# infrastructure, not a pass.
if ! ci::common::command_exists bats; then
  echo "bats is not installed, so the ci/tests/ suites cannot run."
  echo "  This lane is scheduled as a blocker precisely so those suites execute;"
  echo "  reporting PASS here would mean the layout and node gates are unguarded."
  echo ""
  echo "  Provision it into this worktree (pinned, no sudo, nothing installed"
  echo "  outside the repository, and 'rm -rf .ci-gate/bats' undoes it):"
  echo ""
  echo "      make bats-install"
  echo ""
  echo "  Or use a copy you already have -- anything named 'bats' on PATH is"
  echo "  preferred over the provisioned one. A platform package or"
  echo "  'npm i -g bats' both work."
  exit "$CI_RESULT_FAIL_INFRA"
fi

# The dispatcher treats a missing ci/tests/ as a successful skip, which is right
# for a generic lane in a repo that has no shell tests and wrong for this one.
# This wrapper exists *because* those suites must run: with every file under
# ci/tests/ deleted, the dispatcher logged "skipped: no ci/tests directory
# found", reported PASS, and the gate approved the removal of the entire
# regression net it was added to enforce. Asked here, before delegating, for the
# same reason the bats-missing case is asked here.
#
# One level only, because that is what the runner behind this actually executes.
# tests.sh runs `bats ci/tests/`, which is *not* recursive, while this guard
# walked the whole subtree -- so `ci/tests/fixtures/shell/tests/hello.bats`, a
# committed fixture, satisfied the guard on behalf of suites that were not
# there. Deleting every real suite then left bats collecting nothing, exiting 0,
# and the lane printing "Tests result: PASS" over zero tests: found-nothing read
# as everything-passed, which is the exact state this wrapper says it exists to
# prevent. The guard has to ask about the same set the runner runs.
#
# A glob rather than `find -maxdepth 1 ... -print -quit`, and it is the plainer
# tool for the question as well as the portable one: `*.bats` in one directory
# *is* one level, where `-maxdepth` is outside POSIX and `-quit` is not in every
# find that has `-maxdepth`. Nothing here needs a tree walk.
_ts_have_bats_suite() {
  local _ts_b
  for _ts_b in "$ROOT_DIR"/ci/tests/*.bats; do
    [ -f "$_ts_b" ] && return 0
  done
  return 1
}
if [ ! -d "$ROOT_DIR/ci/tests" ] || ! _ts_have_bats_suite; then
  echo "No .bats suites found under ci/tests/."
  echo "  This lane is scheduled as a blocker precisely so those suites execute;"
  echo "  reporting PASS with nothing to run would leave the layout, node and"
  echo "  changeset gates unguarded — the state this check exists to prevent."
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

exec env CI_GATE_CHECK_ID=tests-shell bash "$SCRIPT_DIR/tests.sh" "$@"
