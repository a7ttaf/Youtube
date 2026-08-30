import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // ============================================================================
    // Purpose: Declare the single test layout for this workspace — every
    //   automated test lives under the top-level tests/ tree, mirroring src/
    //   without a __tests__ segment. Declared rather than left to vitest's
    //   default glob so the layout is explicit and enforceable.
    // Database/ORM: None.
    // Standards: This glob is the collection contract, not a convenience. A test
    //   outside it is not reported as misplaced — it is not collected at all, so
    //   it passes by never running. Narrowing or commenting out `include` is
    //   therefore a silent reduction in coverage, which is why an external guard
    //   reads this value rather than trusting it.
    // Blast Radius: Test coverage for the entire frontend. A change here decides
    //   which suites the `tests-js` gate lane executes; it has no runtime or data
    //   effect on the shipped application.
    // Connections:
    //   - File: ci/checks/test-layout.sh -> Parses this `include` value and fails
    //     the gate on a test outside the glob, a lingering `__tests__/`
    //     directory, a suffix the glob misses, or this key no longer being live
    //     config. It is what makes the declaration binding.
    //   - File: ci/config/affected.yml -> Maps changed sources to
    //     `frontend/tests/**` patterns; those patterns and this glob have to name
    //     the same tree or the narrowing selects nothing.
    //   - File: Docs/15_DELIVERY_BACKLOG.md -> Records the migration this layout
    //     came from and the counts it produced.
    // ============================================================================
    include: ["tests/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    setupFiles: ["src/test-setup.ts"],
    globals: true,
    css: false,
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      "@ums/design-system": fileURLToPath(
        new URL("./packages/design-system/src/index.tsx", import.meta.url),
      ),
    },
  },
});
