#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  SECURITY_SB="$(mktemp -d "${TMPDIR:-/tmp}/ums-security-bats.XXXXXX")"
  mkdir -p "$SECURITY_SB/ci/checks" "$SECURITY_SB/ci/lib"
  cp "$REPO_ROOT/ci/checks/security.sh" "$REPO_ROOT/ci/checks/common.sh" \
    "$SECURITY_SB/ci/checks/"
  cp "$REPO_ROOT/ci/hook-dispatch.sh" "$SECURITY_SB/ci/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$SECURITY_SB/ci/lib/"
  cat > "$SECURITY_SB/ci/preflight.sh" <<'SH'
#!/usr/bin/env bash
exec ci/checks/security.sh
SH
  chmod +x "$SECURITY_SB/ci/preflight.sh"
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

security_utf16_secret() {
  local encoding="$1" bom="${2:-no-bom}"
  case "$encoding:$bom" in
    UTF-16LE:bom) printf '\377\376' ;;
    UTF-16BE:bom) printf '\376\377' ;;
  esac
  security_secret_line | iconv -f UTF-8 -t "$encoding"
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

@test "security: replacement refs cannot substitute safe bytes for the pushed object" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  security_secret_line > "$SECURITY_SB/replaced.txt"
  security_commit secret-object
  secret_tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  rm "$SECURITY_SB/replaced.txt"
  security_commit safe-replacement
  safe_tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  (
    cd "$SECURITY_SB"
    unset GIT_NO_REPLACE_OBJECTS
    git replace "$secret_tip" "$safe_tip"
    # Populate a status-clean index/worktree from the safe replacement while
    # HEAD still names the original credential-bearing commit.
    git reset -q --hard "$secret_tip"
    git init -q --bare remote.git
    git status --porcelain > .replacement-status
  )
  [ ! -s "$SECURITY_SB/.replacement-status" ]

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$secret_tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -ne 0 ]
  [[ "$output" == *"clean tracked worktree and index"* || "$output" == *"replaced.txt"* ]]

  # Git push publishes the original object, not the local replacement view.
  run bash -c "cd '$SECURITY_SB' && git push -q --no-verify \
    '$SECURITY_SB/remote.git' HEAD:refs/heads/leaked && \
    GIT_NO_REPLACE_OBJECTS=1 git --git-dir='$SECURITY_SB/remote.git' \
      show refs/heads/leaked:replaced.txt"
  [ "$status" -eq 0 ]
  [[ "$output" == *"example.invalid/transient"* ]]
}

@test "security: a custom replacement namespace cannot redirect object traversal" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  security_secret_line > "$SECURITY_SB/custom-replaced.txt"
  security_commit custom-secret-object
  secret_tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  rm "$SECURITY_SB/custom-replaced.txt"
  security_commit custom-safe-replacement
  safe_tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  (
    cd "$SECURITY_SB"
    unset GIT_NO_REPLACE_OBJECTS
    git update-ref "refs/custom-replace/$secret_tip" "$safe_tip"
    GIT_REPLACE_REF_BASE=refs/custom-replace git reset -q --hard "$secret_tip"
  )

  run bash -c "cd '$SECURITY_SB' && GIT_REPLACE_REF_BASE=refs/custom-replace \
    CI_GATE_PUSH_OLD_SHA='$base' CI_GATE_PUSH_NEW_SHA='$secret_tip' \
    bash ci/checks/security.sh 2>&1"

  [ "$status" -ne 0 ]
  [[ "$output" == *"clean tracked worktree and index"* || "$output" == *"custom-replaced.txt"* ]]
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

@test "security: committed UTF-16 BOM and BOM-less blobs are normalized" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  security_utf16_secret UTF-16LE bom > "$SECURITY_SB/utf16le-bom.bin"
  security_utf16_secret UTF-16BE > "$SECURITY_SB/utf16be-no-bom.bin"
  security_commit committed-utf16-secrets
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in utf16le-bom.bin"* ]]
  [[ "$output" == *"Potential secret-like content in utf16be-no-bom.bin"* ]]
}

@test "security: a UTF-16 blob modification scans only normalized additions" {
  printf 'safe baseline\n' | iconv -f UTF-8 -t UTF-16BE > "$SECURITY_SB/utf16-modified.bin"
  security_commit utf16-safe-baseline
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  {
    printf 'safe baseline\n'
    security_secret_line
  } | iconv -f UTF-8 -t UTF-16BE > "$SECURITY_SB/utf16-modified.bin"
  security_commit utf16-secret-addition
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OLD_SHA='$base' \
    CI_GATE_PUSH_NEW_SHA='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in utf16-modified.bin"* ]]
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

@test "security: committed empty-tree path bytes are scanned" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  token="$(security_github_token)"
  (
    cd "$SECURITY_SB"
    empty_tree="$(git mktree </dev/null)"
    root_tree="$({ git ls-tree HEAD; printf '040000 tree %s\t%s\n' "$empty_tree" "$token"; } | git mktree)"
    tip="$(printf 'add empty tree\n' | \
      git -c user.email=t@t -c user.name=t commit-tree "$root_tree" -p HEAD)"
    git reset -q --hard "$tip"
  )
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

@test "security: staged UTF-16 LE and BE blobs are normalized" {
  security_utf16_secret UTF-16LE > "$SECURITY_SB/staged-utf16le.bin"
  security_utf16_secret UTF-16BE bom > "$SECURITY_SB/staged-utf16be-bom.bin"
  (
    cd "$SECURITY_SB"
    git add staged-utf16le.bin staged-utf16be-bom.bin
  )

  run bash -c "cd '$SECURITY_SB' && bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in staged-utf16le.bin"* ]]
  [[ "$output" == *"Potential secret-like content in staged-utf16be-bom.bin"* ]]
}

@test "security: unstaged worktree UTF-16 blob is normalized" {
  security_utf16_secret UTF-16LE bom > "$SECURITY_SB/safe.txt"

  run bash -c "cd '$SECURITY_SB' && bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in safe.txt"* ]]
}

@test "security: malformed BOM-declared UTF-16 fails closed" {
  printf '\377\376\000' > "$SECURITY_SB/malformed-utf16.bin"
  (
    cd "$SECURITY_SB"
    git add malformed-utf16.bin
  )

  run bash -c "cd '$SECURITY_SB' && bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 30 ]
  [[ "$output" == *"UTF-16LE"* ]]
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

@test "security: annotated tag message bytes are scanned in tag-only context" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  tag_message="$(security_secret_line)"
  (
    cd "$SECURITY_SB"
    git -c user.email=t@t -c user.name=t tag -a security-tag -m "$tag_message" HEAD
  )
  tag_oid="$(cd "$SECURITY_SB" && git rev-parse refs/tags/security-tag)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_REMOTE=origin \
    CI_GATE_PUSH_REMOTE_TIPS_FOR=origin CI_GATE_PUSH_REMOTE_TIPS='$base' \
    CI_GATE_PUSH_OUTGOING_REFS=refs/tags/security-tag \
    CI_GATE_PUSH_TAG_TIPS='$tag_oid' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in tag-metadata-$tag_oid"* ]]
}

@test "security: nested annotated tag chain scans inner metadata" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  inner_message="$(security_secret_line)"
  (
    cd "$SECURITY_SB"
    git -c user.email=t@t -c user.name=t tag -a inner-security-tag -m "$inner_message" HEAD
    git -c user.email=t@t -c user.name=t tag -a outer-security-tag -m safe-tag inner-security-tag
  )
  inner_oid="$(cd "$SECURITY_SB" && git rev-parse refs/tags/inner-security-tag)"
  outer_oid="$(cd "$SECURITY_SB" && git rev-parse refs/tags/outer-security-tag)"
  [ "$(cd "$SECURITY_SB" && git cat-file tag "$outer_oid" | sed -n '2p')" = "type tag" ]

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_REMOTE=origin \
    CI_GATE_PUSH_REMOTE_TIPS_FOR=origin CI_GATE_PUSH_REMOTE_TIPS='$base' \
    CI_GATE_PUSH_OUTGOING_REFS=refs/tags/outer-security-tag \
    CI_GATE_PUSH_TAG_TIPS='$outer_oid' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in tag-metadata-$inner_oid"* ]]
}

@test "security: lightweight tag scans unpublished add-delete history" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  security_secret_line > "$SECURITY_SB/tag-transient.txt"
  security_commit tag-add-secret
  rm "$SECURITY_SB/tag-transient.txt"
  security_commit tag-delete-secret
  (
    cd "$SECURITY_SB"
    git tag lightweight-security HEAD
  )
  tag_tip="$(cd "$SECURITY_SB" && git rev-parse refs/tags/lightweight-security)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_REMOTE=origin \
    CI_GATE_PUSH_REMOTE_TIPS_FOR=origin CI_GATE_PUSH_REMOTE_TIPS='$base' \
    CI_GATE_PUSH_OUTGOING_REFS=refs/tags/lightweight-security \
    CI_GATE_PUSH_TAG_TIPS='$tag_tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in tag-transient.txt"* ]]
}

@test "security: label-only tag does not rescan destination-reachable history" {
  security_secret_line > "$SECURITY_SB/already-published-secret.txt"
  security_commit published-add-secret
  secret_tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  rm "$SECURITY_SB/already-published-secret.txt"
  security_commit published-delete-secret
  remote_tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_REMOTE=origin \
    CI_GATE_PUSH_REMOTE_TIPS_FOR=origin CI_GATE_PUSH_REMOTE_TIPS='$remote_tip' \
    CI_GATE_PUSH_OUTGOING_REFS=refs/tags/old-published-state \
    CI_GATE_PUSH_TAG_TIPS='$secret_tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Security checks passed."* ]]
}

@test "security: other-ref tips scan unpublished reachable history" {
  base="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  security_secret_line > "$SECURITY_SB/other-transient.txt"
  security_commit other-add-secret
  rm "$SECURITY_SB/other-transient.txt"
  security_commit other-delete-secret
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_REMOTE=origin \
    CI_GATE_PUSH_REMOTE_TIPS_FOR=origin CI_GATE_PUSH_REMOTE_TIPS='$base' \
    CI_GATE_PUSH_OUTGOING_REFS=refs/publish/security \
    CI_GATE_PUSH_OTHER_TIPS='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in other-transient.txt"* ]]
}

@test "security: hook ref names without object tips fail closed" {
  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_OUTGOING_REFS=refs/tags/missing-tip \
    bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 30 ]
  [[ "$output" == *"without their object tips"* ]]
}

@test "security: actual notes-only hook context scans note objects" {
  note_message="$(security_secret_line)"
  (
    cd "$SECURITY_SB"
    git -c user.email=t@t -c user.name=t notes --ref=commits add -m "$note_message" HEAD
  )
  note_tip="$(cd "$SECURITY_SB" && git rev-parse refs/notes/commits)"
  zero=0000000000000000000000000000000000000000

  run bash -c "cd '$SECURITY_SB' && printf 'refs/notes/commits %s refs/notes/commits %s\\n' \
    '$note_tip' '$zero' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in"* ]]
}

@test "security: notes rollback scans the complete visible note state" {
  note_message="$(security_secret_line)"
  (
    cd "$SECURITY_SB"
    git -c user.email=t@t -c user.name=t notes --ref=commits add -m "$note_message" HEAD
    other_object="$(printf 'other object\n' | git hash-object -w --stdin)"
    git -c user.email=t@t -c user.name=t notes --ref=commits add -m safe-note "$other_object"
  )
  secret_tip="$(cd "$SECURITY_SB" && git rev-parse refs/notes/commits)"
  (
    cd "$SECURITY_SB"
    git -c user.email=t@t -c user.name=t notes --ref=commits remove HEAD
    git init -q --bare remote.git
    git remote add origin "$SECURITY_SB/remote.git"
    git push -q origin refs/notes/commits
  )
  remote_tip="$(cd "$SECURITY_SB" && git rev-parse refs/notes/commits)"
  (cd "$SECURITY_SB" && git update-ref refs/notes/commits "$secret_tip")

  run bash -c "cd '$SECURITY_SB' && printf 'refs/notes/commits %s refs/notes/commits %s\\n' \
    '$secret_tip' '$remote_tip' | bash ci/hook-dispatch.sh pre-push origin '$SECURITY_SB/remote.git' 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in"* ]]
}

@test "security: notes state scans empty-tree path bytes" {
  token="$(security_github_token)"
  (
    cd "$SECURITY_SB"
    empty_tree="$(git mktree </dev/null)"
    secret_tree="$(printf '040000 tree %s\t%s\n' "$empty_tree" "$token" | git mktree)"
    secret_tip="$(printf 'secret notes state\n' | \
      git -c user.email=t@t -c user.name=t commit-tree "$secret_tree")"
    safe_tree="$(git mktree </dev/null)"
    remote_tip="$(printf 'safe notes descendant\n' | \
      git -c user.email=t@t -c user.name=t commit-tree "$safe_tree" -p "$secret_tip")"
    printf '%s %s\n' "$secret_tip" "$remote_tip" > .note-state-shas
  )
  read -r secret_tip remote_tip < "$SECURITY_SB/.note-state-shas"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_REMOTE=origin \
    CI_GATE_PUSH_REMOTE_TIPS_FOR=origin CI_GATE_PUSH_REMOTE_TIPS='$remote_tip' \
    CI_GATE_PUSH_OUTGOING_REFS=refs/notes/commits \
    CI_GATE_PUSH_NOTES_TIPS='$secret_tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in path: [redacted-secret-like-path]"* ]]
  [[ "$output" != *"$token"* ]]
}

@test "security: actual hook scans secret-like non-branch ref names" {
  token="$(security_github_token)"
  outgoing_ref="refs/tags/release-$token"
  tip="$(cd "$SECURITY_SB" && git rev-parse HEAD)"
  zero=0000000000000000000000000000000000000000
  (
    cd "$SECURITY_SB"
    git init -q --bare remote.git
    git remote add origin "$SECURITY_SB/remote.git"
    git push -q origin HEAD:refs/heads/main
  )

  run bash -c "cd '$SECURITY_SB' && printf '%s %s %s %s\\n' \
    '$outgoing_ref' '$tip' '$outgoing_ref' '$zero' | \
    bash ci/hook-dispatch.sh pre-push origin '$SECURITY_SB/remote.git' 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in outgoing ref name: [redacted-secret-like-ref]"* ]]
  [[ "$output" != *"$token"* ]]
}

@test "security: real label-only preflight runs the ref-object scan" {
  plan_sb="$BATS_TEST_TMPDIR/label-only-plan"
  mkdir -p "$plan_sb"
  cp -R "$REPO_ROOT/ci" "$plan_sb/ci"
  rm -rf "$plan_sb/ci/reports" "$plan_sb/ci/artifacts"
  local check_file check_name
  for check_file in "$plan_sb"/ci/checks/*.sh; do
    check_name="$(basename "$check_file")"
    case "$check_name" in
      common.sh|security.sh) continue ;;
    esac
    printf '#!/usr/bin/env bash\nexit 0\n' > "$check_file"
    chmod +x "$check_file"
  done
  (
    cd "$plan_sb"
    git init -q -b main .
    printf 'safe\n' > safe.txt
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm baseline
    git init -q --bare remote.git
    git remote add origin "$plan_sb/remote.git"
    git push -q origin HEAD:refs/heads/main
    tag_message="$(security_secret_line)"
    git -c user.email=t@t -c user.name=t tag -a release-secret -m "$tag_message" HEAD
  )
  tag_oid="$(cd "$plan_sb" && git rev-parse refs/tags/release-secret)"
  zero=0000000000000000000000000000000000000000

  run bash -c "cd '$plan_sb' && printf 'refs/tags/release-secret %s refs/tags/release-secret %s\\n' \
    '$tag_oid' '$zero' | CI_GATE_USE_LANES=0 bash ci/hook-dispatch.sh \
    pre-push origin '$plan_sb/remote.git' 2>&1"

  [ "$status" -ne 0 ]
  [[ "$output" == *"Result [security]: FAIL_NEW_ISSUE"* ]]
  [[ "$output" == *"Potential secret-like content in tag-metadata-$tag_oid"* ]]
}

@test "security: empty hook ref lists fall back to the checked-out tree" {
  security_secret_line > "$SECURITY_SB/safe.txt"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_REMOTE_REFS= \
    CI_GATE_PUSH_BRANCH_TIPS= CI_GATE_PUSH_TAG_TIPS= CI_GATE_PUSH_OTHER_TIPS= \
    CI_GATE_PUSH_NOTES_TIPS= CI_GATE_PUSH_OUTGOING_REFS= \
    bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in safe.txt"* ]]
}

@test "security: explicit content-free hook context does not scan the checkout" {
  security_secret_line > "$SECURITY_SB/safe.txt"
  secret_ref="$(security_github_token)"

  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_REMOTE_REFS='$secret_ref' \
    CI_GATE_PUSH_BRANCH_TIPS= CI_GATE_PUSH_TAG_TIPS= CI_GATE_PUSH_OTHER_TIPS= \
    CI_GATE_PUSH_NOTES_TIPS= CI_GATE_PUSH_OUTGOING_REFS= \
    CI_GATE_PUSH_DELETIONS_ONLY=1 bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Security checks passed."* ]]
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
