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

security_data_has_secret() {
  local data="$1" grep_rc=0

  set +e
  printf '%s' "$data" | grep -a -q -E "$CI_CHECKS_SECRET_PATTERN"
  grep_rc=$?
  set -e
  case "$grep_rc" in
    0) return 0 ;;
    1) return 1 ;;
    *) security_infra_failure "Security could not scan raw metadata bytes." ;;
  esac
}

security_path_for_log() {
  if security_data_has_secret "$1"; then
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
  if security_data_has_secret "$path"; then
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
    raw_hits="$(scan_searchable_file "$path" "$security_temp_dir/blob" worktree)"
  elif [ -f "$filesystem_path" ]; then
    raw_hits="$(scan_searchable_file "$path" "$filesystem_path" worktree)"
  else
    return 0
  fi
  record_secret_hits "$path" "$raw_hits"
}

security_infra_failure() {
  echo "$1" >&2
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

prepare_searchable_projections() {
  local input_file="$1" output_prefix="$2" label="$3" bom_hex=""

  ci::common::command_exists od \
    || security_infra_failure "Security requires od to inspect encoded content."
  bom_hex="$(LC_ALL=C od -An -tx1 -N2 "$input_file" 2>/dev/null | tr -d '[:space:]')" \
    || security_infra_failure "Security could not inspect encoded content: $(security_path_for_log "$label")"
  case "$bom_hex" in
    fffe)
      ci::common::command_exists iconv \
        || security_infra_failure "Security requires iconv for UTF-16LE content."
      iconv -f UTF-16LE -t UTF-8 "$input_file" > "${output_prefix}-primary" 2>/dev/null \
        || security_infra_failure "Security could not decode UTF-16LE content: $(security_path_for_log "$label")"
      ;;
    feff)
      ci::common::command_exists iconv \
        || security_infra_failure "Security requires iconv for UTF-16BE content."
      iconv -f UTF-16BE -t UTF-8 "$input_file" > "${output_prefix}-primary" 2>/dev/null \
        || security_infra_failure "Security could not decode UTF-16BE content: $(security_path_for_log "$label")"
      ;;
    *)
      cp -- "$input_file" "${output_prefix}-primary" \
        || security_infra_failure "Security could not prepare content: $(security_path_for_log "$label")"
      ;;
  esac

  # BOM-less UTF-16 has no reliable declaration. A second projection removes
  # interleaved NUL bytes, exposing ASCII credential patterns in either endian
  # order without guessing that arbitrary binary data is valid Unicode text.
  LC_ALL=C tr -d '\000' < "$input_file" > "${output_prefix}-nul-stripped" \
    || security_infra_failure "Security could not normalize NUL content: $(security_path_for_log "$label")"
}

append_secret_hits() {
  local input_file="$1" output_file="$2" label="$3" grep_rc=0

  set +e
  grep -a -n -o -E "$CI_CHECKS_SECRET_PATTERN" -- "$input_file" >> "$output_file" 2>/dev/null
  grep_rc=$?
  set -e
  [ "$grep_rc" -le 1 ] \
    || security_infra_failure "Security could not scan content: $(security_path_for_log "$label")"
}

projections_differ() {
  local first="$1" second="$2" cmp_rc=0

  ci::common::command_exists cmp \
    || security_infra_failure "Security requires cmp for normalized content."
  set +e
  cmp -s -- "$first" "$second"
  cmp_rc=$?
  set -e
  case "$cmp_rc" in
    0) return 1 ;;
    1) return 0 ;;
    *) security_infra_failure "Security could not compare normalized content." ;;
  esac
}

scan_searchable_file() {
  local label="$1" input_file="$2" slot="$3"
  local projection_prefix="$security_temp_dir/searchable-$slot"

  prepare_searchable_projections "$input_file" "$projection_prefix" "$label"
  : > "$security_temp_dir/hits-$slot"
  append_secret_hits "${projection_prefix}-primary" "$security_temp_dir/hits-$slot" "$label"
  if projections_differ "${projection_prefix}-primary" "${projection_prefix}-nul-stripped"; then
    append_secret_hits "${projection_prefix}-nul-stripped" "$security_temp_dir/hits-$slot" "$label"
  fi
  cat -- "$security_temp_dir/hits-$slot"
}

append_projection_additions() {
  local path="$1" old_projection="$2" new_projection="$3" slot="$4" diff_rc=0

  set +e
  git diff --no-index --text --no-ext-diff --no-textconv --unified=0 \
    --output-indicator-new='>' -- "$old_projection" "$new_projection" \
    > "$security_temp_dir/blob-diff-$slot" 2>/dev/null
  diff_rc=$?
  set -e
  [ "$diff_rc" -le 1 ] \
    || security_infra_failure "Security could not compare committed blob content: $(security_path_for_log "$path")"

  # Git's dedicated new-line indicator avoids confusing diff headers with
  # content. Removing exactly one marker preserves a content line beginning >.
  sed -n 's/^>//p' "$security_temp_dir/blob-diff-$slot" > "$security_temp_dir/added-lines-$slot" \
    || security_infra_failure "Security could not extract committed additions: $(security_path_for_log "$path")"
  append_secret_hits "$security_temp_dir/added-lines-$slot" "$security_temp_dir/blob-addition-hits" "$path"
}

scan_blob_additions() {
  local path="$1" old_blob="$2" new_blob="$3" raw_hits=""
  local old_prefix="$security_temp_dir/delta-old" new_prefix="$security_temp_dir/delta-new"

  prepare_searchable_projections "$old_blob" "$old_prefix" "$path"
  prepare_searchable_projections "$new_blob" "$new_prefix" "$path"
  : > "$security_temp_dir/blob-addition-hits"
  append_projection_additions "$path" "${old_prefix}-primary" "${new_prefix}-primary" primary
  if projections_differ "${old_prefix}-primary" "${old_prefix}-nul-stripped" \
    || projections_differ "${new_prefix}-primary" "${new_prefix}-nul-stripped"; then
    append_projection_additions "$path" \
      "${old_prefix}-nul-stripped" "${new_prefix}-nul-stripped" nul-stripped
  fi
  raw_hits="$(cat -- "$security_temp_dir/blob-addition-hits")"
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
  # `git diff-tree -t` includes tree entries so an empty directory name is not
  # invisible merely because it has no recursive leaf. Its path was scanned
  # above; only blobs have payload bytes to materialize.
  [ "$new_type" = "tree" ] && return 0
  [ "$new_type" = "blob" ] \
    || security_infra_failure "Security expected a committed blob for: $(security_path_for_log "$path")"
  if ! git cat-file blob "$new_oid" > "$security_temp_dir/blob-new"; then
    security_infra_failure "Security could not materialize committed blob: $new_oid ($(security_path_for_log "$path"))"
  fi

  if [ "$old_mode" = "160000" ] || printf '%s' "$old_oid" | grep -Eq '^0+$'; then
    raw_hits="$(scan_searchable_file "$path" "$security_temp_dir/blob-new" committed-new)"
    record_secret_hits "$path" "$raw_hits"
    return 0
  fi
  if ! old_type="$(git cat-file -t "$old_oid" 2>/dev/null)"; then
    security_infra_failure "Security could not read prior committed object: $old_oid ($(security_path_for_log "$path"))"
  fi
  if [ "$old_type" != "blob" ]; then
    raw_hits="$(scan_searchable_file "$path" "$security_temp_dir/blob-new" committed-new)"
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
  raw_hits="$(scan_searchable_file "$path" "$security_temp_dir/blob" staged)"
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
    git diff-tree -t --raw --no-abbrev --no-commit-id --no-renames --diff-filter=ACMRTUXB \
      -r -z "$base_commit" "$commit" -- > "$security_temp_dir/paths" \
      || security_infra_failure "Security could not enumerate $failure_context: $commit"
  else
    git diff-tree --root -t --raw --no-abbrev --no-commit-id --no-renames \
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

scan_commit_tree_state() {
  local commit="$1" failure_context="$2" entry="" metadata="" path=""
  local mode="" object_type="" oid="" extra="" raw_hits=""

  git ls-tree -r -t -z --full-tree "$commit" -- > "$security_temp_dir/tree-state" \
    || security_infra_failure "Security could not enumerate $failure_context: $commit"
  while IFS= read -r -d '' entry; do
    metadata="${entry%%$'\t'*}"
    path="${entry#*$'\t'}"
    [ "$metadata" != "$entry" ] \
      || security_infra_failure "Security received an incomplete tree entry: $commit"
    read -r mode object_type oid extra <<< "$metadata"
    if [ -n "${extra:-}" ] \
      || [[ ! "$mode" =~ ^[0-7]{6}$ ]] \
      || [[ ! "$oid" =~ ^[0-9a-f]{40,64}$ ]] \
      || [[ ! "$object_type" =~ ^(blob|tree|commit)$ ]]; then
      security_infra_failure "Security received an ambiguous tree entry: $commit"
    fi
    scan_sensitive_path "$path"
    [ "$object_type" = "blob" ] || continue
    git cat-file blob "$oid" > "$security_temp_dir/tree-state-blob" \
      || security_infra_failure "Security could not read tree-state blob: $(security_path_for_log "$path")"
    raw_hits="$(scan_searchable_file "$path" "$security_temp_dir/tree-state-blob" tree-state)"
    record_secret_hits "$path" "$raw_hits"
  done < "$security_temp_dir/tree-state"
}

scan_metadata_file() {
  local label="$1" input_file="$2" slot="$3" separator_rc=0
  local header_hits="" message_hits=""

  set +e
  grep -a -q -x '' -- "$input_file"
  separator_rc=$?
  set -e
  [ "$separator_rc" -eq 0 ] \
    || security_infra_failure "Security received malformed object metadata: $label"
  sed -n '1,/^$/p' "$input_file" > "$security_temp_dir/metadata-headers-$slot" \
    || security_infra_failure "Security could not extract object headers: $label"
  sed '1,/^$/d' "$input_file" > "$security_temp_dir/metadata-message-$slot" \
    || security_infra_failure "Security could not extract object message: $label"
  header_hits="$(scan_searchable_file "$label" "$security_temp_dir/metadata-headers-$slot" "metadata-headers-$slot")"
  message_hits="$(scan_searchable_file "$label" "$security_temp_dir/metadata-message-$slot" "metadata-message-$slot")"
  printf '%s%s%s' "$header_hits" "${header_hits:+${message_hits:+$'\n'}}" "$message_hits"
}

scan_commit_metadata() {
  local commit="$1" raw_hits=""

  if ! git cat-file commit "$commit" > "$security_temp_dir/commit-object"; then
    security_infra_failure "Security could not materialize commit metadata: $commit"
  fi
  raw_hits="$(scan_metadata_file "commit-metadata-$commit" "$security_temp_dir/commit-object" commit)"
  record_secret_hits "commit-metadata-$commit" "$raw_hits"
}

scan_commit_list_file() {
  local commits_file="$1" commit="" commit_line="" first_parent=""
  local -a commit_fields=()

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
  done < "$commits_file"
}

scan_commit_history() {
  local old_commit="$1" new_commit="$2"

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
  scan_commit_list_file "$security_temp_dir/commits"

  if [ -n "$old_commit" ]; then
    # A force update can move backward to a commit already reachable from the
    # old tip, making old..new empty while resurrecting unsafe bytes. Scan the
    # exact destination state transition in addition to newly reachable history.
    scan_commit_delta "$old_commit" "$new_commit" "destination transition"
  fi
}

scanned_tip_commit=""

scan_tag_chain() {
  local object_oid="$1" object_type="" tag_line="" type_line="" target_oid=""
  local declared_type="" actual_type="" raw_hits="" seen=""

  while :; do
    if printf '%s\n' "$seen" | grep -F -x -q -- "$object_oid"; then
      security_infra_failure "Security found a cycle in the outgoing tag chain: $object_oid"
    fi
    seen="${seen}${seen:+$'\n'}${object_oid}"
    object_type="$(git cat-file -t "$object_oid" 2>/dev/null)" \
      || security_infra_failure "Security could not read outgoing object: $object_oid"
    case "$object_type" in
      commit)
        scanned_tip_commit="$object_oid"
        return 0
        ;;
      tag)
        git cat-file tag "$object_oid" > "$security_temp_dir/tag-object" \
          || security_infra_failure "Security could not materialize outgoing tag: $object_oid"
        raw_hits="$(scan_metadata_file "tag-metadata-$object_oid" "$security_temp_dir/tag-object" tag)"
        record_secret_hits "tag-metadata-$object_oid" "$raw_hits"
        IFS= read -r tag_line < "$security_temp_dir/tag-object" \
          || security_infra_failure "Security could not read outgoing tag target: $object_oid"
        IFS= read -r type_line < <(sed -n '2p' "$security_temp_dir/tag-object") \
          || security_infra_failure "Security could not read outgoing tag type: $object_oid"
        target_oid="${tag_line#object }"
        declared_type="${type_line#type }"
        if [ "$tag_line" = "$target_oid" ] \
          || [[ ! "$target_oid" =~ ^[0-9a-f]{40,64}$ ]] \
          || [ "$type_line" = "$declared_type" ]; then
          security_infra_failure "Security received ambiguous outgoing tag metadata: $object_oid"
        fi
        actual_type="$(git cat-file -t "$target_oid" 2>/dev/null)" \
          || security_infra_failure "Security could not read outgoing tag target: $target_oid"
        [ "$actual_type" = "$declared_type" ] \
          || security_infra_failure "Security outgoing tag type does not match its target: $object_oid"
        case "$actual_type" in
          commit|tag) object_oid="$target_oid" ;;
          *) security_infra_failure "Security only supports tags resolving to commits: $object_oid" ;;
        esac
        ;;
      *) security_infra_failure "Security outgoing ref does not resolve to a commit: $object_oid" ;;
    esac
  done
}

scan_outgoing_tip_history() {
  local tip_commit="$1" remote_tip="" remote_commit=""
  local -a rev_args=("$tip_commit")

  # Destination tips are exclusions, not a guessed single base. If they are
  # absent or unreadable, omit the exclusion and safely scan more history.
  if [ -n "${CI_GATE_PUSH_REMOTE_TIPS_FOR:-}" ] \
    && [ "${CI_GATE_PUSH_REMOTE_TIPS_FOR}" = "${CI_GATE_PUSH_REMOTE:-}" ]; then
    # Word splitting is the hook's contract: these are hexadecimal object IDs.
    # shellcheck disable=SC2086
    for remote_tip in ${CI_GATE_PUSH_REMOTE_TIPS:-}; do
      remote_commit="$(git rev-parse --verify "${remote_tip}^{commit}" 2>/dev/null || true)"
      [ -n "$remote_commit" ] && rev_args+=("^$remote_commit")
    done
  fi
  git rev-list --reverse --topo-order "${rev_args[@]}" -- \
    > "$security_temp_dir/outgoing-commits" \
    || security_infra_failure "Security could not enumerate outgoing ref history: $tip_commit"
  scan_commit_list_file "$security_temp_dir/outgoing-commits"
}

scan_outgoing_ref_tips() {
  local tip="" object_oid=""

  ensure_security_temp_dir
  # Word splitting is safe for hook-exported object IDs and rejects everything
  # that does not resolve to one exact local object below.
  # shellcheck disable=SC2086
  for tip in ${CI_GATE_PUSH_TAG_TIPS:-} ${CI_GATE_PUSH_OTHER_TIPS:-}; do
    object_oid="$(git rev-parse --verify "$tip" 2>/dev/null)" \
      || security_infra_failure "Security outgoing ref tip is missing or unreadable: $tip"
    [[ "$object_oid" =~ ^[0-9a-f]{40,64}$ ]] \
      || security_infra_failure "Security outgoing ref tip is ambiguous: $tip"
    scan_tag_chain "$object_oid"
    scan_outgoing_tip_history "$scanned_tip_commit"
  done

  # Notes are mutable annotations, not immutable labels. Moving a notes ref
  # backward changes the note content users see even when the old commit object
  # remains reachable from the destination's current notes history. Scan the
  # complete target state as well as newly published history so rollback cannot
  # resurrect a credential-bearing note blob.
  # shellcheck disable=SC2086
  for tip in ${CI_GATE_PUSH_NOTES_TIPS:-}; do
    object_oid="$(git rev-parse --verify "$tip" 2>/dev/null)" \
      || security_infra_failure "Security outgoing notes tip is missing or unreadable: $tip"
    [[ "$object_oid" =~ ^[0-9a-f]{40,64}$ ]] \
      || security_infra_failure "Security outgoing notes tip is ambiguous: $tip"
    scan_tag_chain "$object_oid"
    scan_commit_metadata "$scanned_tip_commit"
    scan_commit_tree_state "$scanned_tip_commit" "outgoing notes state"
    scan_outgoing_tip_history "$scanned_tip_commit"
  done
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
push_context_seen=0
if [ -n "${CI_GATE_PUSH_OLD_SHA:-}${CI_GATE_PUSH_NEW_SHA:-}" ]; then
  push_context_seen=1
  old_sha="${CI_GATE_PUSH_OLD_SHA:-}"
  new_sha="${CI_GATE_PUSH_NEW_SHA:-HEAD}"
  ensure_security_temp_dir
  if ! new_object="$(git rev-parse --verify "$new_sha" 2>/dev/null)"; then
    security_infra_failure "Security committed-range head is missing or unreadable: $new_sha"
  fi
  [[ "$new_object" =~ ^[0-9a-f]{40,64}$ ]] \
    || security_infra_failure "Security committed-range head is ambiguous: $new_sha"
  scan_tag_chain "$new_object"
  new_commit="$scanned_tip_commit"
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
    [[ "$old_commit" =~ ^[0-9a-f]{40,64}$ ]] \
      || security_infra_failure "Security committed-range base is ambiguous: $old_sha"
  fi
  scan_commit_history "$old_commit" "$new_commit"
fi

if [ -n "${CI_GATE_PUSH_TAG_TIPS:-}${CI_GATE_PUSH_OTHER_TIPS:-}${CI_GATE_PUSH_NOTES_TIPS:-}" ]; then
  push_context_seen=1
  scan_outgoing_ref_tips
fi

if [ -n "${CI_GATE_PUSH_BRANCH_TIPS:-}" ] \
  && [ -z "${CI_GATE_PUSH_NEW_SHA:-}" ]; then
  security_infra_failure "Security received branch tips without an authoritative branch range."
fi

outgoing_ref_names="${CI_GATE_PUSH_OUTGOING_REFS-${CI_GATE_PUSH_REMOTE_REFS:-}}"
if [ -n "$outgoing_ref_names" ]; then
  push_context_seen=1
  if security_data_has_secret "$outgoing_ref_names"; then
    echo "Potential secret-like content in outgoing ref name: [redacted-secret-like-ref]"
    matches=1
  fi
fi

# hook-dispatch exports its ref lists as empty when git supplies no push
# records. That is not an authoritative empty content set: it can also mean a
# hook runner failed to forward stdin, so the hook deliberately gates the
# checked-out tree. Only its explicit application-content-free marker may
# suppress that fallback; notes still supply NOTES_TIPS and are scanned above.
if [ "${CI_GATE_PUSH_DELETIONS_ONLY:-0}" = "1" ]; then
  push_context_seen=1
fi

if [ -n "$outgoing_ref_names" ] \
  && [ -z "${CI_GATE_PUSH_NEW_SHA:-}${CI_GATE_PUSH_BRANCH_TIPS:-}${CI_GATE_PUSH_TAG_TIPS:-}${CI_GATE_PUSH_OTHER_TIPS:-}${CI_GATE_PUSH_NOTES_TIPS:-}" ] \
  && [ "${CI_GATE_PUSH_DELETIONS_ONLY:-0}" != "1" ]; then
  security_infra_failure "Security received outgoing ref names without their object tips."
fi

if [ "$push_context_seen" -eq 0 ]; then
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
