#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  # The gate exports its own mode and push state, and these suites run
  # under it -- so a case inherited a range belonging to another tree.
  # shellcheck source=ci/tests/gate_env.bash
  source "$REPO_ROOT/ci/tests/gate_env.bash"
  ci::tests::clear_gate_env
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
echo "TIPS=${CI_GATE_PUSH_BRANCH_TIPS-<unset>}"
echo "TAGS=${CI_GATE_PUSH_TAG_TIPS-<unset>}"
echo "OTHER=${CI_GATE_PUSH_OTHER_TIPS-<unset>}"
echo "DELONLY=${CI_GATE_PUSH_DELETIONS_ONLY-<unset>}"
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

@test "hooks: every branch tip in a push is collected, not just the widest" {
  # The records are collapsed to one A..B range, which is right for the history
  # checks and says nothing about the trees at the other tips. The tips are
  # collected distinctly so ci::git::worktree_covers_push can ask about each.
  _hd_sandbox
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\nrefs/heads/old %s refs/heads/old %s\n' \
    '$HD_TIP' '$HD_ROOT' '$HD_BASE' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"TIPS="* ]]
  [[ "$output" == *"$HD_TIP"* ]]
  [[ "$output" == *"$HD_BASE"* ]]

  # One branch pushed to two names is one tip, not two: the same commit twice
  # would otherwise read as a multi-tip push and refuse an ordinary mirror.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\nrefs/heads/main %s refs/heads/release %s\n' \
    '$HD_TIP' '$HD_ROOT' '$HD_TIP' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"TIPS=$HD_TIP"* ]]

  # A tag alongside a branch is not a second tip. It names a commit inside the
  # history being pushed, which the range checks already cover, and treating it
  # as a destination would refuse `git push origin main v1.0`.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\nrefs/tags/v1 %s refs/tags/v1 %s\n' \
    '$HD_TIP' '$HD_ROOT' '$HD_BASE' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"TIPS=$HD_TIP"* ]]

  # A deletion carries no tree, so it contributes no tip to validate.
  local zero=0000000000000000000000000000000000000000
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\n(delete) %s refs/heads/gone %s\n' \
    '$HD_TIP' '$HD_ROOT' '$zero' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"TIPS=$HD_TIP"* ]]
  rm -rf "$HD_SB"
}

@test "git: one worktree cannot vouch for two branch tips" {
  # `git push origin broken:staging fixed:main`, where the repair descends from
  # the break, collapsed to the descendant: the lanes validated that tree,
  # reported a pass, and `staging` was left at the commit whose suite fails.
  # Ancestry proves the range covers the chain; it proves nothing about the
  # ancestor's tree having been checked, and there is only one worktree.
  _hd_sandbox
  source "$REPO_ROOT/ci/lib/git.sh"
  cd "$HD_SB"

  # The premise: HEAD is one of the two tips, so the old rule was satisfied by
  # the collapsed sha alone.
  [ "$(git rev-parse --verify HEAD)" = "$HD_TIP" ]

  export CI_GATE_PUSH_NEW_SHA="$HD_TIP"
  export CI_GATE_PUSH_REMOTE_REFS="main"

  export CI_GATE_PUSH_BRANCH_TIPS="$HD_TIP"
  run ci::git::worktree_covers_push
  [ "$status" -eq 0 ]

  export CI_GATE_PUSH_BRANCH_TIPS="$HD_TIP $HD_BASE"
  run ci::git::worktree_covers_push
  [ "$status" -ne 0 ]

  # An unreadable tip is not evidence that it matches, as everywhere else here.
  export CI_GATE_PUSH_BRANCH_TIPS="$HD_TIP deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
  run ci::git::worktree_covers_push
  [ "$status" -ne 0 ]

  # And the diagnostic names the condition rather than printing a collapsed sha
  # that does match HEAD against a HEAD that does.
  export CI_GATE_PUSH_BRANCH_TIPS="$HD_TIP $HD_BASE"
  run ci::git::explain_push_tip_drift
  [[ "$output" == *"more than one commit"* ]]
  [[ "$output" == *"$HD_BASE"* ]]

  # Unset is nobody-said, which is CI and every direct invocation: those run
  # against whatever is checked out by design.
  unset CI_GATE_PUSH_BRANCH_TIPS
  run ci::git::worktree_covers_push
  [ "$status" -eq 0 ]

  unset CI_GATE_PUSH_NEW_SHA CI_GATE_PUSH_REMOTE_REFS
  cd "$REPO_ROOT"
  rm -rf "$HD_SB"
}

@test "git: a tag is the checkout or is already on the destination" {
  # This case asserted a third answer for a while -- that a tag on a commit a
  # branch in the same push is sending adds a label and no content, because the
  # objects go out with the branch either way. The premise is wrong, and it is
  # wrong for a reason no amount of looking at this repository would show:
  # `git push -h` documents `--atomic` as *requesting* "atomic transaction on
  # remote side", so a push is not atomic unless asked, and the server may
  # reject the branch while accepting the tag. Then nothing carried the commit
  # out except the tag, and its tree was never run by a content lane.
  #
  # Nothing tells a pre-push hook whether `--atomic` was passed, so the
  # condition that arm depended on cannot be checked from here. A rule that
  # cannot be checked is not kept.
  #
  # The cost, stated rather than hidden: `git push origin main v1.0` with the
  # tag on a commit main is sending now needs two commands. Push the branch,
  # then the tag -- by then the commit is on the destination and the tag really
  # does carry no content. That remedy is followable, which is the test this
  # gate applies to its own refusals.
  local sb
  sb="$(mktemp -d)"
  (
    cd "$sb"
    git init -q -b main .
    printf 'a\n' > a.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c1
    old="$(git rev-parse HEAD)"
    printf 'b\n' > b.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c2
    tip="$(git rev-parse HEAD)"
    # A commit on a branch this push does not name.
    git checkout -q -b side "$old"
    printf 's\n' > s.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm side1
    side="$(git rev-parse HEAD)"
    git checkout -q main
    printf '%s %s %s\n' "$old" "$tip" "$side" > .shas
  ) >/dev/null 2>&1
  local old tip side
  read -r old tip side < "$sb/.shas"

  _covers() { # _covers <tag tips> [branch tips]
    bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' \
      && CI_GATE_PUSH_NEW_SHA='$tip' CI_GATE_PUSH_REMOTE_REFS=main \
         CI_GATE_PUSH_BRANCH_TIPS='${2-$tip}' CI_GATE_PUSH_TAG_TIPS='$1' \
         CI_GATE_PUSH_REMOTE=origin ci::git::worktree_covers_push"
  }

  # The premise: the branch tip is the checkout, which is what made the old
  # rule return before it could look at anything else.
  run bash -c "cd '$sb' && git rev-parse --verify HEAD"
  [ "$output" = "$tip" ]

  run _covers ""
  [ "$status" -eq 0 ]
  run _covers "$tip"
  [ "$status" -eq 0 ]

  # The release push: a tag on a commit this same push is sending under main.
  # Refused now, because a non-atomic push can drop the branch and keep the tag.
  run _covers "$old"
  [ "$status" -ne 0 ]

  # And what the rule is for: a tag on a commit the destination does not have.
  # That tag is what takes the commit out, and its tree has never been run.
  run _covers "$side"
  [ "$status" -ne 0 ]

  # A tag-only push is judged by the same test, which is now the only test.
  run _covers "$old" ""
  [ "$status" -ne 0 ]
  run bash -c "cd '$sb' && git update-ref refs/remotes/origin/main '$old'"
  [ "$status" -eq 0 ]
  run _covers "$old" ""
  [ "$status" -eq 0 ]

  # An unreadable tip is not evidence that it matches, as everywhere else here.
  run _covers "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
  [ "$status" -ne 0 ]

  # And the diagnostic names the condition it found, rather than comparing a
  # collapsed tip against a HEAD that equals it.
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' \
    && CI_GATE_PUSH_NEW_SHA='$tip' CI_GATE_PUSH_BRANCH_TIPS='$tip' \
       CI_GATE_PUSH_TAG_TIPS='$side' CI_GATE_PUSH_REMOTE=origin \
       ci::git::explain_push_tip_drift"
  [[ "$output" == *"publishes a tag on a commit nothing here has vouched for"* ]]
  [[ "$output" != *"is not the commit checked out"* ]]
  rm -rf "$sb"
}

@test "hooks: tag tips are collected separately from branch tips" {
  _hd_sandbox
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\nrefs/tags/v1 %s refs/tags/v1 %s\n' \
    '$HD_TIP' '$HD_ROOT' '$HD_BASE' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"TIPS=$HD_TIP"* ]]
  [[ "$output" == *"TAGS=$HD_BASE"* ]]

  # A deletion of a tag carries no tree, so it contributes no tip.
  local zero=0000000000000000000000000000000000000000
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\n(delete) %s refs/tags/old %s\n' \
    '$HD_TIP' '$HD_ROOT' '$zero' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"TAGS="* ]]
  [[ "$output" != *"TAGS=$HD_ROOT"* ]]
  rm -rf "$HD_SB"
}

@test "hook: reading no records is not the same statement as reading deletions" {
  # `_push_any_content` starts at 0 and only rises inside the read loop, so
  # "every record was a deletion" and "there were no records at all" set the
  # identical flag -- and that flag now selects the whole ship plan. A hook run
  # with nothing on stdin therefore reported PASS having scheduled one lane,
  # which then announced that it did not apply.
  #
  # git really does run this hook with zero records, on a push with nothing to
  # send, so failing here would refuse an ordinary no-op push. A hook runner
  # that does not forward the ref list is silent in exactly the same way while
  # git still sends the refs, and the two cannot be told apart from inside the
  # hook. So the empty case narrows nothing.
  _hd_sandbox
  local zero=0000000000000000000000000000000000000000

  run bash -c "cd '$HD_SB' && bash ci/hook-dispatch.sh pre-push origin file:///x </dev/null 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELONLY=<unset>"* ]]
  [[ "$output" == *"no ref records on stdin"* ]]

  # The statement it must not be confused with: records that are all deletions.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/x %s refs/heads/feature %s\n' \
    '$zero' '$HD_TIP' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELONLY=1"* ]]

  # And a push that carries content is neither.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s\n' \
    '$HD_TIP' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELONLY=<unset>"* ]]
  rm -rf "$HD_SB"
}

@test "hook: only a branch destination has to fit the one range" {
  # The single A..B range exists for the history checks over content being
  # uploaded, and only a branch destination needs one. A tag or a pointer in
  # another namespace publishes a name for a commit, and whether that commit may
  # go out is decided per ref by worktree_covers_push. Letting them into the
  # chain broke two ordinary pushes in opposite directions.
  _hd_sandbox
  local zero=0000000000000000000000000000000000000000

  # `git push --follow-tags origin main`: the new tag has an all-zero remote
  # sha, which blanked the base for the whole push. Every history check then
  # re-walked the repository instead of the commits actually being sent.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s
refs/tags/v1 %s refs/tags/v1 %s
'     '$HD_TIP' '$HD_BASE' '$HD_TIP' '$zero' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"OLD=$HD_BASE"* ]]
  [[ "$output" == *"NEW=$HD_TIP"* ]]
  [[ "$output" == *"TAGS=$HD_TIP"* ]]

  # `git push --tags origin` with tags on two lines of history: neither commit
  # contains the other, so the chain check refused the run outright -- before
  # the rule that would have cleared both tags had run at all.
  run bash -c "cd '$HD_SB' && printf 'refs/tags/v1 %s refs/tags/v1 %s
refs/tags/v3 %s refs/tags/v3 %s
'     '$HD_TIP' '$zero' '$HD_FORK' '$zero' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"do not form one chain"* ]]
  [[ "$output" == *"TAGS=$HD_TIP $HD_FORK"* ]]

  # Two divergent BRANCHES are still refused -- that is what the rule is for --
  # and now inside the stated result contract rather than beside it.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s
refs/heads/fork %s refs/heads/fork %s
'     '$HD_TIP' '$zero' '$HD_FORK' '$zero' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"do not form one chain"* ]]
  rm -rf "$HD_SB"
}

@test "hook: a destination outside refs/heads and refs/tags is still a destination" {
  # Both collectors recognised refs/heads/* and refs/tags/* and nothing else, so
  # a record publishing to Gerrit's refs/for/*, a refs/publish/* pointer or
  # refs/meta/config contributed to no list at all: worktree_covers_push walked
  # an empty tag list and an all-matching branch list and returned 0, and the
  # content lanes vouched for HEAD while an unexamined tree went out under the
  # other ref.
  _hd_sandbox
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s
refs/heads/old %s refs/publish/prod %s
'     '$HD_TIP' '$HD_ROOT' '$HD_BASE' '$HD_ROOT' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"OTHER=$HD_BASE"* ]]

  # And the rule it is judged by is the tag rule: the checkout, or already on
  # the destination. "Carried by a branch in this same push" was a third answer
  # here for a while and is gone, because a push is not atomic unless asked for
  # -- the server can reject the branch and accept this ref, and then nothing
  # carried the commit out except the ref itself. Both of these are ancestors of
  # HEAD and neither is published, so both are refused; being an ancestor says
  # the worktree contains that commit's history, not that its tree was run.
  run bash -c "cd '$HD_SB' && . '$REPO_ROOT/ci/lib/git.sh'     && CI_GATE_PUSH_NEW_SHA='$HD_TIP' CI_GATE_PUSH_BRANCH_TIPS='$HD_TIP'        CI_GATE_PUSH_OTHER_TIPS='$HD_FORK' CI_GATE_PUSH_REMOTE=origin        ci::git::worktree_covers_push"
  [ "$status" -ne 0 ]
  run bash -c "cd '$HD_SB' && . '$REPO_ROOT/ci/lib/git.sh'     && CI_GATE_PUSH_NEW_SHA='$HD_TIP' CI_GATE_PUSH_BRANCH_TIPS='$HD_TIP'        CI_GATE_PUSH_OTHER_TIPS='$HD_BASE' CI_GATE_PUSH_REMOTE=origin        ci::git::worktree_covers_push"
  [ "$status" -ne 0 ]

  # The controls, so this stays a rule about publication and not a refusal of
  # every other-namespace push: the checkout itself is covered, and so is a
  # commit the destination already has.
  run bash -c "cd '$HD_SB' && . '$REPO_ROOT/ci/lib/git.sh'     && CI_GATE_PUSH_NEW_SHA='$HD_TIP' CI_GATE_PUSH_BRANCH_TIPS='$HD_TIP'        CI_GATE_PUSH_OTHER_TIPS='$HD_TIP' CI_GATE_PUSH_REMOTE=origin        ci::git::worktree_covers_push"
  [ "$status" -eq 0 ]
  run bash -c "cd '$HD_SB' && git update-ref refs/remotes/origin/main '$HD_BASE'"
  [ "$status" -eq 0 ]
  run bash -c "cd '$HD_SB' && . '$REPO_ROOT/ci/lib/git.sh'     && CI_GATE_PUSH_NEW_SHA='$HD_TIP' CI_GATE_PUSH_BRANCH_TIPS='$HD_TIP'        CI_GATE_PUSH_OTHER_TIPS='$HD_BASE' CI_GATE_PUSH_REMOTE=origin        ci::git::worktree_covers_push"
  [ "$status" -eq 0 ]
  rm -rf "$HD_SB"
}

@test "hook: a notes push is not a tree this gate stands behind" {
  # `git push origin refs/notes/commits` publishes a commit whose tree is note
  # blobs. It became CI_GATE_PUSH_NEW_SHA, worktree_covers_push found it was
  # neither the checkout nor on the destination -- `git branch -r --contains`
  # never lists a notes commit -- and the push was refused with "check out the
  # commit being pushed", which cannot be done to a notes commit.
  _hd_sandbox
  local zero=0000000000000000000000000000000000000000
  run bash -c "cd '$HD_SB' && printf 'refs/notes/commits %s refs/notes/commits %s
'     '$HD_ROOT' '$zero' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"NEW="* ]]
  [[ "$output" != *"NEW=$HD_ROOT"* ]]
  [[ "$output" == *"OTHER="* ]]
  [[ "$output" != *"OTHER=$HD_ROOT"* ]]
  [[ "$output" == *"TAGS="* ]]
  [[ "$output" != *"TAGS=$HD_ROOT"* ]]
  rm -rf "$HD_SB"
}

@test "hook: the push state this hook exports is its own conclusion, not the caller's" {
  # Every one of these variables is only ever *set*, never cleared, so a value
  # inherited from the calling environment survived into a run that never
  # reached the line which would have set it. CI_GATE_PUSH_DELETIONS_ONLY is the
  # sharpest of them: ci/preflight.sh selects the entire ship plan from it, so a
  # stray `CI_GATE_PUSH_DELETIONS_ONLY=1` in the caller's environment reduced an
  # ordinary content-bearing push to destination protection alone -- git-safety,
  # test-layout, the node lane, the build and the shell suites all skipped.
  _hd_sandbox
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s
' '$HD_TIP' '$HD_BASE' | CI_GATE_PUSH_DELETIONS_ONLY=1 CI_GATE_PUSH_TAG_TIPS=stale CI_GATE_PUSH_NEW_SHA=stale bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELONLY=<unset>"* ]]
  [[ "$output" == *"NEW=$HD_TIP"* ]]
  [[ "$output" != *"=stale"* ]]

  # The control: a push this hook itself finds to be deletions-only still says
  # so, so the reset is a reset and not a removal.
  local zero=0000000000000000000000000000000000000000
  run bash -c "cd '$HD_SB' && printf 'refs/heads/gone %s refs/heads/gone %s
' '$zero' '$HD_BASE' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELONLY=1"* ]]
  rm -rf "$HD_SB"
}

@test "hook: a notes-only push carries nothing a content lane reads" {
  # `_push_any_content=1` was raised before the record's namespace was known, so
  # the arm that treats refs/notes/* as annotations -- which it is -- came too
  # late: the push counted as content, exported no tip and no deletions-only
  # flag, and ship preflight fell back to the checked-out HEAD. An ordinary
  # `git notes` update then ran the node lane, the build and the shell suites
  # over unrelated project content, where any pre-existing failure blocked it.
  _hd_sandbox
  local zero=0000000000000000000000000000000000000000
  run bash -c "cd '$HD_SB' && printf 'refs/notes/commits %s refs/notes/commits %s
' '$HD_ROOT' '$zero' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELONLY=1"* ]]

  # The control: a branch record beside the notes one raises the flag for
  # itself, and that push is gated in full.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s
refs/notes/commits %s refs/notes/commits %s
' '$HD_TIP' '$HD_BASE' '$HD_ROOT' '$zero' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELONLY=<unset>"* ]]
  [[ "$output" == *"NEW=$HD_TIP"* ]]
  rm -rf "$HD_SB"
}

@test "hook: only a branch record moves the collapsed history tip" {
  # The tip and the base are one range, and a range is the branch question. With
  # tags still able to seed it, a tag-only push listing an already-published tag
  # from an unrelated lineage before a new tag at HEAD took the published SHA
  # first and kept it -- the later tag is not a branch, so it could not displace
  # it. push_range then had git-safety and the history checks walk the published
  # lineage instead of the outgoing one, and a secret added and removed before
  # HEAD was never looked at. What may go out under a tag is decided per ref by
  # worktree_covers_push, which is where that question already lives.
  _hd_sandbox
  local zero=0000000000000000000000000000000000000000
  run bash -c "cd '$HD_SB' && printf 'refs/tags/old %s refs/tags/old %s
refs/tags/new %s refs/tags/new %s
' '$HD_ORPHAN' '$zero' '$HD_TIP' '$zero' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"NEW="* ]]
  [[ "$output" != *"NEW=$HD_ORPHAN"* ]]
  [[ "$output" != *"NEW=$HD_TIP"* ]]
  # Collected, though -- the per-ref rule still gets both of them.
  [[ "$output" == *"TAGS=$HD_ORPHAN $HD_TIP"* ]]

  # The control: a branch record in the same push still sets the tip, and a tag
  # listed after it does not take it away.
  run bash -c "cd '$HD_SB' && printf 'refs/heads/main %s refs/heads/main %s
refs/tags/new %s refs/tags/new %s
' '$HD_TIP' '$HD_BASE' '$HD_ORPHAN' '$zero' | bash ci/hook-dispatch.sh pre-push origin file:///x 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"NEW=$HD_TIP"* ]]
  [[ "$output" == *"TAGS=$HD_ORPHAN"* ]]
  rm -rf "$HD_SB"
}

@test "git: an empty branch tip is not the same statement as an empty push" {
  # The scalar is the collapsed *branch* tip, and the dispatcher deliberately
  # leaves it unset for a push that names no branch -- a tag-only push, or one
  # into a namespace this gate has no model of -- exporting the tip lists
  # instead. Returning on the scalar alone therefore skipped both per-ref loops
  # for exactly the pushes they exist to judge: `git push origin v1`, with `v1`
  # on an older unpublished commit and its repair checked out, was covered by a
  # function that never looked at the tag.
  #
  # The hole opened when the tip collapse was restricted to branch records --
  # which was the right fix, one range is the branch question -- so this is its
  # other half, and the pair is the whole rule.
  local sb
  sb="$(mktemp -d)"
  (
    cd "$sb"
    git init -q -b main .
    printf 'a\n' > a.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c1
    old="$(git rev-parse HEAD)"
    printf 'b\n' > b.txt && git add -A
    git -c user.email=t@t -c user.name=t commit -qm c2
    tip="$(git rev-parse HEAD)"
    printf '%s %s\n' "$old" "$tip" > .shas
  ) >/dev/null 2>&1
  local old tip
  read -r old tip < "$sb/.shas"

  _tagonly() { # _tagonly <tag tips> <other tips>
    bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' \
      && CI_GATE_PUSH_NEW_SHA='' CI_GATE_PUSH_BRANCH_TIPS='' \
         CI_GATE_PUSH_TAG_TIPS='$1' CI_GATE_PUSH_OTHER_TIPS='$2' \
         CI_GATE_PUSH_REMOTE=origin ci::git::worktree_covers_push"
  }

  # The premise: the checkout is the newer commit, and the tag names the older
  # one, which the destination does not have.
  run bash -c "cd '$sb' && git rev-parse --verify HEAD"
  [ "$output" = "$tip" ]

  run _tagonly "$old" ""
  [ "$status" -ne 0 ]

  # The same for a namespace this gate has no model of.
  run _tagonly "" "$old"
  [ "$status" -ne 0 ]

  # The controls. A tag on the checkout is the tree every content lane just
  # read, and a tag on a commit the destination already has carries nothing.
  run _tagonly "$tip" ""
  [ "$status" -eq 0 ]
  run bash -c "cd '$sb' && git update-ref refs/remotes/origin/main '$old'"
  [ "$status" -eq 0 ]
  run _tagonly "$old" ""
  [ "$status" -eq 0 ]

  # And the statement that really is "nobody said": every list empty, which is
  # CI and every direct invocation, and must stay covered or the gate refuses
  # itself.
  run bash -c "cd '$sb' && . '$REPO_ROOT/ci/lib/git.sh' \
    && CI_GATE_PUSH_NEW_SHA='' CI_GATE_PUSH_BRANCH_TIPS='' \
       CI_GATE_PUSH_TAG_TIPS='' CI_GATE_PUSH_OTHER_TIPS='' \
       ci::git::worktree_covers_push"
  [ "$status" -eq 0 ]
  rm -rf "$sb"
}

# --- the result contract, at three places that stepped outside it -------------
#
# 0 PASS / 10 PASS_WITH_KNOWN_DEBT / 20 FAIL_NEW_ISSUE / 30 FAIL_INFRA. A lane
# that leaves with anything else is recorded through preflight's unrecognised
# arm as FAIL_INFRA, so a code defect arrives labelled as a broken gate and the
# output names no cause.

@test "hook: commit-msg translates the result contract instead of handing it to git" {
  # The arm exec'd the check, so its status became the hook's and git blocks a
  # commit on any non-zero. PASS_WITH_KNOWN_DEBT is 10, so a pass-class result
  # refused the commit: a staged diff over CI_GATE_WARN_DIFF_LINES with a
  # perfectly valid subject calls _hygiene_warn, the check exits 10, and the
  # commit is rejected with "Consider splitting" -- advice offered as a rule,
  # with no way to proceed.
  #
  # Three hook entry points read the same four values. The other two exec
  # ci/preflight.sh, which ends non-zero only for 20 and 30. This one skipped
  # the translation by skipping the translator.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib"
  cp "$REPO_ROOT/ci/hook-dispatch.sh" "$sb/ci/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  ( cd "$sb" && git init -q -b main . ) >/dev/null 2>&1
  printf 'feat: a subject\n' > "$sb/msg.txt"

  local rc want
  # rc -> what the hook must do with it
  for rc in "0:0" "10:0" "20:1" "30:1"; do
    want="${rc##*:}"
    rc="${rc%%:*}"
    printf '#!/usr/bin/env bash\nexit %s\n' "$rc" > "$sb/ci/checks/commit-hygiene.sh"
    chmod +x "$sb/ci/checks/commit-hygiene.sh"
    run bash -c "cd '$sb' && bash ci/hook-dispatch.sh commit-msg msg.txt"
    [ "$status" -eq "$want" ] \
      || { echo "check exited $rc, hook exited $status, expected $want" >&2; return 1; }
  done

  # And a status outside the contract is not a pass. preflight records an
  # unrecognised value as FAIL_INFRA rather than assuming, and so does this.
  printf '#!/usr/bin/env bash\nexit 7\n' > "$sb/ci/checks/commit-hygiene.sh"
  chmod +x "$sb/ci/checks/commit-hygiene.sh"
  run bash -c "cd '$sb' && bash ci/hook-dispatch.sh commit-msg msg.txt 2>&1"
  [ "$status" -eq 1 ]
  [[ "$output" == *"not one of"* ]]
  rm -rf "$sb"
}

@test "debt: fixing a registered debt is a pass, not a dead lane" {
  # `count="$(... | grep -F -o -- "$signature" | wc -l ...)"` was bare under
  # this file's own `set -Eeuo pipefail`. A signature that matches nothing makes
  # grep exit 1, the pipeline exit 1, the assignment fail, and errexit end the
  # lane -- so the lane died at the moment a registered debt entry stopped
  # matching, which is what fixing the debt looks like. The branch below it that
  # prints "debt may be reduced or changed" was unreachable.
  #
  # The debt command's own status is taken with `set +e` a few lines up and both
  # ruff greps carry `|| true`, so this file knows the rule; one line did not.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib" "$sb/ci/debt" "$sb/ci/reports"
  cp "$REPO_ROOT/ci/checks/debt.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"
  ( cd "$sb" && git init -q -b main . ) >/dev/null 2>&1

  _debt_entry() {
    cat > "$sb/ci/debt/known-failures.yml" <<YML
version: 1
known_failures:
  - id: sample
    type: ruff
    command: "$1"
    status: known_debt
    first_seen: "2026-01-01"
    blocking: false
    owner: "project"
    reason: "fixture"
    allowed_until: "2099-01-01"
    must_not_increase: false
    signatures:
      - "ASYNC240"
YML
  }

  # The debt is gone: a pass, and a line saying so.
  _debt_entry "echo clean"
  run bash -c "cd '$sb' && bash ci/checks/debt.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Debt result: PASS"* ]]
  [[ "$output" == *"debt may be reduced or changed"* ]]

  # The control: the debt is still there, which is the branch that already
  # worked and must keep working.
  _debt_entry "echo ASYNC240-still-here"
  run bash -c "cd '$sb' && bash ci/checks/debt.sh 2>&1"
  [ "$status" -eq 10 ]
  [[ "$output" == *"PASS_WITH_KNOWN_DEBT"* ]]
  rm -rf "$sb"
}

@test "python: a module the compiler rejects is a code defect, not broken infrastructure" {
  # compileall was the one tool in that lane whose status was not captured.
  # Under `set -Eeuo pipefail` its failure ended the script before the result
  # line and before any exit from the contract, so the lane left with raw 1 and
  # preflight recorded FAIL_INFRA -- while the same tree failing ruff or pytest
  # two screens up is reported as FAIL_NEW_ISSUE. Both are the same kind of
  # defect and only one was described.
  #
  # Reachable without contriving anything: ruff respects .gitignore by default,
  # so an ignored generated module with a syntax error passes `ruff check` and
  # stops here.
  local sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/ci/checks" "$sb/ci/lib" "$sb/pkg"
  cp "$REPO_ROOT/ci/checks/python.sh" "$sb/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$sb/ci/lib/"

  printf 'def broken(:\n    pass\n' > "$sb/pkg/mod.py"
  run bash -c "cd '$sb' && PYTHON_PACKAGE_DIR=pkg bash ci/checks/python.sh 2>&1"
  [ "$status" -eq 20 ]
  [[ "$output" == *"compileall rejected"* ]]

  # The control: a module that compiles still passes, so this captures a status
  # rather than inventing a failure.
  printf 'def fine():\n    pass\n' > "$sb/pkg/mod.py"
  run bash -c "cd '$sb' && PYTHON_PACKAGE_DIR=pkg bash ci/checks/python.sh 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Python lane passed"* ]]
  rm -rf "$sb"
}
