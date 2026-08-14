#!/usr/bin/env bash
# ci/checks/typecheck.sh – Multi-language type-check dispatcher.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"

cd "$ROOT_DIR"

OVERALL_RESULT=$CI_RESULT_PASS

_tc_tool_missing() {
  ci::log::info "skipped: ${1} not installed"
}

# ---------------------------------------------------------------------------
# Per-language typecheck functions
# ---------------------------------------------------------------------------

# Use the workspace's own tsc and nothing else. A globally resolved binary can
# be a different major version than the one the lockfile pins, and npx in
# particular will walk out of the workspace to find one.
# Returns 127 when the workspace-local tsc is not available.
_tc_js_workspace() {
  local ws="$1"
  cd "$ws" || return 30

  # Every TypeScript project under this root, not only one at the root itself.
  #
  # `[ ! -f tsconfig.json ]` read "does this package have TypeScript" as "is
  # there a config beside package.json", so a package whose only project is
  # `e2e/tsconfig.json` -- an ordinary shape -- was reported as having none and
  # its type errors were never looked for. That blind spot arrived with the
  # switch to package-root discovery, which was right for the ambiguity rule it
  # removed and wrong to keep the root-file test alongside it.
  #
  # Through a file so the producer's status is something this can act on: an
  # unreadable subtree must not read as a package with no TypeScript in it.
  local _tc_list _tc_rc=0
  _tc_list="$(mktemp 2>/dev/null)" || return 30
  # The same name list as _ts_project_files in ci/checks/node.sh, which decides
  # whether this workspace is *required* to have a typecheck script. When the
  # two disagree the gate demands a check of a project this lane then declines
  # to compile, or -- as it did -- neither of them sees `tsconfig.app.json` and
  # a Vite scaffold's only project is checked by nobody. ci/lib/changeset.sh's
  # classifier arm is the third, and node.sh's orphan scan -- which decides
  # whether a directory that lost its manifest is noticed at all -- is the
  # fourth; all four have to agree.
  find . -name 'node_modules' -prune -o -name '.git' -prune -o \
    -type f \( -name 'tsconfig.json' -o -name 'tsconfig.*.json' \
               -o -name 'jsconfig.json' -o -name 'jsconfig.*.json' \) \
    -print 2>/dev/null > "$_tc_list" || _tc_rc=$?
  if [ "$_tc_rc" -ne 0 ]; then
    rm -f "$_tc_list"
    echo "Cannot enumerate the TypeScript projects in ${ws} (exit ${_tc_rc})." >&2
    echo "  An empty list reads exactly like a package with none." >&2
    return 30
  fi
  # Which tree this list describes. `find` stats the worktree, and in ship mode
  # the worktree is the commit being built, not the one being pushed -- so an
  # untracked `local-only/tsconfig.json` was compiled as part of a push that
  # will never carry it, and a tracked project deleted locally was skipped by a
  # push that still does. The same narrowing ci::common::node_workspaces applies
  # to manifests, one layer down, through the reader they share: a fifth answer
  # to "is this file part of the push" is what the comment above is warning
  # about.
  if [ "${CI_GATE_MODE:-}" = "ship" ] \
    && command -v git >/dev/null 2>&1 \
    && git rev-parse --git-dir >/dev/null 2>&1 \
    && git rev-parse --verify HEAD >/dev/null 2>&1; then
    # Two spellings of the same location, and they are not interchangeable.
    # `HEAD:<dir>` names a tree and is what ls-tree takes; a single file is
    # `HEAD:<dir>/<file>`. Built as one rev with a second colon it is not a path
    # at all -- `git cat-file -e HEAD:frontend:tsconfig.json` is fatal, every
    # project failed the test, and the narrowing emptied a list it was only
    # supposed to filter. That reports "no TypeScript project" for a workspace
    # that has one, which is the false pass this block exists to prevent.
    local _tc_rev="HEAD" _tc_pfx="" _tc_keep _tc_p _tc_head _tc_missing=""
    if [ "$ws" != "." ]; then
      _tc_rev="HEAD:${ws}"
      _tc_pfx="${ws}/"
    fi
    _tc_keep="$(mktemp 2>/dev/null)" || { rm -f "$_tc_list"; return 30; }
    while IFS= read -r _tc_p; do
      [ -n "$_tc_p" ] || continue
      if git cat-file -e "HEAD:${_tc_pfx}${_tc_p#./}" 2>/dev/null; then
        printf '%s\n' "$_tc_p" >> "$_tc_keep"
      fi
    done < "$_tc_list"
    mv -f "$_tc_keep" "$_tc_list" 2>/dev/null || { rm -f "$_tc_keep" "$_tc_list"; return 30; }

    # And the direction narrowing cannot reach: a project the push carries that
    # is not on disk. Reporting "no TypeScript project" for it would pass the
    # push having compiled none of it, so this run says it cannot check it.
    # `--full-tree`, because this runs after `cd "$ws"` and `git ls-tree`
    # applies the current directory as an implicit path filter. From inside
    # frontend/, listing the tree `HEAD:frontend` -- whose entries are
    # `package.json` and `tsconfig.json` -- filters them against the prefix
    # `frontend/`, matches nothing, and hands back an empty listing. Empty reads
    # exactly like "the push carries no projects", so the missing-project
    # refusal below never fired and the lane reported a skip instead.
    if _tc_head="$(git ls-tree -r --full-tree --name-only "$_tc_rev" 2>/dev/null)"; then
      while IFS= read -r _tc_p; do
        case "$_tc_p" in
          tsconfig.json|tsconfig.*.json|jsconfig.json|jsconfig.*.json \
            |*/tsconfig.json|*/tsconfig.*.json|*/jsconfig.json|*/jsconfig.*.json) ;;
          *) continue ;;
        esac
        case "$_tc_p" in */node_modules/*|node_modules/*) continue ;; esac
        # `[ -f x ] && continue` is a statement whose status is the test's, and
        # this file runs under `set -Eeuo pipefail`: on the last iteration a
        # present file makes it 0 and an absent one makes the loop's status 1.
        # Spelled as an `if` so the check cannot decide the function's exit code.
        if [ -f "$_tc_p" ]; then continue; fi
        _tc_missing="${_tc_missing} ${_tc_p}"
      done <<< "$_tc_head"
    fi
    if [ -n "$_tc_missing" ]; then
      rm -f "$_tc_list"
      echo "These TypeScript projects are in the commit being pushed but not in" >&2
      echo "  ${ws}:${_tc_missing}" >&2
      echo "  Reporting none would pass the push having compiled none of it." >&2
      return 30
    fi
  fi

  if [ ! -s "$_tc_list" ]; then
    rm -f "$_tc_list"
    # A package root with no TypeScript project has nothing for this lane to
    # check, and that is a fact about the workspace rather than a guess about
    # it. Distinguished from "the compiler is missing" below, which is the
    # opposite statement: a project exists and could not be compiled.
    return 3
  fi

  local bin="" candidate
  for candidate in node_modules/.bin/tsc node_modules/.bin/tsc.exe node_modules/.bin/tsc.cmd; do
    if [ -x "$candidate" ]; then
      bin="$candidate"
      break
    fi
  done
  if [ -z "$bin" ]; then
    rm -f "$_tc_list"
    # Deliberately no PATH fallback, for the same reason the JS test runner has
    # none: a global tsc is not the version this workspace pins. An older one
    # rejects supported syntax and a newer one accepts what the locked
    # toolchain would refuse, so either way the result is not reproducible.
    # Reporting the compiler unavailable is recoverable; a false red or a false
    # green is not.
    return 127
  fi

  # The root project by its default resolution, and every other project by name.
  #
  # A nested config is compiled as its own project rather than assumed to be
  # reachable from the root: `tsc --noEmit` at the root compiles what the root
  # config includes, and a config the root neither includes nor references is
  # exactly the one whose errors nobody would see. Compiling a referenced
  # project twice costs time and reports the same errors twice; not compiling an
  # unreferenced one reports none at all, and only one of those is a wrong
  # answer.
  local _tc_proj _tc_status=0 _tc_one
  if [ -f tsconfig.json ]; then
    "$bin" --noEmit || _tc_status=$?
  fi
  while IFS= read -r _tc_proj; do
    [ -n "$_tc_proj" ] || continue
    _tc_proj="${_tc_proj#./}"
    [ "$_tc_proj" = "tsconfig.json" ] && continue
    _tc_one=0
    "$bin" -p "$_tc_proj" --noEmit || _tc_one=$?
    [ "$_tc_one" -ne 0 ] && _tc_status="$_tc_one"
  done < "$_tc_list"
  rm -f "$_tc_list"
  return "$_tc_status"
}

typecheck::run_js() {
  local workspaces _ws_rc=0
  # Package roots, not every tsconfig.json in the tree.
  #
  # A nested `frontend/e2e/tsconfig.json` extending its parent is ordinary
  # TypeScript -- project references and per-area configs are how the language
  # is used -- and asking node_workspaces about tsconfig.json applied the
  # package-manager ambiguity rule to it: two configs became "a workspace with a
  # nested workspace", which is a statement about lockfiles and means nothing
  # here. ci/checks/node.sh already treats the same layout as ordinary.
  #
  # A package root is the unit that owns a compiler and a tsconfig, which is
  # exactly what this lane runs, so it is the unit to discover. A root with no
  # tsconfig.json is reported as having no TypeScript project rather than
  # skipped silently.
  #
  # And the status is captured rather than left to `set -e`. The substitution
  # below aborts this script outright on a non-zero producer -- raw exit 1,
  # outside the 0/10/20/30 contract, before the Python, Go and Rust typechecks
  # have run -- so an unreadable tree or an ambiguous layout took the whole lane
  # down with a message that named neither.
  workspaces="$(ci::common::node_workspaces package.json)" || _ws_rc=$?
  if [ "$_ws_rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_INFRA")"
    ci::log::error "Could not enumerate JavaScript workspaces (exit ${_ws_rc}); see above."
    ci::log::error "  This lane cannot report on a set of workspaces it could not determine."
    return 0
  fi

  if [ -z "$workspaces" ]; then
    ci::log::info "skipped: no package.json found"
    return 0
  fi

  # Narrowing the project list to HEAD chose *which* files are compiled and
  # said nothing about their *contents*. This lane runs the workspace's own tsc
  # over the worktree, so a type error committed in HEAD and fixed only on disk
  # compiled clean and the push carrying it was reported PASS.
  #
  # ci/checks/node.sh has refused this since "Workspace files differ between
  # HEAD and the worktree", and typecheck-js never asked because it is
  # scheduled as its own blocker lane and does not go through node.sh. Asked
  # here through the shared reader rather than a second copy of the comparison.
  if [ "${CI_GATE_MODE:-}" = "ship" ]; then
    local _tc_ws
    while IFS= read -r _tc_ws; do
      [ -n "$_tc_ws" ] || continue
      ci::common::refuse_on_workspace_drift "$_tc_ws"
    done <<< "$workspaces"
  fi

  local ws rc
  while IFS= read -r ws; do
    [ -n "$ws" ] || continue

    rc=0
    # Subshell so the cd cannot leak into later languages.
    ( _tc_js_workspace "$ws" ) || rc=$?

    if [ "$rc" -eq 3 ]; then
      ci::log::info "skipped: no tsconfig.json in ${ws}"
      continue
    fi

    if [ "$rc" -eq 127 ]; then
      # A detected TypeScript workspace with no compiler is broken
      # infrastructure, not a skip. Reaching here means ci::common::node_workspaces
      # found a tsconfig.json and this lane was scheduled for it, so "no tsc"
      # does not mean "nothing to check" -- it means the project that exists
      # could not be checked, and logging a skip left OVERALL_RESULT at PASS.
      # An uninstalled node_modules then took an enabled typecheck-js lane green
      # over a tree nothing compiled.
      #
      # The same rule ci/checks/tests.sh applies to a missing workspace-local
      # test runner, and ci/checks/tests-shell.sh to a missing bats: an enabled
      # blocker that cannot find its tool has not passed.
      OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_INFRA")"
      ci::log::error "No workspace-local tsc in ${ws}/node_modules/.bin"
      ci::log::error "  (a global tsc is deliberately not used: it is not the version this"
      ci::log::error "  workspace pins). This workspace declares tsconfig.json and was"
      ci::log::error "  scheduled, so reporting PASS here would mean its types were never"
      ci::log::error "  checked. Install its dependencies, or remove the workspace."
      continue
    fi

    if [ "$rc" -ne 0 ]; then
      ci::log::error "tsc --noEmit failed in ${ws} (exit ${rc})"
      OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
    else
      ci::log::info "tsc --noEmit passed in ${ws}"
    fi
  done <<< "$workspaces"

  return 0
}

typecheck::run_python() {
  if ! ci::common::command_exists mypy && ! ci::common::command_exists pyright; then
    _tc_tool_missing "mypy/pyright"
    return 0
  fi
  ci::log::info "Running Python type checker..."
  local rc=0
  if ci::common::command_exists mypy; then
    mypy . --ignore-missing-imports || rc=$?
  else
    pyright . || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

typecheck::run_go() {
  if ! ci::common::command_exists go; then
    _tc_tool_missing go
    return 0
  fi
  if [ ! -f go.mod ]; then
    ci::log::info "skipped: no go.mod found"
    return 0
  fi
  ci::log::info "Running go vet ./..."
  local rc=0
  go vet ./... || rc=$?
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

typecheck::run_rust() {
  if ! ci::common::command_exists cargo; then
    _tc_tool_missing cargo
    return 0
  fi
  if [ ! -f Cargo.toml ]; then
    ci::log::info "skipped: no Cargo.toml found"
    return 0
  fi
  ci::log::info "Running cargo check..."
  local rc=0
  cargo check || rc=$?
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

_typecheck_main() {
  ci::log::section "Check: typecheck"

  local check_id="${CI_GATE_CHECK_ID:-all}"

  case "$check_id" in
    typecheck-js)     typecheck::run_js ;;
    typecheck-python) typecheck::run_python ;;
    typecheck-go)     typecheck::run_go ;;
    typecheck-rust)   typecheck::run_rust ;;
    all|*)
      typecheck::run_js
      typecheck::run_python
      typecheck::run_go
      typecheck::run_rust
      ;;
  esac

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "Typecheck result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" = "${0}" ]]; then
  _typecheck_main "$@"
fi
