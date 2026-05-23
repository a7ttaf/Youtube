import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import AppShell from "@/components/srcc/AppShell";
import { TenantProvider } from "@/contexts/TenantContext";
import "@/styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TenantProvider>
      <AppShell />
    </TenantProvider>
  </StrictMode>,
);
