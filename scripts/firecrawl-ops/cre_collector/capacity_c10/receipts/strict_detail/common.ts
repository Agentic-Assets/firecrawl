/**
 * Mechanics shared by the source-owned strict-detail receipt producers.
 *
 * This module deliberately owns no URLs, parsers, source policy, or request
 * bodies.  Those stay in the individual source modules.  It only enforces the
 * sealed request-graph lifecycle common to every C10 source.
 */
import {
  C10ReceiptError,
  canonicalJson,
  type PublicReceipt,
} from "../contracts.js";
import {
  type C10Member,
  type ReceiptProducer,
  type ReceiptProducerContext,
  requireReceiptSource,
  sealStageReceipt,
} from "../producer.js";
import type {
  RequestCardInput,
  SealedTransportEvent,
  SourceProjection,
  SourceResponseProjector,
} from "../transport.js";

export interface StrictDetailPlan<Member extends C10Member> {
  /** Fixed, source-owned enumeration cards known before any provider request. */
  readonly enumerationCards: readonly RequestCardInput[];
  /** Exact cohort members selected by the separately governed planner. */
  readonly members: readonly Member[];
}

export interface EnumerationRun<Member extends C10Member> {
  /** A sealed enumeration event which canonically anchors every member card. */
  readonly parent: Readonly<SealedTransportEvent<SourceProjection>>;
  /** Source-local, canonical, body-free evidence. */
  readonly evidence: SourceProjection;
  /** Member keys observed in native enumeration, in canonical order. */
  readonly observedMemberKeys: readonly string[];
  readonly memberRoutes: ReadonlyMap<string, string>;
}

export interface StrictDetailSourceSpec<Member extends C10Member> {
  readonly sourceKey: string;
  readonly enumerate: (
    context: ReceiptProducerContext,
    plan: StrictDetailPlan<Member>,
  ) => Promise<EnumerationRun<Member>>;
  readonly memberCard: (
    parent: Readonly<SealedTransportEvent<SourceProjection>>,
    member: Member,
    index: number,
    route: string,
  ) => RequestCardInput;
  readonly memberProjector: (
    member: Member,
    route: string,
  ) => SourceResponseProjector<SourceProjection>;
  readonly memberEvidence: (
    member: Member,
    route: string,
    event: Readonly<SealedTransportEvent<SourceProjection>>,
  ) => SourceProjection;
}

function exactMemberKeys<Member extends C10Member>(members: readonly Member[]): readonly string[] {
  const keys = members.map((member) => member.key);
  if (!keys.length || keys.some((key) => !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,160}$/.test(key))) {
    throw new C10ReceiptError("strict-detail plan has an invalid member key");
  }
  if (new Set(keys).size !== keys.length) {
    throw new C10ReceiptError("strict-detail plan repeats a member key");
  }
  return Object.freeze([...keys]);
}

function deepFreeze<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.freeze(value);
    for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child);
  }
  return value;
}

/** Clone and freeze one complete source plan before cards and specs close over it. */
export function immutableStrictDetailPlan<Plan extends object>(plan: Plan): Plan {
  return deepFreeze(JSON.parse(canonicalJson(plan)) as Plan);
}

function assertContext<Member extends C10Member>(
  context: ReceiptProducerContext,
  spec: StrictDetailSourceSpec<Member>,
): void {
  requireReceiptSource(context, spec.sourceKey);
}

/**
 * Lifecycle wrapper around source-local cards and projectors.  The wrapper has
 * no transport fallback, retry, registry, cache, or collection behavior.
 */
export class StrictDetailReceiptProducer<Member extends C10Member> implements ReceiptProducer {
  private prepared = false;
  private readonly cardIds = new Map<string, string>();
  private routes = new Map<string, string>();

  constructor(
    private readonly plan: StrictDetailPlan<Member>,
    private readonly spec: StrictDetailSourceSpec<Member>,
  ) {
    const memberKeys = exactMemberKeys(plan.members);
    if (!plan.enumerationCards.length || plan.enumerationCards.some((card) =>
      card.sourceKey !== spec.sourceKey || card.stage !== "enumeration",
    )) {
      throw new C10ReceiptError("strict-detail plan must start with source-bound enumeration cards");
    }
    // Keep a recursively immutable copy even for direct wrapper construction.
    this.plan = immutableStrictDetailPlan(plan);
    void memberKeys;
  }

  async produceEnumerationReceipt(context: ReceiptProducerContext): Promise<PublicReceipt> {
    assertContext(context, this.spec);
    if (this.prepared) throw new C10ReceiptError("strict-detail member graph is already prepared");
    context.transport.assertInitialCards(this.plan.enumerationCards);
    const memberKeys = exactMemberKeys(this.plan.members);
    const enumerated = await this.spec.enumerate(context, this.plan);
    const observed = [...enumerated.observedMemberKeys];
    if (
      observed.length !== memberKeys.length ||
      new Set(observed).size !== observed.length ||
      observed.some((key, index) => key !== memberKeys[index])
    ) {
      throw new C10ReceiptError("native enumeration does not exactly bind the selected member cohort");
    }
    for (const member of this.plan.members) {
      const route = enumerated.memberRoutes.get(member.key);
      if (!route) throw new C10ReceiptError("native enumeration omitted a canonical member route");
      this.routes.set(member.key, route);
    }
    for (const [index, member] of this.plan.members.entries()) {
      const route = this.routes.get(member.key);
      if (!route) throw new C10ReceiptError("strict-detail member route is unavailable");
      const expansion = await context.transport.appendFrom(
        enumerated.parent,
        {
          sourceKey: this.spec.sourceKey,
          stage: "member",
          maximumCards: this.plan.members.length,
          create: (parent, coordinate: { member: Member; index: number; route: string }) =>
            this.spec.memberCard(parent, coordinate.member, coordinate.index, coordinate.route),
        },
        { member, index, route },
      );
      this.cardIds.set(member.key, expansion.cardId);
    }
    const graph = await context.transport.freezeMemberGraph();
    this.prepared = true;
    return sealStageReceipt(context, "enumeration", null, {
      nativeEnumeration: enumerated.evidence,
      memberGraph: graph,
      memberKeys,
    });
  }

  async produceMemberReceipt(context: ReceiptProducerContext, member: C10Member): Promise<PublicReceipt> {
    assertContext(context, this.spec);
    if (!this.prepared) throw new C10ReceiptError("strict-detail member graph has not been prepared");
    const index = this.plan.members.findIndex((candidate) => candidate.key === member.key);
    if (index < 0 || this.plan.members[index].providerId !== member.providerId) {
      throw new C10ReceiptError("member is not bound to this strict-detail graph");
    }
    const bound = this.plan.members[index];
    const route = this.routes.get(bound.key);
    const cardId = this.cardIds.get(bound.key);
    if (!route || !cardId) throw new C10ReceiptError("strict-detail member request card is unavailable");
    const event = await context.transport.oneShot(cardId, this.spec.memberProjector(bound, route));
    return sealStageReceipt(context, "member", bound.key, {
      member: this.spec.memberEvidence(bound, route, event),
      memberCardId: cardId,
      memberProjectionSha256: event.projectionSha256,
    });
  }
}

export function utf8Json(body: Uint8Array, label: string): unknown {
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    throw new C10ReceiptError(`${label} response is not valid UTF-8 JSON`);
  }
}

export function utf8Text(body: Uint8Array, label: string): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(body);
  } catch {
    throw new C10ReceiptError(`${label} response is not valid UTF-8`);
  }
}
