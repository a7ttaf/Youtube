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

security_path_for_log() {
  if printf '%s' "$1" | grep -a -q -E "$CI_CHECKS_SECRET_PATTERN"; then
    printf '[redacted-secret-like-path]'
  else
    printf '%q' "$1"
  fi
}

scan_sensitive_path() {
  local path="$1"
  local base_name="" policy_base_name="" path_for_log=""
  [ -z "$path" ] && return 0

  path_for_log="$(security_path_for_log "$path")"
  base_name="${path##*/}"
  # Git preserves case while Windows path lookup does not. Normalize only the
  # policy comparison; object reads and diagnostics retain the exact Git path.
  policy_base_name="$(printf '%s' "$base_name" | LC_ALL=C tr '[:upper:]' '[:lower:]')"

  case "$policy_base_name" in
    *.env.example|*.env.sample|*.example)
      ;;
    .env|.env.*|.env-*|env.local|env.local.*|*.env|*.env.*|*.env-*|*.pem|*.key|*.p12|*.pfx|*id_rsa|*id_ed25519)
      echo "Sensitive file path detected: $path_for_log"
      matches=1
      ;;
  esac

  # A credential can be leaked in a tree entry even when the blob is harmless.
  # Feed the raw decoded path through stdin so leading dashes are never options.
  if printf '%s' "$path" | grep -a -q -E "$CI_CHECKS_SECRET_PATTERN"; then
    echo "Potential secret-like content in path: $path_for_log"
    matches=1
  fi
}

record_secret_hits() {
  local path="$1" raw_hits="$2" filtered_hits="" hit="" hit_content="" path_for_log=""
  local canonical_pattern_record=""

  if [ -n "$raw_hits" ]; then
    # Exempt only the canonical regex record itself. Broad substring filters
    # let a real credential hide behind a trailing `# SECRET_PATTERN=` comment.
    if [ "$path" = "ci/checks/common.sh" ]; then
      canonical_pattern_record="$(printf '%s%s' 'DATABASE' "_URL=[^[:space:]]+'")"
      filtered_hits=""
      while IFS= read -r hit; do
        hit_content="${hit#*:}"
        if [ "$hit_content" = "$canonical_pattern_record" ]; then
          continue
        fi
        filtered_hits="${filtered_hits}${filtered_hits:+$'\n'}${hit}"
      done <<< "$raw_hits"
      raw_hits="$filtered_hits"
    fi
    if [ -n "$raw_hits" ]; then
      local hit_lines=""
      hit_lines="$(printf '%s\n' "$raw_hits" | sed 's/:.*$//' | sort -n -u | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
      path_for_log="$(security_path_for_log "$path")"
      echo "Potential secret-like content in $path_for_log at line(s): ${hit_lines:-unknown}"
      matches=1
    fi
  fi
}

scan_file() {
  local path="$1" filesystem_path="" raw_hits=""
  [ -z "$path" ] && return 0

  scan_sensitive_path "$path"
  # Git reports repository-relative paths without a `./` prefix. Add one for
  # filesystem consumers because GNU grep treats the bare operand `-` as stdin
  # even after option termination; `./-` is unambiguously a file name.
  filesystem_path="./$path"
  if [ -L "$filesystem_path" ]; then
    ensure_security_temp_dir
    # Never follow a worktree symlink during a secret scan. The link text is
    # the byte payload Git stores; following it could read outside the project.
    readlink -- "$filesystem_path" > "$security_temp_dir/blob" \
      || security_infra_failure "Security could not read symlink payload: $(security_path_for_log "$path")"
    raw_hits="$(grep -a -n -o -E "$CI_CHECKS_SECRET_PATTERN" -- "$security_temp_dir/blob" 2>/dev/null || true)"
  elif [ -f "$filesystem_path" ]; then
    raw_hits="$(grep -a -n -o -E "$CI_CHECKS_SECRET_PATTERN" -- "$filesystem_path" 2>/dev/null || true)"
  else
    return 0
  fi
  record_secret_hits "$path" "$raw_hits"
}

security_infra_failure() {
  echo "$1"
  exit "$CI_RESULT_FAIL_INFRA"
}

security_temp_dir=""

cleanup_security_temp() {
  [ -z "$security_temp_dir" ] || rm -rf -- "$security_temp_dir"
}

trap cleanup_security_temp EXIT

ensure_security_temp_dir() {
  [ -n "$security_temp_dir" ] && return 0
  security_temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/ums-security.XXXXXX")" \
    || security_infra_failure "Security could not create its scan workspace."
}

scan_blob_additions() {
  local path="$1" old_blob="$2" new_blob="$3" diff_rc=0 raw_hits=""

  # A NUL byte must not suppress either the diff or the secret matcher. Force
  # Git and grep to treat arbitrary committed blob bytes as searchable text.
  set +e
  git diff --no-index --text --no-ext-diff --no-textconv --unified=0 \
    --output-indicator-new='>' -- "$old_blob" "$new_blob" \
    > "$security_temp_dir/blob-diff" 2>/dev/null
  diff_rc=$?
  set -e
  [ "$diff_rc" -le 1 ] \
    || security_infra_failure "Security could not compare committed blob content: $(security_path_for_log "$path")"

  # Git's dedicated new-line indicator avoids confusing diff headers with
  # content. Removing exactly one marker preserves a content line beginning >.
  sed -n 's/^>//p' "$security_temp_dir/blob-diff" > "$security_temp_dir/added-lines" \
    || security_infra_failure "Security could not extract committed additions: $(security_path_for_log "$path")"
  raw_hits="$(grep -a -n -o -E "$CI_CHECKS_SECRET_PATTERN" -- "$security_temp_dir/added-lines" 2>/dev/null || true)"
  record_secret_hits "$path" "$raw_hits"
}

scan_commit_object_delta() {
  local old_oid="$1" new_oid="$2" old_mode="$3" new_mode="$4" path="$5"
  local old_type="" new_type="" raw_hits=""

  scan_sensitive_path "$path"
  # A gitlink names an object in another repository, which need not exist in
  # this object database. Its path is auditable, but no local blob is available.
  [ "$new_mode" = "160000" ] && return 0

  if ! new_type="$(git cat-file -t "$new_oid" 2>/dev/null)"; then
    security_infra_failure "Security could not read committed object: $new_oid ($(security_path_for_log "$path"))"
  fi
  [ "$new_type" = "blob" ] \
    || security_infra_failure "Security expected a committed blob for: $(security_path_for_log "$path")"
  if ! git cat-file blob "$new_oid" > "$security_temp_dir/blob-new"; then
    security_infra_failure "Security could not materialize committed blob: $new_oid ($(security_path_for_log "$path"))"
  fi

  if [ "$old_mode" = "160000" ] || printf '%s' "$old_oid" | grep -Eq '^0+$'; then
    raw_hits="$(grep -a -n -o -E "$CI_CHECKS_SECRET_PATTERN" -- "$security_temp_dir/blob-new" 2>/dev/null || true)"
    record_secret_hits "$path" "$raw_hits"
    return 0
  fi
  if ! old_type="$(git cat-file -t "$old_oid" 2>/dev/null)"; then
    security_infra_failure "Security could not read prior committed object: $old_oid ($(security_path_for_log "$path"))"
  fi
  if [ "$old_type" != "blob" ]; then
    raw_hits="$(grep -a -n -o -E "$CI_CHECKS_SECRET_PATTERN" -- "$security_temp_dir/blob-new" 2>/dev/null || true)"
    record_secret_hits "$path" "$raw_hits"
    return 0
  fi
  if ! git cat-file blob "$old_oid" > "$security_temp_dir/blob-old"; then
    security_infra_failure "Security could not materialize prior committed blob: $old_oid ($(security_path_for_log "$path"))"
  fi
  scan_blob_additions "$path" "$security_temp_dir/blob-old" "$security_temp_dir/blob-new"
}

scan_index_blob() {
  local new_oid="$1" new_mode="$2" path="$3" object_type="" raw_hits=""

  scan_sensitive_path "$path"
  [ "$new_mode" = "160000" ] && return 0
  if ! object_type="$(git cat-file -t "$new_oid" 2>/dev/null)"; then
    security_infra_failure "Security could not read staged object: $(security_path_for_log "$path")"
  fi
  [ "$object_type" = "blob" ] \
    || security_infra_failure "Security expected a staged blob for: $(security_path_for_log "$path")"
  if ! git cat-file blob "$new_oid" > "$security_temp_dir/blob"; then
    security_infra_failure "Security could not materialize staged blob: $(security_path_for_log "$path")"
  fi
  raw_hits="$(grep -a -n -o -E "$CI_CHECKS_SECRET_PATTERN" -- "$security_temp_dir/blob" 2>/dev/null || true)"
  record_secret_hits "$path" "$raw_hits"
}

scan_index_deltas() {
  local metadata="" path="" old_mode="" new_mode="" old_oid="" new_oid="" status="" extra=""

  while IFS= read -r -d '' metadata <&3; do
    IFS= read -r -d '' path <&3 \
      || security_infra_failure "Security received an incomplete staged path record."
    metadata="${metadata#:}"
    read -r old_mode new_mode old_oid new_oid status extra <<< "$metadata"
    if [ -n "${extra:-}" ] \
      || [[ ! "$old_mode" =~ ^[0-7]{6}$ ]] \
      || [[ ! "$new_mode" =~ ^[0-7]{6}$ ]] \
      || [[ ! "$old_oid" =~ ^[0-9a-f]{40,64}$ ]] \
      || [[ ! "$new_oid" =~ ^[0-9a-f]{40,64}$ ]] \
      || [[ ! "$status" =~ ^[ACMTUXB]$ ]]; then
      security_infra_failure "Security received an ambiguous staged delta."
    fi
    scan_index_blob "$new_oid" "$new_mode" "$path"
  done 3< "$security_temp_dir/index-paths"
}

scan_commit_delta() {
  local base_commit="$1" commit="$2" failure_context="$3"
  local metadata="" path="" old_mode="" new_mode="" old_oid="" new_oid="" status="" extra=""

  if [ -n "$base_commit" ]; then
    git diff-tree --raw --no-abbrev --no-commit-id --no-renames --diff-filter=ACMRTUXB \
      -r -z "$base_commit" "$commit" -- > "$security_temp_dir/paths" \
      || security_infra_failure "Security could not enumerate $failure_context: $commit"
  else
    git diff-tree --root --raw --no-abbrev --no-commit-id --no-renames \
      --diff-filter=ACMRTUXB -r -z "$commit" -- > "$security_temp_dir/paths" \
      || security_infra_failure "Security could not enumerate $failure_context: $commit"
  fi

  while IFS= read -r -d '' metadata <&3; do
    IFS= read -r -d '' path <&3 \
      || security_infra_failure "Security received an incomplete committed path record: $commit"
    metadata="${metadata#:}"
    read -r old_mode new_mode old_oid new_oid status extra <<< "$metadata"
    if [ -n "${extra:-}" ] \
      || [[ ! "$old_mode" =~ ^[0-7]{6}$ ]] \
      || [[ ! "$new_mode" =~ ^[0-7]{6}$ ]] \
      || [[ ! "$old_oid" =~ ^[0-9a-f]{40,64}$ ]] \
      || [[ ! "$new_oid" =~ ^[0-9a-f]{40,64}$ ]] \
      || [[ ! "$status" =~ ^[ACMTUXB]$ ]]; then
      security_infra_failure "Security received an ambiguous committed delta: $commit"
    fi
    scan_commit_object_delta "$old_oid" "$new_oid" "$old_mode" "$new_mode" "$path"
  done 3< "$security_temp_dir/paths"
}

scan_commit_metadata() {
  local commit="$1" raw_hits=""

  if ! git cat-file commit "$commit" > "$security_temp_dir/commit-object"; then
    security_infra_failure "Security could not materialize commit metadata: $commit"
  fi
  raw_hits="$(grep -a -n -o -E "$CI_CHECKS_SECRET_PATTERN" -- "$security_temp_dir/commit-object" 2>/dev/null || true)"
  record_secret_hits "commit-metadata-$commit" "$raw_hits"
}

scan_commit_history() {
  local old_commit="$1" new_commit="$2" commit="" commit_line="" first_parent=""
  local -a commit_fields=()

  ensure_security_temp_dir

  # `old..new` is intentional even for a force update: it is exactly the set
  # of commits newly reachable from the destination tip. When there is no old
  # destination, every commit reachable from new is new and must be inspected.
  if [ -z "$old_commit" ]; then
    git rev-list --reverse --topo-order "$new_commit" -- > "$security_temp_dir/commits" \
      || security_infra_failure "Security could not enumerate the initial committed history."
  else
    git rev-list --reverse --topo-order "${old_commit}..${new_commit}" \
      -- > "$security_temp_dir/commits" \
      || security_infra_failure "Security could not enumerate the committed range."
  fi

  while IFS= read -r commit; do
    [ -n "$commit" ] || continue
    if ! commit_line="$(git rev-list --parents -n 1 "$commit" 2>/dev/null)"; then
      security_infra_failure "Security could not read commit parents: $commit"
    fi
    read -r -a commit_fields <<< "$commit_line"
    [ "${commit_fields[0]:-}" = "$commit" ] \
      || security_infra_failure "Security received an ambiguous commit record: $commit"
    for parent in "${commit_fields[@]:1}"; do
      git cat-file -e "${parent}^{commit}" 2>/dev/null \
        || security_infra_failure "Security committed history is incomplete at parent: $parent"
    done

    # Commit subjects, bodies, author, committer, and signature headers are
    # published object bytes too; scan each newly reachable commit exactly once.
    scan_commit_metadata "$commit"
    first_parent="${commit_fields[1]:-}"
    # First-parent delta is the merge contract: every newly reachable merged
    # commit is scanned separately above, while resolution-only bytes appear in
    # the merge result compared with its first parent.
    scan_commit_delta "$first_parent" "$commit" "commit delta"
  done < "$security_temp_dir/commits"

  if [ -n "$old_commit" ]; then
    # A force update can move backward to a commit already reachable from the
    # old tip, making old..new empty while resurrecting unsafe bytes. Scan the
    # exact destination state transition in addition to newly reachable history.
    scan_commit_delta "$old_commit" "$new_commit" "destination transition"
  fi
}

scan_local_paths() {
  ensure_security_temp_dir
  : > "$security_temp_dir/worktree-paths"
  : > "$security_temp_dir/index-paths"
  # NUL-delimited end to end: spaces, newlines, and a path literally named `-`
  # are file names, never syntax for sort/read/grep.
  if git rev-parse --verify HEAD >/dev/null 2>&1; then
    git diff --name-only --diff-filter=ACMRTUXB -z -- \
      > "$security_temp_dir/worktree-paths" \
      || security_infra_failure "Security could not enumerate worktree changes."
  fi
  git diff --cached --raw --no-abbrev --no-renames --diff-filter=ACMRTUXB -z -- \
    > "$security_temp_dir/index-paths" \
    || security_infra_failure "Security could not enumerate staged changes."

  if [ ! -s "$security_temp_dir/worktree-paths" ] \
    && [ ! -s "$security_temp_dir/index-paths" ]; then
    git ls-files -z -- > "$security_temp_dir/worktree-paths" \
      || security_infra_failure "Security could not enumerate tracked files."
  fi
  while IFS= read -r -d '' path; do
    scan_file "$path"
  done < "$security_temp_dir/worktree-paths"
  scan_index_deltas
}

# The hosted gate checks out committed bytes, so its worktree is clean. Falling
# back to every tracked file in that state makes historical findings look new
# and prevents the required context from ever reporting on the PR range. The
# push contract already carries the exact old/new commits; validate them and
# scan every newly reachable commit without inventing a debt baseline.
if [ -n "${CI_GATE_PUSH_OLD_SHA:-}${CI_GATE_PUSH_NEW_SHA:-}" ]; then
  old_sha="${CI_GATE_PUSH_OLD_SHA:-}"
  new_sha="${CI_GATE_PUSH_NEW_SHA:-HEAD}"
  if ! new_commit="$(git rev-parse --verify "${new_sha}^{commit}" 2>/dev/null)"; then
    security_infra_failure "Security committed-range head is missing or unreadable: $new_sha"
  fi
  if ! head_commit="$(git rev-parse --verify "HEAD^{commit}" 2>/dev/null)" \
    || [ "$new_commit" != "$head_commit" ]; then
    security_infra_failure "Security committed-range head must be the checked-out HEAD."
  fi
  if ! git diff --quiet -- || ! git diff --cached --quiet --; then
    security_infra_failure "Security committed-range scan requires a clean tracked worktree and index."
  fi

  if [ -z "$old_sha" ] || printf '%s' "$old_sha" | grep -Eq '^0+$'; then
    old_commit=""
  else
    if ! old_commit="$(git rev-parse --verify "${old_sha}^{commit}" 2>/dev/null)"; then
      security_infra_failure "Security committed-range base is missing or unreadable: $old_sha"
    fi
  fi
  scan_commit_history "$old_commit" "$new_commit"
else
  scan_local_paths
fi

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
