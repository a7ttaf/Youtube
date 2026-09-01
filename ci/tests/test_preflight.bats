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
  GS_SB="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  # The library helpers those functions call. preflight.sh sources
  # ci/lib/common.sh before any of them runs, so an extraction that carries only
  # the preflight half leaves the case measuring "command not found" -- read as a
  # decision by the very branches under test -- instead of the code it is about.
  # Every temporary file in the gate goes through mktemp_file now, so that is the
  # one that has to travel with them.
  for fn in ci::common::mktemp_file ci::common::mktemp_dir; do
    sed -n "/^${fn}() {/,/^}/p" "$REPO_ROOT/ci/lib/common.sh" >> "$out"
    grep -q "^${fn}() {" "$out" || {
      echo "no such function in ci/lib/common.sh: ${fn}" >&2
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
  _pf_fns "$fns" _check_should_skip _checks_config_resolve _check_disabled_in_config \
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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

@test "tests-shell: a Makefile the suites read is in scope both ways" {
  # Two halves of one rule, and the suites are the reason for both:
  # ci/tests/test_preflight.bats asserts that the `bats-install` target exists
  # and matches the refusal message this lane prints -- against the *worktree*
  # Makefile, since that is the file bats opens.
  #
  # Half one, the ship comparison. A push whose HEAD carries a broken Makefile,
  # with an unstaged local repair, ran that assertion against the repair and
  # approved a commit that fails its own provisioning check. The comparison scope
  # and what the suites read have to be the same set.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks" "$sb/ci/tests"
  cp "$REPO_ROOT/ci/checks/tests-shell.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf '@test "x" { true; }\n' > "$sb/ci/tests/t.bats"
  printf 'nothing\n' > "$sb/.gitignore"
  printf 'bats-install:\n\t@echo broken\n' > "$sb/Makefile"
  (
    cd "$sb"
    git init -q -b feature/x .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
  ) >/dev/null 2>&1
  # The unstaged repair: on disk, not in the commit being pushed.
  printf 'bats-install:\n\t@bash ci/scripts/install-bats.sh\n' > "$sb/Makefile"

  run bash -c "cd '$sb' && CI_GATE_MODE=ship bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 20 ] || { rm -rf "$sb"; echo "$output" >&2; return 1; }
  [[ "$output" == *"Makefile"* ]] || { rm -rf "$sb"; echo "$output" >&2; return 1; }
  rm -rf "$sb"

  # Half two, the scheduler. `Makefile` classifies as the language `make`, which
  # no lane maps, so a commit touching only it scheduled nothing at all -- and
  # the provisioning assertion above is in the suite that would not have run.
  # Pinned to the expression rather than driven through a whole changeset: what
  # has to hold is that this path forces the lane, and the four entries beside it
  # are asserted with it so a rewrite cannot quietly drop one.
  local expr
  expr="$(grep -oE "\(\^\|\[\[:space:\]\]\)\([^']*" "$REPO_ROOT/ci/preflight.sh" | head -1)"
  [ -n "$expr" ] || { echo "could not lift the tests-shell path exception" >&2; return 1; }
  local p
  for p in 'Makefile' 'ci/checks/node.sh' '.githooks/pre-push' '.gitignore' 'frontend/README.md'; do
    printf 'M\t%s\n' "$p" | grep -qE "$expr" \
      || { echo "this path no longer forces tests-shell: ${p}" >&2; return 1; }
  done
  # And a path that must not force it, or the exception is not an exception.
  for p in 'backend/app/main.py' 'frontend/src/app.ts' 'Makefile.bak'; do
    printf 'M\t%s\n' "$p" | grep -qE "$expr" \
      && { echo "an unrelated path forces tests-shell: ${p}" >&2; return 1; }
  done
  return 0
}

@test "tests-shell: a deleted .gitignore replaced only on disk is drift" {
  # Three scopes for one question is how they came to disagree: the tracked
  # scan covered .gitignore, the untracked and ignored scans covered ci and
  # .githooks alone. A commit deleting .gitignore while a worktree copy stays
  # behind was therefore invisible to all three -- the tracked scan sees the
  # deletion, and the scan that would have seen the replacement was not looking
  # there. The suites then ran against rules the push removes.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  # `_checks_config_resolve` too: the reader asks it which copy of checks.yml to
  # parse -- the worktree's outside ship mode, HEAD's inside it -- so lifting the
  # parser without it leaves the parser calling a function that is not there.
  # This case is about the parse and drives it in a non-ship mode, where the
  # answer is the worktree file it always was.
  local fns="$BATS_TEST_TMPDIR/cfg.sh"
  _pf_fns "$fns" _checks_config_resolve _check_disabled_in_config
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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

@test "runner: init keeps the EXIT trap its caller already installed" {
  # `trap ... EXIT` replaces rather than adds, and ci::runner::init's own comment
  # claimed a merge it never performed. ci/preflight.sh installs an EXIT trap
  # before the first run_phase -- the one that removes the HEAD copy of
  # ci/config/checks.yml materialized for a ship run -- so every ship validation
  # leaked one temporary file, silently and forever.
  local sb; sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  run bash -c "
    set -Eeuo pipefail
    . '$REPO_ROOT/ci/lib/runner.sh'
    printf 'x' > '$sb/caller.tmp'
    trap 'rm -f \"$sb/caller.tmp\" ; echo CALLER_TRAP_RAN' EXIT
    ci::runner::init 2 >/dev/null 2>&1
    exit 0"
  [ "$status" -eq 0 ] || { echo "$output" >&2; rm -rf "$sb"; false; }
  [[ "$output" == *"CALLER_TRAP_RAN"* ]] || { echo "$output" >&2; rm -rf "$sb"; false; }
  [ ! -e "$sb/caller.tmp" ] || { echo "the caller's cleanup did not run" >&2; rm -rf "$sb"; false; }

  # Twice, because the obvious guard against chaining this function onto itself
  # -- clearing the whole previous command when it mentions the cleanup -- drops
  # the caller's handler on the second call while preserving it on the first.
  # That is the same leak one call later, so the repeat is the case that matters.
  printf 'x' > "$sb/caller.tmp"
  run bash -c "
    set -Eeuo pipefail
    . '$REPO_ROOT/ci/lib/runner.sh'
    trap 'rm -f \"$sb/caller.tmp\" ; echo CALLER_TRAP_RAN' EXIT
    ci::runner::init 2 >/dev/null 2>&1
    ci::runner::init 2 >/dev/null 2>&1
    trap -p EXIT
    exit 0"
  [ "$status" -eq 0 ] || { echo "$output" >&2; rm -rf "$sb"; false; }
  [[ "$output" == *"CALLER_TRAP_RAN"* ]] || { echo "$output" >&2; rm -rf "$sb"; false; }
  [ ! -e "$sb/caller.tmp" ] || { echo "the caller's cleanup did not run twice" >&2; rm -rf "$sb"; false; }
  # And the handler does not grow a copy of the cleanup per call.
  local _copies
  _copies="$(printf '%s\n' "$output" | grep -c 'ci::runner::_cleanup' || true)"
  [ "$_copies" -eq 1 ] || { echo "cleanup appears ${_copies} times: $output" >&2; rm -rf "$sb"; false; }

  # The trap argument itself stays a fixed string. Splicing the previous handler
  # into it -- `trap "ci::runner::_cleanup ; ${cmd}" EXIT`, which is how this was
  # written first -- expands at *declaration* time, so the argument differs per
  # call and a reader has to verify the expansion every time. The chain lives in
  # the handler now, which is also where a trap's variable part belongs: read
  # when the signal arrives.
  ! grep -nE '^[[:space:]]*trap +"' "$REPO_ROOT/ci/lib/runner.sh" \
    || { echo "a trap argument in runner.sh interpolates at declaration time" >&2; rm -rf "$sb"; return 1; }

  # The runner's own state is still removed -- chaining must not cost the thing
  # the trap was installed for.
  run bash -c "
    set -Eeuo pipefail
    . '$REPO_ROOT/ci/lib/runner.sh'
    trap 'echo CALLER_TRAP_RAN' EXIT
    ci::runner::init 2 >/dev/null 2>&1
    echo \"JOBS=\$_CI_RUNNER_JOBS_DIR\"
    exit 0"
  local _jobs
  _jobs="$(printf '%s\n' "$output" | sed -n 's/^JOBS=//p')"
  [ -n "$_jobs" ] || { echo "$output" >&2; rm -rf "$sb"; false; }
  [ ! -d "$_jobs" ] || { echo "the runner's jobs dir survived: $_jobs" >&2; rm -rf "$sb"; false; }
  rm -rf "$sb"
}

@test "common: no bare mktemp survives in the gate or its suites" {
  # `mktemp` with no operand is a GNU extension. BSD mktemp -- what macOS ships,
  # and macOS is the platform whose Bash 3.2 floor this gate is written against
  # -- documents `template ...` as required and exits with a usage error without
  # one. Every bare call failed there, and the lanes reported that failure
  # correctly and uselessly: ci/checks/test-layout.sh runs this on every
  # scheduled quick, full and ship run, so it exited FAIL_INFRA before inspecting
  # a single test, on every run, on a supported platform.
  #
  # ci/scripts/install-bats.sh already carried this reasoning for its own
  # `mktemp -d`. It is a property of the tool rather than of that script, which
  # is why the rule is repo-wide and why it is asserted rather than remembered.
  #
  # Matched only where the tool is actually invoked -- inside a command
  # substitution -- so the word appearing in a comment or in an error message is
  # not a finding, and `ci::common::mktemp_file` is not one either. This comment
  # deliberately does not spell the substitution out: the scan reads this file
  # too, and prose that quotes the pattern is a finding about itself.
  # Extracted first and filtered second, rather than written as one expression:
  # an optional ` -d` inside the pattern can always match empty, so a single
  # regex reports the templated `-d` form as bare by skipping the flag it was
  # meant to consume. Two passes cannot do that.
  local bare
  bare="$(grep -rnoE '\$\(mktemp[^)]{0,14}' \
            --include='*.sh' --include='*.bats' --include='*.bash' ci \
          | grep -vE 'mktemp (-d )?"' || true)"
  [ -z "$bare" ] || { echo "mktemp called without a template:"$'\n'"$bare" >&2; return 1; }

  # And the helper the lanes use has to actually produce a file, under the
  # directory the platform names rather than a hard-coded /tmp.
  local f d
  f="$(cd "$REPO_ROOT" && . ci/lib/common.sh >/dev/null 2>&1 && ci::common::mktemp_file probe)"
  [ -f "$f" ] || { echo "mktemp_file produced no file: [$f]" >&2; return 1; }
  case "$f" in
    "${TMPDIR:-/tmp}"/ums-probe.*) ;;
    *) rm -f "$f"; echo "mktemp_file ignored TMPDIR: $f" >&2; return 1 ;;
  esac
  rm -f "$f"

  d="$(cd "$REPO_ROOT" && . ci/lib/common.sh >/dev/null 2>&1 && ci::common::mktemp_dir probe)"
  [ -d "$d" ] || { echo "mktemp_dir produced no directory: [$d]" >&2; return 1; }
  rmdir "$d"

  # Two calls must not collide -- the six X's are what make the name unique, and
  # a template without them is a fixed path two concurrent lanes would share.
  local a b
  a="$(cd "$REPO_ROOT" && . ci/lib/common.sh >/dev/null 2>&1 && ci::common::mktemp_file probe)"
  b="$(cd "$REPO_ROOT" && . ci/lib/common.sh >/dev/null 2>&1 && ci::common::mktemp_file probe)"
  [ "$a" != "$b" ] || { rm -f "$a"; echo "two calls returned the same path: $a" >&2; return 1; }
  rm -f "$a" "$b"
}

@test "hook: the destination tips query matches the one git.sh falls back to" {
  # Two copies of one query, because ci/lib/git.sh is deliberately standalone --
  # it reads no ci::common:: helper -- and ci/hook-dispatch.sh sources common.sh
  # and log.sh only, so there is no file they can share a function through. What
  # can be shared is this assertion: they are compared to each other rather than
  # each to a string written twice here.
  #
  # An asymmetry would not be a missed case but an inverted one. The dispatcher's
  # answer is exported and preferred; git.sh's runs only when it was not, so the
  # two disagreeing means the same push is judged published or unpublished
  # depending on whether it went through the hook.
  local q_git q_hook
  q_git="$(grep -o "awk '\$2 .*sort -u" ci/lib/git.sh | head -1)"
  q_hook="$(grep -o "awk '\$2 .*sort -u" ci/hook-dispatch.sh | head -1)"
  [ -n "$q_git" ]  || { echo "could not find the query in ci/lib/git.sh" >&2; return 1; }
  [ -n "$q_hook" ] || { echo "could not find the query in ci/hook-dispatch.sh" >&2; return 1; }
  [ "$q_git" = "$q_hook" ] \
    || { echo "the two queries differ:"$'\n'"  git.sh:  $q_git"$'\n'"  hook:    $q_hook" >&2; return 1; }
  # Both must query every namespace, so the assertion above cannot be satisfied
  # by two identically *narrow* copies.
  grep -qF 'git ls-remote "$remote"' ci/lib/git.sh \
    || { echo "ci/lib/git.sh no longer queries every destination ref" >&2; return 1; }
  grep -qF 'git ls-remote "$CI_GATE_PUSH_REMOTE"' ci/hook-dispatch.sh \
    || { echo "ci/hook-dispatch.sh no longer queries every destination ref" >&2; return 1; }

  # Neither may go back to limiting the query to two namespaces: that is the
  # defect, not a detail of it. A destination reaching a commit only through
  # `refs/publish/prod` read as a destination that does not have it.
  ! grep -qF 'ls-remote --heads --tags' ci/lib/git.sh ci/hook-dispatch.sh \
    || { echo "a namespace-limited ls-remote is back" >&2; return 1; }

  # And the filter has to exclude the forge's proposal mirrors, which is the one
  # fail-open direction in widening the query: a commit reachable only from
  # refs/pull/* is proposed, not merged, and counting it as published lets
  # push_is_label_only skip every content lane for a tag push on it.
  local sample kept
  sample="$(printf '%s\n' \
    'aaaaaaa1	refs/heads/main' \
    'aaaaaaa2	refs/tags/v1.0' \
    'aaaaaaa3	refs/tags/v1.0^{}' \
    'aaaaaaa4	refs/publish/prod' \
    'aaaaaaa5	refs/notes/commits' \
    'aaaaaaa6	HEAD' \
    'bbbbbbb1	refs/pull/7/head' \
    'bbbbbbb2	refs/pull/7/merge' \
    'bbbbbbb3	refs/merge-requests/9/head' \
    'bbbbbbb4	refs/changes/34/1234/1' \
    'bbbbbbb5	refs/changes/34/1234/meta')"
  # Through the pipeline lifted from the lane above, not a copy of it written
  # here: a transcription is a third place the namespace list can drift, and the
  # Gerrit namespace arrived by being missing from exactly that kind of copy.
  kept="$(printf '%s\n' "$sample" | eval "$q_git" | tr '\n' ' ')"
  local want
  for want in aaaaaaa1 aaaaaaa2 aaaaaaa3 aaaaaaa4 aaaaaaa5 aaaaaaa6; do
    [[ "$kept" == *"$want"* ]] || { echo "a published ref was dropped: ${want} (got [$kept])" >&2; return 1; }
  done
  local unwanted
  # Gerrit's `refs/changes/*` is the sharpest of the three and arrived a round
  # after the other two: there every change lives in that namespace until it is
  # submitted, so on a Gerrit remote counting them as published is not an edge
  # case but the normal state of unmerged work.
  for unwanted in bbbbbbb1 bbbbbbb2 bbbbbbb3 bbbbbbb4 bbbbbbb5; do
    [[ "$kept" != *"$unwanted"* ]] || { echo "a proposal ref counted as published: ${unwanted}" >&2; return 1; }
  done
}

@test "runner: a malformed timeout_sec is rejected, not stripped into a number" {
  # `gsub(/[^0-9]/, "")` deleted the non-digits and joined what was left, so
  # `1e3` became 13 and `-1` became 1. The runner then killed a blocking check
  # seconds in and reported an infrastructure timeout the configuration never
  # asked for -- a lane that does not finish is a lane that does not run.
  local cfg
  cfg="$(mktemp "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
    # The library helper the extracted block reads through. git-safety.sh sources
    # ci/lib/common.sh before it, so leaving it out makes the collector fail on
    # "command not found" and the case would assert the dedup against a block
    # that never ran.
    sed -n '/^ci::common::mktemp_file() {/,/^}/p' "$REPO_ROOT/ci/lib/common.sh"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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

@test "tests-shell: an ignored suite with a non-ASCII name is still drift" {
  # The same quoting fault as the workspace drift scan, in the reader that
  # decides whether bats is about to run coverage the push does not carry. git
  # renders `ci/tests/café.bats` as `"ci/tests/caf\303\251.bats"` -- ending in a
  # quote character rather than in `.bats` -- so the suffix filter dropped it and
  # the scan reported nothing. A commit that deletes and ignores that suite while
  # the local copy stays then leaves the suites validating themselves against a
  # tree that does not contain them.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib" "$sb/ci/tests" "$sb/.githooks"
  cp "$REPO_ROOT/ci/checks/tests-shell.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf '#!/usr/bin/env bats\n@test "x" { true; }\n' > "$sb/ci/tests/t.bats"
  printf '#!/usr/bin/env bats\n@test "y" { true; }\n' > "$sb/ci/tests/café.bats"
  printf '#!/usr/bin/env bash\ntrue\n' > "$sb/.githooks/pre-push"
  printf 'nothing\n' > "$sb/.gitignore"
  (
    cd "$sb"
    git init -q -b main .
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm init
    git rm -q "ci/tests/café.bats"
    printf 'nothing\nci/tests/café.bats\n' > .gitignore
    git add .gitignore
    git -c user.email=t@t -c user.name=t commit -qm 'delete and ignore the suite'
    printf '#!/usr/bin/env bats\n@test "shadow" { true; }\n' > "ci/tests/café.bats"
  ) >/dev/null 2>&1

  if [ ! -f "$sb/ci/tests/café.bats" ]; then
    rm -rf "$sb"; skip "this filesystem cannot hold a non-ASCII path"
  fi
  local quoted
  quoted="$( cd "$sb" && git ls-files --others --ignored --exclude-standard -- ci )"
  [[ "$quoted" == *'\303\251'* ]] \
    || { rm -rf "$sb"; skip "git is not quoting here (core.quotepath off); the case cannot show the fault"; }

  run bash -c "cd '$sb' && CI_GATE_MODE=ship bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -ne 0 ] \
    || { rm -rf "$sb"; echo "the quoted shadow was not reported: $output" >&2; return 1; }
  [[ "$output" == *"caf"* ]] \
    || { rm -rf "$sb"; echo "something else refused, not the shadow: $output" >&2; return 1; }
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  cfg="$(mktemp "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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

@test "git-safety: a path holding a glob character is deduplicated as text" {
  # The seen-set is a `case` membership test, and a `case` pattern is a glob --
  # so if the path were interpolated unquoted, an earlier `docs/a0bin` would
  # make a later `docs/a?bin` look already seen, the second file would never be
  # added, and a >5MB artifact could slip the size block by putting a pattern
  # character in its name. The path is quoted inside the pattern, which makes it
  # literal; this pins that, because the quotes are easy to lose in an edit.
  local lf p1 p2 seen
  lf=$'\n'
  p1='docs/a0bin'
  p2='docs/a?bin'
  seen="${lf}${p1}${lf}"

  # The real spelling from ci/checks/git-safety.sh: quoted, therefore literal.
  case "$seen" in
    *"${lf}${p2}${lf}"*) echo "quoted pattern matched a different path" >&2 ; return 1 ;;
  esac

  # The control that proves this case can tell the difference: unquoted, the
  # same comparison does match, which is the bug being guarded against.
  local matched=0
  # shellcheck disable=SC2295
  case "$seen" in
    *${lf}${p2}${lf}*) matched=1 ;;
  esac
  [ "$matched" -eq 1 ] || { echo "control did not match; case is not discriminating" >&2 ; return 1; }

  # And the same for `*`, the other metacharacter a filename may legally carry.
  case "$seen" in
    *"${lf}docs/a*bin${lf}"*) echo "quoted star pattern matched" >&2 ; return 1 ;;
  esac

  # An identical path must still be recognised, or the guard would size every
  # file twice rather than once.
  case "$seen" in
    *"${lf}${p1}${lf}"*) ;;
    *) echo "an identical path was not recognised as seen" >&2 ; return 1 ;;
  esac

  # The line under test still carries the quotes.
  grep -q 'case "\$_gs_seen" in \*"\${_gs_lf}\${path}\${_gs_lf}"\*' \
    "$REPO_ROOT/ci/checks/git-safety.sh"
}

@test "runner: a timeout this host cannot enforce is refused, not dropped" {
  # The case above covers a timeout the *configuration* cannot express. This is
  # the other half: the value is fine, and the host has neither `timeout` nor
  # `gtimeout` to apply it. That left timeout_cmd empty and the check ran with
  # no deadline at all -- so a hung blocker stalls preflight for as long as the
  # caller will wait, under a configuration that explicitly asked for a bound.
  # A stock macOS box is exactly this host until coreutils is installed.
  run bash -c ". '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    out=\"\$(PATH=/nonexistent-dir ci::runner::_timeout_cmd 900)\" && st=0 || st=\$?; \
    printf 'st=%s out=[%s]\n' \"\$st\" \"\$out\""
  [[ "$output" == *"st=1"* ]]
  [[ "$output" == *"out=[]"* ]]

  # And submit acts on that refusal rather than launching unbounded.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  printf '#!/usr/bin/env bash\ntouch "%s/ran"\nexit 0\n' "$sb" > "$sb/check.sh"
  chmod +x "$sb/check.sh"

  # PATH is narrowed for the submit call only: init and the readers still need a
  # working environment, and the decision under test is made inside submit.
  run bash -c ". '$REPO_ROOT/ci/lib/common.sh' >/dev/null 2>&1; \
    . '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
    export CI_CHECKS_CONFIG='$sb/absent.yml' CI_GATE_PARALLEL=1 CI_GATE_TIMEOUT=900; \
    ci::runner::init 1 >/dev/null 2>&1; \
    PATH=/nonexistent-dir ci::runner::submit alpha '$sb/check.sh' >/dev/null 2>&1; \
    ci::runner::wait_all >/dev/null 2>&1; \
    printf 'rc=%s\n' \"\$(ci::runner::get_result alpha)\"; \
    ci::runner::get_output alpha"
  [[ "$output" == *"rc=30"* ]]
  [[ "$output" == *"cannot enforce"* ]]
  # The point: an unbounded run of a blocking lane is what is being refused.
  [ ! -f "$sb/ran" ]

  # The control: with a usable timeout utility the same check submits and runs,
  # so the refusal above is about the missing utility and nothing else.
  if command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1; then
    run bash -c ". '$REPO_ROOT/ci/lib/common.sh' >/dev/null 2>&1; \
      . '$REPO_ROOT/ci/lib/runner.sh' >/dev/null 2>&1; \
      export CI_CHECKS_CONFIG='$sb/absent.yml' CI_GATE_PARALLEL=1 CI_GATE_TIMEOUT=900; \
      ci::runner::init 1 >/dev/null 2>&1; \
      ci::runner::submit alpha '$sb/check.sh' >/dev/null 2>&1; \
      ci::runner::wait_all >/dev/null 2>&1; \
      printf 'rc=%s\n' \"\$(ci::runner::get_result alpha)\""
    [[ "$output" == *"rc=0"* ]]
    [ -f "$sb/ran" ]
  fi
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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

  # In quick as well, and this half was missing while the case read as though it
  # covered the rule. The `-p` acceptance in ci/checks/node.sh is not
  # mode-aware: it accepts a script naming one of several projects in every
  # mode, on the strength of this lane compiling the rest. The lane was
  # scheduled only from run_full_or_ship_checks, so in quick the acceptance
  # stood while the thing making it true never ran --
  #
  #   --mode quick  ->  git-safety node test-layout
  #   --mode ship   ->  ... node ... typecheck-js
  #
  # -- and a type error in the project the script does not name passed the
  # pre-commit gate. Asserting the rule in one mode is how that survived.
  # Staged, never committed, and put back afterwards. quick reads the index, so
  # nothing here needs a commit -- and the ship control at the end of this case
  # measures the range from `js_tip`, so a commit added in the middle would
  # silently turn its docs-only range into one carrying JavaScript. That is what
  # the first draft of this block did, and the control failed for a reason that
  # had nothing to do with what it tests.
  _tc_lanes_quick() {
    ( cd "$sb" && env "$@" CI_GATE_USE_LANES=0 bash ci/preflight.sh --mode quick 2>&1 ) \
      | grep -o 'RAN:[a-z-]*' | sed 's/RAN://' | sort -u | tr '\n' ' '
  }
  ( cd "$sb" && printf 'export const q = 3;\n' > frontend/src/q.ts \
    && git add frontend/src/q.ts ) >/dev/null 2>&1
  run _tc_lanes_quick
  [[ "$output" == *typecheck* ]] \
    || { echo "quick did not schedule the typecheck lane for a staged JS change: $output" >&2; rm -rf "$sb"; return 1; }
  [[ "$output" == *node* ]] \
    || { echo "quick lost the node lane: $output" >&2; rm -rf "$sb"; return 1; }
  ( cd "$sb" && git rm -q --cached frontend/src/q.ts && rm -f frontend/src/q.ts ) >/dev/null 2>&1

  # And its cost stays bounded by the changeset the way every other language
  # lane's does, or scheduling it in quick would tax every commit in the repo.
  #
  # Staged by name, not `git add -A`: the preflight runs above write their own
  # output under ci/reports/, and `-A` sweeps that in, so the "docs-only" change
  # would not be docs-only.
  ( cd "$sb" && printf '# doc\n' > README.md && git add README.md ) >/dev/null 2>&1
  # The premise, or "no typecheck lane" proves nothing.
  run bash -c "cd '$sb' && git diff --cached --name-only"
  [ "$output" = "README.md" ] \
    || { echo "fixture staged more than the doc: $output" >&2; rm -rf "$sb"; return 1; }
  run _tc_lanes_quick
  [[ "$output" != *typecheck* ]] \
    || { echo "a docs-only change scheduled the typecheck lane: $output" >&2; rm -rf "$sb"; return 1; }
  ( cd "$sb" && git checkout -q -- README.md && git reset -q ) >/dev/null 2>&1

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
  #
  # The prune is written by *shape* rather than by name: `__pycache__` and any
  # dot-directory carrying `cache` cover the four that had to be added one at a
  # time, and the one a tool adds next week. That is what lets the keep-list
  # below be the real input set -- these suites read the configuration beside
  # them, not only shell and bats, and while it was suffix-filtered a commit
  # that deleted ci/config/affected.yml and ignored the path left a worktree
  # copy no scan could see.
  local flt='(^|/)(\.ci-gate|node_modules|__pycache__|\.venv|\.[^/]*cache[^/]*)/|^ci/(reports|artifacts)/'
  local suffixes='\.(sh|bats|bash|yml|yaml|conf|toml|json|py|js|cjs|mjs|ts|go|mod|md)$'

  # The extensionless half is lifted from the lane, not restated here. It used to
  # be a literal `(\.gitignore|VERSION)` in both places, and pinning the spelling
  # meant that naming those basenames once -- which is what stopped SHELL_INPUTS
  # and this filter from drifting apart, after `Makefile` reached one and not the
  # other -- read as the rule changing. What has to hold is that this case tests
  # the lane's rule, not that the lane spells it a particular way.
  local basenames
  basenames="$(grep -m1 '^  SHELL_INPUT_BASENAMES=' "$REPO_ROOT/ci/checks/tests-shell.sh" | cut -d"'" -f2)"
  [ -n "$basenames" ] \
    || { echo "the lane no longer names its extensionless inputs; update this case" >&2; return 1; }

  # Every extensionless entry in SHELL_INPUTS has to appear in it, and that is the
  # coupling that was missing: the scope named `Makefile` and the filter did not,
  # so an ignored replacement for a deleted Makefile was invisible to all three
  # scans at once.
  local want
  for want in '\.gitignore' 'Makefile' 'VERSION'; do
    case "|$basenames|" in
      *"|$want|"*) ;;
      *) echo "an extensionless gate input is missing from the filter: $want" >&2; return 1 ;;
    esac
  done

  local keep="${suffixes}|(^|/)(${basenames})\$|(^|/)\.githooks/"

  # Both halves are still taken from the lane rather than restated, or this case
  # pins a copy of the rule and not the rule.
  grep -qF -- "$suffixes" "$REPO_ROOT/ci/checks/tests-shell.sh" \
    || { echo "the lane no longer uses this suffix set; update this case" >&2; return 1; }
  grep -qF -- 'SHELL_INPUT_BASENAMES' "$REPO_ROOT/ci/checks/tests-shell.sh" \
    || { echo "the lane no longer interpolates its basename list; update this case" >&2; return 1; }
  grep -qF -- "$flt" "$REPO_ROOT/ci/checks/tests-shell.sh" \
    || { echo "the lane no longer uses this prune; update this case" >&2; return 1; }

  _drift_reports() {
    printf '%s\n' "$1" | grep -Ev "$flt" | grep -E "$keep"
  }

  # Every one of these is the gate's own scratch, and reporting any of them
  # exits 20 before a suite runs -- on a push whose commits and suite files are
  # unchanged. The `.json` and `.py` entries are the ones the widened keep-list
  # would have caught if the prune had stayed a list of four names, and
  # `.pytest_cache/.gitignore` was already reported before it was widened.
  local noise
  for noise in "ci/__pycache__/x.pyc" "ci/lib/__pycache__/y.pyc" "ci/.ruff_cache/z" \
               "ci/tmp/local.txt" "ci/tests/.pytest_cache/w" "ci/reports/junit.xml" \
               "ci/.ci-gate/state" "ci/.ruff_cache/0.6.9.json" \
               "ci/tests/.pytest_cache/.gitignore" "ci/.mypy_cache/meta.json" \
               "ci/.venv/lib/site.py" "ci/artifacts/report.json"; do
    run _drift_reports "$noise"
    [ -z "$output" ] \
      || { echo "a path the suites never read was reported as drift: $noise" >&2; return 1; }
  done

  # The control, and it is what the ignored half exists for: a commit that
  # deletes a suite input and ignores its path leaves the worktree replacement
  # invisible to the tracked and untracked lists, and this is the scan that can
  # still see it. Dropping caches must not drop that.
  #
  # The configuration is listed with the shell because that is most of what
  # these suites assert on: test_js_lane reads checks.yml and affected.yml,
  # test_preflight reads lanes.conf, the layout suite reads manifest.yml, and
  # the language lanes run the fixtures under ci/tests/fixtures/ as real
  # projects.
  local real
  for real in "ci/tests/test_hooks.bats" "ci/lib/git.sh" "ci/checks/node.sh" \
              ".githooks/pre-push" ".gitignore" "frontend/README.md" "ci/lib/helper.bash" \
              "ci/config/affected.yml" "ci/config/checks.yml" "ci/config/lanes.conf" \
              "ci/config/path-rules.conf" "ci/checks/manifest.yml" "ci/VERSION" \
              "ci/config/schema/gate.schema.json" "ci/debt/known-failures.yml" \
              "ci/tests/fixtures/node/package.json" "ci/tests/fixtures/node/src/hello.js" \
              "ci/tests/fixtures/python/pyproject.toml" "ci/tests/fixtures/go/go.mod"; do
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
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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

@test "typecheck: a tree it could not list is not a workspace with no project" {
  # `ls_tree_paths` returns non-zero exactly when it could not enumerate the
  # tree -- a failed read, or a path it cannot spell without splitting it -- and
  # the caller tested it in an `if`, so either one skipped the missing-project
  # scan entirely. The empty `_tc_list` beside it then reads as "this workspace
  # carries no TypeScript project", the lane returns 3, and the push passes
  # having compiled none of it. Could-not-ask and nothing-there, one more time,
  # at the producer this block had just been given.
  local sb; sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks" "$sb/ws"
  cp "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  cp "$REPO_ROOT/ci/checks/typecheck.sh" "$sb/ci/checks/"
  # common.sh with the reader forced to its unspellable-path status, which is
  # the branch a real repository reaches only with a newline in a file name.
  {
    cat "$REPO_ROOT/ci/lib/common.sh"
    printf '\nci::common::ls_tree_paths() { return 2; }\n'
  } > "$sb/ci/lib/common.sh"
  (
    cd "$sb" || exit 1
    git init -q -b main . && git config user.email t@t && git config user.name t
    printf '{ "name": "w", "private": true, "scripts": { "typecheck": "tsc --noEmit" } }\n' > ws/package.json
    printf '{}\n' > ws/tsconfig.json
    git add -A && git commit -qm base
  ) >/dev/null 2>&1 || { rm -rf "$sb"; echo "fixture failed" >&2; return 1; }

  run bash -c "cd '$sb' && CI_GATE_MODE=ship CI_GATE_CHECK_ID=typecheck-js bash ci/checks/typecheck.sh 2>&1"
  [ "$status" -ne 0 ] \
    || { rm -rf "$sb"; echo "an unreadable tree passed the lane: $output" >&2; return 1; }
  # Either refusal is the right one, and which fires depends on how far the lane
  # gets: workspace enumeration reads the same listing and now refuses there
  # first, before this lane has a workspace to look at. The pair is the rule --
  # what must never happen is the run continuing to a verdict about a tree it
  # could not read.
  [[ "$output" == *"Cannot read"* || "$output" == *"Cannot list"* ]] \
    || { rm -rf "$sb"; echo "refused without saying the listing failed: $output" >&2; return 1; }
  [[ "$output" == *"carries a newline"* ]] \
    || { rm -rf "$sb"; echo "did not name the reason for status 2: $output" >&2; return 1; }
  [[ "$output" != *"no TypeScript project"* ]] \
    || { rm -rf "$sb"; echo "reported the workspace as having no project: $output" >&2; return 1; }
  [[ "$output" != *"skipped"* ]] \
    || { rm -rf "$sb"; echo "reported a skip for a tree it could not read: $output" >&2; return 1; }
  rm -rf "$sb"

  # The control: with the reader working, the same tree is checked as before and
  # this refusal does not fire. Without it the case is satisfied by a lane that
  # refuses everything.
  local sb2; sb2="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb2/ci/lib" "$sb2/ci/checks" "$sb2/ws"
  cp "$REPO_ROOT/ci/lib/log.sh" "$REPO_ROOT/ci/lib/common.sh" "$sb2/ci/lib/"
  cp "$REPO_ROOT/ci/checks/typecheck.sh" "$sb2/ci/checks/"
  (
    cd "$sb2" || exit 1
    git init -q -b main . && git config user.email t@t && git config user.name t
    printf '{ "name": "w", "private": true, "scripts": { "typecheck": "tsc --noEmit" } }\n' > ws/package.json
    printf '{}\n' > ws/tsconfig.json
    git add -A && git commit -qm base
  ) >/dev/null 2>&1 || { rm -rf "$sb2"; echo "control fixture failed" >&2; return 1; }
  run bash -c "cd '$sb2' && CI_GATE_MODE=ship CI_GATE_CHECK_ID=typecheck-js bash ci/checks/typecheck.sh 2>&1"
  [[ "$output" != *"Cannot read"* ]] \
    || { rm -rf "$sb2"; echo "a readable tree was refused as unreadable: $output" >&2; return 1; }
  rm -rf "$sb2"
}

@test "git: a tag-only push is measured over the tag that publishes something" {
  # `_pr_best` collapses over every tip, published or not, and it collapses by
  # ancestry -- so a push listing an already-published tag before an unpublished
  # one on an unrelated lineage kept the published tag, because neither is the
  # other's ancestor. The range came out `published..published`: zero commits,
  # and a non-empty *string*, so git-safety's "cannot determine the range" guard
  # did not fire. It and the signature and linear-history checks all walked
  # nothing while the unpublished tagged history went out unexamined.
  local root; root="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  (
    cd "$root" || exit 1
    git init -q --bare remote.git
    git init -q -b main work
    cd work || exit 1
    git config user.email t@t && git config user.name t
    printf 'a\n' > a.txt && git add -A && git commit -qm a
    git tag old
    git checkout -q --orphan side && git rm -qrf . && printf 'b\n' > b.txt \
      && git add -A && git commit -qm b
    git tag new
    git checkout -q main
    git remote add origin "$root/remote.git"
    # Only `old` is published. `new` is a lineage the destination has never seen.
    git push -q origin main refs/tags/old
    printf '%s %s\n' "$(git rev-parse old)" "$(git rev-parse new)" > .shas
  ) >/dev/null 2>&1 || { rm -rf "$root"; echo "fixture failed" >&2; return 1; }

  local pub unpub
  read -r pub unpub < "$root/work/.shas"

  local range
  range="$(bash -c "cd '$root/work' && . '$REPO_ROOT/ci/lib/git.sh' >/dev/null 2>&1 \
    && CI_GATE_PUSH_NEW_SHA='' CI_GATE_PUSH_OLD_SHA='' CI_GATE_PUSH_BRANCH_TIPS='' \
       CI_GATE_PUSH_TAG_TIPS='$pub $unpub' CI_GATE_PUSH_OTHER_TIPS='' \
       CI_GATE_PUSH_REMOTE=origin ci::git::push_range 2>/dev/null")"

  # The assertion is the number of commits the history checks would walk, not
  # the text of the range: `published..published` is a perfectly well-formed
  # range and that is exactly what made it dangerous.
  local walked
  walked="$(cd "$root/work" && git rev-list --count $range 2>/dev/null || echo 0)"
  [ "${walked:-0}" -ge 1 ] \
    || { rm -rf "$root"; echo "the range [$range] walks ${walked} commits; the push publishes one" >&2; return 1; }
  [[ "$range" == *"$unpub"* ]] \
    || { rm -rf "$root"; echo "the range [$range] does not reach the unpublished tag" >&2; return 1; }

  # The control, and it is the reason `_pr_best` is still the fallback: a push
  # of nothing but already-published tags publishes nothing, and its true range
  # is that tag's empty one. ci::git::push_is_label_only routes such a push past
  # the content lanes separately.
  local pubrange
  pubrange="$(bash -c "cd '$root/work' && . '$REPO_ROOT/ci/lib/git.sh' >/dev/null 2>&1 \
    && CI_GATE_PUSH_NEW_SHA='' CI_GATE_PUSH_OLD_SHA='' CI_GATE_PUSH_BRANCH_TIPS='' \
       CI_GATE_PUSH_TAG_TIPS='$pub' CI_GATE_PUSH_OTHER_TIPS='' \
       CI_GATE_PUSH_REMOTE=origin ci::git::push_range 2>/dev/null")"
  [ "$pubrange" = "${pub}..${pub}" ] \
    || { rm -rf "$root"; echo "a published-only push no longer resolves to its own empty range: [$pubrange]" >&2; return 1; }
  rm -rf "$root"
}

@test "preflight: the one cached lane carries the reports it owns" {
  # A cached lane does not run, so anything it writes besides its result is not
  # written. `changed-files` produces three file lists under ci/reports/ and a
  # hit restored neither of them -- they were left as an earlier run had them,
  # or absent on a fresh checkout, while the lane reported PASS. An audit trail
  # describing a different tree, with a green result beside it.
  local fns="$BATS_TEST_TMPDIR/reports.sh"
  _pf_fns "$fns" _cached_reports_for _check_is_cacheable

  local owned
  # shellcheck disable=SC1090
  owned="$( . "$fns"; _cached_reports_for changed-files | tr '\n' ' ' )"
  local f
  for f in changed-files.txt staged-files.txt untracked-files.txt; do
    [[ "$owned" == *"$f"* ]] \
      || { echo "the cache does not carry ${f}, which the lane writes" >&2; return 1; }
  done

  # Every report the lane actually writes has to be in that list, read from the
  # lane rather than restated -- a fourth one added there and not here is the
  # same stale-artifact bug again.
  local written
  written="$(grep -oE 'ci/reports/[a-z-]+\.txt' "$REPO_ROOT/ci/checks/changed-files.sh" \
    | sed 's|ci/reports/||' | sort -u)"
  [ -n "$written" ] || { echo "could not read what the lane writes" >&2; return 1; }
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    [[ "$owned" == *"$f"* ]] \
      || { echo "the lane writes ${f} and the cache would not restore it" >&2; return 1; }
  done <<< "$written"

  # The control: every other lane owns nothing, so this is a declaration and not
  # a blanket copy of ci/reports/.
  local lane
  for lane in node python typecheck-js git-safety test-layout; do
    # shellcheck disable=SC1090
    [ -z "$( . "$fns"; _cached_reports_for "$lane" )" ] \
      || { echo "${lane} claims to own reports but is not cached" >&2; return 1; }
  done

  # And the restore side treats an entry that cannot produce one of them as a
  # miss rather than patching it up: an entry written by an older revision of
  # this file has no way to know what it was supposed to carry.
  grep -q 'cache entry incomplete' "$REPO_ROOT/ci/preflight.sh" \
    || { echo "an incomplete cache entry is still served as a hit" >&2; return 1; }
}

@test "preflight: the changed-files key describes the lists that lane reports" {
  # The key is made of the changeset, and in quick mode the changeset is the
  # staged paths whenever there are any -- while this lane reports on changed,
  # staged *and* untracked. Two trees with identical staging and different
  # untracked files therefore shared a key, and a hit served counts and lists
  # describing the other one.
  local fns="$BATS_TEST_TMPDIR/key.sh"
  _pf_fns "$fns" _compute_cache_key _tool_fingerprint _changeset_content_hash

  local sb; sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  (
    cd "$sb" || exit 1
    git init -q -b main . && git config user.email t@t && git config user.name t
    printf 'a\n' > a.txt && git add -A && git commit -qm a
    printf 'b\n' > staged.txt && git add staged.txt
  ) >/dev/null 2>&1 || { rm -rf "$sb"; echo "fixture failed" >&2; return 1; }

  _key() {
    bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/common.sh' >/dev/null 2>&1 \
      && . '$REPO_ROOT/ci/lib/git.sh' >/dev/null 2>&1 \
      && . '$REPO_ROOT/ci/lib/cache.sh' >/dev/null 2>&1 \
      && . '$fns' && _compute_cache_key changed-files"
  }

  local before after
  before="$(_key)"
  [ -n "$before" ] || { rm -rf "$sb"; echo "no key was produced" >&2; return 1; }

  # An untracked file appears. Staging is untouched, so the changeset the key
  # used to be made of is byte-identical -- and the lane's untracked count is not.
  printf 'c\n' > "$sb/untracked.txt"
  after="$(_key)"
  [ "$before" != "$after" ] \
    || { rm -rf "$sb"; echo "an untracked file did not move the key" >&2; return 1; }

  # The control: nothing changed, so the key is stable. Without it the case is
  # satisfied by a key that is different every time, which caches nothing and
  # tests nothing.
  [ "$after" = "$(_key)" ] \
    || { rm -rf "$sb"; echo "the key is not stable for an unchanged tree" >&2; return 1; }
  rm -rf "$sb"
}

@test "preflight: the ship lane plan is the one in the push" {
  # This branch selected the lanes *and* read them from the worktree copy of
  # ci/config/lanes.conf. An unstaged edit deleting the content-lane rows
  # therefore removed those lanes from the run and, with the gate's own
  # self-check lanes gone with them, removed what would have reported the edit
  # -- so a pushed commit carrying a failing frontend test passed, on a plan
  # that is not in the push.
  #
  # Driven through run_mode with run_phase stubbed, rather than through a whole
  # preflight run: the question here is which file the lane list is read from,
  # and a real run answers it only indirectly -- through whichever lanes that
  # sandbox happens to be able to execute.
  local fns="$BATS_TEST_TMPDIR/lanes.sh"
  _pf_fns "$fns" run_mode

  local sb; sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  (
    cd "$sb" || exit 1
    git init -q -b main . && git config user.email t@t && git config user.name t
    mkdir -p ci/config
    printf 'node|Node|./ci/checks/node.sh|yes|d
' > ci/config/lanes.conf
    printf 'test-layout|Layout|./ci/checks/test-layout.sh|yes|d
' >> ci/config/lanes.conf
    printf 'lint|Lint|./ci/checks/lint.sh|no|d
' >> ci/config/lanes.conf
    git add -A && git commit -qm base
  ) >/dev/null 2>&1 || { rm -rf "$sb"; echo "fixture failed" >&2; return 1; }

  # run_mode reaches the lane branch and then the mode dispatch; both are
  # stubbed so what comes back is the plan and nothing else.
  _plan() { # _plan <mode>
    bash -c "cd '$sb' && . '$fns' >/dev/null 2>&1
      run_phase() { for e in \"\$@\"; do printf 'LANE:%s ' \"\${e%%:*}\"; done; }
      run_common_checks() { printf 'FULLPLAN '; }
      run_full_or_ship_checks() { printf 'FULLPLAN '; }
      _push_is_content_free() { return 1; }
      MODE=\"$1\" CI_GATE_USE_LANES=1 run_mode 2>/dev/null" _ "$1"
  }

  local before
  before="$(_plan ship)"
  [[ "$before" == *"LANE:node"* ]]     || { rm -rf "$sb"; echo "the lane branch was not reached at all: [$before]" >&2; return 1; }

  # The edit: on disk only, nothing staged, nothing committed.
  ( cd "$sb" && printf 'lint|Lint|./ci/checks/lint.sh|no|d
' > ci/config/lanes.conf ) >/dev/null 2>&1

  local after
  after="$(_plan ship)"
  [ "$before" = "$after" ]     || { rm -rf "$sb"; echo "an unstaged lanes.conf edit changed the ship plan: [$before] -> [$after]" >&2; return 1; }

  # The control, and it is what keeps this from being "ship ignores
  # lanes.conf": the same removal, committed, is genuinely not scheduled.
  ( cd "$sb" && git add -A && git commit -qm 'drop the content lanes' ) >/dev/null 2>&1
  local committed
  committed="$(_plan ship)"
  [[ "$committed" != *"LANE:node"* ]]     || { rm -rf "$sb"; echo "a committed removal was ignored, so HEAD is not being read: [$committed]" >&2; return 1; }
  [[ "$committed" == *"LANE:lint"* ]]     || { rm -rf "$sb"; echo "the committed plan did not run either: [$committed]" >&2; return 1; }

  # And `full` is deliberately a whole-tree run against the worktree, so it
  # still reads the file on disk -- the narrowing is a ship rule, not a new
  # rule for every mode.
  ( cd "$sb" && git checkout -q -- ci/config/lanes.conf       && printf 'lint|Lint|./ci/checks/lint.sh|no|d
' > ci/config/lanes.conf ) >/dev/null 2>&1
  local full
  full="$(_plan full)"
  [[ "$full" != *"LANE:node"* ]]     || { rm -rf "$sb"; echo "full mode stopped reading the worktree: [$full]" >&2; return 1; }
  rm -rf "$sb"
}

@test "runner: a check that ignores SIGTERM still meets its deadline" {
  # `timeout N` is not a deadline on its own. It sends TERM, and TERM only ends
  # a process that does not catch it -- so a check that traps it, or whose child
  # ignores it, outlives the timeout and `timeout` waits on it forever. The lane
  # then never reports at all, which is worse than the FAIL_INFRA the deadline
  # exists to produce: a gate that hangs gets killed by hand and the push goes
  # out unjudged.
  local prefix
  prefix="$( . "$REPO_ROOT/ci/lib/runner.sh" >/dev/null 2>&1
             ci::runner::_timeout_cmd 300 )"
  [[ "$prefix" == *"--kill-after="* ]] \
    || { echo "the timeout prefix cannot escalate: [$prefix]" >&2; return 1; }
  [[ "$prefix" == *" 300" ]] \
    || { echo "the declared timeout is no longer the last word: [$prefix]" >&2; return 1; }

  # Both arms, because which one a host reaches depends on what it has
  # installed, and the escalation was added to one of them once already.
  local body
  body="$(sed -n '/^ci::runner::_timeout_cmd() {/,/^}/p' "$REPO_ROOT/ci/lib/runner.sh")"
  [ "$(printf '%s\n' "$body" | grep -c -- '--kill-after=')" -eq 2 ] \
    || { echo "one of the two timeout utilities still has no escalation" >&2; return 1; }

  # And it works. Run under an outer guard so a regression here fails the case
  # instead of hanging the suite -- which is the very failure being tested.
  command -v timeout >/dev/null 2>&1 || skip "no timeout(1) on this host"
  local rc=0
  timeout 20 timeout --kill-after=1s 2 \
    bash -c 'trap "" TERM; while :; do sleep 0.2; done' >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 137 ] \
    || { echo "a TERM-ignoring child was not killed (rc=$rc; 124 means the outer guard fired)" >&2; return 1; }
}

@test "common: a workspace path holding a newline is refused, not split" {
  # `find -print` is line-delimited, so `odd\nname/package.json` arrived as two
  # records and discovery derived the workspace `name` -- a directory that does
  # not exist. The node, tests and typecheck lanes then reported FAIL_INFRA for
  # it, or, one spelling over, would have inspected a different directory than
  # the push carries. Refused rather than split, which is what the shared NUL
  # reader returns 2 for and what every other path collector here already does.
  local sb nl
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  nl="$(printf 'odd\nname')"
  mkdir -p "$sb/$nl" 2>/dev/null || { rm -rf "$sb"; skip "this filesystem cannot hold a newline in a path"; }
  printf '{ "name": "w" }\n' > "$sb/$nl/package.json" 2>/dev/null \
    || { rm -rf "$sb"; skip "this filesystem cannot hold a newline in a path"; }
  printf '{}\n' > "$sb/$nl/package-lock.json"

  local out rc=0
  out="$( cd "$sb" && . "$REPO_ROOT/ci/lib/common.sh" >/dev/null 2>&1 \
          && ci::common::node_workspaces package.json )" || rc=$?
  [ "$rc" -ne 0 ] \
    || { rm -rf "$sb"; echo "a split path was reported as a workspace: [$out]" >&2; return 1; }
  [[ "$out" != *"name"* ]] \
    || { rm -rf "$sb"; echo "the tail of the path was emitted as a workspace: [$out]" >&2; return 1; }

  # The control: an ordinary tree beside it still enumerates, so this refuses the
  # path it cannot carry rather than every repository that has one.
  rm -rf "$sb/$nl"
  mkdir -p "$sb/frontend"
  printf '{ "name": "w" }\n' > "$sb/frontend/package.json"
  printf '{}\n' > "$sb/frontend/package-lock.json"
  rc=0
  out="$( cd "$sb" && . "$REPO_ROOT/ci/lib/common.sh" >/dev/null 2>&1 \
          && ci::common::node_workspaces package.json )" || rc=$?
  [ "$rc" -eq 0 ] && [ "$out" = "frontend" ] \
    || { rm -rf "$sb"; echo "an ordinary tree stopped enumerating (rc=$rc): [$out]" >&2; return 1; }
  rm -rf "$sb"
}

@test "test-layout: a frontend it could not read is not a repository without one" {
  # `$(git ls-tree ... | head -n 1)` reduced a failed listing -- an unavailable
  # tree object in a partial or damaged checkout -- to empty output, which is
  # exactly what an absent frontend produces. The caller logged "skipped: no
  # frontend/ directory" and returned PASS, so the one guard standing between a
  # misplaced test and a green build that never ran it was switched off by an
  # unreadable object rather than by a decision.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib" "$sb/frontend/tests" "$sb/bin"
  cp "$REPO_ROOT/ci/checks/test-layout.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  printf 'it("x", () => {});\n' > "$sb/frontend/tests/a.test.ts"
  printf 'export default { test: { include: ["tests/**/*.test.ts"] } };\n' > "$sb/frontend/vitest.config.ts"
  ( cd "$sb" && git init -q -b main . && git add -A \
      && git -c user.email=t@t -c user.name=t commit -qm base ) >/dev/null 2>&1

  # A git that answers every question except listing a tree.
  local realgit
  realgit="$(command -v git)"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'if [ "${1:-}" = "ls-tree" ]; then exit 128; fi\n'
    printf 'exec "%s" "$@"\n' "$realgit"
  } > "$sb/bin/git"
  chmod +x "$sb/bin/git"

  run bash -c "cd '$sb' && PATH=\"$sb/bin:\$PATH\" CI_GATE_MODE=ship bash ci/checks/test-layout.sh 2>&1"
  [ "$status" -eq 30 ] \
    || { rm -rf "$sb"; echo "an unreadable tree did not refuse (status=$status): $output" >&2; return 1; }
  [[ "$output" == *"Cannot read HEAD:frontend"* ]] \
    || { rm -rf "$sb"; echo "$output" >&2; return 1; }
  [[ "$output" != *"skipped: no frontend"* ]] \
    || { rm -rf "$sb"; echo "$output" >&2; return 1; }

  # The control that keeps the three answers apart: a repository that genuinely
  # has no frontend in HEAD is still a skip, not an infrastructure failure.
  local sb2
  sb2="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb2/ci/checks" "$sb2/ci/lib"
  cp "$REPO_ROOT/ci/checks/test-layout.sh" "$sb2/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb2/ci/lib/"
  printf 'x\n' > "$sb2/a.txt"
  ( cd "$sb2" && git init -q -b main . && git add -A \
      && git -c user.email=t@t -c user.name=t commit -qm base ) >/dev/null 2>&1
  run bash -c "cd '$sb2' && CI_GATE_MODE=ship bash ci/checks/test-layout.sh 2>&1"
  [ "$status" -eq 0 ] || { rm -rf "$sb" "$sb2"; echo "$output" >&2; return 1; }
  [[ "$output" == *"skipped: no frontend"* ]] || { rm -rf "$sb" "$sb2"; echo "$output" >&2; return 1; }
  rm -rf "$sb" "$sb2"
}

@test "common: generated output is not a workspace nested under one" {
  # Nuxt writes a real `.nuxt/package.json`, so recursive discovery read it as a
  # child package: node_workspaces reported `frontend/.nuxt` as a workspace
  # nested under `frontend`, the ambiguity rule refused the pair, and the whole
  # enumeration failed -- quick and full validation blocked on an ordinary
  # workspace after ordinary tooling had run in it.
  #
  # `.nuxt` was in every other Node scan in this repository and missing from the
  # predicate that is meant to be the single definition. That is the fourth
  # finding of exactly that shape in this PR, so the siblings are asserted here
  # too rather than the one entry that was reported.
  #
  # `.cache` was the fifth, and it is in the list below for the reason the
  # others are: Parcel, Babel and several bundlers write
  # `frontend/.cache/<something>/package.json`, discovery read the cache as a
  # package nested under `frontend`, and node_workspaces exited 1 -- the node,
  # tests and typecheck lanes all blocked on a repository whose only fault was
  # that ordinary tooling had run in it. It reached this predicate a round after
  # it reached ci/checks/test-layout.sh's prune, which is the direction
  # "check the siblings" does not cover, so the assertion below closes it.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb/frontend"
  printf '{ "name": "w", "private": true }\n' > "$sb/frontend/package.json"
  printf '{}\n' > "$sb/frontend/package-lock.json"
  local d
  for d in .cache .nuxt .next .turbo .vite dist build coverage htmlcov node_modules; do
    mkdir -p "$sb/frontend/$d"
    printf '{ "name": "generated" }\n' > "$sb/frontend/$d/package.json"
  done

  local out rc=0
  out="$( cd "$sb" && . "$REPO_ROOT/ci/lib/common.sh" >/dev/null 2>&1 \
          && ci::common::node_workspaces package.json )" || rc=$?
  [ "$rc" -eq 0 ] \
    || { rm -rf "$sb"; echo "discovery failed over generated output (rc=$rc): [$out]" >&2; return 1; }
  [ "$out" = "frontend" ] \
    || { rm -rf "$sb"; echo "expected just frontend, got: [$out]" >&2; return 1; }

  # The control: a genuine nested package is still seen, so this did not make
  # discovery blind to real ones.
  mkdir -p "$sb/frontend/packages/app"
  printf '{ "name": "app", "private": true }\n' > "$sb/frontend/packages/app/package.json"
  rc=0
  out="$( cd "$sb" && . "$REPO_ROOT/ci/lib/common.sh" >/dev/null 2>&1 \
          && ci::common::node_workspaces package.json )" || rc=$?
  [ "$rc" -ne 0 ] \
    || { rm -rf "$sb"; echo "a real nested package was not noticed: [$out]" >&2; return 1; }
  rm -rf "$sb"
}

@test "common: a HEAD listing that failed is not a push with no workspaces" {
  # `|| return 0` read "could not ask" -- the three preconditions, which really
  # are nothing to report -- into a fourth case that is not one: HEAD exists, it
  # was asked, and the answer did not come back. With a name in the tree that
  # ls_tree_paths refuses to fold and a committed workspace missing locally,
  # this returned 0, node_workspaces handed back an empty list with status 0,
  # and ship-mode tests-js and typecheck-js printed "skipped: no package.json
  # found" for a push that carries one.
  local sb; sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb/ci/lib"
  {
    cat "$REPO_ROOT/ci/lib/common.sh"
    printf '\nci::common::ls_tree_paths() { return 2; }\n'
  } > "$sb/ci/lib/common.sh"
  (
    cd "$sb" || exit 1
    git init -q -b main . && git config user.email t@t && git config user.name t
    mkdir -p frontend && printf '{ "name": "f", "private": true }\n' > frontend/package.json
    git add -A && git commit -qm base
    rm -rf frontend
  ) >/dev/null 2>&1 || { rm -rf "$sb"; echo "fixture failed" >&2; return 1; }

  local rc=0
  ( cd "$sb" && . ci/lib/common.sh >/dev/null 2>&1 \
    && CI_GATE_MODE=ship ci::common::_head_manifests_present package.json ) >/dev/null 2>&1 || rc=$?
  [ "$rc" -ne 0 ] \
    || { rm -rf "$sb"; echo "an unreadable HEAD listing reported nothing to report" >&2; return 1; }

  # It reaches the caller, which is the half that matters: node_workspaces
  # returning an empty list with status 0 is what the lanes read as "no
  # workspace here".
  rc=0
  ( cd "$sb" && . ci/lib/common.sh >/dev/null 2>&1 \
    && CI_GATE_MODE=ship ci::common::node_workspaces package.json ) >/dev/null 2>&1 || rc=$?
  [ "$rc" -ne 0 ] \
    || { rm -rf "$sb"; echo "node_workspaces passed an unreadable tree through as empty" >&2; return 1; }

  # The controls. The three preconditions are still nothing to report, or this
  # function starts refusing every pre-commit run.
  rc=0
  ( cd "$sb" && . ci/lib/common.sh >/dev/null 2>&1 \
    && CI_GATE_MODE=quick ci::common::_head_manifests_present package.json ) >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 0 ] \
    || { rm -rf "$sb"; echo "a non-ship run was refused" >&2; return 1; }

  # And with the reader working, the same tree is answered as before: the
  # workspace is committed and absent locally, which is the case this function
  # was written for and still has to report as 1.
  cp -f "$REPO_ROOT/ci/lib/common.sh" "$sb/ci/lib/common.sh"
  rc=0
  ( cd "$sb" && . ci/lib/common.sh >/dev/null 2>&1 \
    && CI_GATE_MODE=ship ci::common::_head_manifests_present package.json ) >/dev/null 2>&1 || rc=$?
  [ "$rc" -ne 0 ] \
    || { rm -rf "$sb"; echo "a genuinely missing workspace stopped being reported" >&2; return 1; }
  rm -rf "$sb"
}

@test "branch-protection: a required policy whose range will not resolve is infra, not a pass" {
  # These two logged a skip and left OVERALL_RESULT at PASS, so with the policies
  # required and a tip that cannot be read -- CI_GATE_PUSH_NEW_SHA naming an
  # object this clone does not carry -- the run reported success having checked
  # neither policy. push_range returns non-zero only where the tip itself will
  # not resolve; every case where there is simply nothing new to measure returns
  # 0 with an empty or bare-tip range and still reaches the walk. So a non-zero
  # status is the tool failing, not the push being empty.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb/ci/lib" "$sb/ci/checks"
  cp "$REPO_ROOT/ci/checks/branch-protection.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" \
     "$REPO_ROOT/ci/lib/git.sh" "$sb/ci/lib/"
  (
    cd "$sb"
    # Not a protected name. The protected-branch check is a different verdict
    # reached before this one, and on `main` it would mask what is under test.
    git init -q -b work .
    printf 'a\n' > a.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm base
  ) >/dev/null 2>&1

  local unreadable=0123456789012345678901234567890123456789
  run bash -c "cd '$sb' && git cat-file -e '$unreadable' 2>&1"
  [ "$status" -ne 0 ] || { rm -rf "$sb"; echo "the premise failed: that object is readable" >&2; return 1; }

  run bash -c "cd '$sb' && CI_GATE_REQUIRE_SIGNED_COMMITS=1 CI_GATE_REQUIRE_LINEAR_HISTORY=1 \
    CI_GATE_MODE=ship CI_GATE_PUSH_REMOTE=origin CI_GATE_PUSH_NEW_SHA='$unreadable' \
    bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -ne 0 ] \
    || { rm -rf "$sb"; echo "an unresolvable tip reported PASS over two required policies" >&2; echo "$output" >&2; return 1; }
  [[ "$output" == *"Cannot resolve the range this push publishes"* ]] \
    || { rm -rf "$sb"; echo "the refusal did not name its reason: $output" >&2; return 1; }
  [[ "$output" != *"skipping signed commit check"* ]] \
    || { rm -rf "$sb"; echo "still reported as a skip" >&2; return 1; }

  # The control that keeps this from becoming "refuse everything": with the
  # policies off, an unresolvable tip is not this check's business.
  run bash -c "cd '$sb' && CI_GATE_MODE=ship CI_GATE_PUSH_REMOTE=origin \
    CI_GATE_PUSH_NEW_SHA='$unreadable' bash ci/checks/branch-protection.sh 2>&1"
  [ "$status" -eq 0 ] \
    || { rm -rf "$sb"; echo "an unresolvable tip failed a run that requires neither policy" >&2; echo "$output" >&2; return 1; }

  # And the control that keeps a real push passing: a resolvable tip with no
  # merge in it, both policies still required, must not be refused by this
  # branch. (Signatures are a separate verdict and are not asserted here.)
  local tip
  tip="$(cd "$sb" && git rev-parse HEAD)"
  run bash -c "cd '$sb' && CI_GATE_REQUIRE_LINEAR_HISTORY=1 CI_GATE_MODE=ship \
    CI_GATE_PUSH_REMOTE=origin CI_GATE_PUSH_NEW_SHA='$tip' \
    bash ci/checks/branch-protection.sh 2>&1"
  [[ "$output" != *"Cannot resolve the range this push publishes"* ]] \
    || { rm -rf "$sb"; echo "a resolvable tip was reported unresolvable: $output" >&2; return 1; }
  rm -rf "$sb"
}

@test "git-safety: a temporary file it cannot create is infrastructure, not exit 1" {
  # Every temporary file in this lane goes through ci::common::mktemp_file now,
  # and two of the call sites were left unguarded. Under `set -Eeuo pipefail` a
  # failing mktemp -- a full or unwritable TMPDIR, a restricted host -- aborts
  # the script where it stands with raw exit 1, which is outside the 0/10/20/30
  # contract preflight reads. The lane then looks like a broken script rather
  # than like infrastructure that could not run, and this is the lane where "it
  # did not run" must never be quiet.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib" "$sb/bin"
  cp "$REPO_ROOT/ci/checks/git-safety.sh" "$REPO_ROOT/ci/checks/common.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" \
     "$REPO_ROOT/ci/lib/git.sh" "$sb/ci/lib/"
  mkdir -p "$sb/ci/config"
  [ -f "$REPO_ROOT/ci/config/gate.yml" ] && cp "$REPO_ROOT/ci/config/gate.yml" "$sb/ci/config/"
  printf '#!/usr/bin/env bash\nexit 1\n' > "$sb/bin/mktemp"
  chmod +x "$sb/bin/mktemp"
  ( cd "$sb" && git init -q -b main . \
      && printf 'x\n' > a.txt && git add -A \
      && git -c user.email=t@t -c user.name=t commit -qm base ) >/dev/null 2>&1

  run bash -c "cd '$sb' && PATH=\"$sb/bin:\$PATH\" CI_GATE_MODE=ship bash ci/checks/git-safety.sh 2>&1"
  [ "$status" -eq 30 ] \
    || { rm -rf "$sb"; echo "a failing mktemp left the contract (status=$status): $output" >&2; return 1; }
  [[ "$output" == *"temporary file"* ]] \
    || { rm -rf "$sb"; echo "the refusal did not say what failed: $output" >&2; return 1; }
  rm -rf "$sb"

  # And both call sites carry the guard, since the behavioural half above can
  # only reach the first of them: the second is past a range this sandbox never
  # resolves. Pinned to the shape rather than to the line, so moving the code
  # keeps the assertion.
  local unguarded
  unguarded="$(grep -n 'ci::common::mktemp_file' "$REPO_ROOT/ci/checks/git-safety.sh" \
               | grep -v '|| {' || true)"
  [ -z "$unguarded" ] \
    || { echo "an unguarded mktemp_file remains in git-safety.sh:"$'\n'"$unguarded" >&2; return 1; }
}

@test "ship drift: an ignored shadow with a non-ASCII name is still drift" {
  # git quotes by default. `git ls-files` renders `frontend/src/café.ts` as
  # `"frontend/src/caf\303\251.ts"` -- with the quotes, with the octal escapes,
  # and ending in a quote character rather than in `.ts`. So the extension filter
  # discarded it, this function reported clean, and a committed deletion of that
  # path with a local shadow left ship-mode tests-js and typecheck-js validating
  # a file the push does not carry. One accented letter switched the guard off.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  (
    cd "$sb"
    git init -q -b main .
    mkdir -p frontend/src
    printf 'export const a = 1;\n' > "frontend/src/café.ts"
    printf 'export const b = 2;\n' > frontend/src/app.ts
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm base
    git rm -q "frontend/src/café.ts"
    printf 'frontend/src/café.ts\n' > .gitignore
    git add .gitignore
    git -c user.email=t@t -c user.name=t commit -qm 'delete and ignore the module'
    printf 'export const a = 999;\n' > "frontend/src/café.ts"
  ) >/dev/null 2>&1

  # The premise: git really does quote it, or this case is asserting nothing.
  # Skipped rather than failed when the platform cannot hold the name at all.
  if [ ! -f "$sb/frontend/src/café.ts" ]; then
    rm -rf "$sb"; skip "this filesystem cannot hold a non-ASCII path"
  fi
  local quoted
  quoted="$( cd "$sb" && git ls-files --others --ignored --exclude-standard -- frontend )"
  [[ "$quoted" == *'\303\251'* ]] \
    || { rm -rf "$sb"; skip "git is not quoting here (core.quotepath off); the case cannot show the fault"; }

  local out rc=0
  out="$( cd "$sb" \
          && . "$REPO_ROOT/ci/lib/common.sh" >/dev/null 2>&1 \
          && ci::common::workspace_drift frontend )" || rc=$?
  [ "$rc" -eq 1 ] \
    || { rm -rf "$sb"; echo "the quoted shadow was not reported as drift (rc=$rc, out=[$out])" >&2; return 1; }
  [[ "$out" == *"caf"* ]] \
    || { rm -rf "$sb"; echo "drift was reported but not for the shadow: [$out]" >&2; return 1; }
  rm -rf "$sb"
}

@test "common: the NUL reader carries what git quotes and refuses what it cannot" {
  # The shared half of the two quoting fixes, asserted directly: a caller pipes
  # `git ls-files -z` into it, so what has to hold is that NUL becomes a line
  # break and an embedded newline is refused rather than split into two paths --
  # one path arriving as two is how a tail can be spelled to look like an
  # ordinary file.
  source "$REPO_ROOT/ci/lib/common.sh"
  local out rc

  out="$(printf 'a.ts\000b c.ts\000café.ts\000' | ci::common::nul_to_lines)"
  [ "$out" = "$(printf 'a.ts\nb c.ts\ncafé.ts')" ] \
    || { echo "the reader did not carry the paths: [$out]" >&2; return 1; }

  rc=0
  printf 'a.ts\000bad\nname.ts\000' | ci::common::nul_to_lines >/dev/null || rc=$?
  [ "$rc" -eq 2 ] \
    || { echo "a path holding a newline was not refused (rc=$rc)" >&2; return 1; }

  # Empty input is an answer, not a failure: a workspace with nothing ignored is
  # the ordinary case, and returning non-zero for it would read as "could not
  # compare" at every caller.
  rc=0
  out="$(printf '' | ci::common::nul_to_lines)" || rc=$?
  [ "$rc" -eq 0 ] && [ -z "$out" ] \
    || { echo "empty input was not carried (rc=$rc, out=[$out])" >&2; return 1; }
}

@test "ship drift: an ignored dotenv shadow is drift, and .envrc is not" {
  # The sharpest version of the stylesheet case below, and the one class the
  # extension filter could not see because it carries no extension at all. Vite
  # and Vitest load `.env`, `.env.local`, `.env.<mode>` and `.env.<mode>.local`
  # before a single test runs, and those files are ignored by convention -- so a
  # push that deletes and ignores `frontend/.env.test` while a local copy remains
  # left all three scans empty, and ship-mode tests-js read `VITE_*` settings
  # that exist in nobody's tree but the pusher's. A value that switches a test
  # off is the worst shape of it: the lane reports PASS having skipped exactly
  # the cases the pushed tree would have run.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  (
    cd "$sb"
    git init -q -b main .
    mkdir -p frontend/src
    printf 'VITE_RUN_INTEGRATION=1\n' > frontend/.env.test
    printf 'export const a = 1;\n'    > frontend/src/app.ts
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm base
    git rm -q frontend/.env.test
    printf 'frontend/.env.test\n' > .gitignore
    git add .gitignore
    git -c user.email=t@t -c user.name=t commit -qm 'delete and ignore the env file'
    printf 'VITE_RUN_INTEGRATION=0\n' > frontend/.env.test
  ) >/dev/null 2>&1

  local out rc=0
  out="$( cd "$sb" \
          && . "$REPO_ROOT/ci/lib/common.sh" >/dev/null 2>&1 \
          && ci::common::workspace_drift frontend )" || rc=$?
  [ "$rc" -eq 1 ] \
    || { rm -rf "$sb"; echo "the ignored dotenv shadow was not reported as drift (rc=$rc, out=[$out])" >&2; return 1; }
  [[ "$out" == *"frontend/.env.test"* ]] \
    || { rm -rf "$sb"; echo "drift was reported but not for the env file: [$out]" >&2; return 1; }
  rm -rf "$sb"

  # Every spelling Vite documents, asserted against the filter itself rather than
  # through a second sandbox: what has to hold is which names the expression
  # keeps, and a fixture per name would test the fixture.
  #
  # `.envrc` is direnv's and nothing here loads it, so it stays out. Keeping it
  # would make a directory with direnv configured permanently drifted, and a
  # guard that refuses correct pushes gets switched off.
  local expr kept want unwanted
  expr="$(grep -oE "\|\(\^\|/\)\\\\\.env[^']*" "$REPO_ROOT/ci/lib/common.sh" | head -1)"
  [ -n "$expr" ] || { echo "could not lift the dotenv arm from ci/lib/common.sh" >&2; return 1; }
  expr="${expr#|}"
  kept="$(printf '%s\n' 'frontend/.env' 'frontend/.env.local' 'frontend/.env.test' \
            'frontend/.env.test.local' 'frontend/.env.production' '.env' \
            'frontend/.envrc' 'frontend/notes.txt' \
          | grep -E "$expr" | tr '\n' ' ')"
  for want in 'frontend/.env' 'frontend/.env.local' 'frontend/.env.test' \
              'frontend/.env.test.local' 'frontend/.env.production' '.env'; do
    [[ "$kept" == *"$want"* ]] \
      || { echo "a dotenv Vite loads was dropped: ${want} (got [$kept])" >&2; return 1; }
  done
  for unwanted in 'frontend/.envrc' 'frontend/notes.txt'; do
    [[ "$kept" != *"$unwanted"* ]] \
      || { echo "not a dotenv, but kept: ${unwanted} (got [$kept])" >&2; return 1; }
  done
}

@test "ship drift: an ignored shadow of a deleted stylesheet is drift" {
  # The extension list is what these lanes *load*, not what a person would call
  # source. Vite and Vitest resolve a stylesheet import like any other module,
  # and ci/config/affected.yml already treats a stylesheet change as a reason to
  # run the frontend suite. So a push that deletes and ignores
  # frontend/src/theme.css while an ignored local copy remains left the tracked
  # scan, the untracked scan and the ignored scan all empty -- and ship-mode
  # tests-js ran against the shadow and passed over a pushed tree whose import
  # does not resolve.
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  (
    cd "$sb"
    git init -q -b main .
    mkdir -p frontend/src
    printf 'body{}\n' > frontend/src/theme.css
    printf 'import "./theme.css"\n' > frontend/src/app.ts
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm base
    git rm -q frontend/src/theme.css
    printf 'frontend/src/theme.css\n' > .gitignore
    git add .gitignore
    git -c user.email=t@t -c user.name=t commit -qm 'delete and ignore the stylesheet'
    printf 'body{}\n' > frontend/src/theme.css
  ) >/dev/null 2>&1

  local out rc=0
  out="$( cd "$sb" \
          && . "$REPO_ROOT/ci/lib/common.sh" >/dev/null 2>&1 \
          && ci::common::workspace_drift frontend )" || rc=$?
  [ "$rc" -eq 1 ] \
    || { rm -rf "$sb"; echo "the ignored shadow was not reported as drift (rc=$rc, out=[$out])" >&2; return 1; }
  [[ "$out" == *"frontend/src/theme.css"* ]] \
    || { rm -rf "$sb"; echo "drift was reported but not for the shadow: [$out]" >&2; return 1; }

  # The control that keeps the filter a filter: the vendored and generated trees
  # this deliberately prunes stay pruned, or every workspace is permanently
  # drifted and the refusal means nothing.
  #
  # `.vite` and `htmlcov` are in here for a reason, not for symmetry. This copy
  # of the pruned set was missing them while every other copy in the repository
  # had them, and `frontend/.vite/cache.js` is ignored, generated, and ends in an
  # extension the filter keeps — so ship-mode tests-js and typecheck-js refused
  # an otherwise clean push over a build cache. A guard that refuses correct
  # pushes gets switched off, which is the same outcome as not having it.
  local d
  (
    cd "$sb"
    mkdir -p frontend/node_modules/x frontend/dist frontend/.vite frontend/htmlcov \
             frontend/.turbo frontend/coverage
    printf 'a{}\n' > frontend/node_modules/x/a.css
    printf 'b{}\n' > frontend/dist/b.css
    printf 'cached\n'  > frontend/.vite/cache.js
    printf '<html>\n'  > frontend/htmlcov/index.html
    printf 'x\n'       > frontend/.turbo/log.json
    printf 'y{}\n'     > frontend/coverage/style.css
    printf 'node_modules/\ndist/\n.vite/\nhtmlcov/\n.turbo/\ncoverage/\nfrontend/src/theme.css\n' > .gitignore
  ) >/dev/null 2>&1
  out="$( cd "$sb" && . "$REPO_ROOT/ci/lib/common.sh" >/dev/null 2>&1 \
          && ci::common::workspace_drift frontend || true )"
  for d in node_modules 'frontend/dist/' 'frontend/.vite/' 'frontend/htmlcov/' \
           'frontend/.turbo/' 'frontend/coverage/'; do
    [[ "$out" != *"$d"* ]] \
      || { rm -rf "$sb"; echo "a pruned tree came back as drift: ${d} in [$out]" >&2; return 1; }
  done
  # And the shadow is still reported through all of that, so the controls have
  # not simply emptied the answer.
  [[ "$out" == *"frontend/src/theme.css"* ]] \
    || { rm -rf "$sb"; echo "the shadow was lost once the pruned trees existed: [$out]" >&2; return 1; }
  rm -rf "$sb"
}

@test "gate: no check reaches for a find flag POSIX does not define" {
  # `-maxdepth` is implemented by GNU and BSD find, which is why every use of it
  # here worked — but "both of the two we expect" is a smaller guarantee than the
  # standard, and this gate runs wherever a developer's shell does. `-quit` is
  # narrower still: it is not in every find that has `-maxdepth`.
  #
  # Each site was replaced by a prune of the depth it excluded — `./*/*/*` is
  # depth three and below — or, where the question was really "the files in this
  # one directory", by a glob. Both are POSIX, and each replacement was checked
  # to select the identical set.
  # `-e` rather than a bare pattern, because the patterns begin with `-`; and
  # ci/tests/ is excluded because this case and the equivalence check below both
  # have to name the flag in order to talk about it.
  local hits
  hits="$(grep -rn --include='*.sh' --exclude-dir=tests \
            -e '-maxdepth' -e '-print -quit' "$REPO_ROOT/ci" 2>/dev/null \
          | grep -v '^[^:]*:[0-9]*: *#' || true)"
  [ -z "$hits" ] \
    || { echo "a non-POSIX find flag is back in the gate:" >&2; echo "$hits" >&2; return 1; }

  # The equivalence the replacement rests on, asserted rather than assumed: a
  # prune of `./*/*/*` selects what `-maxdepth 2` selected.
  local sb; sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  local f
  for f in a.conf x/b.conf x/y/c.conf x/y/z/d.conf; do
    mkdir -p "$sb/$(dirname "$f")"; : > "$sb/$f"
  done
  local old new
  old="$(cd "$sb" && find . -maxdepth 2 -type f -name '*.conf' 2>/dev/null | sort | tr '\n' ' ')"
  new="$(cd "$sb" && find . -path './*/*/*' -prune -o -type f -name '*.conf' -print 2>/dev/null | sort | tr '\n' ' ')"
  rm -rf "$sb"
  [ "$old" = "$new" ] \
    || { echo "the portable form is not equivalent: maxdepth=[$old] prune=[$new]" >&2; return 1; }
}

@test "preflight: in ship mode the check toggles come from HEAD, not the worktree" {
  # These toggles decide which lanes run at all, and reading them from the
  # worktree let an *unstaged* edit switch lanes off for a push that does not
  # carry the edit. Setting every related check to false is enough on its own,
  # so _all_related_checks_disabled then drops the whole `node` lane as well,
  # and tests-shell is the lane that would have reported the config file as
  # drift.
  # A pushed frontend commit with failing tests passes on toggles that exist in
  # nobody's tree but the pusher's.
  local sb drv
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  drv="$(mktemp "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  {
    sed -n '/^_CHECKS_CONFIG_FILE=/,/^_CHECKS_CONFIG_TMP=""$/p' "$REPO_ROOT/ci/preflight.sh"
    # The library helper the resolver materializes HEAD's copy through.
    # preflight.sh sources ci/lib/common.sh before it runs; an extraction that
    # leaves it out makes the resolver fail on "command not found", fall back to
    # the worktree, and this case would then report the worktree's answer as
    # HEAD's -- which is exactly the confusion it exists to detect.
    sed -n '/^ci::common::mktemp_file() {/,/^}/p' "$REPO_ROOT/ci/lib/common.sh"
    sed -n '/^_checks_config_resolve()/,/^}/p' "$REPO_ROOT/ci/preflight.sh"
    sed -n '/^_check_disabled_in_config()/,/^}/p' "$REPO_ROOT/ci/preflight.sh"
  } > "$drv"
  bash -n "$drv" || { rm -rf "$sb" "$drv"; echo "the extraction is not valid shell" >&2; return 1; }

  mkdir -p "$sb/ci/config"
  cat > "$sb/ci/config/checks.yml" <<'YML'
checks:
  tests-shell:
    enabled: true
  tests-js:
    enabled: true
  typecheck-js:
    enabled: true
YML
  (
    cd "$sb"
    git init -q -b work .
    git config user.email t@t
    git config user.name t
    git add -A
    git commit -qm base
  ) >/dev/null 2>&1
  # The unstaged edit, present only on disk.
  cat > "$sb/ci/config/checks.yml" <<'YML'
checks:
  tests-shell:
    enabled: false
  tests-js:
    enabled: false
  typecheck-js:
    enabled: false
YML

  # A helper that reports each toggle under a given MODE.
  _n_toggles() {
    (
      cd "$sb" || exit 1
      MODE="$1"
      # shellcheck disable=SC1090
      . "$drv"
      local c
      for c in tests-shell tests-js typecheck-js; do
        if _check_disabled_in_config "$c"; then printf '%s=OFF ' "$c"; else printf '%s=ON ' "$c"; fi
      done
    ) 2>/dev/null
  }

  local ship quick
  ship="$(_n_toggles ship)"
  [[ "$ship" == *"tests-shell=ON"* ]] && [[ "$ship" == *"tests-js=ON"* ]] \
    && [[ "$ship" == *"typecheck-js=ON"* ]] \
    || { rm -rf "$sb" "$drv"; echo "an unstaged edit switched ship-mode lanes off: [$ship]" >&2; return 1; }

  # `full` and `quick` are deliberately whole-tree runs against the worktree and
  # keep reading it -- the rule is about what a *push* is measured against, and
  # asserting it here stops the fix widening into modes it was not for.
  quick="$(_n_toggles quick)"
  [[ "$quick" == *"tests-shell=OFF"* ]] \
    || { rm -rf "$sb" "$drv"; echo "quick mode stopped reading the worktree: [$quick]" >&2; return 1; }

  # Committing the same edit makes it HEAD's, and then ship mode honours it --
  # the control that keeps this from becoming "ignore the configuration".
  ( cd "$sb" && git add -A && git commit -qm 'disable the lanes' ) >/dev/null 2>&1
  ship="$(_n_toggles ship)"
  [[ "$ship" == *"tests-shell=OFF"* ]] \
    || { rm -rf "$sb" "$drv"; echo "a committed toggle was ignored: [$ship]" >&2; return 1; }

  # And an unreadable HEAD copy runs every lane rather than falling back to the
  # worktree: an empty configuration disables nothing, so the failure direction
  # is more work, not less.
  # `git rm` of the last file in ci/config/ takes the directory with it, so the
  # worktree copy has to be re-created before it can be written.
  ( cd "$sb" && git rm -q ci/config/checks.yml && git commit -qm 'drop the config' ) >/dev/null 2>&1
  mkdir -p "$sb/ci/config"
  cat > "$sb/ci/config/checks.yml" <<'YML'
checks:
  tests-shell:
    enabled: false
YML
  ship="$(_n_toggles ship)"
  rm -rf "$sb" "$drv"
  [[ "$ship" == *"tests-shell=ON"* ]] \
    || { echo "an unreadable HEAD copy fell back to the worktree: [$ship]" >&2; return 1; }
}

@test "tests-shell: a fresh clone has a way to satisfy the blocker it just enabled" {
  # The refusal itself is right and stays: a lane that reports PASS without
  # running these suites leaves the layout, node and changeset gates unguarded.
  # What was missing was the other half. `uv sync` does not provision bats and
  # neither did ci/install-hooks.sh, so a fresh clone that followed the
  # documented setup had every push touching ci/ blocked -- with the refusal
  # arriving at push time and naming no remedy that exists in this repository.
  local sb; sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib" "$sb/ci/tests" "$sb/.ci-gate/bats/bin"
  cp "$REPO_ROOT/ci/checks/tests-shell.sh" "$REPO_ROOT/ci/checks/tests.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT"/ci/lib/*.sh "$sb/ci/lib/"
  printf '@test "x" { true; }\n' > "$sb/ci/tests/a.bats"

  # A PATH with no bats on it. Set explicitly rather than filtered, because
  # "which entries of the caller's PATH contain a bats" is a question with a
  # different answer on every machine this runs on.
  local clean="/usr/bin:/bin"
  run bash -c "cd '$sb' && PATH='$clean' bash ci/checks/tests-shell.sh 2>&1"
  [ "$status" -eq 30 ] \
    || { rm -rf "$sb"; echo "a missing bats was not FAIL_INFRA (status=$status)" >&2; echo "$output" >&2; return 1; }
  [[ "$output" == *"make bats-install"* ]] \
    || { rm -rf "$sb"; echo "the refusal names no in-repo remedy: $output" >&2; return 1; }

  # And the remedy takes effect without anyone exporting PATH by hand, which is
  # the half that makes it a fix rather than a message.
  printf '#!/usr/bin/env bash\nexit 0\n' > "$sb/.ci-gate/bats/bin/bats"
  chmod +x "$sb/.ci-gate/bats/bin/bats"
  run bash -c "cd '$sb' && PATH='$clean' bash ci/checks/tests-shell.sh 2>&1"
  [[ "$output" != *"bats is not installed"* ]] \
    || { rm -rf "$sb"; echo "a provisioned .ci-gate/bats was not found: $output" >&2; return 1; }
  rm -rf "$sb"

  # The installer the message names has to exist and be runnable, or the remedy
  # is a string. Not executed here -- it clones from the network -- but its
  # syntax and its pin are asserted.
  [ -f "$REPO_ROOT/ci/scripts/install-bats.sh" ] \
    || { echo "ci/scripts/install-bats.sh is missing" >&2; return 1; }
  bash -n "$REPO_ROOT/ci/scripts/install-bats.sh" \
    || { echo "the installer is not valid shell" >&2; return 1; }
  grep -qE '^BATS_VERSION="\$\{BATS_VERSION:-v[0-9]+\.[0-9]+\.[0-9]+\}"' \
    "$REPO_ROOT/ci/scripts/install-bats.sh" \
    || { echo "the installer does not pin a bats version" >&2; return 1; }

  # `make bats-install` is what the refusal, the hook installer and the docs all
  # tell the reader to run, so the target has to be there.
  grep -q '^bats-install:' "$REPO_ROOT/Makefile" \
    || { echo "the Makefile has no bats-install target" >&2; return 1; }

  # And ci/install-hooks.sh says it at the moment it makes the hook live, which
  # is the difference between finding out at setup and finding out at push.
  grep -q 'make bats-install' "$REPO_ROOT/ci/install-hooks.sh" \
    || { echo "install-hooks.sh does not mention the prerequisite" >&2; return 1; }
}
