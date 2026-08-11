#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
}

@test "hook: hook-dispatch.sh exists and is executable" {
  [ -f "$REPO_ROOT/ci/hook-dispatch.sh" ]
  [ -x "$REPO_ROOT/ci/hook-dispatch.sh" ]
  bash -n "$REPO_ROOT/ci/hook-dispatch.sh"
}

@test "hook: pre-commit hook has correct syntax" {
  [ -f "$REPO_ROOT/.githooks/pre-commit" ]
  [ -x "$REPO_ROOT/.githooks/pre-commit" ]
  bash -n "$REPO_ROOT/.githooks/pre-commit"
}

@test "hook: commit-msg hook has correct syntax" {
  [ -f "$REPO_ROOT/.githooks/commit-msg" ]
  [ -x "$REPO_ROOT/.githooks/commit-msg" ]
  bash -n "$REPO_ROOT/.githooks/commit-msg"
}

@test "hook: prepare-commit-msg hook has correct syntax" {
  [ -f "$REPO_ROOT/.githooks/prepare-commit-msg" ]
  [ -x "$REPO_ROOT/.githooks/prepare-commit-msg" ]
  bash -n "$REPO_ROOT/.githooks/prepare-commit-msg"
}

@test "hook: pre-push hook has correct syntax" {
  [ -f "$REPO_ROOT/.githooks/pre-push" ]
  [ -x "$REPO_ROOT/.githooks/pre-push" ]
  bash -n "$REPO_ROOT/.githooks/pre-push"
}

# --- the pre-push tip must not depend on the order git lists refs -------------

_hd_sandbox() {
  # A repo with two lineages: main..feature (related, feature ahead) and an
  # orphan branch sharing no history with either.
  HD_SB="$(mktemp -d)"
  mkdir -p "$HD_SB/ci/lib"
  cp "$REPO_ROOT/ci/hook-dispatch.sh" "$HD_SB/ci/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$HD_SB/ci/lib/"
  # preflight is replaced by a probe: the hook execs it, and what this asserts
  # on is the environment the hook hands over.
  cat > "$HD_SB/ci/preflight.sh" <<'SH'
#!/usr/bin/env bash
echo "NEW=${CI_GATE_PUSH_NEW_SHA:-}"
echo "OLD=${CI_GATE_PUSH_OLD_SHA:-}"
exit 0
SH
  chmod +x "$HD_SB/ci/preflight.sh"
  (
    cd "$HD_SB"
    git init -q -b main .
    printf 'a\n' > a.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c1
    HD_ROOT="$(git rev-parse HEAD)"
    printf 'b\n' > b.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c2
    HD_BASE="$(git rev-parse HEAD)"
    printf 'c\n' > c.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c3
    HD_TIP="$(git rev-parse HEAD)"
    git checkout -q --orphan orphan
    git rm -rqf . 2>/dev/null || true
    printf 'z\n' > z.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm orphan
    HD_ORPHAN="$(git rev-parse HEAD)"
    git checkout -q main
    printf '%s %s %s %s\n' "$HD_ROOT" "$HD_BASE" "$HD_TIP" "$HD_ORPHAN" > .shas
  ) >/dev/null 2>&1
  read -r HD_ROOT HD_BASE HD_TIP HD_ORPHAN < "$HD_SB/.shas"
}

@test "hook: the pre-push tip is the descendant, whatever order git lists refs" {
  # `_push_new="$_lsha"` on every record meant "whichever ref git listed last",
  # while the base beside it was widened by ancestry -- so the two halves of one
  # range described different pushes, and the answer changed with input order.
  _hd_sandbox
  # Real remote shas on both records, deliberately. A zero remote sha made the
  # old code hit its `break` and stop reading, which hides the ordering bug
  # behind a different one -- this case has to isolate the choice of tip.

  # Ancestor listed last: the tip must still be the descendant.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\nrefs/heads/old %s refs/heads/old %s\n' \
    '$HD_TIP' '$HD_ROOT' '$HD_BASE' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"NEW=$HD_TIP"* ]]

  # And the other way round, which is the order that used to work by luck.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/old %s refs/heads/old %s\nrefs/heads/main %s refs/heads/main %s\n' \
    '$HD_BASE' '$HD_ROOT' '$HD_TIP' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"NEW=$HD_TIP"* ]]

  # The base is the oldest remote sha across the records, whatever the order.
  [[ "$output" == *"OLD=$HD_ROOT"* ]]
  rm -rf "$HD_SB"
}

@test "hook: a new branch listed first does not stop the remaining refs being read" {
  # The zero remote sha used to `break`, so stdin stopped being read and any ref
  # after it was never seen -- the tip then depended on git's ordering again.
  _hd_sandbox
  local zero=0000000000000000000000000000000000000000
  run bash -c "cd '$HD_SB' && printf 'refs/heads/new %s refs/heads/new %s\nrefs/heads/main %s refs/heads/main %s\n' \
    '$HD_BASE' '$zero' '$HD_TIP' '$HD_BASE' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  # The later record was read: the tip is the descendant, not the first record.
  [[ "$output" == *"NEW=$HD_TIP"* ]]
  # And a ref with no base means the run as a whole has none.
  [[ "$output" == *"OLD="* ]]
  [[ "$output" != *"OLD=$HD_BASE"* ]]
  rm -rf "$HD_SB"
}

@test "hook: a push spanning unrelated histories is refused, not silently halved" {
  # One A..B range cannot describe two lineages with no common ancestor, and
  # picking either leaves the other unscanned.
  _hd_sandbox
  local zero=0000000000000000000000000000000000000000
  # The premise: these two really are unrelated.
  run bash -c "cd '$HD_SB' && git merge-base '$HD_TIP' '$HD_ORPHAN'"
  [ "$status" -ne 0 ]

  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\nrefs/heads/orphan %s refs/heads/orphan %s\n' \
    '$HD_TIP' '$zero' '$HD_ORPHAN' '$zero' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -ne 0 ]
  [[ "$output" == *"unrelated histories"* ]]
  [[ "$output" == *"orphan"* ]]
  # And it never reached the gate, so it cannot have reported on half the push.
  [[ "$output" != *"NEW="* ]]
  rm -rf "$HD_SB"
}
