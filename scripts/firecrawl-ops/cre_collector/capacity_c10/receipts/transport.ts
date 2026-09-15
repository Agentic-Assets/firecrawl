import {
  C10ReceiptError,
  assertBinding,
  type ReceiptBinding,
  type RequestAccounting,
  type RequestAccountingEvent,
  canonicalSha256,
  sha256,
} from "./contracts.js";
import { type PrivateReceiptStore } from "./private_store.js";

export interface RequestCard {
  readonly id: string;
  readonly sourceKey: string;
  readonly method: "GET" | "POST";
  readonly url: string;
  readonly allowedHost: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly cacheMode: "no-store";
  readonly timeoutMs: number;
  readonly maxBytes: number;
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
}

export interface DirectProviderTransport {
  execute(card: Readonly<RequestCard>): Promise<TransportResponse>;
}

export interface SealedTransportEvent {
  readonly cardId: string;
  readonly status: number;
  readonly elapsedMs: number;
  readonly bytes: number;
  readonly bodySha256: string;
  readonly privateEventSha256: string;
}

function freezeCard(sourceKey: string, card: RequestCard): RequestCard {
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
  if (!Number.isInteger(card.timeoutMs) || card.timeoutMs < 1 || card.timeoutMs > 120_000) {
    throw new C10ReceiptError("request card timeout is invalid");
  }
  if (!Number.isInteger(card.maxBytes) || card.maxBytes < 1 || card.maxBytes > 2 * 1024 * 1024) {
    throw new C10ReceiptError("request card byte bound is invalid");
  }
  return Object.freeze({ ...card, headers: Object.freeze({ ...card.headers }) });
}

export function allowlistedCards(
  sourceKey: string,
  cards: readonly RequestCard[],
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
  private readonly binding: ReceiptBinding;
  private readonly cards: ReadonlyMap<string, RequestCard>;

  constructor(
    private readonly sourceKey: string,
    binding: ReceiptBinding,
    cards: ReadonlyMap<string, RequestCard>,
    private readonly store: PrivateReceiptStore,
    private readonly direct: DirectProviderTransport,
  ) {
    this.binding = assertBinding(binding);
    const privateCards = new Map<string, RequestCard>();
    for (const [id, card] of cards) {
      const frozen = freezeCard(sourceKey, card);
      if (id !== frozen.id || privateCards.has(id)) {
        throw new C10ReceiptError("request-card registry is malformed");
      }
      privateCards.set(id, frozen);
    }
    if (privateCards.size === 0) throw new C10ReceiptError("source needs an explicit request card");
    this.cards = privateCards;
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

  async oneShot(cardId: string): Promise<SealedTransportEvent> {
    const card = this.cards.get(cardId);
    if (!card || card.sourceKey !== this.sourceKey) {
      throw new C10ReceiptError("request card is not allowlisted for this source");
    }
    if (this.consumed.has(cardId)) throw new C10ReceiptError("request card was already consumed");
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
        bodySha256,
        bodyArtifactSha256: body.sha256,
      },
    });
    const event = Object.freeze({
      cardId,
      status: response.status,
      elapsedMs: response.elapsedMs,
      bytes: response.body.byteLength,
      bodySha256,
      privateEventSha256: privateEvent.sha256,
    });
    this.events.push({ ...event, outcome: "accepted" });
    return event;
  }
}
