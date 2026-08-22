import { computeDisplayDigest } from "@/lib/displayDigest";
import type { ChannelImportResult } from "@/lib/api/types";

/** Attach a server-matching display_digest to a plan fixture for tests. */
export const withDisplayDigest = (
  plan: Omit<ChannelImportResult, "display_digest"> & { display_digest?: string },
): ChannelImportResult => ({
  ...plan,
  display_digest: computeDisplayDigest(plan),
});
