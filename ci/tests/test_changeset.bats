#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  # The gate exports its own mode and push state, and these suites run
  # under it -- so a case inherited a range belonging to another tree.
  # shellcheck source=ci/tests/gate_env.bash
  source "$REPO_ROOT/ci/tests/gate_env.bash"
  ci::tests::clear_gate_env
  source "$REPO_ROOT/ci/lib/common.sh"
  source "$REPO_ROOT/ci/lib/changeset.sh"

  TEST_REPO="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
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

@test "changeset: a non-ASCII path is classified, not quoted into nothing" {
  # `--name-status` without -z emits git's quoted representation for any path
  # outside plain ASCII, so frontend/src/café.ts arrived as
  # "frontend/src/caf\303\251.ts" -- a string ending in a quote character, not
  # in .ts. Its language became unknown and the emitted checks were the
  # always-list alone: the Node tests, typecheck and build were filtered out of
  # a commit that changes TypeScript.
  mkdir -p frontend/src
  printf '{}' > frontend/package.json
  printf 'export const x = 1;\n' > "frontend/src/café.ts"
  git add -A

  # The premise: git really does quote it in the non-z form.
  run bash -c "cd '$TEST_REPO' && git diff --cached --name-status"
  [[ "$output" == *'\303\251'* ]]

  run bash -c "cd '$TEST_REPO' && . '$REPO_ROOT/ci/lib/common.sh' \
    && . '$REPO_ROOT/ci/lib/changeset.sh' && ci::changeset::detect pre-commit \
    && printf '%s' \"\$_CI_CHANGESET_CHECKS\""
  [ "$status" -eq 0 ]
  [[ "$output" == *"tests-js"* ]]
  [[ "$output" == *"typecheck-js"* ]]
  # And the raw entry carries the real bytes, not the escape sequence.
  run bash -c "cd '$TEST_REPO' && . '$REPO_ROOT/ci/lib/common.sh' \
    && . '$REPO_ROOT/ci/lib/changeset.sh' && ci::changeset::detect pre-commit \
    && printf '%s' \"\$_CI_CHANGESET_FILES_RAW\""
  [[ "$output" != *'\303\251'* ]]
  [[ "$output" == *"café.ts"* ]]
}

@test "changeset: a renamed path keeps both sides through the NUL reader" {
  # A rename is three NUL fields, not two. Reading it as two would consume the
  # destination as the next record's status and desynchronise everything after
  # it -- and the scheduler needs both sides, since a changeset listing only
  # the destination describes a different change from the one being gated.
  mkdir -p frontend/src
  printf '{}' > frontend/package.json
  printf 'export const x = 1;\n' > frontend/src/old.ts
  git add -A
  git -c user.email=t@t -c user.name=t commit -qm init
  git mv frontend/src/old.ts frontend/src/new.ts
  printf 'export const y = 2;\n' > frontend/src/after.ts
  git add -A

  run bash -c "cd '$TEST_REPO' && . '$REPO_ROOT/ci/lib/common.sh' \
    && . '$REPO_ROOT/ci/lib/changeset.sh' && ci::changeset::detect pre-commit \
    && printf '%s' \"\$_CI_CHANGESET_FILES_RAW\""
  [ "$status" -eq 0 ]
  [[ "$output" == *"old.ts"* ]]
  [[ "$output" == *"new.ts"* ]]
  # The record after the rename is still read as its own entry.
  [[ "$output" == *"after.ts"* ]]
}

@test "changeset: a path holding a tab or newline still classifies" {
  # The reader takes git's NUL-delimited `--name-status -z` and immediately
  # writes tab- and newline-delimited records. A path is any byte sequence
  # without a NUL, so `backend/foo<newline>bar.py` split into two records --
  # both nonsense -- and the language came out `unknown`, dropping every Python
  # lint, typecheck, format and test check from a change that is Python.
  #
  # Driven through the reader directly rather than through git. This defect
  # lives entirely between _read_name_status and _populate_state_from_raw, and
  # the input cannot be staged on every platform the suite runs on: Windows
  # substitutes U+F00A for a newline in a filename and `git update-index`
  # refuses the path outright, so a filesystem-based case would pass against
  # the broken reader too and assert nothing.
  local nl=$'backend/foo\nbar.py'
  local tb=$'backend/a\tb.py'

  # The premise: the input really does carry the byte in question.
  [[ "$nl" == *$'\n'* ]]
  [[ "$tb" == *$'\t'* ]]

  _CI_CHANGESET_FILES_RAW="$(printf 'A\000%s\000' "$nl" | ci::changeset::_read_name_status)"
  ci::changeset::_populate_state_from_raw
  [[ "$(ci::changeset::get_languages)" == *python* ]]

  _CI_CHANGESET_FILES_RAW="$(printf 'A\000%s\000' "$tb" | ci::changeset::_read_name_status)"
  ci::changeset::_populate_state_from_raw
  [[ "$(ci::changeset::get_languages)" == *python* ]]

  # The controls: ordinary paths are unaffected, and a literal `%` survives the
  # escaping rather than being read back as an escape.
  _CI_CHANGESET_FILES_RAW="$(printf 'A\000%s\000' 'backend/plain.py' | ci::changeset::_read_name_status)"
  ci::changeset::_populate_state_from_raw
  [[ "$(ci::changeset::get_languages)" == *python* ]]

  _CI_CHANGESET_FILES_RAW="$(printf 'A\000%s\000' 'frontend/src/a.ts' | ci::changeset::_read_name_status)"
  ci::changeset::_populate_state_from_raw
  [[ "$(ci::changeset::get_languages)" == *javascript* ]]

  ci::changeset::_decode_path "$(ci::changeset::_encode_path 'backend/100%_done.py')"
  [ "$_CI_CHANGESET_PATH" = 'backend/100%_done.py' ]
  ci::changeset::_decode_path "$(ci::changeset::_encode_path "$nl")"
  [ "$_CI_CHANGESET_PATH" = "$nl" ]
}

@test "changeset: a path byte that is not UTF-8 does not make the report unreadable" {
  # JSON is defined over text and a Git path is defined over bytes. `bad\xff.py`
  # is a legal POSIX filename that no sequence of Unicode characters encodes,
  # and written through unchanged it made the whole of changeset.json invalid
  # UTF-8 -- the entire audit record lost to one byte in one name.
  #
  # setup() has already sourced the library.
  local ff cafe trunc surrogate overlong cjk
  ff="$(printf 'bad\377.py')"
  cafe="$(printf 'caf\303\251.ts')"
  trunc="$(printf 'a\303.ts')"
  surrogate="$(printf 'a\355\240\200b')"
  overlong="$(printf 'a\300\257b')"
  cjk="$(printf 'a\350\252\236b')"

  # The bytes that cannot be text are escaped, one at a time, so the walk
  # resynchronises on the next lead byte instead of swallowing what followed.
  [ "$(ci::changeset::_json_escape "$ff")" = 'bad\u00ff.py' ]
  [ "$(ci::changeset::_json_escape "$trunc")" = 'a\u00c3.ts' ]

  # Strictly well-formed means strictly: an overlong encoding and a lone
  # surrogate are rejected by the decoder that reads this file, so they are
  # rejected here too rather than passed through as "high bytes".
  [ "$(ci::changeset::_json_escape "$surrogate")" = 'a\u00ed\u00a0\u0080b' ]
  [ "$(ci::changeset::_json_escape "$overlong")" = 'a\u00c0\u00afb' ]

  # The control, and the reason this is not simply "escape every high byte":
  # real UTF-8 goes through untouched and stays itself.
  [ "$(ci::changeset::_json_escape "$cafe")" = "$cafe" ]
  [ "$(ci::changeset::_json_escape "$cjk")" = "$cjk" ]
  [ "$(ci::changeset::_json_escape 'frontend/src/a.ts')" = 'frontend/src/a.ts' ]

  # And the escaping that was already there is unchanged by the new pass, which
  # runs last precisely so it cannot escape its own output.
  [ "$(ci::changeset::_json_escape "$(printf 'q"\\w\tx')")" = 'q\"\\w\tx' ]

  # The property the whole rule is for, checked rather than reasoned about: the
  # document a strict decoder is handed actually parses. Skipped only where
  # there is no decoder to ask.
  if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
    skip "no python available to parse the artifact"
  fi
  local py=python3
  command -v python3 >/dev/null 2>&1 || py=python
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
  cat > "$sb/check.py" <<'PY'
import io, json, sys
with io.open(sys.argv[1], encoding='utf-8') as fh:
    json.load(fh)
PY
  local bad
  for bad in "$ff" "$trunc" "$surrogate" "$overlong" "$cafe" "$cjk"; do
    printf '%s' "{\"path\":\"$(ci::changeset::_json_escape "$bad")\"}" > "$sb/changeset.json"
    run "$py" "$sb/check.py" "$sb/changeset.json"
    [ "$status" -eq 0 ]
  done
  rm -rf "$sb"
}

@test "changeset: a pre-push changeset is the pushed range, not the index beside it" {
  # The index is the commit being made, not the commit being pushed, and this
  # mode unioned it in -- so a README-only outgoing commit with a staged
  # frontend/src/wip.ts emitted lint-js, typecheck-js, format-js and tests-js,
  # and unrelated local work decided what a push had to survive.
  #
  # Nothing is lost by dropping it: the lanes the union reached stand behind
  # HEAD in ship mode themselves, and the checks that must run whatever changed
  # are on the always-list, which no filter touches. Both are asserted below.
  source "$REPO_ROOT/ci/lib/git.sh"
  mkdir -p "$TEST_REPO/frontend/src"
  printf '# readme\n' > "$TEST_REPO/README.md"
  printf 'export const a = 1;\n' > "$TEST_REPO/frontend/src/app.ts"
  printf '{ "name": "f" }\n' > "$TEST_REPO/frontend/package.json"
  git add -A
  git commit -qm init
  local base tip
  base="$(git rev-parse HEAD)"
  printf '# readme v2\n' > "$TEST_REPO/README.md"
  git add README.md
  git commit -qm docs
  tip="$(git rev-parse HEAD)"

  printf 'export const wip = 1;\n' > "$TEST_REPO/frontend/src/wip.ts"
  git add frontend/src/wip.ts

  # The premise: the push carries README.md alone; the JS file is staged only.
  run git diff --name-only "${base}..${tip}"
  [ "$output" = "README.md" ]
  run git diff --cached --name-only
  [ "$output" = "frontend/src/wip.ts" ]

  export CI_GATE_MODE=ship CI_GATE_PUSH_OLD_SHA="$base" CI_GATE_PUSH_NEW_SHA="$tip" \
         CI_GATE_PUSH_REMOTE=origin
  ci::changeset::detect pre-push
  local checks
  checks="$(ci::changeset::get_checks_needed)"
  [[ "$checks" != *"tests-js"* ]] \
    || { echo "staged WIP scheduled a JS lane on a docs-only push: $checks" >&2; return 1; }
  [[ "$checks" != *"typecheck-js"* ]]
  [[ "$checks" != *"lint-js"* ]]

  # And positively, so the case cannot be satisfied by a detector that emits
  # nothing: what the push does carry is still classified, and the always-run
  # checks are still there.
  [[ "$checks" == *"lint-markdown"* ]]
  [[ "$checks" == *"secrets"* ]]
  [[ "$checks" == *"commit-hygiene"* ]]

  # The control that keeps the rule: a JS file in the pushed range still
  # schedules the JS lanes. Without this the fix would read as "never schedule
  # JavaScript from a pre-push changeset".
  git commit -qm wip
  tip="$(git rev-parse HEAD)"
  export CI_GATE_PUSH_NEW_SHA="$tip"
  ci::changeset::detect pre-push
  checks="$(ci::changeset::get_checks_needed)"
  [[ "$checks" == *"tests-js"* ]]
  [[ "$checks" == *"typecheck-js"* ]]
}

@test "changeset: ship-mode workspace membership is read from the pushed tree" {
  # _in_node_workspace decides whether a non-JS path sits under a Node workspace,
  # and that answer picks the JS check ids -- which preflight turns into the node
  # lane. It read the worktree and the index in every mode.
  #
  # In ship mode those are the commit being built, not the one being pushed. With
  # frontend/package.json deleted locally while HEAD still carries it, an
  # outgoing frontend/public asset classified as "not a workspace", the changeset
  # emitted no JS ids, and the node lane was filtered out of a push that carries
  # frontend files. Seventh reader of "which tree does this run stand behind",
  # and the first inside the scheduler, where being wrong removes a lane instead
  # of misreporting inside one.
  mkdir -p frontend/public
  printf '{ "name": "f", "private": true }\n' > frontend/package.json
  printf '<svg/>\n' > frontend/public/logo.svg
  git add -A
  git commit -qm "workspace in HEAD"

  # The premise: with the manifest present everywhere, this is a workspace.
  CI_GATE_MODE=ship run ci::changeset::_in_node_workspace frontend/public/logo.svg
  [ "$status" -eq 0 ]

  # Now the manifest is gone from the worktree and the index, and still in HEAD,
  # which is the tree a push sends.
  git rm -q --cached frontend/package.json
  rm -f frontend/package.json
  CI_GATE_MODE=ship run ci::changeset::_in_node_workspace frontend/public/logo.svg
  [ "$status" -eq 0 ] \
    || { echo "ship mode lost a workspace HEAD still declares" >&2; return 1; }

  # And the converse, which is the half a plain union would get wrong: a
  # manifest that exists only on disk describes a commit this push is not
  # sending, so it does not make the path a workspace member in ship mode.
  git rm -q -r --cached frontend >/dev/null
  git commit -qm "workspace removed from HEAD"
  mkdir -p frontend/public
  printf '{ "name": "f", "private": true }\n' > frontend/package.json
  CI_GATE_MODE=ship run ci::changeset::_in_node_workspace frontend/public/logo.svg
  [ "$status" -ne 0 ] \
    || { echo "ship mode counted a manifest the push does not send" >&2; return 1; }

  # The control: quick mode is the pre-commit gate and stands behind the index
  # and worktree, so the same file there is a workspace member. The rule chooses
  # which tree to read; it does not read both.
  CI_GATE_MODE=quick run ci::changeset::_in_node_workspace frontend/public/logo.svg
  [ "$status" -eq 0 ] \
    || { echo "quick mode stopped seeing the tree it stands behind" >&2; return 1; }
}
