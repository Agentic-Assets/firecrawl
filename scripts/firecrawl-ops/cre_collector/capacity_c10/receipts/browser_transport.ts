import { C10ReceiptError, assertBinding, canonicalSha256, sha256, type ReceiptBinding } from "./contracts.js";
import { type DirectProviderTransport, type RequestCard, type TransportResponse } from "./transport.js";

export const C10_REVIEWED_BROWSER_ENGINE = "playwright-service" as const;
export interface BrowserTrustedEvidence {
  readonly protocolVersion: 3;
  readonly kind: "cre_capacity_c10_browser_execution_evidence_v3";
  readonly binding: Readonly<Record<string, string>>;
  readonly engineAttempt: Readonly<{ engine: typeof C10_REVIEWED_BROWSER_ENGINE; ordinal: 1; fallbackDisabled: true; fallbackUsed: false }>;
  readonly pageLease: Readonly<{ leaseId: string; slot: number }>;
  readonly leaseStartMonotonicNs: string;
  readonly leaseEndMonotonicNs: string;
  readonly observedActivePages: number;
  readonly configuredCapacity: number;
  readonly context: Readonly<{ ephemeral: true; storageState: "none"; cache: "disabled-cdp-and-fetch-no-store" }>;
  readonly rawResponseSha256: string;
  readonly cacheRead: false;
  readonly cacheWrite: false;
  readonly source: Readonly<{ status: number; finalUrl: string; contentType: string | null }>;
}
export interface BrowserExecutionInstruction { readonly card: Readonly<RequestCard>; readonly cardSha256: string; readonly binding: ReceiptBinding; }
export interface InternalBrowserExecutor { execute(instruction: Readonly<BrowserExecutionInstruction>): Promise<TransportResponse>; }

/** Constructor-sealed one-shot browser transport. It neither mints nor verifies capabilities. */
export class BrowserTransport implements DirectProviderTransport {
  private readonly cards = new Map<string, string>();
  private readonly consumed = new Set<string>();
  private readonly binding: ReceiptBinding;
  constructor(sourceKey: string, binding: ReceiptBinding, cards: ReadonlyMap<string, RequestCard>, private readonly executor: InternalBrowserExecutor) {
    this.binding = assertBinding(binding);
    if (cards.size === 0) throw new C10ReceiptError("browser transport needs predeclared request cards");
    for (const [id, card] of cards) { if (id !== card.id || card.sourceKey !== sourceKey) throw new C10ReceiptError("browser transport request-card registry is malformed"); this.cards.set(id, canonicalSha256(card)); }
  }
  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    const cardSha256 = canonicalSha256(card);
    if (this.cards.get(card.id) !== cardSha256 || this.consumed.has(cardSha256)) throw new C10ReceiptError("browser transport accepts one predeclared request card");
    this.consumed.add(cardSha256);
    let response: TransportResponse;
    try { response = await this.executor.execute(Object.freeze({ card, cardSha256, binding: this.binding })); } catch (error) { if (error instanceof C10ReceiptError) throw error; throw new C10ReceiptError("browser transport execution failed without fallback"); }
    const evidence = response.trustedBrowserEvidence;
    if (!evidence || evidence.protocolVersion !== 3 || evidence.kind !== "cre_capacity_c10_browser_execution_evidence_v3" || evidence.engineAttempt.engine !== C10_REVIEWED_BROWSER_ENGINE || evidence.engineAttempt.ordinal !== 1 || evidence.engineAttempt.fallbackDisabled !== true || evidence.engineAttempt.fallbackUsed !== false || evidence.cacheRead !== false || evidence.cacheWrite !== false || evidence.context.ephemeral !== true || evidence.context.storageState !== "none" || evidence.rawResponseSha256 !== sha256(response.body) || evidence.source.status !== response.status || evidence.source.finalUrl !== response.finalUrl || evidence.source.contentType !== response.contentType || evidence.binding.cardSha256 !== cardSha256 || evidence.binding.armSha256 !== this.binding.armSha256) throw new C10ReceiptError("trusted C10 v3 browser evidence is contradictory");
    return Object.freeze({ ...response, trustedBrowserEvidence: evidence });
  }
}
