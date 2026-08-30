import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

// Keep this flat config explicitly ESM so analyzers load the same module as ESLint.
export default tseslint.config(
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
              message: "Production components must not import mock data; use @/types/domain and API hooks.",
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
  },
);
