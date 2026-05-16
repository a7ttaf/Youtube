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
