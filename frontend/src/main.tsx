import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import AppShell from "@/components/srcc/AppShell";
import { SessionProvider } from "@/contexts/SessionContext";
import { TenantProvider } from "@/contexts/TenantContext";
import "@/styles.css";

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Root element #root not found in document");
createRoot(rootEl).render(
  <StrictMode>
    <SessionProvider>
      <TenantProvider>
        <AppShell />
      </TenantProvider>
    </SessionProvider>
  </StrictMode>,
);
