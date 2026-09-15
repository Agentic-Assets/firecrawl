import {
  C10ReceiptError,
  assertBinding,
  type ReceiptBinding,
  type RequestAccounting,
  type RequestAccountingEvent,
  canonicalJson,
  canonicalSha256,
  sha256,
} from "./contracts.js";
import { type ReceiptArtifactStore } from "./private_store.js";
import { type BrowserTrustedEvidence } from "./browser_transport.js";

export interface RequestCardInput {
  readonly id: string;
  readonly sourceKey: string;
  readonly stage: "enumeration" | "member";
  readonly method: "GET" | "POST";
  readonly url: string;
  readonly allowedHost: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly contentType: "application/json" | null;
  readonly body: string | null;
  /** Required by the C10 browser executor for same-origin browser context setup. */
  readonly browserBootstrapUrl?: string;
  readonly cacheMode: "no-store";
  readonly timeoutMs: number;
  readonly maxBytes: number;
}

export interface RequestCard extends RequestCardInput {
  readonly bodySha256: string | null;
}

export interface TransportResponse {
  readonly status: number;
  readonly finalUrl: string;
  readonly redirectCount: number;
  readonly elapsedMs: number;
  readonly challengeDetected: boolean;
  readonly body: Uint8Array;
  readonly contentType: string | null;
  /** The source transport must report exactly one direct provider attempt. */
  readonly providerAttempts: number;
  readonly cacheMode: "no-store";
  /** Present only for the internal C10 browser executor and sealed privately. */
  readonly trustedBrowserEvidence?: BrowserTrustedEvidence;
}

export interface DirectProviderTransport {
  execute(card: Readonly<RequestCard>): Promise<TransportResponse>;
}

export interface SourceResponseView {
  readonly requestedUrl: string;
  readonly finalUrl: string;
  readonly status: number;
  readonly contentType: string | null;
  /** Ephemeral parser input. It is zeroed after the callback settles. */
  readonly body: Uint8Array;
}

export type SourceProjection = Readonly<Record<string, unknown>>;
export type SourceResponseProjector<T extends SourceProjection> = (
  response: Readonly<SourceResponseView>,
) => T | Promise<T>;

export interface SealedTransportEvent<T extends SourceProjection> {
  readonly sourceKey: string;
  readonly bindingSha256: string;
  readonly cardId: string;
  readonly status: number;
  readonly elapsedMs: number;
  readonly bytes: number;
  readonly bodySha256: string;
  readonly privateEventSha256: string;
  readonly projectionSha256: string;
  /** Canonical, deep-frozen source projection. It contains no response body. */
  readonly projection: T;
}

export interface RequestGraphFactory<C> {
  readonly sourceKey: string;
  readonly stage: "enumeration" | "member";
  readonly maximumCards: number;
  create(parent: Readonly<SealedTransportEvent<SourceProjection>>, coordinate: C): RequestCardInput;
}

export interface SealedGraphExpansion {
  readonly sourceKey: string;
  readonly stage: "enumeration" | "member";
  readonly cardId: string;
  readonly parentPrivateEventSha256: string;
  readonly parentProjectionSha256: string;
  readonly graphArtifactSha256: string;
}

export interface FrozenMemberGraph {
  readonly sourceKey: string;
  readonly bindingSha256: string;
  readonly memberCardCount: number;
  readonly graphArtifactSha256: string;
}

function deepFreeze<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.freeze(value);
    for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child);
  }
  return value;
}

function canonicalProjection<T extends SourceProjection>(value: T): T {
  if (!value || Array.isArray(value) || typeof value !== "object") {
    throw new C10ReceiptError("source projection must be an object");
  }
  return deepFreeze(JSON.parse(canonicalJson(value)) as T);
}

function freezeCard(sourceKey: string, card: RequestCardInput): RequestCard {
  if (!/^[a-z0-9][a-z0-9-]{0,80}$/.test(card.id) || card.sourceKey !== sourceKey) {
    throw new C10ReceiptError("request card identity is invalid");
  }
  const url = new URL(card.url);
  if (url.protocol !== "https:" || url.host !== card.allowedHost) {
    throw new C10ReceiptError("request card host is not allowlisted");
  }
  if (card.cacheMode !== "no-store") {
    throw new C10ReceiptError("request card must disable cache use");
  }
  if (card.browserBootstrapUrl !== undefined) {
    const bootstrap = new URL(card.browserBootstrapUrl);
    if (
      bootstrap.protocol !== "https:"
      || bootstrap.host !== card.allowedHost
      || bootstrap.origin !== url.origin
      || bootstrap.hash
    ) {
      throw new C10ReceiptError("browser bootstrap URL is not allowlisted for this request card");
    }
  }
  const body = card.body;
  if (card.method === "GET" && (body !== null || card.contentType !== null)) {
    throw new C10ReceiptError("GET request cards cannot have a body");
  }
  if (card.method === "POST") {
    if (!body || body.length > 64 * 1024 || card.contentType !== "application/json") {
      throw new C10ReceiptError("POST request card body is invalid");
    }
    let canonicalBody: string;
    try {
      canonicalBody = canonicalJson(JSON.parse(body));
    } catch {
      throw new C10ReceiptError("POST request body must be canonical JSON");
    }
    if (body !== canonicalBody) throw new C10ReceiptError("POST request body is not canonical");
  }
  if (!Number.isInteger(card.timeoutMs) || card.timeoutMs < 1 || card.timeoutMs > 120_000) {
    throw new C10ReceiptError("request card timeout is invalid");
  }
  if (!Number.isInteger(card.maxBytes) || card.maxBytes < 1 || card.maxBytes > 2 * 1024 * 1024) {
    throw new C10ReceiptError("request card byte bound is invalid");
  }
  return Object.freeze({
    ...card,
    headers: Object.freeze({ ...card.headers }),
    bodySha256: body === null ? null : sha256(body),
  });
}

export function allowlistedCards(
  sourceKey: string,
  cards: readonly RequestCardInput[],
): ReadonlyMap<string, RequestCard> {
  if (cards.length === 0) throw new C10ReceiptError("source needs an explicit request card");
  const result = new Map<string, RequestCard>();
  for (const card of cards) {
    const frozen = freezeCard(sourceKey, card);
    if (result.has(frozen.id)) throw new C10ReceiptError("request card ids must be unique");
    result.set(frozen.id, frozen);
  }
  return result;
}

/** Source-bound request execution with a one-attempt terminal result per card. */
export class SourceBoundOneShotTransport {
  private readonly consumed = new Set<string>();
  private readonly events: RequestAccountingEvent[] = [];
  readonly binding: ReceiptBinding;
  private readonly bindingSha256: string;
  private readonly cards = new Map<string, RequestCard>();
  private readonly accepted = new Map<string, SealedTransportEvent<SourceProjection>>();
  private readonly expansions: SealedGraphExpansion[] = [];
  private memberGraphFrozen = false;

  constructor(
    readonly sourceKey: string,
    binding: ReceiptBinding,
    cards: ReadonlyMap<string, RequestCard>,
    readonly store: ReceiptArtifactStore,
    private readonly direct: DirectProviderTransport,
  ) {
    this.binding = assertBinding(binding);
    this.bindingSha256 = canonicalSha256(this.binding);
    for (const [id, card] of cards) {
      const frozen = freezeCard(sourceKey, card);
      if (id !== frozen.id || this.cards.has(id) || frozen.stage !== "enumeration") {
        throw new C10ReceiptError("request-card registry is malformed");
      }
      this.cards.set(id, frozen);
    }
    if (this.cards.size === 0) throw new C10ReceiptError("source needs an explicit request card");
  }

  async appendFrom<C>(
    parent: Readonly<SealedTransportEvent<SourceProjection>>,
    factory: RequestGraphFactory<C>,
    coordinate: C,
  ): Promise<SealedGraphExpansion> {
    if (factory.sourceKey !== this.sourceKey || parent.sourceKey !== this.sourceKey || parent.bindingSha256 !== this.bindingSha256) {
      throw new C10ReceiptError("request-graph parent is bound to another source or plan");
    }
    if (this.accepted.get(parent.privateEventSha256) !== parent) {
      throw new C10ReceiptError("request-graph parent event was not sealed by this transport");
    }
    if (factory.stage === "enumeration" && this.memberGraphFrozen) {
      throw new C10ReceiptError("cannot expand enumeration after member graph freeze");
    }
    if (factory.stage === "member" && this.memberGraphFrozen) {
      throw new C10ReceiptError("cannot expand member graph after freeze");
    }
    if (!Number.isInteger(factory.maximumCards) || factory.maximumCards < 1) {
      throw new C10ReceiptError("request-graph card cap is invalid");
    }
    const count = [...this.cards.values()].filter((card) => card.stage === factory.stage).length;
    if (count >= factory.maximumCards) throw new C10ReceiptError("request-graph card cap exceeded");
    const card = freezeCard(this.sourceKey, factory.create(parent, coordinate));
    if (card.stage !== factory.stage || this.cards.has(card.id)) {
      throw new C10ReceiptError("request-graph expansion is duplicated or has the wrong stage");
    }
    const sealed = await this.store.sealJson(`graph-${this.sourceKey}-${card.id}`, {
      binding: this.binding,
      parent: {
        privateEventSha256: parent.privateEventSha256,
        projectionSha256: parent.projectionSha256,
      },
      card,
    });
    this.cards.set(card.id, card);
    const expansion = Object.freeze({
      sourceKey: this.sourceKey,
      stage: card.stage,
      cardId: card.id,
      parentPrivateEventSha256: parent.privateEventSha256,
      parentProjectionSha256: parent.projectionSha256,
      graphArtifactSha256: sealed.sha256,
    });
    this.expansions.push(expansion);
    return expansion;
  }

  async freezeMemberGraph(): Promise<FrozenMemberGraph> {
    if (this.memberGraphFrozen) throw new C10ReceiptError("member request graph is already frozen");
    const memberCardCount = [...this.cards.values()].filter((card) => card.stage === "member").length;
    if (memberCardCount === 0) throw new C10ReceiptError("member request graph cannot be empty");
    const sealed = await this.store.sealJson(`graph-${this.sourceKey}-frozen`, {
      binding: this.binding,
      cards: [...this.cards.values()],
      expansions: this.expansions,
    });
    this.memberGraphFrozen = true;
    return Object.freeze({
      sourceKey: this.sourceKey,
      bindingSha256: this.bindingSha256,
      memberCardCount,
      graphArtifactSha256: sealed.sha256,
    });
  }

  requestAccounting(): RequestAccounting {
    const events = Object.freeze(this.events.map((event) => Object.freeze({ ...event })));
    return Object.freeze({
      logicalRequests: events.length,
      attempts: events.length,
      retries: 0,
      eventsSha256: canonicalSha256(events),
      events,
    });
  }

  async oneShot<T extends SourceProjection>(
    cardId: string,
    project: SourceResponseProjector<T>,
  ): Promise<SealedTransportEvent<T>> {
    const card = this.cards.get(cardId);
    if (!card || card.sourceKey !== this.sourceKey) {
      throw new C10ReceiptError("request card is not allowlisted for this source");
    }
    if (this.consumed.has(cardId)) throw new C10ReceiptError("request card was already consumed");
    if (card.stage === "enumeration" && this.memberGraphFrozen) {
      throw new C10ReceiptError("enumeration request graph is frozen");
    }
    if (card.stage === "member" && !this.memberGraphFrozen) {
      throw new C10ReceiptError("member request graph must be frozen before execution");
    }
    this.consumed.add(cardId);
    let response: TransportResponse;
    try {
      response = await this.direct.execute(card);
    } catch {
      this.events.push({
        cardId,
        outcome: "transport_error",
        status: null,
        elapsedMs: null,
        bytes: null,
        bodySha256: null,
        privateEventSha256: null,
      });
      throw new C10ReceiptError("source-bound request failed without retry");
    }
    const responseShapeIsInvalid =
      !Number.isInteger(response.status) ||
      !Number.isInteger(response.redirectCount) ||
      !Number.isFinite(response.elapsedMs) ||
      typeof response.finalUrl !== "string" ||
      typeof response.challengeDetected !== "boolean" ||
      !(response.body instanceof Uint8Array) ||
      typeof response.contentType !== "string" && response.contentType !== null ||
      !Number.isInteger(response.providerAttempts);
    const bodySha256 = responseShapeIsInvalid ? null : sha256(response.body);
    const rejected =
      responseShapeIsInvalid ||
      response.status < 200 ||
      response.status > 299 ||
      response.redirectCount !== 0 ||
      response.finalUrl !== card.url ||
      response.challengeDetected ||
      response.providerAttempts !== 1 ||
      response.cacheMode !== "no-store" ||
      response.elapsedMs < 0 ||
      response.elapsedMs > card.timeoutMs ||
      response.body.byteLength > card.maxBytes;
    if (rejected) {
      this.events.push({
        cardId,
        outcome: "rejected",
        status: response.status,
        elapsedMs: response.elapsedMs,
        bytes: response.body.byteLength,
        bodySha256,
        privateEventSha256: null,
      });
      throw new C10ReceiptError("source response violates its one-shot request card");
    }
    if (bodySha256 === null) throw new C10ReceiptError("source response body is invalid");
    let projection: T;
    try {
      const parserBody = new Uint8Array(response.body);
      try {
        projection = canonicalProjection(
          await project({
            requestedUrl: card.url,
            finalUrl: response.finalUrl,
            status: response.status,
            contentType: response.contentType,
            body: parserBody,
          }),
        );
      } finally {
        parserBody.fill(0);
      }
    } catch {
      this.events.push({
        cardId,
        outcome: "rejected",
        status: response.status,
        elapsedMs: response.elapsedMs,
        bytes: response.body.byteLength,
        bodySha256,
        privateEventSha256: null,
      });
      throw new C10ReceiptError("source response projection failed without retry");
    }
    const projectionSha256 = canonicalSha256(projection);
    const body = await this.store.sealBytes(`body-${this.sourceKey}-${cardId}`, response.body);
    const privateEvent = await this.store.sealJson(`event-${this.sourceKey}-${cardId}`, {
      binding: this.binding,
      card,
      response: {
        status: response.status,
        finalUrl: response.finalUrl,
        redirectCount: response.redirectCount,
        elapsedMs: response.elapsedMs,
        challengeDetected: response.challengeDetected,
        providerAttempts: response.providerAttempts,
        cacheMode: response.cacheMode,
        contentType: response.contentType,
        trustedBrowserEvidence: response.trustedBrowserEvidence ?? null,
        bodySha256,
        bodyArtifactSha256: body.sha256,
        projection,
        projectionSha256,
      },
    });
    const event = Object.freeze({
      sourceKey: this.sourceKey,
      bindingSha256: this.bindingSha256,
      cardId,
      status: response.status,
      elapsedMs: response.elapsedMs,
      bytes: response.body.byteLength,
      bodySha256,
      privateEventSha256: privateEvent.sha256,
      projectionSha256,
      projection,
    });
    this.events.push({
      cardId: event.cardId,
      outcome: "accepted",
      status: event.status,
      elapsedMs: event.elapsedMs,
      bytes: event.bytes,
      bodySha256: event.bodySha256,
      privateEventSha256: event.privateEventSha256,
    });
    this.accepted.set(event.privateEventSha256, event);
    return event;
  }
}
