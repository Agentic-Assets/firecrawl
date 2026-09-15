/** JLL native GraphQL enumeration plus one-shot public detail evidence. */
import {
  C10ReceiptError,
  canonicalJson,
} from "../contracts.js";
import type { RequestCardInput, SealedTransportEvent, SourceProjection } from "../transport.js";
import type { C10Member } from "../producer.js";
import {
  JLL_GRAPHQL_URL,
  JLL_SEARCH_RESULTS_QUERY,
  jllGraphqlVariables,
  jllNextData,
  normalizedJllListingUrl,
  parseJllGraphqlSearchEnvelope,
} from "../../../sources/pure/jll-receipt.js";
import {
  StrictDetailReceiptProducer,
  immutableStrictDetailPlan,
  type StrictDetailPlan,
  type StrictDetailSourceSpec,
  utf8Json,
  utf8Text,
} from "./common.js";

export interface JllReceiptMember extends C10Member {
  readonly canonicalUrl: string;
}

export interface JllEnumerationSlice {
  readonly transaction: "sale" | "lease";
  readonly propertyType: string;
  readonly page: number;
}

export interface JllReceiptPlan extends StrictDetailPlan<JllReceiptMember> {
  /** Exact filter/page strata needed to recover every immutable cohort member. */
  readonly enumerations: readonly JllEnumerationSlice[];
}

const JLL_HOST = "property.jll.com";
export const JLL_BROWSER_BOOTSTRAP_URL = "https://property.jll.com/";

function validateEnumerationSlices(slices: readonly JllEnumerationSlice[]): void {
  const keys = slices.map((slice) => {
    if (
      (slice.transaction !== "sale" && slice.transaction !== "lease")
      || typeof slice.propertyType !== "string"
      || !slice.propertyType.trim()
      || !Number.isInteger(slice.page)
      || slice.page < 1
    ) {
      throw new C10ReceiptError("JLL enumeration slice is invalid");
    }
    return `${slice.transaction}\u0000${slice.propertyType}\u0000${slice.page}`;
  });
  if (!keys.length || new Set(keys).size !== keys.length) {
    throw new C10ReceiptError("JLL enumeration slices must be nonempty and unique");
  }
}

export function jllEnumerationCard(plan: JllEnumerationSlice, index = 0): RequestCardInput {
  validateEnumerationSlices([plan]);
  return {
    id: `jll-enumeration-${index}`,
    sourceKey: "jll",
    stage: "enumeration",
    method: "POST",
    url: JLL_GRAPHQL_URL,
    allowedHost: JLL_HOST,
    headers: Object.freeze({
      accept: "application/json",
      "cache-control": "no-cache",
      "content-type": "application/json",
      pragma: "no-cache",
    }),
    contentType: "application/json",
    body: canonicalJson({
      operationName: "SearchResults",
      query: JLL_SEARCH_RESULTS_QUERY,
      variables: jllGraphqlVariables(plan.transaction, plan.propertyType, plan.page),
    }),
    browserBootstrapUrl: JLL_BROWSER_BOOTSTRAP_URL,
    cacheMode: "no-store",
    timeoutMs: 30_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

function memberCard(
  _parent: unknown,
  member: JllReceiptMember,
  index: number,
  route: string,
): RequestCardInput {
  if (normalizedJllListingUrl(member.canonicalUrl) !== route) {
    throw new C10ReceiptError("JLL member route is not canonical");
  }
  return {
    id: `jll-member-${index}`,
    sourceKey: "jll",
    stage: "member",
    method: "GET",
    url: route,
    allowedHost: JLL_HOST,
    headers: Object.freeze({ accept: "text/html,application/xhtml+xml" }),
    contentType: null,
    body: null,
    browserBootstrapUrl: JLL_BROWSER_BOOTSTRAP_URL,
    cacheMode: "no-store",
    timeoutMs: 30_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

function detailProjection(member: JllReceiptMember, route: string) {
  return (response: { readonly body: Uint8Array; readonly status: number; readonly finalUrl: string }) => {
    if (response.status !== 200 || response.finalUrl !== route) {
      throw new C10ReceiptError("JLL browser detail is not a qualified canonical response");
    }
    const next = jllNextData(utf8Text(response.body, "JLL detail")) as any;
    const property = next?.props?.pageProps?.property ?? next?.props?.pageProps?.listing;
    const providerId = String(property?.id ?? property?.propertyId ?? "").trim();
    if (providerId !== member.providerId) {
      throw new C10ReceiptError("JLL detail provider identity does not match native enumeration");
    }
    const observedUrl = property?.pageUrl ?? property?.url ?? route;
    if (normalizedJllListingUrl(String(observedUrl)) !== route) {
      throw new C10ReceiptError("JLL detail canonical URL does not match native enumeration");
    }
    const assets = Array.isArray(property?.images)
      ? property.images.map((value: unknown) => String(value)).filter(Boolean)
      : [];
    return {
      canonicalUrl: route,
      nativeAssets: assets,
      providerId,
    } satisfies SourceProjection;
  };
}

function spec(plan: JllReceiptPlan): StrictDetailSourceSpec<JllReceiptMember> {
  return {
    sourceKey: "jll",
    async enumerate(context, sourcePlan) {
      validateEnumerationSlices(plan.enumerations);
      const events: Readonly<SealedTransportEvent<SourceProjection>>[] = [];
      const projections: SourceProjection[] = [];
      const byProvider = new Map<string, string>();
      const byUrl = new Map<string, string>();
      const parentsByProvider = new Map<string, Readonly<SealedTransportEvent<SourceProjection>>>();
      for (const [index, slice] of plan.enumerations.entries()) {
        const event = await context.transport.oneShot(`jll-enumeration-${index}`, (response) => {
          const parsed = parseJllGraphqlSearchEnvelope(utf8Json(response.body, "JLL GraphQL"));
          const routes = parsed.items.map((item) => {
            const providerId = String(item.id ?? "").trim();
            const canonicalUrl = normalizedJllListingUrl(String(item.pageUrl ?? ""));
            if (!providerId || !canonicalUrl) {
              throw new C10ReceiptError("JLL GraphQL enumeration contains a missing identity");
            }
            return { providerId, canonicalUrl };
          });
          if (
            new Set(routes.map((route) => route.providerId)).size !== routes.length
            || new Set(routes.map((route) => route.canonicalUrl)).size !== routes.length
          ) {
            throw new C10ReceiptError("JLL GraphQL page contains duplicate identities");
          }
          return {
            index,
            page: slice.page,
            propertyType: slice.propertyType,
            providerIds: routes.map((route) => route.providerId),
            total: parsed.total,
            transaction: slice.transaction,
            urls: routes.map((route) => route.canonicalUrl),
          } satisfies SourceProjection;
        });
        const projection = event.projection as {
          readonly providerIds: readonly string[];
          readonly urls: readonly string[];
        };
        for (const [routeIndex, providerId] of projection.providerIds.entries()) {
          const canonicalUrl = projection.urls[routeIndex];
          if (!canonicalUrl) throw new C10ReceiptError("JLL GraphQL route is missing");
          const priorUrl = byProvider.get(providerId);
          const priorProvider = byUrl.get(canonicalUrl);
          if (
            (priorUrl !== undefined && priorUrl !== canonicalUrl)
            || (priorProvider !== undefined && priorProvider !== providerId)
          ) {
            throw new C10ReceiptError("JLL GraphQL strata disagree on native identity");
          }
          byProvider.set(providerId, canonicalUrl);
          byUrl.set(canonicalUrl, providerId);
          if (!parentsByProvider.has(providerId)) parentsByProvider.set(providerId, event);
        }
        events.push(event);
        projections.push(event.projection);
      }
      const memberRoutes = new Map<string, string>();
      const memberParents = new Map<string, Readonly<SealedTransportEvent<SourceProjection>>>();
      for (const member of sourcePlan.members) {
        const observed = byProvider.get(member.providerId);
        const parent = parentsByProvider.get(member.providerId);
        if (!observed || !parent || observed !== normalizedJllListingUrl(member.canonicalUrl)) {
          throw new C10ReceiptError("JLL selected member is absent from exact native enumeration");
        }
        memberRoutes.set(member.key, observed);
        memberParents.set(member.key, parent);
      }
      return {
        evidence: {
          enumerations: projections,
          providerIds: [...byProvider.keys()],
          urls: [...byProvider.values()],
        },
        memberParents,
        observedMemberKeys: sourcePlan.members.map((member) => member.key),
        memberRoutes,
      };
    },
    memberCard: (_parent, member, index, route) => memberCard(_parent, member, index, route),
    memberProjector: (member, route) => detailProjection(member, route),
    memberEvidence: (member, route, event) => ({
      canonicalUrl: route,
      providerId: member.providerId,
      sourceProjection: event.projection,
    }),
  };
}

/**
 * This creates no network client.  A future governed executor supplies the
 * one-shot direct transport and independently binds the exact source plan.
 */
export function createJllReceiptProducer(plan: JllReceiptPlan): StrictDetailReceiptProducer<JllReceiptMember> {
  const immutablePlan = immutableStrictDetailPlan(plan);
  validateEnumerationSlices(immutablePlan.enumerations);
  const cards = immutablePlan.enumerations.map((slice, index) => jllEnumerationCard(slice, index));
  const configured: StrictDetailPlan<JllReceiptMember> = {
    enumerationCards: cards,
    members: immutablePlan.members,
  };
  return new StrictDetailReceiptProducer(configured, spec(immutablePlan));
}
