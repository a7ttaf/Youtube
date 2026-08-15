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
  SHELL_INPUTS=(ci .githooks .gitignore frontend/README.md)
  SHELL_DRIFT="$( {
    git diff --name-only HEAD -- "${SHELL_INPUTS[@]}" 2>/dev/null || printf '%s\n' "$SCAN_UNREADABLE"
    git ls-files --others --exclude-standard -- "${SHELL_INPUTS[@]}" 2>/dev/null || printf '%s\n' "$SCAN_UNREADABLE"
    # And the ignored ones. A commit that deletes a bats file and adds its path
    # to .gitignore leaves the worktree replacement invisible to both lists
    # above — HEAD does not carry the path, and --exclude-standard drops
    # exactly this file — so the suites ran the replacement for a commit that
    # removes their own coverage. Nothing under ci/ or .githooks/ is
    # legitimately ignored except the gate's own generated state.
    #
    # Which is more than `.ci-gate`. `ci/.gitignore` also declares `reports/*`
    # and `artifacts/*`, and the parallel runner writes its job state and
    # per-check logs into `ci/reports/` *before* this lane runs -- so on every
    # ship run this scan reported the gate's own output as drift and exited 20
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
    _ts_ig_rc=0
    _ts_ig="$(git ls-files --others --ignored --exclude-standard -- "${SHELL_INPUTS[@]}" 2>/dev/null)" \
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
      # output, and the list was still short by every cache this repository's
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
      # `cache` in its name, and so is the next tool's. Naming the shape is
      # what stops the widened suffix set below from reporting a cached `.json`
      # or `.py` as drift and blocking the push on the gate's own scratch
      # output -- the failure this prune was added for, and one a wider
      # allow-list would otherwise have brought straight back.
      printf '%s\n' "$_ts_ig" \
        | grep -Ev '(^|/)(\.ci-gate|node_modules|__pycache__|\.venv|\.[^/]*cache[^/]*)/|^ci/(reports|artifacts)/' \
        | grep -E '\.(sh|bats|bash|yml|yaml|conf|toml|json|py|js|cjs|mjs|ts|go|mod|md)$|(^|/)(\.gitignore|VERSION)$|(^|/)\.githooks/' || true
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
    while IFS= read -r _p; do
      [ -n "$_p" ] || continue
      echo "    $_p"
    done <<< "$SHELL_DRIFT"
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
  echo "  Install bats (e.g. 'npm i -g bats', or your platform's package manager)."
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
# `-maxdepth 1`, because that is what the runner behind this actually executes.
# tests.sh runs `bats ci/tests/`, which is *not* recursive, while this guard
# walked the whole subtree -- so `ci/tests/fixtures/shell/tests/hello.bats`, a
# committed fixture, satisfied the guard on behalf of suites that were not
# there. Deleting every real suite then left bats collecting nothing, exiting 0,
# and the lane printing "Tests result: PASS" over zero tests: found-nothing read
# as everything-passed, which is the exact state this wrapper says it exists to
# prevent. The guard has to ask about the same set the runner runs.
if [ ! -d "$ROOT_DIR/ci/tests" ] \
  || [ -z "$(find "$ROOT_DIR/ci/tests" -maxdepth 1 -type f -name '*.bats' -print -quit 2>/dev/null)" ]; then
  echo "No .bats suites found under ci/tests/."
  echo "  This lane is scheduled as a blocker precisely so those suites execute;"
  echo "  reporting PASS with nothing to run would leave the layout, node and"
  echo "  changeset gates unguarded — the state this check exists to prevent."
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

exec env CI_GATE_CHECK_ID=tests-shell bash "$SCRIPT_DIR/tests.sh" "$@"
