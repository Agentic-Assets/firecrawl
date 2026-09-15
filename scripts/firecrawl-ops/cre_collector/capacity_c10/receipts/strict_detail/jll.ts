/** JLL native GraphQL enumeration plus one-shot public detail evidence. */
import {
  C10ReceiptError,
  canonicalJson,
  type C10Member,
  type RequestCardInput,
  type SourceProjection,
} from "../index.js";
import {
  JLL_GRAPHQL_URL,
  JLL_SEARCH_RESULTS_QUERY,
  jllGraphqlVariables,
  jllNextData,
  normalizedJllListingUrl,
  parseJllGraphqlSearchPage,
} from "../../../sources/jll.js";
import {
  StrictDetailReceiptProducer,
  type StrictDetailPlan,
  type StrictDetailSourceSpec,
  utf8Json,
  utf8Text,
} from "./common.js";

export interface JllReceiptMember extends C10Member {
  readonly canonicalUrl: string;
}

export interface JllReceiptPlan extends StrictDetailPlan<JllReceiptMember> {
  readonly transaction: "sale" | "lease";
  readonly propertyType: string;
  readonly page: number;
}

const JLL_HOST = "property.jll.com";
export const JLL_BROWSER_BOOTSTRAP_URL = "https://property.jll.com/";

export function jllEnumerationCard(plan: Pick<JllReceiptPlan, "transaction" | "propertyType" | "page">): RequestCardInput {
  return {
    id: "jll-enumeration",
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
    const next = jllNextData(utf8Text(response.body, "JLL detail"));
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
      const event = await context.transport.oneShot("jll-enumeration", (response) => {
        const parsed = parseJllGraphqlSearchPage(
          utf8Json(response.body, "JLL GraphQL"),
          plan.transaction,
          plan.propertyType,
          plan.page,
        );
        const routes = parsed.listings.map((listing) => ({
          providerId: String(listing.id),
          canonicalUrl: normalizedJllListingUrl(String(listing.url)),
        }));
        return {
          page: plan.page,
          propertyType: plan.propertyType,
          providerIds: routes.map((route) => route.providerId),
          total: parsed.total,
          urls: routes.map((route) => route.canonicalUrl),
        } satisfies SourceProjection;
      });
      const routes = event.projection as { readonly providerIds: readonly string[]; readonly urls: readonly string[] };
      const byProvider = new Map(routes.providerIds.map((providerId, index) => [providerId, routes.urls[index]]));
      const memberRoutes = new Map<string, string>();
      for (const member of sourcePlan.members) {
        const observed = byProvider.get(member.providerId);
        if (!observed || observed !== normalizedJllListingUrl(member.canonicalUrl)) {
          throw new C10ReceiptError("JLL selected member is absent from exact native enumeration");
        }
        memberRoutes.set(member.key, observed);
      }
      return {
        parent: event,
        evidence: event.projection,
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
  const card = jllEnumerationCard(plan);
  const configured: StrictDetailPlan<JllReceiptMember> = {
    enumerationCards: [card],
    members: plan.members,
  };
  return new StrictDetailReceiptProducer(configured, spec(plan));
}
