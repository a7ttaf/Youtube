#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  source "$REPO_ROOT/ci/lib/common.sh"
  source "$REPO_ROOT/ci/lib/changeset.sh"

  TEST_REPO="$(mktemp -d)"
  cd "$TEST_REPO"
  git init -q
  git config user.email "test@test.com"
  git config user.name "Test"
}

teardown() {
  rm -rf "$TEST_REPO" 2>/dev/null || true
}

@test "changeset: classify shell file" {
  result="$(ci::changeset::classify_file "script.sh")"
  [ "$result" = "shell" ]
}

@test "changeset: classify python file" {
  result="$(ci::changeset::classify_file "app.py")"
  [ "$result" = "python" ]
}

@test "changeset: classify javascript file" {
  result="$(ci::changeset::classify_file "app.ts")"
  [ "$result" = "javascript" ]
}

@test "changeset: classify go file" {
  result="$(ci::changeset::classify_file "main.go")"
  [ "$result" = "go" ]
}

@test "changeset: classify dockerfile" {
  result="$(ci::changeset::classify_file "Dockerfile")"
  [ "$result" = "dockerfile" ]
}

@test "changeset: should_ignore node_modules" {
  ci::changeset::should_ignore "node_modules/foo/bar.js"
}

# --- detection failure is not an empty changeset ------------------------------

@test "changeset: a broken git reports failure rather than an empty changeset" {
  # Every git call in detect was wrapped in `|| true`, so "git is broken" and
  # "nothing is staged" produced the same empty result -- and preflight reads
  # an empty result as "no relevant changes detected" and exits 0 before a
  # single check runs. With `git diff` failing and everything else working, a
  # tree carrying staged secrets passed the gate while git-safety.sh run
  # directly against that same tree exited 20.
  printf 'x = 1\n' > a.py
  git add a.py
  # The premise: detection works here when git does.
  run bash -c "cd '$TEST_REPO' && . '$REPO_ROOT/ci/lib/common.sh' \
    && . '$REPO_ROOT/ci/lib/changeset.sh' && ci::changeset::detect pre-commit \
    && printf '%s' \"\$_CI_CHANGESET_FILES_RAW\""
  [ "$status" -eq 0 ]
  [[ "$output" == *"a.py"* ]]

  # A git that runs, but whose `diff` subcommand fails. The real binary is
  # resolved here and baked in: a shim that cannot find git exits 127, which is
  # also non-zero, and the case would then pass without ever exercising the
  # path it claims to.
  local real_git shim
  real_git="$(command -v git)"
  [ -n "$real_git" ]
  shim="$TEST_REPO/shim"
  mkdir -p "$shim"
  {
    printf '#!/usr/bin/env bash
'
    printf 'for a in "$@"; do
'
    printf '  case "$a" in
'
    printf '    -*) continue ;;
'
    printf '    diff) exit 128 ;;
'
    printf '    *) break ;;
'
    printf '  esac
'
    printf 'done
'
    printf 'exec %s "$@"
' "$real_git"
  } > "$shim/git"
  chmod +x "$shim/git"

  # The premise, twice over: the shim still runs git for everything else, and
  # it really does break `git diff`.
  run env PATH="$shim:$PATH" git rev-parse --git-dir
  [ "$status" -eq 0 ]
  run env PATH="$shim:$PATH" git diff --cached --name-status
  [ "$status" -eq 128 ]

  run env PATH="$shim:$PATH" bash -c "
    . '$REPO_ROOT/ci/lib/common.sh'
    . '$REPO_ROOT/ci/lib/changeset.sh'
    ci::changeset::detect pre-commit
  "
  [ "$status" -ne 0 ]
  # Not 127: the failure must come from detection, not from a broken shim.
  [ "$status" -ne 127 ]
}

@test "changeset: pre-push with no resolvable base does not narrow to nothing" {
  # push_range falls back to the whole of HEAD when it finds no base; this
  # resolved the same question a second way and fell back to nothing, which
  # became an empty changeset and then a skipped gate. On a branch named main
  # with no remote, that is every push.
  printf 'x = 1\n' > a.py
  git add a.py
  git -c user.email=t@t -c user.name=t commit -qm c1
  run bash -c "cd '$TEST_REPO' && . '$REPO_ROOT/ci/lib/common.sh' \
    && . '$REPO_ROOT/ci/lib/git.sh' && . '$REPO_ROOT/ci/lib/changeset.sh' \
    && ci::changeset::detect pre-push"
  # Non-zero: this changeset cannot narrow the run, so the caller must not
  # treat it as "nothing changed".
  [ "$status" -ne 0 ]
}
