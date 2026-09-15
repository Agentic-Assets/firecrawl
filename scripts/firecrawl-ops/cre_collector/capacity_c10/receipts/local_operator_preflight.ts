/** Local-only, ephemeral C10 sidecar secret lifecycle. No value is logged or persisted. */
import { randomBytes } from "node:crypto";

import { C10ReceiptError } from "./contracts.js";
import { PrivateReceiptStore } from "./private_store.js";

const ARM_SECRET = "C10_BROWSER_ARM_SECRET";
const SIDECAR_SECRET = "C10_BROWSER_INTERNAL_SECRET";

export interface C10SidecarLifecycle {
  start(environment: Readonly<Record<string, string>>): Promise<{ stop(): Promise<void> }>;
}

export type C10HealthCheck = (serviceUrl: string) => Promise<boolean>;

export interface LocalC10OperatorPreflight {
  readonly serviceUrl: string;
  readonly lifecycle: C10SidecarLifecycle;
  readonly healthCheck: C10HealthCheck;
  /** The real private store gate is Linux-only and is checked before sidecar health. */
  readonly receiptStoreAvailable?: () => boolean;
  readonly environment?: Record<string, string | undefined>;
}

/** The default health probe contains no secret or card and only permits loopback URLs. */
export const loopbackHealthCheck: C10HealthCheck = async (serviceUrl) => {
  const url = new URL(serviceUrl);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) || url.pathname !== "/" || url.search || url.hash) {
    throw new C10ReceiptError("C10 sidecar health check must be loopback-only");
  }
  const response = await fetch(`${serviceUrl.replace(/\/$/, "")}/health`);
  return response.ok;
};

function set(environment: Record<string, string | undefined>, key: string, value: string | undefined): void {
  if (value === undefined) delete environment[key];
  else environment[key] = value;
}

/**
 * Starts one local sidecar with a generated shared secret, performs Linux and
 * health gates, and restores the caller environment even if startup/run fails.
 */
export async function withEphemeralLocalC10Sidecar<T>(
  options: LocalC10OperatorPreflight,
  run: (secret: string) => Promise<T>,
): Promise<T> {
  const receiptStoreAvailable = options.receiptStoreAvailable ?? PrivateReceiptStore.safeRuntimeAvailable;
  if (!receiptStoreAvailable()) throw new C10ReceiptError("C10 requires a Linux fd-relative private receipt store");
  const environment = options.environment ?? process.env;
  const priorArm = environment[ARM_SECRET];
  const priorSidecar = environment[SIDECAR_SECRET];
  if (priorArm !== undefined || priorSidecar !== undefined) {
    throw new C10ReceiptError("C10 refuses to overwrite an existing browser secret");
  }
  const secret = randomBytes(48).toString("hex");
  let sidecar: Awaited<ReturnType<C10SidecarLifecycle["start"]>> | undefined;
  try {
    set(environment, ARM_SECRET, secret);
    set(environment, SIDECAR_SECRET, secret);
    sidecar = await options.lifecycle.start(Object.freeze({ [SIDECAR_SECRET]: secret }));
    if (!await options.healthCheck(options.serviceUrl)) {
      throw new C10ReceiptError("C10 Playwright sidecar health is unavailable");
    }
    return await run(secret);
  } finally {
    await sidecar?.stop().catch(() => undefined);
    set(environment, ARM_SECRET, priorArm);
    set(environment, SIDECAR_SECRET, priorSidecar);
  }
}
