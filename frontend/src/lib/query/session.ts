import { useQuery } from "@tanstack/react-query";

import { useApiClient } from "@/lib/api/client";
import type { SessionMe } from "@/lib/api/types";

export function useSessionMeQuery() {
  const api = useApiClient();
  return useQuery({
    queryKey: ["session", "me"],
    queryFn: () => api.get<SessionMe>("/session/me"),
  });
}
