#!/usr/bin/env bats
#
# The layout guard is the only thing standing between a misplaced frontend test
# and a green build that never ran it, so these tests assert the two ways it can
# fail open: a test file the scan does not reach, and an include the drift check
# accepts without vitest ever seeing it.

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  cd "$REPO_ROOT"

  # Sandbox with the real script and libs, but a synthetic frontend/ tree, so a
  # case can be built without touching the repository's own layout.
  SANDBOX="$(mktemp -d)"
  mkdir -p "$SANDBOX/ci/checks" "$SANDBOX/ci/lib"
  cp "$REPO_ROOT/ci/checks/test-layout.sh" "$SANDBOX/ci/checks/"
  cp "$REPO_ROOT/ci/lib/common.sh" "$REPO_ROOT/ci/lib/log.sh" "$SANDBOX/ci/lib/"

  mkdir -p "$SANDBOX/frontend/src" "$SANDBOX/frontend/tests"
  printf 'export const ok = true;\n' > "$SANDBOX/frontend/src/app.ts"
  printf 'it("runs", () => {});\n' > "$SANDBOX/frontend/tests/app.test.ts"
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
}

teardown() {
  [ -n "${SANDBOX:-}" ] && rm -rf "$SANDBOX"
}

run_guard() {
  run bash "$SANDBOX/ci/checks/test-layout.sh"
}

# extract_shell_fn <file> <name> - print the named shell function's definition.
#
# A sed range anchored on "^name()" and "^}" assumes both sit at column zero, so
# re-indenting a file - a change with no effect on behaviour - broke these cases
# rather than the code they guard. The header is matched at any indentation and
# the body closes on the first "}" at the same column, which is what the shell
# formatter this repo runs produces. Callers that source the result run bash -n
# on it first, so a mis-extraction fails loudly instead of quietly defining
# nothing.
extract_shell_fn() {
  awk -v name="$2" '
    !inside {
      line = $0
      sub(/[[:space:]]+$/, "", line)
      probe = line
      sub(/^[[:space:]]+/, "", probe)
      # Compared as text, not as a pattern: a function name is a literal here,
      # and building a regex out of one is how the parentheses would have to be
      # escaped in the first place.
      if (probe == name "()" || probe == name "() {") {
        indent = line
        sub(/[^[:space:]].*$/, "", indent)
        inside = 1
        print
      }
      next
    }
    { print }
    $0 == indent "}" { inside = 0 }
  ' "$1"
}

@test "test-layout: syntax check passes" {
  bash -n ci/checks/test-layout.sh
}

@test "test-layout: passes on a conforming tree" {
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: passes on this repository as committed" {
  run bash ci/checks/test-layout.sh
  [ "$status" -eq 0 ]
}

# --- outside-tree scan --------------------------------------------------------

@test "test-layout: catches a test under frontend/src" {
  printf 'it("skipped", () => {});\n' > "$SANDBOX/frontend/src/app.test.ts"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"frontend/src/app.test.ts"* ]]
}

@test "test-layout: catches a test in a frontend subdirectory that is neither src nor tests" {
  mkdir -p "$SANDBOX/frontend/e2e"
  printf 'it("skipped", () => {});\n' > "$SANDBOX/frontend/e2e/checkout.test.ts"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"frontend/e2e/checkout.test.ts"* ]]
}

@test "test-layout: catches a test at the frontend root" {
  printf 'it("skipped", () => {});\n' > "$SANDBOX/frontend/smoke.test.tsx"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"frontend/smoke.test.tsx"* ]]
}

@test "test-layout: ignores vendored and built output" {
  mkdir -p "$SANDBOX/frontend/node_modules/pkg" "$SANDBOX/frontend/dist"
  printf 'it("vendored", () => {});\n' > "$SANDBOX/frontend/node_modules/pkg/index.test.js"
  printf 'it("built", () => {});\n' > "$SANDBOX/frontend/dist/bundle.test.js"
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: ignores a nested node_modules" {
  # Nested copies are real and never first-party, so this prune stays by name.
  mkdir -p "$SANDBOX/frontend/src/feature/node_modules/pkg"
  printf 'it("vendored", () => {});\n' > "$SANDBOX/frontend/src/feature/node_modules/pkg/index.test.js"
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: catches a test under a first-party directory named build" {
  # An unanchored -name 'build' prune would hide this: neither collected by
  # vitest nor reported by the guard.
  mkdir -p "$SANDBOX/frontend/e2e/build"
  printf 'it("skipped", () => {});\n' > "$SANDBOX/frontend/e2e/build/checkout.test.ts"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"frontend/e2e/build/checkout.test.ts"* ]]
}

@test "test-layout: catches tests under every build-output name when nested" {
  local dir
  for dir in build dist coverage .next .turbo .vite; do
    rm -rf "$SANDBOX/frontend/feature"
    mkdir -p "$SANDBOX/frontend/feature/$dir"
    printf 'it("skipped", () => {});\n' > "$SANDBOX/frontend/feature/$dir/x.test.ts"
    run_guard
    [ "$status" -eq 20 ] || { echo "nested $dir/ evaded the guard" >&2; return 1; }
  done
  rm -rf "$SANDBOX/frontend/feature"
}

@test "test-layout: still ignores build output at the workspace root" {
  local dir
  for dir in build dist coverage .next .turbo .vite; do
    mkdir -p "$SANDBOX/frontend/$dir"
    printf 'it("built", () => {});\n' > "$SANDBOX/frontend/$dir/x.test.ts"
  done
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: catches a retired __tests__ directory outside src" {
  mkdir -p "$SANDBOX/frontend/features/__tests__"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"frontend/features/__tests__"* ]]
}

@test "test-layout: catches a .spec file inside tests/ that the include never collects" {
  printf 'it("skipped", () => {});\n' > "$SANDBOX/frontend/tests/app.spec.ts"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"frontend/tests/app.spec.ts"* ]]
}

@test "test-layout: catches a module-suffixed test inside tests/" {
  # vitest's default glob collects .mts; this config's include does not, so the
  # file reads as a real test to every tool except the one that would run it.
  printf 'it("skipped", () => {});\n' > "$SANDBOX/frontend/tests/missed.test.mts"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"frontend/tests/missed.test.mts"* ]]
}

@test "test-layout: catches every module suffix the default glob would collect" {
  local ext
  for ext in mts cts mjs cjs; do
    rm -f "$SANDBOX/frontend/tests/missed.test."*
    printf 'it("skipped", () => {});\n' > "$SANDBOX/frontend/tests/missed.test.$ext"
    run_guard
    [ "$status" -eq 20 ] || { echo "suffix .$ext passed the guard" >&2; return 1; }
  done
}

@test "test-layout: catches a module-suffixed test outside tests/" {
  mkdir -p "$SANDBOX/frontend/e2e"
  printf 'it("skipped", () => {});\n' > "$SANDBOX/frontend/e2e/smoke.test.mjs"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"frontend/e2e/smoke.test.mjs"* ]]
}

@test "test-layout: the two collected suffixes are not reported as unrunnable" {
  printf 'it("runs", () => {});\n' > "$SANDBOX/frontend/tests/nested.test.tsx"
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: exempt from incremental changeset filtering" {
  # The changeset emits language-derived check ids and never emits test-layout,
  # so without an always-run exemption the scheduled lane is discarded before it
  # runs whenever the diff does not look like JavaScript.
  run bash -c "sed -n '/Always-run checks are never skipped/,/esac/p' ci/preflight.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"test-layout"* ]]
}

# --- drift guard --------------------------------------------------------------

@test "test-layout: catches a dropped include" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {},
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: catches an include commented out with //" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: catches an include commented out with a block comment" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    /*
    include: ["tests/**/*.test.{ts,tsx}"],
    */
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: catches the glob surviving only in prose" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

// Tests used to live at tests/**/*.test.{ts,tsx}; now they do not.
export default defineConfig({
  test: {},
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: accepts an active include carrying a trailing comment" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"], // keep in lockstep with test-layout.sh
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: rejects the glob living in a non-test include" {
  # optimizeDeps.include carrying the glob must not satisfy the guard while
  # test.include has been narrowed to a single file — vitest would run one test.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  optimizeDeps: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
  test: {
    include: ["tests/only-this-one.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"active test.include"* ]]
}

@test "test-layout: accepts the glob in test.include alongside other include fields" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  optimizeDeps: {
    include: ["react", "react-dom"],
  },
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: rejects a decoy include in a section AFTER the test block" {
  # Ordering matters independently: an over-wide brace match would run past the
  # test object's closing brace and swallow later sections. The real config has
  # resolve: after test:, so post-test sections are not hypothetical.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {},
  optimizeDeps: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: rejects a narrowed test.include with the glob in a later section" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/only-one.test.ts"],
  },
  optimizeDeps: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
}

@test "test-layout: the extracted block stops at the test object's closing brace" {
  # Asserts the brace match directly rather than only its consequence.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
  resolve: {
    alias: { "@": "./src" },
  },
});
EOF
  # Extracted here rather than inside the subshell below, which does not inherit
  # bats helper functions.
  extract_shell_fn ci/checks/test-layout.sh strip_ts_comments  > "$SANDBOX/fns.sh"
  extract_shell_fn ci/checks/test-layout.sh extract_test_block >> "$SANDBOX/fns.sh"
  # A mis-extraction would otherwise define nothing and fail as "command not
  # found", which reads like a missing function rather than a broken fixture.
  bash -n "$SANDBOX/fns.sh"
  grep -q 'extract_test_block()' "$SANDBOX/fns.sh"
  run bash -c "
    cd '$SANDBOX'
    . '$SANDBOX/fns.sh'
    strip_ts_comments frontend/vitest.config.ts | extract_test_block
  "
  [ "$status" -eq 0 ]
  [[ "$output" == *"tests/**/*.test.{ts,tsx}"* ]]
  [[ "$output" != *"resolve"* ]]
  [[ "$output" != *"alias"* ]]
}

@test "test-layout: a helper object above defineConfig does not shadow the export" {
  # The first `test: {` token in the file is not necessarily vitest's. Anchoring
  # on `export default` is what makes the guard read the config that actually
  # ships.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const base = {
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
};

export default defineConfig({
  test: {
    include: ["tests/only.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: a plain exported object without defineConfig is read" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
export default {
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
};
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: a nested test key does not pass for the exported one" {
  # `test` under coverage/ or another sub-object must not satisfy the guard —
  # only the exported config's own property counts.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  server: {
    test: {
      include: ["tests/**/*.test.{ts,tsx}"],
    },
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
}

@test "test-layout: catches a config with no test block at all" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  optimizeDeps: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"test: { ... }"* ]]
}

@test "test-layout: reads a test block that nests other objects" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    coverage: { provider: "v8", thresholds: { lines: 80 } },
    include: ["tests/**/*.test.{ts,tsx}"],
    environment: "jsdom",
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: rejects a test.exclude it cannot verify" {
  # exclude is applied on top of include, so every include assertion can pass
  # while collection silently drops whole directories.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    exclude: ["tests/lib/**"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"test.exclude"* ]]
}

@test "test-layout: a commented-out exclude does not trip the guard" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    // exclude: ["tests/lib/**"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: an exclude outside the test block is not the guard's business" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  build: {
    exclude: ["something"],
  },
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: a stray test present only in the index fails the guard" {
  # A partially staged move: the index still holds the out-of-tree file while
  # the worktree shows only the moved copy. A filesystem-only scan blesses a
  # commit whose test is never collected.
  cd "$SANDBOX"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  printf 'it("stray", () => {});\n' > "$SANDBOX/frontend/src/probe.test.ts"
  git add frontend/src/probe.test.ts >/dev/null 2>&1
  mv "$SANDBOX/frontend/src/probe.test.ts" "$SANDBOX/frontend/tests/probe.test.ts"
  [ ! -f "$SANDBOX/frontend/src/probe.test.ts" ]
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"frontend/src/probe.test.ts"* ]]
}

@test "test-layout: a clean index and worktree still pass" {
  cd "$SANDBOX"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  run_guard
  [ "$status" -eq 0 ]
}

# --- a quoted property is the same property -----------------------------------

@test "test-layout: catches a quoted test.exclude" {
  # `"exclude": [...]` is valid TS and vitest applies it, but an
  # identifier-only pattern does not see it — so an exclusion that drops whole
  # test directories read as clean.
  mkdir -p "$SANDBOX/frontend/tests/lib"
  printf 'it("y", () => {});\n' > "$SANDBOX/frontend/tests/lib/b.test.ts"
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    "exclude": ["tests/lib/**"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"exclude"* ]]
}

@test "test-layout: catches a single-quoted test.exclude" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    'exclude': ["tests/lib/**"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"exclude"* ]]
}

@test "test-layout: accepts a quoted include" {
  # The include side failed the other way: a quoted key was reported missing.
  # Fail-closed, but still wrong about the file, and a false positive on a
  # guard like this is how it ends up switched off.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    "include": ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: accepts a quoted test block" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  "test": {
    "include": ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: a decoy inside a template literal is not read as config" {
  # A brace-and-token scan that reads through strings takes the decoy for the
  # exported test block and stops there, so the narrowed real include never
  # gets checked and both files report runnable while vitest collects one.
  printf 'it("y", () => {});\n' > "$SANDBOX/frontend/tests/b.test.ts"
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  __note: `test: { include: ["tests/**/*.test.{ts,tsx}"] }`,
  test: {
    include: ["tests/a.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: a decoy in a double-quoted string is not read as config" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  __note: "test: { include: [\"tests/**/*.test.{ts,tsx}\"] }",
  test: {
    include: ["tests/a.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
}

@test "test-layout: a string carrying an unbalanced brace does not derail the scan" {
  # The declared glob is itself full of braces. One that does not pair would
  # throw a depth count that reads through strings straight off the object.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  define: { __ODD__: "a stray { brace" },
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: catches an exclusion inherited through a spread" {
  # A spread carries in properties the guard never sees. Every direct-property
  # assertion passes while vitest applies whatever exclude the spread object
  # holds — the same silent drop the exclude rule exists to prevent, one
  # indirection out of reach.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const hidden = { exclude: ["tests/lib/**"] };

export default defineConfig({
  test: {
    ...hidden,
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"spreads another object into test"* ]]
}

@test "test-layout: catches an exclude written on the same line as the include" {
  # A line-anchored grep reads formatting, not structure. The compact block is
  # the same configuration and vitest applies it identically.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { include: ["tests/**/*.test.{ts,tsx}"], exclude: ["tests/lib/**"] },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"exclude"* ]]
}

@test "test-layout: accepts a compact test block with no exclude" {
  # The converse: formatting alone must not fail a conforming config either.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({ test: { include: ["tests/**/*.test.{ts,tsx}"] } });
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: an exclude in a nested section is not the guard's business" {
  # Properties are read at the test object's own level. coverage.exclude does
  # not drop tests, and failing on it would be a false positive.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    coverage: { exclude: ["src/generated/**"] },
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: a spread outside the test block is not the guard's business" {
  # The rule has to stay inside test: { }. A spread anywhere else in the config
  # cannot reach test.exclude, and failing on it would be a false positive.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const shared = { clearScreen: false };

export default defineConfig({
  ...shared,
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: catches a persistent test-name filter" {
  # vitest 3.2 exits 0 with every test reported skipped when testNamePattern
  # matches nothing, while the guard reported all files runnable.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    testNamePattern: "nothing-matches-this",
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"testNamePattern"* ]]
}

@test "test-layout: catches any test property it cannot evaluate" {
  # The rule is an allow-list, not a list of known-bad options: enumerating the
  # dangerous ones a finding at a time is what produced testNamePattern after
  # exclude and the spread. passWithNoTests is a different route to the same
  # green-on-nothing result.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    passWithNoTests: true,
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"passWithNoTests"* ]]
}

@test "test-layout: the allow-list admits the properties this repo actually uses" {
  # The converse, and the cost of an allow-list: it must not fail a conforming
  # config. This is the shape of frontend/vitest.config.ts.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    setupFiles: ["src/test-setup.ts"],
    globals: true,
    css: false,
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: catches an include computed by an expression" {
  # A substring search finds the declared glob in the branch vitest never takes.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: false ? ["tests/**/*.test.{ts,tsx}"] : ["tests/only.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"computes test.include"* ]]
}

@test "test-layout: catches an array expression in test.include" {
  # "Starts with [" is not "is an array": .slice(1) hands vitest one element
  # while the declared glob is still visible in the text.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}", "tests/only.test.ts"].slice(1),
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"computes test.include"* ]]
}

@test "test-layout: catches an include built by concatenation" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"].concat(["tests/other.test.ts"]),
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
}

@test "test-layout: a literal include carrying a trailing comment is still literal" {
  # The boundary: literal-with-noise is fine, expression-shaped is not.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"], // the single declared layout
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: catches a missing vitest config" {
  rm "$SANDBOX/frontend/vitest.config.ts"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"cannot confirm the test layout is declared"* ]]
}

@test "test-layout: catches a config staged for deletion but restored on disk" {
  # The mirror image of the stray-test case. `git rm --cached` stages the
  # deletion while the worktree keeps a perfectly valid copy, so a scan that
  # reads only the worktree sees a declared layout that the commit would not
  # contain. The committed tree would have no vitest config at all and vitest
  # would fall back to its default glob — collecting the very files this guard
  # exists to reject.
  cd "$SANDBOX"
  git init -q .
  git add -A >/dev/null 2>&1
  git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
  git rm --cached -q frontend/vitest.config.ts >/dev/null 2>&1
  [ -f "$SANDBOX/frontend/vitest.config.ts" ]
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"not in the git index"* ]]
}

@test "test-layout: an unstaged new config is reported, not accepted" {
  # The same rule from the other direction, pinned so it is not later mistaken
  # for a false positive: a config that exists only in the worktree declares
  # nothing about the commit. The message names the fix rather than the fault.
  cd "$SANDBOX"
  git init -q .
  git add -A >/dev/null 2>&1
  git rm --cached -q frontend/vitest.config.ts >/dev/null 2>&1
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"git add frontend/vitest.config.ts"* ]]
}

@test "test-layout: the index check is inert where there is no index" {
  # ci/preflight.sh also runs from a plain export or tarball with no .git. The
  # guard must fall back to the worktree copy there instead of failing on an
  # index it cannot read.
  cd "$SANDBOX"
  if git -C "$SANDBOX" rev-parse --git-dir >/dev/null 2>&1; then
    skip "the sandbox temp dir is itself inside a repository on this host"
  fi
  run_guard
  [ "$status" -eq 0 ]
}

# --- wiring -------------------------------------------------------------------
#
# Registering the check in manifest.yml documents it; only preflight.sh and
# lanes.conf execute it. A guard nobody runs is the failure mode this whole
# check exists to prevent, so assert the wiring rather than the registration.

@test "test-layout: scheduled by preflight quick mode" {
  run extract_shell_fn ci/preflight.sh run_common_checks
  [ "$status" -eq 0 ]
  [[ "$output" == *"test-layout:./ci/checks/test-layout.sh"* ]]
}

@test "test-layout: scheduled by preflight full and ship modes" {
  run extract_shell_fn ci/preflight.sh run_full_or_ship_checks
  [ "$status" -eq 0 ]
  [[ "$output" == *"test-layout:./ci/checks/test-layout.sh"* ]]
}

@test "test-layout: registered as a blocking lane" {
  run grep -E '^test-layout\|' ci/config/lanes.conf
  [ "$status" -eq 0 ]
  [[ "$output" == *"./ci/checks/test-layout.sh|yes|"* ]]
}

@test "test-layout: the README does not overclaim which modes run it" {
  # debt mode dispatches only git-safety and debt. Documenting "every mode"
  # would tell an operator a green --mode debt means the layout is clean.
  run grep -n "every mode" frontend/README.md
  [ "$status" -ne 0 ]
  run grep -c "debt" frontend/README.md
  [ "$status" -eq 0 ]
  [ "$output" -ge 1 ]
}

@test "test-layout: catches an include whose elements are computed" {
  # The outer brackets can be a genuine array literal and the contents still be
  # decided at runtime. `[...(cond ? [glob] : [])]` opens with `[`, closes on
  # the last character, and contains the declared glob in text — everything the
  # literal test asked for — while vitest may receive an empty include.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: [...(process.env.CI ? ["tests/**/*.test.{ts,tsx}"] : [])],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"computes test.include"* ]]
}

@test "test-layout: catches an include element built from a variable" {
  # Same class, simplest form: the glob is a string in the file but not the
  # string vitest is handed.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const dir = "tests";

export default defineConfig({
  test: {
    include: [dir + "/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"computes test.include"* ]]
}

@test "test-layout: a two-element literal include is still literal" {
  # The boundary for the element rule: several plain quoted strings, with the
  # trailing comma vitest configs are usually written with, stay acceptable.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: [
      "tests/**/*.test.{ts,tsx}",
      "tests/**/*.spec.{ts,tsx}",
    ],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: a decoy export default inside a string does not anchor the read" {
  # The scan below the anchor was made string-aware, but the anchor itself was
  # still a raw substring search — so a quoted `export default` earlier in the
  # file re-pointed the whole extraction at prose. Here the string carries a
  # conforming config and the real one narrows the include to a single file:
  # reading the decoy passes, reading the export fails.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const usage = "export default defineConfig({ test: { include: ['tests/**/*.test.{ts,tsx}'] } })";

export default defineConfig({
  test: {
    include: ["tests/only.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: prose mentioning export default does not fail a conforming config" {
  # The control for the case above: the anchor still has to be found. A string
  # containing the phrase must not make the guard give up on a healthy file.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const note = "see the export default below";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: catches an interpolated template element in test.include" {
  # A backtick value passed every literal test — it opens and closes with the
  # same quote and the outer bracket's match is the last character — while
  # `${...}` chooses the string vitest actually receives at runtime, leaving the
  # declared glob visible in the branch never taken.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: [`${false ? "tests/**/*.test.{ts,tsx}" : "tests/only.test.ts"}`],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"computes test.include"* ]]
}

@test "test-layout: a plain template element is still literal" {
  # The boundary: a backtick with nothing computed in it is an ordinary string,
  # and prettier writes them.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: [`tests/**/*.test.{ts,tsx}`],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: a decoy export default inside a regex literal does not anchor the read" {
  # The string-aware anchor was still blind to the other delimiter a JS file
  # can hide text behind. Here the regex carries the phrase, a helper object
  # below it carries the conforming glob, and the real export narrows the
  # include: anchoring at the regex reads the helper and passes.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const mention = /export default/;

const base = {
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
};

export default defineConfig({
  test: {
    include: ["tests/only.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: division is not mistaken for a regex" {
  # The other half of the disambiguation. A "/" after a value divides, and
  # treating it as a regex would swallow the rest of the config.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const half = 10 / 2;

export default defineConfig({
  test: {
    testTimeout: 1000 / half,
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: a regex inside the test block does not derail the scan" {
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    name: "unit",
    exclude: [],
  },
});
EOF
  run_guard
  # exclude is still rejected — the point is that the block was read at all,
  # rather than the scan losing its place inside a slash.
  [ "$status" -eq 20 ]
  [[ "$output" == *"exclude"* ]]
}

@test "test-layout: a regex after a keyword is not read as division" {
  # `return /x/` ends in the "n" of a keyword, and an identifier character alone
  # said "division" — so the anchor landed inside the regex again, one step past
  # the fix that made it regex-aware.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

function mention() { return /export default/ }

const base = {
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
};

export default defineConfig({
  test: {
    include: ["tests/only.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: an identifier before a slash still divides" {
  # The control that the keyword rule must not swallow: `total / count` ends in
  # an identifier character too, and reading it as a regex would consume the
  # rest of the config.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const total = 10;
const count = 2;
const each = total / count;

export default defineConfig({
  test: {
    testTimeout: 1000 * each,
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: catches a compound expression inside the include array" {
  # `["a" && "b"]` opens and closes with the same quote, so an endpoint-only
  # check called it literal while vitest received only the second operand.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}" && "tests/only.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"computes test.include"* ]]
}

@test "test-layout: a regex after a control statement is not read as division" {
  # `if (true) /re/.test(x)` ends in ")", and treating every ")" as an
  # expression operand classified the slash as division — so the anchor landed
  # inside the regex, one step past the keyword fix.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

if (true) /export default/.test("x");

const base = {
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
};

export default defineConfig({
  test: {
    include: ["tests/only.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"no longer declares an active test.include"* ]]
}

@test "test-layout: a parenthesised expression before a slash still divides" {
  # The control the keyword-and-paren rule must not swallow: `(a + b) / c` ends
  # in ")" too, and reading it as a regex consumes the rest of the config.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const a = 10;
const b = 6;
const each = (a + b) / 2;

export default defineConfig({
  test: {
    testTimeout: 100 * each,
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
}

@test "test-layout: ship mode sees a stray test that only HEAD carries" {
  # The union was worktree plus index, which describes the pre-commit gate. Take
  # a stray test out of both without committing the removal and the guard
  # reported OK for a push whose tree still carries it — while node.sh, on the
  # identical state, reported drift.
  ( cd "$SANDBOX" && git init -q -b f . && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm init ) >/dev/null 2>&1
  printf 'it("stray", () => {});\n' > "$SANDBOX/frontend/src/probe.test.ts"
  ( cd "$SANDBOX" && git add -A && git -c user.email=t@t -c user.name=t commit -qm stray ) >/dev/null 2>&1
  ( cd "$SANDBOX" && git rm -q --cached frontend/src/probe.test.ts ) >/dev/null 2>&1
  rm -f "$SANDBOX/frontend/src/probe.test.ts"
  # The premise: HEAD still carries it.
  run bash -c "cd '$SANDBOX' && git ls-tree -r --name-only HEAD -- frontend"
  [[ "$output" == *"probe.test.ts"* ]]
  run bash -c "cd '$SANDBOX' && bash ci/checks/test-layout.sh"
  [ "$status" -eq 0 ]
  run bash -c "cd '$SANDBOX' && CI_GATE_MODE=ship bash ci/checks/test-layout.sh"
  [ "$status" -eq 20 ]
  [[ "$output" == *"probe.test.ts"* ]]
}

@test "test-layout: two top-level test blocks are refused, not resolved" {
  # The scan returned on the *first* `test:` at the exported object's own level,
  # but JavaScript keeps the later property. A broad include followed by a
  # narrow one therefore validated the broad one -- every filesystem test
  # reported runnable, exit 0 -- while vitest collected the single file the
  # second block names.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
  test: {
    include: ["tests/only.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"more than one top-level"* ]]
}

@test "test-layout: a nested test key is not mistaken for a second block" {
  # The control. `depth == 1` is what makes the rule above safe: a `test`
  # property inside another object is not a second exported config, and
  # refusing it would fail an ordinary file.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  define: { test: { nested: true } },
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
  [[ "$output" != *"more than one top-level"* ]]
}

@test "test-layout: a computed test key is refused, not skipped over" {
  # `["test"]: {...}` is applied by JavaScript exactly like `test:`, and later
  # if it comes later -- but the quoted token is consumed by the string handling
  # in the scan, so a broad plain block followed by a narrow computed one was
  # counted once and reported as fully covered while vitest collected the
  # narrowed file. This reader works on text and cannot evaluate a computed key,
  # so it stops rather than guessing.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
  ["test"]: {
    include: ["tests/only.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"computed"* ]]
}

@test "test-layout: an array value elsewhere is not read as a computed key" {
  # The control: `[` appears constantly in ordinary configs -- every include is
  # an array. Only a `[` at the exported object's own level, where a key would
  # be, is a computed property, and treating any bracket as one would fail
  # every config there is.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    setupFiles: ["tests/setup.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
  [[ "$output" != *"computed"* ]]
}

@test "test-layout: a negated include entry is refused" {
  # `['tests/**/*.test.{ts,tsx}', '!tests/lib/**']` is a literal array and does
  # contain the declared glob, so every check above it passed -- while vitest
  # collected no tests/lib file at all. It is the same silent drop as
  # test.exclude, written one property over, and out of reach of the rule that
  # catches exclude.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}", "!tests/lib/**"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"negates inside test.include"* ]]
}

@test "test-layout: a negation in the middle of an include entry is refused too" {
  # Globs subtract in two shapes. Matching only a leading `!` would leave
  # picomatch's extglob `!(...)` doing the same thing from inside the pattern --
  # the same partial cover that let the first shape through.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/!(lib)/*.test.{ts,tsx}", "tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"negates inside test.include"* ]]
}

@test "test-layout: an ordinary additive include is still accepted" {
  # The control. A rule that refuses every include would satisfy both cases
  # above, and this repository's own config has to keep passing.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}", "tests/**/*.spec.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
  [[ "$output" != *"negates inside"* ]]
}

@test "test-layout: a top-level array value is not a computed key" {
  # The round that added computed-key detection marked *any* `[` at the exported
  # object own level as one -- and `[` does not change brace depth, so an
  # ordinary `plugins: [react()]` sat at that level too. Adding a bare
  # `plugins: []` to this repository's own config made the check exit 20 with
  # "more than one top-level test block". A guard that fails correct work gets
  # switched off, and then it guards nothing.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [],
  resolve: { alias: [] },
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
  [[ "$output" != *"more than one top-level"* ]]
}

@test "test-layout: a computed key after a top-level array is still refused" {
  # The control for the narrowing above. `[` is a computed key where a *key*
  # would be -- after the brace or a comma -- and a value everywhere else, so
  # the case the rule exists for has to survive sitting next to an array.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [],
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
  ["test"]: {
    include: ["tests/only.test.ts"],
  },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"computed"* ]]
}

@test "test-layout: a composed export is refused, not read as its first object" {
  # This reader takes the first object literal after `export default` as the
  # config. Object.assign hands it the *broad* object first and then replaces
  # `test` wholesale with the second, so the guard validated a config that is
  # not in force while vitest collected one file.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig(Object.assign({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
}, { test: { include: ["tests/only.test.ts"] } }));
EOF
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"composes its exported config"* ]]
}

@test "test-layout: a plain defineConfig export is still accepted" {
  # The control. The wrapper allow-list is `defineConfig(` or nothing at all,
  # and a rule that refused every wrapper would fail this repository outright.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
EOF
  run_guard
  [ "$status" -eq 0 ]
  [[ "$output" != *"composes"* ]]
}

@test "test-layout: a spread at the exported object own level is refused" {
  # The same failure as a computed key, one property over: `{ test: {...},
  # ...narrow }` applies the spread last, so the block captured here is not the
  # one vitest ends up using. The block reader already refuses a spread inside
  # test; this level had never been asked.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";
import narrow from "./narrow";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
  },
  ...narrow,
});
EOF
  run_guard
  [ "$status" -eq 20 ]
}

@test "test-layout: a newline in a path cannot fake a tests/ location" {
  # The candidate list was line-oriented, so a path containing a newline arrived
  # as two, and the tail could be spelled to look like it sits under
  # frontend/tests/. The stray test then reported as covered: "Test layout OK",
  # exit 0, while vitest collects nothing of the sort.
  local evil="$SANDBOX/frontend/foo
frontend/tests"
  if ! mkdir -p "$evil" 2>/dev/null; then
    skip "this filesystem refuses a newline in a directory name"
  fi
  printf 'it("y", () => {});\n' > "$evil/evil.test.ts"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"NEVER RUN"* ]]
  rm -rf "$evil"
}

@test "test-layout: ordinary paths are unaffected by the NUL-delimited scan" {
  # The control. Reading NUL-delimited from find, ls-files and ls-tree is a
  # change to every path this guard sees, so the ordinary layout has to keep
  # passing -- and a stray test with a perfectly normal name has to keep being
  # caught.
  run_guard
  [ "$status" -eq 0 ]

  printf 'it("z", () => {});\n' > "$SANDBOX/frontend/src/stray.test.ts"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"stray.test.ts"* ]]
  rm -f "$SANDBOX/frontend/src/stray.test.ts"
}

@test "test-layout: a shorthand test property overrides the block and is refused" {
  # `const test = {...}` plus `defineConfig({ test: { include: [broad] }, test })`
  # applies the shorthand last, so vitest used the narrow one while the guard
  # read the broad one and reported every file runnable. Only the colon form was
  # ever recognised as a `test` key.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

const test = { include: ["tests/only.test.ts"] };

export default defineConfig({ test: { include: ["tests/**/*.test.{ts,tsx}"] }, test });
EOF
  run_guard
  [ "$status" -eq 20 ]

  # A method form reaches the same place by another spelling.
  cat > "$SANDBOX/frontend/vitest.config.ts" <<'EOF'
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { include: ["tests/**/*.test.{ts,tsx}"] },
  test() { return { include: ["tests/only.test.ts"] }; },
});
EOF
  run_guard
  [ "$status" -eq 20 ]
}

@test "test-layout: an unreadable staged config is infrastructure, not a pass" {
  # The index lists the blob and git cannot produce it. Dropping it silently
  # left the caller validating the worktree copy alone and reporting PASS --
  # approving a commit whose active include was never inspected.
  run grep -n 'cannot read that blob' "$REPO_ROOT/ci/checks/test-layout.sh"
  [ "$status" -eq 0 ]
  run bash -c "sed -n '/^config_sources()/,/^}/p' '$REPO_ROOT/ci/checks/test-layout.sh'"
  [[ "$output" == *"CI_RESULT_FAIL_INFRA"* ]]
  # And it must not be reachable only through the mktemp arm: the git-show
  # failure has its own exit.
  run bash -c "sed -n '/^config_sources()/,/^}/p' '$REPO_ROOT/ci/checks/test-layout.sh' | grep -c 'exit \"\$CI_RESULT_FAIL_INFRA\"'"
  [ "$output" -eq 2 ]
}
