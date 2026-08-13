#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/git.sh
source "$ROOT_DIR/ci/lib/git.sh"

cd "$ROOT_DIR"

ci::common::section "Check: node lane"

# Before anything is discovered, installed or run: this lane executes scripts
# out of the worktree, so if the worktree is not the commit being pushed it can
# only report on the wrong tree. Checked here as well as in preflight.sh because
# the lane is invoked directly too, and both call the one helper rather than
# each carrying its own copy of the comparison.
if [ "${CI_GATE_MODE:-}" = "ship" ] && ! ci::git::worktree_covers_push; then
  ci::git::explain_push_tip_drift
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

# The lane body below handles exactly one workspace. When invoked normally we
# discover the workspaces and re-enter once per directory; CI_GATE_NODE_WORKSPACE
# marks that recursive call. A repo with a root package.json resolves to "."
# and behaves exactly as before.
if [ -z "${CI_GATE_NODE_WORKSPACE:-}" ]; then
  # Which tree the workspaces come from is the gate's question, the same as
  # everywhere else in this file.
  #
  # ci::common::node_workspaces stats the filesystem, and in ship mode its
  # result was unioned with HEAD's rather than replaced by it -- so an untracked
  # zz_review_pkg/package.json entered the list, and the HEAD manifest check
  # below then failed the push for a workspace the pushed commit does not
  # contain and git will not send. Sixth reader of this one question, after the
  # orphan scan, the manifest walk, and three in ci/checks/test-layout.sh.
  #
  # The lib is left alone: it is shared with tests.sh and typecheck.sh, and its
  # root-manifest ambiguity refusal is a statement about the filesystem. The
  # source is switched here, and the same ambiguity is asked of HEAD's list
  # below, so ship mode neither inherits the worktree's layout nor loses the
  # rule.
  _ws_ship=0
  if [ "${CI_GATE_MODE:-}" = "ship" ] \
    && command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1 \
    && git rev-parse --verify HEAD >/dev/null 2>&1; then
    _ws_ship=1
  fi

  # Discovery can now refuse: a root manifest coexisting with nested ones is a
  # layout it cannot cover without guessing, and under `set -e` the bare
  # assignment would abort the script with status 1 rather than say so.
  NODE_WORKSPACES=""
  if [ "$_ws_ship" -eq 0 ]; then
    _ws_rc=0
    NODE_WORKSPACES="$(ci::common::node_workspaces package.json)" || _ws_rc=$?
    if [ "$_ws_rc" -ne 0 ]; then
      echo "Workspace discovery could not resolve this layout; see above."
      exit "$CI_RESULT_FAIL_INFRA"
    fi
  fi

  # Discovery stats the filesystem, so a workspace that exists only in the index
  # is invisible to it: stage a new added/package.json whose test script fails,
  # then remove added/ from disk, and the lane never looks at it. The index is a
  # second source of workspaces, and one that describes the commit rather than
  # the tree.
  #
  # To any depth, matching discovery. `NF == 2` covered one level only, so a
  # staged packages/app/package.json was dropped here exactly as it was dropped
  # by the filesystem walk before that was made recursive — the two sources have
  # to agree about what a workspace is or the lane covers the intersection.
  #
  # Pruned by the same rule the filesystem walk uses. Discovery drops
  # ci/tests/fixtures/node via ci::common::is_vendored_path; this scan added it
  # straight back, and being alphabetically first it was the workspace the lane
  # entered before any real one. It has no lockfile, so `CI_GATE_MODE=full bash
  # ci/checks/node.sh` exited FAIL_INFRA on a fixture and never reached
  # frontend. Two sources of workspaces have to agree about what a workspace is
  # in *both* directions -- the last round taught this scan to see as deep as
  # discovery, and left it seeing more than discovery too.
  if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    # The listing status is taken before its output is shaped. `|| true` on the
    # pipeline turned an unreadable index into "no workspaces are staged", so a
    # workspace present only in the index -- the case this scan exists for --
    # disappeared and the lane printed "No package.json found" and exited 0,
    # never reaching its failing test script.
    # And *which* second source depends on the gate.
    #
    # The index is the commit being made; HEAD is the commit being pushed. This
    # read the index in every mode, which is wrong twice over in ship mode. A
    # workspace staged for a later commit joined the list, and the HEAD manifest
    # check below then reported the pushed commit as missing a manifest for a
    # directory that push has nothing to do with -- a clean HEAD refused because
    # of work staged for next time. And a workspace that exists only in HEAD was
    # contributed by nobody: filesystem discovery cannot see it, the index no
    # longer lists it, and the ship drift scan iterates the workspaces this list
    # already contains -- so its test, typecheck and build scripts were never
    # run and never reported on, which is the fail-open half of the same line.
    #
    # ci/checks/test-layout.sh answers this for its own inputs by switching the
    # source rather than widening it, and node.sh already switches to `HEAD:`
    # for the manifest check one screen below.
    _idx_rc=0
    if [ "$_ws_ship" -eq 1 ]; then
      # ls-tree takes no glob pathspec, so the tree is listed and filtered. The
      # listing's status is taken before the filter runs: grep exits 1 when it
      # matches nothing, which is an answer, and the enumeration failing is not.
      _idx_head=""
      _idx_head="$(git ls-tree -r --name-only HEAD 2>/dev/null)" || _idx_rc=$?
      if [ "$_idx_rc" -eq 0 ]; then
        _idx_raw="$(printf '%s\n' "$_idx_head" | grep -E '(^|/)package\.json$' || true)"
      fi
    else
      _idx_raw="$(git ls-files -- 'package.json' '*/package.json' 2>/dev/null)" || _idx_rc=$?
    fi
    if [ "$_idx_rc" -ne 0 ]; then
      echo "Cannot read the tree this run stands behind to find its workspaces."
      echo "  Refusing to conclude there are none from a listing that failed to"
      echo "  produce one."
      exit "$CI_RESULT_FAIL_INFRA"
    fi
    INDEXED_WS="$(printf '%s\n' "$_idx_raw" \
      | sed '/^$/d' \
      | awk -F/ 'NF == 1 { print "."; next }
                 { d = $1; for (i = 2; i < NF; i++) d = d "/" $i; print d }' \
      | sort -u || true)"
    if [ -n "$INDEXED_WS" ]; then
      _kept_ws=""
      while IFS= read -r _iw; do
        [ -n "$_iw" ] || continue
        ci::common::is_vendored_path "$_iw" && continue
        _kept_ws="${_kept_ws}${_iw}"$'\n'
      done <<< "$INDEXED_WS"
      INDEXED_WS="$(printf '%s' "$_kept_ws")"
    fi
    if [ "$_ws_ship" -eq 1 ]; then
      # Replaced, not unioned. See the comment at the discovery call above.
      NODE_WORKSPACES="$INDEXED_WS"
      # The ambiguity the lib refuses on the filesystem, asked of the pushed
      # tree: a root manifest beside nested ones is a layout this lane cannot
      # cover without guessing which one owns the lockfile, and guessing is what
      # made it skip every child package. Dropping the worktree source must not
      # drop the rule with it.
      _amb_root=0 _amb_child=0
      while IFS= read -r _aw; do
        [ -n "$_aw" ] || continue
        if [ "$_aw" = "." ]; then _amb_root=1; else _amb_child=1; fi
      done <<< "$NODE_WORKSPACES"
      if [ "$_amb_root" -eq 1 ] && [ "$_amb_child" -eq 1 ]; then
        echo "The pushed commit carries a root package.json and nested ones:"
        while IFS= read -r _aw; do
          [ -n "$_aw" ] || continue
          [ "$_aw" = "." ] && continue
          echo "    ${_aw}"
        done <<< "$NODE_WORKSPACES"
        echo "  Only the root carries a lockfile in a workspaces monorepo, and"
        echo "  this lane refuses to install a workspace without one -- so which"
        echo "  of the two readings applies cannot be settled from here."
        echo "  Workspace discovery could not resolve this layout."
        exit "$CI_RESULT_FAIL_INFRA"
      fi
    elif [ -n "$INDEXED_WS" ]; then
      NODE_WORKSPACES="$(printf '%s\n%s\n' "$NODE_WORKSPACES" "$INDEXED_WS" \
        | sed '/^$/d' | sort -u)"
    fi
  elif [ "$_ws_ship" -eq 1 ]; then
    # Ship mode with no readable git: there is no pushed tree to discover from,
    # and the worktree is not a stand-in for it.
    echo "Cannot read the tree this run stands behind to find its workspaces."
    exit "$CI_RESULT_FAIL_INFRA"
  fi

  # "No manifest" and "no JavaScript here" are not the same statement. Deleting
  # frontend/package.json leaves the lockfile, the tsconfig and the vitest
  # config behind; discovery then finds nothing there, and a commit that makes
  # the workspace uninstallable ran no install, typecheck, test or build.
  #
  # Keyed on workspace *configuration*, not on source files: a lockfile or a
  # tsconfig/vite/vitest config with no manifest beside it is unambiguous
  # evidence of a removed manifest, whereas a stray .js in a non-JS repo is not.
  #
  # Runs for every directory, not only when the repository has no workspace left
  # at all. Nesting it under that condition made the scan a property of the
  # *repository*: with two sibling workspaces, deleting b/package.json still
  # left a, discovery succeeded, and b -- lockfile and all -- was never looked
  # at. An orphan is a property of the directory it sits in.
  # The TypeScript names here are the ones _ts_project_files enumerates, and
  # they have to stay that way: this scan is what turns "no manifest" into "a
  # workspace lost its manifest", so a name it does not know is a directory
  # whose loss it cannot notice. `tsconfig.json|jsconfig.json` alone was the
  # list before discovery widened, which left the `npm create vite` shape --
  # tsconfig.app.json and tsconfig.node.json, no plain tsconfig.json -- as a
  # workspace that could lose its package.json silently. Found by sweeping the
  # other readers of this list rather than reported.
  #
  # The pushed tree in ship mode, and *only* it -- not a union with the
  # worktree.
  #
  # Walking the filesystem here was the whole scan, which missed the fault it
  # exists to catch: delete frontend/package.json in a commit, then delete the
  # lockfile and the tsconfig on disk only, and push. Discovery finds nothing,
  # this scan finds nothing to be orphaned, and the lane prints "No package.json
  # found" and exits 0 -- install, typecheck, tests and build all skipped for a
  # broken pushed tree. Adding HEAD beside the walk fixed that and opened the
  # other direction: an untracked scratch/tsconfig.json, a local experiment git
  # will not send, failed a ship run over a directory the push has nothing to do
  # with. Both were reported, one round apart, about the same scan.
  #
  # Which tree a run vouches for chooses the source; it does not add to it. The
  # pre-commit gate stands behind the worktree and keeps the walk.
  _oc_ship=0
  if [ "${CI_GATE_MODE:-}" = "ship" ] \
    && command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1 \
    && git rev-parse --verify HEAD >/dev/null 2>&1; then
    _oc_ship=1
  fi

  if [ "$_oc_ship" -eq 1 ]; then
    # The listing's status is taken before the filter, for the reason given
    # above: a tree that could not be listed is not a tree with nothing in it,
    # and this is the scan whose empty answer reads as "nothing was lost".
    _oc_rc=0
    _oc_head="$(git ls-tree -r --name-only HEAD 2>/dev/null)" || _oc_rc=$?
    if [ "$_oc_rc" -ne 0 ]; then
      echo "Cannot read the pushed tree to look for workspaces that lost a manifest."
      echo "  A tree that could not be listed is not a tree with nothing in it."
      exit "$CI_RESULT_FAIL_INFRA"
    fi
    ORPHAN_CFG="$(printf '%s\n' "$_oc_head" \
      | grep -Ev '(^|/)(node_modules|dist|build)/' \
      | grep -E '(^|/)(package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lock|bun\.lockb|tsconfig\.json|tsconfig\.[^/]+\.json|jsconfig\.json|jsconfig\.[^/]+\.json|vitest\.config\.[^/]+|vite\.config\.[^/]+)$' \
      | sed 's|^|./|' || true)"
  else
    ORPHAN_CFG="$(find . \
        \( -name 'node_modules' -o -name '.git' -o -name 'dist' -o -name 'build' \) -prune -o \
        -type f \( -name 'package-lock.json' -o -name 'npm-shrinkwrap.json' \
          -o -name 'pnpm-lock.yaml' -o -name 'yarn.lock' -o -name 'bun.lock' -o -name 'bun.lockb' \
          -o -name 'tsconfig.json' -o -name 'tsconfig.*.json' \
          -o -name 'jsconfig.json' -o -name 'jsconfig.*.json' \
          -o -name 'vitest.config.*' -o -name 'vite.config.*' \) -print 2>/dev/null || true)"
  fi

  ORPHANS=""
  while IFS= read -r _cfg; do
    [ -n "$_cfg" ] || continue
    _cfg_dir="$(dirname "$_cfg")"
    # A manifest settles the question — on disk, or in the tree this run stands
    # behind, because a workspace being added is not an orphan either.
    #
    # Which tree that is depends on the gate, the same as everywhere else here.
    # Asking the index in ship mode gets it wrong in both directions: a manifest
    # staged for a later commit settles the question for a pushed tree that does
    # not carry it, and a manifest that HEAD carries but the index no longer
    # does fails to settle it at all.
    #
    # The disk test in front of it is skipped in ship mode for the same reason,
    # and honestly: no input was found where that changes the verdict. Every
    # state it could have hidden -- a manifest deleted in the commit and
    # restored on disk, or one on disk that .gitignore keeps out of the tree --
    # is caught first by the discovery drift check further down, which exits 20
    # with "a workspace manifest exists on disk but not in the commit being
    # pushed". Both trees exit 20 on all of them; only the message differs.
    #
    # Changed anyway, because a scan that consults two trees is the defect this
    # file keeps producing, and the drift check is a different question that
    # happens to overlap -- not a guarantee this one is entitled to lean on.
    #
    # Looked for up the tree, not only beside it. A nested TypeScript project
    # config is ordinary: `frontend/e2e/tsconfig.json` extending
    # `../tsconfig.json` shares its package manager and dependencies with
    # frontend, and demanding a second package.json next to it made
    # `CI_GATE_MODE=full bash ci/checks/node.sh` exit 20 before reaching the
    # frontend workspace at all. The rule is meant to catch a workspace that
    # *lost* its manifest, and a config with an ancestor workspace has lost
    # nothing.
    #
    # The case it was written for still fails: delete frontend/package.json and
    # the walk from frontend reaches the repository root, which has no manifest
    # either, so the lockfile and tsconfig left behind are still reported.
    _mf_found=0
    _mf_dir="$_cfg_dir"
    while :; do
      if [ "$_oc_ship" -eq 0 ] && [ -f "$_mf_dir/package.json" ]; then _mf_found=1; break; fi
      if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
        _mf_rel="${_mf_dir#./}"
        _mf_ref=":"
        if [ "$_oc_ship" -eq 1 ]; then
          _mf_ref="HEAD:"
        fi
        if [ "$_mf_rel" = "." ] || [ -z "$_mf_rel" ]; then
          git cat-file -e "${_mf_ref}package.json" 2>/dev/null && { _mf_found=1; break; }
        else
          git cat-file -e "${_mf_ref}${_mf_rel}/package.json" 2>/dev/null && { _mf_found=1; break; }
        fi
      fi
      case "$_mf_dir" in
        .|/|"") break ;;
      esac
      _mf_dir="$(dirname "$_mf_dir")"
    done
    [ "$_mf_found" -eq 1 ] && continue
    ORPHANS="${ORPHANS}${_cfg#./}"$'\n'
  done <<< "$ORPHAN_CFG"
  ORPHANS="$(printf '%s' "$ORPHANS" | sed '/^$/d' | head -10)"

  if [ -n "$ORPHANS" ]; then
    echo "Workspace configuration with no package.json beside it:"
    while IFS= read -r _orphan; do
      [ -n "$_orphan" ] || continue
      echo "    $_orphan"
    done <<< "$ORPHANS"
    echo "  Nothing can be installed or run against these, so the lane would"
    echo "  pass having executed no install, typecheck, test or build there."
    echo "  Restore the manifest, or remove the configuration with it."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi

  if [ -z "$NODE_WORKSPACES" ]; then
    echo "No package.json found. Skipping Node lane."
    exit "$CI_RESULT_PASS"
  fi

  # Discovery reads the filesystem, so a staged deletion of package.json with
  # the file restored in the worktree looked like a healthy workspace: every
  # manifest and script check then read that worktree copy, and both pre-commit
  # and pre-push passed for a commit that carries no manifest at all.
  if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    # Which tree is "the commit" depends on the gate.
    #
    # The index is right for pre-commit, where the commit is being assembled in
    # it. It is not the tree a push carries: in ship mode the commit already
    # exists, and the index can hold anything staged for the *next* one. Staging
    # a deletion of frontend/package.json while the worktree copy stays, then
    # pushing an unrelated HEAD that still contains the manifest, failed the
    # push over a tree it is not sending. The drift scan below already reads
    # HEAD in ship mode for exactly this reason; this check is one earlier and
    # was still reading the index.
    _mf_ref=":"
    _mf_what="the git index"
    _mf_says="The commit being made would carry no manifest for that workspace,"
    _mf_fix="Stage it (git add <path>), or restore it if the deletion was staged by mistake."
    if [ "${CI_GATE_MODE:-}" = "ship" ] && git rev-parse --verify HEAD >/dev/null 2>&1; then
      _mf_ref="HEAD:"
      _mf_what="the commit being pushed"
      _mf_says="The commit being pushed carries no manifest for that workspace,"
      _mf_fix="Commit it, or drop the workspace from the tree being pushed."
    fi
    MISSING_FROM_INDEX=""
    while IFS= read -r _ws; do
      [ -n "$_ws" ] || continue
      if [ "$_ws" = "." ]; then _mf="package.json"; else _mf="$_ws/package.json"; fi
      if ! git cat-file -e "${_mf_ref}${_mf}" 2>/dev/null; then
        MISSING_FROM_INDEX="${MISSING_FROM_INDEX}${_mf}"$'\n'
      fi
    done <<< "$NODE_WORKSPACES"

    if [ -n "$MISSING_FROM_INDEX" ]; then
      echo "A workspace manifest exists on disk but not in ${_mf_what}:"
      while IFS= read -r _mf; do
        [ -n "$_mf" ] || continue
        echo "    $_mf"
      done <<< "$MISSING_FROM_INDEX"
      echo "  ${_mf_says}"
      echo "  so nothing there could be installed or run -- while every check"
      echo "  below would read the worktree copy and pass."
      echo "  ${_mf_fix}"
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi

    # The converse: staged into the commit but absent from the tree. The child
    # would find no package.json there, print "Skipping Node lane" and return
    # PASS -- so a workspace the commit adds, with a failing test script, is
    # never run at all.
    MISSING_FROM_DISK=""
    while IFS= read -r _ws; do
      [ -n "$_ws" ] || continue
      if [ "$_ws" = "." ]; then _mf="package.json"; else _mf="$_ws/package.json"; fi
      if git cat-file -e ":$_mf" 2>/dev/null && [ ! -f "$_mf" ]; then
        MISSING_FROM_DISK="${MISSING_FROM_DISK}${_mf}"$'\n'
      fi
    done <<< "$NODE_WORKSPACES"

    if [ -n "$MISSING_FROM_DISK" ]; then
      echo "A workspace manifest is staged but missing from the worktree:"
      while IFS= read -r _mf; do
        [ -n "$_mf" ] || continue
        echo "    $_mf"
      done <<< "$MISSING_FROM_DISK"
      echo "  The commit adds that workspace, but there is nothing on disk to"
      echo "  install or run, so the lane would skip it and pass."
      echo "  Restore the workspace, or unstage it."
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi

    # Existing in the index is not enough, and the manifest is not the only file
    # that matters. Every check below -- engines, packageManager, the lockfile
    # fingerprint, and then the workspace's own test, typecheck and build
    # scripts -- consumes the worktree. Stage a failing source file, restore the
    # passing copy on disk, and the lane reports on code the commit does not
    # contain.
    #
    # The rule is *partial staging*, not any divergence: a file whose index copy
    # differs from HEAD (it is part of this commit) AND whose worktree copy
    # differs from the index. An ordinary dirty worktree -- edited, not yet
    # staged -- is left alone, because there the developer is deliberately
    # testing what is on disk and nothing claims otherwise.
    #
    # That rule is written against the index, which is the right reference for
    # exactly one gate. In ship mode the commit already exists: the index
    # matches HEAD, `git diff --cached` is empty, and the rule never fires --
    # so a workspace file broken in a commit and repaired only on disk sailed
    # through the pre-push gate on the strength of the repair. The tree a run
    # vouches for is the gate's, not the changeset's, so the reference follows
    # CI_GATE_MODE: ship stands behind HEAD, everything else behind the index.
    # The range this push carries, resolved once for the deletion lookup below.
    # A scan that cannot read its reference has not found a clean one. `|| true`
    # made those the same answer, so an unreadable object left the drift list
    # empty and the lane installed, typechecked, tested and built a worktree
    # nothing had compared it against. The sentinel survives the pipeline and is
    # checked before the output is read as a list of paths.
    _SCAN_UNREADABLE="__ci_gate_unreadable__"
    _gate_range=""
    if [ "${CI_GATE_MODE:-}" = "ship" ] && type ci::git::push_range >/dev/null 2>&1; then
      _gate_range="$(ci::git::push_range 2>/dev/null || true)"
    fi
    [ -n "$_gate_range" ] || _gate_range="HEAD"

    PARTIAL=""
    while IFS= read -r _ws; do
      [ -n "$_ws" ] || continue
      _scope="$_ws"
      [ "$_scope" = "." ] && _scope="."

      if [ "${CI_GATE_MODE:-}" = "ship" ]; then
        _drift="$( {
          git -c core.quotepath=false diff --name-only HEAD -- "$_scope" 2>/dev/null || printf '%s\n' "$_SCAN_UNREADABLE"
          git -c core.quotepath=false ls-files --others --exclude-standard -- "$_scope" 2>/dev/null || printf '%s\n' "$_SCAN_UNREADABLE"
          # And the ignored ones — but only where an ignored file *shadows a
          # path this push removes*. That is the whole of the reported defect: a
          # commit deletes a script and adds its path to .gitignore, so HEAD no
          # longer carries the path (`git diff HEAD` is silent) and
          # --exclude-standard drops the replacement, and the lane runs it.
          #
          # An earlier attempt filtered the whole ignored list through a prune
          # list of directory names. That was wrong twice, and both ways were
          # demonstrated rather than argued: the names matched at any depth, so
          # it swallowed a genuine source file under `frontend/src/build/`; and
          # it reported `frontend/.env.local`, a `tsbuildinfo` and Playwright
          # output as drift, which makes the ship gate unpassable during
          # ordinary development — while its printed remedy, "commit the rest",
          # is precisely what git-safety.sh blocks. Two checks, one file,
          # contradictory instructions. Keying on deletions needs no prune list.
          # `if`, not `[ -e ] && printf`. A deletion whose path is genuinely
          # gone leaves the test false, and on the *last* iteration that becomes
          # the status of the loop, then of the command substitution, then of
          # the `_drift` assignment -- and under this script's `set -e` the node
          # lane aborted with a raw status 1 and no diagnostic at all, before
          # install, typecheck, test or build had run. A commit that cleanly
          # deletes a workspace file is the ordinary case, so the gate broke on
          # correct work and did not even say so.
          #
          # The log walk finds candidates; what makes one drift is that the
          # pushed tree does not carry the path. Existing on disk is not
          # enough, because `git log --diff-filter=D` lists every path any
          # commit in the range deleted, including one a later commit in the
          # same push puts back -- and a delete-then-re-add pair then reported
          # the re-added file as drift, exiting 20 before a check ran over a
          # worktree matching HEAD exactly.
          #
          # Asked against HEAD rather than by diffing the range's endpoints,
          # because the range is not always a range: `push_range` returns a bare
          # tip for a genuine first push, and an endpoint diff there compares
          # the worktree to the tip -- a different question with a
          # plausible-looking answer, which silently dropped this whole scan on
          # every push without a remote base. HEAD is the pushed tree:
          # `worktree_covers_push` above has already refused anything else.
          while IFS= read -r _d; do
            [ -n "$_d" ] || continue
            [ -e "$_d" ] || continue
            if ! git cat-file -e "HEAD:$_d" 2>/dev/null; then
              printf '%s\n' "$_d"
            fi
          done <<< "$(git -c core.quotepath=false log --diff-filter=D --name-only --format= "$_gate_range" -- "$_scope" 2>/dev/null || printf '%s\n' "$_SCAN_UNREADABLE")"
        } | sort -u | sed '/^$/d')"
        case "$_drift" in
          *"$_SCAN_UNREADABLE"*)
            echo "Cannot read the tree this push is being compared against."
            echo "  Refusing to report on the worktree alone: a reference that"
            echo "  could not be read is not a reference that matches."
            exit "$CI_RESULT_FAIL_INFRA"
            ;;
        esac
        [ -n "$_drift" ] && PARTIAL="${PARTIAL}${_drift}"$'\n'
        continue
      fi

      _staged="$(git diff --cached --name-only -- "$_scope" 2>/dev/null | sort || printf '%s\n' "$_SCAN_UNREADABLE")"
      [ -n "$_staged" ] || continue

      # Something here is part of this commit, so the whole workspace has to
      # match the index — not merely the staged paths.
      #
      # Restricting the comparison to staged paths made the rule per-file, and
      # the lane is not per-file: it installs, typechecks, tests and builds the
      # workspace as a unit. Stage a test that imports a new helper, leave the
      # helper untracked, and every staged path matched the index while `tsc`
      # passed only because of a file the commit does not contain. Checking out
      # that commit gives an unresolved import.
      #
      # The three lists are one question asked three ways, and each was needed
      # in turn: `git diff` compares tracked content and misses a path staged
      # for deletion and recreated; `--others` finds that one but `.gitignore`
      # hides it; `--ignored` finds it, and finds everything else with it.
      _diverged="$( {
        git -c core.quotepath=false diff --name-only -- "$_scope" 2>/dev/null || printf '%s\n' "$_SCAN_UNREADABLE"
        git -c core.quotepath=false ls-files --others --exclude-standard -- "$_scope" 2>/dev/null || printf '%s\n' "$_SCAN_UNREADABLE"
        # And the ignored ones -- but only where an ignored file *shadows a path
        # this commit removes*, which is the same rule the ship-mode scan above
        # settled on and the same argument for it.
        #
        # This branch still carried the earlier version: the whole ignored list,
        # filtered through a prune list of directory names. That reports
        # `frontend/.env.local`, a tsbuildinfo and Playwright output as
        # divergence, so staging any workspace file at all failed the commit
        # gate over a file the repository deliberately ignores -- and the remedy
        # it printed, stage it or discard it, means committing a secrets file
        # that git-safety.sh then blocks. Two checks in one repository giving
        # contradictory instructions, which is exactly why ship mode stopped
        # doing this. Fixed there and left here: one rule, two trees.
        #
        # In quick mode the index *is* the commit, so a path staged for deletion
        # that still exists on disk is the whole of the case: the commit removes
        # it and the lane would run it. A path deleted and re-added in the index
        # is not filter=D and does not appear.
        while IFS= read -r _d; do
          [ -n "$_d" ] || continue
          if [ -e "$_d" ]; then
            printf '%s\n' "$_d"
          fi
        done <<< "$(git -c core.quotepath=false diff --cached --diff-filter=D --name-only -- "$_scope" 2>/dev/null || printf '%s\n' "$_SCAN_UNREADABLE")"
      } | sort -u | sed '/^$/d')"
      case "$_diverged" in
        *"$_SCAN_UNREADABLE"*)
          echo "Cannot read the index or the worktree for ${_scope}."
          echo "  Refusing to report a clean comparison from a listing that"
          echo "  failed to produce one."
          exit "$CI_RESULT_FAIL_INFRA"
          ;;
      esac
      [ -n "$_diverged" ] && PARTIAL="${PARTIAL}${_diverged}"$'\n'
    done <<< "$NODE_WORKSPACES"
    PARTIAL="$(printf '%s' "$PARTIAL" | sed '/^$/d')"

    if [ -n "$PARTIAL" ]; then
      if [ "${CI_GATE_MODE:-}" = "ship" ]; then
        echo "Workspace files differ between HEAD and the worktree:"
      else
        echo "Workspace files are staged but changed again in the worktree:"
      fi
      while IFS= read -r _p; do
        [ -n "$_p" ] || continue
        echo "    $_p"
      done <<< "$PARTIAL"
      echo "  The lane installs, typechecks, tests and builds the worktree, so"
      if [ "${CI_GATE_MODE:-}" = "ship" ]; then
        echo "  it would report on content the pushed commits do not contain."
        echo "  Commit the rest, stash it, or discard it."
      else
        echo "  it would report on content the commit does not contain."
        echo "  Stage the rest (git add <path>), or discard it."
      fi
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi
  fi

  NODE_OVERALL="$CI_RESULT_PASS"
  while IFS= read -r _ws; do
    [ -n "$_ws" ] || continue
    echo ""
    echo "--- Node lane workspace: ${_ws}"
    _ws_rc=0
    CI_GATE_NODE_WORKSPACE="$_ws" bash "$SCRIPT_DIR/node.sh" "$@" || _ws_rc=$?
    # A failing package script exits 1, which is not a gate result code and
    # would otherwise be merged in at the FAIL_INFRA level — a real test,
    # typecheck or build regression reported as broken infrastructure.
    _ws_rc="$(ci::common::normalize_result "$_ws_rc")"
    NODE_OVERALL="$(ci::common::merge_results "$NODE_OVERALL" "$_ws_rc")"
  done <<< "$NODE_WORKSPACES"

  exit "$NODE_OVERALL"
fi

cd "$ROOT_DIR/$CI_GATE_NODE_WORKSPACE"

if [ ! -f package.json ]; then
  echo "No package.json found in ${CI_GATE_NODE_WORKSPACE}. Skipping Node lane."
  exit "$CI_RESULT_PASS"
fi

# Dependency-cache fingerprints stay at the repo root so enabling a
# subdirectory workspace does not scatter .ci-gate/ dirs through the tree.
if [ "$CI_GATE_NODE_WORKSPACE" = "." ]; then
  WS_SLUG="root"
else
  WS_SLUG="$(printf '%s' "$CI_GATE_NODE_WORKSPACE" | tr '/' '-')"
fi
NODE_HASH_FILE="$ROOT_DIR/.ci-gate/node_modules-${WS_SLUG}.hash"

if ! ci::common::command_exists node; then
  echo "package.json exists but node is not installed."
  exit "$CI_RESULT_FAIL_INFRA"
fi

MANAGER=""
LOCKFILE=""
LOCKFILE_COUNT=0

if [ -f pnpm-lock.yaml ]; then
  MANAGER="pnpm"
  LOCKFILE="${LOCKFILE:-pnpm-lock.yaml}"
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi
if [ -f package-lock.json ] || [ -f npm-shrinkwrap.json ]; then
  MANAGER="${MANAGER:-npm}"
  if [ -z "$LOCKFILE" ]; then
    if [ -f npm-shrinkwrap.json ]; then
      LOCKFILE="npm-shrinkwrap.json"
    else
      LOCKFILE="package-lock.json"
    fi
  fi
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi
if [ -f yarn.lock ]; then
  MANAGER="${MANAGER:-yarn}"
  LOCKFILE="${LOCKFILE:-yarn.lock}"
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi
if [ -f bun.lockb ] || [ -f bun.lock ]; then
  MANAGER="${MANAGER:-bun}"
  if [ -z "$LOCKFILE" ]; then
    if [ -f bun.lock ]; then
      LOCKFILE="bun.lock"
    else
      LOCKFILE="bun.lockb"
    fi
  fi
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi

if [ "$LOCKFILE_COUNT" -gt 1 ]; then
  echo "Multiple lockfiles detected. Resolve package-manager ambiguity first."
  exit "$CI_RESULT_FAIL_INFRA"
fi

if [ -z "$MANAGER" ]; then
  echo "No lockfile detected for package.json. Refusing mutable install."
  exit "$CI_RESULT_FAIL_INFRA"
fi

# ---- Declared toolchain ------------------------------------------------------
#
# The workspace manifest states the toolchain its results are reproducible
# under. Accepting whatever `node` and the package manager happen to resolve to
# means a green lane vouches for nothing: the same commit can pass here and
# fail on a conforming machine, or the reverse, and neither result is evidence.
# Both assertions read the workspace's own package.json, so a manifest that
# declares nothing is unaffected; a manifest whose range cannot be evaluated is
# reported rather than assumed to hold.

# _manifest_field <dotted.path> – echo the value, or nothing when absent.
# node is already a hard requirement above and parses its own manifest format
# on any version, so this stays correct even while the version is in question.
_manifest_field() {
  node -e '
    const fs = require("fs");
    const pkg = JSON.parse(fs.readFileSync("package.json", "utf8"));
    const val = process.argv[1]
      .split(".")
      .reduce((o, k) => (o === null || o === undefined ? o : o[k]), pkg);
    if (val !== null && val !== undefined) process.stdout.write(String(val));
  ' "$1" 2>/dev/null || true
}

# _semver_part <version> <1|2|3> – echo one numeric component, defaulting to 0.
# An absent component really is 0: `cut -d. -f2` would echo the whole string for
# a version like "23", making ">=22.12.0 <23" accept 23.1.0.
_semver_part() {
  printf '%s' "${1#v}" | awk -v i="$2" '{
    v = $0
    sub(/[-+].*$/, "", v)
    n = split(v, p, ".")
    x = (i <= n ? p[i] : "0")
    gsub(/[^0-9]/, "", x)
    print (x == "" ? "0" : x)
  }'
}

# _semver_spec_part <range-token> <1|2|3> – echo the component AS WRITTEN, or
# nothing when the token does not state it. Deliberately distinct from
# _semver_part: for a *version* an absent component is 0, but for a *range* it
# is unconstrained, so "20" must admit 20.20.2 the way npm's X-ranges do.
_semver_spec_part() {
  printf '%s' "${1#v}" | awk -v i="$2" '{
    v = $0
    sub(/[-+].*$/, "", v)
    n = split(v, p, ".")
    print (i <= n ? p[i] : "")
  }'
}

# _semver_is_version <string> – 0 when the string is a usable semver operand.
#
# The component parser strips non-numeric text and defaults to 0, which is right
# for a runtime that reports "v20.20.2" but catastrophic for a range: ">=banana"
# and a bare ">=" both parsed as >=0.0.0 and admitted every version there is,
# switching the whole toolchain boundary off. Operands are checked before they
# reach the comparator so a malformed one is reported as unverifiable instead.
# The whole grammar, not just the first character and the character set:
# ">=20banana", ">=20..1" and ">=20.1.2.3" all begin with a digit and carry only
# legal characters, and node-semver rejects every one of them. Checking the
# shape piecemeal let each of those through as >=20.0.0.
# A numeric identifier is `0` or a digit string with no leading zero, and it has
# to stay inside the range shell arithmetic can hold. Both matter here and both
# were `[0-9]+`:
#   * `020` is octal to `$(( 020 + 1 ))` (17) and decimal to `[ 020 -gt 20 ]`, so
#     _semver_upper_bound and _semver_cmp read the same operand as two different
#     numbers; `08` aborts the arithmetic outright, and the empty result is
#     indistinguishable from "all three components stated", which silently
#     reverts <= and > to the comparison the bound exists to replace.
#   * 20 digits wraps: _semver_upper_bound 99999999999999999999 produced
#     7766279631452241920, and >= against it came back satisfied for Node 20.
# node-semver rejects both outright, which is the behaviour worth copying.
# The ceiling is node-semver's, which is Number.MAX_SAFE_INTEGER —
# 9007199254740991, sixteen digits. A flat 15-digit cap was neither that
# boundary nor a shell one (bash counts to 9223372036854775807, nineteen
# digits): it rejected the exact value node-semver documents and accepts, and
# because an oversized operand is classed *malformed* rather than merely
# unsupported, it poisoned every sibling — ">=20 || >=9007199254740991" stopped
# the lane over a digit count in an alternative npm reads fine, while the first
# alternative plainly admitted the runtime. Sixteen digits is well inside
# bash arithmetic, so the real boundary can simply be compared.
_semver_num_ok() {
  case "$1" in
    0) return 0 ;;
    ''|*[!0-9]*|0*) return 1 ;;
  esac
  case "${#1}" in
    [1-9]|1[0-5]) return 0 ;;
    16) [ "$1" -le 9007199254740991 ] ;;
    *) return 1 ;;
  esac
}

# node-semver's PRERELEASEIDENTIFIER and BUILDIDENTIFIER, which are not the
# loose `[0-9A-Za-z.-]+` this used to accept. That looseness admitted a
# leading-zero numeric identifier and empty identifiers — "1.2.3-01",
# "1.2.3-a..b", "1.2.3+." — every one of which node-semver refuses to parse.
# Unlike the cases above this one failed *open*: "1.2.3-01 || >=20" came back
# SATISFIED against a range npm will not construct at all.
_SEMVER_PRE_ID='(0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)'
_SEMVER_PRE="(-${_SEMVER_PRE_ID}([.]${_SEMVER_PRE_ID})*)?"
_SEMVER_BUILD='([+][0-9A-Za-z-]+([.][0-9A-Za-z-]+)*)?'

# _semver_is_version <string> [x_order_ok] – 0 when the string is a usable
# semver operand. Pass a non-empty second argument to permit a stated component
# to the right of a wildcard.
#
# That second argument exists because invalidXRangeOrder is not a property of
# the operand, it is a property of where the operand sits. node-semver calls it
# from replaceXRange only, so "20.x.3" throws on its own and after any of
# = >= > < <=, and is accepted after ^ or ~ (replaceTilde and replaceCaret
# rewrite the operand before any X-range pass sees it) and as a hyphen endpoint
# (hyphenReplace never routes through it). Verified against semver 7.8.5:
# ~20.x.3, ^20.x.3 and "20.x.3 - 22" all construct; 20.x.3 and >=20.x.3 throw.
# Applying the rule to every operand made the gate refuse "~22.x.1", a range
# every published version of node-semver accepts.
_semver_is_version() {
  local s="${1#v}" x_order_ok="${2:-}" core i part seen_wildcard=0
  case "$s" in *' '*|'') return 1 ;; esac
  core="${s%%[-+]*}"
  # The grammar first: shape, characters, component count.
  printf '%s' "$s" | grep -Eq \
    "^([0-9]+|[xX*])([.]([0-9]+|[xX*]))?([.]([0-9]+|[xX*]))?${_SEMVER_PRE}${_SEMVER_BUILD}\$" \
    || return 1
  # A prerelease may only attach to a fully stated operand. node-semver's
  # XRANGEPLAIN puts the prerelease inside the third component's group, so it
  # is reachable only once all three are present -- and this holds whether they
  # are numbers or wildcards. Measured on 7.8.5: `20.1.2-alpha`, `20.x.x-alpha`
  # and `20.1.x-alpha` construct; `20-alpha`, `20.1-alpha`, `20.x-alpha` and
  # `x-alpha` all throw, under every operator. Without this, "^20.x-alpha"
  # passed the grammar, truncated at the wildcard to a bare "20", and came back
  # SATISFIED for a range npm will not build -- the same fail-open the
  # identifier grammar above closes, one component to the left. Build metadata
  # carries no such rule: "20+b" and "20.x+b" are both fine.
  if _semver_has_prerelease "$s"; then
    case "$core" in
      *.*.*) ;;
      *) return 1 ;;
    esac
  fi
  # Then each component, which the pattern above cannot express.
  for i in 1 2 3; do
    part="$(printf '%s' "$core" | cut -d. -f"$i" -s)"
    [ "$i" -eq 1 ] && part="$(printf '%s' "$core" | cut -d. -f1)"
    [ -n "$part" ] || break
    case "$part" in
      x|X|'*') seen_wildcard=1 ; continue ;;
    esac
    # A stated number to the right of a wildcard is an invalid X-range order.
    # node-semver's invalidXRangeOrder rejects "20.x.3" outright rather than
    # reading it as "20.x.x" -- it aborts the X-range rewrite and the untouched
    # comparator then throws. This gate accepted it and evaluated a range npm
    # will not install against.
    if [ "$seen_wildcard" -eq 1 ] && [ -z "$x_order_ok" ]; then return 1; fi
    _semver_num_ok "$part" || return 1
  done
  return 0
}

# _semver_has_prerelease <string> – 0 when the operand or version carries one.
#
# This comparator implements numeric precedence over major.minor.patch and
# nothing else, and prerelease precedence is a genuinely different ordering:
# 1.2.3-alpha.1 is *below* 1.2.3, and alpha.7 is above alpha.3 by identifier
# rules, not by any comparison of the three numbers. Ignoring the tail did not
# make those cases unsupported, it made them wrong in the fail-open direction —
# measured against semver 7.8.5, `1.2.3-alpha.1` came back satisfied by 1.2.3
# and by 1.2.3-alpha.2, `<=1.2.3-alpha.1` by 1.2.3, and a plain `20.1.0` by a
# 20.1.0-rc.1 runtime. Every one of those is npm=false, bash=satisfied.
#
# So it is declared unsupported instead, on either side of the comparison, and
# an unsupported form stops the lane rather than passing it. A sibling
# alternative that plainly admits the runtime still wins, exactly as for a
# hyphen range.
_semver_has_prerelease() {
  local s="${1#v}"
  s="${s%%+*}"
  case "$s" in *-*) return 0 ;; esac
  return 1
}

# _semver_hyphen_endpoint_ok <string> – 0 when the string may sit either side of
# the `-` in a hyphen range.
#
# node-semver's XRANGEPLAIN starts `[v=\s]*`, so an endpoint absorbs a redundant
# leading `=` the way an operator's operand does, and hyphenReplace applies no
# X-range order rule. Reusing the bare-operand predicate here rejected "= 20 -
# 22", "20.x.3 - 22" and 16-digit endpoints — all three accepted by 7.8.5 — and
# a missed hyphen range is classed malformed, which then poisons the sibling
# alternative that the hyphen handling exists to let win.
_semver_hyphen_endpoint_ok() {
  local s="${1#v}"
  _semver_is_version "${s#=}" x_order_ok
}

# _semver_is_concrete_version <string> – 0 when the string is a real version, as
# opposed to a range operand: three numbers, no wildcards.
#
# Nothing validated the *runtime* at all. `_semver_part` strips non-digits and
# defaults to 0, so "banana" became 0.0.0; and an empty string gives awk no
# record at all, so it printed nothing, `[ "" -gt "20" ]` errored, and every
# component compared equal — reporting a match. `node --version` emitting a shim
# banner, a wrapper's noise, or nothing at all therefore turned a `>=` gate into
# a pass, which is the one failure that defeats the whole check.
_semver_is_concrete_version() {
  local s="${1#v}" core i part
  case "$s" in *' '*|'') return 1 ;; esac
  core="${s%%[-+]*}"
  printf '%s' "$s" | grep -Eq \
    "^[0-9]+[.][0-9]+[.][0-9]+${_SEMVER_PRE}${_SEMVER_BUILD}\$" || return 1
  for i in 1 2 3; do
    part="$(printf '%s' "$core" | cut -d. -f"$i")"
    _semver_num_ok "$part" || return 1
  done
  return 0
}

# _semver_upper_bound <operand> – echo the version one past everything a partial
# operand covers, or nothing when the operand states all three components.
#
# node-semver reads a partial as an X-range, so "20" spans all of major 20 and
# "20.1" all of minor 20.1. Both comparators that care about the top of that
# span were comparing against the zero-filled floor instead: "<=20" rejected
# 20.20.2, and ">20" admitted it. The bound is the same value for both.
_semver_upper_bound() {
  local stated=0 i
  for i in 1 2 3; do
    case "$(_semver_spec_part "$1" "$i")" in
      ''|'x'|'X'|'*') break ;;
    esac
    stated="$i"
  done
  [ "$stated" -eq 0 ] && return 0
  [ "$stated" -ge 3 ] && return 0
  case "$stated" in
    1) printf '%s.0.0' "$(( $(_semver_part "$1" 1) + 1 ))" ;;
    2) printf '%s.%s.0' "$(_semver_part "$1" 1)" "$(( $(_semver_part "$1" 2) + 1 ))" ;;
  esac
}

# _semver_spec_states <operand> <i> – 0 when the operand states component i as a
# number.
#
# "20.x" states a minor textually but as a wildcard, so the tilde and caret
# rules must not pin against it: ~20.x is the whole of major 20, exactly as ~20
# is. Testing for non-empty rather than numeric compared the runtime's minor
# against 0 and rejected a conforming version.
_semver_spec_states() {
  case "$(_semver_spec_part "$1" "$2")" in
    ''|*[!0-9]*) return 1 ;;
  esac
  return 0
}

# _semver_truncate_wildcard <operand> – echo the operand with every component
# from the first wildcard onwards removed; echo it unchanged when it has none.
#
# node-semver reads any X in an operand as making everything to its right an X
# too ("we know patch is an x, because we have any x at all"), so "20.*.3" is
# "20.x.x" and ">=20.*.3" is ">=20.0.0". _semver_part strips the "*" to 0 but
# keeps the stated 3, so the comparison ran against 20.0.3 and rejected 20.0.1 —
# a runtime the range admits. _semver_upper_bound already stopped at the first
# wildcard, which is why "<=20.*.3" was right and ">=20.*.3" was not; truncating
# the operand once makes every comparator read the same range.
_semver_truncate_wildcard() {
  local i part out=""
  for i in 1 2 3; do
    part="$(_semver_spec_part "$1" "$i")"
    case "$part" in
      '') break ;;
      'x'|'X'|'*')
        # A wildcard *major* states nothing at all, and the empty string is not
        # a usable operand: "=x" came back malformed, when node-semver reads it
        # as "*". Left as written, so the callers that ask whether a component
        # is stated still get an answer.
        [ "$i" -eq 1 ] && break
        printf '%s' "$out"
        return 0
        ;;
    esac
    out="${out:+$out.}$part"
  done
  printf '%s' "$1"
}

# _semver_cmp <a> <b> – echo -1, 0 or 1. Pre-release suffixes are dropped: a
# gate that accepted 22.12.0-rc for a >=22.12.0 requirement would be lying by a
# hair, but the check exists to catch whole-version drift, not tag splitting.
_semver_cmp() {
  local i av bv
  for i in 1 2 3; do
    av="$(_semver_part "$1" "$i")"
    bv="$(_semver_part "$2" "$i")"
    if [ "$av" -gt "$bv" ]; then printf '1'; return 0; fi
    if [ "$av" -lt "$bv" ]; then printf -- '-1'; return 0; fi
  done
  printf '0'
}

# _semver_token_ok <version> <token> – 0 satisfied, 1 unsatisfied,
# 2 unsupported range form, 3 invalid operand.
#
# The last two are different failures and must not share a status. An exotic but
# VALID form -- a hyphen range -- is something this comparator does not
# implement, and a sibling alternative that plainly admits the runtime should
# still win. A malformed operand is a typo in the manifest, and no sibling
# should rescue it: ">=20banana || >=20" came back satisfied while
# _semver_is_version was rejecting the first operand outright.
_semver_token_ok() {
  local version="$1" tok="$2" want i spec
  case "$tok" in
    '*'|'x'|'X'|'')
      return 0
      ;;
    # A multi-component all-wildcard token is the same range written longer:
    # `x.x`, `*.*.*`, `vx`, `v*`. The fast path above matched only the exact
    # one-character spellings, and the bare-version branch below demands a
    # leading digit, so these fell through to the catch-all and stopped the
    # lane on a range npm reads as "any version".
    [xX*]|v[xX*]|[xX*].*|v[xX*].*)
      case "$(printf '%s' "${tok#v}" | tr -d 'xX*.')" in
        '') return 0 ;;
      esac
      ;;
    '>='*) want="${tok#>=}" ;;
    '<='*) want="${tok#<=}" ;;
    '>'*)  want="${tok#>}" ;;
    '<'*)  want="${tok#<}" ;;
    '^'*)  want="${tok#^}" ;;
    # LONETILDE is `~>?`, so `~>20` is `~20` — Ruby's spelling, which npm
    # accepts. Stripping only the tilde left `>20`, which the grammar check
    # then rejected, and a valid range came back unverifiable.
    '~>'*) want="${tok#\~>}" ;;
    '~'*)  want="${tok#\~}" ;;
    '='*)  want="${tok#=}" ;;
    *)     want="" ;;
  esac

  # An operator with a malformed operand is unverifiable, not satisfied.
  # _semver_part strips non-numeric text and defaults to 0, so ">=banana" and a
  # bare ">=" both became >=0.0.0 and admitted everything -- the boundary
  # switched off by a typo in a manifest.
  # node-semver's XRANGEPLAIN begins `[v=\s]*`, so it absorbs a redundant `=`
  # on the operand: `>==20` is `>=20`. Stripping only `>=` left `=20`, which the
  # grammar check rejected.
  case "$tok" in
    '>='*|'<='*|'>'*|'<'*|'^'*|'~'*) want="${want#=}" ;;
  esac

  case "$tok" in
    # ^ and ~ rewrite their operand before any X-range pass, so an X-range
    # order that throws bare is legal after them. See _semver_is_version.
    '^'*|'~'*)
      if ! _semver_is_version "$want" x_order_ok; then return 3; fi
      want="$(_semver_truncate_wildcard "$want")"
      ;;
    '>='*|'<='*|'>'*|'<'*|'='*)
      if ! _semver_is_version "$want"; then return 3; fi
      # Checked for grammar first, then normalised: "20..1" must still be
      # rejected rather than truncated into something legal.
      want="$(_semver_truncate_wildcard "$want")"
      ;;
  esac

  # Valid, and not something this comparator can order. Deliberately after the
  # grammar check, and only on the operator paths -- a bare operand is not
  # validated until its own branch below, and testing it here classed the
  # malformed "1.2.3-01" as merely unsupported, which is sibling-rescuable:
  # "1.2.3-01 || >=20" came back satisfied for a range npm refuses to build.
  case "$tok" in
    '>='*|'<='*|'>'*|'<'*|'^'*|'~'*|'='*)
      _semver_has_prerelease "$want" && return 2
      ;;
  esac

  # node-semver resolves an X-range with an unstated major before any comparator
  # sees it: ">=x" and "<=x" become "*", "^x" and "~x" become "*", and ">x" and
  # "<x" become "<0.0.0-0", which nothing satisfies. Falling through to the
  # numeric path read the wildcard as 0 instead and got four of the six
  # backwards -- "<=x", "^x" and "~x" rejected every runtime, and ">x" admitted
  # every one. Bare "x" is not listed because the X-range branch below already
  # returns 0 for it, which is the same answer.
  if ! _semver_spec_states "$want" 1; then
    case "$tok" in
      '>='*|'<='*|'^'*|'~'*) return 0 ;;
      '>'*|'<'*)             return 1 ;;
    esac
  fi

  case "$tok" in
    '>='*)
      # >=20 is >=20.0.0: zero-filling is already the right reading.
      if [ "$(_semver_cmp "$version" "$want")" != "-1" ]; then return 0; fi
      return 1
      ;;
    '<='*)
      # <=20 admits every 20.x: node-semver expands it to <21.0.0-0. Comparing
      # against a zero-filled 20.0.0 rejected 20.20.2, a conforming runtime.
      if [ -n "$(_semver_upper_bound "$want")" ]; then
        if [ "$(_semver_cmp "$version" "$(_semver_upper_bound "$want")")" = "-1" ]; then return 0; fi
        return 1
      fi
      if [ "$(_semver_cmp "$version" "$want")" != "1" ]; then return 0; fi
      return 1
      ;;
    '>'*)
      # >20 requires 21 or later; against a zero-filled 20.0.0 it wrongly
      # admitted 20.20.2. The bound is the same one <= uses, from the other side.
      if [ -n "$(_semver_upper_bound "$want")" ]; then
        if [ "$(_semver_cmp "$version" "$(_semver_upper_bound "$want")")" != "-1" ]; then return 0; fi
        return 1
      fi
      if [ "$(_semver_cmp "$version" "$want")" = "1" ]; then return 0; fi
      return 1
      ;;
    '<'*)
      # <20 is <20.0.0: zero-filling is already the right reading.
      if [ "$(_semver_cmp "$version" "$want")" = "-1" ]; then return 0; fi
      return 1
      ;;
    '^'*)
      if [ "$(_semver_cmp "$version" "$want")" = "-1" ]; then return 1; fi
      if [ "$(_semver_part "$version" 1)" != "$(_semver_part "$want" 1)" ]; then return 1; fi
      # Below 1.0.0 the minor carries the breaking change, so ^0.2.3 admits
      # 0.2.x only. Comparing majors alone would accept 0.3.0 -- but only when
      # the operand states a minor: bare "^0" is >=0.0.0 <1.0.0.
      if [ "$(_semver_part "$want" 1)" = "0" ] \
        && _semver_spec_states "$want" 2 \
        && [ "$(_semver_part "$version" 2)" != "$(_semver_part "$want" 2)" ]; then
        return 1
      fi
      # The rule recurses one level further down: "^0.0.3" is ">=0.0.3 <0.0.4",
      # because below 0.1.0 the patch is what carries the breaking change.
      # Constraining the minor alone admitted 0.0.9 for a range that means
      # 0.0.3 and nothing else.
      if [ "$(_semver_part "$want" 1)" = "0" ] \
        && _semver_spec_states "$want" 2 && [ "$(_semver_part "$want" 2)" = "0" ] \
        && _semver_spec_states "$want" 3 \
        && [ "$(_semver_part "$version" 3)" != "$(_semver_part "$want" 3)" ]; then
        return 1
      fi
      return 0
      ;;
    '~'*)
      if [ "$(_semver_cmp "$version" "$want")" = "-1" ]; then return 1; fi
      if [ "$(_semver_part "$version" 1)" != "$(_semver_part "$want" 1)" ]; then return 1; fi
      # "~20" is the whole of major 20; only "~20.1" pins the minor. Comparing
      # unconditionally read the omitted minor as 0 and rejected 20.20.2, and
      # then "~20.x" -- a minor stated as a wildcard -- did the same thing.
      if _semver_spec_states "$want" 2 \
        && [ "$(_semver_part "$version" 2)" != "$(_semver_part "$want" 2)" ]; then
        return 1
      fi
      return 0
      ;;
    '='*|[0-9]*|v[0-9]*)
      # An X-range: compare only the components the range actually states. "22"
      # means >=22.0.0 <23.0.0 and "22.x" the same, so an unstated component
      # ends the comparison rather than defaulting to 0 — reading "20" as
      # "20.0.0" rejected Node 20.20.2, a conforming runtime.
      #
      # "=20" is the same range. Routing it through exact comparison instead
      # reintroduced the defaulting this branch exists to avoid, for the one
      # spelling that makes the intent explicit.
      case "$tok" in '='*) spec="$want" ;; *) spec="$tok" ;; esac
      # A bare operand needs the same grammar check as an operator's: "20banana"
      # starts with a digit, so it reached the loop, matched on the major and
      # returned satisfied on the unstated minor.
      if ! _semver_is_version "$spec"; then return 3; fi
      # Grammar first, then orderability: see the operator paths above.
      _semver_has_prerelease "$spec" && return 2
      for i in 1 2 3; do
        case "$(_semver_spec_part "$spec" "$i")" in
          ''|'x'|'X'|'*') return 0 ;;
        esac
        if [ "$(_semver_part "$version" "$i")" != "$(_semver_part "$spec" "$i")" ]; then
          return 1
        fi
      done
      return 0
      ;;
    *)
      # An unsupported *form* and a typo are different failures, and only the
      # first may lose to a satisfied sibling in a `||`. This catch-all returned
      # 2 for both, so `banana || >=20` came back satisfied while
      # `>=20banana || >=20` -- the same typo, one character further along --
      # correctly refused. A hyphen range is the form this comparator does not
      # implement; everything else here is malformed.
      # `1.2.3 - 2.3.4` reaches here as three tokens, and its middle one is a
      # bare `-`. That shape is recognised by the caller, which is the only
      # place with enough context to tell it from a lone `-` typo.
      return 3
      ;;
  esac
}

# _semver_satisfies <version> <range> – 0 satisfied, 1 unsatisfied,
# 2 unverifiable. Alternatives split on "||"; tokens within one alternative are
# conjunctive, as npm defines them.
_semver_satisfies() {
  local version="$1" rest="$2"
  # The runtime is checked before any range is. An operand this comparator
  # cannot read is reported unverifiable; a *version* it cannot read was
  # coerced to 0.0.0, or worse to nothing at all, and quietly satisfied ">=".
  # Reported as its own status. Folding it into "unverifiable range" sent an
  # operator to stare at a perfectly good manifest while the actual fault was a
  # runtime that printed a shim banner, or nothing at all.
  if ! _semver_is_concrete_version "$version"; then
    return 4
  fi
  # A prerelease runtime is readable but not orderable here; see
  # _semver_has_prerelease. Reported separately so the message names the
  # runtime rather than accusing the manifest.
  if _semver_has_prerelease "$version"; then
    return 5
  fi
  local alt tok ok alt_tokens rc more=1
  local satisfied=0 malformed=0 unrecognised=0 seen_alt=0
  local -a alt_toks
  while [ "$more" -eq 1 ]; do
    case "$rest" in
      *'||'*) alt="${rest%%||*}"; rest="${rest#*||}" ;;
      *)      alt="$rest"; more=0 ;;
    esac
    seen_alt=1
    ok=1
    alt_tokens=0
    # Word-splitting is the point: the tokens of one alternative are whitespace
    # separated and conjunctive, as npm defines them. Globbing is disabled
    # around the split because a bare "*" range would otherwise expand to the
    # directory listing and stop looking like a range at all.
    set -f
    # shellcheck disable=SC2086
    set -- $alt
    # npm allows whitespace between a comparator and its operand: ">= 20" is
    # the same range as ">=20". Splitting on whitespace alone left a bare ">=",
    # which the grammar check correctly calls malformed -- and so a conforming
    # environment was blocked by an infrastructure failure over a space.
    alt_toks=()
    while [ "$#" -gt 0 ]; do
      case "$1" in
        '>='|'<='|'>'|'<'|'='|'^'|'~')
          if [ "$#" -ge 2 ]; then
            alt_toks+=("$1$2")
            shift 2
            continue
          fi
          ;;
      esac
      alt_toks+=("$1")
      shift
    done
    # A hyphen range is `A - B`: three tokens, versions on both sides. Detected
    # here because a bare `-` is indistinguishable from a typo at token level,
    # and the two must not share a status -- an unsupported form may lose to a
    # satisfied sibling in a `||`, a typo may not.
    if [ "${#alt_toks[@]}" -eq 3 ] && [ "${alt_toks[1]}" = "-" ] \
      && _semver_hyphen_endpoint_ok "${alt_toks[0]}" \
      && _semver_hyphen_endpoint_ok "${alt_toks[2]}"; then
      unrecognised=1
      set +f
      continue
    fi

    for tok in ${alt_toks[@]+"${alt_toks[@]}"}; do
      alt_tokens=$((alt_tokens + 1))
      rc=0
      _semver_token_ok "$version" "$tok" || rc=$?
      if [ "$rc" -eq 2 ]; then
        unrecognised=1
      fi
      if [ "$rc" -eq 3 ]; then
        malformed=1
      fi
      # No early exit: a later token in this alternative may be the
      # unrecognised one, and "cannot evaluate" must win over "does not
      # satisfy" so the message names the real problem.
      if [ "$rc" -ne 0 ]; then
        ok=0
      fi
    done
    set +f
    # An empty alternative comes from a trailing or doubled "||", and this is a
    # deliberate, evidence-backed divergence from node-semver.
    #
    # What npm does: Range keeps the empty comparator set (`.filter(c =>
    # c.length)` runs over the *sets*, and an empty set parses to ANY), so the
    # whole range collapses to "". Verified against 7.8.5, not reasoned:
    # `new Range(">=999.0.0 ||").range` is "" and `.test("20.1.0")` is true.
    #
    # Why this gate refuses anyway: that reading nullifies the entire declared
    # constraint. A manifest that says ">=999.0.0" and, because of one stray
    # keystroke, admits every runtime in existence is the enforcement boundary
    # switched off by a typo -- the exact failure this comparator was hardened
    # against everywhere else. Copying npm here would be the single fail-open
    # left in the file, and it would be the loudest one. So an empty
    # alternative is classed malformed: not sibling-rescuable, reported as
    # "Cannot evaluate", naming the manifest so the typo gets fixed.
    if [ "$alt_tokens" -eq 0 ]; then
      malformed=1
      continue
    fi
    if [ "$ok" -eq 1 ]; then
      satisfied=1
    fi
  done
  # Malformed input is unverifiable however well one of its alternatives
  # matches -- there is no early return above, so a satisfied alternative cannot
  # hide a broken one. That covers an empty alternative from a stray "||" and an
  # invalid operand alike, because both are typos rather than range forms.
  # An *unrecognised* alternative is different: a valid range form this
  # comparator does not implement must not fail a runtime that another
  # alternative plainly admits.
  if [ "$malformed" -eq 1 ] || [ "$seen_alt" -eq 0 ]; then
    return 2
  fi
  if [ "$satisfied" -eq 1 ]; then
    return 0
  fi
  if [ "$unrecognised" -eq 1 ]; then
    return 2
  fi
  return 1
}

DECLARED_NODE="$(_manifest_field engines.node)"
if [ -n "$DECLARED_NODE" ]; then
  ACTUAL_NODE="$(node --version 2>/dev/null || true)"
  _node_rc=0
  _semver_satisfies "$ACTUAL_NODE" "$DECLARED_NODE" || _node_rc=$?
  case "$_node_rc" in
    0) ;;
    1)
      echo "Node ${ACTUAL_NODE:-<unknown>} does not satisfy the engines.node range"
      echo "  declared by ${CI_GATE_NODE_WORKSPACE}/package.json: ${DECLARED_NODE}"
      echo "  Results produced under an undeclared runtime are not reproducible,"
      echo "  so the lane stops before installing or running anything."
      exit "$CI_RESULT_FAIL_INFRA"
      ;;
    5)
      echo "Node reports a prerelease version: '${ACTUAL_NODE}'"
      echo "  Prerelease precedence is not implemented here, so this gate cannot"
      echo "  say whether it satisfies ${CI_GATE_NODE_WORKSPACE}/package.json's"
      echo "  engines.node (${DECLARED_NODE}). Run the lane on a release build."
      exit "$CI_RESULT_FAIL_INFRA"
      ;;
    4)
      echo "Node reported an unreadable version: '${ACTUAL_NODE}'"
      echo "  The engines.node range declared by ${CI_GATE_NODE_WORKSPACE}/package.json"
      echo "  (${DECLARED_NODE}) cannot be checked against it, so the lane stops."
      exit "$CI_RESULT_FAIL_INFRA"
      ;;
    *)
      echo "Cannot evaluate the engines.node range declared by"
      echo "  ${CI_GATE_NODE_WORKSPACE}/package.json: ${DECLARED_NODE}"
      echo "  The lane refuses to run rather than assume the toolchain conforms."
      exit "$CI_RESULT_FAIL_INFRA"
      ;;
  esac
fi

DECLARED_PM="$(_manifest_field packageManager)"
if [ -n "$DECLARED_PM" ]; then
  # corepack's format is name@version, optionally with a +integrity suffix. The
  # version is a pin, not a range: two patch releases of a package manager can
  # resolve a lockfile differently.
  PM_NAME="${DECLARED_PM%%@*}"
  PM_VERSION="${DECLARED_PM#*@}"
  PM_VERSION="${PM_VERSION%%+*}"

  if [ "$PM_NAME" != "$MANAGER" ]; then
    echo "The lockfile in ${CI_GATE_NODE_WORKSPACE} selects ${MANAGER}, but"
    echo "  package.json declares packageManager ${DECLARED_PM}."
    echo "  Installing with a manager the manifest does not declare produces a"
    echo "  tree the lockfile does not describe."
    exit "$CI_RESULT_FAIL_INFRA"
  fi

  ci::common::command_exists "$PM_NAME" || {
    echo "package.json declares packageManager ${DECLARED_PM} but ${PM_NAME} is missing."
    exit "$CI_RESULT_FAIL_INFRA"
  }
  ACTUAL_PM="$("$PM_NAME" --version 2>/dev/null || true)"
  ACTUAL_PM="${ACTUAL_PM#v}"
  if [ "$ACTUAL_PM" != "$PM_VERSION" ]; then
    echo "${PM_NAME} ${ACTUAL_PM:-<unknown>} is installed, but"
    echo "  ${CI_GATE_NODE_WORKSPACE}/package.json pins packageManager ${DECLARED_PM}."
    echo "  Install the declared version (corepack, or the manager's own"
    echo "  upgrade path) so the install is the one the lockfile describes."
    exit "$CI_RESULT_FAIL_INFRA"
  fi
fi

# The fingerprint covers the manifest as well as the lockfile. Keyed on the
# lockfile alone, a package.json edited without a matching lockfile update looks
# cached, the frozen install that would have caught the mismatch is skipped, and
# tests pass as long as they do not touch the changed dependency.
#
# And the runtime, because the first two say what was asked for and not what was
# built. Native addons, optional packages and install-script output are compiled
# against the Node ABI and for a platform and architecture, so moving between
# two releases that both satisfy a broad `engines.node` range left node_modules
# looking current: the install was skipped and the lane validated a tree a clean
# install would not produce. See ci::common::node_runtime_id, which is shared
# with the test fixtures so both compute the same value.
_deps_fingerprint() {
  printf '%s %s %s\n' \
    "$(ci::common::hash_file "$LOCKFILE")" \
    "$(ci::common::hash_file package.json)" \
    "$(ci::common::node_runtime_id)"
}

SKIP_INSTALL=0
if [ -n "$LOCKFILE" ] && [ -d "node_modules" ] && [ -f "$NODE_HASH_FILE" ]; then
  CURRENT_HASH="$(_deps_fingerprint)"
  CACHED_HASH="$(cat "$NODE_HASH_FILE")"
  if [ "$CURRENT_HASH" = "$CACHED_HASH" ]; then
    echo "node_modules up to date. Skipping install."
    SKIP_INSTALL=1
  fi
fi

# Dependency installation is provisioning, not a code result. Under set -e a
# registry outage or auth failure exits 1, which normalize_result would map to
# FAIL_NEW_ISSUE and blame the code for a broken environment. Each install is
# pinned to FAIL_INFRA explicitly so the classification survives that mapping.
if [ "$SKIP_INSTALL" = "0" ]; then
  case "$MANAGER" in
  pnpm)
    ci::common::command_exists pnpm || { echo "pnpm lockfile found but pnpm is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: pnpm install --frozen-lockfile"
    pnpm install --frozen-lockfile || exit "$CI_RESULT_FAIL_INFRA"
    ;;
  npm)
    ci::common::command_exists npm || { echo "npm lockfile found but npm is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: npm ci --quiet"
    npm ci --quiet || exit "$CI_RESULT_FAIL_INFRA"
    ;;
  yarn)
    ci::common::command_exists yarn || { echo "yarn lockfile found but yarn is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    YARN_VERSION="$(yarn --version 2>/dev/null || echo "0")"
    YARN_MAJOR="${YARN_VERSION%%.*}"
    if [ "$YARN_MAJOR" = "1" ]; then
      echo "Installing dependencies: yarn install --frozen-lockfile"
      yarn install --frozen-lockfile || exit "$CI_RESULT_FAIL_INFRA"
    else
      echo "Installing dependencies: yarn install --immutable"
      yarn install --immutable || exit "$CI_RESULT_FAIL_INFRA"
    fi
    ;;
  bun)
    ci::common::command_exists bun || { echo "bun lockfile found but bun is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: bun install --frozen-lockfile"
    bun install --frozen-lockfile || exit "$CI_RESULT_FAIL_INFRA"
    ;;
  esac

  mkdir -p "$(dirname "$NODE_HASH_FILE")"
  _deps_fingerprint > "$NODE_HASH_FILE"
fi

case "$MANAGER" in
  pnpm) ci::common::command_exists pnpm || { echo "pnpm is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
  npm) ci::common::command_exists npm || { echo "npm is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
  yarn) ci::common::command_exists yarn || { echo "yarn is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
  bun) ci::common::command_exists bun || { echo "bun is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
esac

PACKAGE_SCRIPTS="$(node -e "try{const p=require('./package.json');console.log(Object.keys(p.scripts||{}).join('\n'))}catch(e){console.error('Invalid package.json:',e.message);process.exit(1)}" 2>&1)" || {
  echo "$PACKAGE_SCRIPTS" >&2
  echo "Failed to read scripts from package.json."
  exit "$CI_RESULT_FAIL_INFRA"
}

script_exists() {
  local script_name="$1"
  printf '%s\n' "$PACKAGE_SCRIPTS" | grep -Fx -- "$script_name" >/dev/null 2>&1
}

# The command a package script runs, with grouping punctuation blanked.
#
# Every caller of this function is a reader -- the delegation resolver, the test
# path and the typecheck path -- and what each of them wants to know is which
# words the shell will hand to which command. `( vitest run --exclude=x )` and
# `{ tsc --noEmit src/a.ts; }` hand over exactly what the unwrapped forms do, so
# the wrapper is normalised away once, here, rather than taught to each rule
# separately. Doing it per rule is what left `_script_names_a_checker` stepping
# over `(` while `_command_runner` returned it as the program name: the
# predicate said a runner was named and the argument rules were never reached.
#
# The substitution preserves length, so a message quoting this text differs from
# package.json only by the two characters that were structure. See
# _normalize_groups for which parentheses and braces qualify -- `$(...)`,
# `${...}` and `{ts,tsx}` are arguments and are left alone.
script_command() {
  local _ng_out=""
  _normalize_command "$(node -e "try{const p=require('./package.json');process.stdout.write(String((p.scripts||{})['$1']||''))}catch(e){}" 2>/dev/null || true)"
  printf '%s' "$_ng_out"
}

# A test script that narrows the suite is a suite that does not run.
#
# The layout guard rejects a persistent filter written into vitest.config.ts,
# and this is the same filter one layer out: `vitest run -t no-such-name` exits
# 0 with every collected test skipped, and the config the guard inspects is
# untouched. Checking that a `test` script *exists* said nothing about whether
# it runs anything, in exactly the way that checking a `typecheck` script exists
# said nothing about whether tsc ran.
#
# Matched as whole arguments so a path or a test name containing "-t" is not
# mistaken for the flag.
# _script_names_a_checker <command> [tool-list] [depth] - 0 when the command
# actually runs a known checker, or delegates to something that does.
#
# Rewritten around *command position*. Scanning for a tool token anywhere in the
# string accepted `"test": "echo vitest"`, which runs echo and collects nothing,
# and `bash scripts/x.sh` reached through `echo bash scripts/x.sh` the same way.
# A token is evidence that a checker runs only where a command starts: at the
# beginning, or after a separator.
#
# This rule has now been fixed five times, and every previous attempt lost for
# the same reason -- it asked whether a string contains something rather than
# whether the shell would execute it. Presence is not execution.
# _reject_untrustworthy_composition <script name> <command> – exits the lane
# when the command composes in a way whose result cannot be attributed to the
# checker. Shared by the typecheck and test paths so both give the same reason.
#
# Spelled without spaces on purpose: `a||b` is the same operator as `a || b`,
# and matching only the spaced form was a bypass two keystrokes wide.
_reject_untrustworthy_composition() {
  local _name="$1" _cmd="$2"
  # Quoted text is data, and this read the raw string.
  #
  # `PATTERN='foo|bar' vitest run` was refused as a pipeline. The shell treats
  # that `|` as part of an assignment's value and then runs the whole suite, so
  # the rule fired on a script it has no objection to and the message sent the
  # developer looking for a pipeline that is not there. _blank_quoted is in this
  # file for exactly this and three other readers already go through it: one
  # rule, four readers, and this was the one asking the raw text.
  local _bq_state="" _bq_out="" _bq_cont=0 _bq_esc=0
  _blank_quoted "$_cmd"
  local _mask="$_bq_out"
  # An escaped word is refused rather than masked, the same way the readers that
  # already mask refuse it: blanking it hides a word from every rule reading the
  # mask while the shell still runs it.
  if [ "$_bq_esc" -eq 1 ]; then _reject_escaped_word "$_name"; fi
  # And a quote that never closes is refused here rather than masked past.
  # _blank_quoted blanks to the end of the string once it is inside an unclosed
  # quote, so an operator after it is invisible to the two tests below -- the
  # masking that fixes a false red would otherwise open a fail-through two
  # keystrokes wide. This function's question is whether the result can be
  # attributed to the checker, and a command whose quoting does not resolve
  # cannot be read at all, let alone vouched for.
  if [ -n "$_bq_state" ] || [ "$_bq_cont" -eq 1 ]; then
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} leaves a quote open in the '${_name}' script:"
    echo "    ${_name}: ${_cmd}"
    echo "  Where the quoting ends decides which characters are operators, so"
    echo "  this gate cannot say what the script composes -- and reading past it"
    echo "  would hide a pipeline or a '||' inside the unclosed span."
    echo "  Close the quote, or move the command into a script this gate does"
    echo "  not have to reason about."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi
  # `&&` is removed before the `&` test rather than enumerated around it: it is
  # the one composition that is safe, since either the checker runs or the thing
  # before it failed and the script fails with it.
  #
  # Removed from the *mask*, so a literal `&&` inside quoted data is not taken
  # for the operator either.
  local _cmp="${_mask//&&/}"
  case "$_mask" in
    *"|"*)
      echo "Workspace ${CI_GATE_NODE_WORKSPACE} pipes or branches the '${_name}' script, so its"
      echo "  result cannot be trusted:"
      echo "    ${_name}: ${_cmd}"
      echo "  A pipeline reports the status of its *last* command, so"
      echo "  'vitest run | tee out.log' exits 0 with a failing suite behind it,"
      echo "  and 'tsc --noEmit | cat' reports success while printing errors."
      echo "  '||' is the other half: either the checker is never reached, or it"
      echo "  ran and its failure was swallowed. Whether 'pipefail' is set inside"
      echo "  the script is not something this gate can see."
      echo "  Use '&&', or ';', or move the composition into a script this gate"
      echo "  does not have to reason about."
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
      ;;
  esac
  case "$_cmp" in
    *"&"*)
      echo "Workspace ${CI_GATE_NODE_WORKSPACE} backgrounds the '${_name}' script:"
      echo "    ${_name}: ${_cmd}"
      echo "  The script exits before the checker it started has finished, so the"
      echo "  lane reports PASS with no result behind it."
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
      ;;
  esac
}

# Strip one leading and one trailing quote from $tok, in place.
#
# A bracket expression is the obvious way to write this and is treacherous:
# `${tok#[\"\']}` and `${tok#[\"']}` differ by one backslash and behave
# differently, and the failure is silent -- the wrong one strips the first
# *character* of every token, so `tsc` became `sc` and every TypeScript
# workspace was told it has no type checker. Matched one quote at a time here,
# with `?` removing exactly one character, because that cannot be misread.
_unquote_tok() {
  case "$tok" in
    '"'*) tok="${tok#?}" ;;
    "'"*) tok="${tok#?}" ;;
  esac
  case "$tok" in
    *'"') tok="${tok%?}" ;;
    *"'") tok="${tok%?}" ;;
  esac
}

# _resolve_delegated_target <target> <tool-list> <depth> – 0 when the thing a
# runner hands to can be shown to reach a checker.
#
# Split out of the scan because a delegation has to be resolved wherever the
# command *ends*, not only at the end of the string: `bash scripts/test.sh ;
# exit 0` had its target cleared by the separator before anything asked what
# was in the script, and an ordinary wrapper-then-exit was rejected.
# Quoted text is data, not code, and the structure reader below counts braces
# and control keywords -- both of which can be spelled inside a string. `echo
# "}"` closed a function body a line early, and `echo "using profile"` closed an
# `if` block, because `profile` contains `fi`. Either one makes a checker the
# shell may never reach read as top level, so the lane reports PASS on a suite
# that never runs.
#
# Every quoted span becomes spaces, one character for one, so an offset into the
# blanked line still addresses the same character of the original.
#
# With one exception, and it is the one that makes argument boundaries readable:
# whitespace *inside* a quoted span becomes `_` rather than a space. A blanked
# space and a real space are otherwise the same character, so nothing could tell
# `echo "trap fake EXIT"` -- one argument -- from `echo trap fake EXIT`, and the
# trap rule read a word out of the middle of a string. `_` is not structure, not
# a separator and not a comment, so no other reader here notices; what it buys
# is that a position holding a space in both copies is a genuine boundary. `_bq_state`
# carries an unterminated quote into the next line, because a string spanning
# lines makes those lines data as well; `_bq_cont` reports a trailing backslash,
# which makes the next line a continuation of this command rather than a new
# one. Callers own all three as locals, so a nested resolution cannot disturb
# the scan that invoked it.
_blank_quoted() {
  local _s="$1" _i=0 _n=${#1} _c _out=""
  _bq_cont=0
  _bq_esc=0
  # Nothing to blank and no state to carry: the overwhelmingly common line.
  if [ -z "$_bq_state" ]; then
    case "$_s" in
      *[\'\"\\]*) ;;
      *) _bq_out="$_s"; return 0 ;;
    esac
  fi
  while [ "$_i" -lt "$_n" ]; do
    _c="${_s:$_i:1}"
    # A backslash escapes the next character -- which is therefore neither a
    # quote nor structure. Inside single quotes it is literal and escapes
    # nothing.
    if [ "$_c" = "\\" ] && [ "$_bq_state" != "'" ]; then
      if [ "$((_i + 1))" -lt "$_n" ]; then
        # An escaped *word* character is reported, because blanking it hides a
        # word from every rule that reads this mask while the shell still runs
        # it: `\trap 'exit 0' EXIT` is the trap command, spelled so that the
        # scan sees `rap`, and the EXIT handler was installed with the guard
        # none the wiser. Reported rather than kept -- keeping the character
        # would make `echo \done` close a loop the reader is inside, which is
        # the same defect pointing the other way. Callers refuse the line.
        if [ -z "$_bq_state" ]; then
          case "${_s:$((_i + 1)):1}" in
            [A-Za-z0-9_]) _bq_esc=1 ;;
          esac
        fi
        case "${_s:$((_i + 1)):1}" in
          [[:space:]]) _out="${_out} _" ;;
          *) _out="$_out  " ;;
        esac
      else
        _out="$_out "
        _bq_cont=1
      fi
      _i=$((_i + 2))
      continue
    fi
    if [ -n "$_bq_state" ]; then
      if [ "$_c" = "$_bq_state" ]; then _bq_state=""; fi
      case "$_c" in
        [[:space:]]) _out="${_out}_" ;;
        *) _out="$_out " ;;
      esac
    else
      case "$_c" in
        \'|\") _bq_state="$_c"; _out="$_out " ;;
        *) _out="$_out$_c" ;;
      esac
    fi
    _i=$((_i + 1))
  done
  _bq_out="$_out"
}

_resolve_delegated_target() {
  # Out-parameter: whether the resolved target passes the caller's arguments on
  # to the runner. Cleared here, so a caller reads the answer for the
  # resolution it just asked for and not for an earlier one.
  _rdt_fwd=0
  local target="$1"
  local tools="$2"
  local depth="${3:-0}"
  # Whether the command that named this target also carried `--test`, which is
  # the only thing that makes `node` a runner. Passed rather than re-derived:
  # the flag belongs to the invocation, and by here the invocation is gone.
  local node_ok="${4:-0}"
  local t
  local _bq_state="" _bq_out="" _bq_cont=0 _bq_esc=0


  # For `npx`, `pnpm dlx` and friends the target *is* the executable, so a
  # checker there is a checker being run and needs no further resolution.
  # Reached only through a runner in command position, so `echo vitest` does
  # not arrive here.
  for t in $tools; do
    if [ "${target##*/}" = "$t" ]; then
      if [ "$t" = "node" ] && [ "$node_ok" -eq 0 ]; then continue; fi
      # The target *is* the executable, so whatever the caller wrote after it is
      # an argument to that executable. There is nothing here to forward
      # through, and `npx vitest tests/a.test.ts` is a filter like any other.
      _rdt_fwd=1
      return 0
    fi
  done

  # Delegation is followed, not taken on trust. Accepting a runner plus a target
  # token meant `bash scripts/test.sh` passed while that script was `exit 0`.
  # A package script is in the manifest and a shell script is a file in the
  # workspace, so both are read.
  #
  # What cannot be resolved -- `make test`, or a target that is neither -- is
  # refused: a gate that cannot see what it delegates to has no basis for
  # reporting PASS on it.
  local sub
  sub="$(script_command "$target" 2>/dev/null || true)"
  if [ -n "$sub" ]; then
    if _script_names_a_checker "$sub" "$tools" "$((depth + 1))"; then
      # Whether the caller's own arguments can reach the runner through this
      # target at all. A script that names no positional parameter consumes
      # what it is given; one that forwards passes it straight to the runner,
      # where a bare word is a filter. Asked so that `bash scripts/build.sh
      # --ci` -- a wrapper handling its own flag -- is not refused for a filter
      # that never arrives anywhere.
      case "$sub" in
        *'$@'*|*'$*'*) _rdt_fwd=1 ;;
      esac
      # A package script reached through another one gets the same reading as a
      # direct command. Resolving it and stopping at the runner meant `"test":
      # "bun run verify"` over `"verify": "vitest run --exclude=..."` passed
      # with the exclusion never inspected -- the flag and positional rules held
      # for the command in the `test` key and for nothing it delegated to.
      _reject_tool_args "$target" "$sub" "$tools"
      return 0
    fi
    return 1
  fi

  # A readable file, judged line by line: one line reaching a checker is enough,
  # and a `-c` elsewhere does not condemn the whole script. Only the code is
  # judged -- comments, string bodies and here-document bodies are removed
  # first, because a checker named in any of them is not one being run, and
  # removing text can only ever cause a refusal, never an acceptance.
  if [ -f "$target" ] && [ -r "$target" ]; then
    # Only lines the script is bound to reach.
    #
    # A flat line-by-line scan accepted `unused() { node --test; }` followed by
    # `exit 0`: the runner is named, in command position, inside a function
    # nobody calls. Same for a checker buried in an `if` branch or a loop -- the
    # shell may or may not reach it, and this reader cannot evaluate shell
    # control flow to find out. So a line counts only at the top level of the
    # script, outside every brace group and every block.
    #
    # Deliberately strict, and the message says what to do about it: a workspace
    # whose invocation really is conditional can name its runner in the manifest
    # instead, which is the thing this gate can actually read.
    local line real code toks tok pre opens closes
    local brace=0 block=0 _fn=0 _kw=0 _cont=0 _cont_prev=0 state_in=""
    local hit=0 errexit=0 _tail="" _body=""
    local hd_end="" hd_dash=0 hd_rest
    # The body is read once, up front, rather than kept open across the loop.
    #
    # The scan calls out to the filter validator with this same path as its
    # label while the file is the loop input, which is the shape SC2094
    # exists to catch -- and the analyser cannot tell that the callee only
    # prints the name. Reading first removes the coupling rather than
    # arguing about it, and a wrapper script is a few lines.
    _body="$(cat "$target" 2>/dev/null)" || return 1
    while IFS= read -r line || [ -n "$line" ]; do
      # A here-document body is data. A checker named inside one is text being
      # written somewhere, not a command being run, and reading it as code is
      # the same mistake as reading a quoted brace as structure.
      if [ -n "$hd_end" ]; then
        tok="$line"
        if [ "$hd_dash" -eq 1 ]; then tok="${tok#"${tok%%[![:space:]]*}"}"; fi
        if [ "$tok" = "$hd_end" ]; then hd_end=""; fi
        continue
      fi

      state_in="$_bq_state"
      _cont_prev="$_cont"
      _blank_quoted "$line"
      code="$_bq_out"
      _cont="$_bq_cont"

      # A `#` inside a string does not start a comment, so the comment comes off
      # the blanked line and the same offset comes off the real one -- blanking
      # preserves length precisely so this holds.
      case "$code" in
        '#'*) real=""; code="" ;;
        *[[:space:]]'#'*)
          pre="${code%%[[:space:]]#*}"
          real="${line:0:$(( ${#pre} + 1 ))}"
          code="${code:0:$(( ${#pre} + 1 ))}"
          ;;
        *) real="$line" ;;
      esac

      case "$code" in
        *[![:space:]]*) ;;
        *) continue ;;
      esac

      # Asked of every line, not only the ones judged below. A handler is
      # installed wherever it is written -- inside a function, inside an `if`,
      # anywhere the structure reader refuses to judge -- and it still fires at
      # exit. Restricting this to top-level lines would have left
      # `if [ -n "$CI" ]; then trap "exit 0" EXIT; fi` accepted.
      if [ "$_bq_esc" -eq 1 ]; then _reject_escaped_word "$real"; fi
      _reject_status_trap "$real" "$real" "$code"

      # Split on every separator so control keywords are matched as whole
      # tokens. Matching them as substrings is what let `well done` close a
      # loop and `/opt/esaclib` close a `case`.
      toks="$code"
      toks="${toks//;/ }"
      toks="${toks//&/ }"
      toks="${toks//|/ }"
      toks="${toks//(/ }"
      toks="${toks//)/ }"
      toks="${toks//\{/ }"
      toks="${toks//\}/ }"

      # A line carrying any control keyword is a compound statement this reader
      # cannot evaluate -- `if [ -n "$CI" ]; then vitest run; fi` opens and
      # closes on one line, so depth alone would call it top level. Refuse to
      # judge it instead of guessing which branch runs.
      _kw=0
      set -f
      # Quoted patterns, and not only for the two that need it. `esac` and
      # `done` are keywords a `case` arm cannot carry bare -- bash tolerates
      # them after a `|`, other shells terminate the statement there -- and
      # quoting also says what these arms mean: the literal word, never a glob.
      for tok in $toks; do
        case "$tok" in
          'if'|'then'|'elif'|'else'|'fi'|'for'|'while'|'until'|'do'|'done' \
            |'case'|'esac'|'select'|'function')
            _kw=1
            break
            ;;
        esac
      done
      set +f

      # A function definition line is never itself a command being run. It is
      # not skipped outright, though: a multi-line `f() {` still opens a brace
      # that the accounting below has to see, and skipping the line before
      # counting it left the body reading as top level -- which accepted the
      # very case this rule is for, one line lower down.
      _fn=0
      case "$code" in
        *'()'*) _fn=1 ;;
      esac

      if [ "$_fn" -eq 0 ] && [ "$brace" -eq 0 ] && [ "$block" -eq 0 ] &&
         [ "$_kw" -eq 0 ] && [ "$_cont_prev" -eq 0 ] &&
         [ -z "$state_in" ] && [ -z "$_bq_state" ]; then
        # A top-level line after the one that reached the checker replaces the
        # script's status with its own, exactly as a `;` does inside a command:
        # `vitest run` followed by `true` exits 0 however the suite went. So
        # the scan does not stop at the runner -- it keeps reading, and another
        # command below refuses.
        #
        # Unless the script asked for `set -e`, which is the whole point of
        # that line: the shell leaves on the runner's failure and the lines
        # below never execute. Read before the runner, since it only governs
        # what follows it.
        if [ "$hit" -eq 1 ] && [ "$errexit" -eq 0 ]; then
          # The rule is not "nothing may follow" -- it is that nothing may turn
          # a failure into a pass. A bare `exit` and `exit $?` both leave with
          # the status already there, and `exit 1` forces a failure, which is
          # the safe direction. `true`, `exit 0` and anything whose status this
          # reader cannot know are what the rule is for.
          _tail="${real#"${real%%[![:space:]]*}"}"
          _tail="${_tail%"${_tail##*[![:space:]]}"}"
          case "$_tail" in
            'exit'|'exit $?'|'exit "$?"'|'return'|'return $?') return 0 ;;
            exit\ [1-9]*|return\ [1-9]*) return 0 ;;
          esac
          return 1
        fi
        # `set +e` turns it back off, and a wrapper that does so is exactly
        # where the trailing-command rule has to apply again: `set -e`, then
        # `set +e`, then a failing runner, then `true` exits 0 while this
        # reader believed errexit was still protecting it.
        case "$real" in
          *'set +e'*|*'set +o errexit'*) errexit=0 ;;
          *set\ -*e*) errexit=1 ;;
        esac
        if _script_names_a_checker "$real" "$tools" "$((depth + 1))"; then
          hit=1
          # Same question as the package-script path: whether this script hands
          # the caller's arguments on to the runner, or handles them itself.
          case "$real" in
            *'$@'*|*'$*'*) _rdt_fwd=1 ;;
          esac
          # The filter rule holds one layer down as well. `"test": "bash
          # scripts/test.sh"` leaves is_test_runner at zero, so a script whose
          # body is `vitest run tests/only.test.ts` skipped positional
          # validation entirely -- accepted here while the identical direct
          # command was refused.
          #
          # Only when this line starts with a known runner: a positional means
          # something else for `bash scripts/inner.sh`, which is delegation and
          # is resolved rather than filtered.
          #
          # The flag rule travels with it. Applying only the positional one here
          # left `--exclude=tests/a.test.ts` accepted in a delegated shell
          # script while the identical spelling was refused in a delegated
          # package script -- the same rule holding for one spelling of a
          # delegation and not the other, which is the shape this lane has now
          # had to fix five times. One dispatcher, both rules, every site.
          _reject_tool_args "$target" "$real" "$tools"
        fi
      fi

      # Track what this line opens or closes, after it has been judged.
      opens="${code//[!\{]/}"
      closes="${code//[!\}]/}"
      brace=$((brace + ${#opens} - ${#closes}))
      if [ "$brace" -lt 0 ]; then brace=0; fi
      set -f
      for tok in $toks; do
        case "$tok" in
          'if'|'for'|'while'|'until'|'case'|'select') block=$((block + 1)) ;;
          'fi'|'done'|'esac')
            if [ "$block" -gt 0 ]; then block=$((block - 1)); fi
            ;;
        esac
      done
      set +f

      # `<<` starts a here-document whose body begins on the next line. Found on
      # the blanked line so `echo "a << b"` does not arm it, but the delimiter
      # is read from the real one, since `<<'EOF'` quotes its own delimiter.
      case "$code" in
        *\<\<*)
          pre="${code%%\<\<*}"
          hd_rest="${line:$(( ${#pre} + 2 ))}"
          case "$hd_rest" in
            '<'*) ;;
            *)
              hd_dash=0
              case "$hd_rest" in -*) hd_dash=1; hd_rest="${hd_rest#-}" ;; esac
              hd_rest="${hd_rest#"${hd_rest%%[![:space:]]*}"}"
              hd_end="${hd_rest%%[[:space:];|&)]*}"
              hd_end="${hd_end//\'/}"
              hd_end="${hd_end//\"/}"
              hd_end="${hd_end//\\/}"
              ;;
          esac
          ;;
      esac
    done <<< "$_body"
    if [ "$hit" -eq 1 ]; then return 0; fi
    return 1
  fi

  return 1
}

# A handler that fires on EXIT runs after everything else and can replace the
# status the whole lane is asking about: `trap 'exit 0' EXIT` in front of a
# failing runner leaves the script with a zero, and the suite's result never
# reaches the gate at all.
#
# Read as *arguments*, not as words. The first version split the blanked mask on
# whitespace, which is wrong twice over: a quoted action disappears entirely, so
# the first visible word is the signal rather than the action -- and a quoted
# *signal*, `trap 'exit 0' 'EXIT'`, disappears too, so the loop saw `trap` and
# no signal at all and accepted it. Both are valid spellings and the second one
# defeated the rule outright.
#
# The mask is length-preserving, which is what makes the split possible: a
# position where the mask holds a space and the original does not is quoted
# text, and a position where both do is a genuine argument boundary. So the
# arguments come out whole, quotes and all, and the quotes come off afterwards
# -- which also settles `'trap' 'exit 0' EXIT`, since a quoted command name is
# still that command.
#
# The action is not read. Quoted text is exactly what _blank_quoted removes,
# deliberately, and a reader that cannot see what a handler does cannot vouch
# for it -- so the allow-list is the one spelling that installs nothing: `trap -
# EXIT`, which restores the default. Everything else naming EXIT (or signal 0,
# its numeric spelling) is refused, including an ordinary `trap 'rm -f "$tmp"'
# EXIT` cleanup: the shell does preserve the status across a handler that does
# not exit, and nothing here can tell that handler apart from one that does.
_reject_status_trap() {
  local _what="$1" _cmd="$2" _mask="$3"
  # On the real text, not the mask: a fully quoted `'trap'` is blanked out of
  # the mask and is still the trap command. A false start here costs one walk.
  case "$_cmd" in
    *trap*) ;;
    *) return 0 ;;
  esac
  local _n=${#_cmd} _i=0 _arg="" _state=0 _sig _endcmd
  local _sq="'" _dq='"'
  while [ "$_i" -le "$_n" ]; do
    if [ "$_i" -lt "$_n" ]       && { [ "${_mask:$_i:1}" != " " ] || [ "${_cmd:$_i:1}" != " " ]; }; then
      _arg="${_arg}${_cmd:$_i:1}"
      _i=$((_i + 1))
      continue
    fi
    _i=$((_i + 1))
    [ -n "$_arg" ] || continue
    # Quotes off, all of them: bash lets a word be quoted in pieces, so `EX'IT'`
    # is the signal EXIT and stripping only the outermost pair would miss it.
    _sig="${_arg//"$_sq"/}"
    _sig="${_sig//"$_dq"/}"
    _arg=""
    # A separator ends the trap command wherever it appears, attached to a word
    # or standing alone. Recorded and applied *after* the piece in front of it
    # has been judged: `trap 'x' INT; echo EXIT` ends the trap at the `;`, and
    # leaving the state alone there had the `EXIT` two words later read as a
    # signal of a command that finished -- refusing an echo.
    _endcmd=0
    case "$_sig" in
      *';'*|*'&'*|*'|'*)
        _sig="${_sig%%[;&|]*}"
        _endcmd=1
        ;;
    esac
    if [ -z "$_sig" ]; then
      :
    elif [ "$_state" -eq 0 ]; then
      case "$_sig" in trap) _state=1 ;; esac
    elif [ "$_state" -eq 1 ]; then
      # The action, whatever it is. `-` restores the default handler and
      # installs nothing, which is the one form that cannot carry a status.
      #
      # `-p` installs nothing either: bash documents it as displaying the trap
      # commands associated with each signal, so `trap -p EXIT && vitest run`
      # prints and returns, and the runner behind it keeps its own status. Read
      # as an action, the `EXIT` after it was judged as that action's signal and
      # an ordinary diagnostic script was refused before the suite ran. It is
      # the same distinction `-` already makes here: inspection is not
      # installation.
      case "$_sig" in
        '-'|'-p'|'--print') _state=0 ;;
        *) _state=2 ;;
      esac
    else
      _sig="$(printf '%s' "$_sig" | tr '[:lower:]' '[:upper:]')"
      _sig="${_sig#SIG}"
      case "$_sig" in
        EXIT|0)
          echo "Workspace ${CI_GATE_NODE_WORKSPACE} installs an EXIT trap around a checker:"
          echo "    ${_what}"
          echo "  An EXIT handler runs after the runner and can leave with its own"
          echo "  status -- 'trap \"exit 0\" EXIT' in front of a failing suite exits"
          echo "  0, and the lane reports PASS on a suite that failed. The handler"
          echo "  is quoted text, which this gate reads as data and cannot"
          echo "  evaluate, so it cannot be shown to preserve the result."
          echo "  Move the cleanup into whatever invokes this script, or name the"
          echo "  runner in package.json where there is no handler to reason about."
          exit "$CI_RESULT_FAIL_NEW_ISSUE"
          ;;
      esac
    fi
    if [ "$_endcmd" -eq 1 ]; then _state=0; fi
  done
}

# The program a command actually runs: the first word that is not an
# environment assignment, a package-manager wrapper, a wrapper prefix or a flag,
# taken by basename so a runner named by path still matches.
#
# One function because there were four copies of this loop, and the four
# disagreeing is a defect in its own right -- `command` was in the resolver's
# copy and not in discovery's, so `command vitest run --exclude=...` had the
# argument rules skipped entirely while the resolver accepted vitest one
# function over. `NODE_ENV=test vitest run --exclude=...` was the same bypass
# with an assignment instead of a prefix, and is closed by the same list.
# Grouping is structure, and every reader here was taking it for a command name.
#
# `( vitest run tests/a.test.ts )` and `{ vitest run --exclude=x; }` run exactly
# what the unwrapped forms run. `_script_names_a_checker` already knew that --
# it steps over `(`, `)`, `{` and `}` when looking for a runner in command
# position -- so the predicate said "this script runs vitest" while
# `_command_runner` derived `(` as the program, `(` is in no tool list, and
# `_reject_tool_args_one` was never called at all. That function is the only
# route to the tsc rules, the narrowing rules and the positional-filter rule, so
# one character in front of a command switched off every argument rule at once,
# for the test path and the typecheck path alike.
#
# Blanked rather than tokenised away, because `(vitest run)` needs no spaces to
# be a subshell and `(vitest` is one word.
#
# Only in *command position*, which is what separates a subshell from the two
# constructs that look like one: `$(...)` and `<(...)` are substitutions whose
# parenthesis follows a character, and their contents are an argument to the
# command in front of them, not a command of their own. A `)` closes whichever
# kind was opened, so the two are tracked on a stack rather than counted.
#
# `{` and `}` only when they stand alone as words, because that is the only
# spelling the shell reads as a group: `tests/**/*.{ts,tsx}` and `${VAR}` are an
# argument and an expansion, and blanking those would corrupt the very arguments
# these rules exist to read.
# And an unquoted newline is `;`.
#
# A package.json script is a JSON string, and a JSON string may contain a real
# newline. The shell reads it as a command separator exactly like `;`, so
# a `"test"` script whose text is `vitest run`, a newline, then `exit 0` runs
# the suite and then replaces its status --
# byte-for-byte the case the status rule refuses when it is spelled with a
# semicolon. Nothing saw it: `;` is matched as a *token* by the reader below,
# and a newline arrives there as ordinary whitespace between two tokens. Turned
# into the separator it is, once, so every rule downstream reads one spelling.
_normalize_command() {
  local _s="$1"
  local _nl=$'\n' _cr=$'\r'
  # `&&` and `;` bind to the token before them, and this is the normaliser every
  # word-splitting reader in this file goes through -- so `true&&vitest run`
  # arrived at _command_runner as the single token `true&&vitest`, which is not
  # a runner, and the script was judged as running none.
  #
  # _script_names_a_checker has its own copy of this spacing and was corrected
  # first; this one was not, and the two disagreed about the same string. Found
  # by the case that drives both readers over one table rather than by the next
  # review round -- which is what that case is for.
  case "$_s" in
    *['(){};']*) ;;
    *'&&'*) ;;
    *"$_nl"*) ;;
    *"$_cr"*) ;;
    *) _ng_out="$_s"; return 0 ;;
  esac
  local _bq_state="" _bq_out="" _bq_cont=0 _bq_esc=0
  _blank_quoted "$_s"
  local _m="$_bq_out" _i=0 _n=${#_s} _out="" _mc _nx _stack="" _prev=" " _pre=0 _post=0
  while [ "$_i" -lt "$_n" ]; do
    _mc="${_m:$_i:1}"
    # Separators first, and against the mask, so a `&&` or `;` inside a quoted
    # argument stays where it is: quoted text is data.
    if [ "${_m:$_i:2}" = "&&" ]; then
      _out="$_out && "
      _prev=" "
      _i=$((_i + 2))
      continue
    fi
    if [ "$_mc" = ";" ]; then
      _out="$_out ; "
      _prev=" "
      _i=$((_i + 1))
      continue
    fi
    _pre=0
    case "$_prev" in [[:space:]]) _pre=1 ;; esac
    case "$_mc" in
      '(')
        if [ "$_pre" -eq 1 ]; then
          _stack="${_stack}s" ; _out="$_out "
        else
          _stack="${_stack}u" ; _out="$_out${_s:$_i:1}"
        fi
        ;;
      ')')
        case "$_stack" in
          *s) _stack="${_stack%?}" ; _out="$_out " ;;
          *u) _stack="${_stack%?}" ; _out="$_out${_s:$_i:1}" ;;
          *)  _out="$_out${_s:$_i:1}" ;;
        esac
        ;;
      "$_nl"|"$_cr")
        _out="$_out;"
        ;;
      '{'|'}')
        _nx="${_m:$((_i + 1)):1}"
        _post=0
        case "$_nx" in ''|[[:space:]]) _post=1 ;; esac
        if [ "$_pre" -eq 1 ] && [ "$_post" -eq 1 ]; then
          _out="$_out "
        else
          _out="$_out${_s:$_i:1}"
        fi
        ;;
      *) _out="$_out${_s:$_i:1}" ;;
    esac
    _prev="$_mc"
    _i=$((_i + 1))
  done
  _ng_out="$_out"
}

# _timeout_advance <token> – whether this token still belongs to a `timeout`
# wrapper, advancing `_tp_state` as it goes. Returns 0 when the caller should
# skip the token, 1 when the wrapper is behind us and the token is the caller's.
#
# `timeout [OPTION] DURATION COMMAND [ARG]...`, which is what `timeout --help`
# documents, and only the two-word shape was ever read: the token after the
# wrapper was taken as the duration if it looked numeric and the state was
# cleared either way. So `timeout --foreground 300 vitest run` cleared the state
# on `--foreground`, `300` arrived as the command name, and an ordinary
# full-suite script was refused for running no test runner. `-s SIGKILL`,
# `-k 10` and `--preserve-status` all do it, and `-s`/`-k` bring a value of
# their own that is not the duration either.
#
# One function because there were four copies of the two-word rule -- in
# _command_runner, in _script_names_a_checker, and twice in
# _reject_positional_filters -- and four copies is how a rule ends up fixed in
# one place and left in the other three. The state is a variable rather than a
# return value because two of those callers iterate words and two shift
# positional parameters, and a shared *predicate* is the only shape that fits
# both.
#
# _tp_state: 1 = inside the wrapper, still looking for the duration
#            2 = this token is the value of the option before it
#            0 = the wrapper is fully consumed
_timeout_advance() {
  case "$_tp_state" in
    2) _tp_state=1 ; return 0 ;;
    1) ;;
    *) return 1 ;;
  esac
  case "$1" in
    # The options that take a separate value. The `=` spellings carry theirs in
    # the same token and need nothing here.
    -k|--kill-after|-s|--signal) _tp_state=2 ; return 0 ;;
    # Any other option belongs to timeout and carries no value.
    -*) return 0 ;;
    # The first non-option word is the duration, and the wrapper ends with it.
    # Matched by position rather than by looking numeric, because `5m`, `1.5h`
    # and `0.5s` are durations too and the numeric test was already wrong for
    # a wrapper written with an option in front.
    *) _tp_state=0 ; return 0 ;;
  esac
}

# _pm_advance <token> – whether this token still belongs to a package-manager
# prefix, advancing `_pp_state` as it goes. Third of its kind, and for the third
# time the same cause: the wrapper was skipped by name and the grammar after it
# was not read.
#
# `pnpm --filter web vitest run`, `yarn --cwd packages/web vitest run`,
# `npm --prefix packages/web run test` -- every one of these puts a bare word
# after a flag, and every reader here took that word as the program. The
# standard monorepo invocation was refused with "does not appear to run a test
# runner" while the runner sat in the string.
#
# The options that take a separate value, from each manager's own help:
#   pnpm  --filter/-F, --dir/-C, --workspace-root is a boolean
#   yarn  --cwd
#   npm   --prefix, --workspace/-w, -C
#   bun   --cwd
# `--filter=web` and the rest carry their value in the same token and need
# nothing here.
#
# Like env and unlike timeout, the first word that is neither an option nor an
# option's value is the command, and is handed back to the caller.
_pm_advance() {
  case "$_pp_state" in
    2) _pp_state=1 ; return 0 ;;
    1) ;;
    *) return 1 ;;
  esac
  # Keyed by which manager is in front, for the reason the flag allow-list in
  # _reject_narrowing_flags is keyed by runner: one list cannot describe five
  # vocabularies. `-p` is `--package` to `npx` and `--parseable` to `pnpm`, so a
  # shared list either misses npx's value or eats pnpm's command.
  case "${_pm_name}|$1" in
    'npx|-p'|'npx|--package'|'npx|-c'|'npx|--call' \
      |'npm|-p'|'npm|--package'|'npm|-c'|'npm|--call' \
      |'npm|--prefix'|'npm|-w'|'npm|--workspace'|'npm|-C' \
      |'pnpm|--filter'|'pnpm|-F'|'pnpm|--dir'|'pnpm|-C'|'pnpm|--workspace-dir' \
      |'yarn|--cwd' \
      |'bun|--cwd')
      _pp_state=2 ; return 0 ;;
  esac
  case "$1" in
    -*) return 0 ;;
    *) _pp_state=0 ; return 1 ;;
  esac
}

# _env_advance <token> – whether this token still belongs to an `env` prefix,
# advancing `_ep_state` as it goes. Same contract as _timeout_advance, and here
# for the same reason: the wrapper was skipped by name with nothing reading the
# grammar that follows it.
#
# `env [OPTION]... [NAME=VALUE]... [COMMAND [ARG]...]`, from `env --help`, and
# `-u NAME` / `-C DIR` / `-S STRING` take a value as a separate word. Skipping
# the wrapper alone left `env -u NODE_ENV vitest run` selecting `NODE_ENV` as
# the program -- it is neither a flag nor a `NAME=VALUE` assignment, so it fell
# through as the command name -- and the lane refused a full-suite script for
# running no test runner.
#
# Unlike timeout there is no duration: the first word that is neither an option
# nor an assignment *is* the command, so it is handed back to the caller rather
# than consumed.
#
# _ep_state: 1 = inside the prefix, 2 = this token is an option's value,
#            0 = the prefix is behind us
_env_advance() {
  case "$_ep_state" in
    2) _ep_state=1 ; return 0 ;;
    1) ;;
    *) return 1 ;;
  esac
  case "$1" in
    -u|--unset|-C|--chdir|-S|--split-string) _ep_state=2 ; return 0 ;;
    -*) return 0 ;;
    # An assignment, which is what env is usually there for.
    [A-Za-z_]*=*) return 0 ;;
    *) _ep_state=0 ; return 1 ;;
  esac
}

_command_runner() {
  local _tok _ng_out="" _tp_state=0 _ep_state=0 _pp_state=0 _pm_name="" tok
  _cr=""
  _normalize_command "$1"
  set -f
  # shellcheck disable=SC2086
  for _tok in $_ng_out; do
    # A quoted name is the same program to the shell. `quoted vitest run
    # --exclude=tests/a.test.ts` executes vitest, and _script_names_a_checker
    # unquotes before it looks -- so the predicate accepted the runner while
    # this function returned a quoted `vitest` with the quotes still on, matched no
    # tool, and every argument rule was skipped. Two readers of one token, and
    # only one of them was reading what the shell reads.
    tok="$_tok" ; _unquote_tok ; _tok="$tok"
    # `timeout` takes a duration before the command it wraps, and a duration is
    # a bare word. Without this the number became the program name, so
    # `timeout 300 vitest run` resolved to `300`, matched no tool, and every
    # argument rule was skipped -- while the predicate beside it also stopped at
    # the wrapper and reported that the script runs no test runner at all.
    # The wrapper's options come before the duration, which is why this is a
    # state machine and not a one-token lookahead; see _timeout_advance.
    if [ "$_tp_state" -ne 0 ] && _timeout_advance "$_tok"; then
      continue
    fi
    if [ "$_ep_state" -ne 0 ] && _env_advance "$_tok"; then
      continue
    fi
    if [ "$_pp_state" -ne 0 ] && _pm_advance "$_tok"; then
      continue
    fi
    case "${_tok##*/}" in
      # `env`, `cross-env` and `timeout` join the list for the reason `command`
      # and `nohup` are on it: they prefix a command without being one.
      # `cross-env` is the portable way to set a variable in a package script,
      # and this gate states a Windows host, where it is the only way -- so
      # `cross-env NODE_ENV=test vitest run` is an ordinary full-suite script,
      # and it was refused with "does not appear to run a test runner" while
      # vitest sat in the string.
      timeout) _tp_state=1; continue ;;
      # `env` and `cross-env` take options of their own before the command, and
      # `-u NAME` puts a bare word after a flag; see _env_advance.
      env|cross-env) _ep_state=1; continue ;;
      # The package managers take options of their own before the command, and
      # `--filter web` puts a bare word after a flag; see _pm_advance.
      # `bunx` is Bun's package runner -- its own docs call it an alias for
      # `bun x` and the equivalent of npx -- so a workspace with
      # `"test": "bunx vitest run"` was refused for running no test runner.
      # It takes no value-bearing options of its own, so _pm_advance's
      # keyed list has no arm for it and every `-flag` is skipped as a
      # boolean, which is the correct grammar for it. `pnpx` is pnpm's
      # spelling of the same thing and was already in one of these lists and
      # in none of the others.
      #
      # Eight lists in this file answer "is this token a package runner", and
      # adding a name to the one in the report is not the fix: with this arm
      # and the tsc reader both carrying `bunx`, a fixture whose typecheck
      # script was `bunx tsc --noEmit` was still refused for running no type
      # checker, by _script_names_a_checker's separate `runners=` string.
      # `js lane: every reader of the runner vocabulary knows the same names`
      # enumerates them from the source so the next name added has to reach
      # all of them.
      npx|bunx|pnpx|pnpm|bun|yarn|npm) _pp_state=1; _pm_name="${_tok##*/}"; continue ;;
      exec|dlx|command|nohup|time \
        |--yes|-y|run) continue ;;
      -*) continue ;;
      [A-Za-z_]*=*) continue ;;
      *) _cr="${_tok##*/}"; break ;;
    esac
  done
  set +f
}

# A command name spelled with a backslash escape is one this reader cannot see.
#
# `\trap` is the `trap` command to the shell and `rap` to the mask, because an
# escaped character is blanked -- it has to be, since keeping it would let
# `echo \done` close a block the reader is standing inside. So the escape is
# refused rather than resolved: every word rule in this file reads that mask,
# and a word hidden from all of them is a bypass of all of them at once.
_reject_escaped_word() {
  echo "Workspace ${CI_GATE_NODE_WORKSPACE} spells a word with a backslash escape:"
  echo "    $1"
  echo "  A backslash before a word character hides that word from this gate"
  echo "  while the shell still runs it: the trap command written with one"
  echo "  reads here as 'rap', and the handler was installed with this guard"
  echo "  none the wiser. Nothing that has to be read through an escape can be"
  echo "  vouched for, so it is refused rather than guessed at. Write the word"
  echo "  plainly, or move the line into a script this gate does not read."
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
}

# What a delegated wrapper is handed, judged by the rules its runner's own
# arguments meet -- because that is what they become.
#
# The wrapper reader accepts `vitest run "$@"` and says why: what a caller
# forwards is not visible from inside the script. It is visible here, and it was
# being dropped, so `"test": "bash scripts/test.sh tests/a.test.ts"` over that
# wrapper collected one file and the lane exited 0. Two halves of one question
# with only one of them asked.
_reject_forwarded_args() {
  local script_name="$1" args="$2" tok _rfa_tool="${1##*/}"
  case "$args" in
    *[![:space:]]*) ;;
    *) return 0 ;;
  esac

  # A tool this file has rules for is judged by *its* rules, through the same
  # dispatcher the direct spelling goes through.
  #
  # This path is reached when the target IS the executable -- `npx tsc
  # --noEmit`, `pnpm dlx vitest run` -- and it called _reject_narrowing_flags
  # bare, with no runner. That is vitest's and `node --test`'s vocabulary
  # applied to every tool: `npx tsc --noEmit` was refused for "narrowing a
  # suite" that does not exist, `npx jest --ci` and `npx mocha --recursive` for
  # flags the runner-keyed list beside it names as harmless, and
  # `npx playwright test` for the only invocation playwright has. The bare-word
  # loop below then refused `npx vitest run --reporter json` for the reporter's
  # value, which _reject_positional_filters knows is a value and not a filter.
  #
  # _reject_tool_args_one exists for exactly this and says so in its own
  # comment: falling through hands a type checker the test-runner allow-list and
  # refuses every ordinary invocation. It was reached from the direct path and
  # not from this one -- one rule, two spellings, on the spelling that is most
  # of the ecosystem's documentation.
  #
  # The runner is prepended to the arguments because that is the shape the
  # dispatcher's readers expect: _reject_positional_filters drops the first
  # token as "the runner or its wrapper", and handing it arguments alone would
  # eat the first one.
  case "$_rfa_tool" in
    tsc|tsc.cmd|tsgo|vue-tsc|svelte-check|astro|tsd|attw \
      |vitest|jest|mocha|ava|jasmine|tap|node|playwright|cypress|wdio|karma \
      |tsx|ts-node|deno)
      _reject_tool_args_one "$script_name" "${_rfa_tool} ${args}" "$_rfa_tool"
      return 0
      ;;
  esac

  # And for a target this file has no rules for, the conservative reading it
  # always had: a bare word forwarded into something that reaches a runner is a
  # filter, and nothing here can say otherwise.
  _reject_narrowing_flags "$script_name" "$args"
  set -f
  # shellcheck disable=SC2086
  for tok in $args; do
    case "$tok" in
      -*) continue ;;
      # Forwarding on again is not an argument this reader can resolve either
      # way, and refusing it would refuse every wrapper of a wrapper.
      '"$@"'|'$@'|'"$*"'|'$*') continue ;;
    esac
    set +f
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} passes an argument into the"
    echo "  '${script_name}' wrapper:"
    echo "    arguments: ${args# }"
    echo "    offending argument: ${tok}"
    echo "  A wrapper forwards what it is given, and this gate accepts the"
    echo "  forwarding on the grounds that what arrives cannot be seen from"
    echo "  inside the script. Here it can: a bare word reaching a test runner"
    echo "  is a filter, so the suite is reduced to whatever is named and the"
    echo "  lane still exits 0. Move the selection into a script this gate does"
    echo "  not run."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  done
  set +f
}

# A checker whose status is inverted is not a checker this gate can report on.
#
# Said out loud rather than answered with "does not appear to run a test
# runner", which is what falling through to the caller's message would have
# printed: it names one and runs it, and then reverses the answer. Two
# diagnostics in this lane have already had to be fixed for being right about
# the outcome and wrong about the cause.
_reject_negated_checker() {
  echo "Workspace ${CI_GATE_NODE_WORKSPACE} negates a checker:"
  echo "    $1"
  echo "  '!' inverts the status of the command it prefixes, so a failing suite"
  echo "  leaves the script with a zero and the lane reports PASS on it. A"
  echo "  passing suite fails the same way round. Remove the negation, or move"
  echo "  it into a script this gate does not run."
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
}

_script_names_a_checker() {
  local cmd="$1"
  local tools="${2:-tsc tsc.cmd tsgo vue-tsc svelte-check astro tsd attw}"
  local depth="${3:-0}"
  # `make` is deliberately absent. Its argument is a Makefile target, not a
  # package script and not a file, so resolving it the way the others are
  # resolved is simply wrong -- `make test` looked up a `test` *script* in the
  # manifest and accepted whatever that ran, which is not what make would do.
  # A Makefile is not something this gate reads, so `make test` is delegation it
  # cannot follow, and unresolvable delegation is refused like any other.
  local runners="npm pnpm yarn bun npx bunx pnpx turbo nx lerna bash sh zsh"
  local tok t

  [ "$depth" -ge 8 ] && return 1

  # `;` binds to the token before it. `vitest run; echo done` tokenizes as
  # `run;`, so the separator arm below never saw a `;` at all and the
  # status-preservation rule could be stepped past by deleting one space --
  # the same bypass the `||` rule had before it stopped requiring spaces.
  # Spaced out here, and only where it is really a separator: the blanked copy
  # marks quoted spans, so a `;` inside an argument is left where it is.
  #
  # `&&` gets the same treatment, and for the opposite consequence. It is the
  # one composition this function calls safe -- either the checker runs or the
  # thing before it failed and the script fails with it -- and leaving it joined
  # made `true&&vitest run` tokenize as `true&&vitest`, which is in command
  # position and is not a runner, so a legitimate script was refused as "does
  # not appear to run a test runner". Reproduced: the spaced spelling reached
  # `Running script: test`, the compact one exited 20. The `;` rule spaced its
  # separator to close a bypass; this one spaces its separator to stop
  # inventing a failure. Same normalisation, opposite direction, and both are
  # the tokenizer disagreeing with the shell.
  local _bq_state="" _bq_out="" _bq_cont=0 _bq_esc=0
  _blank_quoted "$cmd"
  local _mask="$_bq_out" _norm="" _ci=0 _cn=${#cmd}
  if [ "$_bq_esc" -eq 1 ]; then _reject_escaped_word "$1"; fi
  _reject_status_trap "$1" "$1" "$_mask"
  while [ "$_ci" -lt "$_cn" ]; do
    if [ "${_mask:$_ci:2}" = "&&" ]; then
      _norm="$_norm && "
      _ci=$((_ci + 2))
      continue
    fi
    if [ "${_mask:$_ci:1}" = ";" ]; then
      _norm="$_norm ; "
    else
      _norm="$_norm${cmd:$_ci:1}"
    fi
    _ci=$((_ci + 1))
  done
  cmd="$_norm"

  # Compositions that prove nothing. `true || vitest run` never reaches the
  # checker; `vitest run || true` reaches it and throws the result away, which
  # is worse -- the suite fails and the script still exits 0. Neither can be
  # vouched for, so `||` is refused outright rather than reasoned about
  # case by case. `&` backgrounds the checker and lets the script exit before
  # it finishes, which is the same lie with different timing.
  # Matched without requiring spaces around them. `a||b` is the same shell
  # operator as `a || b`, and a rule that only recognised the spaced spelling
  # was a bypass anyone could reach by deleting two characters.
  #
  # A pipeline goes with them. `tsc --noEmit | cat` puts the checker in command
  # position and then throws its status away -- the shell reports the *last*
  # command's, so tsc printing TS2322 arrives as a pass. Whether `pipefail` is
  # set inside the package script is not something this reader can know, so a
  # pipeline is refused for the same reason as everything else here: the result
  # cannot be shown to come from the checker.
  # `&&` is removed before the `&` test rather than enumerated around it: it is
  # the one form that is safe, since either the checker runs or the thing before
  # it failed and the script fails with it.
  # Against the blanked copy, not the raw string, for the reason
  # _reject_untrustworthy_composition now does the same: quoted text is data.
  # This is the second reader of that one rule and it had the identical fault --
  # `PATTERN='foo|bar' vitest run` returned "not a checker" here, and because a
  # runner *is* named the caller then reported the runner's status as lost to a
  # `;` the script does not contain. Fixing the composition check alone only
  # changed which wrong message came out, which is how I found this one.
  #
  # `_mask` is the mask of the command as it arrived, computed above; the
  # normalisation since then only spaced out separators, and these are substring
  # tests, so the mask is the right thing to ask.
  local _comp="${_mask//&&/}"
  case "$_mask" in
    *"|"*) return 1 ;;
  esac
  case "$_comp" in
    *"&"*) return 1 ;;
  esac

  # `node` counts as a runner only in test mode, and the question has to be
  # asked here as well as at the manifest. Otherwise the rule is right on the
  # manifest and wrong one layer down: `"test": "bash scripts/test.sh"` over a
  # script whose body is `node` reads an empty program from stdin and exits 0,
  # which is the same pass the manifest form was giving.
  #
  # Answered per command rather than by pruning `node` out of the tools list.
  # Pruning is inherited and cannot be undone: `"test": "bash scripts/nt.sh"`
  # has no `--test` of its own, so the list handed to the script would arrive
  # without `node` and the `node --test` inside it -- the correct form -- would
  # be refused one layer down.
  #
  # Per command, and `cmd` here is a *string* -- which is where this went wrong.
  # The scan looked for `--test` anywhere in it, so `"test": "echo --test &&
  # node"` had the flag belonging to `echo` license the bare `node` after the
  # separator: echo exits 0, node reads an empty program from stdin at EOF and
  # exits 0, and the lane reported PASS with no suite behind it. The comment two
  # lines up asserted the scope and the code did not implement it.
  #
  # Computed per segment, in one pass, because the flag can sit either side of
  # the runner in its own command and the main loop below reads left to right.
  # The last-token index goes with it: a runner invoked with no arguments at all
  # is a REPL that sees EOF and exits 0, which is the same empty pass in another
  # spelling, and `node` was the only one of the four that this list refused.
  local _nt=0 _t _tp_state=0 _ep_state=0 _pp_state=0 _pm_name=""
  local _cur_nt=0 _ti=0 _last=-1
  local _nt_seg=() _last_seg=()
  # shellcheck disable=SC2086
  for _t in $cmd; do
    tok="$_t" ; _unquote_tok ; _t="$tok"
    case "$_t" in
      ";"|"&&"|"|"|"("|")"|"{"|"}")
        _nt_seg[${#_nt_seg[@]}]="$_cur_nt"
        _last_seg[${#_last_seg[@]}]="$_last"
        _cur_nt=0
        _last=-1
        _ti=$((_ti + 1))
        continue
        ;;
    esac
    [ "$_t" = "--test" ] && _cur_nt=1
    _last=$_ti
    _ti=$((_ti + 1))
  done
  _nt_seg[${#_nt_seg[@]}]="$_cur_nt"
  _last_seg[${#_last_seg[@]}]="$_last"
  # The first segment's value is what the delegation path asks about when the
  # string is one command, which is overwhelmingly the common case; the loop
  # below re-reads it per segment.
  _nt="${_nt_seg[0]}"

  # An inline shell command is judged as the command it is. `bash -c tsc` runs
  # a checker and is accepted; `bash -c true` names nothing and is not. Only
  # when the thing being handed `-c` is actually a shell -- `echo -c tsc` runs
  # echo.
  local first=""
  # shellcheck disable=SC2086
  for tok in $cmd; do
    _unquote_tok
    case "$tok" in
      [A-Za-z_]*=*) continue ;;
    esac
    first="${tok##*/}"
    break
  done
  case " $first " in
    " bash "|" sh "|" zsh ")
      case " $cmd " in
        *" -c "*)
          local inline="${cmd#* -c }"
          tok="$inline"
          _unquote_tok
          inline="$tok"
          _script_names_a_checker "$inline" "$tools" "$((depth + 1))" || return 1
          # The argument rules travel into the string as well. Recursing and
          # returning meant `bash -c 'vitest run tests/only.test.ts'` reached
          # the runner, satisfied every composition rule and ran four tests --
          # accepted here while the identical command spelled directly, or
          # through a shell script, or through a package script, was refused.
          # Fourth spelling of one delegation, fourth time the rule had to be
          # carried to it; the dispatcher is shared now so there is one place
          # left to carry it to.
          _reject_tool_args "$inline" "$inline" "$tools"
          return 0
          ;;
      esac
      ;;
  esac

  # One pass, tracking whether the next token starts a command.
  #
  # Reaching a checker is necessary and not sufficient: its status has to
  # survive to become the script's. `tsc --noEmit ; true` reaches the compiler
  # and then throws its result away, because the shell reports the *last*
  # command's status -- so a failing tsc arrived as a pass, satisfying every
  # rule here by a token that runs after the one being vouched for.
  #
  # `;` is therefore not the same separator as `&&`. After `&&` the checker's
  # failure short-circuits and the composite fails with it; after `;` the next
  # command's status replaces it. So the scan no longer returns the moment it
  # sees a checker -- it records the hit and keeps reading, and a command
  # sequenced after one with `;` refuses.
  local expect_cmd=1 runner="" target="" found=0 seq_after=0 hit=0 pending_exit=0
  local _rdt_fwd=0
  # What the current delegation is being handed, if anything.
  local _fwd=""
  # Whether the command now being read is prefixed by `!`, which inverts its
  # status. Per command, cleared at every separator, because that is the scope
  # the shell gives it: in `! command -v foo && vitest run` the negation belongs
  # to the lookup and not to the suite.
  local neg=0
  # Where in the string the loop is, so the per-command answers computed above
  # can be read back for the command it is actually reading.
  local _mi=-1 _seg_i=0
  # shellcheck disable=SC2086
  for tok in $cmd; do
    _unquote_tok
    _mi=$((_mi + 1))
    case "$tok" in
      ";"|"&&"|"|"|"("|")"|"{"|"}")
        # A separator ends the current command, so any delegation it was
        # carrying has to be resolved *here*. Clearing it first meant
        # `bash scripts/test.sh ; exit 0` threw away the target before anything
        # asked what was in it, and a wrapper followed by an exit -- an entirely
        # ordinary script -- was rejected.
        if [ -n "$runner" ] && [ -n "$target" ]; then
          if _resolve_delegated_target "$target" "$tools" "$depth" "$_nt"; then
            if [ "$neg" -eq 1 ]; then _reject_negated_checker "$1"; fi
            if [ "$_rdt_fwd" -eq 1 ]; then
              _reject_forwarded_args "$target" "$_fwd"
            fi
            found=1
          fi
        fi
        if [ "$found" -eq 1 ] && [ "$tok" = ";" ]; then seq_after=1; fi
        expect_cmd=1
        runner=""
        target=""
        _fwd=""
        neg=0
        _tp_state=0
        _ep_state=0
        _pp_state=0
        _seg_i=$((_seg_i + 1))
        _nt="${_nt_seg[$_seg_i]:-0}"
        continue
        ;;
    esac

    if [ "$expect_cmd" -eq 1 ]; then
      # A command sequenced after the checker with `;` replaces its status.
      # Asked before anything else here, because the question is not what this
      # token runs -- it is that it runs at all.
      #
      # The exception is the same one the script reader makes: a bare `exit`
      # and `exit $?` leave with the status already there, and `exit 1` forces
      # a failure, which cannot become a false pass. `exit 0` and everything
      # else cannot be vouched for.
      if [ "$seq_after" -eq 1 ]; then
        if [ "$pending_exit" -eq 1 ]; then
          case "$tok" in
            '$?'|'"$?"'|[1-9]*) break ;;
          esac
          return 1
        fi
        case "${tok##*/}" in
          exit|return) pending_exit=1; continue ;;
        esac
        return 1
      fi
      # `!` is a reserved word in command position, and it inverts the status of
      # the command it prefixes. Read as a separator -- which it was -- `!
      # ./scripts/vitest run` looked like an empty command followed by a runner
      # in command position, and every rule here was satisfied while the shell
      # turned the suite's failure into a zero. A negated checker is the plainest
      # form of the thing this whole function is for: the result that reaches the
      # lane is not the result the checker produced.
      #
      # Recorded rather than refused outright, because the negation binds to one
      # command. It condemns the command it prefixes, and nothing after the next
      # separator.
      case "$tok" in
        '!') neg=1; continue ;;
      esac
      # `NODE_ENV=test vitest run` still starts with vitest.
      case "$tok" in
        [A-Za-z_]*=*) continue ;;
      esac
      # And so does `exec vitest run`, which is how a wrapper script normally
      # hands over. These replace the current process or merely prefix it; the
      # token after them is still where the command starts, so `continue`
      # without clearing expect_cmd.
      #
      # `env`, `cross-env` and `timeout` are the same shape and were missing, so
      # the scan classified the wrapper as "some other command", cleared
      # expect_cmd, and dropped every token after it -- `vitest` included. An
      # ordinary `cross-env NODE_ENV=test vitest run` was refused with "does not
      # appear to run a test runner" while the runner sat in the string, and
      # cross-env is the portable way to set a variable in a package script on
      # the Windows host this gate supports.
      # The wrapper's own options come before the duration; see
      # _timeout_advance, which is the one reader of that grammar now.
      if [ "$_tp_state" -ne 0 ] && _timeout_advance "$tok"; then
        continue
      fi
      if [ "$_ep_state" -ne 0 ] && _env_advance "$tok"; then
        continue
      fi
      if [ "$_pp_state" -ne 0 ] && _pm_advance "$tok"; then
        continue
      fi
      case "${tok##*/}" in
        # A duration is a bare word, so it has to be stepped over explicitly or
        # it becomes the command.
        timeout) _tp_state=1; continue ;;
        env|cross-env) _ep_state=1; continue ;;
        exec|command|nohup|time) continue ;;
      esac
      # The package managers are deliberately absent from that list, unlike in
      # every other reader here. To those readers a manager is a prefix to step
      # over; to this one it is the delegating runner itself, and stepping over
      # it leaves `npm run typecheck:all` with no runner, no target and no
      # delegation to follow. Its option grammar is read below, where the
      # delegation target is chosen -- which is the token the grammar was
      # getting wrong.
      # A command that ends the shell ends the scan with it. `"test": "exit 0 ;
      # vitest run"` put the runner in command position after a separator, so
      # every rule above was satisfied by a token the shell never reaches. The
      # separator resets *where a command starts*, which is not the same as
      # whether one runs.
      case "${tok##*/}" in
        exit|return)
          # Same as a separator: whatever ran before this still ran.
          if [ -n "$runner" ] && [ -n "$target" ]; then
            if _resolve_delegated_target "$target" "$tools" "$depth" "$_nt"; then
              if [ "$neg" -eq 1 ]; then _reject_negated_checker "$1"; fi
              if [ "$_rdt_fwd" -eq 1 ]; then
                _reject_forwarded_args "$target" "$_fwd"
              fi
              found=1
            fi
          fi
          break
          ;;
      esac
      hit=0
      for t in $tools; do
        if [ "${tok##*/}" = "$t" ]; then
          # `node` without `--test` runs an empty program from stdin.
          if [ "$t" = "node" ] && [ "$_nt" -eq 0 ]; then continue; fi
          # And the three beside it do the same thing invoked bare. `tsx`,
          # `ts-node` and `deno` with no arguments drop into a REPL, which under
          # this gate reads EOF immediately and exits 0 -- `"test": "tsx"` was
          # accepted and ran nothing. The rule existed for `node` and was absent
          # on the three names next to it in the same list. What makes them
          # runners is what they are pointed at, so the test is whether anything
          # follows in this command at all.
          case "$t" in
            tsx|ts-node|deno)
              if [ "$_mi" -ge "${_last_seg[$_seg_i]:--1}" ]; then continue; fi
              ;;
          esac
          hit=1
          break
        fi
      done
      if [ "$hit" -eq 1 ]; then
        if [ "$neg" -eq 1 ]; then _reject_negated_checker "$1"; fi
        found=1
        expect_cmd=0
        continue
      fi
      for t in $runners; do
        if [ "${tok##*/}" = "$t" ]; then
          runner="$tok"
          # And now that the manager is the runner, its own options are read.
          case "${tok##*/}" in
            pnpm|npm|yarn|bun|npx|bunx|pnpx) _pp_state=1 ; _pm_name="${tok##*/}" ;;
          esac
          expect_cmd=0
          continue 2
        fi
      done
      # Some other command. Its arguments are arguments, not commands.
      expect_cmd=0
      continue
    fi

    # Arguments of whatever is currently running. Only a runner has a
    # delegation target worth resolving.
    [ -n "$runner" ] || continue
    if [ -n "$target" ]; then
      # Everything after the target is handed *to* the target, and the wrapper
      # reader explicitly allows `vitest run "$@"` -- it has to, since what a
      # caller forwards is not visible from inside the script. This is where it
      # is visible, and the tokens were being dropped: `"test": "bash
      # scripts/test.sh tests/a.test.ts"` over that wrapper ran one test file
      # and the lane exited 0. The two halves of one question, and only one of
      # them was being asked.
      case "$tok" in
        run|exec|dlx|--) continue ;;
      esac
      _fwd="${_fwd} ${tok}"
      continue
    fi
    # A package manager's own option can take its value as a separate word, and
    # that word was becoming the delegation target: `pnpm --filter web vitest
    # run` resolved `web`, which names no script and no tool, so the standard
    # monorepo invocation was refused for running no test runner while the
    # runner sat two tokens to the right. `--filter=web` carries its value in
    # the same token and needs nothing here. See _pm_advance.
    if [ "$_pp_state" -ne 0 ] && _pm_advance "$tok"; then
      continue
    fi
    case "$tok" in
      -*) continue ;;
      run|exec|dlx|--) continue ;;
    esac
    target="$tok"
  done

  if [ "$found" -eq 1 ]; then return 0; fi
  [ -n "$runner" ] && [ -n "$target" ] || return 1
  _resolve_delegated_target "$target" "$tools" "$depth" "$_nt" || return 1
  # `! bash scripts/test.sh` reaches the end of the string with the delegation
  # still pending, so the negation has to be answered here too, and so does what
  # the delegation was handed.
  if [ "$neg" -eq 1 ]; then _reject_negated_checker "$1"; fi
  if [ "$_rdt_fwd" -eq 1 ]; then
    _reject_forwarded_args "$target" "$_fwd"
  fi
  return 0
}

_filter_reject() {
  echo "Workspace ${CI_GATE_NODE_WORKSPACE} narrows its own suite in the '$1' script:"
  echo "    $1: $2"
  echo "  '$3' selects a subset, so the lane can exit 0 with the rest of the"
  echo "  suite never collected. Remove it, or move the selection into a"
  echo "  separate script this gate does not run."
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
}

assert_no_persistent_filter() {
  local script_name="$1" cmd tok runner="" is_test_runner=0 _cr=""
  cmd="$(script_command "$script_name")"
  [ -n "$cmd" ] || return 0

  # Which program is this script actually running? Skip the package-manager
  # wrappers a script may be written through, then take the first real word.
  #
  # `command`, `nohup` and `time` are on that list because the checker resolver
  # already steps over them, and the two disagreeing is the whole bug: `"test":
  # "command vitest run --exclude=tests/a.test.ts"` recorded `command` as the
  # runner here, so is_test_runner stayed 0 and the argument rules never ran,
  # while _script_names_a_checker skipped the prefix and accepted vitest. The
  # lane exited 0 with the exclusion never inspected.
  _command_runner "$cmd"
  runner="$_cr"
  case "$runner" in
    vitest|jest|mocha|ava|jasmine|tap) is_test_runner=1 ;;
    node)
      # `node` is a runner only in test mode. Bare `node` takes its program
      # from stdin, and under the gate stdin is already at EOF: it runs an
      # empty program and exits 0. `"test": "node"` then satisfied every rule
      # below by having no further tokens to inspect, so the whole suite left
      # the gate on a one-word manifest edit with the lane reporting PASS.
      #
      # `node --test` is the mode that collects and runs anything, which is
      # what the tools list means by `node`.
      # shellcheck disable=SC2086
      for tok in $cmd; do
        if [ "$tok" = "--test" ]; then is_test_runner=1; break; fi
      done
      ;;
  esac

  # And if it is not a runner, it has to be something that could reach one.
  # `"test": "true"` runs, exits 0, and collects nothing -- the whole suite
  # removed from the gate by a one-word manifest edit, with the lane still
  # reporting PASS over a workspace that still contains tests. The presence of
  # a `test` key was never the property worth asserting, exactly as it was not
  # for `typecheck`; this is that rule one script over.
  #
  # Compositions whose outcome cannot be trusted, on either side of the runner.
  #
  # Asked here rather than left to the rules below, because those reached the
  # right verdict for the wrong reason and said so out loud: `vitest run || true`
  # was rejected with "'true' selects a subset, so the lane can exit 0 with the
  # rest of the suite never collected", which is not what is wrong with it. The
  # suite runs in full and its result is then discarded. A developer sent to
  # look for a filter would find none.
  #
  # `true || vitest run` is the other half: the runner is never reached at all.
  # `&` is the same lie with different timing -- the script exits before the
  # suite it backgrounded has finished.
  _reject_untrustworthy_composition "$script_name" "$cmd"

  # Delegation is accepted only when what it hands to can be read and reaches a
  # checker. A no-op is not delegation.
  #
  # Asked of every script, not only of the ones whose first word is not a
  # runner. This predicate answers two questions at once -- is a runner reached,
  # and does its status survive to become the script's -- and skipping it for a
  # direct invocation skipped the second one with it. `"test": "vitest run;true"`
  # was then caught only by the *filter* rule, which read `run;true` as a
  # positional and refused it for narrowing a suite it does not narrow. An
  # accidental protection, and it stopped being one the moment that rule learned
  # where a command ends.
  #
  # `"test": "bash -c true"` is the no-op this rule rejects wearing a wrapper,
  # so an inline `-c` command is judged on its own contents like any other.
  if _script_names_a_checker "$cmd"     "vitest jest mocha ava jasmine tap node playwright cypress wdio karma tsx ts-node deno"; then
    : # names a runner whose result survives, or delegates to one
  elif [ "$is_test_runner" -eq 1 ]; then
    # A runner *is* named here, so "does not appear to run a test runner" would
    # be false and would send a developer looking for a missing runner that is
    # sitting in front of them. What failed is the second half.
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} runs a test runner in the '${script_name}'"
    echo "  script whose result does not become the script's:"
    echo "    ${script_name}: ${cmd}"
    echo "  A command sequenced after the runner with ';' replaces its status,"
    echo "  so the suite can fail and the script still exit 0."
    echo "  '&&' is the composition that keeps it: either the runner passes and"
    echo "  the next command runs, or the script fails with it. A bare 'exit',"
    echo "  'exit \$?' and 'exit 1' are fine for the same reason -- none of them"
    echo "  can turn a failure into a pass."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  else
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} defines a '${script_name}' script that does"
    echo "  not appear to run a test runner:"
    echo "    ${script_name}: ${cmd}"
    echo "  A script named ${script_name} that exits 0 without collecting anything"
    echo "  removes the whole suite from the gate while the lane still reports"
    echo "  PASS. Recognised: vitest, jest, mocha, ava, jasmine, tap, node --test, a"
    echo "  wrapper script, or a runner that delegates to one. Add yours here"
    echo "  if it belongs."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi

  # The flags are an allow-list for a known runner, not a deny-list.
  #
  # Enumerating the narrowing ones lost this race three times: -t, then a bare
  # positional file, then `--exclude` and `--passWithNoTests`, which together
  # make `vitest run` print "No test files found", exit 0 and satisfy every
  # earlier rule. So the question is inverted, as it already was for the config
  # properties: a flag that cannot reduce what is collected or run is named
  # here, and anything else stops the guard until someone decides which it is.
  #
  # `--passWithNoTests` is the sharpest of them and would be excluded by name
  # even if the list were kept: it converts "collected nothing" into success,
  # which is the precise failure this whole lane exists to catch.
  #
  # `--allowOnly` was on this list and does not belong on it. Vitest defaults
  # `allowOnly` to off under CI, so a committed `it.only` fails the run; the
  # flag turns that back on, and an accidental `.only` then reduces the suite to
  # one test while the run exits 0. That is narrowing, applied to the whole
  # suite by a single stray edit somewhere else in the tree. `--no-allowOnly`
  # stays: it asks for the CI default and cannot reduce anything.
  #
  # `--watch` and `--update` were on this list and do not belong on it.
  #
  # Watch mode does not end. The suite reaches "Waiting for file changes"
  # and stays alive until the timeout in ci/lib/runner.sh kills it, so the
  # lane reports infrastructure failure instead of a result -- a blocking
  # check that can never complete. `--no-watch` stays: it asks for the
  # behaviour this gate needs.
  #
  # `--update` rewrites snapshots to match whatever the code now produces
  # and exits 0, so a mismatched inline snapshot -- a real regression -- is
  # recorded as the new expectation and reported as a pass. That is
  # narrowing of a different kind: not fewer tests, but tests that cannot
  # fail. `--no-update` stays for the same reason `--no-allowOnly` does.
  #
  # Node's own runner flags are here because `node --test` is the only spelling
  # of that runner this gate accepts, and rejecting the flag that makes it a
  # runner left the correct form failing the lane. The narrowing ones --
  # `--test-name-pattern`, `--test-only`, `--test-skip-pattern`, `--test-shard`
  # -- are absent, which under an allow-list is all that is needed.
  # Unconditionally, and not only when the *first* command is a runner: the
  # dispatcher asks per command now, so `"test": "echo hi && vitest run
  # --exclude=x"` -- which leaves is_test_runner at zero and is accepted by the
  # predicate above, since the runner is there after the separator -- has its
  # exclusion inspected like any other.
  _reject_tool_args "$script_name" "$cmd" \
    "vitest jest mocha ava jasmine tap node playwright cypress wdio karma tsx ts-node deno"
}

# The non-narrowing flag allow-list, as its own function so a package script
# reached through another one is read the same way as a direct command.
_reject_narrowing_flags() {
  local script_name="$1" cmd="$2" runner="${3:-}" tok _tp_state=0 _ep_state=0 _pp_state=0 _pm_name="" _rnf_word
  # shellcheck disable=SC2086
  for tok in $cmd; do
    # A `timeout` wrapper's own options are not the runner's. This scan reads
    # every `-` word in the script and judges it against the runner's
    # vocabulary, so `timeout --foreground 300 vitest run` was refused for
    # "selecting a subset" on a flag that belongs to timeout and selects
    # nothing. Before the `-*` filter below, because that is what the wrapper's
    # options look like.
    if [ "$_tp_state" -ne 0 ] && _timeout_advance "$tok"; then
      continue
    fi
    # And an `env` prefix's own options, for the same reason: `-u`, `-i` and
    # `-C` are env's vocabulary and this scan judges every `-` word against the
    # runner's.
    if [ "$_ep_state" -ne 0 ] && _env_advance "$tok"; then
      continue
    fi
    if [ "$_pp_state" -ne 0 ] && _pm_advance "$tok"; then
      continue
    fi
    # By basename and unquoted, because both spellings are the same wrapper to
    # the shell. `/usr/bin/env VAR=v cmd` is an ordinary prefix and `'timeout'
    # 300 vitest run` an ordinary quoting, and each was read here as a flag or a
    # filter belonging to the runner. The path spelling was already handled for
    # `timeout` in the preamble of _reject_positional_filters and nowhere else,
    # which is the same rule-in-one-reader shape twice over.
    # The basename is computed for the wrapper comparison and is NOT written
    # back over the token. Taking the basename of an exclude flag carrying a
    # path leaves the bare file name, which stops looking like a flag at all,
    # and the narrowing rule this function exists for stops firing. The
    # unquoting is written back, because a quoted flag is the same flag.
    #
    # Spelled without the flag-and-value example it used to carry: DeepSource's
    # secret matcher read that as an assignment and reported the token beside it
    # as a hardcoded credential, failing the Secrets analyzer on 1e72261d over a
    # comment. Rewriting the sentence is cheaper than arguing with a syntax
    # matcher, and disposing of it as a false positive is not on the table.
    _unquote_tok
    _rnf_word="${tok##*/}"
    case "$_rnf_word" in
      timeout) _tp_state=1 ; continue ;;
      env|cross-env) _ep_state=1 ; continue ;;
      pnpm|npm|yarn|bun|npx|bunx|pnpx) _pp_state=1 ; _pm_name="$_rnf_word" ; continue ;;
    esac
    case "$tok" in
      -*) ;;
      *) continue ;;
    esac
    case "${tok%%=*}" in
      # `--config`/`-c` and `--root` are gone from this list deliberately.
      #
      # They do not narrow what vitest collects; they change *which config
      # declares* what it collects, which is worse. test-layout.sh validates
      # frontend/vitest.config.ts and nothing else, so `vitest run --config
      # vitest.narrow.config.ts` had the two checks reporting on two different
      # files: one confirming a broad include that is not in force, the other
      # running a config nobody inspected. `--root` moves the whole resolution
      # base and does the same thing one level up.
      #
      # A flag that redirects the guard is not the same kind of thing as a
      # flag that cannot reduce the run, and this list was only ever an
      # allow-list for the second kind.
      --run|--no-watch|--coverage|--no-coverage|--reporter|--reporters \
        |--outputFile|--outputTruncateLength|--mode|--silent \
        |--color|--no-color|--logHeapUsage|--pool|--poolOptions|--isolate \
        |--no-isolate|--threads|--no-threads|--file-parallelism \
        |--no-file-parallelism|--maxWorkers|--minWorkers|--maxConcurrency \
        |--environment|--globals|--no-allowOnly|--testTimeout \
        |--hookTimeout|--teardownTimeout|--sequence|--no-update \
        |--disable-console-intercept|--printConsoleTrace|--typecheck \
        |--no-typecheck|--yes|-y \
        |--test|--test-reporter|--test-reporter-destination \
        |--test-concurrency|--experimental-test-coverage)
        ;;
      *)
        # The list above is vitest's and `node --test`'s vocabulary, and it was
        # applied to every runner this file recognises. `jest --ci` and `mocha
        # --recursive` were both refused for "selecting a subset" -- and
        # `--recursive` makes mocha collect *more* -- so every runner past those
        # two was unusable under a gate that names it as recognised.
        #
        # Keyed by runner, because one list cannot describe two vocabularies:
        # `--bail` reads as ordinary and stops a suite early, `--config`
        # redirects which config declares the suite, and both stay refused for
        # every tool. What is admitted here is only what cannot reduce a run.
        #
        # An empty runner is the forwarded-argument path, where the caller did
        # not say what the flags belong to; the strictest list is the safe one
        # there, so it falls through to the refusal.
        case "${runner}|${tok%%=*}" in
          'jest|--ci'|'jest|--runInBand'|'jest|--verbose'|'jest|--detectOpenHandles' \
            |'jest|--forceExit'|'jest|--colors'|'jest|--no-colors'|'jest|--useStderr' \
            |'jest|--errorOnDeprecated'|'jest|--injectGlobals'|'jest|--cache' \
            |'jest|--no-cache'|'jest|--maxConcurrency'|'jest|--logHeapUsage' \
            |'mocha|--recursive'|'mocha|--exit'|'mocha|--parallel'|'mocha|--jobs' \
            |'mocha|--require'|'mocha|--timeout'|'mocha|--slow'|'mocha|--full-trace' \
            |'mocha|--check-leaks'|'mocha|--color'|'mocha|--no-color' \
            |'ava|--serial'|'ava|--concurrency'|'ava|--verbose'|'ava|--tap' \
            |'tap|--jobs'|'tap|--no-coverage'|'tap|--disable-coverage' \
            |'playwright|--workers'|'playwright|--retries'|'playwright|--forbid-only' \
            |'playwright|--fully-parallel'|'playwright|--headed'|'playwright|--trace' \
            |'deno|--allow-all'|'deno|--allow-read'|'deno|--allow-net' \
            |'deno|--allow-env'|'deno|--no-check'|'deno|--parallel'|'deno|--quiet')
            ;;
          *)
            _filter_reject "$script_name" "$cmd" "$tok"
            ;;
        esac
        ;;
    esac
  done
}

# Which argument rules apply to a resolved command, chosen by the tool it runs.
#
# The dispatch is the fix for handing every resolved command to the test-runner
# validator: `"typecheck": "npm run tc"` over `"tc": "tsc --noEmit"` resolves to
# a compiler, and the allow-list above does not contain `--noEmit` -- so an
# ordinary project-wide typecheck was rejected for narrowing a suite it has
# nothing to do with, and `tsc -p tsconfig.json` for pointing at "individual
# files" that are its project. Worse, `_reject_narrowing_flags` used to read
# `is_test_runner` and `runner` out of its caller's scope; reached from the
# typecheck path those names are not in scope at all, so `set -u` aborted the
# lane. The rules are per tool, so the choice of rules has to be too.
# Which command in a script the argument rules are talking about.
#
# The rules were applied to the whole string, so `vitest run && echo done` had
# `echo` and `done` read as vitest arguments and refused as filters -- a script
# that runs the full suite and whose status is the suite's, since `&&`
# short-circuits, which is exactly why _reject_untrustworthy_composition allows
# it. The composition rules decide whether a script may be composed at all; that
# question is already answered by the time this runs, and what is left is which
# words belong to which command.
#
# Split on the unquoted separators, then each piece is judged by the tool *it*
# runs. Judging only the piece that runs the runner we were handed would have
# been the looser half of the same mistake: `vitest run && jest -t x` would then
# have had the second command unvalidated, where today the flat scan catches it
# for the wrong reason.
_reject_tool_args() {
  local script_name="$1" cmd="$2" tools="$3"
  local _bq_state="" _bq_out="" _bq_cont=0 _bq_esc=0 _cr=""
  _blank_quoted "$cmd"
  local _m="$_bq_out" _i=0 _n=${#cmd} _seg="" _segs="" _sr
  # A separator in the mask is a separator in the command; one inside a quoted
  # argument has been blanked and is left where it is. `&&` is two characters
  # and produces an empty piece between them, which is skipped below.
  while [ "$_i" -lt "$_n" ]; do
    case "${_m:$_i:1}" in
      ';'|'&'|'|')
        _segs="${_segs}${_seg}"$'
'
        _seg=""
        ;;
      *) _seg="${_seg}${cmd:$_i:1}" ;;
    esac
    _i=$((_i + 1))
  done
  _segs="${_segs}${_seg}"

  local _ng_out=""
  while IFS= read -r _seg; do
    case "$_seg" in
      *[![:space:]]*) ;;
      *) continue ;;
    esac
    # Grouping blanked before anything reads the words, so the rules below see
    # the command the shell will run and not the punctuation around it. Passed
    # on in place of the original: `)` at the end of `( vitest run a.test.ts )`
    # would otherwise be one more bare word, and a bare word is what
    # _reject_positional_filters refuses.
    _normalize_command "$_seg"
    _seg="$_ng_out"
    _command_runner "$_seg"
    _sr="$_cr"
    [ -n "$_sr" ] || continue
    # Only the tools the caller is asking about. The membership test used to sit
    # at the call sites, applied to the *first* command in the string, so
    # `echo hi && vitest run --exclude=x` derived `echo`, found it in no tool
    # list and validated nothing -- while _script_names_a_checker, reading the
    # same string, found the runner after the separator and accepted it. Asked
    # per command, it is asked of the command that actually runs the tool.
    case " $tools " in
      *" $_sr "*) _reject_tool_args_one "$script_name" "$_seg" "$_sr" ;;
    esac
  done <<< "$_segs"
}

# The rules for one command, chosen by the tool it runs.
#
# The dispatch is the fix for handing every resolved command to the test-runner
# validator: `"typecheck": "npm run tc"` over `"tc": "tsc --noEmit"` resolves to
# a compiler, and the allow-list above does not contain `--noEmit` -- so an
# ordinary project-wide typecheck was rejected for narrowing a suite it has
# nothing to do with, and `tsc -p tsconfig.json` for pointing at "individual
# files" that are its project. Worse, `_reject_narrowing_flags` used to read
# `is_test_runner` and `runner` out of its caller's scope; reached from the
# typecheck path those names are not in scope at all, so `set -u` aborted the
# lane. The rules are per tool, so the choice of rules has to be too.
#
# A command running something else entirely -- `echo done` after the suite --
# has no rules here and gets none. Whether it is allowed to be there at all is
# the composition question, answered before this runs.
_reject_tool_args_one() {
  local script_name="$1" cmd="$2" runner="$3"
  case "$runner" in
    tsc|tsc.cmd|tsgo|vue-tsc)
      _reject_tsc_args "$script_name" "$cmd" "$runner"
      ;;
    svelte-check|astro|tsd|attw)
      # Type checkers whose argument grammars this gate does not model. Named
      # rather than left to fall through, because falling through hands them the
      # test-runner allow-list and refuses every ordinary invocation.
      :
      ;;
    vitest|jest|mocha|ava|jasmine|tap|node|playwright|cypress|wdio|karma \
      |tsx|ts-node|deno)
      _reject_narrowing_flags "$script_name" "$cmd" "$runner"
      # A positional argument to a test runner *is* a filter — `vitest run
      # [...filters]` is the documented syntax, and `vitest run
      # tests/lib/confidence.test.ts` ran 4 tests instead of 314 while every
      # token in the deny-list above was absent. An enumerated list of narrowing
      # flags loses this race by construction, so the rule is inverted here: for
      # a known runner, nothing but flags and their values may follow.
      _reject_positional_filters "$script_name" "$cmd" "$runner"
      ;;
    *)
      # Not a tool this file has rules for.
      :
      ;;
  esac
}

# Is this path one of the workspace's own TypeScript projects?
#
# One function because there are two spellings of the option that names it,
# `-p <file>` and `--project=<file>`, and the comment above the joined arm has
# said since it was written that the two are "judged by the same test ... rather
# than by a second rule that could drift away from it". They were two rules, and
# they drifted the moment one of them was corrected: the split spelling learned
# about tsconfig.app.json and the joined one went on refusing it. A comment that
# outlives its rule is the shape this file keeps producing, so the claim is made
# true here rather than restated.
#
# `_TS_PROJECTS` is the enumeration in _ts_project_files, rooted at the
# workspace and printed one path per line with any leading `./` removed.
_ts_project_declared() {
  local _tpd_want="${1#./}" _tpd_one
  [ -n "$_tpd_want" ] || return 1
  while IFS= read -r _tpd_one; do
    [ -n "$_tpd_one" ] || continue
    [ "$_tpd_want" = "$_tpd_one" ] && return 0
  done <<< "${_TS_PROJECTS:-}"
  return 1
}

# The argument of -p/--project/-b/--build, which tsc documents as "the path to
# its configuration file, or to a folder with a tsconfig.json" -- so a directory
# is the same argument spelled differently, and refusing it refuses a valid
# script. Measured against tsc 6.0.3 over a project holding one type error:
#
#   tsc -p app --noEmit   -> app/src/bad.ts(1,14): error TS2322 ... exit 2
#   tsc -b app            -> app/src/bad.ts(1,14): error TS2322 ... exit 1
#
# Both readers of this option call this, rather than each testing the token its
# own way. Two spellings of one question answered in two places is the defect
# this branch keeps finding.
_ts_project_arg_ok() {
  local _tpa="${1%/}"
  [ -n "$_tpa" ] || return 1
  _ts_project_declared "$_tpa" && return 0
  # A folder is named by its configuration file, which is what discovery lists.
  _ts_project_declared "${_tpa}/tsconfig.json"
}

# The refusal both spellings share, so the advice cannot drift either.
_reject_undeclared_project() {
  local script_name="$1" cmd="$2" tok="$3" opt="$4" _rup_one
  echo "Workspace ${CI_GATE_NODE_WORKSPACE} points its '${script_name}' script at"
  echo "  a project this workspace does not have:"
  echo "    ${script_name}: ${cmd}"
  echo "    offending argument: ${tok}"
  echo "  '${opt}' compiles the project that file describes, and a file"
  echo "  discovery never found is one nothing here can read to see what"
  echo "  it names. Use 'tsc --noEmit', or point it at one of this"
  echo "  workspace's own configurations:"
  while IFS= read -r _rup_one; do
    [ -n "$_rup_one" ] || continue
    echo "    ${_rup_one}"
  done <<< "${_TS_PROJECTS:-}"
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
}

# The compiler's own argument rules: the modes that do not typecheck, and the
# positionals that make it ignore tsconfig.json.
_reject_tsc_args() {
  local script_name="$1" cmd="$2" runner="$3" tok prev low _tp _tp_state=0 _ep_state=0 _pp_state=0 _pm_name=""
  prev=""
  set -f
  # shellcheck disable=SC2086
  set -- $cmd
  set +f
  while [ "$#" -gt 0 ]; do
    tok="$1"
    # Quoting removed first, because the shell removes it first.
    #
    # `tsc --no'Check'` is `tsc --noCheck` to the shell and was `--no'check'` to
    # the mode scan below, which matched no arm and fell through the generic
    # `-*` case: the typecheck lane reported PASS with the compiler told not to
    # check. Quotes are stripped wherever they sit in the token, not only at its
    # ends, since that is where a bypass would put them.
    #
    # Above the runner comparison, not below it, or the fix would have been the
    # defect it was fixing one line over: `'tsc' --noEmit` runs the compiler,
    # and with the comparison reading the quoted spelling the runner's own name
    # fell through to the trailing arm and was refused as a source file it had
    # been pointed at.
    tok="$(printf '%s' "$tok" | tr -d '\042\047')"
    # The `timeout` wrapper, which this scan had no notion of at all: the
    # duration is a bare word, so `timeout 300 tsc --noEmit` -- an ordinary way
    # to bound a compile, and the same wrapper the test lane already knew about
    # -- was refused for pointing tsc at a file named `300`. A `--noCheck`
    # behind the wrapper was never reached either, because the wrong refusal
    # came first. The test lane carried this rule in four places; the typecheck
    # lane beside it carried it in none.
    if [ "$_tp_state" -ne 0 ] && _timeout_advance "$tok"; then
      prev="" ; shift ; continue
    fi
    if [ "$_ep_state" -ne 0 ] && _env_advance "$tok"; then
      prev="" ; shift ; continue
    fi
    if [ "$_pp_state" -ne 0 ] && _pm_advance "$tok"; then
      prev="" ; shift ; continue
    fi
    case "${tok##*/}" in
      timeout) _tp_state=1 ; prev="" ; shift ; continue ;;
      # `env` was on no list in this scan at all, so `env NODE_ENV=test tsc
      # --noEmit` was refused for pointing the compiler at a file named `env`.
      env|cross-env) _ep_state=1 ; prev="" ; shift ; continue ;;
      # And the package managers, whose own options take a separate word.
      pnpm|npm|yarn|bun|npx|bunx|pnpx) _pp_state=1 ; _pm_name="${tok##*/}" ; prev="" ; shift ; continue ;;
    esac
    if [ "${tok##*/}" = "$runner" ]; then
      prev="" ; shift ; continue
    fi
    # tsc matches its options case-insensitively: `--showconfig` and
    # `--showConfig` are one option to the compiler and were two different
    # things to this rule, so every mode named below could be reached by
    # changing a letter's case. Lowered once, here, and compared lowered
    # everywhere after.
    case "$tok" in
      -*) low="$(printf '%s' "$tok" | tr '[:upper:]' '[:lower:]')" ;;
      *) low="$tok" ;;
    esac
    # And one dash is the same option to tsc as two.
    #
    # TypeScript's command line strips the leading dashes before it looks the
    # option up, so `-noCheck` is `--noCheck` to the compiler and was a token
    # this scan had never heard of: it matched no arm of the non-compiling list
    # and fell through the generic `-*` case that accepts ordinary flags. The
    # typecheck lane reported PASS with the compiler told not to type check,
    # which is precisely what that list exists to prevent, reachable by deleting
    # one character.
    #
    # Only a long option is rewritten. `-p`, `-w`, `-v`, `-h` and `-?` are
    # single-character flags the list already names in their one-dash spelling,
    # and turning those into `--p` would take them out of it.
    case "$low" in
      --*) ;;
      -[!-][!-]*) low="-${low}" ;;
    esac
    case "$low" in
      # Modes that print instead of compiling. `tsc --showConfig` resolves the
      # configuration and writes it out; it does not typecheck, so a workspace
      # holding `const n: number = "bad"` passed the lane with the compiler
      # never having looked at it. Naming the compiler is not running it, and
      # neither is running it in a mode that does not check.
      #
      # `--noCheck` is the sharpest of them and is the same statement in one
      # flag: TypeScript documents it as emitting without full type checking, so
      # `tsc --noEmit --noCheck` walks the project, reports nothing and exits 0
      # over code that does not compile.
      #
      # `--watch`/`-w` is here for the reason the same flag left the test-runner
      # allow-list: watch mode does not exit, so the lane is killed by the
      # gate's timeout and a manifest edit is reported as broken infrastructure
      # rather than as a result.
      # `--listFiles` was on this list and does not belong on it. It prints the
      # files read *during* a compilation that still happens -- a project error
      # still reports and still exits 2 -- while `--listFilesOnly` stops after
      # listing them. Two options one letter apart, one of which checks, and
      # taking them for the same thing blocked a legitimate diagnostic script
      # before it could run. The pair is the reason this list is enumerated by
      # behaviour rather than by name resemblance.
      #
      # `--clean` and `--dry` are the build-mode versions of the same
      # distinction: `tsc --build` checks, `tsc --build --clean` deletes the
      # outputs and `tsc --build --dry` reports what it would do. Both exit 0
      # over a project that does not compile.
      #
      # `-?` is the compiler's own third spelling of --help -- typescript
      # registers the option as `{ name: "help", shortName: "?" }` -- and it was
      # the one this list missed while refusing the other two. It is quoted
      # because an unquoted `?` in a case pattern matches any single character,
      # so `-?` written bare would swallow `-p`, `-b`, `-w` and every other short
      # flag: a rule against one help spelling would have refused the whole
      # short-option vocabulary.
      --showconfig|--listfilesonly|--init|--help|-h|'-?'|--version|-v \
        |--all|--nocheck|--watch|-w|--clean|--dry)
        echo "Workspace ${CI_GATE_NODE_WORKSPACE} runs a non-compiling tsc mode in its"
        echo "  '${script_name}' script:"
        echo "    ${script_name}: ${cmd}"
        echo "    offending argument: ${tok}"
        echo "  That mode does not type check the project, so the lane reports"
        echo "  PASS having checked nothing. Use 'tsc --noEmit' or"
        echo "  'tsc -p tsconfig.json --noEmit'."
        exit "$CI_RESULT_FAIL_NEW_ISSUE"
        ;;
      npx|bunx|pnpx|pnpm|bun|yarn|npm|exec|dlx|command|nohup|time|--yes|-y|run|'&&'|';')
        prev="" ; shift ; continue ;;
      # The joined spelling of the project option, which selects a project
      # exactly as the spaced form does and arrives here as a single token: it
      # matches `-p|--project` in no `prev` arm below and was accepted by the
      # generic flag case at the bottom of this list. Judged by the same test as
      # the spaced form -- the value after the `=` -- rather than by a second
      # rule that could drift away from it. Ordered above `[A-Za-z_]*=*`
      # because that arm is for environment assignments and would otherwise
      # swallow nothing here, and above `-*` because that one accepts.
      --project=*|-p=*)
        _tp="${tok#*=}"
        if _ts_project_arg_ok "$_tp"; then
          prev="" ; shift ; continue
        fi
        _reject_undeclared_project "$script_name" "$cmd" "$tok" '--project=<file>'
        ;;
      [A-Za-z_]*=*)
        prev="" ; shift ; continue ;;
      -*)
        prev="$low" ; shift ; continue ;;
    esac
    # Naming the compiler is not enough: it has to be pointed at the project.
    #
    # `tsc src/known-good.ts --noEmit` names tsc and passes every rule above,
    # and tsc documents that listing files ignores tsconfig.json entirely -- so
    # the lane typechecks one file, reports PASS, and every error in every
    # unlisted source is simply not looked for. The build does not typecheck
    # either, so nothing else catches it. That is the test-filter defect on the
    # typecheck script, and it is refused the same way: the value-taking options
    # are named, and after anything else a bare word is a source file.
    # An explicit boolean is the value of the switch before it, not a source
    # file. `tsc --noEmit --pretty false` typechecks the whole project -- tsc
    # documents `--pretty` as a boolean whose default is true -- and this scan
    # recorded `--pretty` as `prev`, found it in no value-taking arm, and
    # refused `false` as a file the compiler had been pointed at. Only `true`
    # and `false`, and only immediately after a flag: any other bare word after
    # a boolean switch is still a file.
    case "$prev|$low" in
      -*'|true'|-*'|false')
        prev="" ; shift ; continue ;;
    esac
    # A project other than the workspace configuration is the typecheck twin
    # of `vitest --config`, and it was accepted without anything asking which
    # project it selects. `tsc -p tsconfig.narrow.json --noEmit` compiles the
    # project that file describes: the default tsconfig.json can report
    # TS2322 and exit 2 while an alternate naming one known-good file exits 0,
    # so the lane reports PASS over a workspace nothing checked. Same
    # reasoning that removed `--config` from the test-runner allow-list -- a
    # flag that redirects the guard is not a flag that cannot reduce the run,
    # and this reader cannot open that file to see which it is.
    case "$prev" in
      -p|--project|-b|--build)
        # A project this workspace actually has, not the default name alone.
        #
        # The rule was `tsconfig.json` or nothing, which was right while that
        # was the only name discovery knew. It is not any more: a workspace
        # whose only project is `tsconfig.app.json` is now required to have a
        # typecheck script, and `tsc --noEmit` there finds no configuration to
        # read while `tsc -p tsconfig.app.json --noEmit` was refused -- a
        # requirement with no way to satisfy it, created by widening discovery
        # two commits ago and reported before anyone hit it.
        #
        # What keeps the original protection is the sibling lane, and the first
        # version of this comment was wrong about it. It said ci/checks/
        # typecheck.sh compiles every discovered project "so naming one project
        # here cannot take another out of the run" -- reasoning about a lane
        # instead of checking that it runs. It did not run: typecheck-js is
        # registered in ci/checks/manifest.yml, and `grep typecheck.sh
        # ci/preflight.sh ci/config/lanes.conf` returned nothing, because
        # preflight maps that id back to this combined node lane. So a script
        # naming one project really did let the others ship uncompiled, and the
        # justification was false rather than merely incomplete.
        #
        # The lane is scheduled now, which is what makes the sentence true. If
        # it is ever taken back out of ci/preflight.sh, this rule has to become
        # "cover every discovered project" in the same commit -- the two are one
        # decision, and `preflight: the standalone typecheck lane runs` in
        # ci/tests/test_preflight.bats is what notices if they come apart.
        #
        # The old rule did not have the property either: `-p tsconfig.json` was
        # accepted in a workspace whose code lives under tsconfig.app.json.
        #
        # A path that is not a discovered project is still refused, because that
        # is a file neither reader can see.
        #
        # `-b`/`--build` names projects here for the same reason `-p` does, and
        # was refused: `-b` matched no arm, fell to the generic `-*` accept, and
        # the project after it arrived as a bare word that this scan calls a
        # source file. So `tsc -b tsconfig.app.json --noEmit` -- a workspace
        # using build mode, which is how a repository with project references
        # typechecks at all -- could not be written in a way this lane accepts.
        #
        # It is a typechecking mode, verified rather than assumed, against
        # tsc 6.0.3 over a composite project holding one type error:
        #
        #   tsc -b tsconfig.app.json           -> TS2322, exit 1
        #   tsc -b tsconfig.app.json --noEmit  -> TS2322, exit 1
        #   (fix the file, build, then break it again) -> TS2322, exit 1
        #
        # The third row is the one that mattered: build mode skips a project it
        # considers up to date, so the question was whether a stale .tsbuildinfo
        # could carry a green result over changed sources. It cannot -- a source
        # edit invalidates the entry and the project is checked again -- which is
        # why `-b` belongs with `-p` rather than beside `--clean` and `--dry`,
        # the two build-mode spellings above that really do exit 0 having
        # compiled nothing.
        #
        # Build mode takes one or more projects, so the option stays in force
        # across the words that follow it: `tsc -b tsconfig.app.json
        # tsconfig.node.json` names two, and spending `prev` on the first would
        # refuse the second as a source file -- the same defect one token later.
        if _ts_project_arg_ok "$tok"; then
          case "$prev" in
            -b|--build) shift ; continue ;;
          esac
          prev="" ; shift ; continue
        fi
        _reject_undeclared_project "$script_name" "$cmd" "$tok" "${prev} <file>"
        ;;
    esac
    case "$prev" in
      --outdir|--outfile|--target|--module|--moduleresolution \
        |--lib|--jsx|--jsxfactory|--jsxfragmentfactory|--typeroots|--types \
        |--rootdir|--rootdirs|--baseurl|--tsbuildinfofile|--declarationdir \
        |--maxnodemodulejsdepth|--charset|--locale \
        |--newline|--reactnamespace)
        ;;
      *)
        echo "Workspace ${CI_GATE_NODE_WORKSPACE} points its '${script_name}' script at"
        echo "  individual files:"
        echo "    ${script_name}: ${cmd}"
        echo "    offending argument: ${tok}"
        echo "  Naming files on the command line makes tsc ignore tsconfig.json,"
        echo "  so only those files are compiled and every error elsewhere goes"
        echo "  unreported while the lane exits 0. Point it at the project"
        echo "  instead -- 'tsc --noEmit', or 'tsc -p tsconfig.json --noEmit'."
        exit "$CI_RESULT_FAIL_NEW_ISSUE"
        ;;
    esac
    prev=""
    shift
  done
}

# Whether a known runner is handed a positional, which is a filter.
#
# Its own function because the delegated path needs it too. A `test` script
# reading `bash scripts/test.sh` leaves is_test_runner at zero, so a script
# whose body is `vitest run tests/only.test.ts` skipped this entirely -- the
# rule held for the direct spelling and not for the one layer down, which is
# the same right-rule-wrong-tree shape this lane has had to fix twice.
_reject_positional_filters() {
  local script_name="$1" cmd="$2" runner="$3" tok prev _q _tp_state=0 _ep_state=0 _pp_state=0 _pm_name="" _rpf_head
  prev=""
  # shellcheck disable=SC2086
  set -- $cmd
  # The wrapper's own argument goes with it. This drops one token as "the runner
  # or its wrapper", and `timeout` is a wrapper that carries a duration -- a
  # bare word, which the scan below then read as a file the runner had been
  # pointed at, so `timeout 300 vitest run` was refused for narrowing a suite it
  # does not narrow. Answered here because the arm inside the loop cannot: the
  # wrapper in front position has already been shifted away by the time the loop
  # starts.
  # Unquoted first: a quoted wrapper name is the same wrapper to the shell, and
  # this preamble compares before anything strips the quotes -- so `'timeout'
  # 300 vitest run` fell to the `*)` arm, the wrapper was dropped as "the runner
  # or its wrapper", and the duration behind it was read as a test filter. Three
  # readers unquote before their wrapper arms and two did not; this is one of
  # the two.
  _rpf_head="${1:-}"
  tok="$_rpf_head" ; _unquote_tok ; _rpf_head="$tok"
  case "$_rpf_head" in
    timeout|*/timeout)
      shift
      # Everything else the wrapper owns -- its options, an option's value, and
      # the duration -- is consumed by the loop below through _timeout_advance,
      # rather than by a second copy of that grammar here. The copy is what
      # broke: it stepped over one numeric token, so `timeout --foreground 300
      # vitest run` left `300` for the scan to read as a test filter.
      _tp_state=1
      ;;
    # And `env`, for the same reason and with the same fault available: this
    # preamble drops the first token as "the runner or its wrapper", so the
    # in-loop arm that would have recognised `env` never sees it, and
    # `env -u NODE_ENV vitest run` left `NODE_ENV` to be read as a test filter.
    env|*/env|cross-env|*/cross-env)
      shift
      _ep_state=1
      ;;
    npx|*/npx|bunx|*/bunx|pnpx|*/pnpx|pnpm|*/pnpm|bun|*/bun|yarn|*/yarn|npm|*/npm)
      _pm_name="${_rpf_head##*/}"
      shift
      _pp_state=1
      ;;
    *) shift ;;
  esac
  while [ "$#" -gt 0 ]; do
    tok="$1"
    # Anything still owned by a `timeout` wrapper, whether it was in front of
    # the command or reached through a separator. Before the quote rejoining
    # below, because the wrapper's own words are never quoted values of the
    # runner's flags.
    if [ "$_tp_state" -ne 0 ] && _timeout_advance "$tok"; then
      prev="" ; shift ; continue
    fi
    if [ "$_ep_state" -ne 0 ] && _env_advance "$tok"; then
      prev="" ; shift ; continue
    fi
    if [ "$_pp_state" -ne 0 ] && _pm_advance "$tok"; then
      prev="" ; shift ; continue
    fi
    # A quoted value holding whitespace arrives here as several tokens, because
    # this scan word-splits and the shell does not reinterpret the quotes.
    # `--reporter "my reporter"` becomes `--reporter`, `"my`, `reporter"`, and
    # the tail word has no flag in front of it -- so a legitimate script failed
    # the lane for narrowing a suite it does not narrow.
    #
    # Rejoined before the rule is applied rather than skipped past, so the
    # verdict is unchanged for a value and for a filter alike: the run is one
    # token, and whether that token is a filter is the same question as for any
    # other. `vitest run "tests/a b.test.ts"` is still refused.
    case "$tok" in
      \"*|\'*)
        _q="${tok:0:1}"
        while [ "${#tok}" -lt 2 ] || [ "${tok: -1}" != "$_q" ]; do
          [ "$#" -gt 1 ] || break
          shift
          tok="$tok $1"
        done
        ;;
    esac
    # The runner compared by basename, because a delegated script names it by
    # path: `exec ./scripts/vitest run` puts `./scripts/vitest` here, which the
    # literal arm below cannot match, and the runner itself was then read as a
    # positional filter. The direct path never saw this because it shifts the
    # runner off before the scan starts.
    if [ -n "$runner" ] && [ "${tok##*/}" = "$runner" ]; then
      prev="" ; shift ; continue
    fi
    case "$tok" in
      # Wrapper words, the runner itself, subcommands, and shell operators are
      # not filters.
      # `watch` is not on this list. It is the subcommand spelling of the flag
      # removed from the allow-list last round, and it does the same thing:
      # `vitest watch` never returns, so the lane is killed by the runner
      # timeout and a manifest edit is reported as broken infrastructure.
      # Exempting it here left that spelling accepted while the flag was
      # refused -- the rule right on one form and absent on the other.
      watch)
        echo "Workspace ${CI_GATE_NODE_WORKSPACE} runs watch mode in the '${script_name}' script:"
        echo "    ${script_name}: ${cmd}"
        echo "  Watch mode does not exit, so this lane is killed by the gate's"
        echo "  timeout and reported as infrastructure failure rather than a"
        echo "  result. Use the one-shot form -- 'vitest run'."
        exit "$CI_RESULT_FAIL_NEW_ISSUE"
        ;;
      # `timeout` carries a duration, which is a bare word and was read as a
      # file the runner had been pointed at: `timeout 300 vitest run` was
      # refused for narrowing a suite it does not narrow. What follows it is
      # read by _timeout_advance above, which knows the wrapper's options.
      timeout|*/timeout)
        _tp_state=1 ; prev="" ; shift ; continue ;;
      # By path too, the way `timeout` above already is: `/usr/bin/env` is the
      # same prefix as `env`, and the two arms sat next to each other with only
      # one of them saying so.
      env|*/env|cross-env|*/cross-env)
        _ep_state=1 ; prev="" ; shift ; continue ;;
      npx|*/npx|bunx|*/bunx|pnpx|*/pnpx|pnpm|*/pnpm|bun|*/bun|yarn|*/yarn|npm|*/npm)
        _pp_state=1 ; _pm_name="${tok##*/}" ; prev="" ; shift ; continue ;;
      exec|dlx|command|nohup|time \
        |--yes|-y|run|related \
        |"$runner"|'&&'|'||'|';'|'|')
        prev="" ; shift ; continue ;;
    esac
    # The one invocation each of these tools has.
    #
    # `playwright test` and `deno test` are not filters -- they are how you run
    # the entire suite, and there is no other spelling. The exempt list above
    # carried `run`, which is vitest's and cypress's word, and nothing else, so
    # every runner on the recognised list past vitest and `node --test` was
    # refused for the subcommand that makes it run at all. Keyed by runner
    # rather than added to the flat list, because `test` is a filter to vitest
    # and a subcommand to playwright, and one list cannot say both.
    case "${runner}|${tok}" in
      'playwright|test'|'deno|test'|'cypress|run'|'wdio|run'|'karma|start')
        prev="" ; shift ; continue ;;
    esac
    case "$tok" in
      # Argument forwarding is not a filter. `vitest run "$@"` is how a wrapper
      # script passes on what it was given, and under `bun run test` it is
      # given nothing -- reading it as a positional refused every ordinary
      # wrapper in the repository the moment this rule reached delegated
      # scripts. What the caller actually forwards is not visible here either
      # way, and was not visible before this rule existed.
      '"$@"'|'$@'|'"$*"'|'$*')
        prev="" ; shift ; continue ;;
      # `NODE_ENV=test vitest run` still starts with vitest, and the assignment
      # in front of it is not a file the runner was pointed at.
      [A-Za-z_]*=*)
        prev="" ; shift ; continue ;;
      -*)
        prev="$tok" ; shift ; continue ;;
    esac
    # A bare word: the value of the flag before it, or a filter.
    #
    # Which one it is depends on whether that flag takes a value, and assuming
    # every flag does gave the positional back: `vitest run --run
    # tests/lib/confidence.test.ts` read the path as the value of `--run`,
    # which is a boolean, and collected four tests instead of the suite. The
    # rule inverted here for the flags themselves has to be inverted for their
    # arguments too -- the value-taking flags are named, and after anything
    # else a bare word is a filter.
    #
    # `--reporter=json` does not appear here on purpose: its value is already
    # attached, so a bare word after it is a filter, and the exact-match arms
    # below let it fall through to the rejection.
    # The runner-keyed ones first, for the reason the allow-list above this
    # function is runner-keyed: this list is vitest's and `node --test`'s
    # vocabulary, and it was the only list consulted for every runner. So the
    # flag allow-list said `mocha --timeout` cannot reduce a run -- by name --
    # and the reader beside it then refused `5000` as a test filter. Same for
    # `mocha --require ts-node/register`, which is the standard mocha
    # TypeScript setup, and for `playwright --workers 2` and
    # `ava --concurrency 4`. One rule in two readers that had to agree about
    # the same flag and did not.
    #
    # Only flags that list already accepts for that runner appear here. A flag
    # it refuses is refused on its own line, and exempting its value would
    # describe a state that cannot be reached.
    case "${runner}|${prev}" in
      'jest|--maxConcurrency' \
        |'mocha|--jobs'|'mocha|--require'|'mocha|--timeout'|'mocha|--slow' \
        |'ava|--concurrency' \
        |'tap|--jobs' \
        |'playwright|--workers'|'playwright|--retries'|'playwright|--trace')
        prev="" ; shift ; continue ;;
    esac
    case "$prev" in
      --reporter|--reporters|--outputFile|--outputTruncateLength|--mode \
        |--pool|--poolOptions|--maxWorkers|--minWorkers|--maxConcurrency \
        |--environment|--testTimeout|--hookTimeout|--teardownTimeout \
        |--sequence|--test-reporter|--test-reporter-destination \
        |--test-concurrency)
        ;;
      *) _filter_reject "$script_name" "$cmd" "$tok" ;;
    esac
    prev=""
    shift
  done
}

run_script() {
  local script_name="$1"
  local rc=0
  if script_exists "$script_name"; then
    echo "Running script: $script_name"
    case "$MANAGER" in
      pnpm) pnpm run "$script_name" || rc=$? ;;
      npm) npm run "$script_name" || rc=$? ;;
      yarn) yarn run "$script_name" || rc=$? ;;
      bun) bun run "$script_name" || rc=$? ;;
    esac
    if [ "$rc" -ne 0 ]; then
      # A package script does not implement the gate's result contract, and its
      # exit code collides with it: `vitest` or any tool it wraps exiting 10
      # propagated straight through as PASS_WITH_KNOWN_DEBT, so preflight
      # recorded the failed lane as passed, skipped the remaining scripts and
      # exited 0. Every nonzero script status is a failing script.
      echo "Script '${script_name}' failed with status ${rc}."
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi
  else
    echo "Skipping missing script: $script_name"
  fi
}

# A workspace that declares TypeScript must be able to typecheck it. `vite build`
# does not run tsc -- frontend/README.md says so explicitly -- so with the
# `typecheck` script removed or renamed, run_script logs "Skipping missing
# script" and the lane exits 0 after tests and a build that never looked at a
# type. The one check that would have caught it is the one that silently
# stopped running.
# Every TypeScript project in the workspace, not only one at its root.
#
# The root-file test read "does this workspace declare TypeScript" as "is there
# a tsconfig.json beside package.json", and a package whose only project is a
# nested one -- `e2e/tsconfig.json`, an ordinary shape -- answered no. The lane
# then required no typecheck script, skipped the missing one, and reported PASS
# with the nested project's type errors never looked for. Its counterpart in
# ci/checks/typecheck.sh had the same blind spot from the same root-file test.
#
# Printed one per line and read through a file so the producer's status is
# something this can act on, which is the rule the workspace enumeration in
# ci/lib/common.sh already follows: could not look is not found nothing.
_ts_project_files() {
  local _tp_list _tp_rc=0
  _tp_list="$(mktemp 2>/dev/null)" || return 2
  # Every name TypeScript's own scaffolds use, not only the default one.
  #
  # `tsconfig.app.json` and `tsconfig.node.json` are what `npm create vite` ends
  # up with, and a workspace whose only project is one of those answered "no
  # TypeScript here": no `typecheck` script was required of it, ci/checks/
  # typecheck.sh skipped it for the same reason, and a Vite build passed with
  # nothing having type checked anything. ci/lib/changeset.sh has classified
  # `tsconfig.*.json` as TypeScript configuration since it was written, so the
  # scheduler already knew about a file the two lanes it schedules could not
  # see -- the third reader of one question, and the only one that had it right.
  #
  # Four lists ask this one question and all four have to agree:
  # ci/checks/typecheck.sh, ci/lib/changeset.sh's classifier arm, the orphan
  # scan near the top of this file, and here. The orphan scan was the one that
  # did not, and it was found by sweeping for the others rather than reported.
  find . -name 'node_modules' -prune -o -name '.git' -prune -o \
    -type f \( -name 'tsconfig.json' -o -name 'tsconfig.*.json' \
               -o -name 'jsconfig.json' -o -name 'jsconfig.*.json' \) \
    -print 2>/dev/null \
    > "$_tp_list" || _tp_rc=$?
  if [ "$_tp_rc" -ne 0 ]; then
    rm -f "$_tp_list"
    return 2
  fi
  sed 's|^\./||' "$_tp_list" | sort
  rm -f "$_tp_list"
}

_TS_PROJECTS="$(_ts_project_files)" || {
  echo "Workspace ${CI_GATE_NODE_WORKSPACE}: cannot enumerate its TypeScript projects."
  echo "  An empty list reads exactly like a workspace with none, which is why"
  echo "  this is a failure and not an answer."
  exit "$CI_RESULT_FAIL_INFRA"
}

if [ -n "$_TS_PROJECTS" ]; then
  if ! script_exists "typecheck"; then
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} declares TypeScript configuration but"
    echo "  defines no 'typecheck' script, so nothing here ever runs tsc:"
    while IFS= read -r _ts_proj; do
      [ -n "$_ts_proj" ] || continue
      echo "    ${CI_GATE_NODE_WORKSPACE}/${_ts_proj}"
    done <<< "$_TS_PROJECTS"
    echo "  A bundler build is not a typecheck. Restore the script, or remove"
    echo "  the TypeScript configuration with it."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi
  # And it has to be a typecheck, not merely a key with that name. `"typecheck":
  # "true"` satisfies a presence test, exits 0, and never invokes a compiler --
  # a workspace containing `const n: number = "not a number"` passed the lane
  # with no type checker installed at all. That is the same defect the presence
  # rule above was written to close, reached by editing the command instead of
  # deleting the key, so presence is not the property worth asserting.
  #
  # An allow-list, like the test-script filter: a command that cannot typecheck
  # is not something to classify, and a checker this does not know about stops
  # the lane until someone says which it is.
  #
  # Delegation counts only when it delegates to something. Accepting any token
  # from a runner list meant `typecheck: "bash -c true"` satisfied the guard --
  # the same no-op the rule was written to reject, one wrapper out. So a
  # delegation has to *name* what it hands to: a script path, or a package
  # script by name. `bash scripts/typecheck.sh` and `npm run typecheck:all`
  # both do; `bash -c true` and `npm --version` do not, and an inline `-c`
  # command is judged on its own contents like any other.
  _tc_cmd="$(script_command typecheck)"
  # The composition check runs here too, and before the predicate, so the
  # message describes what is actually wrong. `tsc --noEmit | cat` is refused
  # either way -- the predicate returns 1 for a pipeline -- but the verdict that
  # came out was "does not appear to run a type checker", which is false: it
  # names tsc and runs it, and then throws the answer away. This PR has already
  # had to fix two diagnostics that were right about the outcome and wrong about
  # the cause; that is worth one shared function.
  _reject_untrustworthy_composition typecheck "$_tc_cmd"

  # Which program the typecheck script actually runs, by the same rule the
  # test-script guard uses: skip the package-manager wrappers, take the first
  # real word.
  _command_runner "$_tc_cmd"
  _tc_runner="$_cr"
  _tc_ok=0
  # Word-splitting is the point; globbing is not. `tsc -p tsconfig.*.json` would
  # otherwise expand against the workspace and be matched as filenames — the
  # same reason the range splitter in _semver_satisfies disables it.
  set -f
  _script_names_a_checker "$_tc_cmd" && _tc_ok=1
  set +f
  if [ "$_tc_ok" -ne 1 ]; then
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} defines a 'typecheck' script that does not"
    echo "  appear to run a type checker:"
    echo "    typecheck: ${_tc_cmd}"
    echo "  A script named typecheck that exits 0 without invoking a compiler is"
    echo "  the enabled typecheck lane silently switched off by a manifest edit."
    echo "  Recognised: tsc, tsgo, vue-tsc, svelte-check, astro, tsd, attw, or a"
    echo "  runner that delegates to one. Add yours here if it belongs."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi

  # The compiler's own argument rules -- the non-compiling modes and the
  # positional source files -- applied through the same dispatcher a delegated
  # command goes through, so the direct spelling and the delegated one cannot
  # drift apart.
  #
  # Only for a direct compiler invocation. `bash scripts/typecheck.sh` and
  # `npm run typecheck:all` are delegation, whose target is resolved and then
  # validated at the point of resolution.
  _reject_tool_args typecheck "$_tc_cmd" \
    "tsc tsc.cmd tsgo vue-tsc svelte-check astro tsd attw"
fi

# A workspace that ships tests must be able to run them. run_script only logs
# "Skipping missing script", so deleting or renaming `test` would remove the
# whole suite from the gate while the lane still exits 0 — the suite passing by
# never running, which is the failure this gate exists to catch. Keyed on the
# presence of test files rather than on checks.yml, so the rule travels with the
# workspace and a genuinely test-free workspace is unaffected.
if ! script_exists "test" && ! script_exists "test:unit"; then
  ORPHAN_TESTS="$(find . \( -name 'node_modules' -o -name 'dist' -o -name 'build' \) -prune -o \
    -type f \( -name '*.test.*' -o -name '*.spec.*' \) -print 2>/dev/null | head -5 || true)"
  if [ -n "$ORPHAN_TESTS" ]; then
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} ships tests but defines no 'test' or 'test:unit' script."
    echo "  These would never run:"
    # Read line by line: a path with a space is one file, not two.
    while IFS= read -r _orphan; do
      [ -n "$_orphan" ] || continue
      echo "    $_orphan"
    done <<< "$ORPHAN_TESTS"
    echo "  Restore the script in package.json, or remove the tests."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi
fi

# A workspace that had a suite must still have one. Deleting every file under
# tests/ leaves nothing to be orphaned, test-layout reports "0 file(s)" quite
# happily, and a successful build carries the gate to exit 0 after the suite has
# disappeared.
#
# Deliberately NOT nested under "and no test script": that made losing the whole
# suite safe as long as the script survived, and `vitest run --passWithNoTests`
# -- documented as exiting 0 when it collects nothing -- is exactly the script
# that gets left behind. The presence of a runner says nothing about whether
# there is anything left for it to run.
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1 \
  && git rev-parse --verify HEAD >/dev/null 2>&1; then
  WORKTREE_TESTS="$(find . \( -name 'node_modules' -o -name 'dist' -o -name 'build' \) -prune -o \
    -type f \( -name '*.test.*' -o -name '*.spec.*' \) -print 2>/dev/null | head -1 || true)"
  if [ -z "$WORKTREE_TESTS" ]; then
    # Which commit is "before"? HEAD is right for the pre-commit gate, where the
    # deletion is still only staged. In ship mode the deletion is already
    # committed, so HEAD carries no tests either, this comparison found nothing
    # missing, and a push that removes every test file and the test script with
    # them exited 0 -- the suite disappearing by becoming the new HEAD.
    #
    # The push base is what the remote still has, so it is what "before" means
    # for a push. Falls back to HEAD when no base is available, which is the
    # pre-commit gate and a first push; a first push that carries no tests at
    # all has no earlier state to have lost them from.
    _lost_ref="HEAD"
    if [ "${CI_GATE_MODE:-}" = "ship" ] && type ci::git::push_range >/dev/null 2>&1; then
      _lost_range="$(ci::git::push_range 2>/dev/null || true)"
      case "$_lost_range" in
        *..*) _lost_ref="${_lost_range%%..*}" ;;
      esac
      git rev-parse --verify "${_lost_ref}^{commit}" >/dev/null 2>&1 || _lost_ref="HEAD"
    fi
    # "Could not inspect the previous tree" is not "the previous tree had no
    # tests", and reading it the second way lets a deleted suite through: the
    # guard sees nothing to have lost and continues. The producer status is
    # taken before its output is filtered.
    _lost_rc=0
    _lost_raw="$(git ls-tree -r --name-only "$_lost_ref" -- . 2>/dev/null)" || _lost_rc=$?
    if [ "$_lost_rc" -ne 0 ]; then
      echo "Cannot read ${_lost_ref} to find out which tests this push had."
      echo "  Refusing to conclude the suite was never there from a listing"
      echo "  that failed to produce one."
      exit "$CI_RESULT_FAIL_INFRA"
    fi
    HEAD_TESTS="$(printf '%s\n' "$_lost_raw" \
      | grep -E '\.(test|spec)\.[cm]?[jt]sx?$' | head -5 || true)"
    if [ -n "$HEAD_TESTS" ]; then
      echo "Workspace ${CI_GATE_NODE_WORKSPACE} has lost its entire test suite."
      echo "  ${_lost_ref} carries test files here and this tree has none, so the"
      echo "  lane would pass having run no tests at all -- a test script left"
      echo "  behind does not change that, there being nothing for it to run."
      echo "  Present at ${_lost_ref}, for example:"
      while IFS= read -r _gone; do
        [ -n "$_gone" ] || continue
        echo "    $_gone"
      done <<< "$HEAD_TESTS"
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi
  fi
fi

assert_no_persistent_filter "test"
assert_no_persistent_filter "test:unit"

run_script "format:check"
run_script "lint"
run_script "typecheck"
run_script "test"
run_script "test:unit"
run_script "build"

echo "Node lane passed."
exit "$CI_RESULT_PASS"
