#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT_DIR"

for hook in .githooks/pre-push .githooks/pre-commit .githooks/commit-msg .githooks/prepare-commit-msg; do
  if [ ! -f "$hook" ]; then
    echo "ERROR: $hook not found. Hook installation failed." >&2
    exit 1
  fi
done

if [ ! -f ci/hook-dispatch.sh ]; then
  echo "ERROR: ci/hook-dispatch.sh not found." >&2
  exit 1
fi

chmod +x ci/hook-dispatch.sh
for hook in .githooks/pre-push .githooks/pre-commit .githooks/commit-msg .githooks/prepare-commit-msg; do
  chmod +x "$hook"
done

git config core.hooksPath .githooks

echo "Git hooks installed."
echo "core.hooksPath=$(git config core.hooksPath)"
echo ""
echo "Installed hooks:"
echo "  pre-commit         — fast checks (lint + format + secrets) on staged files, budget <= 10s"
echo "  commit-msg         — conventional commit message lint"
echo "  prepare-commit-msg — inject ticket ID from branch name"
echo "  pre-push           — full affected gate, budget <= 2 min, blocking"
echo ""
echo "All hooks dispatch through ci/hook-dispatch.sh"

# Said here because this is the moment the blocker becomes live.
#
# Any change under ci/ schedules `tests-shell`, which refuses with FAIL_INFRA
# when bats is missing -- deliberately, since a lane reporting PASS without
# running those suites is worse than no lane. But nothing provisions bats:
# `uv sync` does not, and neither does this script. So a fresh clone that
# followed the documented setup had every push touching ci/ blocked, with the
# refusal arriving at push time rather than at setup time and no in-repo way to
# resolve it. This is that half.
if ! command -v bats >/dev/null 2>&1 \
  && [ ! -x ".ci-gate/bats/bin/bats" ]; then
  echo ""
  echo "NOTE: bats is not installed, and the pre-push hook you just enabled"
  echo "      blocks any change under ci/ without it (the 'tests-shell' lane)."
  echo ""
  echo "      Provision a pinned copy into this worktree — no sudo, nothing"
  echo "      outside the repository, 'rm -rf .ci-gate/bats' undoes it:"
  echo ""
  echo "          make bats-install"
  echo ""
  echo "      A bats already on PATH is used in preference to that one."
fi
