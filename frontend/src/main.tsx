import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";

import { AppRouter } from "@/router/AppRouter";
import { SessionProvider } from "@/contexts/SessionContext";
import { TenantProvider } from "@/contexts/TenantContext";
import "@/styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
    },
  },
});

// ============================================================================
// Purpose: Keep authenticated route providers inside the data router so
//          AppShell can block browser history while an unabortable write runs.
// Database/ORM: None (frontend providers only).
// Standards: Query, session, and tenant state remain shared for every route;
//            RouterProvider supplies the transition blocker contract.
// Blast Radius: Client navigation and provider lifetime; no API or finance
//                calculation changes.
// Connections:
//   - File: frontend/src/router/AppRouter.tsx -> route tree.
//   - File: frontend/src/components/srcc/AppShell.tsx -> useBlocker guard.
// ============================================================================
const RoutedApp = () => (
  <SessionProvider>
    <TenantProvider>
      <AppRouter />
    </TenantProvider>
  </SessionProvider>
);

const appRouter = createBrowserRouter([
  {
    path: "*",
    element: <RoutedApp />,
  },
]);

// ============================================================================
// Purpose: Mount the one shared QueryClient and the browser data router.
// Database/ORM: None (frontend bootstrap).
// Standards: The router owns history transitions; the QueryClient is created
//            once so view queries retain their existing cache behavior.
// Blast Radius: Application bootstrap only.
// Connections:
//   - File: frontend/src/lib/query/session.ts -> session query cache boundary.
//   - File: frontend/src/router/AppRouter.tsx -> rendered route declarations.
// ============================================================================
const AppProviders = () => {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={appRouter} />
    </QueryClientProvider>
  );
};

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Root element #root not found in document");
createRoot(rootEl).render(
  <StrictMode>
    <AppProviders />
  </StrictMode>,
);
