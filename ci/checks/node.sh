#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"

cd "$ROOT_DIR"

ci::common::section "Check: node lane"

# The lane body below handles exactly one workspace. When invoked normally we
# discover the workspaces and re-enter once per directory; CI_GATE_NODE_WORKSPACE
# marks that recursive call. A repo with a root package.json resolves to "."
# and behaves exactly as before.
if [ -z "${CI_GATE_NODE_WORKSPACE:-}" ]; then
  NODE_WORKSPACES="$(ci::common::node_workspaces package.json)"

  if [ -z "$NODE_WORKSPACES" ]; then
    echo "No package.json found. Skipping Node lane."
    exit "$CI_RESULT_PASS"
  fi

  NODE_OVERALL="$CI_RESULT_PASS"
  while IFS= read -r _ws; do
    [ -n "$_ws" ] || continue
    echo ""
    echo "--- Node lane workspace: ${_ws}"
    _ws_rc=0
    CI_GATE_NODE_WORKSPACE="$_ws" bash "$SCRIPT_DIR/node.sh" "$@" || _ws_rc=$?
    # A failing package script exits 1, which is not a gate result code and
    # would otherwise be merged in at the FAIL_INFRA level — a real test,
    # typecheck or build regression reported as broken infrastructure.
    _ws_rc="$(ci::common::normalize_result "$_ws_rc")"
    NODE_OVERALL="$(ci::common::merge_results "$NODE_OVERALL" "$_ws_rc")"
  done <<< "$NODE_WORKSPACES"

  exit "$NODE_OVERALL"
fi

cd "$ROOT_DIR/$CI_GATE_NODE_WORKSPACE"

if [ ! -f package.json ]; then
  echo "No package.json found in ${CI_GATE_NODE_WORKSPACE}. Skipping Node lane."
  exit "$CI_RESULT_PASS"
fi

# Dependency-cache fingerprints stay at the repo root so enabling a
# subdirectory workspace does not scatter .ci-gate/ dirs through the tree.
if [ "$CI_GATE_NODE_WORKSPACE" = "." ]; then
  WS_SLUG="root"
else
  WS_SLUG="$(printf '%s' "$CI_GATE_NODE_WORKSPACE" | tr '/' '-')"
fi
NODE_HASH_FILE="$ROOT_DIR/.ci-gate/node_modules-${WS_SLUG}.hash"

if ! ci::common::command_exists node; then
  echo "package.json exists but node is not installed."
  exit "$CI_RESULT_FAIL_INFRA"
fi

MANAGER=""
LOCKFILE=""
LOCKFILE_COUNT=0

if [ -f pnpm-lock.yaml ]; then
  MANAGER="pnpm"
  LOCKFILE="${LOCKFILE:-pnpm-lock.yaml}"
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi
if [ -f package-lock.json ] || [ -f npm-shrinkwrap.json ]; then
  MANAGER="${MANAGER:-npm}"
  if [ -z "$LOCKFILE" ]; then
    if [ -f npm-shrinkwrap.json ]; then
      LOCKFILE="npm-shrinkwrap.json"
    else
      LOCKFILE="package-lock.json"
    fi
  fi
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi
if [ -f yarn.lock ]; then
  MANAGER="${MANAGER:-yarn}"
  LOCKFILE="${LOCKFILE:-yarn.lock}"
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi
if [ -f bun.lockb ] || [ -f bun.lock ]; then
  MANAGER="${MANAGER:-bun}"
  if [ -z "$LOCKFILE" ]; then
    if [ -f bun.lock ]; then
      LOCKFILE="bun.lock"
    else
      LOCKFILE="bun.lockb"
    fi
  fi
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi

if [ "$LOCKFILE_COUNT" -gt 1 ]; then
  echo "Multiple lockfiles detected. Resolve package-manager ambiguity first."
  exit "$CI_RESULT_FAIL_INFRA"
fi

if [ -z "$MANAGER" ]; then
  echo "No lockfile detected for package.json. Refusing mutable install."
  exit "$CI_RESULT_FAIL_INFRA"
fi

SKIP_INSTALL=0
if [ -n "$LOCKFILE" ] && [ -d "node_modules" ] && [ -f "$NODE_HASH_FILE" ]; then
  CURRENT_HASH="$(ci::common::hash_file "$LOCKFILE")"
  CACHED_HASH="$(cat "$NODE_HASH_FILE")"
  if [ "$CURRENT_HASH" = "$CACHED_HASH" ]; then
    echo "node_modules up to date. Skipping install."
    SKIP_INSTALL=1
  fi
fi

if [ "$SKIP_INSTALL" = "0" ]; then
  case "$MANAGER" in
  pnpm)
    ci::common::command_exists pnpm || { echo "pnpm lockfile found but pnpm is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: pnpm install --frozen-lockfile"
    pnpm install --frozen-lockfile
    ;;
  npm)
    ci::common::command_exists npm || { echo "npm lockfile found but npm is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: npm ci --quiet"
    npm ci --quiet
    ;;
  yarn)
    ci::common::command_exists yarn || { echo "yarn lockfile found but yarn is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    YARN_VERSION="$(yarn --version 2>/dev/null || echo "0")"
    YARN_MAJOR="${YARN_VERSION%%.*}"
    if [ "$YARN_MAJOR" = "1" ]; then
      echo "Installing dependencies: yarn install --frozen-lockfile"
      yarn install --frozen-lockfile
    else
      echo "Installing dependencies: yarn install --immutable"
      yarn install --immutable
    fi
    ;;
  bun)
    ci::common::command_exists bun || { echo "bun lockfile found but bun is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: bun install --frozen-lockfile"
    bun install --frozen-lockfile
    ;;
  esac

  mkdir -p "$(dirname "$NODE_HASH_FILE")"
  ci::common::hash_file "$LOCKFILE" > "$NODE_HASH_FILE"
fi

case "$MANAGER" in
  pnpm) ci::common::command_exists pnpm || { echo "pnpm is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
  npm) ci::common::command_exists npm || { echo "npm is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
  yarn) ci::common::command_exists yarn || { echo "yarn is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
  bun) ci::common::command_exists bun || { echo "bun is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
esac

PACKAGE_SCRIPTS="$(node -e "try{const p=require('./package.json');console.log(Object.keys(p.scripts||{}).join('\n'))}catch(e){console.error('Invalid package.json:',e.message);process.exit(1)}" 2>&1)" || {
  echo "$PACKAGE_SCRIPTS" >&2
  echo "Failed to read scripts from package.json."
  exit "$CI_RESULT_FAIL_INFRA"
}

script_exists() {
  local script_name="$1"
  printf '%s\n' "$PACKAGE_SCRIPTS" | grep -Fx -- "$script_name" >/dev/null 2>&1
}

run_script() {
  local script_name="$1"
  if script_exists "$script_name"; then
    echo "Running script: $script_name"
    case "$MANAGER" in
      pnpm) pnpm run "$script_name" ;;
      npm) npm run "$script_name" ;;
      yarn) yarn run "$script_name" ;;
      bun) bun run "$script_name" ;;
    esac
  else
    echo "Skipping missing script: $script_name"
  fi
}

run_script "format:check"
run_script "lint"
run_script "typecheck"
run_script "test"
run_script "test:unit"
run_script "build"

echo "Node lane passed."
exit "$CI_RESULT_PASS"
