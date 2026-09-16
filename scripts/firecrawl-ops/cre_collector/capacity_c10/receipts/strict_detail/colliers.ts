/** Colliers SalesTracker RCM list/map/SLP evidence without collector helpers. */
import {
  C10ReceiptError,
} from "../contracts.js";
import type { RequestCardInput, SealedTransportEvent, SourceProjection } from "../transport.js";
import type { C10Member } from "../producer.js";
import {
  COLLIERS_PAGE_SIZE,
  COLLIERS_RCM_BASE,
  colliersHeaders,
  colliersListUrl,
  colliersMapUrl,
  colliersSlpInitUrl,
  colliersAssertDetailProjectId,
  groupColliersMapLocations,
  parseColliersReceiptCards,
} from "../../../sources/pure/colliers-receipt.js";
import {
  StrictDetailReceiptProducer,
  immutableStrictDetailPlan,
  type StrictDetailPlan,
  type StrictDetailSourceSpec,
  utf8Json,
} from "./common.js";

export interface ColliersReceiptMember extends C10Member {
  readonly canonicalUrl: string;
  readonly detailPv: string;
}

export interface ColliersEnumerationSlice {
  readonly start: number;
  readonly pageSize: number;
}

export interface ColliersReceiptPlan extends StrictDetailPlan<ColliersReceiptMember> {
  readonly engineKey: string;
  /** Exact provider slices needed to recover every immutable member. */
  readonly slices: readonly ColliersEnumerationSlice[];
}

const COLLIERS_HOST = new URL(COLLIERS_RCM_BASE).host;

function assertSlice(slice: ColliersEnumerationSlice): void {
  if (
    !Number.isInteger(slice.start)
    || slice.start < 1
    || !Number.isInteger(slice.pageSize)
    || slice.pageSize < 1
    || slice.pageSize > COLLIERS_PAGE_SIZE
  ) {
    throw new C10ReceiptError("Colliers request plan has an invalid page range");
  }
}

function assertPlan(plan: ColliersReceiptPlan): void {
  if (!plan.engineKey.trim() || !plan.slices.length) {
    throw new C10ReceiptError("Colliers request plan has an invalid page range");
  }
  const keys = plan.slices.map((slice) => {
    assertSlice(slice);
    return `${slice.start}:${slice.pageSize}`;
  });
  if (new Set(keys).size !== keys.length) {
    throw new C10ReceiptError("Colliers request slices must be unique");
  }
}

export function colliersMapEnumerationCard(
  plan: Pick<ColliersReceiptPlan, "engineKey">,
  slice: ColliersEnumerationSlice,
  index = 0,
): RequestCardInput {
  if (!plan.engineKey.trim()) throw new C10ReceiptError("Colliers engine key is empty");
  assertSlice(slice);
  return {
    id: `colliers-map-enumeration-${index}`,
    sourceKey: "colliers",
    stage: "enumeration",
    method: "GET",
    url: colliersMapUrl(plan.engineKey, slice.start, slice.pageSize),
    allowedHost: COLLIERS_HOST,
    headers: Object.freeze(colliersHeaders()),
    contentType: null,
    body: null,
    cacheMode: "no-store",
    timeoutMs: 30_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

export function colliersListEnumerationCard(
  plan: Pick<ColliersReceiptPlan, "engineKey">,
  slice: ColliersEnumerationSlice,
  index: number,
): RequestCardInput {
  if (!plan.engineKey.trim()) throw new C10ReceiptError("Colliers engine key is empty");
  assertSlice(slice);
  return {
    id: `colliers-list-enumeration-${index}`,
    sourceKey: "colliers",
    stage: "enumeration",
    method: "GET",
    url: colliersListUrl(plan.engineKey, slice.start, slice.pageSize),
    allowedHost: COLLIERS_HOST,
    headers: Object.freeze(colliersHeaders()),
    contentType: null,
    body: null,
    cacheMode: "no-store",
    timeoutMs: 30_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

function memberCard(
  _parent: unknown,
  member: ColliersReceiptMember,
  index: number,
  route: string,
): RequestCardInput {
  if (route !== colliersSlpInitUrl(member.detailPv)) throw new C10ReceiptError("Colliers SLP route is not canonical");
  return {
    id: `colliers-member-${index}`,
    sourceKey: "colliers",
    stage: "member",
    method: "GET",
    url: route,
    allowedHost: COLLIERS_HOST,
    headers: Object.freeze(colliersHeaders()),
    contentType: null,
    body: null,
    cacheMode: "no-store",
    timeoutMs: 30_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

function spec(plan: ColliersReceiptPlan): StrictDetailSourceSpec<ColliersReceiptMember> {
  return {
    sourceKey: "colliers",
    async enumerate(context, sourcePlan) {
      assertPlan(plan);
      const events: Readonly<SealedTransportEvent<SourceProjection>>[] = [];
      const projections: SourceProjection[] = [];
      const routes = new Map<string, { readonly canonicalUrl: string | null; readonly detailPv: string | null; readonly projectId: string }>();
      const providersByUrl = new Map<string, string>();
      const providersByPv = new Map<string, string>();
      const parentsByProvider = new Map<string, Readonly<SealedTransportEvent<SourceProjection>>>();
      for (const [index, slice] of plan.slices.entries()) {
        const mapEvent = await context.transport.oneShot(`colliers-map-enumeration-${index}`, (response) => {
          const payload = utf8Json(response.body, "Colliers map");
          const rows = Array.isArray((payload as { projectLocations?: unknown }).projectLocations)
            ? (payload as { projectLocations: any[] }).projectLocations
            : [];
          const groups = groupColliersMapLocations(rows);
          return { projectIds: groups.map((group) => group.projectId), groups } satisfies SourceProjection;
        });
        const mapProjection = mapEvent.projection as { readonly groups: readonly { readonly projectId: string; readonly pins: readonly unknown[] }[] };
        const listEvent = await context.transport.oneShot(`colliers-list-enumeration-${index}`, (response) => {
          const payload = utf8Json(response.body, "Colliers listing");
          const html = String((payload as { html?: unknown }).html ?? "");
          if (!html) throw new C10ReceiptError("Colliers list response has no HTML cards");
          const cards = parseColliersReceiptCards(html, mapProjection.groups as any[], slice.start);
          const rawPageCount = (payload as { numProjects?: unknown }).numProjects;
          if (
            rawPageCount === null
            || rawPageCount === undefined
            || (typeof rawPageCount === "string" && rawPageCount.trim() === "")
            || (typeof rawPageCount !== "string" && typeof rawPageCount !== "number")
          ) {
            throw new C10ReceiptError("Colliers list response has invalid numProjects");
          }
          const reportedPageCount = Number(rawPageCount);
          if (!Number.isInteger(reportedPageCount) || reportedPageCount < 0 || reportedPageCount !== cards.length) {
            throw new C10ReceiptError("Colliers numProjects/card parity failed");
          }
          return {
            cards: cards.map((card) => ({
              canonicalUrl: card.detailUrl,
              detailPv: card.detailPv,
              projectId: card.mapProjectId,
            })),
            reportedPageCount,
          } satisfies SourceProjection;
        });
        const listing = listEvent.projection as { readonly cards: readonly { readonly canonicalUrl: string | null; readonly detailPv: string | null; readonly projectId: string }[] };
        for (const card of listing.cards) {
          if (
            !card.canonicalUrl
            || !card.detailPv
            || routes.has(card.projectId)
            || providersByUrl.has(card.canonicalUrl)
            || providersByPv.has(card.detailPv)
          ) {
            throw new C10ReceiptError("Colliers slices repeat or omit native identity");
          }
          routes.set(card.projectId, card);
          providersByUrl.set(card.canonicalUrl, card.projectId);
          providersByPv.set(card.detailPv, card.projectId);
          parentsByProvider.set(card.projectId, listEvent);
        }
        events.push(listEvent);
        projections.push({ list: listEvent.projection, map: mapEvent.projection });
      }
      const memberRoutes = new Map<string, string>();
      const memberParents = new Map<string, Readonly<SealedTransportEvent<SourceProjection>>>();
      for (const member of sourcePlan.members) {
        const observed = routes.get(member.providerId);
        const parent = parentsByProvider.get(member.providerId);
        if (!observed || !parent || observed.canonicalUrl !== member.canonicalUrl || observed.detailPv !== member.detailPv) {
          throw new C10ReceiptError("Colliers selected member is absent from native list/map enumeration");
        }
        memberRoutes.set(member.key, colliersSlpInitUrl(member.detailPv));
        memberParents.set(member.key, parent);
      }
      return {
        evidence: { slices: projections },
        memberParents,
        observedMemberKeys: sourcePlan.members.map((member) => member.key),
        memberRoutes,
      };
    },
    memberCard,
    memberProjector: (member, route) => (response) => {
      const payload = utf8Json(response.body, "Colliers SLP detail") as any;
      const projectId = colliersAssertDetailProjectId(
        member.providerId,
        payload?.ProjectSummary?.AttributeVisibility?.ProjectId ?? payload?.ProjectSummary?.ProjectId,
      );
      const canonicalUrl = String(payload?.ProjectSummary?.CanonicalUrl ?? payload?.ProjectSummary?.SiteUrl ?? member.canonicalUrl);
      if (canonicalUrl !== member.canonicalUrl) throw new C10ReceiptError("Colliers detail canonical URL does not match enumeration");
      const nativeAssets = Array.isArray(payload?.GalleryImages)
        ? payload.GalleryImages.map((row: any) => String(row?.Url ?? row?.url ?? "")).filter(Boolean)
        : [];
      return { canonicalUrl, detailRoute: route, nativeAssets, projectId } satisfies SourceProjection;
    },
    memberEvidence: (member, route, event) => ({
      canonicalUrl: member.canonicalUrl,
      detailRoute: route,
      projectId: member.providerId,
      sourceProjection: event.projection,
    }),
  };
}

export function createColliersReceiptProducer(plan: ColliersReceiptPlan): StrictDetailReceiptProducer<ColliersReceiptMember> {
  const immutablePlan = immutableStrictDetailPlan(plan);
  assertPlan(immutablePlan);
  const cards = immutablePlan.slices.flatMap((slice, index) => [
    colliersMapEnumerationCard(immutablePlan, slice, index),
    colliersListEnumerationCard(immutablePlan, slice, index),
  ]);
  return new StrictDetailReceiptProducer(
    { enumerationCards: cards, members: immutablePlan.members },
    spec(immutablePlan),
  );
}
