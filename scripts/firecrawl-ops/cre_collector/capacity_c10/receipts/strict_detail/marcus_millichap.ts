/** Marcus & Millichap native content-search and map-detail receipt producer. */
import {
  C10ReceiptError,
  canonicalJson,
} from "../contracts.js";
import type { RequestCardInput, SourceProjection } from "../transport.js";
import type { C10Member } from "../producer.js";
import {
  MARCUS_BASE,
  marcusHeaders,
  marcusMapDetailBody,
  marcusSearchBody,
  marcusUrl,
  parseMarcusMapRowsResponse,
  parseMarcusPropertiesResponse,
} from "../../../sources/pure/marcus-receipt.js";
import {
  StrictDetailReceiptProducer,
  immutableStrictDetailPlan,
  type StrictDetailPlan,
  type StrictDetailSourceSpec,
  utf8Json,
} from "./common.js";

export interface MarcusReceiptMember extends C10Member {
  readonly activityId: string;
  readonly canonicalUrl: string;
}

export type MarcusReceiptPlan = StrictDetailPlan<MarcusReceiptMember>;

const MARCUS_HOST = new URL(MARCUS_BASE).host;

export function marcusCountEnumerationCard(): RequestCardInput {
  return {
    id: "marcus-count-enumeration",
    sourceKey: "marcus-millichap",
    stage: "enumeration",
    method: "POST",
    url: `${MARCUS_BASE}/api/contentsearch/properties`,
    allowedHost: MARCUS_HOST,
    headers: Object.freeze(marcusHeaders()),
    contentType: "application/json",
    body: canonicalJson(marcusSearchBody(1)),
    cacheMode: "no-store",
    timeoutMs: 30_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

export function marcusMapEnumerationCard(): RequestCardInput {
  return {
    ...marcusCountEnumerationCard(),
    id: "marcus-map-enumeration",
    url: `${MARCUS_BASE}/api/contentsearch/mapproperties`,
  };
}

function memberCard(
  _parent: unknown,
  member: MarcusReceiptMember,
  index: number,
  route: string,
): RequestCardInput {
  if (route !== member.activityId) throw new C10ReceiptError("Marcus native activity route is invalid");
  return {
    id: `marcus-member-${index}`,
    sourceKey: "marcus-millichap",
    stage: "member",
    method: "POST",
    url: `${MARCUS_BASE}/api/contentsearch/mappropertydetail`,
    allowedHost: MARCUS_HOST,
    headers: Object.freeze(marcusHeaders()),
    contentType: "application/json",
    body: canonicalJson(marcusMapDetailBody(route)),
    cacheMode: "no-store",
    timeoutMs: 30_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

function spec(plan: MarcusReceiptPlan): StrictDetailSourceSpec<MarcusReceiptMember> {
  return {
    sourceKey: "marcus-millichap",
    async enumerate(context, sourcePlan) {
      const countEvent = await context.transport.oneShot("marcus-count-enumeration", (response) => {
        const parsed = parseMarcusPropertiesResponse(utf8Json(response.body, "Marcus properties"), true);
        if (parsed.total === null) throw new C10ReceiptError("Marcus properties response lacks a native count");
        return { total: parsed.total } satisfies SourceProjection;
      });
      const total = (countEvent.projection as { readonly total: number }).total;
      const mapEvent = await context.transport.oneShot("marcus-map-enumeration", (response) => {
        const payload = utf8Json(response.body, "Marcus map properties") as any;
        const rawRows = payload?.Results?.Properties ?? payload?.Properties;
        if (!Array.isArray(rawRows)) {
          throw new C10ReceiptError("Marcus map enumeration has no Properties array");
        }
        const rows = parseMarcusMapRowsResponse(payload, true);
        if (rows.length !== rawRows.length || rows.length !== total) {
          throw new C10ReceiptError("Marcus map enumeration does not reconcile to native inventory count");
        }
        return {
          activityIds: rows.map((row: any) => String(row.ActivityId).trim()),
          total,
        } satisfies SourceProjection;
      });
      const activityIds = (mapEvent.projection as { readonly activityIds: readonly string[] }).activityIds;
      const available = new Set(activityIds);
      const memberRoutes = new Map<string, string>();
      for (const member of sourcePlan.members) {
        if (!available.has(member.activityId)) {
          throw new C10ReceiptError("Marcus selected member is absent from exact native enumeration");
        }
        memberRoutes.set(member.key, member.activityId);
      }
      return {
        parent: mapEvent,
        evidence: { count: countEvent.projection, map: mapEvent.projection },
        observedMemberKeys: sourcePlan.members.map((member) => member.key),
        memberRoutes,
      };
    },
    memberCard,
    memberProjector: (member, route) => (response) => {
      const payload = utf8Json(response.body, "Marcus map detail") as any;
      const results = payload?.Results ?? payload;
      const propertyDetail = typeof results?.PropertyDetail === "string" ? results.PropertyDetail.trim() : "";
      const observedUrl = marcusUrl(results?.PropertyUrl);
      if (!propertyDetail || !observedUrl) {
        throw new C10ReceiptError("Marcus map detail omitted PropertyDetail or PropertyUrl");
      }
      if (observedUrl !== member.canonicalUrl) throw new C10ReceiptError("Marcus map detail canonical URL does not match enumeration");
      return {
        activityId: route,
        canonicalUrl: observedUrl,
        hasPropertyDetail: true,
        nativeAssets: [],
        providerId: member.providerId,
      } satisfies SourceProjection;
    },
    memberEvidence: (member, route, event) => ({
      activityId: route,
      canonicalUrl: member.canonicalUrl,
      providerId: member.providerId,
      sourceProjection: event.projection,
    }),
  };
}

export function createMarcusReceiptProducer(plan: MarcusReceiptPlan): StrictDetailReceiptProducer<MarcusReceiptMember> {
  const immutablePlan = immutableStrictDetailPlan(plan);
  return new StrictDetailReceiptProducer(
    {
      enumerationCards: [marcusCountEnumerationCard(), marcusMapEnumerationCard()],
      members: immutablePlan.members,
    },
    spec(immutablePlan),
  );
}
