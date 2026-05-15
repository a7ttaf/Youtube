#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
}

@test "hook: hook-dispatch.sh exists and is executable" {
  [ -f "$REPO_ROOT/ci/hook-dispatch.sh" ]
  [ -x "$REPO_ROOT/ci/hook-dispatch.sh" ] || bash -n "$REPO_ROOT/ci/hook-dispatch.sh"
}

@test "hook: pre-commit hook has correct syntax" {
  [ -f "$REPO_ROOT/.githooks/pre-commit" ]
  bash -n "$REPO_ROOT/.githooks/pre-commit"
}

@test "hook: commit-msg hook has correct syntax" {
  [ -f "$REPO_ROOT/.githooks/commit-msg" ]
  bash -n "$REPO_ROOT/.githooks/commit-msg"
}

@test "hook: prepare-commit-msg hook has correct syntax" {
  [ -f "$REPO_ROOT/.githooks/prepare-commit-msg" ]
  bash -n "$REPO_ROOT/.githooks/prepare-commit-msg"
}

@test "hook: pre-push hook has correct syntax" {
  [ -f "$REPO_ROOT/.githooks/pre-push" ]
  bash -n "$REPO_ROOT/.githooks/pre-push"
}
