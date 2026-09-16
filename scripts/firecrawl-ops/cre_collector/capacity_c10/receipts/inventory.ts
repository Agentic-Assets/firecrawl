/**
 * Source-owned C10 inventory receipt producers. This sealed direct-provider
 * lane imports neither the normal collector nor Firecrawl/cache/write helpers.
 */
import { C10ReceiptError, canonicalJson, sha256 } from "./contracts.js";
import {
  type C10Member,
  type ReceiptProducer,
  type ReceiptProducerContext,
  requireReceiptSource,
  sealStageReceipt,
} from "./producer.js";
import {
  type RequestCardInput,
  type RequestGraphFactory,
  type SourceProjection,
  type SourceResponseView,
} from "./transport.js";

export type InventorySourceKey =
  | "cbre"
  | "cbre-dealflow"
  | "cushman-wakefield"
  | "newmark"
  | "srs"
  | "svn"
  | "lee-associates"
  | "bull-realty";

type JsonRecord = Readonly<Record<string, unknown>>;
type MemberCoordinate = Readonly<{ key: string; providerId: string; url: string }>;

interface PageProjection extends SourceProjection {
  readonly kind: "native-inventory-page";
  readonly sourceKey: InventorySourceKey;
  readonly page: number;
  readonly total: number;
  readonly rows: readonly MemberCoordinate[];
  /** Native source rows are retained only by the private sealed event. */
  readonly nativeRows: readonly JsonRecord[];
}

interface MemberProjection extends SourceProjection {
  readonly kind: "native-member";
  readonly sourceKey: InventorySourceKey;
  readonly providerId: string;
  readonly memberSha256: string;
}

export interface InventoryReceiptProducer extends ReceiptProducer {
  readonly sourceKey: InventorySourceKey;
  /** Admission remains closed pending independent source review. */
  readonly fully_verified: false;
  /** Fixed root cards; callers pass only these into allowlistedCards. */
  readonly initialCards: readonly RequestCardInput[];
}

/**
 * A source whose native protocol cannot yet be represented without weakening
 * the sealed request-card boundary.  Blocked sources are deliberately absent
 * from the executable producer map.
 */
export interface BlockedInventoryReceiptProducer {
  readonly sourceKey: InventorySourceKey;
  readonly executable: false;
  readonly reason: string;
  refuse(): never;
}

interface SourceSpec {
  readonly sourceKey: InventorySourceKey;
  readonly memberHost: string;
  readonly pageSize: number;
  readonly initialCards: readonly RequestCardInput[];
  readonly pageCard: (page: number) => RequestCardInput;
  readonly memberCard: (coordinate: MemberCoordinate) => RequestCardInput;
  readonly parsePage: (response: Readonly<SourceResponseView>, page: number) => PageProjection;
  readonly parseMember: (response: Readonly<SourceResponseView>, member: C10Member) => MemberProjection;
}

const decoder = new TextDecoder();
const MAX_PAGES = 1_200;
const MAX_MEMBERS = 50_000;
const JSON_HEADERS = Object.freeze({ accept: "application/json", "cache-control": "no-store" });
const HTML_HEADERS = Object.freeze({ accept: "text/html,application/json", "cache-control": "no-store" });

function asRecord(value: unknown, label: string): JsonRecord {
  if (!value || Array.isArray(value) || typeof value !== "object") throw new C10ReceiptError(`${label} must be an object`);
  return value as JsonRecord;
}

function decodeJson(response: Readonly<SourceResponseView>, label: string): JsonRecord {
  try {
    return asRecord(JSON.parse(decoder.decode(response.body)), label);
  } catch (error) {
    if (error instanceof C10ReceiptError) throw error;
    throw new C10ReceiptError(`${label} is not JSON`);
  }
}

function requiredInteger(value: unknown, label: string): number {
  if (!Number.isInteger(value) || (value as number) < 0) throw new C10ReceiptError(`${label} must be a nonnegative integer`);
  return value as number;
}

function nonemptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim() === "") throw new C10ReceiptError(`${label} must be a nonempty string`);
  return value.trim();
}

function requireRows(record: JsonRecord, field: string, label: string): readonly JsonRecord[] {
  const rows = record[field];
  if (!Array.isArray(rows) || rows.some((row) => !row || Array.isArray(row) || typeof row !== "object")) {
    throw new C10ReceiptError(`${label} lacks a native ${field} row array`);
  }
  return rows as readonly JsonRecord[];
}

function safeCardPart(value: string, label: string): string {
  const normalized = value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  if (!normalized || normalized.length > 60) throw new C10ReceiptError(`${label} cannot form a request-card id`);
  return normalized;
}

function getCard(
  sourceKey: InventorySourceKey,
  id: string,
  stage: "enumeration" | "member",
  url: string,
  allowedHost: string,
  headers: Readonly<Record<string, string>> = JSON_HEADERS,
): RequestCardInput {
  return Object.freeze({ id, sourceKey, stage, method: "GET", url, allowedHost, headers, contentType: null, body: null, cacheMode: "no-store", timeoutMs: 120_000, maxBytes: 2 * 1024 * 1024 });
}

function postCard(
  sourceKey: InventorySourceKey,
  id: string,
  stage: "enumeration" | "member",
  url: string,
  allowedHost: string,
  body: unknown,
): RequestCardInput {
  return Object.freeze({
    id, sourceKey, stage, method: "POST", url, allowedHost,
    headers: Object.freeze({ ...JSON_HEADERS, "content-type": "application/json" }),
    contentType: "application/json", body: canonicalJson(body), cacheMode: "no-store", timeoutMs: 120_000, maxBytes: 2 * 1024 * 1024,
  });
}

function absoluteUrl(value: unknown, base: string, allowedHost: string, label: string): string {
  let url: URL;
  try { url = new URL(nonemptyString(value, label), base); } catch { throw new C10ReceiptError(`${label} is not an HTTPS URL`); }
  if (url.protocol !== "https:" || url.host !== allowedHost || url.username || url.password || url.hash) {
    throw new C10ReceiptError(`${label} is not an allowlisted HTTPS URL`);
  }
  return url.toString();
}

function pageProjection(
  sourceKey: InventorySourceKey, page: number, total: number, nativeRows: readonly JsonRecord[], coordinates: readonly MemberCoordinate[],
): PageProjection {
  if (nativeRows.length !== coordinates.length) throw new C10ReceiptError(`${sourceKey} projection rows drifted`);
  const identities = new Set<string>();
  for (const row of coordinates) {
    if (identities.has(row.providerId)) throw new C10ReceiptError(`${sourceKey} page has duplicate provider identity`);
    identities.add(row.providerId);
  }
  return { kind: "native-inventory-page", sourceKey, page, total, rows: coordinates, nativeRows };
}

function expectPage(projection: PageProjection, expectedTotal: number, pageSize: number): void {
  if (projection.total !== expectedTotal) throw new C10ReceiptError(`${projection.sourceKey} population changed during enumeration`);
  const expectedRows = Math.max(0, Math.min(pageSize, expectedTotal - projection.page * pageSize));
  if (projection.nativeRows.length !== expectedRows) throw new C10ReceiptError(`${projection.sourceKey} page ${projection.page} has ${projection.nativeRows.length}/${expectedRows} native rows`);
}

function buildoutProjection(sourceKey: "svn" | "lee-associates" | "bull-realty", memberHost: string, page: number, response: Readonly<SourceResponseView>): PageProjection {
  const record = decodeJson(response, `${sourceKey} Buildout response`);
  const total = requiredInteger(asRecord(record.meta, `${sourceKey} Buildout meta`).total, `${sourceKey} Buildout meta.total`);
  const rows = requireRows(record, "inventory", `${sourceKey} Buildout response`);
  const coordinates = rows.map((row) => {
    const providerId = nonemptyString(row.id, `${sourceKey} Buildout id`);
    const url = absoluteUrl(row.show_link, `https://${memberHost}/`, memberHost, `${sourceKey} Buildout show_link`);
    const propertyIds = new URL(url).searchParams.getAll("propertyId");
    if (propertyIds.length !== 1 || !propertyIds[0]?.trim()) {
      throw new C10ReceiptError(`${sourceKey} Buildout show_link lacks propertyId`);
    }
    return { key: `member-${safeCardPart(providerId, `${sourceKey} Buildout id`)}`, providerId, url };
  });
  return pageProjection(sourceKey, page, total, rows, coordinates);
}

function cbreProjection(response: Readonly<SourceResponseView>, page: number): PageProjection {
  const record = decodeJson(response, "CBRE listings response");
  const total = requiredInteger(record.DocumentCount, "CBRE DocumentCount");
  const rows = requireRows(record, "Documents", "CBRE listings response");
  const coordinates = rows.map((row) => {
    const providerId = nonemptyString(row["Common.PrimaryKey"], "CBRE Common.PrimaryKey");
    const url = absoluteUrl(row.canonicalUrl ?? row.url ?? `https://www.cbre.com/properties/properties-for-lease/commercial-space/details/${encodeURIComponent(providerId)}/`, "https://www.cbre.com/", "www.cbre.com", "CBRE canonical URL");
    return { key: `member-${safeCardPart(providerId, "CBRE Common.PrimaryKey")}`, providerId, url };
  });
  return pageProjection("cbre", page, total, rows, coordinates);
}

function cushmanProjection(response: Readonly<SourceResponseView>, page: number): PageProjection {
  const record = decodeJson(response, "Cushman inventory response");
  const total = requiredInteger(record.total_item, "Cushman total_item");
  const rows = requireRows(record, "content", "Cushman inventory response");
  const coordinates = rows.map((row) => {
    const providerId = nonemptyString(row.id, "Cushman provider id");
    const url = absoluteUrl(row.url ?? row.relative_url, "https://www.cushmanwakefield.com/", "www.cushmanwakefield.com", "Cushman canonical URL");
    return { key: `member-${safeCardPart(providerId, "Cushman provider id")}`, providerId, url };
  });
  return pageProjection("cushman-wakefield", page, total, rows, coordinates);
}

function newmarkProjection(response: Readonly<SourceResponseView>, page: number): PageProjection {
  const record = decodeJson(response, "Newmark NIM response");
  const total = requiredInteger(record.total, "Newmark total");
  const rows = requireRows(record, "data", "Newmark NIM response");
  const coordinates = rows.map((row) => {
    const providerId = nonemptyString(row.id, "Newmark id");
    const slug = nonemptyString(row.slug, "Newmark slug");
    const url = absoluteUrl(`https://www.nmrk.com/properties/${encodeURIComponent(slug)}`, "https://www.nmrk.com/", "www.nmrk.com", "Newmark canonical URL");
    return { key: `member-${safeCardPart(providerId, "Newmark id")}`, providerId, url };
  });
  return pageProjection("newmark", page, total, rows, coordinates);
}

function srsProjection(response: Readonly<SourceResponseView>, page: number): PageProjection {
  const record = decodeJson(response, "SRS inventory response");
  const total = requiredInteger(record.total, "SRS total");
  const rows = requireRows(record, "properties", "SRS inventory response");
  const coordinates = rows.map((row) => {
    const providerId = nonemptyString(asRecord(row.apto_data, "SRS apto_data").SRS_Listings_ID__c, "SRS listing id");
    const url = absoluteUrl(row.permalink, "https://www.srsre.com/", "www.srsre.com", "SRS canonical URL");
    return { key: `member-${safeCardPart(providerId, "SRS listing id")}`, providerId, url };
  });
  return pageProjection("srs", page, total, rows, coordinates);
}

function memberProjection(sourceKey: InventorySourceKey, member: C10Member, response: Readonly<SourceResponseView>): MemberProjection {
  const json = /(?:^|[+\/])json(?:;|$)/i.test(response.contentType ?? "") ? decodeJson(response, `${sourceKey} member response`) : null;
  return { kind: "native-member", sourceKey, providerId: member.providerId, memberSha256: sha256(response.body), ...(json ? { native: json } : { nativeHtmlSha256: sha256(response.body) }) };
}

function memberCardFor(spec: Pick<SourceSpec, "sourceKey" | "memberHost">, coordinate: MemberCoordinate): RequestCardInput {
  return getCard(spec.sourceKey, coordinate.key, "member", coordinate.url, spec.memberHost, HTML_HEADERS);
}

function makeProducer(spec: SourceSpec): InventoryReceiptProducer {
  const enumerationFactory: RequestGraphFactory<number> = {
    sourceKey: spec.sourceKey, stage: "enumeration", maximumCards: MAX_PAGES + 1,
    create(parent, page) {
      const projection = parent.projection as PageProjection;
      if (projection.kind !== "native-inventory-page" || projection.sourceKey !== spec.sourceKey || !Number.isInteger(page) || page < 1) throw new C10ReceiptError(`${spec.sourceKey} refused a non-native page coordinate`);
      return spec.pageCard(page);
    },
  };
  const memberFactory: RequestGraphFactory<MemberCoordinate> = {
    sourceKey: spec.sourceKey, stage: "member", maximumCards: MAX_MEMBERS,
    create(parent, coordinate) {
      const projection = parent.projection as PageProjection;
      if (projection.kind !== "native-inventory-page" || projection.sourceKey !== spec.sourceKey || !projection.rows.some((candidate) => candidate.key === coordinate.key && candidate.providerId === coordinate.providerId && candidate.url === coordinate.url)) {
        throw new C10ReceiptError(`${spec.sourceKey} refused an arbitrary member coordinate`);
      }
      return spec.memberCard(coordinate);
    },
  };
  return Object.freeze({
    sourceKey: spec.sourceKey, fully_verified: false, initialCards: spec.initialCards,
    async produceEnumerationReceipt(context: ReceiptProducerContext) {
      const transport = requireReceiptSource(context, spec.sourceKey);
      transport.assertInitialCards(spec.initialCards);
      const first = await transport.oneShot("enumeration-0", (response) => spec.parsePage(response, 0));
      const firstPage = first.projection as PageProjection;
      const total = firstPage.total;
      const pageCount = Math.ceil(total / spec.pageSize);
      if (total === 0 || pageCount > MAX_PAGES) throw new C10ReceiptError(`${spec.sourceKey} population cannot form a bounded member graph`);
      expectPage(firstPage, total, spec.pageSize);
      const pages: Array<typeof first> = [first];
      for (let page = 1; page < pageCount; page++) {
        await transport.appendFrom(first, enumerationFactory, page);
        const event = await transport.oneShot(`enumeration-${page}`, (response) => spec.parsePage(response, page));
        expectPage(event.projection as PageProjection, total, spec.pageSize);
        pages.push(event);
      }
      const members: Array<{ coordinate: MemberCoordinate; pageEvent: typeof first }> = [];
      const identities = new Set<string>();
      for (const event of pages) for (const coordinate of (event.projection as PageProjection).rows) {
        if (identities.has(coordinate.providerId)) throw new C10ReceiptError(`${spec.sourceKey} population has duplicate provider identity`);
        identities.add(coordinate.providerId);
        members.push({ coordinate, pageEvent: event });
      }
      if (members.length > MAX_MEMBERS || (spec.sourceKey !== "cbre-dealflow" && members.length !== total)) throw new C10ReceiptError(`${spec.sourceKey} member population is incomplete`);
      if (members.length === 0) throw new C10ReceiptError(`${spec.sourceKey} has no comparable native members`);
      for (const member of members) await transport.appendFrom(member.pageEvent, memberFactory, member.coordinate);
      const frozenGraph = await transport.freezeMemberGraph();
      return sealStageReceipt(context, "enumeration", null, {
        sourceKey: spec.sourceKey, total, pageCount, memberCardCount: frozenGraph.memberCardCount,
        pageEvents: pages.map((event) => ({ cardId: event.cardId, projectionSha256: event.projectionSha256, privateEventSha256: event.privateEventSha256 })),
        memberGraphArtifactSha256: frozenGraph.graphArtifactSha256,
      });
    },
    async produceMemberReceipt(context: ReceiptProducerContext, member: C10Member) {
      requireReceiptSource(context, spec.sourceKey);
      const cardId = `member-${safeCardPart(member.providerId, "member provider id")}`;
      if (member.key !== cardId) throw new C10ReceiptError("member key does not bind its native provider identity");
      const event = await context.transport.oneShot(cardId, (response) => spec.parseMember(response, member));
      return sealStageReceipt(context, "member", member.key, { sourceKey: spec.sourceKey, providerId: member.providerId, projectionSha256: event.projectionSha256, privateEventSha256: event.privateEventSha256 });
    },
  });
}

const cbreSpec: SourceSpec = {
  sourceKey: "cbre", memberHost: "www.cbre.com", pageSize: 500,
  initialCards: [getCard("cbre", "enumeration-0", "enumeration", "https://www.cbre.com/listings-api/propertylistings/query?site=us-comm&Common.Aspects=isSale&PageSize=500&Page=1", "www.cbre.com")],
  pageCard: (page) => getCard("cbre", `enumeration-${page}`, "enumeration", `https://www.cbre.com/listings-api/propertylistings/query?site=us-comm&Common.Aspects=isSale&PageSize=500&Page=${page + 1}`, "www.cbre.com"),
  memberCard: (coordinate) => memberCardFor(cbreSpec, coordinate), parsePage: cbreProjection, parseMember: (response, member) => memberProjection("cbre", member, response),
};
const cushmanSpec: SourceSpec = {
  sourceKey: "cushman-wakefield", memberHost: "www.cushmanwakefield.com", pageSize: 100,
  initialCards: [getCard("cushman-wakefield", "enumeration-0", "enumeration", "https://www.cushmanwakefield.com/api/properties/search?rfkId=property_search&view=pins&site_country=US&listing_type=Buy&language=en&limit=100&offset=0", "www.cushmanwakefield.com")],
  pageCard: (page) => getCard("cushman-wakefield", `enumeration-${page}`, "enumeration", `https://www.cushmanwakefield.com/api/properties/search?rfkId=property_search&view=pins&site_country=US&listing_type=Buy&language=en&limit=100&offset=${page * 100}`, "www.cushmanwakefield.com"),
  memberCard: (coordinate) => memberCardFor(cushmanSpec, coordinate), parsePage: cushmanProjection, parseMember: (response, member) => memberProjection("cushman-wakefield", member, response),
};
function newmarkBody(page: number): JsonRecord {
  return { brokers: [], excludeUnpriced: false, isAscending: true, leaseTypes: [], listingIds: [], page, propertySubtypes: [], propertyTypes: [], sortBy: "createdOn", statuses: [], take: 100, type: 2 };
}
const newmarkSpec: SourceSpec = {
  sourceKey: "newmark", memberHost: "www.nmrk.com", pageSize: 100,
  initialCards: [postCard("newmark", "enumeration-0", "enumeration", "https://api-public.nim.nmrk.com/api/properties/search", "api-public.nim.nmrk.com", newmarkBody(0))],
  pageCard: (page) => postCard("newmark", `enumeration-${page}`, "enumeration", "https://api-public.nim.nmrk.com/api/properties/search", "api-public.nim.nmrk.com", newmarkBody(page)),
  memberCard: (coordinate) => memberCardFor(newmarkSpec, coordinate), parsePage: newmarkProjection, parseMember: (response, member) => memberProjection("newmark", member, response),
};
function srsBody(page: number): JsonRecord {
  return { client_ip: "", query: { address: null, availabilityType: ["sale", "lease", "investment-sale"], broker: "", capRateRange: { required: true }, latLong: null, lotSizeRange: { required: false }, office: "", offset: page * 12, orderBy: "date", orderDirection: "DESC", ownershipType: ["fee-simple-land-building", "ground-lease-land-only", "leasehold-lease-only", "other"], pageSize: 12, portfolio: [], priceRange: { required: true }, propertyType: ["retail", "industrial", "office", "land", "multifamily", "hospitality", "healthcare", "special_purpose"], searchTerms: "", sizeRange: { required: false }, subType: null, tenancyType: ["single-tenant", "multi-tenant", "land"], tenant: "" } };
}
const srsSpec: SourceSpec = {
  sourceKey: "srs", memberHost: "www.srsre.com", pageSize: 12,
  initialCards: [postCard("srs", "enumeration-0", "enumeration", "https://srsre-next-412955565034.us-central1.run.app/api/property-search", "srsre-next-412955565034.us-central1.run.app", srsBody(0))],
  pageCard: (page) => postCard("srs", `enumeration-${page}`, "enumeration", "https://srsre-next-412955565034.us-central1.run.app/api/property-search", "srsre-next-412955565034.us-central1.run.app", srsBody(page)),
  memberCard: (coordinate) => memberCardFor(srsSpec, coordinate), parsePage: srsProjection, parseMember: (response, member) => memberProjection("srs", member, response),
};
function buildoutSpec(sourceKey: "svn" | "lee-associates" | "bull-realty", pluginKey: string, memberHost: string): SourceSpec {
  const url = (page: number) => `https://buildout.com/plugins/${pluginKey}/inventory.json?page=${page}&q%5Bs%5D=created_at%20asc%2C%20id%20asc`;
  const spec: SourceSpec = {
    sourceKey, memberHost, pageSize: 30,
    initialCards: [getCard(sourceKey, "enumeration-0", "enumeration", url(0), "buildout.com")],
    pageCard: (page) => getCard(sourceKey, `enumeration-${page}`, "enumeration", url(page), "buildout.com"),
    memberCard: (coordinate) => memberCardFor(spec, coordinate), parsePage: (response, page) => buildoutProjection(sourceKey, memberHost, page, response), parseMember: (response, member) => memberProjection(sourceKey, member, response),
  };
  return spec;
}
export const cbreDealflowReceiptProducerBlock: BlockedInventoryReceiptProducer = Object.freeze({
  sourceKey: "cbre-dealflow",
  executable: false,
  reason: "blocked: ListingEngine requires a provider-derived engine key and form-urlencoded POST response html; the sealed card contract does not represent that protocol",
  refuse(): never {
    throw new C10ReceiptError(this.reason);
  },
});

export const cbreReceiptProducer = makeProducer(cbreSpec);
export const cushmanWakefieldReceiptProducer = makeProducer(cushmanSpec);
export const newmarkReceiptProducer = makeProducer(newmarkSpec);
export const srsReceiptProducer = makeProducer(srsSpec);
export const svnReceiptProducer = makeProducer(buildoutSpec("svn", "b933480474026c41d248b77156c84aef37dcac68", "svn.com"));
export const leeAssociatesReceiptProducer = makeProducer(buildoutSpec("lee-associates", "9a64a93980aeae8db347e72cdfa8ca61017acc9a", "www.lee-associates.com"));
export const bullRealtyReceiptProducer = makeProducer(buildoutSpec("bull-realty", "6e2064ba71e11d85d50740c87a9372ef9c961a46", "www.bullrealty.com"));
export const inventoryReceiptProducers: ReadonlyMap<InventorySourceKey, InventoryReceiptProducer> = new Map([
  ["cbre", cbreReceiptProducer], ["cushman-wakefield", cushmanWakefieldReceiptProducer], ["newmark", newmarkReceiptProducer],
  ["srs", srsReceiptProducer], ["svn", svnReceiptProducer], ["lee-associates", leeAssociatesReceiptProducer], ["bull-realty", bullRealtyReceiptProducer],
]);

export const blockedInventoryReceiptProducers: ReadonlyMap<InventorySourceKey, BlockedInventoryReceiptProducer> = new Map([
  ["cbre-dealflow", cbreDealflowReceiptProducerBlock],
]);
