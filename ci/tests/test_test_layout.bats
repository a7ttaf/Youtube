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
  run bash -c "
    cd '$SANDBOX'
    sed -n '/^strip_ts_comments()/,/^}/p;/^extract_test_block()/,/^}/p' ci/checks/test-layout.sh > '$SANDBOX/fns.sh'
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

@test "test-layout: catches a missing vitest config" {
  rm "$SANDBOX/frontend/vitest.config.ts"
  run_guard
  [ "$status" -eq 20 ]
  [[ "$output" == *"cannot confirm the test layout is declared"* ]]
}

# --- wiring -------------------------------------------------------------------
#
# Registering the check in manifest.yml documents it; only preflight.sh and
# lanes.conf execute it. A guard nobody runs is the failure mode this whole
# check exists to prevent, so assert the wiring rather than the registration.

@test "test-layout: scheduled by preflight quick mode" {
  run bash -c "sed -n '/^run_common_checks()/,/^}/p' ci/preflight.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"test-layout:./ci/checks/test-layout.sh"* ]]
}

@test "test-layout: scheduled by preflight full and ship modes" {
  run bash -c "sed -n '/^run_full_or_ship_checks()/,/^}/p' ci/preflight.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"test-layout:./ci/checks/test-layout.sh"* ]]
}

@test "test-layout: registered as a blocking lane" {
  run grep -E '^test-layout\|' ci/config/lanes.conf
  [ "$status" -eq 0 ]
  [[ "$output" == *"./ci/checks/test-layout.sh|yes|"* ]]
}
