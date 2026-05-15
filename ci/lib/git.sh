#!/usr/bin/env bash
set -Eeuo pipefail

ci::git::current_branch() {
  git branch --show-current 2>/dev/null || true
}

ci::git::current_sha() {
  git rev-parse HEAD 2>/dev/null || true
}

ci::git::changed_files() {
  git diff --name-only 2>/dev/null || true
}

ci::git::staged_files() {
  git diff --cached --name-only 2>/dev/null || true
}

ci::git::untracked_files() {
  git ls-files --others --exclude-standard 2>/dev/null || true
}

ci::git::is_repo() {
  git rev-parse --git-dir >/dev/null 2>&1
}

ci::git::has_conflict_markers_in_staged() {
  git diff --cached -U0 | grep -E '^\+[[:space:]]*(<{7}|={7}|>{7})([[:space:]]|$)' >/dev/null 2>&1
}

ci::git::has_conflict_markers_in_changed() {
  git diff -U0 | grep -E '^\+[[:space:]]*(<{7}|={7}|>{7})([[:space:]]|$)' >/dev/null 2>&1
}
