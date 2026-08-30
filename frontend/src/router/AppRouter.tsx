import { Navigate, Route, Routes, useParams } from "react-router-dom";

import AppShell from "@/components/srcc/AppShell";
import { isViewKey } from "@/config/navigation";
import type { ViewKey } from "@/types/domain";

// ============================================================================
// Purpose: Resolve one URL view segment into the typed AppShell entry point;
//          malformed single-segment URLs are redirected to the canonical home.
// Database/ORM: None (frontend route boundary).
// Standards: Route input is validated before reaching the typed shell; invalid
//            paths use replace navigation so the bad URL is not retained in
//            browser history.
// Blast Radius: URL correctness and browser history only; no data or authz.
// Connections:
//   - File: frontend/src/config/navigation.ts -> canonical ViewKey guard.
//   - File: frontend/src/components/srcc/AppShell.tsx -> typed view shell.
// ============================================================================
const ShellRoute = () => {
  const { view } = useParams<{ view?: string }>();
  if (!view || !isViewKey(view)) {
    return <Navigate to="/command" replace />;
  }
  const initialView: ViewKey = view;
  return <AppShell initialView={initialView} />;
};

// ============================================================================
// Purpose: Declare the canonical SRCC routes and their invalid-route fallback.
// Database/ORM: None (frontend route declarations).
// Standards: Root and unknown paths redirect with replace; supported view keys
//            are resolved by ShellRoute before AppShell renders.
// Blast Radius: Client-side navigation and deep-link behavior only.
// Connections:
//   - File: frontend/src/config/navigation.ts -> supported ViewKey values.
//   - File: frontend/src/main.tsx -> mounts this tree inside the data router.
// ============================================================================
export const AppRouter = () => {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/command" replace />} />
      <Route path="/:view" element={<ShellRoute />} />
      <Route path="*" element={<Navigate to="/command" replace />} />
    </Routes>
  );
};
