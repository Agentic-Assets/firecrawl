/** JLL Investor native search and Next.js detail receipt producer. */
import {
  C10ReceiptError,
} from "../contracts.js";
import type { RequestCardInput, SourceProjection } from "../transport.js";
import type { C10Member } from "../producer.js";
import {
  JLL_INVESTOR_HOST,
  jllInvestorBuildId,
  jllInvestorDetailRoute,
  jllInvestorSearchPageUrl,
  jllInvestorStructuredListing,
  jllInvestorUrlFromAlias,
  parseJllInvestorSearchPage,
} from "../../../sources/pure/jll-investor-receipt.js";
import {
  StrictDetailReceiptProducer,
  immutableStrictDetailPlan,
  type StrictDetailPlan,
  type StrictDetailSourceSpec,
  utf8Json,
  utf8Text,
} from "./common.js";

export interface JllInvestorReceiptMember extends C10Member {
  readonly canonicalUrl: string;
}

export interface JllInvestorReceiptPlan extends StrictDetailPlan<JllInvestorReceiptMember> {
  /** Exact provider search pages needed to recover every immutable member. */
  readonly pages: readonly number[];
}

const JLL_INVESTOR_HOSTNAME = new URL(JLL_INVESTOR_HOST).host;

function validatePages(pages: readonly number[]): void {
  if (
    !pages.length
    || pages.some((page) => !Number.isInteger(page) || page < 1)
    || new Set(pages).size !== pages.length
  ) {
    throw new C10ReceiptError("JLL Investor pages must be nonempty, positive, and unique");
  }
}

export function jllInvestorEnumerationCard(page: number, index = 0): RequestCardInput {
  validatePages([page]);
  return {
    id: `jll-investor-enumeration-${index}`,
    sourceKey: "jll-investor",
    stage: "enumeration",
    method: "GET",
    url: jllInvestorSearchPageUrl(page),
    allowedHost: JLL_INVESTOR_HOSTNAME,
    headers: Object.freeze({ accept: "text/html,application/xhtml+xml" }),
    contentType: null,
    body: null,
    cacheMode: "no-store",
    timeoutMs: 60_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

function investorMemberCard(
  _parent: unknown,
  _member: JllInvestorReceiptMember,
  index: number,
  route: string,
): RequestCardInput {
  return {
    id: `jll-investor-member-${index}`,
    sourceKey: "jll-investor",
    stage: "member",
    method: "GET",
    url: route,
    allowedHost: JLL_INVESTOR_HOSTNAME,
    headers: Object.freeze({ accept: "application/json" }),
    contentType: null,
    body: null,
    cacheMode: "no-store",
    timeoutMs: 60_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

function investorDetailProjection(member: JllInvestorReceiptMember, route: string) {
  return (response: { readonly body: Uint8Array }) => {
    const payload = utf8Json(response.body, "JLL Investor structured detail");
    const listing = jllInvestorStructuredListing(payload);
    if (!listing || String(listing?.id ?? "").trim() !== member.providerId) {
      throw new C10ReceiptError("JLL Investor structured detail identity does not match enumeration");
    }
    const observedUrl = jllInvestorUrlFromAlias(String(listing?.alias ?? ""));
    if (observedUrl !== member.canonicalUrl) {
      throw new C10ReceiptError("JLL Investor structured detail canonical URL does not match enumeration");
    }
    return {
      canonicalUrl: observedUrl,
      detailRoute: route,
      nativeAssets: Array.isArray(listing?.images) ? listing.images.map(String).filter(Boolean) : [],
      providerId: member.providerId,
    } satisfies SourceProjection;
  };
}

function spec(plan: JllInvestorReceiptPlan): StrictDetailSourceSpec<JllInvestorReceiptMember> {
  return {
    sourceKey: "jll-investor",
    async enumerate(context, sourcePlan) {
      validatePages(plan.pages);
      const events = [];
      const projections: SourceProjection[] = [];
      const routes = new Map<string, { readonly canonicalUrl: string; readonly detailRoute: string; readonly providerId: string }>();
      const providersByUrl = new Map<string, string>();
      let expectedBuildId: string | null = null;
      let expectedCount: number | null = null;
      for (const [index, page] of plan.pages.entries()) {
        const event = await context.transport.oneShot(`jll-investor-enumeration-${index}`, (response) => {
          const raw = utf8Text(response.body, "JLL Investor search");
          const search = parseJllInvestorSearchPage(raw, page);
          const buildId = jllInvestorBuildId(raw);
          if (!buildId) throw new C10ReceiptError("JLL Investor search lacks a safe Next.js build id");
          const pageRoutes = search.rows.map((row) => {
            const providerId = String(row?.id ?? "").trim();
            const canonicalUrl = jllInvestorUrlFromAlias(String(row?.alias ?? ""));
            if (!providerId || !canonicalUrl) throw new C10ReceiptError("JLL Investor search has an incomplete identity");
            return { canonicalUrl, detailRoute: jllInvestorDetailRoute(buildId, canonicalUrl).url, providerId };
          });
          return {
            buildId,
            count: search.count,
            page: search.page,
            routes: pageRoutes,
          } satisfies SourceProjection;
        });
        const projection = event.projection as {
          readonly buildId: string;
          readonly count: number;
          readonly routes: readonly { readonly canonicalUrl: string; readonly detailRoute: string; readonly providerId: string }[];
        };
        if (
          expectedBuildId !== null && projection.buildId !== expectedBuildId
          || expectedCount !== null && projection.count !== expectedCount
        ) {
          throw new C10ReceiptError("JLL Investor pages disagree on build or inventory count");
        }
        expectedBuildId = projection.buildId;
        expectedCount = projection.count;
        for (const row of projection.routes) {
          const priorRoute = routes.get(row.providerId);
          const priorProvider = providersByUrl.get(row.canonicalUrl);
          if (
            priorRoute !== undefined
            || priorProvider !== undefined
          ) {
            throw new C10ReceiptError("JLL Investor pages repeat or conflict on native identity");
          }
          routes.set(row.providerId, row);
          providersByUrl.set(row.canonicalUrl, row.providerId);
        }
        events.push(event);
        projections.push(event.projection);
      }
      const memberRoutes = new Map<string, string>();
      for (const member of sourcePlan.members) {
        const observed = routes.get(member.providerId);
        if (!observed || observed.canonicalUrl !== member.canonicalUrl) {
          throw new C10ReceiptError("JLL Investor selected member is absent from exact native enumeration");
        }
        memberRoutes.set(member.key, observed.detailRoute);
      }
      return {
        parent: events[0]!,
        evidence: { count: expectedCount, pages: projections },
        observedMemberKeys: sourcePlan.members.map((member) => member.key),
        memberRoutes,
      };
    },
    memberCard: (parent, member, index, route) => investorMemberCard(parent, member, index, route),
    memberProjector: (member, route) => investorDetailProjection(member, route),
    memberEvidence: (member, route, event) => ({
      canonicalUrl: member.canonicalUrl,
      detailRoute: route,
      providerId: member.providerId,
      sourceProjection: event.projection,
    }),
  };
}

export function createJllInvestorReceiptProducer(
  plan: JllInvestorReceiptPlan,
): StrictDetailReceiptProducer<JllInvestorReceiptMember> {
  const immutablePlan = immutableStrictDetailPlan(plan);
  validatePages(immutablePlan.pages);
  const cards = immutablePlan.pages.map((page, index) => jllInvestorEnumerationCard(page, index));
  return new StrictDetailReceiptProducer(
    { enumerationCards: cards, members: immutablePlan.members },
    spec(immutablePlan),
  );
}
