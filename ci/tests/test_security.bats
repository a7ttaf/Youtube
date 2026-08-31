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
    CI_GATE_PUSH_REMOTE_REFS=refs/tags/security-tag \
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
    CI_GATE_PUSH_REMOTE_REFS=refs/tags/outer-security-tag \
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
    CI_GATE_PUSH_REMOTE_REFS=refs/tags/lightweight-security \
    CI_GATE_PUSH_TAG_TIPS='$tag_tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in tag-transient.txt"* ]]
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
    CI_GATE_PUSH_REMOTE_REFS=refs/notes/security \
    CI_GATE_PUSH_OTHER_TIPS='$tip' bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 20 ]
  [[ "$output" == *"Potential secret-like content in other-transient.txt"* ]]
}

@test "security: hook ref names without object tips fail closed" {
  run bash -c "cd '$SECURITY_SB' && CI_GATE_PUSH_REMOTE_REFS=refs/tags/missing-tip \
    bash ci/checks/security.sh 2>&1"

  [ "$status" -eq 30 ]
  [[ "$output" == *"without their object tips"* ]]
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
