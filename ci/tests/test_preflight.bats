#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  # The gate exports its own mode and push state, and these suites run
  # under it -- so a case inherited a range belonging to another tree.
  # shellcheck source=ci/tests/gate_env.bash
  source "$REPO_ROOT/ci/tests/gate_env.bash"
  ci::tests::clear_gate_env
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

# A secret-shaped line, assembled at run time.
#
# ci/checks/common.sh defines the patterns as literal text, so a fixture that
# spells one out is a line in *this* repository matching CI_CHECKS_SECRET_PATTERN
# -- and git-safety.sh refuses a push whose additions match it. Spelling it out
# therefore made the branch carrying these tests unpushable through the gate the
# tests are for. Measured, not inferred:
#
#   CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA=f6bd39ea~1 \
#   CI_GATE_PUSH_NEW_SHA=f6bd39ea CI_GATE_PUSH_REMOTE=origin \
#   bash ci/checks/git-safety.sh
#     -> exit 20, "Potential secret-like value detected in additions in the
#        pushed commits." / "Blocking content checks: secret-pattern-match"
#
# f6bd39ea is a commit on this branch, and the only reason the push went through
# is that no hook is installed in this clone. The comment in gs_setup already
# notes that the patterns match their own definitions; the fixtures had the same
# problem one level up and nothing was reading them for it.
#
# Split so no line here matches, while the bytes written to the file are
# identical to what the scanner is being asked to catch. The value is not a
# credential and never was -- twenty letters of the alphabet -- but "it is
# obviously fake" is not something a regex can see, and a scanner that took my
# word for it would be the wrong scanner.
gs_secret_line() {
  printf 'AWS_SECRET_ACCESS%s%s\n' '_KEY=' 'abcdefghijklmnopqrst'
}

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
    # themselves — the `DATABASE_URL` alternative is its own witness — so a
    # range reaching the first commit would flag the fixture, not the case.
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

@test "git-safety: a path that changes file type is still scanned" {
  # The counterpart to the case above, and the letter that was missing from it.
  # `D` is excluded because a removal introduces no content; `T` was excluded
  # with it, and a type change is not a removal -- the path is still there
  # afterwards, holding whatever the commit put in it.
  #
  # A tracked symlink replaced by a regular file is the shape: git reports the
  # mode swap as `T` and nothing else, so the sensitive-file, build-artifact and
  # large-blob scans all read a list this path was not on.
  #
  # Built with plumbing rather than `ln -s`, because the case has to mean the
  # same thing on a checkout where the filesystem has no symlinks.
  gs_setup
  local blob base
  blob="$( cd "$GS_SB" && printf 'a.txt' | git hash-object -w --stdin )"
  ( cd "$GS_SB" && git update-index --add --cacheinfo "120000,${blob},config.env" \
      && git -c user.email=t@t -c user.name=t commit -qm "config.env as a symlink" ) >/dev/null 2>&1
  base="$( cd "$GS_SB" && git rev-parse HEAD )"

  # Through gs_secret_line, not spelled out: ci/checks/common.sh defines the
  # secret patterns as literal text and git-safety refuses a push whose
  # additions match them, so a fixture that writes one out is a line this
  # repository cannot push through its own gate. `tests: no fixture spells out a
  # string the gate refuses to push` is the case that says so, and it caught
  # this one.
  blob="$( cd "$GS_SB" && gs_secret_line | git hash-object -w --stdin )"
  ( cd "$GS_SB" && git update-index --cacheinfo "100644,${blob},config.env" \
      && git -c user.email=t@t -c user.name=t commit -qm "and now a regular file" ) >/dev/null 2>&1

  # The premise, asserted rather than assumed: git really does call this `T`,
  # and the filter this gate used really did drop it. Without both, the run
  # below could pass for a reason that has nothing to do with the fix.
  run bash -c "cd '$GS_SB' && git diff --name-status '$base' HEAD"
  [[ "$output" == T*"config.env"* ]]
  run bash -c "cd '$GS_SB' && git diff --name-only --diff-filter=ACMR '$base' HEAD"
  [ -z "$output" ]
  run bash -c "cd '$GS_SB' && git diff --name-only --diff-filter=ACMRT '$base' HEAD"
  [ "$output" = "config.env" ]

  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"config.env"* ]]

  # And the reason `D` stays out is unchanged by this: deleting the file is
  # still the fix, not a second offence.
  gs_setup
  printf 'SECRET=1\n' > "$GS_SB/secrets.env"
  gs_commit "add secret"
  base="$( cd "$GS_SB" && git rev-parse HEAD )"
  ( cd "$GS_SB" && git rm -q secrets.env ) >/dev/null 2>&1
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
  # security reaches a live advisory database, build shellchecks the whole ci/
  # tree, and debt runs the underlying tools repo-wide against a ratchet file.
  # None of the three is described by a key made of the changed paths.
  for id in test-layout tests-shell node python git-safety branch-protection \
            security build debt typecheck-js; do
    rc=0
    # shellcheck disable=SC1090
    ( set +e; . "$fns"; _check_is_cacheable "$id"; exit $? ) || rc=$?
    [ "$rc" -eq 1 ] || { echo "cacheable but must not be: $id" >&2; return 1; }
  done

  # The control, so this is a classification and not a blanket refusal: the one
  # lane whose answer really is a function of the changed-file list still hits.
  rc=0
  # shellcheck disable=SC1090
  ( set +e; . "$fns"; _check_is_cacheable changed-files; exit $? ) || rc=$?
  [ "$rc" -eq 0 ] || { echo "changed-files should be cacheable" >&2; return 1; }
}

@test "preflight: every lane it schedules is classified for the cache" {
  # The list of lanes that may not be cached was an exclusion, and an exclusion
  # has no tail: everything it does not name was cacheable for not being named.
  # security, build and debt were three that never were, and the same shape in
  # this same file is what made `--mode debt` run the whole full plan for not
  # being `quick`.
  #
  # The default is closed now, so a lane nobody classified is simply run. This
  # case is the other half: it enumerates the lanes from the plan rather than
  # restating them, and fails on one this function has never heard of -- so the
  # classification is made deliberately instead of collected by falling off the
  # end of a case.
  local lanes
  lanes="$(grep -oE '"[a-z-]+:\./ci/checks/[a-z-]+\.sh"' "$REPO_ROOT/ci/preflight.sh" \
           | sed -E 's/^"([a-z-]+):.*/\1/' | sort -u)"
  # The floor: a selector that matches nothing makes every assertion below
  # vacuously true, which is how a meta-case comes to pass while testing air.
  [ "$(printf '%s\n' "$lanes" | grep -c .)" -ge 8 ] \
    || { echo "found only these lanes: $lanes" >&2; return 1; }

  # And the lane file has to be covered too, since --mode full/ship reads its
  # rows instead of the built-in plan when CI_GATE_USE_LANES=1.
  local conf_lanes
  conf_lanes="$(sed -E '/^#/d; /^$/d; s/\|.*//' "$REPO_ROOT/ci/config/lanes.conf" | sort -u)"
  lanes="$(printf '%s\n%s\n' "$lanes" "$conf_lanes" | sort -u | grep -v '^$')"

  local src="$REPO_ROOT/ci/preflight.sh" lane unclassified=""
  while IFS= read -r lane; do
    [ -n "$lane" ] || continue
    # Named in an arm of _check_is_cacheable, either alone or beside another
    # label in the same pattern.
    grep -qE "^[[:space:]]*([a-z-]+[[:space:]]*\|[[:space:]]*)*${lane}([[:space:]]*\|[[:space:]]*[a-z-]+)*\)[[:space:]]*return [01]" "$src" \
      || unclassified="${unclassified} ${lane}"
  done <<< "$lanes"
  [ -z "$unclassified" ] \
    || { echo "lanes the cache has no ruling on:${unclassified}" >&2; return 1; }
}

@test "git: a push across two lineages is refused, not answered with HEAD" {
  # The tag-range fix collapsed tips by ancestry and, for tips containing
  # neither each other, set _pr_split and *fell through* -- to the code below it
  # that defaults an unset new sha to HEAD. So a push publishing two unrelated
  # lineages got a range describing the checked-out branch: non-empty, so
  # git-safety's "cannot determine the range" guard never fired, and the
  # signature walk, the merge count and every content scan reported on commits
  # nobody was pushing while the orphan lineage went out unlooked-at.
  #
  #   old  ->  rc=0  <base>..HEAD   (main's commits; the orphan never scanned)
  #   new  ->  rc=3  refused, naming what to do instead
  #
  # There is no honest single range here: every caller hands this string to
  # `git log` or `git rev-list` as one argument, and git has no one-token
  # spelling for the union of two disjoint ranges. Refusing says so; the old
  # answer said something confident and false.
  local sb l1 l2 base
  sb="$(mktemp -d)"
  git init -q --bare "$sb/dest.git"
  git init -q -b main "$sb/w"
  (
    cd "$sb/w"
    git config user.email t@t && git config user.name t
    git remote add origin "file://$sb/dest.git"
    printf 'base\n' > a.txt && git add -A && git commit -qm base
    git push -q origin main
    printf 'one\n' >> a.txt && git add -A && git commit -qm one && git tag v1
    git checkout -q --orphan other
    git rm -rq --cached . 2>/dev/null
    printf 'two\n' > b.txt && git add -A && git commit -qm two && git tag v2
    git checkout -q main
  ) >/dev/null 2>&1
  l1="$(cd "$sb/w" && git rev-parse v1^{commit})"
  l2="$(cd "$sb/w" && git rev-parse v2^{commit})"

  # The premise: these two really do contain neither each other, or the case
  # below is asserting nothing about split lineages at all.
  run bash -c "cd '$sb/w' && git merge-base --is-ancestor '$l1' '$l2'"
  [ "$status" -ne 0 ]
  run bash -c "cd '$sb/w' && git merge-base --is-ancestor '$l2' '$l1'"
  [ "$status" -ne 0 ]

  _pr_ask() { # <tag tips>
    bash -c "cd '$sb/w' && CI_GATE_PUSH_REMOTE=origin CI_GATE_PUSH_TAG_TIPS='$1' \
             CI_GATE_PUSH_NEW_SHA= bash -c \". '$REPO_ROOT/ci/lib/common.sh' >/dev/null 2>&1
             . '$REPO_ROOT/ci/lib/git.sh'
             ci::git::push_range\" 2>&1"
  }

  run _pr_ask "$l1 $l2"
  [ "$status" -eq 3 ]
  [[ "$output" == *"more than one"* ]]
  [[ "$output" == *"separately"* ]]
  # Nothing that could be read as a range, and in particular not one ending in
  # HEAD -- that was the old answer.
  [[ "$output" != *".."* ]]

  # One new lineage still resolves.
  run _pr_ask "$l1"
  [ "$status" -eq 0 ]
  [ -n "$output" ]
  run bash -c "cd '$sb/w' && git rev-list --count $output"
  [ "$output" -eq 1 ]

  # And the refusal is narrowed to pushes that need it. `git push origin v1 v2`
  # with both tags on commits the destination already carries is the ordinary
  # release shape: it publishes nothing, so it has no split to refuse. Without
  # the published-tip filter this is a false refusal of a routine push, which is
  # the trap the first attempt at the tag range fell into.
  ( cd "$sb/w" && git push -q origin v1 v2 ) >/dev/null 2>&1
  run _pr_ask "$l1 $l2"
  [ "$status" -eq 0 ]
  [[ "$output" != *"more than one"* ]]
  base="$(cd "$sb/w" && git rev-list --count "$output" 2>/dev/null || echo BAD)"
  [ "$base" -eq 0 ]
  rm -rf "$sb"
}

@test "branch-protection: an unmeasurable push is not a skipped signature check" {
  # "There is no push to measure" and "this push cannot be measured" were one
  # condition, and this check draws opposite conclusions from them. push_range
  # returning non-zero meant "skip", so the moment it started refusing a
  # split-lineage push the signature and linear-history checks would have gone
  # quietly silent on exactly the push that could not be scanned -- trading a
  # wrong answer for no answer, on the check the ruleset exists to enforce.
  local sb l1 l2
  sb="$(mktemp -d)"
  git init -q --bare "$sb/dest.git"
  git init -q -b main "$sb/w"
  mkdir -p "$sb/w/ci/lib" "$sb/w/ci/checks"
  cp "$REPO_ROOT/ci/checks/branch-protection.sh" "$REPO_ROOT/ci/checks/common.sh" "$sb/w/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$REPO_ROOT/ci/lib/git.sh" "$sb/w/ci/lib/"
  (
    cd "$sb/w"
    git config user.email t@t && git config user.name t
    git remote add origin "file://$sb/dest.git"
    printf 'ci/\n' > .gitignore
    printf 'base\n' > a.txt && git add -A && git commit -qm base
    git push -q origin main
    printf 'one\n' >> a.txt && git add -A && git commit -qm one && git tag v1
    git checkout -q --orphan other
    git rm -rq --cached . 2>/dev/null
    printf 'two\n' > b.txt && git add -A && git commit -qm two && git tag v2
    git checkout -q main
  ) >/dev/null 2>&1
  l1="$(cd "$sb/w" && git rev-parse v1^{commit})"
  l2="$(cd "$sb/w" && git rev-parse v2^{commit})"

  run bash -c "cd '$sb/w' && CI_GATE_MODE=ship CI_GATE_REQUIRE_SIGNED_COMMITS=1 \
    CI_GATE_REQUIRE_LINEAR_HISTORY=1 CI_GATE_PUSH_REMOTE=origin \
    CI_GATE_PUSH_TAG_TIPS='$l1 $l2' CI_GATE_PUSH_NEW_SHA= CI_GATE_PUSH_REMOTE_REFS=refs/tags/v1 \
    bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -eq 30 ]
  [[ "$output" == *"more than one lineage"* ]]
  # And it did not quietly say it was skipping instead.
  [[ "$output" != *"skipping signed commit check"* ]]
  rm -rf "$sb"
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

@test "branch-protection: linear history is judged on the push range, not the checkout" {
  # The same distinction the case above draws, in the check below it: where the
  # push is going is not where the working tree is standing. This one returned
  # early whenever `git rev-parse --abbrev-ref HEAD` said `HEAD`, and the merge
  # count needs no branch name at all -- _bp_commit_range is ci::git::push_range,
  # which the pre-push hook fills from the SHAs git handed it. So
  # `git push origin <sha>:refs/heads/main` from a detached checkout -- a release
  # script, or a manual push of a tested commit -- skipped the count entirely and
  # reported PASS over a range containing a merge.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks"
  cp "$REPO_ROOT/ci/checks/branch-protection.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" \
     "$REPO_ROOT/ci/lib/git.sh" "$sb/ci/lib/"
  (
    cd "$sb"
    git init -q -b main .
    printf 'a\n' > a.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm base
    git checkout -q -b side
    printf 'b\n' > b.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm side
    git checkout -q main
    printf 'c\n' > c.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm main2
    git -c user.email=t@t -c user.name=t merge -q --no-ff side -m "merge side"
  ) >/dev/null 2>&1

  local base tip
  base="$(cd "$sb" && git rev-parse HEAD~2)"
  tip="$(cd "$sb" && git rev-parse HEAD)"

  # The premise: the range really does carry one merge commit.
  run bash -c "cd '$sb' && git rev-list --count --merges '$base..$tip'"
  [ "$output" = "1" ]

  local bp_env="CI_GATE_REQUIRE_LINEAR_HISTORY=1 CI_GATE_MODE=ship"
  bp_env="$bp_env CI_GATE_PUSH_OLD_SHA=$base CI_GATE_PUSH_NEW_SHA=$tip CI_GATE_PUSH_REMOTE=origin"

  # On a branch, which is the spelling that always worked.
  run bash -c "cd '$sb' && $bp_env bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"Linear history required"* ]]

  # Detached, with the identical range. The checkout has no branch name and the
  # range is unchanged, so anything that answers differently here is answering
  # a question nobody asked.
  ( cd "$sb" && git checkout -q --detach HEAD ) >/dev/null 2>&1
  run bash -c "cd '$sb' && git rev-parse --abbrev-ref HEAD"
  [ "$output" = "HEAD" ]
  run bash -c "cd '$sb' && $bp_env bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -eq 20 ] \
    || { echo "a detached push skipped the merge count" >&2; echo "$output" >&2; return 1; }
  [[ "$output" == *"Linear history required"* ]]

  # The control that keeps the rule from becoming "refuse everything": a range
  # with no merge in it passes, detached or not.
  local lin
  lin="$(cd "$sb" && git rev-parse HEAD~2)"
  run bash -c "cd '$sb' && git rev-list --count --merges '$lin..$(cd "$sb" && git rev-parse HEAD~1)'"
  [ "$output" = "0" ]
  run bash -c "cd '$sb' && CI_GATE_REQUIRE_LINEAR_HISTORY=1 CI_GATE_MODE=ship \
    CI_GATE_PUSH_OLD_SHA='$lin' CI_GATE_PUSH_NEW_SHA=\"\$(git rev-parse HEAD~1)\" \
    CI_GATE_PUSH_REMOTE=origin bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"Linear history required"* ]]

  # And the control that keeps the rest of the check intact: with the rule off,
  # a merge in the range is not this check's business in either checkout.
  run bash -c "cd '$sb' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' CI_GATE_PUSH_REMOTE=origin \
    bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -eq 0 ]
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

@test "git: a tag push is covered only when its commit is already published" {
  # Two reviewers pulled this in opposite directions and both were right about
  # their own failure. Refusing every tag push failed the whole ship gate on
  # `git tag v1.0 <older commit>; git push origin v1.0`, an ordinary release
  # workflow. But allowing any tag on an *ancestor* of HEAD was too loose: a
  # failing commit can be tagged, repaired in a descendant, and the tag pushed
  # while the lanes validate the repaired HEAD. Ancestry says the worktree
  # contains that history; it says nothing about the tagged tree being checked.
  #
  # What settles it is publication. A commit a remote branch already contains
  # went out with a branch push and was gated then, so the tag adds a label and
  # no content. A commit no remote branch contains is carried out by the tag
  # itself, and nothing has ever checked that tree.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/remote"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" \
     "$REPO_ROOT/ci/lib/git.sh" "$sb/ci/lib/"
  ( cd "$sb/remote" && git init -q --bare . ) >/dev/null 2>&1
  (
    cd "$sb"
    git init -q -b main .
    printf 'a\n' > a.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c1
    git remote add origin ./remote
    git push -q origin main
    printf 'b\n' > b.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c2
    printf 'c\n' > c.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c3
    printf '%s %s\n' "$(git rev-parse HEAD~2)" "$(git rev-parse HEAD~1)" > .shas
  ) >/dev/null 2>&1
  local published unpublished
  read -r published unpublished < "$sb/.shas"

  # The premises: one commit is on a remote branch and the other is not, and
  # neither is HEAD -- otherwise the exact-match arm answers before this rule.
  run bash -c "cd '$sb' && git branch -r --contains '$published'"
  [ -n "$output" ]
  run bash -c "cd '$sb' && git branch -r --contains '$unpublished'"
  [ -z "$output" ]
  run bash -c "cd '$sb' && git rev-parse HEAD"
  [ "$output" != "$published" ]
  [ "$output" != "$unpublished" ]

  # A tag push names no branch destination: set-and-empty, not unset. The
  # destination remote comes from the pre-push hook's first argument, and the
  # question is whether *that* remote already carries the commit.
  run bash -c "cd '$sb' && . ci/lib/common.sh && . ci/lib/git.sh \
    && CI_GATE_PUSH_NEW_SHA='$published' CI_GATE_PUSH_REMOTE_REFS= \
       CI_GATE_PUSH_REMOTE=origin ci::git::worktree_covers_push"
  [ "$status" -eq 0 ]

  # Published somewhere else is not published here. `git branch -r --contains`
  # walks every remote-tracking branch, so a commit carried only by another
  # remote counted as gated for a push to origin -- and the tag then uploads a
  # tree origin has never seen while the lanes report on the repaired HEAD.
  run bash -c "cd '$sb' && . ci/lib/common.sh && . ci/lib/git.sh \
    && CI_GATE_PUSH_NEW_SHA='$published' CI_GATE_PUSH_REMOTE_REFS= \
       CI_GATE_PUSH_REMOTE=upstream ci::git::worktree_covers_push"
  [ "$status" -ne 0 ]

  # With no destination remote there is nothing to scope the question to, and
  # answering it from every remote is the defect. This refuses instead.
  run bash -c "cd '$sb' && . ci/lib/common.sh && . ci/lib/git.sh \
    && CI_GATE_PUSH_NEW_SHA='$published' CI_GATE_PUSH_REMOTE_REFS= ci::git::worktree_covers_push"
  [ "$status" -ne 0 ]

  # A tag carrying an unpublished commit is refused, ancestor or not.
  run bash -c "cd '$sb' && . ci/lib/common.sh && . ci/lib/git.sh \
    && CI_GATE_PUSH_NEW_SHA='$unpublished' CI_GATE_PUSH_REMOTE_REFS= \
       CI_GATE_PUSH_REMOTE=origin ci::git::worktree_covers_push"
  [ "$status" -ne 0 ]

  # And a *branch* push of the published commit is still refused -- the
  # relaxation is for tags only, or `git push origin other-branch` walks back in.
  run bash -c "cd '$sb' && . ci/lib/common.sh && . ci/lib/git.sh \
    && CI_GATE_PUSH_NEW_SHA='$published' CI_GATE_PUSH_REMOTE_REFS=other \
       CI_GATE_PUSH_REMOTE=origin ci::git::worktree_covers_push"
  [ "$status" -ne 0 ]

  # A destination list that is *unset* is not the tag case: it means nobody told
  # us what this push targets, and a tip that is set and unequal is still a tree
  # the lanes cannot speak for -- even for a commit that is published, since
  # without a destination there is nothing to say this is a tag at all.
  run bash -c "cd '$sb' && . ci/lib/common.sh && . ci/lib/git.sh \
    && CI_GATE_PUSH_NEW_SHA='$published' ci::git::worktree_covers_push"
  [ "$status" -ne 0 ]

  # No tip at all is the real "nobody said": CI and every direct invocation run
  # against whatever is checked out by design, and there is no second tree to be
  # wrong about.
  run bash -c "cd '$sb' && . ci/lib/common.sh && . ci/lib/git.sh \
    && ci::git::worktree_covers_push"
  [ "$status" -eq 0 ]
  rm -rf "$sb"
}

@test "runner: a malformed timeout_sec is rejected, not stripped into a number" {
  # `gsub(/[^0-9]/, "")` deleted the non-digits and joined what was left, so
  # `1e3` became 13 and `-1` became 1. The runner then killed a blocking check
  # seconds in and reported an infrastructure timeout the configuration never
  # asked for -- a lane that does not finish is a lane that does not run.
  local cfg
  cfg="$(mktemp)"
  printf 'checks:\n  alpha:\n    timeout_sec: 1e3\n  beta:\n    timeout_sec: -1\n  gamma:\n    timeout_sec: 900\n' > "$cfg"

  run bash -c ". '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    CI_CHECKS_CONFIG='$cfg' ci::runner::_declared_timeout alpha 2>/dev/null"
  [ -z "$output" ]

  run bash -c ". '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    CI_CHECKS_CONFIG='$cfg' ci::runner::_declared_timeout beta 2>/dev/null"
  [ -z "$output" ]

  # It must also say so rather than failing silently.
  run bash -c ". '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    CI_CHECKS_CONFIG='$cfg' ci::runner::_declared_timeout alpha 2>&1 >/dev/null"
  [[ "$output" == *"not a positive whole number"* ]]

  # The control: a well-formed value is still read, or this would have turned
  # every declared timeout off.
  run bash -c ". '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    CI_CHECKS_CONFIG='$cfg' ci::runner::_declared_timeout gamma 2>/dev/null"
  [ "$output" = "900" ]

  # And the real checks.yml still parses, since that is what actually runs.
  run bash -c "cd '$REPO_ROOT' && . ci/lib/runner.sh >/dev/null 2>&1; \
    ci::runner::_declared_timeout tests-shell 2>/dev/null"
  [ "$status" -eq 0 ]
  rm -f "$cfg"
}

@test "typecheck: a TypeScript workspace with no tsc is infrastructure, not a skip" {
  # _tc_js_workspace returns 127 when nothing under node_modules/.bin can be
  # executed -- the deliberate refusal to fall back to a global tsc, which would
  # be a different version from the one the lockfile pins. The caller logged
  # "skipped" and left OVERALL_RESULT at PASS, so an enabled typecheck-js lane
  # reported success over a project no compiler ever looked at. Reaching here
  # means discovery found a tsconfig.json and this lane was scheduled for it, so
  # "no tsc" does not mean "nothing to check".
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks" "$sb/ws/node_modules/.bin"
  cp "$REPO_ROOT/ci/checks/typecheck.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf '{ "compilerOptions": { "strict": true } }\n' > "$sb/ws/tsconfig.json"
  printf '{ "name": "w", "private": true }\n' > "$sb/ws/package.json"

  # The premise: a workspace is discovered, and it has no compiler.
  run bash -c "cd '$sb' && . ci/lib/common.sh && ci::common::node_workspaces tsconfig.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *ws* ]]
  [ ! -e "$sb/ws/node_modules/.bin/tsc" ]

  run bash -c "cd '$sb' && CI_GATE_CHECK_ID=typecheck-js bash ci/checks/typecheck.sh 2>&1"
  [ "$status" -eq 30 ]
  [[ "$output" == *"No workspace-local tsc"* ]]

  # The control: with a compiler present the lane reports on what it ran.
  printf '#!/usr/bin/env bash\nexit 0\n' > "$sb/ws/node_modules/.bin/tsc"
  chmod +x "$sb/ws/node_modules/.bin/tsc"
  run bash -c "cd '$sb' && CI_GATE_CHECK_ID=typecheck-js bash ci/checks/typecheck.sh 2>&1"
  [ "$status" -eq 0 ]

  # And a failing compiler is still a new issue, not infrastructure -- the two
  # verdicts have to stay apart or the distinction the contract draws is lost.
  printf '#!/usr/bin/env bash\nexit 2\n' > "$sb/ws/node_modules/.bin/tsc"
  chmod +x "$sb/ws/node_modules/.bin/tsc"
  run bash -c "cd '$sb' && CI_GATE_CHECK_ID=typecheck-js bash ci/checks/typecheck.sh 2>&1"
  [ "$status" -eq 20 ]
  rm -rf "$sb"
}

@test "git-safety: a newline path repeated across commits is collected once" {
  # _gs_content_files emits one record per commit per modification, and the
  # newline arm appended every one of them before reaching the _gs_seen dedup
  # below it. The slow path then called _gs_max_blob_size once per duplicate,
  # and each call walks every outgoing commit again -- the quadratic cost the
  # batch exists to remove, reintroduced on the one path that cannot use the
  # batch. The name is the pusher's to choose, so it is reachable on purpose.
  #
  # The real collector is extracted and driven, rather than a transcription of
  # it: a filename holding a newline cannot be staged on every platform this
  # suite runs on -- git on Windows refuses the path outright ("Ignoring path")
  # -- so a fixture built through git would pass against the broken code too and
  # assert nothing. The producer is the only thing replaced.
  local sb
  sb="$(mktemp -d)"
  # The extraction now starts at the temp file the collector reads through
  # rather than at the array, because the producer's status has to be checkable
  # and a process substitution is a subshell that swallows it. Two names the
  # extracted block needs are supplied here, for the same reason the producer
  # is: what this case is about is the dedup, not the diagnostics around it.
  {
    printf '%s\n' 'set -Eeuo pipefail'
    printf '%s\n' 'CI_RESULT_FAIL_INFRA=30'
    printf '%s\n' 'GATE_WHAT=staged'
    printf '%s\n' '_gs_content_files() { printf "%s\0" "$NL1" "$NL1" "plain.py" "plain.py" "$NL2" "$NL1"; }'
    sed -n '/^_GS_PATHLIST=/,/^rm -f "\$_GS_PATHLIST"/p' "$REPO_ROOT/ci/checks/git-safety.sh"
    printf '%s\n' 'printf "nl=%s ordinary=%s\n" "${#_GS_NL_PATHS[@]}" "${#_GS_PATHS[@]}"'
  } > "$sb/drive.sh"

  # The premise: the extraction found the arm under test.
  run grep -c '_GS_NL_PATHS+=' "$sb/drive.sh"
  [ "$output" -eq 1 ]

  run bash -c "NL1=\$'a\nb.py' NL2=\$'c\nd.py' bash '$sb/drive.sh'"
  [ "$status" -eq 0 ]
  # Six records in, three distinct paths out: two newline paths and one
  # ordinary one, which is what _gs_seen was already doing for the ordinary
  # half all along.
  [[ "$output" == *"nl=2"* ]]
  [[ "$output" == *"ordinary=1"* ]]
  rm -rf "$sb"
}

@test "git: a hook-supplied tip with no base walks the whole tip" {
  # The fallbacks in push_range exist for callers who said nothing -- CI, a
  # direct invocation. They were being applied to the pre-push hook too, which
  # does say: it leaves the base unset precisely when the destination has none
  # of this history. Filling that in from @{upstream} or a local `main` gave
  # `main..tip` for a first push to an empty destination, and git-safety, the
  # signature check and the changeset scan all skipped every commit up to
  # `main`. A secret in the omitted history uploads past a green gate.
  local sb
  sb="$(mktemp -d)"
  (
    cd "$sb"
    git init -q -b main .
    printf 'a\n' > a.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c1
    root="$(git rev-parse HEAD)"
    printf 'b\n' > b.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c2
    # A remote-tracking `main` exists and is exactly what the old fallback
    # would have found and used as a base.
    git update-ref refs/remotes/origin/main "$root"
    git checkout -q -b feature
    printf 'c\n' > c.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c3
    printf '%s %s\n' "$root" "$(git rev-parse HEAD)" > .shas
  ) >/dev/null 2>&1
  local root tip
  read -r root tip < "$sb/.shas"

  # The premise: the guess is available, and it is wrong.
  run bash -c "cd '$sb' && git rev-parse --verify refs/remotes/origin/main"
  [ "$status" -eq 0 ]

  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' && CI_GATE_PUSH_NEW_SHA='$tip' ci::git::push_range"
  [ "$status" -eq 0 ]
  [ "$output" = "$tip" ]

  # Which is not a cosmetic difference: it is the number of commits the scans
  # then walk.
  run bash -c "cd '$sb' && git rev-list '$output' | wc -l"
  [ "$(printf '%s' "$output" | tr -d ' ')" = "3" ]

  # And a base the hook *did* supply is still honoured.
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' && CI_GATE_PUSH_NEW_SHA='$tip' CI_GATE_PUSH_OLD_SHA='$root' ci::git::push_range"
  [ "$output" = "${root}..${tip}" ]

  # The other half of the same rule, and the reason the guess above is refused
  # while this is not. The *named destination* bounds what this push adds, so
  # the first push of a branch does not walk its entire history -- 439 commits
  # in this repository, twelve of which fail the whitespace scan, so `git push
  # -u origin <new-branch>` was refused with no remedy available to the person
  # pushing.
  #
  # Established by publishing rather than by writing a tracking ref. That ref is
  # this clone's memory of the destination and is no longer what the answer
  # comes from: a force-push elsewhere leaves it reaching commits the remote has
  # dropped, and this would then narrow past exactly the commits the push makes
  # reachable again.
  run ci::tests::publish "$sb" origin main "$root"
  [ "$status" -eq 0 ]
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' && CI_GATE_PUSH_REMOTE=origin CI_GATE_PUSH_NEW_SHA='$tip' ci::git::push_range"
  [ "$output" = "${root}..${tip}" ]

  # Scoped to that destination, for the reason the publication test is: a commit
  # that exists only on `upstream` says nothing about a push to `origin`.
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' && CI_GATE_PUSH_REMOTE=upstream CI_GATE_PUSH_NEW_SHA='$tip' ci::git::push_range"
  [ "$output" = "$tip" ]
  rm -rf "$sb"
}

@test "git: the destination remote is matched literally, not as a pattern" {
  # `grep "^${remote}/"` interpolates the remote name into an expression, so
  # `release.prod` matched `releaseXprod/main` and a tag whose commit is
  # published only on some unrelated remote read as published to this one. The
  # gate then approves a tree the destination has never seen while the lanes
  # validate the repaired HEAD.
  local sb
  sb="$(mktemp -d)"
  (
    cd "$sb"
    git init -q -b main .
    printf 'a\n' > a.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c1
    printf 'b\n' > b.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c2
    tip="$(git rev-parse HEAD)"
    git checkout -q HEAD~1
    # The tagged commit is not the checkout, which is what sends the question
    # to the publication rule at all.
    git update-ref refs/remotes/releaseXprod/main "$tip"
    printf '%s\n' "$tip" > .sha
  ) >/dev/null 2>&1
  local tip
  read -r tip < "$sb/.sha"

  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' \
    && CI_GATE_PUSH_NEW_SHA='$tip' CI_GATE_PUSH_REMOTE_REFS= CI_GATE_PUSH_REMOTE=release.prod \
       ci::git::worktree_covers_push"
  [ "$status" -ne 0 ]

  # The control: the remote it really names still matches. Published to a real
  # remote of that name, because the tracking ref alone no longer answers.
  run ci::tests::publish "$sb" release.prod main "$tip"
  [ "$status" -eq 0 ]
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' \
    && CI_GATE_PUSH_NEW_SHA='$tip' CI_GATE_PUSH_REMOTE_REFS= CI_GATE_PUSH_REMOTE=release.prod \
       ci::git::worktree_covers_push"
  [ "$status" -eq 0 ]
  rm -rf "$sb"
}

@test "tests-shell: an unreadable ignored listing is infrastructure, not a clean tree" {
  # `| grep ... || true` puts the `|| true` on the pipeline, so it answers for
  # grep -- which exits 1 whenever it filters everything out, the ordinary case
  # -- and never for the enumeration in front of it. An unreadable index then
  # produced an empty list that reads exactly like "nothing is ignored here",
  # and this is the one of the three scans that can see a worktree replacement
  # for a path the pushed commits delete.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib" "$sb/ci/tests" "$sb/.githooks" "$sb/bin"
  cp "$REPO_ROOT/ci/checks/tests-shell.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf '#!/usr/bin/env bats\n@test "x" { true; }\n' > "$sb/ci/tests/t.bats"
  printf '#!/usr/bin/env bash\ntrue\n' > "$sb/.githooks/pre-push"
  printf 'nothing\n' > "$sb/.gitignore"
  (
    cd "$sb"
    git init -q -b main .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
  ) >/dev/null 2>&1

  # A git that answers every question except the ignored-file enumeration.
  # Nothing else about the run changes, so a refusal can only come from this
  # producer.
  local realgit
  realgit="$(command -v git)"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'for a in "$@"; do\n'
    printf '  if [ "$a" = "--ignored" ]; then exit 1; fi\n'
    printf 'done\n'
    printf 'exec "%s" "$@"\n' "$realgit"
  } > "$sb/bin/git"
  chmod +x "$sb/bin/git"

  # The premises: the stub fails for that one question and for nothing else.
  run bash -c "cd '$sb' && PATH=\"$sb/bin:\$PATH\" git rev-parse --verify HEAD"
  [ "$status" -eq 0 ]
  run bash -c "cd '$sb' && PATH=\"$sb/bin:\$PATH\" git ls-files --others --ignored --exclude-standard -- ci"
  [ "$status" -ne 0 ]

  run bash -c "cd '$sb' && PATH=\"$sb/bin:\$PATH\" CI_GATE_MODE=ship bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 30 ]
  [[ "$output" == *"Cannot read the tree these suites are being compared against"* ]]
  rm -rf "$sb"
}

@test "preflight: a deletion-only push runs destination protection and nothing else" {
  # `git push --delete origin feature` sends no content, so every content lane
  # was reporting on whatever happened to be checked out: a layout error or a
  # failing suite in the worktree refused a deletion that carries neither, and
  # it cost the half hour the shell suites take. git-safety already self-skipped
  # on the flag; the ship *plan* did not, which is the same rule missing one
  # tree over.
  local sb
  sb="$(mktemp -d)"
  cp -r "$REPO_ROOT/ci" "$sb/ci"
  rm -rf "$sb/ci/tests" "$sb/ci/reports" "$sb/ci/artifacts"
  # Every check becomes a stub that names itself and passes, so what the run
  # *scheduled* is readable from the output and no real lane executes.
  local f
  for f in "$sb"/ci/checks/*.sh; do
    printf '#!/usr/bin/env bash\necho "RAN:%s"\nexit 0\n' "$(basename "$f" .sh)" > "$f"
    chmod +x "$f"
  done
  (
    cd "$sb"
    printf 'x\n' > a.txt
    git init -q -b main .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
  ) >/dev/null 2>&1

  _pf_lanes() { # _pf_lanes <env assignments...>
    ( cd "$sb" && env "$@" CI_GATE_USE_LANES=0 bash ci/preflight.sh --mode "$MODE_UNDER_TEST" 2>&1 ) \
      | grep -o 'RAN:[a-z-]*' | sed 's/RAN://' | sort -u | tr '\n' ' '
  }

  # The premise: an ordinary ship push does schedule the content lanes, so an
  # empty list below is the flag acting and not the harness failing to run.
  MODE_UNDER_TEST=ship
  run _pf_lanes CI_GATE_PUSH_NEW_SHA=HEAD
  [[ "$output" == *node* ]]
  [[ "$output" == *test-layout* ]]
  [[ "$output" == *tests-shell* ]]
  [[ "$output" == *branch-protection* ]]

  run _pf_lanes CI_GATE_PUSH_DELETIONS_ONLY=1 CI_GATE_PUSH_REMOTE_REFS=feature
  [ "$status" -eq 0 ]
  # Destination protection is the one thing that must survive: `git push origin
  # :main` is a deletion too, and it is exactly the push to refuse.
  [[ "$output" == *branch-protection* ]]
  [[ "$output" != *node* ]]
  [[ "$output" != *test-layout* ]]
  [[ "$output" != *tests-shell* ]]
  [[ "$output" != *build* ]]
  [[ "$output" != *security* ]]

  # And `full` is a deliberate whole-tree run. It is not a push at all, so an
  # environment variable left over from one must not narrow it.
  MODE_UNDER_TEST=full
  run _pf_lanes CI_GATE_PUSH_DELETIONS_ONLY=1
  [[ "$output" == *test-layout* ]]
  [[ "$output" == *tests-shell* ]]
  rm -rf "$sb"
}

@test "git-safety: a conflict resolved by committing the markers is caught" {
  # `git show` of a merge produces a *combined* diff, which shows only hunks
  # differing from every parent -- so git prints nothing for the ordinary case
  # and `--check` reports nothing over it. Both conflict-marker scans read that
  # output, so a merge whose resolution left the markers in the file went out
  # through a gate that printed "Git safety checks passed".
  gs_setup
  (
    cd "$GS_SB"
    printf 'base\n' > f.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm base
    git rev-parse HEAD > .base
    git checkout -q -b side
    printf 'side\n' > f.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm side
    git checkout -q feature/x
    printf 'mainline\n' > f.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm mainline
    git merge side || true
    printf 'resolved\n<<<<<<< HEAD\nkeep\n=======\n' > f.txt
    git add f.txt
    git -c user.email=t@t -c user.name=t commit -qm merge
  ) >/dev/null 2>&1
  local base tip
  read -r base < "$GS_SB/.base"
  tip="$(cd "$GS_SB" && git rev-parse HEAD)"

  # The premise: git really is silent about this merge.
  run bash -c "cd '$GS_SB' && git show --format= --check HEAD"
  [ "$status" -eq 0 ]
  [ -z "$output" ]

  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"conflict-marker"* || "$output" == *"conflict markers"* ]]
  rm -rf "$GS_SB"
}

@test "git-safety: a diff that cannot be produced is not a clean tree" {
  # Two independent holes, both live. In index mode `_gs_diff` never captured
  # `git diff --cached`'s status at all, so a failing diff returned 0 with no
  # output; and `_gs_diff | grep ... || rc=$?` records grep's status, where a
  # producer failure (1) is indistinguishable from grep's "no match" and can
  # also overwrite grep's "matched". A .gitattributes naming a textconv filter
  # that is not installed reaches both.
  gs_setup
  (
    cd "$GS_SB"
    printf '*.dat diff=nope\n' > .gitattributes
    gs_secret_line > s.dat
    git config diff.nope.textconv /nonexistent-textconv-binary
    git add .gitattributes s.dat
  ) >/dev/null 2>&1

  run bash -c "cd '$GS_SB' && CI_GATE_MODE=quick bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 30 ]
  [[ "$output" == *"never ran"* || "$output" == *"could not be produced"* ]]

  # The control: the identical file without the broken filter is caught, so the
  # scan does work and it was the producer that silenced it.
  ( cd "$GS_SB" && git rm -q --cached .gitattributes && rm -f .gitattributes ) >/dev/null 2>&1
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=quick bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like value"* ]]
  rm -rf "$GS_SB"
}

@test "git-safety: an unstaged worktree edit is not part of the push" {
  # `git diff --check` over the worktree ran unconditionally, above the mode
  # branch, so a trailing space in a work-in-progress line -- in no outgoing
  # commit and no commit at all -- refused the push. There is no remedy in the
  # push: you have to stash unrelated work to send commits that are clean. It
  # contradicts the rule the same file states twelve lines lower, that the
  # reference follows the gate's own mode.
  gs_setup
  (
    cd "$GS_SB"
    printf 'clean\n' > g.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm clean
    git rev-parse HEAD~1 > .base
    printf 'wip line with trailing space \n' >> g.txt
  ) >/dev/null 2>&1
  local base tip
  read -r base < "$GS_SB/.base"
  tip="$(cd "$GS_SB" && git rev-parse HEAD)"

  # The premise: the worktree really is dirty in the way that used to refuse.
  run bash -c "cd '$GS_SB' && git diff --check"
  [ "$status" -ne 0 ]

  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 0 ]

  # And the pre-commit gate, which does stand behind the worktree, still says so.
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=quick bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"unstaged changes"* ]]
  rm -rf "$GS_SB"
}

@test "tests-shell: the presence guard asks about the set the runner runs" {
  # The guard walked ci/tests/ recursively while tests.sh runs `bats ci/tests/`,
  # which is not recursive. A committed fixture at
  # ci/tests/fixtures/shell/tests/hello.bats therefore satisfied the guard on
  # behalf of suites that were not there: bats collected nothing, exited 0, and
  # the lane reported PASS over zero tests -- found-nothing read as
  # everything-passed, the exact state this wrapper says it exists to prevent.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib" "$sb/ci/tests/fixtures/shell/tests"
  cp "$REPO_ROOT/ci/checks/tests-shell.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf '#!/usr/bin/env bats\n@test "x" { true; }\n' > "$sb/ci/tests/fixtures/shell/tests/hello.bats"
  ( cd "$sb" && git init -q -b main . && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm init ) >/dev/null 2>&1

  # The premise: bats really does collect nothing from that tree.
  run bash -c "cd '$sb' && bats --formatter junit ci/tests/ 2>/dev/null"
  [ "$status" -eq 0 ]

  run bash -c "cd '$sb' && bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"No .bats suites found"* ]]

  # The control: a suite where the runner will actually see it. The runner is
  # replaced by a stand-in that announces itself, because "the guard's message
  # is absent" would also be satisfied by an exec that failed with 127 -- the
  # control has to show the guard handed control on, not that something else
  # went wrong first.
  printf '#!/usr/bin/env bash\necho DELEGATED\nexit 0\n' > "$sb/ci/checks/tests.sh"
  chmod +x "$sb/ci/checks/tests.sh"
  printf '#!/usr/bin/env bats\n@test "y" { true; }\n' > "$sb/ci/tests/real.bats"
  run bash -c "cd '$sb' && bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELEGATED"* ]]
  [[ "$output" != *"No .bats suites found"* ]]
  rm -rf "$sb"
}

@test "preflight: a changeset of ignored paths is not an empty changeset" {
  # "No relevant changes" meant "no path named a language", which is a statement
  # about the lanes and not about the tree. Every path can be auto-ignored --
  # dist/, build/, vendor/, node_modules/ -- or simply carry no language, a .md
  # file or a LICENSE, and the changeset comes out with an empty language list
  # and a perfectly non-empty file list. git-safety is the lane that blocks
  # exactly those paths, and it never ran.
  local sb
  sb="$(mktemp -d)"
  cp -r "$REPO_ROOT/ci" "$sb/ci"
  rm -rf "$sb/ci/tests" "$sb/ci/reports" "$sb/ci/artifacts"
  local f
  for f in "$sb"/ci/checks/*.sh; do
    case "$(basename "$f")" in common.sh) continue ;; esac
    printf '#!/usr/bin/env bash\necho "RAN:%s"\nexit 0\n' "$(basename "$f" .sh)" > "$f"
    chmod +x "$f"
  done
  ( cd "$sb" && git init -q -b main . && printf 'x\n' > a.txt && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm init ) >/dev/null 2>&1
  ( cd "$sb" && mkdir -p dist \
    && gs_secret_line > dist/bundle.js \
    && git add -f dist/bundle.js ) >/dev/null 2>&1

  run bash -c "cd '$sb' && CI_GATE_USE_LANES=0 bash ci/preflight.sh --mode quick 2>&1"
  [[ "$output" != *"No relevant changes detected"* ]]
  [[ "$output" == *"RAN:git-safety"* ]]

  # The control: nothing staged at all still takes the fast exit, which is what
  # that exit is for.
  ( cd "$sb" && git reset -q --hard && rm -rf dist ) >/dev/null 2>&1
  run bash -c "cd '$sb' && CI_GATE_USE_LANES=0 bash ci/preflight.sh --mode quick 2>&1"
  [[ "$output" == *"No relevant changes detected"* ]]
  rm -rf "$sb"
}

@test "git-safety: work that is not being pushed does not block the push" {
  # The conflict-marker rule has two halves: a range scan, which answers for the
  # outgoing commits, and has_conflict_markers_in_changed, which reads `git
  # diff` -- the unstaged worktree. Only the range half was scoped to the mode,
  # so in ship mode a marker in a file nobody is sending refused a clean push.
  # The unstaged `git diff --check` beside it was scoped to pre-commit two
  # rounds ago and this half was left behind: the same rule, fixed on one
  # spelling and absent on the other.
  #
  # The marker is indented, which is what isolates the helper. `git diff
  # --check` only reports one at column zero, so it stays quiet here and the
  # grep-based scan is the only thing that can speak -- otherwise "refused in
  # quick mode" would be satisfied by the whitespace check and would assert
  # nothing about the helper this case is scoping.
  gs_setup
  printf 'clean
' > "$GS_SB/f.txt"
  gs_commit "clean"
  local base tip
  base="$( cd "$GS_SB" && git rev-parse HEAD~1 )"
  tip="$( cd "$GS_SB" && git rev-parse HEAD )"
  printf '  <<<<<<< LOCAL-WIP
' >> "$GS_SB/f.txt"

  # The premise: the whitespace check does not see this one, so whatever answers
  # below is the helper and not that.
  run bash -c "cd '$GS_SB' && git diff --check"
  [ "$status" -eq 0 ]

  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"conflict markers found"* ]]

  # The control that keeps the rule: the pre-commit gate is the one that stands
  # behind the worktree, and the helper still refuses the same file there.
  run gs_run quick
  [ "$status" -eq 20 ]
  [[ "$output" == *"Merge conflict markers found in changed content"* ]]

  # And the second control: a marker inside the pushed range is still caught in
  # ship mode, so this scopes the worktree helper rather than removing the rule.
  gs_commit "wip"
  local tip2
  tip2="$( cd "$GS_SB" && git rev-parse HEAD )"
  run bash -c "cd '$GS_SB' && CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA='$base' CI_GATE_PUSH_NEW_SHA='$tip2' bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"conflict markers found"* ]]
  rm -rf "$GS_SB"
}

@test "preflight: a content-free push is decided before the scheduler is chosen" {
  # Both content-free paths lived inside the `full|ship` arm, which sits after
  # the lanes.conf branch and its `return 0`. With CI_GATE_USE_LANES=1 and
  # ci/config/lanes.conf present -- a supported configuration -- neither was
  # reachable: a deletion-only push ran the whole lane list over whatever
  # happened to be checked out.
  #
  # And worse there than in the default plan, because lanes.conf carries no
  # branch-protection entry. The wide path was missing the one check both narrow
  # paths deliberately keep, and the narrow path was unreachable, so each was
  # missing the other's protection.
  local sb
  sb="$(mktemp -d)"
  cp -r "$REPO_ROOT/ci" "$sb/ci"
  rm -rf "$sb/ci/tests" "$sb/ci/reports" "$sb/ci/artifacts"
  local f
  for f in "$sb"/ci/checks/*.sh; do
    printf '#!/usr/bin/env bash\necho "RAN:%s"\nexit 0\n' "$(basename "$f" .sh)" > "$f"
    chmod +x "$f"
  done
  (
    cd "$sb"
    printf 'x\n' > a.txt
    git init -q -b main .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
  ) >/dev/null 2>&1
  local published
  published="$(cd "$sb" && git rev-parse HEAD)"
  ci::tests::publish "$sb" origin main "$published"

  _pf_lanes() { # _pf_lanes <mode> <env assignments...>
    local _m="$1"; shift
    ( cd "$sb" && env "$@" bash ci/preflight.sh --mode "$_m" 2>&1 ) \
      | grep -o 'RAN:[a-z-]*' | sed 's/RAN://' | sort -u | tr '\n' ' '
  }

  # The configuration this case is about, read from the file rather than
  # asserted: lanes.conf schedules the content lanes, so their absence below is
  # the rule acting.
  #
  # This premise used to read `grep -c 'branch-protection' lanes.conf` and
  # require 0, with a message saying the case needed rewriting if that changed.
  # It changed: a later review round had branch-protection and typecheck-js
  # added to the file so the two full/ship schedulers agree. So
  # branch-protection present no longer separates the short path from the wide
  # one -- both run it now -- and test-layout absent is what does. The rule
  # under test is unchanged; only the discriminator moved.
  run grep -c '^test-layout|' "$sb/ci/config/lanes.conf"
  [ "$output" = "1" ] \
    || { echo "lanes.conf no longer schedules test-layout; this case needs rewriting" >&2; rm -rf "$sb"; return 1; }

  # The premise: under lanes.conf an ordinary ship push does schedule the lane
  # list, so an absence below is the rule acting and not the harness failing.
  run _pf_lanes ship CI_GATE_USE_LANES=1 CI_GATE_PUSH_NEW_SHA="$published"
  [[ "$output" == *test-layout* ]] \
    || { echo "the lanes.conf plan did not run: $output" >&2; rm -rf "$sb"; return 1; }
  [[ "$output" == *node* ]]

  # A deletion-only push, in the mode where it used to be unreachable.
  run _pf_lanes ship CI_GATE_USE_LANES=1 CI_GATE_PUSH_DELETIONS_ONLY=1 \
                CI_GATE_PUSH_REMOTE_REFS=feature
  [ "$status" -eq 0 ]
  [[ "$output" == *branch-protection* ]] \
    || { echo "lanes.conf mode skipped destination protection: $output" >&2; rm -rf "$sb"; return 1; }
  [[ "$output" != *test-layout* ]] \
    || { echo "a deletion ran the content lanes under lanes.conf: $output" >&2; rm -rf "$sb"; return 1; }
  [[ "$output" != *node* ]]
  [[ "$output" != *tests-shell* ]]

  # And the label-only path, which is the same hoist. Asserted separately
  # because "the same rule missing one tree over" is how each of these was found
  # in the first place: one of the two nested back under the scheduler would
  # leave the other's case green.
  run _pf_lanes ship CI_GATE_USE_LANES=1 CI_GATE_PUSH_REMOTE=origin \
                CI_GATE_PUSH_REMOTE_REFS= CI_GATE_PUSH_TAG_TIPS="$published"
  [ "$status" -eq 0 ]
  [[ "$output" == *branch-protection* ]] \
    || { echo "lanes.conf mode skipped destination protection for a tag: $output" >&2; rm -rf "$sb"; return 1; }
  [[ "$output" != *test-layout* ]] \
    || { echo "a label-only push ran the content lanes under lanes.conf: $output" >&2; rm -rf "$sb"; return 1; }

  # The control that the hoist did not widen the rule: `full` is a deliberate
  # whole-tree run, not a push, so a leftover push variable must not narrow it
  # in this mode either.
  run _pf_lanes full CI_GATE_USE_LANES=1 CI_GATE_PUSH_DELETIONS_ONLY=1
  [[ "$output" == *test-layout* ]] \
    || { echo "a push variable narrowed a full run: $output" >&2; rm -rf "$sb"; return 1; }
  rm -rf "$sb"
}

@test "preflight: quick does not run the full lane list" {
  # ci/config/lanes.conf carries no mode column, and this branch was reached
  # before the dispatch on $MODE -- so `--mode quick` under CI_GATE_USE_LANES=1
  # ran every lane in the file. That file is a description of the full plan:
  # security, build, debt and tests-shell are all in it, and tests-shell is the
  # bats suite, about an hour here against a 30s pre-commit budget. A commit
  # could then fail on a gate self-test unrelated to anything staged.
  local sb
  sb="$(mktemp -d)"
  cp -r "$REPO_ROOT/ci" "$sb/ci"
  rm -rf "$sb/ci/tests" "$sb/ci/reports" "$sb/ci/artifacts"
  local f
  for f in "$sb"/ci/checks/*.sh; do
    printf '#!/usr/bin/env bash\necho "RAN:%s"\nexit 0\n' "$(basename "$f" .sh)" > "$f"
    chmod +x "$f"
  done
  (
    cd "$sb"
    printf 'x\n' > a.txt
    git init -q -b main .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
    # A staged change, because quick with a clean tree reports "No relevant
    # changes detected. Skipping gate." and runs nothing at all -- on which an
    # assertion that the suite did not run passes without the rule existing.
    # And a *shell* change specifically: the lane list is changeset-filtered
    # like any other, so with only a text file staged the old code did not
    # schedule tests-shell either and the case proved nothing about it.
    printf '#!/usr/bin/env bash\ntrue\n' > tool.sh
    git add -A
  ) >/dev/null 2>&1

  _ql_lanes() { # _ql_lanes <mode> <env...>
    local _m="$1"; shift
    ( cd "$sb" && env "$@" bash ci/preflight.sh --mode "$_m" 2>&1 ) \
      | grep -o 'RAN:[a-z-]*' | sed 's/RAN://' | sort -u | tr '\n' ' '
  }

  # The premise, read from the file: lanes.conf really does schedule the suite,
  # so its absence below is this rule and not a missing entry.
  run grep -c '^tests-shell|' "$sb/ci/config/lanes.conf"
  [ "$output" = "1" ] \
    || { echo "lanes.conf no longer lists tests-shell; this case needs rewriting" >&2; rm -rf "$sb"; return 1; }

  # Measured against the tree before the fix, with this same fixture:
  #   old  quick+lanes -> build changed-files debt git-safety security
  #                       test-layout tests-shell
  #   new  quick+lanes -> changed-files git-safety test-layout
  run _ql_lanes quick CI_GATE_USE_LANES=1
  [[ "$output" != *tests-shell* ]] \
    || { echo "a pre-commit run scheduled the shell suite: $output" >&2; rm -rf "$sb"; return 1; }
  [[ "$output" != *debt* ]]
  [[ "$output" != *security* ]]
  [[ "$output" != *build* ]]
  # And it still runs the pre-commit lanes, so this is quick using its own plan
  # rather than quick running nothing.
  [[ "$output" == *test-layout* ]] \
    || { echo "quick stopped running its own lanes: $output" >&2; rm -rf "$sb"; return 1; }

  # debt, which the first version of this rule let through. That version read
  # `[ "$MODE" != "quick" ]` -- it fixed the mode in the report and nothing
  # else, so `--mode debt` took the lane branch and ran the whole full plan
  # before returning, instead of the two lanes its own arm schedules. Measured
  # on this fixture, with --all so the changeset short-circuit does not answer
  # first:
  #   old  debt+lanes -> branch-protection build changed-files debt git-safety
  #                      security test-layout tests-shell
  #   new  debt+lanes -> debt git-safety
  #
  # Asserted per mode rather than only for the one that was reported, because an
  # exclusion has to name every mode that must not reach the branch, including
  # ones added later; this is the case that notices when the next one is added.
  # Inline rather than through _ql_lanes: that helper hands its arguments to
  # `env`, and --all is preflight's, not the environment's.
  _ql_debt() {
    ( cd "$sb" && env CI_GATE_USE_LANES=1 bash ci/preflight.sh --mode debt --all 2>&1 ) \
      | grep -o 'RAN:[a-z-]*' | sed 's/RAN://' | sort -u | tr '\n' ' '
  }
  run _ql_debt
  [[ "$output" != *tests-shell* ]] \
    || { echo "a debt run scheduled the shell suite: $output" >&2; rm -rf "$sb"; return 1; }
  [[ "$output" != *security* ]]
  [[ "$output" != *test-layout* ]]
  [[ "$output" == *debt* ]] \
    || { echo "debt stopped running its own lanes: $output" >&2; rm -rf "$sb"; return 1; }

  # The control: full and ship are what lanes.conf describes, and they still
  # get it. Gating by mode must not disable the feature.
  run _ql_lanes full CI_GATE_USE_LANES=1
  [[ "$output" == *tests-shell* ]] \
    || { echo "the lane list stopped applying to full: $output" >&2; rm -rf "$sb"; return 1; }
  [[ "$output" == *debt* ]]
  rm -rf "$sb"
}

@test "preflight: the lane file and the built-in plan schedule the same lanes" {
  # Two schedulers describe the full/ship plan -- run_full_or_ship_checks and
  # ci/config/lanes.conf -- and nothing compared them. lanes.conf was missing
  # branch-protection and typecheck-js, so `CI_GATE_USE_LANES=1` ran a strictly
  # smaller plan than the same push without it: no destination protection, and
  # no lane compiling the TypeScript projects a workspace script does not name.
  # The second one matters more than a missing lane usually would, because the
  # `-p <project>` rule in ci/checks/node.sh accepts a script naming one project
  # *because* typecheck-js compiles the rest.
  #
  # Three separate findings came out of this one divergence -- the deletion-only
  # path being unreachable under lanes, quick running the whole file, and
  # typecheck-js missing from it -- which is what a pair of lists nobody
  # compares produces. Enumerated from both sources rather than restated here,
  # so a lane added to either has to reach the other.
  local built_in lanes
  built_in="$(awk '/^run_full_or_ship_checks\(\)/,/^}/' "$REPO_ROOT/ci/preflight.sh" \
    | grep -oE '"[a-z-]+:\./ci/checks/' | sed 's/^"//; s|:\./ci/checks/$||' | sort -u)"
  lanes="$(grep -vE '^[[:space:]]*(#|$)' "$REPO_ROOT/ci/config/lanes.conf" \
    | cut -d'|' -f1 | tr -d ' ' | sort -u)"

  # Both selectors have to still be selecting; either one matching nothing makes
  # the comparison below pass while comparing nothing.
  local n_built n_lanes
  n_built="$(printf '%s\n' "$built_in" | grep -c .)"
  n_lanes="$(printf '%s\n' "$lanes" | grep -c .)"
  [ "$n_built" -ge 8 ] \
    || { echo "read only $n_built lanes from run_full_or_ship_checks; the selector has drifted" >&2; return 1; }
  [ "$n_lanes" -ge 8 ] \
    || { echo "read only $n_lanes lanes from lanes.conf; the selector has drifted" >&2; return 1; }

  if [ "$built_in" != "$lanes" ]; then
    echo "the two full/ship schedulers disagree" >&2
    echo "only in run_full_or_ship_checks:" >&2
    comm -23 <(printf '%s\n' "$built_in") <(printf '%s\n' "$lanes") >&2
    echo "only in ci/config/lanes.conf:" >&2
    comm -13 <(printf '%s\n' "$built_in") <(printf '%s\n' "$lanes") >&2
    return 1
  fi
}

@test "git: a tag-only push gets the range the tag is publishing" {
  # ci/hook-dispatch.sh leaves the scalar tip unset for a push naming no branch
  # -- deliberately, so a tag on an unrelated lineage cannot displace a branch
  # tip -- and exports the per-ref tip lists instead. push_range read neither,
  # so a tag-only push fell to the `@{upstream}` guesses: for a tag at a HEAD
  # the checkout's upstream already contains, that is `HEAD..HEAD`. git-safety,
  # the signature walk and the linear-history check then reported on nothing
  # while the tag published that commit.
  #
  # Measured on the fixture below, before and after:
  #   old  ->  <tip>..HEAD      0 commits
  #   new  ->  <published>..<tip>   1 commit
  local sb
  sb="$(mktemp -d)"
  (
    cd "$sb"
    git init -q -b main .
    printf 'a
' > a.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c1
  ) >/dev/null 2>&1
  local published
  published="$(cd "$sb" && git rev-parse HEAD)"
  ci::tests::publish "$sb" origin main "$published"
  ( cd "$sb" && printf 'b
' > b.txt && git add -A     && git -c user.email=t@t -c user.name=t commit -qm c2     && git branch --set-upstream-to=origin/main main ) >/dev/null 2>&1
  local tip
  tip="$(cd "$sb" && git rev-parse HEAD)"
  # The stale record that produced the empty range: this clone believes the
  # destination already has HEAD. The destination itself does not.
  ( cd "$sb" && git update-ref refs/remotes/origin/main "$tip" ) >/dev/null 2>&1

  # env assignments as real arguments rather than interpolated into one string:
  # `env $* ci::git::push_range` asks env to exec a shell *function*, which it
  # cannot see, and the whole thing exits 127 with the assertion below blaming
  # the range.
  _pr_range() { # _pr_range <VAR=value...>
    ( cd "$sb" && env "$@" bash -c ". '$REPO_ROOT/ci/lib/git.sh' && ci::git::push_range" )
  }

  # The tag publishes the commit the destination does not have, so the range has
  # to contain it.
  run _pr_range CI_GATE_PUSH_NEW_SHA= CI_GATE_PUSH_BRANCH_TIPS= \
                CI_GATE_PUSH_TAG_TIPS="$tip" CI_GATE_PUSH_REMOTE=origin
  [ "$status" -eq 0 ]
  [ "$output" = "${published}..${tip}" ]     || { echo "a tag-only push got range '$output', expected ${published}..${tip}" >&2; rm -rf "$sb"; return 1; }
  run bash -c "cd '$sb' && git rev-list --count '${published}..${tip}'"
  [ "$output" = "1" ]     || { echo "the range walks $output commits, expected 1" >&2; rm -rf "$sb"; return 1; }

  # A tag on a commit the destination already carries is publishing nothing, and
  # an empty range is the true answer for it -- push_is_label_only routes that
  # push past the content lanes separately.
  run _pr_range CI_GATE_PUSH_NEW_SHA= CI_GATE_PUSH_BRANCH_TIPS= \
                CI_GATE_PUSH_TAG_TIPS="$published" CI_GATE_PUSH_REMOTE=origin
  [ "$status" -eq 0 ]
  [ "$output" = "${published}..${published}" ]     || { echo "an already-published tag got range '$output'" >&2; rm -rf "$sb"; return 1; }

  # The control: a branch push is untouched by any of this. The tip it supplies
  # is the tip it gets, and the tag list beside it changes nothing.
  run _pr_range CI_GATE_PUSH_NEW_SHA="$tip" CI_GATE_PUSH_TAG_TIPS="$published" \
                CI_GATE_PUSH_REMOTE=origin
  [ "$status" -eq 0 ]
  [ "$output" = "${published}..${tip}" ]     || { echo "a branch push range changed: '$output'" >&2; rm -rf "$sb"; return 1; }
  rm -rf "$sb"
}

# --- the suites' own hygiene ---------------------------------------------------
#
# Both of these are about the harness rather than about a check, and both exist
# because the gate could not pass its own repository: one because the suites
# inherited the gate's state, the other because a fixture spelled out a string
# the gate refuses.

@test "preflight: every check it schedules is executable in the index" {
  # run_check executes the path -- `output=$("$script" 2>&1)` -- so a check
  # committed 100644 is a lane that cannot start, and the mode git records is
  # what a runner checks out.
  #
  # Nothing here would have caught it. Git does not enforce the bit on Windows,
  # so the file is executable on the disk it was written on; the sandboxes these
  # suites build chmod +x their own stubs; and `bats ci/tests/...` never runs
  # the real script through run_check. ci/checks/typecheck-js.sh was committed
  # 100644 while every other check in that directory is 100755, and the first
  # sight of it would have been "Permission denied" on a Linux runner, from a
  # lane this branch had just finished scheduling.
  #
  # Enumerated from ci/preflight.sh rather than from the directory listing: the
  # property that matters is "scheduled and unrunnable", and a check that no
  # mode runs is a different problem with its own case.
  local _s _bad="" _n=0 _mode
  while IFS= read -r _s; do
    [ -n "$_s" ] || continue
    _n=$(( _n + 1 ))
    _mode="$(git -C "$REPO_ROOT" ls-files -s -- "$_s" | awk '{print $1}')"
    case "$_mode" in
      100755) ;;
      "") _bad="${_bad} ${_s}(not tracked)" ;;
      *)  _bad="${_bad} ${_s}(${_mode})" ;;
    esac
  done <<< "$(grep -ohE '\./ci/checks/[a-z-]+\.sh' "$REPO_ROOT/ci/preflight.sh" \
              | sed 's|^\./||' | sort -u)"

  # The selector has to still be selecting; a regex matching nothing makes the
  # assertion below vacuously true.
  [ "$_n" -ge 10 ] \
    || { echo "found only $_n scheduled checks; the selector has drifted" >&2; return 1; }
  [ -z "$_bad" ] \
    || { echo "scheduled by preflight but not executable:${_bad}" >&2; return 1; }
}

@test "tests: every gate variable the suites can inherit is cleared" {
  # ci/tests/gate_env.bash clears the gate's exported state so a case is not
  # answered by whoever invoked it. That list and the set of variables the gate
  # exports are two lists that have to agree, and nothing was checking: a
  # variable added to the gate is silently absent here, and the failure it
  # causes shows up as a case failing for a reason unrelated to what it asserts,
  # 148 of them at once, only when run under `ci/preflight.sh --mode ship`.
  #
  # Enumerated from the source rather than restated, for the same reason.
  #
  # `env NAME=` counts as well as `export NAME`. A variable placed in a child's
  # environment is inherited by that child's children too, so the spelling makes
  # no difference to what a case can pick up -- but it made all the difference
  # to this enumeration, which read only one of the two:
  #
  #   grep -rhoE 'export CI_GATE_[A-Z_]+'      ci/checks/tests-shell.sh -> []
  #   grep -rhoE '(export|env) CI_GATE_[A-Z_]+' ci/checks/tests-shell.sh
  #     -> CI_GATE_CHECK_ID
  #
  # tests-shell.sh has set CI_GATE_CHECK_ID for the whole shell suite since it
  # was written, and this case reported the two lists as agreeing throughout.
  local _v _missing=""
  while IFS= read -r _v; do
    [ -n "$_v" ] || continue
    grep -qw -- "$_v" "$REPO_ROOT/ci/tests/gate_env.bash" || _missing="${_missing} ${_v}"
  done <<< "$(grep -rhoE '(export|env) CI_GATE_[A-Z_]+' \
                "$REPO_ROOT"/ci/*.sh "$REPO_ROOT"/ci/checks/*.sh \
                "$REPO_ROOT"/ci/lib/*.sh 2>/dev/null \
              | sed -E 's/^(export|env) //' | sort -u)"
  [ -z "$_missing" ] || { echo "exported by the gate, not cleared:${_missing}" >&2; return 1; }

  # And the function does what the list says. A list nothing applies is a list.
  #
  # Driven from the same enumeration rather than from three sampled names: the
  # half above proves a variable is *mentioned* in gate_env.bash, which a
  # comment satisfies as well as an unset does. Naming three of them here left
  # the other twenty-two proven only to be spelled out somewhere in the file.
  local _left=""
  for _v in $(grep -rhoE '(export|env) CI_GATE_[A-Z_]+' \
                "$REPO_ROOT"/ci/*.sh "$REPO_ROOT"/ci/checks/*.sh \
                "$REPO_ROOT"/ci/lib/*.sh 2>/dev/null \
              | sed -E 's/^(export|env) //' | sort -u); do
    _left="${_left}$(
      export "${_v}=sentinel"
      # shellcheck source=ci/tests/gate_env.bash
      source "$REPO_ROOT/ci/tests/gate_env.bash"
      ci::tests::clear_gate_env
      [ -z "$(eval "printf '%s' \"\${${_v}:-}\"")" ] || printf ' %s' "$_v"
    )"
  done
  [ -z "$_left" ] || { echo "named in gate_env.bash but not cleared:${_left}" >&2; return 1; }
}

@test "tests: no fixture spells out a string the gate refuses to push" {
  # ci/checks/common.sh defines the secret patterns as literal text, and
  # git-safety.sh refuses a push whose additions match them. A fixture that
  # spells one out is therefore a line this repository cannot push through its
  # own gate -- which happened: an `AWS_SECRET_ACCESS_KEY` assignment written
  # out in two cases here made the commit that added them exit 20 under
  # git-safety.sh, and only the absence of an installed hook let the branch go
  # out. This comment cannot spell it either, and the first draft did, and this
  # case failed on itself -- which is the shortest demonstration available that
  # it works.
  #
  # The fixtures assemble the string at run time now (see gs_secret_line), so
  # what the scanner is handed is unchanged and what git stores does not match.
  # This case is what stops the literal spelling coming back, since it is the
  # obvious way to write the next such fixture.
  run bash -c ". '$REPO_ROOT/ci/checks/common.sh' \
                 && grep -rnE \"\$CI_CHECKS_SECRET_PATTERN\" '$REPO_ROOT/ci/tests/'"
  # 1 is grep's "no match", which is the pass. 0 means a fixture matches; 2 and
  # above mean grep could not look, and "could not look" is not "found nothing".
  [ "$status" -eq 1 ] || { echo "matches under ci/tests/:"; echo "$output"; return 1; }
}

@test "runner: a timeout it cannot use is infrastructure, not a silent fallback" {
  # The case above pins that a malformed timeout_sec is not stripped into a
  # different number. This is the half it did not ask: the awk printed its
  # diagnostic and exited 0 with no value, so submit could not tell "declares
  # none" from "declares one this runner cannot use" and fell back to the global
  # timeout, or -- with CI_GATE_TIMEOUT unset -- to no bound at all.
  #
  # The value exists precisely because the default is wrong for that check, so
  # ignoring it is not a smaller version of honouring it. A typo on the long
  # `tests-shell` blocker removes its bound and leaves full and ship validation
  # to hang, announced as an infrastructure timeout nobody configured, or not
  # announced at all.
  local cfg
  cfg="$(mktemp)"
  printf 'checks:\n  alpha:\n    timeout_sec: banana\n  gamma:\n    timeout_sec: 900\n' > "$cfg"

  # The status is the answer; the empty output is what it used to be mistaken for.
  run bash -c ". '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    CI_CHECKS_CONFIG='$cfg' ci::runner::_declared_timeout alpha 2>/dev/null"
  [ "$status" -ne 0 ]
  [ -z "$output" ]

  # A declared value is still read, and a check that declares nothing is not an
  # error -- absence and unusable are the two answers this had collapsed.
  run bash -c ". '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    CI_CHECKS_CONFIG='$cfg' ci::runner::_declared_timeout gamma 2>/dev/null"
  [ "$status" -eq 0 ]
  [ "$output" = "900" ]

  run bash -c ". '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    CI_CHECKS_CONFIG='$cfg' ci::runner::_declared_timeout delta 2>/dev/null"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  rm -f "$cfg"
}

@test "runner: a check whose declared timeout is unusable is not run at all" {
  # And submit acts on it. Recorded as FAIL_INFRA (30) -- which is what the
  # contract calls a gate that cannot do its job -- rather than launched without
  # the bound its configuration asked for.
  local sb
  sb="$(mktemp -d)"
  printf 'checks:\n  alpha:\n    timeout_sec: banana\n  gamma:\n    timeout_sec: 900\n' > "$sb/checks.yml"
  printf '#!/usr/bin/env bash\ntouch "%s/ran"\nexit 0\n' "$sb" > "$sb/check.sh"
  chmod +x "$sb/check.sh"

  run bash -c ". '$REPO_ROOT/ci/lib/common.sh' >/dev/null 2>&1; \
    . '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    export CI_CHECKS_CONFIG='$sb/checks.yml' CI_GATE_PARALLEL=1; \
    ci::runner::init 1 >/dev/null 2>&1; \
    ci::runner::submit alpha '$sb/check.sh' >/dev/null 2>&1; \
    ci::runner::wait_all >/dev/null 2>&1; \
    printf 'rc=%s\n' \"\$(ci::runner::get_result alpha)\"; \
    ci::runner::get_output alpha"
  [[ "$output" == *"rc=30"* ]]
  [[ "$output" == *"cannot use"* ]]
  # The check itself never ran, which is the point: an unbounded run of a
  # blocking lane is the outcome being refused.
  [ ! -f "$sb/ran" ]

  # The control: a well-formed timeout still submits and runs the check.
  run bash -c ". '$REPO_ROOT/ci/lib/common.sh' >/dev/null 2>&1; \
    . '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    export CI_CHECKS_CONFIG='$sb/checks.yml' CI_GATE_PARALLEL=1; \
    ci::runner::init 1 >/dev/null 2>&1; \
    ci::runner::submit gamma '$sb/check.sh' >/dev/null 2>&1; \
    ci::runner::wait_all >/dev/null 2>&1; \
    printf 'rc=%s\n' \"\$(ci::runner::get_result gamma)\""
  [[ "$output" == *"rc=0"* ]]
  [ -f "$sb/ran" ]
  rm -rf "$sb"
}

@test "git: the destination is asked what it holds, not this clone's memory of it" {
  # `refs/remotes/<remote>/*` records what this clone last *saw* the destination
  # holding, and two rules read it as what the destination *has*. The two differ
  # asymmetrically: stale-behind refuses, which is safe, but a force-push or a
  # branch deletion by another actor leaves a tracking ref still containing a
  # commit the destination has dropped -- and both rules then answer "already
  # published" about it.
  #
  # The consequence is the one the gate exists to prevent: a tag on that commit
  # republishes it while the content lanes validate the current checkout, and
  # push_range narrows past it so git-safety, the signature walk and the
  # changeset scan never look at it either.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/up"
  (
    cd "$sb/up"
    git init -q -b main .
    printf 'a\n' > a.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm c1
    printf 'secret-shaped\n' > s.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm c2
  ) >/dev/null 2>&1
  local dropped
  dropped="$(cd "$sb/up" && git rev-parse HEAD)"
  ( cd "$sb" && git clone -q "$sb/up" clone ) >/dev/null 2>&1
  # The destination drops it after this clone last looked.
  ( cd "$sb/up" && git reset -q --hard HEAD~1 ) >/dev/null 2>&1

  mkdir -p "$sb/clone/ci/lib"
  cp "$REPO_ROOT/ci/lib/git.sh" "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" \
     "$sb/clone/ci/lib/"

  # The premise: the tracking ref still reaches the dropped commit, and the
  # destination no longer does. Without this the case would pass on a clone
  # that simply never had it.
  run bash -c "cd '$sb/clone' && git merge-base --is-ancestor '$dropped' origin/main"
  [ "$status" -eq 0 ]
  run bash -c "cd '$sb/up' && git merge-base --is-ancestor '$dropped' HEAD"
  [ "$status" -ne 0 ]

  _pub() {
    ( cd "$sb/clone" && env "$@" bash -c '
        . ci/lib/log.sh 2>/dev/null
        . ci/lib/common.sh 2>/dev/null
        . ci/lib/git.sh
        ci::git::published_to_destination "'"$dropped"'" && echo YES || echo NO' )
  }

  run _pub CI_GATE_PUSH_REMOTE=origin
  [ "$output" = "NO" ] \
    || { echo "a dropped commit was reported as published" >&2; rm -rf "$sb"; return 1; }

  # The control that keeps the rule usable: a commit the destination really does
  # carry is still published, and push_range still narrows to base..tip rather
  # than walking the whole branch -- which is the refusal the tracking-ref
  # shortcut was introduced to avoid.
  local kept tip
  kept="$(cd "$sb/up" && git rev-parse HEAD)"
  ( cd "$sb/clone" && printf 'local\n' > l.txt && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm local ) >/dev/null 2>&1
  tip="$(cd "$sb/clone" && git rev-parse HEAD)"
  run bash -c "cd '$sb/clone' && CI_GATE_PUSH_REMOTE=origin bash -c '
      . ci/lib/log.sh 2>/dev/null
      . ci/lib/common.sh 2>/dev/null
      . ci/lib/git.sh
      ci::git::published_to_destination \"$kept\" && echo YES || echo NO
      CI_GATE_PUSH_NEW_SHA=$tip ci::git::push_range'"
  [[ "$output" == *"YES"* ]]
  [[ "$output" == *".."* ]] \
    || { echo "push_range stopped narrowing for a reachable destination" >&2; echo "$output" >&2; rm -rf "$sb"; return 1; }

  # Unreachable is not an answer. A remote that cannot be queried fails closed --
  # not published, and no base -- rather than falling back to the record whose
  # staleness is the whole problem.
  ( cd "$sb/clone" && git remote set-url origin "$sb/gone" ) >/dev/null 2>&1
  run _pub CI_GATE_PUSH_REMOTE=origin
  [ "$output" = "NO" ]
  run bash -c "cd '$sb/clone' && CI_GATE_PUSH_REMOTE=origin CI_GATE_PUSH_NEW_SHA=$tip bash -c '
      . ci/lib/log.sh 2>/dev/null
      . ci/lib/common.sh 2>/dev/null
      . ci/lib/git.sh
      ci::git::push_range'"
  [ "$output" = "$tip" ] \
    || { echo "an unreachable remote still produced a narrowed range: $output" >&2; rm -rf "$sb"; return 1; }

  # And the documented opt-out, so an air-gapped runner is not left with an
  # unfixable refusal. Only an explicit 1 opts out.
  run _pub CI_GATE_PUSH_REMOTE=origin CI_GATE_TRUST_TRACKING_REFS=1
  [ "$output" = "YES" ]
  rm -rf "$sb"
}

@test "preflight: a tag on an already-published commit runs destination protection only" {
  # `git tag v1.2 <commit the destination already has>; git push origin v1.2`
  # sends no tree. ci::git::worktree_covers_push accepts it on exactly that
  # ground, and then the ship dispatch ran the complete plan anyway, because
  # deletions were the only content-free case it knew -- so test-layout, node,
  # python, the build and the shell suites all ran over whatever happened to be
  # checked out, and a failure already sitting there blocked a release that
  # sends none of it.
  local sb
  sb="$(mktemp -d)"
  cp -r "$REPO_ROOT/ci" "$sb/ci"
  rm -rf "$sb/ci/tests" "$sb/ci/reports" "$sb/ci/artifacts"
  local f
  for f in "$sb"/ci/checks/*.sh; do
    printf '#!/usr/bin/env bash\necho "RAN:%s"\nexit 0\n' "$(basename "$f" .sh)" > "$f"
    chmod +x "$f"
  done
  mkdir -p "$sb/up"
  (
    cd "$sb/up"
    git init -q -b main .
    printf 'a\n' > a.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm c1
  ) >/dev/null 2>&1
  ( cd "$sb" && git init -q -b main . && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm init ) >/dev/null 2>&1
  local published unpublished
  published="$(cd "$sb" && git rev-parse HEAD)"
  ci::tests::publish "$sb" origin main "$published"
  ( cd "$sb" && printf 'later\n' > later.txt && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm later ) >/dev/null 2>&1
  unpublished="$(cd "$sb" && git rev-parse HEAD)"

  _pf_lanes() {
    ( cd "$sb" && env "$@" CI_GATE_USE_LANES=0 bash ci/preflight.sh --mode ship 2>&1 ) \
      | grep -o 'RAN:[a-z-]*' | sed 's/RAN://' | sort -u | tr '\n' ' '
  }

  # The premise: an ordinary ship push does schedule the content lanes, so an
  # empty list below is the rule acting rather than the harness failing to run.
  # Asserted on the always-run lanes rather than on `node`: the changeset filter
  # legitimately drops the language lanes for a change that touches no
  # JavaScript, so `node` absent would not tell this case anything. test-layout
  # and tests-shell are on preflight's always-run list, which is what makes
  # their absence below attributable to the rule under test.
  run _pf_lanes CI_GATE_PUSH_NEW_SHA="$unpublished" CI_GATE_PUSH_REMOTE=origin
  [[ "$output" == *test-layout* ]]
  [[ "$output" == *tests-shell* ]]
  [[ "$output" == *security* ]]

  # A tag on a commit the destination already carries, and no branch record.
  run _pf_lanes CI_GATE_PUSH_REMOTE=origin CI_GATE_PUSH_REMOTE_REFS= \
                CI_GATE_PUSH_TAG_TIPS="$published"
  [ "$status" -eq 0 ]
  [[ "$output" == *branch-protection* ]] \
    || { echo "destination protection was skipped: $output" >&2; rm -rf "$sb"; return 1; }
  [[ "$output" != *test-layout* ]] \
    || { echo "a label-only push still ran the content lanes: $output" >&2; rm -rf "$sb"; return 1; }
  [[ "$output" != *tests-shell* ]]
  [[ "$output" != *build* ]]

  # The controls, each one a reason this must NOT take the short path.
  #
  # Asserted on test-layout alone. tests-shell is changeset-filtered and
  # these rows supply no push range, so its absence would say nothing about
  # the rule; test-layout is on the always-run list, and the short path runs
  # branch-protection and nothing else -- so test-layout present is exactly
  # "the short path was not taken".
  #
  # A tag on a commit the destination does not have is carrying that commit out,
  # and the lanes over the checkout are exactly the right thing to run.
  run _pf_lanes CI_GATE_PUSH_REMOTE=origin CI_GATE_PUSH_REMOTE_REFS= \
                CI_GATE_PUSH_TAG_TIPS="$unpublished"
  [[ "$output" == *test-layout* ]]

  # A push that names a branch destination is sending a tree, whatever else is
  # in it.
  run _pf_lanes CI_GATE_PUSH_REMOTE=origin CI_GATE_PUSH_REMOTE_REFS=main \
                CI_GATE_PUSH_TAG_TIPS="$published"
  [[ "$output" == *test-layout* ]]

  # Unset is "nobody told us", which is every caller that is not the pre-push
  # hook, and none of them may be narrowed on a guess.
  run _pf_lanes CI_GATE_PUSH_REMOTE=origin CI_GATE_PUSH_TAG_TIPS="$published"
  [[ "$output" == *test-layout* ]]

  # And an unreachable destination cannot prove publication, so the short path
  # is not taken. Asserted on the banner rather than on the lane list, because
  # what happens instead is stricter than "run everything": worktree_covers_push
  # cannot verify the tag either, so preflight refuses the push outright and no
  # lane runs at all. An assertion looking for test-layout would fail on that --
  # for the opposite of the reason it was written -- which is what my first
  # draft did.
  ( cd "$sb" && git remote set-url origin "$sb/gone" ) >/dev/null 2>&1
  run bash -c "cd '$sb' && CI_GATE_USE_LANES=0 CI_GATE_PUSH_REMOTE=origin \
      CI_GATE_PUSH_REMOTE_REFS= CI_GATE_PUSH_TAG_TIPS='$published' \
      bash ci/preflight.sh --mode ship 2>&1"
  [[ "$output" != *"publishes only refs the destination already carries"* ]] \
    || { echo "an unreachable destination took the short path" >&2; echo "$output" >&2; rm -rf "$sb"; return 1; }
  rm -rf "$sb"
}

@test "preflight: the standalone typecheck lane runs" {
  # ci/checks/typecheck.sh was registered in ci/checks/manifest.yml with
  # `severity: blocker` and scheduled by nothing: `typecheck-js` survived only
  # as a changeset id that the reverse mapping folds back into the combined
  # `node` lane, so the file never executed in quick, full or ship. Registered
  # at one layer and run at none.
  #
  # This case exists for a second reason, and it is the load-bearing one. The
  # `-p/--project` rule in ci/checks/node.sh accepts a typecheck script that
  # names one of several discovered projects, and the justification for
  # accepting it is that this lane compiles the rest. That justification was
  # written while this lane did not run, which made it false. If the lane is
  # ever taken back out of the plan, this case fails and that rule has to change
  # with it -- the two are one decision.
  local sb
  sb="$(mktemp -d)"
  cp -r "$REPO_ROOT/ci" "$sb/ci"
  rm -rf "$sb/ci/tests" "$sb/ci/reports" "$sb/ci/artifacts"
  local f
  for f in "$sb"/ci/checks/*.sh; do
    printf '#!/usr/bin/env bash\necho "RAN:%s"\nexit 0\n' "$(basename "$f" .sh)" > "$f"
    chmod +x "$f"
  done
  mkdir -p "$sb/frontend/src"
  printf '{ "name": "f" }\n' > "$sb/frontend/package.json"
  printf '# r\n' > "$sb/README.md"
  printf 'export const a = 1;\n' > "$sb/frontend/src/a.ts"
  (
    cd "$sb"
    git init -q -b main .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
  ) >/dev/null 2>&1
  local base
  base="$(cd "$sb" && git rev-parse HEAD)"

  _tc_lanes() {
    ( cd "$sb" && env "$@" CI_GATE_USE_LANES=0 bash ci/preflight.sh --mode ship 2>&1 ) \
      | grep -o 'RAN:[a-z-]*' | sed 's/RAN://' | sort -u | tr '\n' ' '
  }

  # A JavaScript change schedules it.
  ( cd "$sb" && printf 'export const b = 2;\n' > frontend/src/b.ts && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm js ) >/dev/null 2>&1
  local js_tip
  js_tip="$(cd "$sb" && git rev-parse HEAD)"
  run _tc_lanes CI_GATE_PUSH_OLD_SHA="$base" CI_GATE_PUSH_NEW_SHA="$js_tip" \
                CI_GATE_PUSH_REMOTE_REFS=main
  [[ "$output" == *typecheck* ]] \
    || { echo "the typecheck lane was not scheduled for a JS change: $output" >&2; rm -rf "$sb"; return 1; }
  # And beside the node lane, not instead of it: they answer different
  # questions -- node runs the workspace's own script, this compiles every
  # discovered project by name.
  [[ "$output" == *node* ]]

  # The control: it is a language lane, so a change touching no JavaScript
  # still filters it out. Without this the case would be satisfied by wiring it
  # into the always-run list, which is a different and worse thing.
  ( cd "$sb" && printf '# r2\n' > README.md && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm docs ) >/dev/null 2>&1
  run _tc_lanes CI_GATE_PUSH_OLD_SHA="$js_tip" \
                CI_GATE_PUSH_NEW_SHA="$(cd "$sb" && git rev-parse HEAD)" \
                CI_GATE_PUSH_REMOTE_REFS=main
  [[ "$output" != *typecheck* ]]
  [[ "$output" != *node* ]]
  rm -rf "$sb"
}

@test "tests-shell: a local cache under ci/ is not shell-suite drift" {
  # The ignored-file half of the drift scan pruned by name -- `.ci-gate`,
  # `ci/reports/`, `ci/artifacts/` -- and each of those was added after the gate
  # blocked itself on its own output. The list was still short by every cache
  # this repository's own tooling writes under ci/, and any one of them exits 20
  # before a single suite runs, on a push whose commits and suite files are
  # unchanged.
  #
  # A deny-list has to name every generated directory that will ever exist. What
  # the suites read is a closed set, so the filter names that instead.
  local flt='(^|/)(\.ci-gate|node_modules)/|^ci/(reports|artifacts)/'
  local keep='\.(sh|bats|bash)$|(^|/)\.gitignore$|(^|/)\.githooks/|(^|/)README\.md$'

  # The filter is taken from the lane rather than restated, or this case pins a
  # copy of the rule and not the rule.
  grep -qF -- "$keep" "$REPO_ROOT/ci/checks/tests-shell.sh" \
    || { echo "the lane no longer uses this filter; update this case" >&2; return 1; }

  _drift_reports() {
    printf '%s\n' "$1" | grep -Ev "$flt" | grep -E "$keep"
  }

  local noise
  for noise in "ci/__pycache__/x.pyc" "ci/lib/__pycache__/y.pyc" "ci/.ruff_cache/z" \
               "ci/tmp/local.txt" "ci/tests/.pytest_cache/w" "ci/reports/junit.xml" \
               "ci/.ci-gate/state"; do
    run _drift_reports "$noise"
    [ -z "$output" ] \
      || { echo "a path the suites never read was reported as drift: $noise" >&2; return 1; }
  done

  # The control, and it is what the ignored half exists for: a commit that
  # deletes a bats file and ignores its path leaves the worktree replacement
  # invisible to the tracked and untracked lists, and this is the scan that can
  # still see it. Dropping caches must not drop that.
  local real
  for real in "ci/tests/test_hooks.bats" "ci/lib/git.sh" "ci/checks/node.sh" \
              ".githooks/pre-push" ".gitignore" "frontend/README.md" "ci/lib/helper.bash"; do
    run _drift_reports "$real"
    [ -n "$output" ] \
      || { echo "a file the suites do read was dropped from the scan: $real" >&2; return 1; }
  done
}

@test "js lane: the root-manifest branch checks HEAD children too" {
  # The missing-workspace guard sat at the end of the walk, and the
  # root-manifest branch returns above it. So a pushed tree carrying a root
  # package.json beside packages/app/package.json, with packages/app/ missing
  # locally, emitted `.` and never reached the guard -- the node, tests and
  # typecheck lanes then ran the root alone and the pushed child's scripts were
  # skipped. The same false pass the guard exists to stop, by the one path that
  # returns before it.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/packages/app"
  (
    cd "$sb"
    git init -q -b main .
    git config user.email t@t && git config user.name t
    printf '{"name":"root","private":true,"workspaces":["packages/*"]}\n' > package.json
    printf '{"name":"app"}\n' > packages/app/package.json
    printf 'x\n' > a.txt
    git add -A && git commit -qm c1
  ) >/dev/null 2>&1

  # The premise: with both present this is the nested-workspace ambiguity, which
  # is a different refusal and proves the fixture really has a root and a child.
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' \
                 && CI_GATE_MODE=ship ci::common::node_workspaces package.json 2>&1"
  [[ "$output" == *"nested one(s)"* ]] \
    || { echo "fixture is not a root-plus-child layout: $output" >&2; rm -rf "$sb"; return 1; }

  ( cd "$sb" && rm -rf packages/app )
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' \
                 && CI_GATE_MODE=ship ci::common::node_workspaces package.json 2>/dev/null"
  [ "$status" -ne 0 ] \
    || { echo "the root branch answered '$output' for a push whose child is missing" >&2; rm -rf "$sb"; return 1; }
  [ -z "$output" ] \
    || { echo "emitted a workspace list for a tree it cannot check: $output" >&2; rm -rf "$sb"; return 1; }

  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' \
                 && CI_GATE_MODE=ship ci::common::node_workspaces package.json 2>&1 1>/dev/null"
  [[ "$output" == *"packages/app/package.json"* ]] \
    || { echo "the refusal does not name the missing child: $output" >&2; rm -rf "$sb"; return 1; }
  rm -rf "$sb"
}
