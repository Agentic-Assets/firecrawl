import {
  C10ReceiptError,
  assertBinding,
  canonicalSha256,
  requireSha256,
  sha256,
  type ReceiptBinding,
} from "./contracts.js";
import {
  type DirectProviderTransport,
  type RequestCard,
  type TransportResponse,
} from "./transport.js";

/** The sole browser engine admitted to C10 until a separately reviewed change. */
export const C10_REVIEWED_BROWSER_ENGINE = "playwright-service" as const;

export interface CoordinatorArmToken {
  readonly sourceKey: string;
  readonly armSha256: string;
  /** Opaque coordinator capability identifier. It is never sealed in plaintext. */
  readonly tokenId: string;
  readonly tokenSha256: string;
}

export interface BrowserPageLease {
  readonly leaseId: string;
  readonly slot: number;
}

export interface BrowserProxyMetadata {
  readonly mode: string;
  readonly proxyId: string | null;
  readonly country: string | null;
}

/**
 * This is deliberately body-free. The raw body itself is sealed only by the
 * receipt store after this proof has been checked against the transport result.
 */
export interface BrowserTrustedEvidence {
  readonly schemaVersion: 1;
  readonly kind: "cre_capacity_c10_browser_execution_evidence";
  readonly engine: typeof C10_REVIEWED_BROWSER_ENGINE;
  readonly engineAttempts: 1;
  readonly fallbackDisabled: true;
  readonly fallbackUsed: false;
  readonly pageLease: BrowserPageLease;
  readonly apiJobId: string;
  readonly coordinatorArmTokenSha256: string;
  readonly requestSha256: string;
  readonly requestBodySha256: string | null;
  readonly rawResponseSha256: string;
  readonly cacheRead: false;
  readonly cacheWrite: false;
  readonly queueMs: number;
  readonly source: {
    readonly status: number;
    readonly finalUrl: string;
    readonly contentType: string | null;
    readonly proxy: BrowserProxyMetadata;
  };
}

export interface BrowserExecutionInstruction {
  readonly card: Readonly<RequestCard>;
  readonly cardSha256: string;
  readonly armToken: Readonly<CoordinatorArmToken>;
  readonly engine: typeof C10_REVIEWED_BROWSER_ENGINE;
  readonly engineAttempts: 1;
  readonly fallbackDisabled: true;
  readonly cacheRead: false;
  readonly cacheWrite: false;
}

/**
 * The coordinator owns the capability verifier. BrowserTransport sees only a
 * one-shot opaque token and cannot mint, re-arm, or reuse it itself.
 */
export interface CoordinatorArmGate {
  consume(
    token: Readonly<CoordinatorArmToken>,
    cardSha256: string,
  ): Promise<void>;
}

/**
 * An internal bridge to a preconfigured browser worker. Implementations are
 * injected by an operator-wired coordinator, never selected from request data.
 */
export interface InternalBrowserExecutor {
  execute(instruction: Readonly<BrowserExecutionInstruction>): Promise<TransportResponse>;
}

function exactKeys(value: unknown, expected: readonly string[]): boolean {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  return Object.keys(value).sort().join(",") === [...expected].sort().join(",");
}

function requireNonEmpty(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0 || value.length > 512) {
    throw new C10ReceiptError(`trusted browser evidence ${label} is unavailable`);
  }
  return value;
}

function validateToken(token: CoordinatorArmToken, binding: ReceiptBinding, sourceKey: string): CoordinatorArmToken {
  if (!exactKeys(token, ["armSha256", "sourceKey", "tokenId", "tokenSha256"])
    || token.sourceKey !== sourceKey
    || token.armSha256 !== binding.armSha256) {
    throw new C10ReceiptError("browser transport coordinator arm token is not bound to this source and arm");
  }
  requireNonEmpty(token.tokenId, "token id");
  requireSha256(token.armSha256, "coordinator arm");
  requireSha256(token.tokenSha256, "coordinator arm token");
  return Object.freeze({ ...token });
}

function validateEvidence(
  evidence: BrowserTrustedEvidence | undefined,
  card: Readonly<RequestCard>,
  cardSha256: string,
  token: Readonly<CoordinatorArmToken>,
  response: TransportResponse,
): BrowserTrustedEvidence {
  if (
    !response
    || typeof response !== "object"
    || !(response.body instanceof Uint8Array)
    || !Number.isInteger(response.status)
    || typeof response.finalUrl !== "string"
    || (response.contentType !== null && typeof response.contentType !== "string")
  ) {
    throw new C10ReceiptError("browser transport returned an invalid source response");
  }
  if (!evidence || !exactKeys(evidence, [
    "apiJobId", "cacheRead", "cacheWrite", "coordinatorArmTokenSha256", "engine", "engineAttempts",
    "fallbackDisabled", "fallbackUsed", "kind", "pageLease", "queueMs", "rawResponseSha256",
    "requestBodySha256", "requestSha256", "schemaVersion", "source",
  ])) {
    throw new C10ReceiptError("trusted browser evidence is unavailable");
  }
  if (
    !exactKeys(evidence.pageLease, ["leaseId", "slot"])
    || !exactKeys(evidence.source, ["contentType", "finalUrl", "proxy", "status"])
    || !exactKeys(evidence.source.proxy, ["country", "mode", "proxyId"])
  ) {
    throw new C10ReceiptError("trusted browser evidence has invalid page, source, or proxy metadata");
  }
  if (
    evidence.schemaVersion !== 1
    || evidence.kind !== "cre_capacity_c10_browser_execution_evidence"
    || evidence.engine !== C10_REVIEWED_BROWSER_ENGINE
    || evidence.engineAttempts !== 1
    || evidence.fallbackDisabled !== true
    || evidence.fallbackUsed !== false
    || evidence.cacheRead !== false
    || evidence.cacheWrite !== false
    || evidence.requestSha256 !== cardSha256
    || evidence.requestBodySha256 !== card.bodySha256
    || evidence.rawResponseSha256 !== sha256(response.body)
    || evidence.coordinatorArmTokenSha256 !== token.tokenSha256
    || evidence.source.status !== response.status
    || evidence.source.finalUrl !== response.finalUrl
    || evidence.source.contentType !== response.contentType
    || !Number.isInteger(evidence.queueMs)
    || evidence.queueMs < 0
  ) {
    throw new C10ReceiptError("trusted browser evidence does not prove the reviewed one-shot execution");
  }
  if (!Number.isInteger(evidence.pageLease.slot)
    || evidence.pageLease.slot < 0
    || (evidence.source.contentType !== null && typeof evidence.source.contentType !== "string")
    || !Number.isInteger(evidence.source.status)
    || typeof evidence.source.finalUrl !== "string"
    || typeof evidence.source.proxy.mode !== "string"
    || (evidence.source.proxy.proxyId !== null && typeof evidence.source.proxy.proxyId !== "string")
    || (evidence.source.proxy.country !== null && typeof evidence.source.proxy.country !== "string")) {
    throw new C10ReceiptError("trusted browser evidence has invalid page, source, or proxy metadata");
  }
  requireNonEmpty(evidence.pageLease.leaseId, "page lease");
  requireNonEmpty(evidence.apiJobId, "API job id");
  requireSha256(evidence.coordinatorArmTokenSha256, "coordinator arm token");
  requireSha256(evidence.requestSha256, "request");
  if (evidence.requestBodySha256 !== null) requireSha256(evidence.requestBodySha256, "request body");
  requireSha256(evidence.rawResponseSha256, "raw response");
  return Object.freeze({
    ...evidence,
    pageLease: Object.freeze({ ...evidence.pageLease }),
    source: Object.freeze({
      ...evidence.source,
      proxy: Object.freeze({ ...evidence.source.proxy }),
    }),
  });
}

/**
 * C10's only browser-capable direct transport. It accepts only constructor-time
 * cards and one coordinator-gated capability, and exposes no public route.
 */
export class BrowserTransport implements DirectProviderTransport {
  private readonly cards = new Map<string, string>();
  private readonly token: CoordinatorArmToken;
  private readonly consumed = new Set<string>();

  constructor(
    sourceKey: string,
    binding: ReceiptBinding,
    cards: ReadonlyMap<string, RequestCard>,
    armToken: CoordinatorArmToken,
    private readonly armGate: CoordinatorArmGate,
    private readonly executor: InternalBrowserExecutor,
  ) {
    const sealedBinding = assertBinding(binding);
    this.token = validateToken(armToken, sealedBinding, sourceKey);
    if (cards.size === 0) throw new C10ReceiptError("browser transport needs predeclared request cards");
    for (const [id, card] of cards) {
      if (id !== card.id || card.sourceKey !== sourceKey) {
        throw new C10ReceiptError("browser transport request-card registry is malformed");
      }
      this.cards.set(id, canonicalSha256(card));
    }
  }

  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    const cardSha256 = canonicalSha256(card);
    if (this.cards.get(card.id) !== cardSha256) {
      throw new C10ReceiptError("browser transport accepts only predeclared allowlisted request cards");
    }
    if (this.consumed.has(cardSha256)) {
      throw new C10ReceiptError("browser transport request card was already consumed by its coordinator arm");
    }
    this.consumed.add(cardSha256);
    await this.armGate.consume(this.token, cardSha256);
    let response: TransportResponse;
    try {
      response = await this.executor.execute(Object.freeze({
        card,
        cardSha256,
        armToken: this.token,
        engine: C10_REVIEWED_BROWSER_ENGINE,
        engineAttempts: 1,
        fallbackDisabled: true,
        cacheRead: false,
        cacheWrite: false,
      }));
    } catch (error) {
      if (error instanceof C10ReceiptError) throw error;
      throw new C10ReceiptError("browser transport execution failed without fallback");
    }
    const evidence = validateEvidence(response.trustedBrowserEvidence, card, cardSha256, this.token, response);
    return Object.freeze({ ...response, trustedBrowserEvidence: evidence });
  }
}
