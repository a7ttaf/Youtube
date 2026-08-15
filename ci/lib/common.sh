#!/usr/bin/env bash
set -Eeuo pipefail

CI_RESULT_PASS=0
CI_RESULT_PASS_WITH_KNOWN_DEBT=10
CI_RESULT_FAIL_NEW_ISSUE=20
CI_RESULT_FAIL_INFRA=30
# Distinguish explicit help requests from failures in CLI parsers.
CI_RESULT_HELP=99
export CI_RESULT_PASS CI_RESULT_PASS_WITH_KNOWN_DEBT CI_RESULT_FAIL_NEW_ISSUE CI_RESULT_FAIL_INFRA CI_RESULT_HELP

ci::common::project_root() {
  git rev-parse --show-toplevel 2>/dev/null || pwd
}

ci::common::section() {
  printf "\n========== %s ==========\n" "$1"
}

ci::common::command_exists() {
  command -v "$1" >/dev/null 2>&1
}

# Emit each directory that anchors a JS/TS workspace, one per line, relative
# to the current directory (callers cd to the repo root first).
#
# A repo whose manifest sits at the root keeps its existing behaviour exactly:
# "." is emitted and nothing else, so single-package repos are unaffected.
# Only when the root has no manifest do we look one level down — that is the
# layout here (frontend/package.json, no root package.json), and without this
# the JS lanes silently skipped and no frontend test ever ran in the gate.
#
# $1: manifest filename to look for (package.json, tsconfig.json, ...)
# Is this manifest part of the tree the run stands behind?
#
# Discovery stats the worktree, which in ship mode is the commit being built and
# not the one being pushed. An untracked scratch/package.json therefore became a
# workspace of the push: the standalone typecheck lane entered it, found no
# scratch/node_modules/.bin/tsc and returned FAIL_INFRA, so unrelated local WIP
# blocked a valid push. Same question as ci::changeset::_in_node_workspace, one
# layer down, and this is the reader the node, tests and typecheck lanes share --
# fixing it in one of them would have been a fourth answer to it.
#
# An intersection with HEAD, never a substitute for the walk. The list stays
# "manifests that are on disk", narrowed in ship mode to those the push carries:
# a workspace present in HEAD and deleted locally is still absent, because the
# walk never offered it. Narrowing only, which is the direction that cannot
# invent a workspace nothing can install.
#
# Non-ship modes, a non-repository, and a repository with no commits all answer
# "in scope" -- quick stands behind the index and the worktree, and could-not-ask
# must not silently empty the list the way a failure to enumerate would.
ci::common::_manifest_in_scope() {
  [ "${CI_GATE_MODE:-}" = "ship" ] || return 0
  command -v git >/dev/null 2>&1 || return 0
  git rev-parse --git-dir >/dev/null 2>&1 || return 0
  git rev-parse --verify HEAD >/dev/null 2>&1 || return 0
  git cat-file -e "HEAD:${1#./}" 2>/dev/null
}

# ci::common::workspace_drift <path> – paths under <path> that differ between
# HEAD and the worktree, on stdout. Returns 1 when there are any, 2 when the
# comparison could not be made, 0 when the two agree.
#
# The lanes that read the worktree need this before they report on a push.
# ci/checks/node.sh has had it since "Workspace files differ between HEAD and
# the worktree", and ci/checks/tests-shell.sh has its own for the gate's own
# sources -- but `typecheck-js` and `tests-js` are scheduled as standalone
# blocker lanes that never go through node.sh, and neither asked. Narrowing the
# project and test lists to HEAD, which is what this branch just did, chooses
# *which* files are read and says nothing about their *contents*:
#
#   CI_GATE_MODE=ship CI_GATE_CHECK_ID=typecheck-js bash ci/checks/typecheck.sh
#
# compiled the worktree's copy, so a type error committed in HEAD and fixed only
# on disk was reported PASS for a push that still carries it. The same for a
# failing test repaired locally and not committed.
#
# Untracked files count. A test or a source module that exists only on disk is
# read by the runner and is not in the push, which is the same divergence from
# the other side.
#
# Ignored files count too, and the first version of this said they did not --
# "those are build output and caches", which is true of most of them and not of
# the one that matters. A commit that deletes frontend/src/helper.ts and adds
# that path to .gitignore leaves the local copy on disk, and it is invisible to
# both lists above: HEAD does not carry the path, so nothing is different about
# it, and --exclude-standard drops exactly this file. Measured:
#
#   file on disk: YES      in HEAD: NO
#   git diff --name-only HEAD -- frontend            -> (nothing)
#   git ls-files --others --exclude-standard         -> (nothing)
#   git ls-files --others --ignored --exclude-standard -> frontend/src/helper.ts
#
# so the lane compiled and ran a file the push deletes. That is precisely the
# case ci/checks/tests-shell.sh's ignored scan exists for, one lane over, and
# excluding ignored files here threw it away.
#
# Narrowed the way that scan is narrowed, and for the same reason: listing every
# ignored path means node_modules and every build directory, which is the cost
# that made a deny-list unfinishable there. What these lanes read is a closed
# set of source and test extensions, so that is what is named.
#
# Both statuses are taken, and separately from the output. `git diff` and
# `git ls-files` each answer half the question, and a pipeline's status answers
# for its last stage -- so an unreadable index produced an empty list that reads
# exactly like "the worktree matches", which is the failure this returns 2 for.
ci::common::workspace_drift() {
  local ws="${1:-.}" _wd_a _wd_b _wd_rc=0
  command -v git >/dev/null 2>&1 || return 0
  git rev-parse --git-dir >/dev/null 2>&1 || return 0
  git rev-parse --verify HEAD >/dev/null 2>&1 || return 0

  _wd_a="$(git diff --name-only HEAD -- "$ws" 2>/dev/null)" || _wd_rc=2
  [ "$_wd_rc" -eq 0 ] || return 2
  _wd_b="$(git ls-files --others --exclude-standard -- "$ws" 2>/dev/null)" || _wd_rc=2
  [ "$_wd_rc" -eq 0 ] || return 2

  # The ignored ones, filtered to files these lanes read. node_modules and the
  # usual build outputs are pruned first: those are ignored on purpose and
  # listing them would bury the one path that matters in tens of thousands.
  #
  # The pruned set is the one this repository already agrees on -- the same
  # eleven directories ci::common::is_vendored_path names and ci/checks/node.sh's
  # suite scans prune. `.vite` and `htmlcov` were missing from this copy alone,
  # and a missing entry here is not a quiet one: `frontend/.vite/cache.js` is
  # ignored, generated, and ends in an extension this filter keeps, so ship-mode
  # tests-js and typecheck-js refused an otherwise clean push over a build cache.
  # A guard that refuses correct pushes gets switched off, which is the same
  # outcome as not having it.
  local _wd_c
  _wd_c="$(git ls-files --others --ignored --exclude-standard -- "$ws" 2>/dev/null)" || _wd_rc=2
  [ "$_wd_rc" -eq 0 ] || return 2
  # The extension list is what these lanes *load*, not what a person would call
  # source. Vite and Vitest resolve a stylesheet import like any other module, and
  # ci/config/affected.yml already treats a stylesheet change as a reason to run
  # the frontend suite -- so a push that deletes and ignores
  # `frontend/src/theme.css` while an ignored local copy remains left every one
  # of these three scans empty, and ship-mode tests-js ran against the shadow and
  # passed over a pushed tree whose import does not resolve. The same argument
  # covers the other things a build graph pulls in by import: the CSS dialects,
  # the asset types a bundler inlines, and the config files these lanes read to
  # decide what to run at all.
  _wd_c="$(printf '%s\n' "$_wd_c" \
    | grep -Ev '(^|/)(node_modules|dist|build|out|coverage|htmlcov|\.next|\.nuxt|\.turbo|\.vite|\.cache|__pycache__)/' \
    | grep -E '\.(ts|tsx|js|jsx|mjs|cjs|cts|mts|vue|svelte|json|css|scss|sass|less|styl|pcss|svg|html|py|yml|yaml|toml)$|(^|/)(package\.json|tsconfig[^/]*\.json|vite\.config\.[cm]?[jt]s|vitest\.config\.[cm]?[jt]s)$' || true)"

  local _wd_all
  _wd_all="$(printf '%s\n%s\n%s\n' "$_wd_a" "$_wd_b" "$_wd_c" | sed '/^$/d' | sort -u)"
  [ -n "$_wd_all" ] || return 0
  printf '%s\n' "$_wd_all"
  return 1
}

# ci::common::refuse_on_workspace_drift <path> – the shared diagnostic for the
# above, for a lane that is about to read the worktree in ship mode. Exits;
# does not return, unless there is nothing to report.
#
# The wording follows ci/checks/node.sh's, because a developer who has hit that
# one should not have to work out that this is the same condition.
ci::common::refuse_on_workspace_drift() {
  local ws="${1:-.}" _rd_out _rd_rc=0 _rd_p
  _rd_out="$(ci::common::workspace_drift "$ws")" || _rd_rc=$?
  case "$_rd_rc" in
    0) return 0 ;;
    2)
      echo "Cannot compare ${ws} against HEAD."
      echo "  Refusing to report a clean comparison from a listing that failed"
      echo "  to produce one."
      exit "${CI_RESULT_FAIL_INFRA:-30}"
      ;;
  esac
  echo "Workspace files differ between HEAD and the worktree:"
  while IFS= read -r _rd_p; do
    [ -n "$_rd_p" ] || continue
    echo "    $_rd_p"
  done <<< "$_rd_out"
  echo "  This lane reads the worktree, so it would report on content the pushed"
  echo "  commits do not contain. Commit the rest, stash it, or discard it."
  exit "${CI_RESULT_FAIL_NEW_ISSUE:-20}"
}

# Manifests the push carries that this worktree cannot supply.
#
# `_manifest_in_scope` narrows the walk to what the push carries, which drops an
# untracked scratch/package.json correctly. It cannot add a manifest HEAD
# carries that the walk never offered -- and that case was once documented as
# "still absent", which is a false pass rather than a narrowing:
#
#   rm -rf frontend
#   CI_GATE_MODE=ship CI_GATE_CHECK_ID=typecheck-js bash ci/checks/typecheck.sh
#   -> skipped: no package.json found        exit 0
#
# for a push whose HEAD still carries frontend/package.json. Inventing the
# workspace is not the fix -- nothing can install a directory that is not there
# -- but "no workspace" is not the truth either. The truth is that this run
# cannot check the push, so it says so and the caller records FAIL_INFRA.
#
# A function, called before node_workspaces' first return rather than after its
# last. It lived at the end of the walk, and the root-manifest branch returns
# above that -- so a pushed tree carrying a root package.json beside
# packages/app/package.json, with packages/app/ missing locally, emitted `.` and
# never reached the check. The node, tests and typecheck lanes then ran the root
# alone and the pushed child's scripts were skipped: the same false pass, by the
# one path that returns before the guard.
#
# ci::common::ls_tree_paths <git-ls-tree-args...> – the paths in a tree, one
# per line, spelled the way they actually are.
#
# `git ls-tree --name-only` does not print a path; it prints git's C-string
# quoting of one whenever the name carries a non-ASCII byte, a quote, a
# backslash or a control character. `café/package.json` comes back as
# `"caf\303\251/package.json"` -- trailing quote and all -- so every reader here
# that filters the listing with an anchored `(^|/)package\.json$` matched
# nothing and the workspace simply was not there. In ship mode that is a lane
# that installs nothing, runs no test, no typecheck, no build, and reports PASS;
# and the three scans that exist to notice a workspace going missing read the
# same listing and miss it the same way. An accent in a directory name is not an
# attack, which is what makes it worth being fail-closed about.
#
# The fix this repository already applies elsewhere -- ci/checks/git-safety.sh
# and ci/checks/test-layout.sh both read NUL-delimited for exactly this reason,
# and three drift scans one screen below in node.sh already pass
# `-c core.quotepath=false`. It was applied to the diff and ls-files readers and
# not to the ls-tree ones, so it lives here now and they share it.
#
# `-z` rather than `core.quotepath=false`, because that option still quotes a
# name containing `"`, `\` or a control byte. NUL separation quotes nothing at
# all -- which leaves one name this newline-joined shape cannot carry, a path
# with a newline in it, and that returns 2 rather than a listing silently short
# by a line. Returns 1 when the enumeration itself failed. Callers refuse on
# both: a path that cannot be spelled is not a path that is absent.
ci::common::ls_tree_paths() {
  local _lt_out
  # The translation happens inside the pipeline because a command substitution
  # strips NUL bytes -- capturing `-z` output into a variable first throws away
  # the only separator the listing has and hands back every path concatenated
  # into one word. Bash says so out loud ("ignored null byte in input") and then
  # carries on, which is the quiet kind of wrong this file keeps finding.
  #
  # One `tr` doing two jobs: NUL becomes the newline that joins the list, and a
  # newline *inside a path* becomes \001 -- a byte no path can contain. Without
  # that second mapping such a path would arrive as two entries, each matching
  # nothing, and a listing silently short by an entry is exactly what this
  # function exists to refuse. Done in the same pass so the tree is walked once.
  #
  # `exit "${PIPESTATUS[0]}"` carries the producer's status out: `git | tr`
  # answers for `tr`, which succeeds at translating nothing, so a failed
  # enumeration would otherwise arrive as an empty listing and read as "the tree
  # holds no workspaces".
  _lt_out="$(git ls-tree -z "$@" 2>/dev/null | tr '\000\012' '\012\001'
             exit "${PIPESTATUS[0]}")" || return 1
  case "$_lt_out" in
    *$'\001'*) return 2 ;;
  esac
  printf '%s' "$_lt_out"
}

# Returns 1 with a reason on stderr, 0 when there is nothing to report.
#
# "Nothing to report" is the three preconditions -- not a ship run, no git, no
# HEAD -- where there is no question to ask in the first place. It is NOT a
# question that was asked and did not come back: a listing that failed returns 1
# with the rest, because could-not-read must not empty a list the way a genuine
# absence would. Those two were the same arm, and the difference is the whole
# point of this function.
ci::common::_head_manifests_present() {
  local manifest="${1:-package.json}"
  [ "${CI_GATE_MODE:-}" = "ship" ] || return 0
  command -v git >/dev/null 2>&1 || return 0
  git rev-parse --git-dir >/dev/null 2>&1 || return 0
  git rev-parse --verify HEAD >/dev/null 2>&1 || return 0

  local _nw_head _nw_missing="" _nw_dir _nw_found
  # `--full-tree` so the answer does not depend on where this was called from:
  # `git ls-tree` applies the current directory as an implicit path filter, and
  # an empty listing reads exactly like "the push carries no manifests".
  #
  # A listing that failed is reported, not swallowed. `|| return 0` was reading
  # "could not ask" -- the three preconditions above, which really are nothing
  # to report -- into a fourth case that is not one: HEAD exists, it was asked,
  # and the answer did not come back. With a name in the tree that
  # `ls_tree_paths` refuses to fold (status 2) and a committed workspace missing
  # locally, this returned 0, `node_workspaces` handed back an empty list with
  # status 0, and ship-mode `tests-js` and `typecheck-js` printed
  # "skipped: no package.json found" for a push that carries one. The whole
  # point of this function is that an unreadable answer must not empty a list,
  # and it was emptying its own.
  local _nw_lt_rc=0
  _nw_head="$(ci::common::ls_tree_paths -r --full-tree --name-only HEAD)" || _nw_lt_rc=$?
  if [ "$_nw_lt_rc" -ne 0 ]; then
    {
      echo "Cannot list the ${manifest} files in the commit being pushed."
      if [ "$_nw_lt_rc" -eq 2 ]; then
        echo "  A path in HEAD carries a newline, which this listing cannot hold"
        echo "  without splitting it into two entries that match nothing."
      fi
      echo "  Reporting no workspace would pass the push having checked none of"
      echo "  it, so this run says it cannot check it instead."
    } >&2
    return 1
  fi
  while IFS= read -r _nw_found; do
    [ -n "$_nw_found" ] || continue
    case "$_nw_found" in
      "$manifest") _nw_dir="." ;;
      *"/$manifest") _nw_dir="${_nw_found%"/$manifest"}" ;;
      *) continue ;;
    esac
    if [ "$_nw_dir" != "." ] && ci::common::is_vendored_path "$_nw_dir"; then
      continue
    fi
    # An `if`, not `[ -f ] && continue`: the latter is a statement whose status
    # is the test's, so on the last iteration it decides the loop's.
    if [ -f "$_nw_found" ]; then continue; fi
    _nw_missing="${_nw_missing} ${_nw_found}"
  done <<< "$_nw_head"

  [ -n "$_nw_missing" ] || return 0
  {
    echo "These ${manifest} files are in the commit being pushed but not in this"
    echo "  worktree:${_nw_missing}"
    echo "  The lane cannot install, typecheck or test a directory that is not"
    echo "  there, and reporting no workspace would pass the push having checked"
    echo "  none of it. Restore the worktree, or push from one that matches HEAD."
  } >&2
  return 1
}

ci::common::node_workspaces() {
  local manifest="${1:-package.json}"
  local found dir

  # Before the root-manifest branch below, because that branch returns.
  ci::common::_head_manifests_present "$manifest" || return 1

  # A root manifest that the push does not carry is not this run's root: ship
  # mode falls through to the walk below, which skips it for the same reason.
  if [ -f "$manifest" ] && ci::common::_manifest_in_scope "$manifest"; then
    # A root manifest used to end discovery here, so a repository with both a
    # root package.json and child packages had every child skipped: the lane
    # ran the root's scripts and exited 0 while packages/app's failing test and
    # build were never invoked.
    #
    # Emitting the children instead is not the fix. In an npm/pnpm/yarn/bun
    # workspaces monorepo only the root carries a lockfile, and this lane
    # refuses to install a workspace that has none ("No lockfile detected") --
    # so unconditional child discovery would turn every real monorepo red.
    #
    # Neither reading is safe to guess, so the ambiguity is reported. In a
    # repository laid out the way this one is (no root manifest) nothing here
    # changes; in a single-package repo with no children, nothing changes
    # either.
    # Through a file, for the reason spelled out at the head of the other
    # branch: `< <(find ... | sort)` is a subshell whose status is discarded, so
    # a find that fails on an unreadable subtree delivered partial or empty
    # output and this loop reported its own success.
    #
    # The cure was written for the no-root-manifest branch below and not applied
    # here, and this branch is where it fails *open*: child_count stays 0, the
    # ambiguity below is never reported, and the function returns "." and
    # success. Its three callers -- node.sh, tests.sh, typecheck.sh -- each
    # treat a non-zero return as FAIL_INFRA on the stated grounds that a lane
    # cannot report on a set of workspaces it could not determine; handed "." and
    # a success, they install, typecheck, test and build the root alone and exit
    # 0, which is the exact harm the ambiguity report above exists to prevent.
    local child_count=0
    local _rw_raw _rw_list _rw_rc=0
    _rw_raw="$(mktemp 2>/dev/null)" || {
      echo "Cannot create a temporary file to enumerate workspaces." >&2
      return 1
    }
    _rw_list="$(mktemp 2>/dev/null)" || {
      rm -f "$_rw_raw"
      echo "Cannot create a temporary file to enumerate workspaces." >&2
      return 1
    }
    find . -name 'node_modules' -prune -o -name '.git' -prune -o \
      -type f -name "$manifest" -print 2>/dev/null > "$_rw_raw" || _rw_rc=$?
    if [ "$_rw_rc" -eq 0 ]; then
      sort < "$_rw_raw" > "$_rw_list" || _rw_rc=$?
    fi
    rm -f "$_rw_raw"
    if [ "$_rw_rc" -ne 0 ]; then
      rm -f "$_rw_list"
      echo "Cannot enumerate ${manifest} files in this repository (exit ${_rw_rc})." >&2
      echo "  A root ${manifest} with no readable children reads exactly like a" >&2
      echo "  single-package repository, which is why this is a failure and not" >&2
      echo "  an answer." >&2
      return 1
    fi
    while IFS= read -r found; do
      [ -n "$found" ] || continue
      found="${found#./}"
      [ "$found" = "$manifest" ] && continue
      dir="${found%"/$manifest"}"
      [ "$dir" != "$found" ] || continue
      ci::common::is_vendored_path "$dir" && continue
      # Before the ambiguity is counted, not after: an untracked child manifest
      # made this branch report a monorepo it is not and return FAIL_INFRA, so
      # local WIP failed the push through the ambiguity report rather than
      # through the lane.
      ci::common::_manifest_in_scope "$found" || continue
      child_count=$((child_count + 1))
      printf '  %s\n' "$dir" >&2
    done < "$_rw_list"
    rm -f "$_rw_list"
    if [ "$child_count" -gt 0 ]; then
      {
        echo "A root ${manifest} coexists with ${child_count} nested one(s), listed above."
        echo "  Running only the root silently skips their scripts; running them"
        echo "  separately fails on the lockfile a workspaces monorepo keeps only"
        echo "  at the root. Declare which this is before the lane can cover it."
      } >&2
      return 1
    fi
    printf '%s\n' "."
    return 0
  fi

  # Below the root, search to any depth rather than the immediate children.
  #
  # Callers that decide *scheduling* rather than execution must agree with this
  # about what a workspace is — see ci::common::is_vendored_path, which both
  # sides consult — or a lane gets scheduled for a package it will never run,
  # or run against a directory nobody meant as a package.
  #
  # One level was enough for this repository's own layout and for nothing else.
  # ci::changeset::_in_node_workspace walks *up* from a changed file until it
  # finds a manifest, so for packages/app/src/x.ts it says "yes, node lane" —
  # and then discovery, looking only at the immediate children, found no
  # workspace at all and node.sh printed "No package.json found" and exited 0.
  # Scheduling and execution have to agree about what a workspace is, or the
  # lane is scheduled for a package it will never run.
  #
  # Pruned at dependency and build directories: a manifest under node_modules
  # belongs to a dependency, and one under dist/ or build/ is copied output.
  # Neither is a workspace this gate installs, typechecks or tests.
  # Path surgery with shell parameter expansion, not sed and grep with the
  # manifest name interpolated into a pattern. `package.json` as a regex makes
  # every `.` a wildcard, so `grep -v "^package.json$"` also drops a directory
  # genuinely named `package-json` or `packagexjson` — a workspace skipped
  # because its name resembles the sentinel being filtered. `${found%"/$manifest"}`
  # is a literal suffix strip and cannot do that.
  local candidate
  local _cands=() _n=0 _i=0 _j=0 _nested=0 _vendored=0
  # Through a file, so the enumeration's own status is something this function
  # can act on. `< <(find ... | sort)` is a subshell: a `find` that fails on an
  # unreadable subtree, or a `sort` that cannot allocate, delivered partial or
  # empty output and the loop reported its own success. This repository has no
  # root manifest, so an empty list here is "no package.json found" and the node
  # lane passes having run nothing -- an unreadable directory silently removing
  # every frontend check. The same shape, and the same cure, as config_sources
  # in test-layout.sh and the path collector in git-safety.sh.
  #
  # Two files and two statuses rather than one pipeline, because a pipeline's
  # status is the last command's unless `pipefail` is set -- and `sort` succeeds
  # perfectly well on nothing. Sourcing this file happens to turn `pipefail` on,
  # which is not a thing the caller should have to know: the producer is asked
  # directly instead, the way the tsc lane and the marker scan already do it.
  local _nw_raw _nw_list _nw_rc=0
  _nw_raw="$(mktemp 2>/dev/null)" || {
    echo "Cannot create a temporary file to enumerate workspaces." >&2
    return 1
  }
  _nw_list="$(mktemp 2>/dev/null)" || {
    rm -f "$_nw_raw"
    echo "Cannot create a temporary file to enumerate workspaces." >&2
    return 1
  }
  find . -name 'node_modules' -prune -o -name '.git' -prune -o \
    -type f -name "$manifest" -print 2>/dev/null > "$_nw_raw" || _nw_rc=$?
  if [ "$_nw_rc" -eq 0 ]; then
    sort -u < "$_nw_raw" > "$_nw_list" || _nw_rc=$?
  fi
  rm -f "$_nw_raw"
  if [ "$_nw_rc" -ne 0 ]; then
    rm -f "$_nw_list"
    echo "Cannot enumerate ${manifest} files in this repository (exit ${_nw_rc})." >&2
    echo "  An empty list reads exactly like a repository with no workspaces," >&2
    echo "  which is why this is a failure and not an answer." >&2
    return 1
  fi
  while IFS= read -r found; do
    [ -n "$found" ] || continue
    found="${found#./}"
    # The root manifest is handled above; anything without a directory prefix
    # here is that same file and is not a nested workspace.
    [ "$found" = "$manifest" ] && continue
    candidate="${found%"/$manifest"}"
    [ "$candidate" != "$found" ] || continue
    if ci::common::is_vendored_path "$candidate"; then
      _vendored=$((_vendored + 1))
      continue
    fi
    # Before the nesting check below, for the reason the root branch filters
    # before its own: an untracked manifest under a real workspace reports a
    # nested monorepo and fails the lane, without ever being pushed.
    ci::common::_manifest_in_scope "$found" || continue
    _cands[_n]="$candidate"
    _n=$((_n + 1))
  done < "$_nw_list"
  rm -f "$_nw_list"

  # "Every manifest was pruned" and "there are no manifests" leave this function
  # looking identical -- empty stdout, exit 0 -- and the callers print the same
  # "skipped: no package.json found" for both. The second sentence is true; the
  # first is a repository whose only manifests sit under a name this predicate
  # calls build output, whose tests, typecheck and build are then skipped
  # entirely by a lane that reports it found nothing at all.
  #
  # Said out loud rather than guessed at. Which names should count as output is
  # a policy question this function does not own -- ci/checks/git-safety.sh
  # blocks the same `*/build/*`, `*/dist/*`, `*/coverage/*` and `*/htmlcov/*`
  # paths from being pushed, so the two rules currently agree and narrowing one
  # alone would make a workspace the node lane runs and the push gate refuses.
  # Reporting is what turns a silent skip into a visible one without taking that
  # decision here.
  if [ "$_n" -eq 0 ] && [ "$_vendored" -gt 0 ]; then
    echo "Found ${_vendored} ${manifest} file(s), all under paths treated as vendored" >&2
    echo "  or build output (node_modules, dist, build, coverage, htmlcov, .venv," >&2
    echo "  fixtures). No workspace is reported, so this lane runs nothing here." >&2
    echo "  If one of those is first-party source, it needs a name this gate does" >&2
    echo "  not read as build output -- see ci::common::is_vendored_path." >&2
  fi

  # A manifest nested under another manifest is the same ambiguity the root
  # branch reports, and it was only ever asked about the repository root. A
  # repository with no root manifest but a subtree that is itself a monorepo
  # root -- frontend/package.json beside frontend/packages/app/package.json --
  # had both emitted as independent workspaces, and the lane then entered the
  # child and returned FAIL_INFRA, because a workspaces monorepo keeps its
  # lockfile only at its own root. Neither reading is safe to guess here for
  # exactly the reason it is not safe to guess there, so it is reported.
  _i=0
  while [ "$_i" -lt "$_n" ]; do
    _j=0
    while [ "$_j" -lt "$_n" ]; do
      if [ "$_i" -ne "$_j" ]; then
        case "${_cands[_i]}" in
          "${_cands[_j]}"/*)
            printf '  %s (nested under %s)\n' "${_cands[_i]}" "${_cands[_j]}" >&2
            _nested=$((_nested + 1))
            ;;
        esac
      fi
      _j=$((_j + 1))
    done
    _i=$((_i + 1))
  done
  if [ "$_nested" -gt 0 ]; then
    {
      echo "A ${manifest} coexists with ${_nested} nested one(s), listed above."
      echo "  Running only the parent silently skips their scripts; running them"
      echo "  separately fails on the lockfile a workspaces monorepo keeps only"
      echo "  at its own root. Declare which this is before the lane can cover it."
    } >&2
    return 1
  fi

  _i=0
  while [ "$_i" -lt "$_n" ]; do
    printf '%s\n' "${_cands[_i]}"
    _i=$((_i + 1))
  done
  return 0
}

# ci::common::is_vendored_path <path> – 0 when the path is inside something no
# gate lane should treat as first-party source.
#
# The single definition both workspace discovery and changeset scheduling read.
# They had separate ideas of what a workspace is: discovery looked one level
# down, scheduling walked up from any depth, and packages/app/ was scheduled but
# never run. Recursive discovery fixes that half and creates the other one —
# ci/tests/fixtures/node/package.json is a real manifest with no lockfile, and
# treating it as a workspace made the lane refuse the install and report
# FAIL_INFRA for a directory that exists to be a test fixture.
#
# Build output and dependency trees are pruned by name because those names are
# universal. A fixture directory is pruned only *under a test tree*, so a
# genuine packages/fixtures/ workspace is still discovered.
ci::common::is_vendored_path() {
  case "/$1/" in
    */node_modules/* | */.git/* \
      | */dist/* | */build/* | */coverage/* | */htmlcov/* \
      | */.next/* | */.turbo/* | */.vite/* \
      | */.venv/* | */venv/* \
      | */tests/fixtures/* | */test/fixtures/* \
      | */__fixtures__/* | */testdata/*)
      return 0
      ;;
  esac
  return 1
}

# The runtime an install would be made under, as one word-safe string.
#
# A dependency fingerprint keyed on the lockfile and the manifest says what was
# asked for and not what was built. Native addons, optional packages and
# install-script output are compiled against the Node ABI and for a platform and
# architecture, so switching between two releases that both satisfy a broad
# `engines.node` range left an existing node_modules looking current: the lane
# skipped the install and validated a tree that a clean install would not
# produce. The version, the platform and the architecture are the three things
# that change what gets built, so they are the three things in here.
#
# In one place because both the lane and its test fixtures have to agree about
# it -- a fixture that derives the fingerprint differently seeds a value the
# lane will never compute, and every case would then attempt a real install.
ci::common::node_runtime_id() {
  if ci::common::command_exists node; then
    if node -e 'process.stdout.write(process.version+"-"+process.platform+"-"+process.arch)' 2>/dev/null; then
      return 0
    fi
  fi
  # No node is a statement too, and a stable one: nothing can have been built
  # under a runtime that is not there.
  printf 'node-unavailable'
}

ci::common::hash_file() {
  local file="$1"
  local hash crc
  if ci::common::command_exists sha256sum; then
    sha256sum "$file" | cut -d' ' -f1
  elif ci::common::command_exists shasum; then
    shasum -a 256 "$file" | cut -d' ' -f1
  elif ci::common::command_exists openssl; then
    openssl dgst -sha256 "$file" | sed 's/^.*= //'
  elif ci::common::command_exists md5sum; then
    hash="$(md5sum "$file" | cut -d' ' -f1)"
    printf '%064s\n' "$hash" | tr ' ' 0
  elif ci::common::command_exists cksum; then
    crc="$(cksum "$file" | awk '{print $1}')"
    printf '%064x\n' "$crc"
  else
    echo "No supported hashing tool found." >&2
    return 1
  fi
}

ci::common::result_name() {
  case "${1:-999}" in
    "$CI_RESULT_PASS") echo "PASS" ;;
    "$CI_RESULT_PASS_WITH_KNOWN_DEBT") echo "PASS_WITH_KNOWN_DEBT" ;;
    "$CI_RESULT_FAIL_NEW_ISSUE") echo "FAIL_NEW_ISSUE" ;;
    "$CI_RESULT_FAIL_INFRA") echo "FAIL_INFRA" ;;
    "$CI_RESULT_HELP") echo "HELP" ;;
    *) echo "FAIL_INFRA" ;;
  esac
}

ci::common::result_severity() {
  case "${1:-999}" in
    "$CI_RESULT_PASS") echo 0 ;;
    "$CI_RESULT_PASS_WITH_KNOWN_DEBT") echo 1 ;;
    "$CI_RESULT_FAIL_NEW_ISSUE") echo 2 ;;
    "$CI_RESULT_FAIL_INFRA") echo 3 ;;
    "$CI_RESULT_HELP") echo 0 ;;
    *) echo 3 ;;
  esac
}

# Map a child process's exit code onto the result contract before merging it.
# result_severity ranks anything it does not recognise at the FAIL_INFRA level,
# so a command that failed on its own terms — a test runner exiting 1 — would
# otherwise be reported as broken infrastructure rather than as a new issue.
# The four contract codes pass through untouched.
ci::common::normalize_result() {
  case "${1:-$CI_RESULT_PASS}" in
    "$CI_RESULT_PASS" | "$CI_RESULT_PASS_WITH_KNOWN_DEBT" | "$CI_RESULT_FAIL_NEW_ISSUE" | "$CI_RESULT_FAIL_INFRA")
      printf '%s' "$1"
      ;;
    *)
      printf '%s' "$CI_RESULT_FAIL_NEW_ISSUE"
      ;;
  esac
}

ci::common::merge_results() {
  local current="${1:-$CI_RESULT_PASS}"
  local next="${2:-$CI_RESULT_PASS}"
  local s_current s_next
  s_current="$(ci::common::result_severity "$current")"
  s_next="$(ci::common::result_severity "$next")"
  if [ "$s_next" -gt "$s_current" ]; then
    echo "$next"
  else
    echo "$current"
  fi
}

ci::common::parse_mode() {
  local mode="full"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --mode)
        if [ "$#" -lt 2 ]; then
          echo "Missing value for --mode" >&2
          return "$CI_RESULT_FAIL_INFRA"
        fi
        mode="$2"
        shift 2
        ;;
      --mode=*)
        mode="${1#--mode=}"
        if [ -z "$mode" ]; then
          echo "Missing value for --mode" >&2
          return "$CI_RESULT_FAIL_INFRA"
        fi
        shift
        ;;
      --help|-h)
        echo "Usage: ./ci/preflight.sh [--mode quick|full|ship|debt]"
        return "$CI_RESULT_HELP"
        ;;
      *)
        echo "Unknown argument: $1" >&2
        return "$CI_RESULT_FAIL_INFRA"
        ;;
    esac
  done

  case "$mode" in
    quick|full|ship|debt)
      echo "$mode"
      ;;
    *)
      echo "Invalid mode: $mode" >&2
      return "$CI_RESULT_FAIL_INFRA"
      ;;
  esac
}

ci::common::now_iso_utc() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

ci::common::is_unsigned_int() {
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

ci::common::warn_non_numeric_config() {
  printf 'Ignoring non-numeric gate config %s: %s\n' "$1" "$2" >&2
}

# ci::common::load_gate_config <file> – simple grep-based YAML parser for gate.yml
ci::common::load_gate_config() {
  local config_file="${1:-ci/config/gate.yml}"
  [ -f "$config_file" ] || return 0
  local key val trimmed
  while IFS= read -r line; do
    line="$(printf '%s' "$line" | sed 's/\r$//')"
    trimmed="$(printf '%s' "$line" | sed 's/^[[:space:]]*//')"
    case "$trimmed" in
      ''|'#'*) continue ;;
    esac
    key="${line%%:*}"
    val="${line#*:}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    val="$(printf '%s' "$val" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    case "$key" in
      parallelism)
        if [ -n "$val" ]; then
          if ci::common::is_unsigned_int "$val"; then
            [ "$val" != "0" ] && export CI_GATE_PARALLEL="$val"
          else
            ci::common::warn_non_numeric_config "$key" "$val"
          fi
        fi
        ;;
      default_timeout_sec)
        if [ -n "$val" ]; then
          if ci::common::is_unsigned_int "$val"; then
            export CI_GATE_TIMEOUT="$val"
          else
            ci::common::warn_non_numeric_config "$key" "$val"
          fi
        fi
        ;;
      pre_commit_budget_sec)
        if [ -n "$val" ]; then
          if ci::common::is_unsigned_int "$val"; then
            export CI_GATE_PRE_COMMIT_BUDGET="$val"
          else
            ci::common::warn_non_numeric_config "$key" "$val"
          fi
        fi
        ;;
      pre_push_budget_sec)
        if [ -n "$val" ]; then
          if ci::common::is_unsigned_int "$val"; then
            export CI_GATE_PRE_PUSH_BUDGET="$val"
          else
            ci::common::warn_non_numeric_config "$key" "$val"
          fi
        fi
        ;;
      fail_fast_on_blocker)
        if [ "$val" = "true" ]; then export CI_GATE_FAIL_FAST=1; else export CI_GATE_FAIL_FAST=0; fi
        ;;
      incremental)
        if [ "$val" = "true" ]; then export CI_GATE_INCREMENTAL=1; else export CI_GATE_INCREMENTAL=0; fi
        ;;
      cache_enabled)
        if [ "$val" = "true" ]; then export CI_GATE_CACHE_ENABLED=1; else export CI_GATE_CACHE_ENABLED=0; fi
        ;;
      verbose)
        if [ "$val" = "true" ]; then export CI_GATE_VERBOSE=1; else export CI_GATE_VERBOSE=0; fi
        ;;
    esac
  done < "$config_file"
}

# ci::common::load_thresholds_config – simple parser for thresholds.yml
ci::common::load_thresholds_config() {
  local config_file="${1:-ci/config/thresholds.yml}"
  [ -f "$config_file" ] || return 0
  local key val trimmed
  while IFS= read -r line; do
    line="$(printf '%s' "$line" | sed 's/\r$//')"
    trimmed="$(printf '%s' "$line" | sed 's/^[[:space:]]*//')"
    case "$trimmed" in
      ''|'#'*) continue ;;
    esac
    key="${line%%:*}"
    val="${line#*:}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    val="$(printf '%s' "$val" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    case "$key" in
      coverage_min_percent)
        if [ -n "$val" ]; then
          if ci::common::is_unsigned_int "$val"; then
            export CI_GATE_COVERAGE_MIN="$val"
          else
            ci::common::warn_non_numeric_config "$key" "$val"
          fi
        fi
        ;;
      complexity_max)
        if [ -n "$val" ]; then
          if ci::common::is_unsigned_int "$val"; then
            export CI_GATE_COMPLEXITY_MAX="$val"
          else
            ci::common::warn_non_numeric_config "$key" "$val"
          fi
        fi
        ;;
      bundle_size_max)
        if [ -n "$val" ]; then
          if ci::common::is_unsigned_int "$val"; then
            export CI_GATE_BUNDLE_SIZE_MAX="$val"
          else
            ci::common::warn_non_numeric_config "$key" "$val"
          fi
        fi
        ;;
    esac
  done < "$config_file"
}
