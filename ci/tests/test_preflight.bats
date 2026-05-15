#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  cd "$REPO_ROOT"
}

@test "preflight: --help exits 0" {
  run bash ci/preflight.sh --help
  [ "$status" -eq 0 ]
}

@test "preflight: --mode quick exits without error" {
  run bash ci/preflight.sh --mode quick
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]
}

@test "preflight: invalid mode exits non-zero" {
  run bash ci/preflight.sh --mode invalid_mode_xyz
  [ "$status" -ne 0 ]
}

@test "preflight: syntax check passes" {
  bash -n ci/preflight.sh
}

@test "preflight: all checks have correct syntax" {
  local failed=0
  for script in ci/checks/*.sh ci/lib/*.sh; do
    if [ -f "$script" ]; then
      if ! bash -n "$script" 2>/dev/null; then
        echo "FAIL: $script" >&2
        failed=1
      fi
    fi
  done
  [ "$failed" -eq 0 ]
}

@test "preflight: exit codes follow convention" {
  source ci/lib/common.sh
  [ "$CI_RESULT_PASS" -eq 0 ]
  [ "$CI_RESULT_PASS_WITH_KNOWN_DEBT" -eq 10 ]
  [ "$CI_RESULT_FAIL_NEW_ISSUE" -eq 20 ]
  [ "$CI_RESULT_FAIL_INFRA" -eq 30 ]
}
