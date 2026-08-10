#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  source "$REPO_ROOT/ci/lib/common.sh"
  source "$REPO_ROOT/ci/lib/cache.sh" 2>/dev/null || true

  export CI_GATE_CACHE_DIR="$(mktemp -d)"
}

teardown() {
  rm -rf "$CI_GATE_CACHE_DIR" 2>/dev/null || true
}

@test "cache: init creates cache directory" {
  if ! declare -f ci::cache::init >/dev/null 2>&1; then
    skip "cache library not loaded"
  fi
  ci::cache::init
  [ -d "$CI_GATE_CACHE_DIR" ]
}

@test "cache: miss on empty cache" {
  if ! declare -f ci::cache::hit >/dev/null 2>&1; then
    skip "cache library not loaded"
  fi
  ci::cache::init
  ! ci::cache::hit "test-key-$(date +%s)"
}

@test "cache: hit after put" {
  if ! declare -f ci::cache::put >/dev/null 2>&1; then
    skip "cache library not loaded"
  fi
  ci::cache::init
  src="$(mktemp -d)"
  # These filenames are the cache contract, not arbitrary fixture names:
  # ci::cache::hit requires result.txt, and ci/preflight.sh writes result.txt
  # and output.txt. A fixture using result/output.log stores an entry that can
  # never be a hit, which is why this test failed against a working cache.
  echo "0" > "$src/result.txt"
  echo "test output" > "$src/output.txt"

  ci::cache::put "test-key-12345" "$src"
  ci::cache::hit "test-key-12345"

  rm -rf "$src"
}

@test "cache: an entry missing result.txt is not a hit" {
  # The other half of the contract: put must not make an incomplete entry look
  # cached, or a check would be skipped with nothing to restore.
  if ! declare -f ci::cache::put >/dev/null 2>&1; then
    skip "cache library not loaded"
  fi
  ci::cache::init
  src="$(mktemp -d)"
  echo "test output" > "$src/output.txt"

  ci::cache::put "test-key-incomplete" "$src"
  ! ci::cache::hit "test-key-incomplete"

  rm -rf "$src"
}

@test "cache: key computation is deterministic" {
  if ! declare -f ci::cache::key >/dev/null 2>&1; then
    skip "cache library not loaded"
  fi
  key1="$(ci::cache::key "shellcheck" "0.9.0" "abc123" "def456")"
  key2="$(ci::cache::key "shellcheck" "0.9.0" "abc123" "def456")"
  [ "$key1" = "$key2" ]
}
