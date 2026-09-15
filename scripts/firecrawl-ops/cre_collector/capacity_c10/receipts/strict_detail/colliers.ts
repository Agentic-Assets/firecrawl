/** Colliers SalesTracker RCM list/map/SLP evidence without collector helpers. */
import {
  C10ReceiptError,
} from "../contracts.js";
import type { RequestCardInput, SourceProjection } from "../transport.js";
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
  type StrictDetailPlan,
  type StrictDetailSourceSpec,
  utf8Json,
} from "./common.js";

export interface ColliersReceiptMember extends C10Member {
  readonly canonicalUrl: string;
  readonly detailPv: string;
}

export interface ColliersReceiptPlan extends StrictDetailPlan<ColliersReceiptMember> {
  readonly engineKey: string;
  readonly start: number;
  readonly pageSize: number;
}

const COLLIERS_HOST = new URL(COLLIERS_RCM_BASE).host;

function assertPlan(plan: ColliersReceiptPlan): void {
  if (!Number.isInteger(plan.start) || plan.start < 1 || !Number.isInteger(plan.pageSize) || plan.pageSize < 1 || plan.pageSize > COLLIERS_PAGE_SIZE) {
    throw new C10ReceiptError("Colliers request plan has an invalid page range");
  }
}

export function colliersMapEnumerationCard(plan: ColliersReceiptPlan): RequestCardInput {
  assertPlan(plan);
  return {
    id: "colliers-map-enumeration",
    sourceKey: "colliers",
    stage: "enumeration",
    method: "GET",
    url: colliersMapUrl(plan.engineKey, plan.start, plan.pageSize),
    allowedHost: COLLIERS_HOST,
    headers: Object.freeze(colliersHeaders()),
    contentType: null,
    body: null,
    cacheMode: "no-store",
    timeoutMs: 30_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

function colliersListEnumerationCard(plan: ColliersReceiptPlan): RequestCardInput {
  return {
    id: "colliers-list-enumeration",
    sourceKey: "colliers",
    stage: "enumeration",
    method: "GET",
    url: colliersListUrl(plan.engineKey, plan.start, plan.pageSize),
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
      const mapEvent = await context.transport.oneShot("colliers-map-enumeration", (response) => {
        const payload = utf8Json(response.body, "Colliers map");
        const rows = Array.isArray((payload as { projectLocations?: unknown }).projectLocations)
          ? (payload as { projectLocations: any[] }).projectLocations
          : [];
        const groups = groupColliersMapLocations(rows);
        return { projectIds: groups.map((group) => group.projectId), groups } satisfies SourceProjection;
      });
      const mapProjection = mapEvent.projection as { readonly groups: readonly { readonly projectId: string; readonly pins: readonly unknown[] }[] };
      await context.transport.appendFrom(
        mapEvent,
        {
          sourceKey: "colliers",
          stage: "enumeration",
          maximumCards: 2,
          create: () => colliersListEnumerationCard(plan),
        },
        null,
      );
      const listEvent = await context.transport.oneShot("colliers-list-enumeration", (response) => {
        const payload = utf8Json(response.body, "Colliers listing");
        const html = String((payload as { html?: unknown }).html ?? "");
        if (!html) throw new C10ReceiptError("Colliers list response has no HTML cards");
        const cards = parseColliersReceiptCards(html, mapProjection.groups as any[], plan.start);
        return {
          cards: cards.map((card) => ({
            canonicalUrl: card.detailUrl,
            detailPv: card.detailPv,
            projectId: card.mapProjectId,
          })),
          reportedTotal: Number((payload as { total?: unknown }).total),
        } satisfies SourceProjection;
      });
      const listing = listEvent.projection as { readonly cards: readonly { readonly canonicalUrl: string | null; readonly detailPv: string | null; readonly projectId: string }[] };
      const routes = new Map(listing.cards.map((card) => [card.projectId, card]));
      const memberRoutes = new Map<string, string>();
      for (const member of sourcePlan.members) {
        const observed = routes.get(member.providerId);
        if (!observed || observed.canonicalUrl !== member.canonicalUrl || observed.detailPv !== member.detailPv) {
          throw new C10ReceiptError("Colliers selected member is absent from native list/map enumeration");
        }
        memberRoutes.set(member.key, colliersSlpInitUrl(member.detailPv));
      }
      return {
        parent: listEvent,
        evidence: { list: listEvent.projection, map: mapEvent.projection },
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
  assertPlan(plan);
  return new StrictDetailReceiptProducer(
    { enumerationCards: [colliersMapEnumerationCard(plan)], members: plan.members },
    spec(plan),
  );
}
