#!/usr/bin/env bats

@test "hello outputs greeting" {
  run bash "$(dirname "$BATS_TEST_FILENAME")/../hello.sh" World
  [ "$status" -eq 0 ]
  [ "$output" = "Hello, World!" ]
}

@test "hello uses default name" {
  run bash "$(dirname "$BATS_TEST_FILENAME")/../hello.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "Hello, World!" ]
}
