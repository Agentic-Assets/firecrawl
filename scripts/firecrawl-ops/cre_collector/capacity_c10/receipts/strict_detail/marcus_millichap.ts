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

export interface MarcusReceiptPlan extends StrictDetailPlan<MarcusReceiptMember> {
  readonly pageSize: number;
}

const MARCUS_HOST = new URL(MARCUS_BASE).host;

function assertPlan(plan: MarcusReceiptPlan): void {
  if (!Number.isInteger(plan.pageSize) || plan.pageSize < 1 || plan.pageSize > 500) {
    throw new C10ReceiptError("Marcus request plan has an invalid page size");
  }
}

export function marcusEnumerationCard(plan: MarcusReceiptPlan): RequestCardInput {
  assertPlan(plan);
  return {
    id: "marcus-enumeration",
    sourceKey: "marcus-millichap",
    stage: "enumeration",
    method: "POST",
    url: `${MARCUS_BASE}/api/contentsearch/properties`,
    allowedHost: MARCUS_HOST,
    headers: Object.freeze(marcusHeaders()),
    contentType: "application/json",
    body: canonicalJson(marcusSearchBody(plan.pageSize)),
    cacheMode: "no-store",
    timeoutMs: 30_000,
    maxBytes: 2 * 1024 * 1024,
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
      const event = await context.transport.oneShot("marcus-enumeration", (response) => {
        const parsed = parseMarcusPropertiesResponse(utf8Json(response.body, "Marcus properties"), true);
        const rows = parsed.rows.map((row: any) => {
          const providerId = String(row?.DealId ?? "").trim();
          const activityId = String(row?.ActivityId ?? "").trim();
          const canonicalUrl = marcusUrl(row?.PropertyUrl);
          if (!providerId || !activityId || !canonicalUrl) {
            throw new C10ReceiptError("Marcus native enumeration has an incomplete member identity");
          }
          return { activityId, canonicalUrl, providerId };
        });
        return { rows, total: parsed.total } satisfies SourceProjection;
      });
      const rows = (event.projection as { readonly rows: readonly { readonly activityId: string; readonly canonicalUrl: string; readonly providerId: string }[] }).rows;
      const byProvider = new Map(rows.map((row) => [row.providerId, row]));
      const memberRoutes = new Map<string, string>();
      for (const member of sourcePlan.members) {
        const observed = byProvider.get(member.providerId);
        if (!observed || observed.activityId !== member.activityId || observed.canonicalUrl !== member.canonicalUrl) {
          throw new C10ReceiptError("Marcus selected member is absent from exact native enumeration");
        }
        memberRoutes.set(member.key, observed.activityId);
      }
      return {
        parent: event,
        evidence: event.projection,
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
  assertPlan(immutablePlan);
  return new StrictDetailReceiptProducer(
    { enumerationCards: [marcusEnumerationCard(immutablePlan)], members: immutablePlan.members },
    spec(immutablePlan),
  );
}
