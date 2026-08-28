import { Navigate, Route, Routes, useParams } from "react-router-dom";

import AppShell from "@/components/srcc/AppShell";
import { isViewKey } from "@/config/navigation";
import type { ViewKey } from "@/types/domain";

function ShellRoute() {
  const { view } = useParams<{ view?: string }>();
  const initialView: ViewKey | undefined =
    view && isViewKey(view) ? view : undefined;
  return <AppShell initialView={initialView} />;
}

export function AppRouter() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/command" replace />} />
      <Route path="/:view" element={<ShellRoute />} />
      <Route path="*" element={<Navigate to="/command" replace />} />
    </Routes>
  );
}
