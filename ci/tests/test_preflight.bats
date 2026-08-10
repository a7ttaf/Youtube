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

  gs_commit "drop secret"
  run gs_run ship
  [ "$status" -eq 0 ]
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
