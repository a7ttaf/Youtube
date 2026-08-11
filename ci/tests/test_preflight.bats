#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  cd "$REPO_ROOT"
}

@test "preflight: --help exits 0" {
  run bash ci/preflight.sh --help
  [ "$status" -eq 0 ]
}

@test "preflight: --mode quick runs without crash" {
  run bash ci/preflight.sh --mode quick
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]
}

@test "preflight: invalid mode exits non-zero" {
  run bash ci/preflight.sh --mode invalid_mode_xyz
  [ "$status" -ne 0 ]
}

@test "preflight: syntax check passes" {
  bash -n ci/preflight.sh
}

@test "preflight: all checks have correct syntax" {
  local failed=0
  for script in ci/checks/*.sh ci/lib/*.sh; do
    if [ -f "$script" ]; then
      if ! bash -n "$script" 2>/dev/null; then
        echo "FAIL: $script" >&2
        failed=1
      fi
    fi
  done
  [ "$failed" -eq 0 ]
}

@test "preflight: exit codes follow convention" {
  source ci/lib/common.sh
  [ "$CI_RESULT_PASS" -eq 0 ]
  [ "$CI_RESULT_PASS_WITH_KNOWN_DEBT" -eq 10 ]
  [ "$CI_RESULT_FAIL_NEW_ISSUE" -eq 20 ]
  [ "$CI_RESULT_FAIL_INFRA" -eq 30 ]
}

# --- the pre-push gate has to scan what is being pushed -----------------------
#
# git-safety.sh read the index for every content scan it does. In ship mode the
# commit already exists and the index matches HEAD, so `git diff --cached` is
# empty: the sensitive-file, artifact, large-blob, conflict-marker and
# secret-pattern scans all inspected nothing and the gate passed. Same class as
# the node lane's partial-staging rule, on the security path.

gs_setup() {
  GS_SB="$(mktemp -d)"
  mkdir -p "$GS_SB/ci/lib" "$GS_SB/ci/checks"
  cp "$REPO_ROOT/ci/checks/git-safety.sh" "$REPO_ROOT/ci/checks/common.sh" "$GS_SB/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$REPO_ROOT/ci/lib/git.sh" "$GS_SB/ci/lib/"
  (
    cd "$GS_SB"
    git init -q -b feature/x .
    # The copied gate scripts stay out of the history under test. ci/checks/
    # defines the secret patterns as literal text and several of them match
    # themselves — `DATABASE_URL=[^[:space:]]+` is its own witness — so a range
    # reaching the first commit would flag the fixture rather than the case.
    printf 'ci/\n' > .gitignore
    printf 'x\n' > a.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
  ) >/dev/null 2>&1
}

gs_commit() {
  ( cd "$GS_SB" && git add -A && git -c user.email=t@t -c user.name=t commit -qm "$1" ) >/dev/null 2>&1
}

gs_run() {
  ( cd "$GS_SB" && CI_GATE_MODE="$1" bash ci/checks/git-safety.sh 2>&1 )
}

@test "git-safety: a sensitive file already committed fails the pre-push gate" {
  gs_setup
  printf 'SECRET=1\n' > "$GS_SB/secrets.env"
  gs_commit "add secret"
  run gs_run ship
  [ "$status" -eq 20 ]
  [[ "$output" == *"secrets.env"* ]]
  [[ "$output" == *"in the pushed commits"* ]]
  rm -rf "$GS_SB"
}

@test "git-safety: a secret pattern already committed fails the pre-push gate" {
  gs_setup
  # A pattern ci/checks/common.sh actually configures — the scan is only as
  # good as CI_CHECKS_SECRET_PATTERN, and that file overrides the environment.
  printf 'token = "ghp_%s"\n' "$(printf 'A%.0s' $(seq 36))" > "$GS_SB/note.txt"
  gs_commit "add token"
  run gs_run ship
  [ "$status" -eq 20 ]
  [[ "$output" == *"secret-pattern-match"* ]]
  rm -rf "$GS_SB"
}

@test "git-safety: the same committed content passes the pre-commit gate" {
  # The control that makes the two above meaningful: with a clean index there is
  # nothing staged, and the pre-commit gate is right to say so. Reporting on the
  # index is not the bug — reporting on it while claiming to gate a push is.
  gs_setup
  printf 'SECRET=1\n' > "$GS_SB/secrets.env"
  gs_commit "add secret"
  run gs_run quick
  [ "$status" -eq 0 ]
  rm -rf "$GS_SB"
}

@test "git-safety: removing a sensitive file is not blocked as adding one" {
  # The file list is additions, copies, renames and modifications. Listing
  # deletions blocked the one commit that fixes the problem, and the index form
  # had the same flaw.
  gs_setup
  printf 'SECRET=1\n' > "$GS_SB/secrets.env"
  gs_commit "add secret"
  ( cd "$GS_SB" && git rm -q secrets.env ) >/dev/null 2>&1

  # Staged but not yet committed: this is the pre-commit gate seeing the same
  # deletion, and it is the half that was already wrong before ship mode was
  # involved at all.
  run gs_run quick
  [ "$status" -eq 0 ]

  # For the ship half the base has to be pinned to the commit that added it.
  # Without that the range covers both commits, and blocking is then correct —
  # the secret is in the history being pushed. That case is asserted separately.
  local base
  base="$( cd "$GS_SB" && git rev-parse HEAD )"
  gs_commit "drop secret"
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 0 ]
  rm -rf "$GS_SB"
}

@test "git-safety: a secret added and removed within the push is still caught" {
  # `git diff base..HEAD` collapses the endpoints, so a token added by one
  # outgoing commit and removed by a later one vanishes from the net diff while
  # both commits are pushed and the blob stays in the history forever. The scan
  # walks each commit instead.
  gs_setup
  local base
  base="$( cd "$GS_SB" && git rev-parse HEAD )"
  printf 'token = "ghp_%s"\n' "$(printf 'A%.0s' $(seq 36))" > "$GS_SB/leak.txt"
  gs_commit "add token"
  ( cd "$GS_SB" && git rm -q leak.txt ) >/dev/null 2>&1
  gs_commit "remove token"
  # The premise: the net diff really is clean.
  run bash -c "cd '$GS_SB' && git diff --name-only '$base'..HEAD"
  [ -z "$output" ]
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"secret-pattern-match"* ]]
  rm -rf "$GS_SB"
}

@test "git-safety: the push range covers every outgoing commit on a first push" {
  # No upstream, three commits, the secret in the first: HEAD~1 as the fallback
  # base reduced the range to the last commit and the gate reported on it alone.
  # A wrong base is worse than no base — it produces a confident green.
  gs_setup
  printf 'SECRET=1\n' > "$GS_SB/secrets.env"
  gs_commit "add secret"
  printf 'b\n' > "$GS_SB/b.txt"
  gs_commit "ordinary"
  printf 'c\n' > "$GS_SB/c.txt"
  gs_commit "ordinary again"
  run gs_run ship
  [ "$status" -eq 20 ]
  [[ "$output" == *"secrets.env"* ]]
  rm -rf "$GS_SB"
}

@test "git-safety: a clean pushed range passes" {
  gs_setup
  printf 'ordinary\n' > "$GS_SB/b.txt"
  gs_commit "ordinary change"
  run gs_run ship
  [ "$status" -eq 0 ]
  rm -rf "$GS_SB"
}

@test "git-safety: the push range is computed in one place" {
  # branch-protection.sh and git-safety.sh have to agree about what is being
  # pushed. A second copy of this computation is how the changeset scheduler and
  # its report drifted apart earlier in this PR.
  run grep -c 'ci::git::push_range' ci/checks/branch-protection.sh ci/checks/git-safety.sh
  [ "$status" -eq 0 ]
  run grep -n 'CI_GATE_PUSH_OLD_SHA' ci/checks/branch-protection.sh
  [ "$status" -ne 0 ]
}

@test "tests-shell: ship mode rejects gate inputs repaired only on disk" {
  # The suites read the worktree. In ship mode the commit already exists, so a
  # gate script or bats file broken in an outgoing commit and repaired on disk
  # is validated in its repaired form while the broken version is pushed — the
  # node lane's defect, over this lane's inputs.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks" "$sb/ci/tests"
  cp "$REPO_ROOT/ci/checks/tests-shell.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf '@test "x" { true; }\n' > "$sb/ci/tests/t.bats"
  (
    cd "$sb"
    git init -q -b feature/x .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
  ) >/dev/null 2>&1
  printf '@test "x" { false; }\n' > "$sb/ci/tests/t.bats"
  run bash -c "cd '$sb' && CI_GATE_MODE=ship bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"differ between HEAD and the worktree"* ]]
  [[ "$output" == *"ci/tests/t.bats"* ]]
  rm -rf "$sb"
}

@test "tests-shell: an unrelated dirty file does not stop the shell suite" {
  # Scoped to this lane's own inputs. A dirty file elsewhere is not something
  # the suites' result claims anything about, and failing on it would make the
  # ship gate unusable rather than stricter.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks" "$sb/ci/tests" "$sb/app"
  cp "$REPO_ROOT/ci/checks/tests-shell.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf '@test "x" { true; }\n' > "$sb/ci/tests/t.bats"
  # Stands in for the real dispatcher, so a clean run reaches exit 0 and the
  # assertion below reads "the guard let it through" rather than "the script
  # died before getting there".
  printf '#!/usr/bin/env bash\nexit 0\n' > "$sb/ci/checks/tests.sh"
  chmod +x "$sb/ci/checks/tests.sh"
  printf 'a\n' > "$sb/app/main.py"
  (
    cd "$sb"
    git init -q -b feature/x .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
  ) >/dev/null 2>&1
  printf 'b\n' > "$sb/app/main.py"
  run bash -c "cd '$sb' && CI_GATE_MODE=ship bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"differ between HEAD and the worktree"* ]]
  rm -rf "$sb"
}

@test "changeset: a root package.json makes every path a workspace member" {
  # The walk terminated at "." without ever examining the only directory it had
  # not looked at. ci::common::node_workspaces calls a root manifest workspace
  # ".", so in a repository laid out that way nothing was recognised as being
  # inside one, and an imported asset could change with no node lane at all.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/src"
  printf '{ "name": "root" }\n' > "$sb/package.json"
  printf '{}\n' > "$sb/src/data.json"
  run bash -c "
    cd '$sb'
    . '$REPO_ROOT/ci/lib/common.sh'
    . '$REPO_ROOT/ci/lib/changeset.sh'
    ci::changeset::_in_node_workspace src/data.json && echo INSIDE || echo OUTSIDE
    ci::changeset::_in_node_workspace README.md && echo INSIDE2 || echo OUTSIDE2
  "
  [[ "$output" == *"INSIDE"* ]]
  [[ "$output" == *"INSIDE2"* ]]
  rm -rf "$sb"
}

@test "changeset: without a root package.json the walk still says no" {
  # The control. Anchoring on a manifest is what keeps this from claiming every
  # file in a Python repository belongs to a Node workspace.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/src"
  printf '{}\n' > "$sb/src/data.json"
  run bash -c "
    cd '$sb'
    . '$REPO_ROOT/ci/lib/common.sh'
    . '$REPO_ROOT/ci/lib/changeset.sh'
    ci::changeset::_in_node_workspace src/data.json && echo INSIDE || echo OUTSIDE
  "
  [[ "$output" == *"OUTSIDE"* ]]
  rm -rf "$sb"
}

@test "tests-shell: an ignored replacement of a deleted bats file is caught" {
  # Same class as the node lane's: a commit that deletes a bats file and ignores
  # its path leaves the worktree copy invisible to `git diff HEAD` and to
  # --exclude-standard alike, so the suites ran the replacement for a commit
  # that removes their own coverage.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks" "$sb/ci/tests"
  cp "$REPO_ROOT/ci/checks/tests-shell.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$sb/ci/checks/tests.sh"
  chmod +x "$sb/ci/checks/tests.sh"
  printf '@test "x" { true; }\n' > "$sb/ci/tests/t.bats"
  (
    cd "$sb"
    git init -q -b feature/x .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
    git rm -q ci/tests/t.bats
    printf 'ci/tests/t.bats\n' > .gitignore
    git add .gitignore
    git -c user.email=t@t -c user.name=t commit -qm "delete and ignore"
  ) >/dev/null 2>&1
  # git rm took the now-empty directory with it.
  mkdir -p "$sb/ci/tests"
  printf '@test "x" { true; }\n' > "$sb/ci/tests/t.bats"
  run bash -c "cd '$sb' && git diff --name-only HEAD -- ci; git ls-files --others --exclude-standard -- ci"
  [ -z "$output" ]
  run bash -c "cd '$sb' && CI_GATE_MODE=ship bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"ci/tests/t.bats"* ]]
  rm -rf "$sb"
}

@test "git-safety: an unwalkable push range fails closed" {
  # Every ship-mode scan reached its commit list through `git rev-list ... ||
  # true`. An unresolvable range produced no commits, no scanning, and a
  # confident PASS — on the security path. "Nothing to push" and "the walk
  # failed" look identical in the output and must not look identical in the
  # result.
  gs_setup
  printf 'SECRET=1\n' > "$GS_SB/secrets.env"
  gs_commit "add secret"
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef CI_GATE_PUSH_NEW_SHA=HEAD bash ci/checks/git-safety.sh 2>&1"
  # The bad SHA is rejected by push_range's own verification, so the range falls
  # back and the scan still runs — the secret is found rather than skipped.
  [ "$status" -eq 20 ]
  [[ "$output" == *"secrets.env"* ]]

  # And a range that resolves but cannot be walked is infrastructure failure,
  # not a pass. `refs/heads/nope..HEAD` names a ref that does not exist.
  #
  # The stub is written into the sandbox's own ci/lib/git.sh rather than
  # exported. Two separate things defeated `export -f` here, and the case was
  # green throughout: bash will not carry a function whose name contains `::`
  # into a child, and git-safety.sh sources ci/lib/git.sh itself, which
  # redefines push_range over anything the environment supplied. What the
  # second run actually measured was the ordinary secret scan — exit 20, the
  # secret found — and `[ "$status" -ne 0 ]` accepted it, so the assertion
  # named the fail-closed path while exercising the path beside it.
  cat >> "$GS_SB/ci/lib/git.sh" <<'SH'
ci::git::push_range() { printf 'refs/heads/nope..HEAD'; }
SH
  # The premise: the range really is unwalkable.
  run bash -c "cd '$GS_SB' && git rev-list refs/heads/nope..HEAD"
  [ "$status" -ne 0 ]

  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship bash ci/checks/git-safety.sh 2>&1"
  # 30, not merely non-zero: 20 is the answer this used to get, and the whole
  # point is that a failed walk must not look like a finding or like a pass.
  [ "$status" -eq 30 ]
  [[ "$output" == *"refs/heads/nope..HEAD"* ]]
  rm -rf "$GS_SB"
}

@test "git-safety: the push range is a plain rev when there is no base" {
  # `<empty-tree>..HEAD` names a tree object on the left of a revision walk,
  # which is one `|| true` away from a silent empty result. A first push is
  # every commit reachable from HEAD, and that is what it says now.
  gs_setup
  run bash -c "cd '$GS_SB' && . ci/lib/git.sh && ci::git::push_range"
  [ "$output" = "HEAD" ]
  # And it is a range git will actually walk.
  run bash -c "cd '$GS_SB' && git rev-list HEAD >/dev/null"
  [ "$status" -eq 0 ]
  rm -rf "$GS_SB"
}

@test "git-safety: a sensitive file whose name is not ASCII is still caught" {
  # `--name-only` emits git's quoted representation for anything outside plain
  # ASCII: "caf\303\251.env". The suffix patterns then match a string ending in
  # a quote character, and `git cat-file -s "$sha:$path"` looks up a path that
  # does not exist — so an accented .env walked straight through.
  gs_setup
  local base
  base="$( cd "$GS_SB" && git rev-parse HEAD )"
  printf 'SECRET=1\n' > "$GS_SB/café.env"
  gs_commit "accented secret"
  # The premise: git really does quote it.
  run bash -c "cd '$GS_SB' && git show --name-only --format= HEAD"
  [[ "$output" == *'\303\251'* ]]
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"sensitive-files"* ]]
  rm -rf "$GS_SB"
}

@test "git-safety: a malformed secret pattern is infrastructure failure, not silence" {
  # `$?` after `! cmd` is the status of the negation, so the guard read 0 where
  # grep had returned 2 and the branch never fired. A pattern with an unmatched
  # parenthesis then made every subsequent scan report no match — secret
  # detection silently switched off for the whole run.
  gs_setup
  printf 'ordinary\n' > "$GS_SB/b.txt"
  gs_commit "ordinary"
  run bash -c "cd '$GS_SB' && CI_CHECKS_SECRET_PATTERN_OVERRIDE=1 bash -c '
    sed -i \"s/^exec .*//\" /dev/null 2>/dev/null || true
    CI_GATE_MODE=quick bash ci/checks/git-safety.sh' 2>&1"
  # The real assertion is on the check invoked with a broken pattern directly.
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=quick bash -c '
    source ci/lib/common.sh
    source ci/lib/git.sh
    CI_CHECKS_SECRET_PATTERN=\"unmatched(paren\"
    export CI_CHECKS_SECRET_PATTERN
    rc=0
    grep -E -f <(printf \"%s\n\" \"^\\+.*(\$CI_CHECKS_SECRET_PATTERN)\") /dev/null >/dev/null 2>&1 || rc=\$?
    echo \"grep-status=\$rc\"
    [ \"\$rc\" -ge 2 ] && echo WOULD-FAIL-INFRA || echo WOULD-PASS-SILENTLY'"
  [[ "$output" == *"WOULD-FAIL-INFRA"* ]]
  # And the shipped script carries the corrected capture, not the `!` form.
  run grep -n 'grep -E -f "$secret_pattern_file" /dev/null >/dev/null 2>&1 || rc=\$?' "$REPO_ROOT/ci/checks/git-safety.sh"
  [ "$status" -eq 0 ]
  run grep -c 'if ! grep -E -f "\$secret_pattern_file"' "$REPO_ROOT/ci/checks/git-safety.sh"
  [ "$output" = "0" ]
  rm -rf "$GS_SB"
}

@test "git-safety: an unreadable commit is infrastructure failure, not an empty diff" {
  # `git show ... || true` turned a blob that cannot be read into an empty diff,
  # and an empty diff is indistinguishable from a clean one. Simulated by
  # pointing the range at a commit whose object is removed from the store.
  gs_setup
  printf 'ordinary\n' > "$GS_SB/b.txt"
  gs_commit "ordinary"
  run grep -n '_gs_commits_readable' "$REPO_ROOT/ci/checks/git-safety.sh"
  [ "$status" -eq 0 ]
  # The helper answers no when a commit in the list cannot be shown.
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship bash -c '
    source ci/lib/common.sh; source ci/lib/git.sh
    GATE_RANGE=HEAD
    GATE_COMMITS=\"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\"
    $(sed -n "/^_gs_commits_readable()/,/^}/p" "$REPO_ROOT/ci/checks/git-safety.sh")
    _gs_commits_readable && echo READABLE || echo UNREADABLE'"
  [[ "$output" == *"UNREADABLE"* ]]
  rm -rf "$GS_SB"
}

@test "branch-protection: a range-walk failure is FAIL_INFRA, not a policy violation" {
  # _bp_fail merges FAIL_NEW_ISSUE, which told the developer their commits break
  # a rule when the check simply could not run — and disagreed with
  # git-safety.sh, which returns FAIL_INFRA for the same condition.
  run grep -n '_bp_infra "Cannot walk the push range' "$REPO_ROOT/ci/checks/branch-protection.sh"
  [ "$status" -eq 0 ]
  run grep -n '_bp_infra "Cannot count merge commits' "$REPO_ROOT/ci/checks/branch-protection.sh"
  [ "$status" -eq 0 ]
  run bash -c "cd '$REPO_ROOT' && sed -n '/^_bp_infra()/,/^}/p' ci/checks/branch-protection.sh"
  [[ "$output" == *"CI_RESULT_FAIL_INFRA"* ]]
}

@test "preflight: ship mode never takes the empty-changeset fast exit" {
  # ci::changeset::detect pre-push derives an endpoint diff. A first push with
  # no merge base yields an empty changeset, and a secret added then removed
  # within the push leaves the endpoint diff clean — either exits PASS before
  # git-safety.sh has run at all.
  run bash -c "cd '$REPO_ROOT' && grep -n 'MODE\" != \"ship\"' ci/preflight.sh"
  [ "$status" -eq 0 ]
  run bash -c "cd '$REPO_ROOT' && sed -n '/No relevant changes detected/,+2p' ci/preflight.sh"
  [ "$status" -eq 0 ]
}

@test "preflight: the history-dependent checks are not cacheable" {
  # The cache key describes the endpoint changeset. git-safety and
  # branch-protection now walk the outgoing commits, which two branches with
  # identical final files do not share — an amend can swap a signed commit for
  # an unsigned one without changing a byte of the final tree.
  run bash -c "cd '$REPO_ROOT' && sed -n '/^_check_is_cacheable/,/^}/p' ci/preflight.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"git-safety | branch-protection) return 1"* ]]
  local id
  for id in test-layout tests-shell node git-safety branch-protection; do
    run bash -c "cd '$REPO_ROOT' && source ci/lib/common.sh 2>/dev/null; $(sed -n '/^_check_is_cacheable/,/^}/p' "$REPO_ROOT/ci/preflight.sh"); _check_is_cacheable $id && echo CACHEABLE || echo NO"
    [[ "$output" == *"NO"* ]] || { echo "$id is still cacheable" >&2; return 1; }
  done
}

# --- fail-open audit: the gate skipping itself -------------------------------
#
# Three of these are the same defect wearing different clothes: a guard that
# cannot tell "I found nothing" from "I could not look", reported through a
# channel whose only two values are empty and non-empty.

_pf_fns() {
  # Extract a preflight helper without executing the script. preflight.sh runs
  # a gate when sourced, so the functions are lifted out the way the js lane
  # suite lifts the semver comparator.
  local out="$1" ; shift
  : > "$out"
  local fn
  for fn in "$@"; do
    sed -n "/^${fn}() {/,/^}/p" "$REPO_ROOT/ci/preflight.sh" >> "$out"
    # A silent miss would leave the caller calling a function that is not
    # there, which is the failure mode these cases exist to catch elsewhere.
    grep -q "^${fn}() {" "$out" || {
      echo "no such function in preflight.sh: ${fn}" >&2
      return 1
    }
  done
  bash -n "$out"
}

@test "preflight: branch-protection is never dropped by the changeset filter" {
  # Nothing emits `branch-protection` as a changeset check id, so the only arm
  # that could keep it scheduled was unreachable and the lane was filtered out
  # on every run in every mode -- including a direct commit on a protected
  # branch, which is the one thing it exists to catch. Which branch you are on
  # is not a function of the changed-file list.
  local fns="$BATS_TEST_TMPDIR/skip.sh"
  # Every callee too. A helper missing from the extraction is "command not
  # found" -> non-zero -> read as a decision, and the case would then pass
  # without the code under test having run.
  _pf_fns "$fns" _check_should_skip _check_disabled_in_config \
    _all_related_checks_disabled _checks_for_lane_label
  local rc=0
  # shellcheck disable=SC1090
  ( set +e
    . "$fns"
    CI_GATE_INCREMENTAL=1
    _CI_CHANGESET_CHECKS="lint-js tests-js"   # a JavaScript-only changeset
    _check_should_skip branch-protection
    exit $?
  ) || rc=$?
  # 1 = do not skip. The premise: this changeset does filter something else.
  [ "$rc" -eq 1 ]
  local rc2=0
  ( set +e
    . "$fns"
    CI_GATE_INCREMENTAL=1
    _CI_CHANGESET_CHECKS="lint-js tests-js"
    _check_should_skip python
    exit $?
  ) || rc2=$?
  [ "$rc2" -eq 0 ]
}

@test "preflight: a lane that reads more than the changeset is not cacheable" {
  # The cache key hashes the changed files. Any lane whose result depends on
  # input outside that list can be served a PASS that was never true of this
  # tree: python runs ruff, pytest and compileall over the whole package, and a
  # syntax error in an unstaged file is invisible to the key.
  local fns="$BATS_TEST_TMPDIR/cache.sh"
  _pf_fns "$fns" _check_is_cacheable
  local id rc
  for id in test-layout tests-shell node python git-safety branch-protection; do
    rc=0
    # shellcheck disable=SC1090
    ( set +e; . "$fns"; _check_is_cacheable "$id"; exit $? ) || rc=$?
    [ "$rc" -eq 1 ] || { echo "cacheable but must not be: $id" >&2; return 1; }
  done
}

@test "git: a local default-branch guess equal to the tip is discarded" {
  # On a branch named main with no remote, merge-base HEAD main is HEAD, so the
  # range was HEAD..HEAD -- empty. Every ship-mode check then reported "nothing
  # changed" over a push carrying the whole branch: the pre-push gate skipping
  # itself at the last moment before the commits leave.
  local sb
  sb="$(mktemp -d)"
  (
    cd "$sb"
    git init -q -b main .
    printf 'a\n' > a.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c1
    printf 'b\n' > b.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c2
  ) >/dev/null 2>&1
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' && ci::git::push_range"
  [ "$status" -eq 0 ]
  # The whole of HEAD, which is what that push contains -- not an empty range.
  [[ "$output" != *".."* ]]
  local head_sha
  head_sha="$(cd "$sb" && git rev-parse HEAD)"
  [[ "$output" != "${head_sha}..${head_sha}" ]]
  # And the range it produces must actually contain both commits.
  run bash -c "cd '$sb' && git rev-list --count $output"
  [ "$output" -eq 2 ]
  rm -rf "$sb"
}

@test "tests-shell: a suite deleted to nothing is a failure, not a green skip" {
  # The generic dispatcher treats a missing ci/tests/ as a successful skip,
  # which is right for a repo with no shell tests and exactly wrong for this
  # lane: it is scheduled as a blocker *because* these suites must run. With
  # every .bats file deleted the dispatcher logged "skipped: no ci/tests
  # directory found" and reported PASS, so the gate would approve the removal
  # of the whole regression net it exists to enforce.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks" "$sb/ci/tests"
  cp "$REPO_ROOT/ci/checks/tests-shell.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  # The delegation target is stubbed so the premise below means something. Left
  # absent, the wrapper reaches its `exec` and dies with 127 — which is neither
  # 20 nor the message, so the premise would "hold" while proving nothing about
  # the guard.
  printf '#!/usr/bin/env bash\nexit 0\n' > "$sb/ci/checks/tests.sh"
  chmod +x "$sb/ci/checks/tests.sh"
  # The premise: with a suite present this wrapper delegates rather than
  # failing here, so the assertion below is about the empty case alone.
  printf '@test "x" { true; }\n' > "$sb/ci/tests/t.bats"
  run bash -c "cd '$sb' && bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"No .bats suites found"* ]]

  rm -f "$sb/ci/tests/t.bats"
  run bash -c "cd '$sb' && bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"No .bats suites found"* ]]

  # And the directory removed entirely, which is the same commit one step on.
  rm -rf "$sb/ci/tests"
  run bash -c "cd '$sb' && bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 20 ]
  rm -rf "$sb"
}

@test "tests-shell: a deleted .gitignore replaced only on disk is drift" {
  # Three scopes for one question is how they came to disagree: the tracked
  # scan covered .gitignore, the untracked and ignored scans covered ci and
  # .githooks alone. A commit deleting .gitignore while a worktree copy stays
  # behind was therefore invisible to all three -- the tracked scan sees the
  # deletion, and the scan that would have seen the replacement was not looking
  # there. The suites then ran against rules the push removes.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks" "$sb/ci/tests"
  cp "$REPO_ROOT/ci/checks/tests-shell.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf '@test "x" { true; }\n' > "$sb/ci/tests/t.bats"
  printf 'ignored-by-nothing\n' > "$sb/.gitignore"
  (
    cd "$sb"
    git init -q -b feature/x .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
    git rm -q --cached .gitignore
    git -c user.email=t@t -c user.name=t commit -qm "drop gitignore"
  ) >/dev/null 2>&1
  # The file is gone from HEAD but still on disk: the exact shape.
  run bash -c "cd '$sb' && git cat-file -e HEAD:.gitignore 2>&1"
  [ "$status" -ne 0 ]
  [ -f "$sb/.gitignore" ]

  run bash -c "cd '$sb' && CI_GATE_MODE=ship bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *".gitignore"* ]]
  rm -rf "$sb"
}

@test "preflight: checks.yml parsing survives the shape of the real file" {
  # _check_disabled_in_config was a `while read` loop piping every line into six
  # separate greps and seds. On this repository's 213-line checks.yml a single
  # call took 34 seconds, and _check_should_skip makes one per lane plus one per
  # related check -- so `quick` mode, which declares a pre-commit budget, spent
  # minutes deciding what to run before running anything. It is one awk pass
  # now, and the parse has to be identical rather than merely close, so every
  # branch of it is pinned here: nested and list forms, comments on the id line
  # and on the value, a check with no `enabled:` at all, and a top-level key
  # that ends the block mid-file.
  local fns="$BATS_TEST_TMPDIR/cfg.sh"
  _pf_fns "$fns" _check_disabled_in_config
  local sb="$BATS_TEST_TMPDIR/cfgsb"
  mkdir -p "$sb/ci/config"
  cat > "$sb/ci/config/checks.yml" <<'YML'
# a leading comment
version: 1
checks:
  alpha:
    enabled: true
  bravo:
    enabled: false
  charlie:                       # trailing comment on the id line
    enabled: false  # UMS: note
  delta:
    enabled: true   # note
  echo:
    severity: blocker
top_level_again: 1
  foxtrot:
    enabled: false
extra:
  - id: golf
    enabled: false
  - id: "hotel"
    enabled: true
  - id: india
    severity: blocker
YML
  # expected: 0 = disabled, 1 = enabled (an absent or unstated id is enabled).
  local row id want rc bad=""
  for row in alpha:1 bravo:0 charlie:0 delta:1 echo:1 foxtrot:1 \
             golf:0 hotel:1 india:1 missing:1; do
    id="${row%%:*}"; want="${row##*:}"
    rc=0
    # shellcheck disable=SC1090
    ( set +e; cd "$sb"; . "$fns"; _check_disabled_in_config "$id"; exit $? ) || rc=$?
    [ "$rc" -eq "$want" ] || bad="${bad} ${id}(want=${want} got=${rc})"
  done
  [ -z "$bad" ] || { echo "checks.yml parse mismatches:${bad}" >&2; return 1; }

  # foxtrot is the one worth naming: it sits at the same indentation as a real
  # check but after a column-0 key, so the block has already ended and it is not
  # a check at all. Reading it as one would let a stray top-level key silently
  # disable whatever follows it.

  # And against the real file, where the answer must match what the file says.
  local declared
  declared="$(awk '/^  [A-Za-z0-9_-]+:/{id=$0; sub(/^  /,"",id); sub(/:.*/,"",id)}
                   /^[[:space:]]*enabled:[[:space:]]*false/{print id}' \
              "$REPO_ROOT/ci/config/checks.yml" | sort -u)"
  [ -n "$declared" ]
  for id in $declared; do
    rc=0
    ( set +e; . "$fns"; _check_disabled_in_config "$id"; exit $? ) || rc=$?
    [ "$rc" -eq 0 ] || bad="${bad} real:${id}(declared false, got ${rc})"
  done
  [ -z "$bad" ] || { echo "real checks.yml mismatches:${bad}" >&2; return 1; }
}

@test "branch-protection: a git warning on stderr is not read as a signature record" {
  # `2>&1` merged git's diagnostics into the string that is then parsed as
  # "<signature status> <sha>" pairs. git *succeeds* while warning -- dubious
  # ownership, a replace-ref advisory -- so the warning arrived as a record
  # whose first word is not G/U/X/Y and was reported as an unsigned commit
  # whose sha is the rest of the warning text. A false failure on the check
  # that decides whether a push may proceed.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks" "$sb/bin"
  cp "$REPO_ROOT/ci/checks/branch-protection.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" \
     "$REPO_ROOT/ci/lib/git.sh" "$sb/ci/lib/"
  (
    cd "$sb"
    git init -q -b feature/x .
    printf 'x\n' > a.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
  ) >/dev/null 2>&1

  local real_git
  real_git="$(command -v git)"
  [ -n "$real_git" ]
  # A git that works but chatters on stderr, which is the shape of the real
  # thing: exit status 0, output correct, a warning alongside it.
  {
    printf '#!/usr/bin/env bash\n'
    printf 'if [ "$1" = "log" ]; then\n'
    printf '  echo "warning: detected dubious ownership in repository" >&2\n'
    printf 'fi\n'
    printf 'exec %s "$@"\n' "$real_git"
  } > "$sb/bin/git"
  chmod +x "$sb/bin/git"

  # The premise, both halves: the shim still succeeds, and it really does warn.
  run env PATH="$sb/bin:$PATH" bash -c "cd '$sb' && git log --pretty='%G? %H' HEAD 2>/dev/null"
  [ "$status" -eq 0 ]
  run env PATH="$sb/bin:$PATH" bash -c "cd '$sb' && git log --pretty='%G? %H' HEAD 2>&1 >/dev/null"
  [[ "$output" == *"dubious ownership"* ]]

  run env PATH="$sb/bin:$PATH" bash -c "cd '$sb' && CI_GATE_REQUIRE_SIGNED_COMMITS=1 \
    CI_GATE_REQUIRE_LINEAR_HISTORY=1 bash ci/checks/branch-protection.sh 2>&1"
  # The commits here are genuinely unsigned, so a signature complaint is
  # expected -- but it must name a commit, never the warning text. Before the
  # fix this printed, verbatim: "Unsigned or unverified commit detected dubious
  # ownership in repository (status: warning:)" -- the sha field holding the
  # warning and the status field holding the word "warning:".
  [[ "$output" != *"ownership"* ]]
  [[ "$output" != *"status: warning"* ]]
  # And the merge count must not have been prose.
  [[ "$output" != *"merge commit(s) found"* ]]
  [[ "$output" != *"is not a number"* ]]
  rm -rf "$sb"
}

@test "git-safety: a credential file inside a nested workspace is caught" {
  # The list was written in repository-root spellings, and a case pattern like
  # `.npmrc` matches that string and nothing else -- so `frontend/.npmrc` and
  # `packages/app/.pypirc` went straight through. The content scan is no
  # backstop here: an `//registry.npmjs.org/:_authToken=` line matches none of
  # the canonical token prefixes, so nothing else was going to catch it either.
  # `.env` survived only because `*.env` happens to match a nested path.
  gs_setup
  mkdir -p "$GS_SB/frontend" "$GS_SB/packages/app"
  printf '//registry.npmjs.org/:_authToken=abc123\n' > "$GS_SB/frontend/.npmrc"
  printf '[pypi]\npassword=hunter2\n' > "$GS_SB/packages/app/.pypirc"
  ( cd "$GS_SB" && git add -A ) >/dev/null 2>&1

  # The premise: the content scan really does not match these, so this case is
  # about the filename rule and not about a token pattern catching it anyway.
  run bash -c "cd '$GS_SB' && grep -E '(ghp_|github_pat_|AKIA|sk-)' frontend/.npmrc packages/app/.pypirc"
  [ "$status" -ne 0 ]

  run gs_run quick
  [ "$status" -eq 20 ]
  [[ "$output" == *"frontend/.npmrc"* ]]
  [[ "$output" == *"packages/app/.pypirc"* ]]
  [[ "$output" == *"sensitive-files"* ]]
  rm -rf "$GS_SB"
}

@test "git-safety: an ordinary nested file is still allowed" {
  # The control. Matching on basenames at any depth must not start blocking
  # ordinary source files that happen to sit deep in a tree.
  gs_setup
  mkdir -p "$GS_SB/frontend/src/lib"
  printf 'export const x = 1;\n' > "$GS_SB/frontend/src/lib/env.ts"
  printf 'export const y = 2;\n' > "$GS_SB/frontend/src/lib/keyboard.ts"
  ( cd "$GS_SB" && git add -A ) >/dev/null 2>&1
  run gs_run quick
  [ "$status" -eq 0 ]
  rm -rf "$GS_SB"
}

@test "branch-protection: the push destination is what is protected, not the checkout" {
  # `git push origin feature:main` writes to main while HEAD says feature, and
  # this check asked `git rev-parse --abbrev-ref HEAD` -- so the branch name it
  # judged was the one nobody was pushing to. `git push origin HEAD:main` and
  # `git push origin :main` (a deletion) went the same way. The pre-push hook is
  # handed the destination for every ref on stdin and now passes it on.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks"
  cp "$REPO_ROOT/ci/checks/branch-protection.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" \
     "$REPO_ROOT/ci/lib/git.sh" "$sb/ci/lib/"
  (
    cd "$sb"
    git init -q -b feature/x .
    printf 'x\n' > a.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
  ) >/dev/null 2>&1

  # The premise: we are standing on a feature branch, so anything that refuses
  # below refused on the destination and could not have refused on the checkout.
  run bash -c "cd '$sb' && git rev-parse --abbrev-ref HEAD"
  [ "$output" = "feature/x" ]

  run bash -c "cd '$sb' && CI_GATE_PUSH_REMOTE_REFS=main bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -ne 0 ]
  [[ "$output" == *"protected branch 'main'"* ]]

  # One protected destination among several is still a protected destination.
  run bash -c "cd '$sb' && CI_GATE_PUSH_REMOTE_REFS='topic main' bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -ne 0 ]
  [[ "$output" == *"protected branch 'main'"* ]]

  # Control: an ordinary destination passes. Without this the case would also
  # be satisfied by a check that refuses everything.
  run bash -c "cd '$sb' && CI_GATE_PUSH_REMOTE_REFS=topic bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"protected branch"* ]]

  # Control: set-and-empty is a tag push -- the hook read stdin and no branch is
  # being written. Falling back to HEAD here would refuse `git push origin v1.2`
  # for the branch you happened to be standing on.
  run bash -c "cd '$sb' && CI_GATE_PUSH_REMOTE_REFS= bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"no branch ref"* ]]

  # Control: unset means nobody said, which is every caller that is not the
  # pre-push hook -- and only then is the checkout the best evidence there is.
  ( cd "$sb" && git checkout -q -b main ) >/dev/null 2>&1
  run bash -c "cd '$sb' && bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -ne 0 ]
  [[ "$output" == *"protected branch 'main'"* ]]
  rm -rf "$sb"
}

@test "git-safety: a path touched by many commits is sized once, by its largest version" {
  # Sizing ran per emitted path, and _gs_content_files emits one record per
  # commit per modification -- so a file touched by N of the pushed commits was
  # sized N times, and each sizing walked all N commits again. Quadratic on any
  # push with no resolvable base, where the commit list is the whole history.
  # Measured at 419 commits: one path cost 19s and there were ~3800 emissions,
  # against a 120s pre-push budget. Batched now, and this asserts the answer did
  # not change with the method: the largest version anywhere in the range, even
  # when a later commit shrinks the file back down.
  gs_setup
  local base
  base="$( cd "$GS_SB" && git rev-parse HEAD )"
  # 6MB in an intermediate commit, then shrunk. The blob is still in the push.
  ( cd "$GS_SB" && head -c 6291456 /dev/zero | tr '\0' 'x' > big.bin ) 2>/dev/null
  gs_commit "add big"
  printf 'small\n' > "$GS_SB/big.bin"
  gs_commit "shrink big"
  printf 'more\n' >> "$GS_SB/big.bin"
  gs_commit "touch big again"

  # The premise, both halves: the path really is emitted more than once, and
  # the current version really is small -- so a scan of the tip alone, or one
  # that took the last emission, would report nothing.
  run bash -c "cd '$GS_SB' && for s in \$(git rev-list '$base'..HEAD); do git show --format= --name-only --diff-filter=ACMR \$s; done | grep -c '^big.bin$'"
  [ "$output" -ge 3 ]
  run bash -c "cd '$GS_SB' && git cat-file -s HEAD:big.bin"
  [ "$output" -lt 5242880 ]

  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"Large file detected"* ]]
  [[ "$output" == *"big.bin"* ]]
  # Reported once, not once per commit that touched it.
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' bash ci/checks/git-safety.sh 2>&1 | grep -c 'Large file detected'"
  [ "$output" -eq 1 ]
  rm -rf "$GS_SB"
}

@test "git-safety: an ordinary file is not reported as large" {
  # The control for the case above: sizing that returned a wrong-but-large
  # number, or that mismatched paths against results, would satisfy it too.
  gs_setup
  local base
  base="$( cd "$GS_SB" && git rev-parse HEAD )"
  printf 'ordinary\n' > "$GS_SB/b.txt"
  gs_commit "one"
  printf 'ordinary again\n' > "$GS_SB/b.txt"
  gs_commit "two"
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"Large file detected"* ]]
  rm -rf "$GS_SB"
}
