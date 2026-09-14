const js = require("@eslint/js");
const globals = require("globals");
const tseslint = require("typescript-eslint");

// Keep this flat config explicitly CJS so analyzers parse it with script
// sourceType while ESLint loads the identical module (eslint.config.cjs is
// auto-discovered the same way as .mjs for flat config).
module.exports = tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/components/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["@/lib/mock", "@/lib/mock/*"],
              message:
                "Production components must not import mock data; use @/types/domain and API hooks.",
            },
          ],
        },
      ],
    },
  },
  {
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // React-owned callback signatures (onCaughtError, componentDidCatch)
      // receive arguments the hardened reporter deliberately ignores; `_` marks
      // that intent instead of weakening the signature.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
);
