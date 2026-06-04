/// <reference types="vite/client" />

// ============================================================================
// Purpose: Vite ambient client types for the dashboard bundle. The triple-slash
//          reference makes `import.meta.env` (VITE_API_BASE_URL, DEV,
//          VITE_ENABLE_ROLE_PREVIEW, etc.) type-check under `tsc --noEmit`,
//          and the `*.css` module declaration types the `@/styles.css`
//          side-effect import in main.tsx (Vite resolves CSS at build time;
//          TypeScript needs the ambient shape).
// Database/ORM: None.
// Standards: Ambient declaration only; no runtime code. Keeps the frontend
//            type-check clean without loosening tsconfig.
// Blast Radius: None detected — type-only, no auth/finance/audit/export impact.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> uses import.meta.env.VITE_API_BASE_URL.
//   - File: frontend/src/components/srcc/AppShell.tsx -> uses import.meta.env.DEV.
//   - File: frontend/src/main.tsx -> imports "@/styles.css" for its side effect.
// ============================================================================

declare module "*.css";
