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
