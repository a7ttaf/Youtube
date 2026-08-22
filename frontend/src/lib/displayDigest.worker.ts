import { computeDisplayDigestFromFields, type DisplayDigestPlanFields } from "@/lib/displayDigestCore";

type DigestWorkerRequest = {
  id: number;
  plan: DisplayDigestPlanFields;
};

type DigestWorkerResponse =
  | { id: number; digest: string }
  | { id: number; error: string };

self.onmessage = (event: MessageEvent<DigestWorkerRequest>) => {
  try {
    const digest = computeDisplayDigestFromFields(event.data.plan);
    const response: DigestWorkerResponse = { id: event.data.id, digest };
    self.postMessage(response);
  } catch (error) {
    const response: DigestWorkerResponse = {
      id: event.data.id,
      error: error instanceof Error ? error.message : String(error),
    };
    self.postMessage(response);
  }
};
