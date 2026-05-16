#!/usr/bin/env bash
hello() {
  printf 'Hello, %s!\n' "${1:-World}"
}
hello "$@"
