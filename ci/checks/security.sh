#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/checks/common.sh
source "$ROOT_DIR/ci/checks/common.sh"

cd "$ROOT_DIR"

ci::common::section "Check: security"

matches=0

if [ -z "${CI_CHECKS_SECRET_PATTERN:-}" ]; then
  echo "CI_CHECKS_SECRET_PATTERN is missing or empty; cannot run security scan."
  exit "$CI_RESULT_FAIL_INFRA"
fi

scan_file() {
  local path="$1"
  local base_name=""
  [ -z "$path" ] && return 0
  [ ! -f "$path" ] && return 0

  base_name="$(basename "$path")"

  case "$base_name" in
    *.env.example|*.env.sample|*.example)
      ;;
    .env|.env.*|.env-*|env.local|env.local.*|*.env|*.env.*|*.env-*|*.pem|*.key|*.p12|*.pfx|*id_rsa|*id_ed25519)
      echo "Sensitive file path detected: $path"
      matches=1
      ;;
  esac

  local raw_hits=""
  raw_hits="$(grep -I -n -E "$CI_CHECKS_SECRET_PATTERN" "$path" 2>/dev/null || true)"
  if [ -n "$raw_hits" ]; then
    # Prevent the shared secret-pattern source file from matching its own
    # definitions; if this file moves, update this path check accordingly.
    if [ "$path" = "ci/checks/common.sh" ]; then
      raw_hits="$(printf '%s\n' "$raw_hits" | grep -Ev 'SECRET_PATTERN=|STAGED_SECRET_PATTERN=|grep -RInE "')"
    fi
    if [ -n "$raw_hits" ]; then
      echo "Potential secret-like content in $path:"
      printf '%s\n' "$raw_hits"
      matches=1
    fi
  fi
}

# Handle initial-commit repos with no HEAD: skip git diff --name-only and only
# inspect staged paths from git diff --cached --name-only.
while IFS= read -r path; do
  scan_file "$path"
done < <({
  if git rev-parse --verify HEAD >/dev/null 2>&1; then
    git diff --name-only
  fi
  git diff --cached --name-only
} | sort -u)

if [ "$matches" -ne 0 ]; then
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

audit_failed=0

if [ -f package.json ]; then
  if [ -f pnpm-lock.yaml ] && ci::common::command_exists pnpm; then
    echo "Running optional dependency audit: pnpm audit --audit-level high"
    if ! pnpm audit --audit-level high; then
      audit_failed=1
    fi
  elif [ -f yarn.lock ] && ci::common::command_exists yarn; then
    yarn_version="$(yarn --version 2>/dev/null || yarn -v 2>/dev/null || echo "1.0.0")"
    yarn_major="${yarn_version%%.*}"
    case "$yarn_major" in
      ''|*[!0-9]*)
        yarn_major=1
        ;;
    esac

    if [ "$yarn_major" -ge 2 ]; then
      echo "Running optional dependency audit: yarn npm audit --json (Yarn ${yarn_version}; fail on high/critical only)"
      set +e
      yarn_audit_output="$(yarn npm audit --json 2>&1)"
      yarn_audit_rc=$?
      set -e
    else
      echo "Running optional dependency audit: yarn audit --json (Yarn ${yarn_version}; fail on high/critical only)"
      set +e
      yarn_audit_output="$(yarn audit --json 2>&1)"
      yarn_audit_rc=$?
      set -e
    fi

    set +e
    has_high_or_critical=0
    if ci::common::command_exists jq; then
      if printf '%s\n' "$yarn_audit_output" | grep -E '^[[:space:]]*\{' | jq -r 'try (.data.advisory.severity // .severity // .advisory.severity // empty)' 2>/dev/null | grep -E '^(high|critical)$' >/dev/null 2>&1; then
        has_high_or_critical=1
      fi
    else
      if printf '%s\n' "$yarn_audit_output" | grep -E '"severity"[[:space:]]*:[[:space:]]*"(high|critical)"' >/dev/null 2>&1; then
        has_high_or_critical=1
      fi
    fi
    set -e

    printf '%s\n' "$yarn_audit_output"

    if [ "$has_high_or_critical" -eq 1 ]; then
      audit_failed=1
    fi

    if [ "$yarn_audit_rc" -ne 0 ] && ! printf '%s\n' "$yarn_audit_output" | grep -Eq '"severity"[[:space:]]*:'; then
      audit_failed=1
    fi
  elif [ -f package-lock.json ] && ci::common::command_exists npm; then
    echo "Running optional dependency audit: npm audit --audit-level=high"
    if ! npm audit --audit-level=high; then
      audit_failed=1
    fi
  else
    echo "Dependency audit skipped: no supported Node package manager detected."
  fi
fi

has_python_project=0
if [ -f requirements.txt ] || [ -f pyproject.toml ] || [ -f setup.cfg ] || [ -f setup.py ] || [ -f Pipfile ] || [ -f Pipfile.lock ] || [ -f poetry.lock ]; then
  has_python_project=1
fi

if [ "$has_python_project" -eq 1 ]; then
  requirements_file=""
  if [ -f requirements.txt ]; then
    requirements_file="requirements.txt"
  fi

  if ci::common::command_exists safety; then
    if [ -n "$requirements_file" ]; then
      requirements_dir="$(dirname "$requirements_file")"
      echo "Running optional dependency audit: safety scan --target $requirements_dir"
      if ! safety scan --target "$requirements_dir"; then
        audit_failed=1
      fi
    else
      echo "Optional audit skipped: safety requires requirements.txt for deterministic scanning."
    fi
  else
    echo "Optional tool skipped: safety not installed."
  fi

  if ci::common::command_exists pip-audit; then
    if [ -n "$requirements_file" ]; then
      echo "Running optional dependency audit: pip-audit -r $requirements_file"
      if ! pip-audit -r "$requirements_file"; then
        audit_failed=1
      fi
    elif [ -f pyproject.toml ]; then
      echo "Running optional dependency audit: pip-audit ."
      if ! pip-audit .; then
        audit_failed=1
      fi
    else
      echo "Optional audit skipped: pip-audit requires requirements.txt or pyproject.toml for deterministic scanning."
    fi
  else
    echo "Optional tool skipped: pip-audit not installed."
  fi
else
  echo "Optional tool skipped: no Python project detected for pip-audit."
fi

if [ "$audit_failed" -eq 1 ]; then
  echo "Dependency audit failed."
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

echo "Security checks passed."
exit "$CI_RESULT_PASS"
