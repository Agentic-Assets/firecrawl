/**
 * C10 Batch B source receipts.
 *
 * Foundry is the only executable producer in this wave. Its sitemap index,
 * property sitemap, and canonical WordPress detail chain is public direct GET
 * data and can be represented by sealed no-store one-shot cards. The remaining
 * Batch B sources stay descriptors only: see `BATCH_B_BLOCKED_SOURCES`.
 */
import {
  FOUNDRY_SITEMAP_URL,
  foundryPropertySitemaps,
  foundryPropertyUrls,
  foundryProviderIdentity,
} from "../../../sources/foundry-commercial.js";
import {
  C10ReceiptError,
  type C10Member,
  type ReceiptProducer,
  type ReceiptProducerContext,
  type RequestCardInput,
  type SealedTransportEvent,
  type SourceProjection,
  type SourceResponseView,
  sealStageReceipt,
  sha256,
} from "../index.js";

const DECODER = new TextDecoder();
const MAX_BYTES = 2 * 1024 * 1024;
const TIMEOUT_MS = 60_000;

export const BATCH_B_BLOCKED_SOURCES = Object.freeze({
  savills: "member fidelity depends on browser/fallback rendering and is not proved by one-shot direct HTML",
  "nai-global": "the public bulk GraphQL feed has no reviewed identity-scoped native member request",
  transwestern: "member fidelity depends on browser-rendered detail evidence",
  matthews: "member fidelity depends on browser-dependent redirect/detail interpretation",
  "daum-commercial": "the native cache-bypass snapshot requires form POST and converged repeated passes, both forbidden in C10 one-shot transport",
} as const);

export type BatchBBlockedSource = keyof typeof BATCH_B_BLOCKED_SOURCES;

function text(body: Uint8Array): string {
  return DECODER.decode(body);
}

function card(id: string, stage: "enumeration" | "member", url: string): RequestCardInput {
  return {
    id,
    sourceKey: "foundry-commercial",
    stage,
    method: "GET",
    url,
    allowedHost: new URL(url).host,
    headers: Object.freeze({ accept: stage === "member" ? "text/html,application/xhtml+xml" : "application/xml,text/xml" }),
    contentType: null,
    body: null,
    cacheMode: "no-store",
    timeoutMs: TIMEOUT_MS,
    maxBytes: MAX_BYTES,
  };
}

/** The sole fixed enumeration root. All other cards are provider-derived. */
export function foundryCommercialInitialCards(): readonly RequestCardInput[] {
  return Object.freeze([card("foundry-sitemap-index", "enumeration", FOUNDRY_SITEMAP_URL)]);
}

type FoundryState = { readonly memberCards: ReadonlyMap<string, string>; readonly memberCount: number };
const states = new WeakMap<object, FoundryState>();

function requireFoundryContext(context: ReceiptProducerContext): void {
  if (context.sourceKey !== "foundry-commercial") throw new C10ReceiptError("Foundry producer received another source context");
}

export const foundryCommercialReceiptProducer: ReceiptProducer = Object.freeze({
  async produceEnumerationReceipt(context: ReceiptProducerContext) {
    requireFoundryContext(context);
    const root = await context.transport.oneShot("foundry-sitemap-index", (view: Readonly<SourceResponseView>) => ({
      propertySitemaps: foundryPropertySitemaps(text(view.body)),
    }));
    const propertySitemaps = root.projection.propertySitemaps;
    if (!Array.isArray(propertySitemaps) || propertySitemaps.some((value) => typeof value !== "string")) {
      throw new C10ReceiptError("Foundry sitemap projection is invalid");
    }
    const sitemapEvents = [];
    for (const [index, url] of propertySitemaps.entries()) {
      const id = `foundry-sitemap-${index}`;
      await context.transport.appendFrom(root, {
        sourceKey: "foundry-commercial", stage: "enumeration", maximumCards: propertySitemaps.length + 1,
        create(parent: Readonly<SealedTransportEvent<SourceProjection>>, coordinate: { readonly id: string; readonly url: string; readonly index: number }) {
          if (!Array.isArray(parent.projection.propertySitemaps) || parent.projection.propertySitemaps[coordinate.index] !== coordinate.url) {
            throw new C10ReceiptError("Foundry sitemap request was not provider-derived");
          }
          return card(coordinate.id, "enumeration", coordinate.url);
        },
      }, { id, url, index });
      sitemapEvents.push(await context.transport.oneShot(id, (view: Readonly<SourceResponseView>) => ({ propertyUrls: foundryPropertyUrls(text(view.body)) })));
    }
    const urls = sitemapEvents.flatMap((event) => event.projection.propertyUrls);
    if (!urls.length || urls.some((url) => typeof url !== "string") || new Set(urls).size !== urls.length) {
      throw new C10ReceiptError("Foundry property sitemap identities are invalid or duplicated");
    }
    const memberCards = new Map<string, string>();
    for (const [index, url] of urls.entries()) {
      const id = `foundry-member-${sha256(url).slice(0, 24)}`;
      const parent = sitemapEvents.find((event) => Array.isArray(event.projection.propertyUrls) && event.projection.propertyUrls.includes(url));
      if (!parent || memberCards.has(id)) throw new C10ReceiptError("Foundry member graph is ambiguous");
      await context.transport.appendFrom(parent, {
        sourceKey: "foundry-commercial", stage: "member", maximumCards: urls.length,
        create(parentEvent: Readonly<SealedTransportEvent<SourceProjection>>, coordinate: { readonly id: string; readonly url: string }) {
          if (!Array.isArray(parentEvent.projection.propertyUrls) || !parentEvent.projection.propertyUrls.includes(coordinate.url)) {
            throw new C10ReceiptError("Foundry member URL was not provider-derived");
          }
          return card(coordinate.id, "member", coordinate.url);
        },
      }, { id, url });
      memberCards.set(id, url);
    }
    await context.transport.freezeMemberGraph();
    states.set(context.transport, Object.freeze({ memberCards, memberCount: urls.length }));
    return sealStageReceipt(context, "enumeration", null, { nativeMethod: "wordpress-property-sitemap", memberCardCount: urls.length });
  },

  async produceMemberReceipt(context: ReceiptProducerContext, member: C10Member) {
    requireFoundryContext(context);
    const state = states.get(context.transport);
    if (!state) throw new C10ReceiptError("Foundry member graph was not sealed");
    const url = state.memberCards.get(member.key);
    if (!url) throw new C10ReceiptError("Foundry member is not in the sealed graph");
    const event = await context.transport.oneShot(member.key, (view: Readonly<SourceResponseView>) => {
      const providerId = foundryProviderIdentity(text(view.body), view.finalUrl);
      if (!providerId) throw new C10ReceiptError("Foundry detail lacks canonical WordPress identity");
      return { providerId, canonicalUrl: view.finalUrl, requiredFields: ["canonical-url", "wordpress-shortlink-id"] };
    });
    if (event.projection.providerId !== member.providerId || event.projection.canonicalUrl !== url) {
      throw new C10ReceiptError("Foundry member receipt does not bind the cohort identity");
    }
    return sealStageReceipt(context, "member", member.key, { nativeMethod: "wordpress-canonical-shortlink", providerId: member.providerId, memberCardCount: state.memberCount });
  },
});

export const BATCH_B_RECEIPT_PRODUCERS: ReadonlyMap<string, ReceiptProducer> = new Map([
  ["foundry-commercial", foundryCommercialReceiptProducer],
]);

export function batchBReceiptProducer(sourceKey: string): ReceiptProducer | null {
  return BATCH_B_RECEIPT_PRODUCERS.get(sourceKey) ?? null;
}
