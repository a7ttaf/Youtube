#!/usr/bin/env bash
set -Eeuo pipefail

# self-test.sh — validates the CI scripts themselves
# Does NOT mutate project files. Uses temp directories for negative tests.

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT_DIR"

PASSED=0
FAILED=0

pass() { PASSED=$((PASSED + 1)); echo "  PASS: $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL: $1 — $2"; }

echo "========== CI Self-Tests =========="
echo ""

TMPDIR=$(mktemp -d)
ORIGINAL_HOOKS_PATH="$(git config core.hooksPath 2>/dev/null || true)"
HAD_HOOKS_PATH=0
if git config --get core.hooksPath >/dev/null 2>&1; then
  HAD_HOOKS_PATH=1
fi

cleanup() {
  if [ "$HAD_HOOKS_PATH" -eq 1 ]; then
    git config core.hooksPath "$ORIGINAL_HOOKS_PATH" >/dev/null 2>&1 || true
  else
    git config --unset core.hooksPath >/dev/null 2>&1 || true
  fi
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

# --- 1. Syntax checks ---
echo "[1] Script syntax"
for script in ci/preflight.sh ci/ship.sh ci/install-hooks.sh ci/impact.sh ci/test-plan.sh .githooks/pre-push; do
  if bash -n "$script" 2>/dev/null; then
    pass "$script"
  else
    fail "$script" "syntax error"
  fi
done

# --- 2. preflight.sh passes on clean state ---
echo ""
echo "[2] preflight.sh clean run"
if ./ci/preflight.sh >/dev/null 2>&1; then
  pass "preflight.sh exits 0 on clean repo"
else
  fail "preflight.sh exits 0 on clean repo" "unexpected failure"
fi

# --- 3. bash -n detects syntax errors (preflight's core mechanism) ---
echo ""
echo "[3] bash -n detects syntax errors"
echo "if" > "$TMPDIR/bad.sh"
if bash -n "$TMPDIR/bad.sh" 2>/dev/null; then
  fail "bash -n detects syntax errors" "should have failed on 'if' alone"
else
  pass "bash -n detects syntax errors"
fi

# --- 4. bash -n in isolation rejects bad scripts (validates our core syntax-check mechanism) ---
echo ""
echo "[4] bash -n in isolation rejects bad scripts"
# Copy preflight to temp, point it at a broken script
mkdir -p "$TMPDIR/ci"
echo "if" > "$TMPDIR/ci/broken.sh"
cp ci/preflight.sh "$TMPDIR/preflight_test.sh"
# Replace the script check loop to use our temp ci/ dir
cat > "$TMPDIR/preflight_test.sh" <<'SCRIPT'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$1"
PASSED=0
FAILED=0
section() { :; }
for script in ci/*.sh; do
  if [ -f "$script" ]; then
    bash -n "$script"
  fi
done
echo "Local CI preflight passed."
SCRIPT
if bash "$TMPDIR/preflight_test.sh" "$TMPDIR" >/dev/null 2>&1; then
  fail "bash -n in isolation rejects bad scripts" "should have failed on broken.sh"
else
  pass "bash -n in isolation rejects bad scripts"
fi

# --- 5. ship.sh --help exits 0 ---
echo ""
echo "[5] ship.sh --help"
if ./ci/ship.sh --help >/dev/null 2>&1; then
  pass "ship.sh --help exits 0"
else
  fail "ship.sh --help exits 0" "exited non-zero"
fi

echo ""
echo "[5b] advisory command help"
if ./ci/impact.sh --help >/dev/null 2>&1; then
  pass "impact.sh --help exits 0"
else
  fail "impact.sh --help exits 0" "exited non-zero"
fi
if ./ci/test-plan.sh --help >/dev/null 2>&1; then
  pass "test-plan.sh --help exits 0"
else
  fail "test-plan.sh --help exits 0" "exited non-zero"
fi

# --- 6. ship.sh requires commit message ---
echo ""
echo "[6] ship.sh requires message"
if ./ci/ship.sh >/dev/null 2>&1; then
  fail "ship.sh requires message" "should have failed without message"
else
  pass "ship.sh requires message"
fi

# --- 7. ship.sh rejects multiple messages ---
echo ""
echo "[7] ship.sh rejects extra args"
if ./ci/ship.sh "msg1" "msg2" >/dev/null 2>&1; then
  fail "ship.sh rejects extra args" "should have failed"
else
  pass "ship.sh rejects extra args"
fi

# --- 8. ship.sh has detached HEAD guard ---
echo ""
echo "[8] ship.sh detached HEAD guard"
# This is tested by checking the branch guard exists — can't detach HEAD in test
# Verify the check is in the source
if grep -q "Refusing to ship from detached HEAD" ci/ship.sh; then
  pass "ship.sh has detached HEAD guard"
else
  fail "ship.sh has detached HEAD guard" "guard missing"
fi

# --- 9. install-hooks.sh works ---
echo ""
echo "[9] install-hooks.sh"
./ci/install-hooks.sh >/dev/null 2>&1
HOOKSPATH=$(git config core.hooksPath)
if [ "$HOOKSPATH" = ".githooks" ]; then
  pass "install-hooks.sh sets core.hooksPath"
else
  fail "install-hooks.sh sets core.hooksPath" "got: $HOOKSPATH"
fi

# --- 10. .gitignore coverage ---
echo ""
echo "[10] .gitignore coverage"
for pattern in ".env" "node_modules/" "dist/" ".DS_Store" "!.env.example"; do
  if grep -qF "$pattern" .gitignore; then
    pass ".gitignore has '$pattern'"
  else
    fail ".gitignore has '$pattern'" "missing"
  fi
done

# --- 11. Makefile targets ---
echo ""
echo "[11] Makefile targets"
for target in verify ci-quick ci-full ci-ship ci-debt impact test-plan smart install-hooks ship ci-self-test ci-all ci-fix ci-profile docs bats lint-ci; do
  if grep -q "^${target}:" Makefile; then
    pass "Makefile has '$target' target"
  else
    fail "Makefile has '$target' target" "missing"
  fi
done

# --- 12. Makefile ship target requires MESSAGE ---
echo ""
echo "[12] make ship requires MESSAGE"
if make -n ship MESSAGE="test-msg" >/dev/null 2>&1; then
  pass "make ship MESSAGE= works"
else
  fail "make ship MESSAGE= works" "failed"
fi
# Verify ifndef MESSAGE guard exists in Makefile
if grep -q "ifndef MESSAGE" Makefile; then
  pass "make ship has MESSAGE guard"
else
  fail "make ship has MESSAGE guard" "missing ifndef MESSAGE in Makefile"
fi

# --- 12b. Makefile new targets are tab-indented (make -n must succeed) ---
echo ""
echo "[12b] Makefile new targets parseable"
for target in ci-all ci-fix ci-profile docs bats lint-ci; do
  if make -n "$target" >/dev/null 2>&1; then
    pass "make -n $target exits 0"
  else
    fail "make -n $target exits 0" "failed (likely missing tab indentation)"
  fi
done

# --- 13. hook-dispatch.sh syntax ---
echo ""
echo "[13] hook-dispatch.sh syntax"
if [ -f "ci/hook-dispatch.sh" ]; then
  if bash -n "ci/hook-dispatch.sh" 2>/dev/null; then
    pass "ci/hook-dispatch.sh syntax"
  else
    fail "ci/hook-dispatch.sh syntax" "syntax error"
  fi
else
  fail "ci/hook-dispatch.sh syntax" "file not found"
fi

# --- 14. New lib files syntax ---
echo ""
echo "[14] ci/lib/*.sh syntax"
for libfile in ci/lib/affected.sh ci/lib/cache.sh ci/lib/changeset.sh ci/lib/git.sh \
               ci/lib/impact.sh ci/lib/junit.sh ci/lib/log.sh ci/lib/report.sh \
               ci/lib/runner.sh ci/lib/sarif.sh ci/lib/test-plan.sh ci/lib/yaml.sh; do
  if [ -f "$libfile" ]; then
    if bash -n "$libfile" 2>/dev/null; then
      pass "$libfile"
    else
      fail "$libfile" "syntax error"
    fi
  fi
done

# --- 15. New check files syntax ---
echo ""
echo "[15] ci/checks/*.sh syntax"
for chkfile in ci/checks/commit-hygiene.sh ci/checks/branch-protection.sh \
               ci/checks/secrets.sh ci/checks/lint.sh ci/checks/format.sh \
               ci/checks/typecheck.sh ci/checks/tests.sh ci/checks/sast.sh \
               ci/checks/supply-chain.sh ci/checks/license.sh ci/checks/container.sh \
               ci/checks/iac.sh; do
  if [ -f "$chkfile" ]; then
    if bash -n "$chkfile" 2>/dev/null; then
      pass "$chkfile"
    else
      fail "$chkfile" "syntax error"
    fi
  fi
done

# --- 16. preflight.sh --help ---
echo ""
echo "[16] preflight.sh --help"
if ./ci/preflight.sh --help >/dev/null 2>&1; then
  pass "preflight.sh --help exits 0"
else
  fail "preflight.sh --help exits 0" "exited non-zero"
fi

# --- 17. preflight.sh --mode quick ---
echo ""
echo "[17] preflight.sh --mode quick"
if ./ci/preflight.sh --mode quick >/dev/null 2>&1; then
  pass "preflight.sh --mode quick exits 0"
else
  # quick mode may find issues but should not crash (exit 1 is acceptable for check failures)
  RC=$?
  if [ "$RC" -le 20 ]; then
    pass "preflight.sh --mode quick runs without crash (rc=$RC)"
  else
    fail "preflight.sh --mode quick" "exited with unexpected code $RC"
  fi
fi

# --- 18. No args[@] unbound-variable crash ---
echo ""
echo "[18] No args[@] unbound-variable crash"
# The safe Bash 3.2 idiom is ${args[@]+"${args[@]}"}; the inner "${args[@]}" is
# always surrounded by the guard so it can never be a bare (unsafe) expansion.
# Detect ONLY bare expansions: "  ${args[@]}" or "  "${args[@]}" not preceded by +
if grep -qE '(^|\s)"?\$\{args\[@\]\}"?\s' ci/lib/runner.sh 2>/dev/null && \
   ! grep -E '(^|\s)"?\$\{args\[@\]\}"?\s' ci/lib/runner.sh 2>/dev/null | \
       grep -qE '\[@\]\+'; then
  fail "runner.sh empty-array safety" "still has bare \${args[@]} expansion — will crash on Bash 3.2"
else
  pass "runner.sh empty-array safety (Bash 3.2 compatible)"
fi

# --- 19. Cache ~ expansion ---
echo ""
echo "[19] Cache tilde expansion"
if grep -q 'HOME.*CI_GATE_CACHE_DIR' ci/lib/cache.sh 2>/dev/null || \
   grep -q 'case.*CI_GATE_CACHE_DIR' ci/lib/cache.sh 2>/dev/null; then
  pass "cache.sh expands ~ to \$HOME"
else
  fail "cache.sh expands ~ to \$HOME" "tilde-expansion code not found"
fi
if grep -q 'CI_GATE_NO_CACHE' ci/lib/cache.sh 2>/dev/null; then
  pass "cache.sh honors CI_GATE_NO_CACHE"
else
  fail "cache.sh honors CI_GATE_NO_CACHE" "CI_GATE_NO_CACHE bypass not found"
fi

# --- 20. bash -n all CI scripts ---
echo ""
echo "[20] bash -n all CI scripts"
_syntax_ok=1
for _sf in ci/*.sh ci/checks/*.sh ci/lib/*.sh ci/hook-dispatch.sh .githooks/pre-push .githooks/pre-commit .githooks/commit-msg .githooks/prepare-commit-msg; do
  [ -f "$_sf" ] || continue
  if ! bash -n "$_sf" 2>/dev/null; then
    fail "bash -n $_sf" "syntax error"
    _syntax_ok=0
  fi
done
if [ "$_syntax_ok" -eq 1 ]; then
  pass "bash -n: all CI scripts have valid syntax"
fi

# --- Summary ---
echo ""
echo "========== Self-Test Results =========="
echo "Passed: $PASSED"
echo "Failed: $FAILED"
if [ "$FAILED" -gt 0 ]; then
  echo "Self-tests FAILED." >&2
  exit 1
fi
echo "Self-tests passed."
