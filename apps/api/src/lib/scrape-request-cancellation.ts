import type { Request, Response } from "express";
import type { AbortInstance } from "../scraper/scrapeURL/lib/abortManager";
import { JobCancelledError } from "./error";

/** Fork-owned cancellation for an in-process synchronous scrape, never queue JSON. */
export function observeScrapeRequestCancellation(req: Request, res: Response) {
  const controller = new AbortController();
  const abort = () => controller.abort(new JobCancelledError());
  const responseClosed = () => {
    if (!res.writableFinished) abort();
  };
  req.once("aborted", abort);
  res.once("close", responseClosed);
  if (req.aborted || (res.destroyed && !res.writableFinished)) abort();
  let admission: AbortController | undefined;
  const forward = () => admission?.abort(controller.signal.reason);

  return {
    bindAdmission(value: AbortController) {
      admission = value;
      controller.signal.addEventListener("abort", forward, { once: true });
      if (controller.signal.aborted) forward();
    },
    get externalAbort(): AbortInstance {
      controller.signal.throwIfAborted();
      return {
        signal: controller.signal,
        tier: "external",
        throwable: () => controller.signal.reason,
      };
    },
    dispose() {
      req.removeListener("aborted", abort);
      res.removeListener("close", responseClosed);
      controller.signal.removeEventListener("abort", forward);
    },
  };
}
