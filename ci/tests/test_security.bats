#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  SECURITY_SB="$(mktemp -d "${TMPDIR:-/tmp}/ums-security-bats.XXXXXX")"
  mkdir -p "$SECURITY_SB/ci/checks" "$SECURITY_SB/ci/lib"
  cp "$REPO_ROOT/ci/checks/security.sh" "$REPO_ROOT/ci/checks/common.sh" \
    "$SECURITY_SB/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$SECURITY_SB/ci/lib/"
  (
    cd "$SECURITY_SB"
    git init -q -b feature/security .
    printf 'ci/\n' > .gitignore
    printf 'historical placeholder\n' > legacy.env
    printf 'safe\n' > safe.txt
    git add .gitignore legacy.env safe.txt
    git -c user.email=t@t -c user.name=t commit -qm baseline
  )
}

teardown() {
  rm -rf "$SECURITY_SB"
}

security_commit() {
  (
    cd "$SECURITY_SB"
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm "$1"
  )
}

@test "security: committed range ignores an unchanged historical finding" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  printf 'changed safely\n' >> "$SECURITY_SB/safe.txt"
  security_commit safe-change
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" != *"legacy.env"* ]]
}

@test "security: committed range rejects a newly added sensitive path" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  printf 'placeholder\n' > "$SECURITY_SB/new.env"
  security_commit sensitive-path
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Sensitive file path detected: new.env"* ]]
}

@test "security: an invalid committed range fails closed" {
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA=missing-base \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 30 ]
  [[ "$output" == *"base is missing or unreadable"* ]]
}

@test "security: an empty explicit range does not rescan historical debt" {
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$tip' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" != *"legacy.env"* ]]
}

@test "security: explicit committed range requires checked-out clean bytes" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  printf 'changed safely\n' >> "$SECURITY_SB/safe.txt"
  security_commit safe-change
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  printf 'local drift\n' >> "$SECURITY_SB/safe.txt"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 30 ]
  [[ "$output" == *"requires a clean tracked worktree and index"* ]]
}
