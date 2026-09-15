import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";

import {
  C10ReceiptError,
  canonicalJson,
  sha256,
  type ReceiptBinding,
} from "./contracts.js";
import {
  BrowserTransport,
  C10_REVIEWED_BROWSER_ENGINE,
  type BrowserTrustedEvidence,
  type CoordinatorArmGate,
  type CoordinatorArmToken,
  type InternalBrowserExecutor,
} from "./browser_transport.js";
import { type RequestCard, type TransportResponse } from "./transport.js";

const INTERNAL_PATH = "/internal/c10/browser-execute";
const TOKEN_PURPOSE = "cre-capacity-c10-browser-arm-v1";

export interface LocalBrowserFetchResponse {
  readonly ok: boolean;
  readonly status: number;
  json(): Promise<unknown>;
  text(): Promise<string>;
}

export type LocalBrowserFetch = (
  input: string,
  init: Readonly<{ method: "POST"; headers: Readonly<Record<string, string>>; body: string }>,
) => Promise<LocalBrowserFetchResponse>;

interface Grant {
  readonly token: CoordinatorArmToken;
  readonly cards: ReadonlySet<string>;
  readonly consumed: Set<string>;
}

function hmac(secret: string, purpose: string, fields: readonly string[]): string {
  return createHmac("sha256", secret)
    .update([purpose, ...fields].join("\u0000"), "utf8")
    .digest("hex");
}

function requireSecret(secret: string): string {
  if (typeof secret !== "string" || secret.length < 32 || secret.length > 4_096) {
    throw new C10ReceiptError("C10 browser arm secret is unavailable or invalid");
  }
  return secret;
}

function tokenDigest(secret: string, token: Pick<CoordinatorArmToken, "sourceKey" | "armSha256" | "tokenId">): string {
  return sha256(hmac(secret, TOKEN_PURPOSE, [token.sourceKey, token.armSha256, token.tokenId]));
}

function capabilityAuthorization(secret: string, token: CoordinatorArmToken, cardSha256: string, card: RequestCard): string {
  const input = { sourceKey: token.sourceKey, armSha256: token.armSha256, tokenId: token.tokenId, tokenSha256: token.tokenSha256, cardSha256, card };
  const capability = {
    nonce: randomUUID(), expiresAt: Date.now() + 60_000, sourceKey: token.sourceKey, armSha256: token.armSha256,
    tokenId: token.tokenId, tokenSha256: token.tokenSha256, cardSha256, manifestSha256: sha256(canonicalJson(input)),
  };
  const payload = Buffer.from(canonicalJson(capability), "utf8").toString("base64url");
  return `${payload}.${hmac(secret, "cre-capacity-c10-browser-capability-v2", [payload])}`;
}

function exactKeys(value: unknown, expected: readonly string[]): value is Record<string, unknown> {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).sort().join(",") === [...expected].sort().join(",");
}

function loopbackServiceUrl(value: string): string {
  let endpoint: URL;
  try {
    endpoint = new URL(value);
  } catch {
    throw new C10ReceiptError("C10 browser service URL is invalid");
  }
  if (
    endpoint.protocol !== "http:"
    || !["127.0.0.1", "localhost", "[::1]"].includes(endpoint.hostname)
    || endpoint.username
    || endpoint.password
    || endpoint.pathname !== "/"
    || endpoint.search
    || endpoint.hash
  ) {
    throw new C10ReceiptError("C10 browser service must use a loopback-only base URL");
  }
  return endpoint.toString().replace(/\/$/, "");
}

/**
 * Coordinator-owned HMAC capability issuer. It binds every consumable arm
 * token to the exact source, sealed arm, and constructor-time card set.
 */
export class LocalCoordinatorArmGate implements CoordinatorArmGate {
  private readonly grants = new Map<string, Grant>();
  private readonly secret: string;

  constructor(secret: string) {
    this.secret = requireSecret(secret);
  }

  issue(
    sourceKey: string,
    binding: ReceiptBinding,
    cards: ReadonlyMap<string, RequestCard>,
  ): CoordinatorArmToken {
    if (cards.size === 0) throw new C10ReceiptError("C10 coordinator cannot arm an empty card set");
    const tokenId = randomUUID();
    const unsigned = { sourceKey, armSha256: binding.armSha256, tokenId };
    const token = Object.freeze({ ...unsigned, tokenSha256: tokenDigest(this.secret, unsigned) });
    const cardDigests = new Set<string>();
    for (const [id, card] of cards) {
      if (id !== card.id || card.sourceKey !== sourceKey) {
        throw new C10ReceiptError("C10 coordinator card set is malformed");
      }
      cardDigests.add(sha256(canonicalJson(card)));
    }
    this.grants.set(tokenId, { token, cards: cardDigests, consumed: new Set() });
    return token;
  }

  async consume(token: Readonly<CoordinatorArmToken>, cardSha256: string): Promise<void> {
    const grant = this.grants.get(token.tokenId);
    if (
      !grant
      || grant.token.sourceKey !== token.sourceKey
      || grant.token.armSha256 !== token.armSha256
      || grant.token.tokenSha256 !== token.tokenSha256
      || tokenDigest(this.secret, token) !== token.tokenSha256
      || !grant.cards.has(cardSha256)
      || grant.consumed.has(cardSha256)
    ) {
      throw new C10ReceiptError("C10 coordinator arm token cannot authorize this request card");
    }
    grant.consumed.add(cardSha256);
  }

  authorizationFor(token: Readonly<CoordinatorArmToken>, cardSha256: string, card: RequestCard): string {
    const grant = this.grants.get(token.tokenId);
    if (!grant || !grant.consumed.has(cardSha256) || !grant.cards.has(cardSha256)) {
      throw new C10ReceiptError("C10 coordinator arm token was not consumed for this request card");
    }
    return capabilityAuthorization(this.secret, grant.token, cardSha256, card);
  }

  /** Coordinator-side verifier only; the capability token never contains this secret. */
  secretForSidecar(): string { return this.secret; }
}

interface SidecarResponse {
  readonly status: number;
  readonly finalUrl: string;
  readonly redirectCount: number;
  readonly elapsedMs: number;
  readonly challengeDetected: boolean;
  readonly contentType: string | null;
  readonly bodyBase64: string;
  readonly jobId: string;
  readonly pageLease: { readonly leaseId: string; readonly slot: number };
  readonly queueMs: number;
  readonly proxy: { readonly mode: string; readonly proxyId: string | null; readonly country: string | null };
  readonly engine: typeof C10_REVIEWED_BROWSER_ENGINE;
  readonly engineAttempts: 1;
  readonly fallbackDisabled: true;
  readonly fallbackUsed: false;
  readonly cacheRead: false;
  readonly cacheWrite: false;
  readonly evidenceSignature: string;
}

function parseSidecarResponse(value: unknown): SidecarResponse {
  if (!exactKeys(value, [
    "bodyBase64", "challengeDetected", "contentType", "elapsedMs", "finalUrl", "jobId", "pageLease",
    "proxy", "queueMs", "redirectCount", "status", "engine", "engineAttempts", "fallbackDisabled",
    "fallbackUsed", "cacheRead", "cacheWrite", "evidenceSignature",
  ]) || !exactKeys(value.pageLease, ["leaseId", "slot"]) || !exactKeys(value.proxy, ["country", "mode", "proxyId"])) {
    throw new C10ReceiptError("C10 browser sidecar evidence is unavailable");
  }
  const response = value as unknown as SidecarResponse;
  if (
    !Number.isInteger(response.status)
    || typeof response.finalUrl !== "string"
    || !Number.isInteger(response.redirectCount)
    || !Number.isFinite(response.elapsedMs)
    || typeof response.challengeDetected !== "boolean"
    || (response.contentType !== null && typeof response.contentType !== "string")
    || typeof response.bodyBase64 !== "string"
    || typeof response.jobId !== "string"
    || typeof response.pageLease.leaseId !== "string"
    || !Number.isInteger(response.pageLease.slot)
    || !Number.isInteger(response.queueMs)
    || typeof response.proxy.mode !== "string"
    || (response.proxy.proxyId !== null && typeof response.proxy.proxyId !== "string")
    || (response.proxy.country !== null && typeof response.proxy.country !== "string")
    || response.engine !== C10_REVIEWED_BROWSER_ENGINE
    || response.engineAttempts !== 1
    || response.fallbackDisabled !== true
    || response.fallbackUsed !== false
    || response.cacheRead !== false
    || response.cacheWrite !== false
    || typeof response.evidenceSignature !== "string"
  ) {
    throw new C10ReceiptError("C10 browser sidecar evidence is malformed");
  }
  return response;
}

/** A real loopback-only caller for the Playwright service's private C10 path. */
export class LocalPlaywrightBrowserExecutor implements InternalBrowserExecutor {
  private readonly endpoint: string;

  constructor(
    endpoint: string,
    private readonly gate: LocalCoordinatorArmGate,
    private readonly fetcher: LocalBrowserFetch = async (input, init) => globalThis.fetch(input, init),
  ) {
    this.endpoint = loopbackServiceUrl(endpoint);
  }

  async execute(instruction: Readonly<Parameters<InternalBrowserExecutor["execute"]>[0]>): Promise<TransportResponse> {
    const { card, cardSha256, armToken } = instruction;
    if (!card.browserBootstrapUrl) {
      throw new C10ReceiptError("C10 browser card requires a reviewed bootstrap URL");
    }
    const authorization = this.gate.authorizationFor(armToken, cardSha256, card);
    const payload = canonicalJson({
      sourceKey: armToken.sourceKey,
      armSha256: armToken.armSha256,
      tokenId: armToken.tokenId,
      tokenSha256: armToken.tokenSha256,
      cardSha256,
      card,
    });
    let http: LocalBrowserFetchResponse;
    try {
      http = await this.fetcher(`${this.endpoint}${INTERNAL_PATH}`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-c10-browser-authorization": authorization,
        },
        body: payload,
      });
    } catch {
      throw new C10ReceiptError("C10 browser sidecar request failed without fallback");
    }
    if (!http.ok) {
      await http.text().catch(() => "");
      throw new C10ReceiptError("C10 browser sidecar rejected the armed request without fallback");
    }
    const evidence = parseSidecarResponse(await http.json());
    const unsignedEvidence = { ...evidence } as Record<string, unknown>;
    const signature = unsignedEvidence.evidenceSignature;
    delete unsignedEvidence.evidenceSignature;
    const expectedSignature = hmac(this.gate.secretForSidecar(), "cre-capacity-c10-browser-evidence-v1", [canonicalJson(unsignedEvidence)]);
    if (typeof signature !== "string" || signature.length !== expectedSignature.length || !timingSafeEqual(Buffer.from(signature), Buffer.from(expectedSignature))) {
      throw new C10ReceiptError("C10 browser sidecar evidence authentication failed");
    }
    const body = Buffer.from(evidence.bodyBase64, "base64");
    const trustedBrowserEvidence: BrowserTrustedEvidence = Object.freeze({
      schemaVersion: 1,
      kind: "cre_capacity_c10_browser_execution_evidence",
      engine: evidence.engine,
      engineAttempts: evidence.engineAttempts,
      fallbackDisabled: evidence.fallbackDisabled,
      fallbackUsed: evidence.fallbackUsed,
      pageLease: Object.freeze({ ...evidence.pageLease }),
      apiJobId: evidence.jobId,
      coordinatorArmTokenSha256: armToken.tokenSha256,
      requestSha256: cardSha256,
      requestBodySha256: card.bodySha256,
      rawResponseSha256: sha256(body),
      cacheRead: evidence.cacheRead,
      cacheWrite: evidence.cacheWrite,
      queueMs: evidence.queueMs,
      source: Object.freeze({
        status: evidence.status,
        finalUrl: evidence.finalUrl,
        contentType: evidence.contentType,
        proxy: Object.freeze({ ...evidence.proxy }),
      }),
    });
    return Object.freeze({
      status: evidence.status,
      finalUrl: evidence.finalUrl,
      redirectCount: evidence.redirectCount,
      elapsedMs: evidence.elapsedMs,
      challengeDetected: evidence.challengeDetected,
      body,
      contentType: evidence.contentType,
      providerAttempts: 1,
      cacheMode: "no-store",
      trustedBrowserEvidence,
    });
  }
}

export function createLocalC10BrowserTransport(
  sourceKey: string,
  binding: ReceiptBinding,
  cards: ReadonlyMap<string, RequestCard>,
  options: Readonly<{ armSecret: string; serviceUrl: string; fetcher?: LocalBrowserFetch }>,
): BrowserTransport {
  const gate = new LocalCoordinatorArmGate(options.armSecret);
  const token = gate.issue(sourceKey, binding, cards);
  const executor = new LocalPlaywrightBrowserExecutor(options.serviceUrl, gate, options.fetcher);
  return new BrowserTransport(sourceKey, binding, cards, token, gate, executor);
}
