import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Single declared layout: every automated test lives under the top-level
    // tests/ tree, mirroring src/ without a __tests__ segment. Declared rather
    // than left to vitest's default glob so the layout is explicit.
    // ci/checks/test-layout.sh enforces it — without that guard a test file
    // dropped outside this glob would be silently skipped instead of failing.
    include: ["tests/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    setupFiles: ["src/test-setup.ts"],
    globals: true,
    css: false,
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
});
