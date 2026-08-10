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

# ---- Declared toolchain ------------------------------------------------------
#
# The workspace manifest states the toolchain its results are reproducible
# under. Accepting whatever `node` and the package manager happen to resolve to
# means a green lane vouches for nothing: the same commit can pass here and
# fail on a conforming machine, or the reverse, and neither result is evidence.
# Both assertions read the workspace's own package.json, so a manifest that
# declares nothing is unaffected; a manifest whose range cannot be evaluated is
# reported rather than assumed to hold.

# _manifest_field <dotted.path> – echo the value, or nothing when absent.
# node is already a hard requirement above and parses its own manifest format
# on any version, so this stays correct even while the version is in question.
_manifest_field() {
  node -e '
    const fs = require("fs");
    const pkg = JSON.parse(fs.readFileSync("package.json", "utf8"));
    const val = process.argv[1]
      .split(".")
      .reduce((o, k) => (o === null || o === undefined ? o : o[k]), pkg);
    if (val !== null && val !== undefined) process.stdout.write(String(val));
  ' "$1" 2>/dev/null || true
}

# _semver_part <version> <1|2|3> – echo one numeric component, defaulting to 0.
# An absent component really is 0: `cut -d. -f2` would echo the whole string for
# a version like "23", making ">=22.12.0 <23" accept 23.1.0.
_semver_part() {
  printf '%s' "${1#v}" | awk -v i="$2" '{
    v = $0
    sub(/[-+].*$/, "", v)
    n = split(v, p, ".")
    x = (i <= n ? p[i] : "0")
    gsub(/[^0-9]/, "", x)
    print (x == "" ? "0" : x)
  }'
}

# _semver_cmp <a> <b> – echo -1, 0 or 1. Pre-release suffixes are dropped: a
# gate that accepted 22.12.0-rc for a >=22.12.0 requirement would be lying by a
# hair, but the check exists to catch whole-version drift, not tag splitting.
_semver_cmp() {
  local i av bv
  for i in 1 2 3; do
    av="$(_semver_part "$1" "$i")"
    bv="$(_semver_part "$2" "$i")"
    if [ "$av" -gt "$bv" ]; then printf '1'; return 0; fi
    if [ "$av" -lt "$bv" ]; then printf -- '-1'; return 0; fi
  done
  printf '0'
}

# _semver_token_ok <version> <token> – 0 satisfied, 1 unsatisfied,
# 2 unrecognised. The third status is what keeps an exotic range (a hyphen
# range, say) from being silently read as "fine".
_semver_token_ok() {
  local version="$1" tok="$2" want i spec
  case "$tok" in
    '*'|'x'|'X'|'')
      return 0
      ;;
    '>='*)
      want="${tok#>=}"
      if [ "$(_semver_cmp "$version" "$want")" != "-1" ]; then return 0; fi
      return 1
      ;;
    '<='*)
      want="${tok#<=}"
      if [ "$(_semver_cmp "$version" "$want")" != "1" ]; then return 0; fi
      return 1
      ;;
    '>'*)
      want="${tok#>}"
      if [ "$(_semver_cmp "$version" "$want")" = "1" ]; then return 0; fi
      return 1
      ;;
    '<'*)
      want="${tok#<}"
      if [ "$(_semver_cmp "$version" "$want")" = "-1" ]; then return 0; fi
      return 1
      ;;
    '^'*)
      want="${tok#^}"
      if [ "$(_semver_cmp "$version" "$want")" = "-1" ]; then return 1; fi
      if [ "$(_semver_part "$version" 1)" != "$(_semver_part "$want" 1)" ]; then return 1; fi
      # Below 1.0.0 the minor carries the breaking change, so ^0.2.3 admits
      # 0.2.x only. Comparing majors alone would accept 0.3.0.
      if [ "$(_semver_part "$want" 1)" = "0" ] \
        && [ "$(_semver_part "$version" 2)" != "$(_semver_part "$want" 2)" ]; then
        return 1
      fi
      return 0
      ;;
    '~'*)
      want="${tok#\~}"
      if [ "$(_semver_cmp "$version" "$want")" = "-1" ]; then return 1; fi
      if [ "$(_semver_part "$version" 1)" != "$(_semver_part "$want" 1)" ]; then return 1; fi
      if [ "$(_semver_part "$version" 2)" != "$(_semver_part "$want" 2)" ]; then return 1; fi
      return 0
      ;;
    '='*)
      want="${tok#=}"
      if [ "$(_semver_cmp "$version" "$want")" = "0" ]; then return 0; fi
      return 1
      ;;
    [0-9]*|v[0-9]*)
      # A bare version is exact, except where a component is a wildcard: 22.x
      # pins the major only. Compare the components the range actually states.
      for i in 1 2 3; do
        spec="$(printf '%s' "${tok#v}" | cut -d. -f"$i")"
        case "$spec" in
          ''|'x'|'X'|'*') return 0 ;;
        esac
        if [ "$(_semver_part "$version" "$i")" != "$(_semver_part "$tok" "$i")" ]; then
          return 1
        fi
      done
      return 0
      ;;
    *)
      return 2
      ;;
  esac
}

# _semver_satisfies <version> <range> – 0 satisfied, 1 unsatisfied,
# 2 unverifiable. Alternatives split on "||"; tokens within one alternative are
# conjunctive, as npm defines them.
_semver_satisfies() {
  local version="$1" rest="$2"
  local alt tok ok tokens=0 unverifiable=0 rc more=1
  while [ "$more" -eq 1 ]; do
    case "$rest" in
      *'||'*) alt="${rest%%||*}"; rest="${rest#*||}" ;;
      *)      alt="$rest"; more=0 ;;
    esac
    ok=1
    # Word-splitting is the point: the tokens of one alternative are whitespace
    # separated and conjunctive, as npm defines them. Globbing is disabled
    # around the split because a bare "*" range would otherwise expand to the
    # directory listing and stop looking like a range at all.
    set -f
    # shellcheck disable=SC2086
    for tok in $alt; do
      tokens=$((tokens + 1))
      rc=0
      _semver_token_ok "$version" "$tok" || rc=$?
      if [ "$rc" -eq 2 ]; then
        unverifiable=1
      fi
      # No early exit: a later token in this alternative may be the
      # unrecognised one, and "cannot evaluate" must win over "does not
      # satisfy" so the message names the real problem.
      if [ "$rc" -ne 0 ]; then
        ok=0
      fi
    done
    set +f
    if [ "$tokens" -gt 0 ] && [ "$ok" -eq 1 ]; then
      return 0
    fi
  done
  if [ "$unverifiable" -eq 1 ] || [ "$tokens" -eq 0 ]; then
    return 2
  fi
  return 1
}

DECLARED_NODE="$(_manifest_field engines.node)"
if [ -n "$DECLARED_NODE" ]; then
  ACTUAL_NODE="$(node --version 2>/dev/null || true)"
  _node_rc=0
  _semver_satisfies "$ACTUAL_NODE" "$DECLARED_NODE" || _node_rc=$?
  case "$_node_rc" in
    0) ;;
    1)
      echo "Node ${ACTUAL_NODE:-<unknown>} does not satisfy the engines.node range"
      echo "  declared by ${CI_GATE_NODE_WORKSPACE}/package.json: ${DECLARED_NODE}"
      echo "  Results produced under an undeclared runtime are not reproducible,"
      echo "  so the lane stops before installing or running anything."
      exit "$CI_RESULT_FAIL_INFRA"
      ;;
    *)
      echo "Cannot evaluate the engines.node range declared by"
      echo "  ${CI_GATE_NODE_WORKSPACE}/package.json: ${DECLARED_NODE}"
      echo "  The lane refuses to run rather than assume the toolchain conforms."
      exit "$CI_RESULT_FAIL_INFRA"
      ;;
  esac
fi

DECLARED_PM="$(_manifest_field packageManager)"
if [ -n "$DECLARED_PM" ]; then
  # corepack's format is name@version, optionally with a +integrity suffix. The
  # version is a pin, not a range: two patch releases of a package manager can
  # resolve a lockfile differently.
  PM_NAME="${DECLARED_PM%%@*}"
  PM_VERSION="${DECLARED_PM#*@}"
  PM_VERSION="${PM_VERSION%%+*}"

  if [ "$PM_NAME" != "$MANAGER" ]; then
    echo "The lockfile in ${CI_GATE_NODE_WORKSPACE} selects ${MANAGER}, but"
    echo "  package.json declares packageManager ${DECLARED_PM}."
    echo "  Installing with a manager the manifest does not declare produces a"
    echo "  tree the lockfile does not describe."
    exit "$CI_RESULT_FAIL_INFRA"
  fi

  ci::common::command_exists "$PM_NAME" || {
    echo "package.json declares packageManager ${DECLARED_PM} but ${PM_NAME} is missing."
    exit "$CI_RESULT_FAIL_INFRA"
  }
  ACTUAL_PM="$("$PM_NAME" --version 2>/dev/null || true)"
  ACTUAL_PM="${ACTUAL_PM#v}"
  if [ "$ACTUAL_PM" != "$PM_VERSION" ]; then
    echo "${PM_NAME} ${ACTUAL_PM:-<unknown>} is installed, but"
    echo "  ${CI_GATE_NODE_WORKSPACE}/package.json pins packageManager ${DECLARED_PM}."
    echo "  Install the declared version (corepack, or the manager's own"
    echo "  upgrade path) so the install is the one the lockfile describes."
    exit "$CI_RESULT_FAIL_INFRA"
  fi
fi

# The fingerprint covers the manifest as well as the lockfile. Keyed on the
# lockfile alone, a package.json edited without a matching lockfile update looks
# cached, the frozen install that would have caught the mismatch is skipped, and
# tests pass as long as they do not touch the changed dependency.
_deps_fingerprint() {
  printf '%s %s\n' \
    "$(ci::common::hash_file "$LOCKFILE")" \
    "$(ci::common::hash_file package.json)"
}

SKIP_INSTALL=0
if [ -n "$LOCKFILE" ] && [ -d "node_modules" ] && [ -f "$NODE_HASH_FILE" ]; then
  CURRENT_HASH="$(_deps_fingerprint)"
  CACHED_HASH="$(cat "$NODE_HASH_FILE")"
  if [ "$CURRENT_HASH" = "$CACHED_HASH" ]; then
    echo "node_modules up to date. Skipping install."
    SKIP_INSTALL=1
  fi
fi

# Dependency installation is provisioning, not a code result. Under set -e a
# registry outage or auth failure exits 1, which normalize_result would map to
# FAIL_NEW_ISSUE and blame the code for a broken environment. Each install is
# pinned to FAIL_INFRA explicitly so the classification survives that mapping.
if [ "$SKIP_INSTALL" = "0" ]; then
  case "$MANAGER" in
  pnpm)
    ci::common::command_exists pnpm || { echo "pnpm lockfile found but pnpm is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: pnpm install --frozen-lockfile"
    pnpm install --frozen-lockfile || exit "$CI_RESULT_FAIL_INFRA"
    ;;
  npm)
    ci::common::command_exists npm || { echo "npm lockfile found but npm is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: npm ci --quiet"
    npm ci --quiet || exit "$CI_RESULT_FAIL_INFRA"
    ;;
  yarn)
    ci::common::command_exists yarn || { echo "yarn lockfile found but yarn is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    YARN_VERSION="$(yarn --version 2>/dev/null || echo "0")"
    YARN_MAJOR="${YARN_VERSION%%.*}"
    if [ "$YARN_MAJOR" = "1" ]; then
      echo "Installing dependencies: yarn install --frozen-lockfile"
      yarn install --frozen-lockfile || exit "$CI_RESULT_FAIL_INFRA"
    else
      echo "Installing dependencies: yarn install --immutable"
      yarn install --immutable || exit "$CI_RESULT_FAIL_INFRA"
    fi
    ;;
  bun)
    ci::common::command_exists bun || { echo "bun lockfile found but bun is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: bun install --frozen-lockfile"
    bun install --frozen-lockfile || exit "$CI_RESULT_FAIL_INFRA"
    ;;
  esac

  mkdir -p "$(dirname "$NODE_HASH_FILE")"
  _deps_fingerprint > "$NODE_HASH_FILE"
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

# A workspace that ships tests must be able to run them. run_script only logs
# "Skipping missing script", so deleting or renaming `test` would remove the
# whole suite from the gate while the lane still exits 0 — the suite passing by
# never running, which is the failure this gate exists to catch. Keyed on the
# presence of test files rather than on checks.yml, so the rule travels with the
# workspace and a genuinely test-free workspace is unaffected.
if ! script_exists "test" && ! script_exists "test:unit"; then
  ORPHAN_TESTS="$(find . \( -name 'node_modules' -o -name 'dist' -o -name 'build' \) -prune -o \
    -type f \( -name '*.test.*' -o -name '*.spec.*' \) -print 2>/dev/null | head -5 || true)"
  if [ -n "$ORPHAN_TESTS" ]; then
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} ships tests but defines no 'test' or 'test:unit' script."
    echo "  These would never run:"
    # Read line by line: a path with a space is one file, not two.
    while IFS= read -r _orphan; do
      [ -n "$_orphan" ] || continue
      echo "    $_orphan"
    done <<< "$ORPHAN_TESTS"
    echo "  Restore the script in package.json, or remove the tests."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi
fi

run_script "format:check"
run_script "lint"
run_script "typecheck"
run_script "test"
run_script "test:unit"
run_script "build"

echo "Node lane passed."
exit "$CI_RESULT_PASS"
