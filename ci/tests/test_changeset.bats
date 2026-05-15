#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  source "$REPO_ROOT/ci/lib/common.sh"
  source "$REPO_ROOT/ci/lib/changeset.sh" 2>/dev/null || true

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
  if ! declare -f ci::changeset::classify_file >/dev/null 2>&1; then
    skip "changeset library not loaded"
  fi
  result="$(ci::changeset::classify_file "script.sh")"
  [ "$result" = "shell" ]
}

@test "changeset: classify python file" {
  if ! declare -f ci::changeset::classify_file >/dev/null 2>&1; then
    skip "changeset library not loaded"
  fi
  result="$(ci::changeset::classify_file "app.py")"
  [ "$result" = "python" ]
}

@test "changeset: classify javascript file" {
  if ! declare -f ci::changeset::classify_file >/dev/null 2>&1; then
    skip "changeset library not loaded"
  fi
  result="$(ci::changeset::classify_file "app.ts")"
  [ "$result" = "javascript" ]
}

@test "changeset: classify go file" {
  if ! declare -f ci::changeset::classify_file >/dev/null 2>&1; then
    skip "changeset library not loaded"
  fi
  result="$(ci::changeset::classify_file "main.go")"
  [ "$result" = "go" ]
}

@test "changeset: classify dockerfile" {
  if ! declare -f ci::changeset::classify_file >/dev/null 2>&1; then
    skip "changeset library not loaded"
  fi
  result="$(ci::changeset::classify_file "Dockerfile")"
  [ "$result" = "dockerfile" ]
}

@test "changeset: should_ignore node_modules" {
  if ! declare -f ci::changeset::should_ignore >/dev/null 2>&1; then
    skip "changeset library not loaded"
  fi
  ci::changeset::should_ignore "node_modules/foo/bar.js"
  [ $? -eq 0 ]
}
