/** Host-side v3 lifecycle: rotate both Ed25519 domains on every sidecar run. */
import { randomBytes } from "node:crypto";
import { C10ReceiptError } from "./contracts.js";
import { generateEphemeralC10Keys, type C10EphemeralKeyMaterial } from "./local_browser_executor.js";
import { PrivateReceiptStore } from "./private_store.js";

const SIDECAR_ENV = ["C10_COORDINATOR_PUBLIC_KEY_PEM", "C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM", "PLAYWRIGHT_HOST_TRANSPORT_V3_KEY"] as const;
export interface C10SidecarLifecycle { start(environment: Readonly<Record<string, string>>): Promise<{ stop(): Promise<void> }>; }
export type C10HealthCheck = (material: C10V3SessionMaterial) => Promise<boolean>;
export interface C10V3SessionMaterial extends C10EphemeralKeyMaterial { readonly hostTransportKey: string; }
export interface LocalC10OperatorPreflight { readonly lifecycle: C10SidecarLifecycle; readonly healthCheck: C10HealthCheck; readonly receiptStoreAvailable?: () => boolean; readonly lockClaim: Readonly<{ canonical: true; held: true; assertHeld(): void }>; readonly environment?: Record<string, string | undefined>; }

/**
 * Production callers must already hold the actual canonical SharedLock.  No
 * keypair, replay state, or capability survives this callback.  A durable
 * replay registry is prohibited while these keys are ephemeral.
 */
export async function withEphemeralLocalC10Sidecar<T>(options: LocalC10OperatorPreflight, run: (material: C10V3SessionMaterial) => Promise<T>): Promise<T> {
  if (!(options.receiptStoreAvailable ?? PrivateReceiptStore.safeRuntimeAvailable)()) throw new C10ReceiptError("C10 v3 requires a Linux fd-relative private receipt store");
  if (options.lockClaim.canonical !== true || options.lockClaim.held !== true) throw new C10ReceiptError("C10 v3 requires an active canonical SharedLock claim");
  options.lockClaim.assertHeld();
  const environment = options.environment ?? process.env;
  if (SIDECAR_ENV.some((key) => environment[key] !== undefined)) throw new C10ReceiptError("C10 v3 refuses pre-existing sidecar credentials");
  const keys = generateEphemeralC10Keys();
  const material = Object.freeze({ ...keys, hostTransportKey: randomBytes(32).toString("base64url") });
  let sidecar: Awaited<ReturnType<C10SidecarLifecycle["start"]>> | undefined;
  try {
    // The coordinator private key is callback-only. The sidecar sees only the
    // coordinator public key plus its own distinct evidence private key.
    sidecar = await options.lifecycle.start(Object.freeze({ C10_COORDINATOR_PUBLIC_KEY_PEM: material.coordinatorPublicKeyPem, C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM: material.sidecarEvidencePrivateKeyPem, PLAYWRIGHT_HOST_TRANSPORT_V3_KEY: material.hostTransportKey }));
    if (!await options.healthCheck(material)) throw new C10ReceiptError("C10 v3 sidecar key/health preflight failed");
    options.lockClaim.assertHeld();
    return await run(material);
  } finally {
    await sidecar?.stop().catch(() => undefined);
  }
}
