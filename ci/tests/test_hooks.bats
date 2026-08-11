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
# `-` not `:-`, because set-and-empty is a different statement from unset and
# the check downstream is required to tell them apart.
echo "DEST=${CI_GATE_PUSH_REMOTE_REFS-<unset>}"
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
    # A fork: shares HD_BASE with main, but neither contains the other. This is
    # the case the "unrelated histories" wording described wrongly — there is a
    # perfectly good merge base here.
    git checkout -q -b fork "$HD_BASE"
    printf 'f\n' > f.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm fork1
    HD_FORK="$(git rev-parse HEAD)"
    git checkout -q main
    git checkout -q --orphan orphan
    git rm -rqf . 2>/dev/null || true
    printf 'z\n' > z.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm orphan
    HD_ORPHAN="$(git rev-parse HEAD)"
    git checkout -q main
    printf '%s %s %s %s %s\n' "$HD_ROOT" "$HD_BASE" "$HD_TIP" "$HD_ORPHAN" "$HD_FORK" > .shas
  ) >/dev/null 2>&1
  read -r HD_ROOT HD_BASE HD_TIP HD_ORPHAN HD_FORK < "$HD_SB/.shas"
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
  [[ "$output" == *"do not form one chain"* ]]
  [[ "$output" == *"orphan"* ]]
  # And it never reached the gate, so it cannot have reported on half the push.
  [[ "$output" != *"NEW="* ]]
  rm -rf "$HD_SB"
}

@test "hook: two diverged refs are refused, and the message says why" {
  # The wording mattered enough to be its own case. What the hook tests is
  # whether one ref *contains* the other; two branches forked from a shared
  # base fail that while having a perfectly good merge base, so a message
  # about "unrelated histories" sent people looking for a rootless history
  # that is not there.
  _hd_sandbox
  # The premise: these two do share a base, and neither contains the other.
  run bash -c "cd '$HD_SB' && git merge-base '$HD_TIP' '$HD_FORK'"
  [ "$status" -eq 0 ]
  [ "$output" = "$HD_BASE" ]
  run bash -c "cd '$HD_SB' && git merge-base --is-ancestor '$HD_TIP' '$HD_FORK'"
  [ "$status" -ne 0 ]
  run bash -c "cd '$HD_SB' && git merge-base --is-ancestor '$HD_FORK' '$HD_TIP'"
  [ "$status" -ne 0 ]

  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\nrefs/heads/fork %s refs/heads/fork %s\n' \
    '$HD_TIP' '$HD_ROOT' '$HD_FORK' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -ne 0 ]
  [[ "$output" == *"do not form one chain"* ]]
  [[ "$output" != *"unrelated histories"* ]]
  [[ "$output" != *"NEW="* ]]
  rm -rf "$HD_SB"
}

@test "hook: incomparable remote bases are refused, not collapsed to one" {
  # The tip was fixed by ancestry, and the base beside it was left choosing
  # "the older, else whichever arrived first". Two remote tips that are not
  # each other's ancestors therefore made the range order-dependent again --
  # A0..tip re-walks everything reachable from the discarded B0, so the gate
  # can block a push over a secret, an unsigned commit or a merge the remote
  # already has.
  _hd_sandbox
  # Local tips form one chain, so the tip selection cannot be what refuses it.
  run bash -c "cd '$HD_SB' && git merge-base --is-ancestor '$HD_BASE' '$HD_TIP'"
  [ "$status" -eq 0 ]
  # The two remote bases do not.
  run bash -c "cd '$HD_SB' && git merge-base --is-ancestor '$HD_FORK' '$HD_ROOT'"
  [ "$status" -ne 0 ]
  run bash -c "cd '$HD_SB' && git merge-base --is-ancestor '$HD_ROOT' '$HD_FORK'"
  [ "$status" -eq 0 ]

  # remote bases: HD_FORK and HD_TIP -- neither contains the other.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/a %s refs/heads/a %s\nrefs/heads/b %s refs/heads/b %s\n' \
    '$HD_TIP' '$HD_FORK' '$HD_TIP' '$HD_TIP' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -ne 0 ]
  [[ "$output" == *"do not form one chain"* ]]
  [[ "$output" != *"NEW="* ]]
  rm -rf "$HD_SB"
}

# --- the push destination, and a base that is merely unfetched ---------------

@test "hook: the destination ref is handed on, not the branch we stand on" {
  # A refspec separates "where the push is going" from "where the working tree
  # is". `git push origin feature:main` sends this hook refs/heads/main as the
  # remote ref while HEAD still says feature -- and the hook read _rref without
  # ever exporting it, so branch-protection.sh went on asking `git rev-parse
  # --abbrev-ref HEAD` and approved a direct push to main as an ordinary
  # feature-branch push.
  _hd_sandbox

  # Pushing the fork's content *to main*, while standing on main is irrelevant:
  # what matters is that the destination is what gets reported.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/fork %s refs/heads/main %s\n' \
    '$HD_FORK' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DEST=main"* ]]

  # A deletion carries no content and is skipped by the range logic, but
  # `git push origin :main` deletes a protected branch outright -- the one push
  # that must not be waved through for having nothing in it.
  local zero=0000000000000000000000000000000000000000
  run bash -c "cd '$HD_SB' && printf 'refs/heads/x %s refs/heads/main %s\n' \
    '$zero' '$HD_TIP' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DEST=main"* ]]

  # Control: a tag push names no branch. The variable is exported and empty --
  # which is not the same as unset, because only unset may fall back to HEAD,
  # and falling back here would refuse `git push origin v1.2` for the branch
  # you happened to be standing on.
  run bash -c "cd '$HD_SB' && printf 'refs/tags/v1 %s refs/tags/v1 %s\n' \
    '$HD_TIP' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DEST="* ]]
  [[ "$output" != *"DEST=<unset>"* ]]
  [[ "$output" != *"DEST=main"* ]]
  rm -rf "$HD_SB"
}

@test "hook: a remote sha this repo does not have is no base, not a divergence" {
  # `merge-base --is-ancestor` exits 128 for an object it cannot find and 1 for
  # "genuinely not contained". Both are non-zero, and both were read as the
  # second -- so a base that had merely never been fetched (the remote moved on,
  # or this is a shallow or partial clone) collapsed into _push_unrelated and
  # hard-refused the push.
  _hd_sandbox
  local absent=0000000000000000000000000000000000000001

  # The premise, both ways round: the object is genuinely absent, and asking
  # about it fails with the same non-zero status a real divergence gives.
  run bash -c "cd '$HD_SB' && git rev-parse --verify '${absent}^{commit}'"
  [ "$status" -ne 0 ]
  run bash -c "cd '$HD_SB' && git merge-base --is-ancestor '$absent' '$HD_TIP'"
  [ "$status" -ne 0 ]

  # Two records so the absent one has an existing base beside it to be compared
  # against -- the shape that used to reach the refusal.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\nrefs/heads/other %s refs/heads/other %s\n' \
    '$HD_TIP' '$HD_ROOT' '$HD_BASE' '$absent' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"do not form one chain"* ]]
  # No base for the run as a whole, so nothing narrows the range and ship mode
  # walks all of HEAD. Scanning more, never less.
  [[ "$output" == *"NEW=$HD_TIP"* ]]
  [[ "$output" == *"OLD="* ]]
  [[ "$output" != *"OLD=$HD_ROOT"* ]]

  # Control: a base that *is* present and genuinely incomparable still refuses.
  # Otherwise this fix would have bought its way out by disabling the rule.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/a %s refs/heads/a %s\nrefs/heads/b %s refs/heads/b %s\n' \
    '$HD_TIP' '$HD_FORK' '$HD_TIP' '$HD_TIP' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -ne 0 ]
  [[ "$output" == *"do not form one chain"* ]]
  rm -rf "$HD_SB"
}

@test "hook: a deletion-only push is not gated as if it carried HEAD" {
  # `git push --delete origin feature` sends an all-zero local sha for every
  # record, so nothing set the new sha -- and ci::git::push_range defaults a
  # missing one to HEAD. git-safety and the signature and linear-history checks
  # then audited the checked-out branch, so deleting an unrelated ref could be
  # blocked by commits that are not being pushed anywhere.
  _hd_sandbox
  local zero=0000000000000000000000000000000000000000
  run bash -c "cd '$HD_SB' && printf 'refs/heads/x %s refs/heads/feature %s\n' \
    '$zero' '$HD_TIP' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  # No content, so no range is claimed for one.
  [[ "$output" == *"NEW="* ]]
  [[ "$output" != *"NEW=$HD_TIP"* ]]
  # And the destination is still reported, because whether the ref may be
  # deleted at all is branch protection's question and it still has to run.
  [[ "$output" == *"DEST=feature"* ]]

  # The control: a push that does carry content is unaffected.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\n' \
    '$HD_TIP' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"NEW=$HD_TIP"* ]]
  rm -rf "$HD_SB"
}

@test "hooks: the pre-push dispatcher exports the remote name, not the URL" {
  # The offset is easy to misread and has been misread once in review. git hands
  # the pre-push hook `<remote-name> <remote-url>`, but .githooks/pre-push runs
  # `exec ci/hook-dispatch.sh pre-push "$@"`, so inside the dispatcher $1 is the
  # hook name, $2 the remote name and $3 the URL.
  #
  # CI_GATE_PUSH_REMOTE scopes the tag-publication check to `${remote}/`, so
  # exporting "pre-push" or the URL would match no remote-tracking branch and
  # refuse every tag push. Driven through the real script with preflight stubbed,
  # rather than asserting on the source text.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci" "$sb/.githooks"
  cp "$REPO_ROOT/ci/hook-dispatch.sh" "$sb/ci/"
  cp "$REPO_ROOT/.githooks/pre-push" "$sb/.githooks/"
  printf '#!/usr/bin/env bash\nprintf "REMOTE=[%%s]\n" "${CI_GATE_PUSH_REMOTE-unset}"\nexit 0\n' \
    > "$sb/ci/preflight.sh"
  chmod +x "$sb/ci/preflight.sh" "$sb/ci/hook-dispatch.sh" "$sb/.githooks/pre-push"

  # The premise: the hook really does prepend its own name before git's args.
  run grep -c 'pre-push "\$@"' "$sb/.githooks/pre-push"
  [ "$output" -eq 1 ]

  # Exactly as git invokes it: hook args are <remote-name> <remote-url>.
  run bash -c "cd '$sb' && printf '' | bash .githooks/pre-push origin https://example.invalid/r.git 2>&1"
  [[ "$output" == *"REMOTE=[origin]"* ]]
  [[ "$output" != *"REMOTE=[pre-push]"* ]]
  [[ "$output" != *"example.invalid"* ]]
  rm -rf "$sb"
}
