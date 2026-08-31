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

security_secret_line() {
  # Assemble the fixture so this test file is not itself a scanner match.
  printf 'DATABASE%s%s\n' '_URL=' 'postgresql://example.invalid/transient'
}

security_binary_secret() {
  printf '\0'
  security_secret_line
}

security_github_token() {
  printf 'ghp_'
  printf 'A%.0s' {1..36}
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

@test "security: modifying a file does not rescan unchanged historical content" {
  security_secret_line > "$SECURITY_SB/historical-pattern.txt"
  security_commit historical-pattern
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  printf 'safe addition\n' >> "$SECURITY_SB/historical-pattern.txt"
  security_commit safe-addition
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" != *"historical-pattern.txt"* ]]
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

@test "security: sensitive path policy is ASCII case-insensitive" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  for path in .ENV config.Env SECRET.PEM ID_RSA; do
    printf 'safe placeholder\n' > "$SECURITY_SB/$path"
  done
  security_commit mixed-case-sensitive-paths
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  for path in .ENV config.Env SECRET.PEM ID_RSA; do
    [[ "$output" == *"Sensitive file path detected: $path"* ]]
  done
}

@test "security: common pattern source cannot suppress a real credential line" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  leaked_value="$(security_secret_line)"
  printf "LEAK_VALUE='%s' # SECRET_PATTERN=\n" "$leaked_value" \
    >> "$SECURITY_SB/ci/checks/common.sh"
  (
    cd "$SECURITY_SB"
    git add -f ci/checks/common.sh
  )
  security_commit common-suppression-secret
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in ci/checks/common.sh"* ]]
}

@test "security: only the canonical common pattern record is self-exempt" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  (
    cd "$SECURITY_SB"
    git add -f ci/checks/common.sh
  )
  security_commit canonical-pattern-source
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" != *"ci/checks/common.sh"* ]]
}

@test "security: committed history catches a secret deleted before the tip" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  security_secret_line > "$SECURITY_SB/transient.txt"
  security_commit add-transient-secret
  rm "$SECURITY_SB/transient.txt"
  security_commit delete-transient-secret
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in transient.txt"* ]]
}

@test "security: committed range scans a leading-NUL blob as text" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  security_binary_secret > "$SECURITY_SB/committed-nul.bin"
  security_commit committed-nul-secret
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in committed-nul.bin"* ]]
}

@test "security: committed binary modification scans introduced NUL content" {
  printf '\0safe binary baseline\n' > "$SECURITY_SB/modified-nul.bin"
  security_commit binary-baseline
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  security_binary_secret >> "$SECURITY_SB/modified-nul.bin"
  security_commit binary-secret
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in modified-nul.bin"* ]]
}

@test "security: committed leading-dash path bytes are scanned for secrets" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  token="$(security_github_token)"
  secret_path="-$token"
  printf 'safe blob\n' > "$SECURITY_SB/$secret_path"
  security_commit committed-secret-path
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in path: [redacted-secret-like-path]"* ]]
  [[ "$output" != *"$token"* ]]
}

@test "security: newly reachable commit subject bytes are scanned" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  token="$(security_github_token)"
  (
    cd "$SECURITY_SB"
    printf 'subject metadata change\n' >> safe.txt
    git add safe.txt
    git -c user.email=t@t -c user.name=t commit -qm "$token"
  )
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in commit-metadata-"* ]]
}

@test "security: newly reachable author and email bytes are scanned" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  token="$(security_github_token)"
  (
    cd "$SECURITY_SB"
    printf 'author metadata change\n' >> safe.txt
    git add safe.txt
    GIT_AUTHOR_NAME="$token" GIT_AUTHOR_EMAIL="$token@example.invalid" \
      git -c user.email=t@t -c user.name=t commit -qm safe-author-metadata
  )
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in commit-metadata-"* ]]
}

@test "security: non-ancestor update scans every newly reachable commit" {
  common="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  (
    cd "$SECURITY_SB"
    git switch -q -c destination-old
    printf 'destination only\n' > old.txt
    git add old.txt
    git -c user.email=t@t -c user.name=t commit -qm destination-old
  )
  old_tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  (
    cd "$SECURITY_SB"
    git switch -q feature/security
  )
  [ "$(cd "$SECURITY_SB" && git rev-parse HEAD)" = "$common" ]
  security_secret_line > "$SECURITY_SB/new-lineage.txt"
  security_commit new-lineage-secret
  new_tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$old_tip' \
    CI_GATE_PUSH_NEW_SHA='$new_tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in new-lineage.txt"* ]]
}

@test "security: force rollback scans bytes resurrected at the destination" {
  security_secret_line > "$SECURITY_SB/resurrected.txt"
  security_commit add-secret
  secret_tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  rm "$SECURITY_SB/resurrected.txt"
  security_commit delete-secret
  old_tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  (
    cd "$SECURITY_SB"
    git switch -q --detach "$secret_tip"
  )

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$old_tip' \
    CI_GATE_PUSH_NEW_SHA='$secret_tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in resurrected.txt"* ]]
}

@test "security: merge scans newly reachable parent history" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  (
    cd "$SECURITY_SB"
    git switch -q -c topic/security
  )
  security_secret_line > "$SECURITY_SB/merged-history.txt"
  security_commit topic-secret
  (
    cd "$SECURITY_SB"
    git switch -q feature/security
    printf 'first parent\n' >> safe.txt
    git add safe.txt
    git -c user.email=t@t -c user.name=t commit -qm first-parent
    git -c user.email=t@t -c user.name=t merge -q --no-ff topic/security -m merge-topic
  )
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in merged-history.txt"* ]]
}

@test "security: merge scans resolution-only committed bytes" {
  (
    cd "$SECURITY_SB"
    printf 'base\n' > conflict.txt
    git add conflict.txt
    git -c user.email=t@t -c user.name=t commit -qm conflict-base
  )
  range_base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  (
    cd "$SECURITY_SB"
    git switch -q -c topic/resolution
    printf 'topic\n' > conflict.txt
    git add conflict.txt
    git -c user.email=t@t -c user.name=t commit -qm topic-change
    git switch -q feature/security
    printf 'first parent\n' > conflict.txt
    git add conflict.txt
    git -c user.email=t@t -c user.name=t commit -qm first-parent-change
    git -c user.email=t@t -c user.name=t merge -q --no-ff topic/resolution -m merge-resolution \
      >/dev/null 2>&1 || true
  )
  security_secret_line > "$SECURITY_SB/conflict.txt"
  security_commit resolution-secret
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$range_base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in conflict.txt"* ]]
}

@test "security: a root file named dash is never treated as grep stdin" {
  security_secret_line > "$SECURITY_SB/-"
  (
    cd "$SECURITY_SB"
    git add -- -
  )

  run bash -c "cd '$SECURITY_SB' && bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in -"* ]]
}

@test "security: committed range scans the blob for a root file named dash" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  security_secret_line > "$SECURITY_SB/-"
  security_commit committed-dash
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in -"* ]]
}

@test "security: staged bytes cannot be hidden by a safe worktree rewrite" {
  security_secret_line > "$SECURITY_SB/staged-only.txt"
  (
    cd "$SECURITY_SB"
    git add staged-only.txt
  )
  printf 'safe worktree replacement\n' > "$SECURITY_SB/staged-only.txt"

  run bash -c "cd '$SECURITY_SB' && bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in staged-only.txt"* ]]
}

@test "security: staged leading-NUL blob is scanned as text" {
  security_binary_secret > "$SECURITY_SB/staged-nul.bin"
  (
    cd "$SECURITY_SB"
    git add staged-nul.bin
  )

  run bash -c "cd '$SECURITY_SB' && bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in staged-nul.bin"* ]]
}

@test "security: staged path bytes are scanned for secrets" {
  token="$(security_github_token)"
  secret_path="staged-$token"
  printf 'safe blob\n' > "$SECURITY_SB/$secret_path"
  (
    cd "$SECURITY_SB"
    git add -- "$secret_path"
  )

  run bash -c "cd '$SECURITY_SB' && bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in path: [redacted-secret-like-path]"* ]]
  [[ "$output" != *"$token"* ]]
}

@test "security: newline secret path is rejected or detected without line parsing" {
  token="$(security_github_token)"
  newline_path=$'line\n-'"$token"
  printf 'safe blob\n' > "$SECURITY_SB/$newline_path"
  run bash -c 'cd "$1" && git add -- "$2"' _ "$SECURITY_SB" "$newline_path"
  if [ "$status" -ne 0 ]; then
    [[ "$output" == *"Invalid path"* || "$output" == *"invalid path"* \
      || "$output" == *"pathspec"* ]]
    return 0
  fi

  run bash -c "cd '$SECURITY_SB' && bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in path:"* ]]
}

@test "security: a zero destination scans the complete new history" {
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && \
    CI_GATE_PUSH_OLD_SHA=0000000000000000000000000000000000000000 \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Sensitive file path detected: legacy.env"* ]]
  [[ "$output" != *"ci/checks/common.sh"* ]]
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
