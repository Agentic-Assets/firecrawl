/** Reviewed JLL-only browser cards and bounded fidelity/saturation execution. */
import {
  C10ReceiptError,
  canonicalSha256,
  type ReceiptBinding,
} from "./contracts.js";
import { type ReceiptArtifactStore } from "./private_store.js";
import {
  type RequestCard,
  type RequestCardInput,
  SourceBoundOneShotTransport,
  allowlistedCards,
} from "./transport.js";
import {
  createLocalC10BrowserTransport,
  type LocalBrowserFetch,
} from "./local_browser_executor.js";
import {
  createJllReceiptProducer,
  jllEnumerationCard,
  type JllReceiptMember,
  type JllReceiptPlan,
} from "./strict_detail/jll.js";

export interface JllBrowserCohort {
  readonly sourceKey: "jll";
  /** Digest of this exact ordered, 16-member source cohort and its card parameters. */
  readonly cohortMemberSha256: string;
  readonly transaction: "sale" | "lease";
  readonly propertyType: string;
  readonly page: number;
  readonly members: readonly JllReceiptMember[];
}

export interface JllBrowserRuntimeOptions {
  readonly armSecret: string;
  readonly serviceUrl: string;
  readonly store: ReceiptArtifactStore;
  readonly fetcher?: LocalBrowserFetch;
  /** Supplied by the held Python C10 coordinator; no standalone execution is admitted. */
  readonly coordinatorLock: Readonly<{ assertHeld(): void }>;
}

export interface JllBrowserRun {
  readonly enumeration: unknown;
  readonly members: readonly unknown[];
  readonly scheduler: Readonly<{
    configuredConcurrency: number;
    observedMaxActive: number;
    scheduledMemberCount: number;
  }>;
  readonly accounting: ReturnType<SourceBoundOneShotTransport["requestAccounting"]>;
}

function digestInput(cohort: Omit<JllBrowserCohort, "cohortMemberSha256">): object {
  return {
    sourceKey: cohort.sourceKey,
    transaction: cohort.transaction,
    propertyType: cohort.propertyType,
    page: cohort.page,
    members: cohort.members.map((member) => ({
      key: member.key,
      providerId: member.providerId,
      canonicalUrl: member.canonicalUrl,
    })),
  };
}

export function jllBrowserCohortSha256(cohort: Omit<JllBrowserCohort, "cohortMemberSha256">): string {
  return canonicalSha256(digestInput(cohort));
}

function assertCohort(cohort: JllBrowserCohort): void {
  if (
    cohort.sourceKey !== "jll"
    || cohort.members.length !== 16
    || cohort.cohortMemberSha256 !== jllBrowserCohortSha256(cohort)
    || !Number.isInteger(cohort.page)
    || cohort.page < 1
    || !cohort.propertyType
    || new Set(cohort.members.map((member) => member.key)).size !== 16
  ) {
    throw new C10ReceiptError("JLL browser cohort is not the immutable reviewed 16-member cohort");
  }
}

function memberCard(member: JllReceiptMember, index: number): RequestCardInput {
  const url = new URL(member.canonicalUrl);
  if (url.protocol !== "https:" || url.host !== "property.jll.com" || url.search || url.hash) {
    throw new C10ReceiptError("JLL browser member URL is not an exact reviewed property route");
  }
  return {
    id: `jll-member-${index}`,
    sourceKey: "jll",
    stage: "member",
    method: "GET",
    url: url.toString().replace(/\/$/, ""),
    allowedHost: "property.jll.com",
    headers: Object.freeze({ accept: "text/html,application/xhtml+xml" }),
    contentType: null,
    body: null,
    browserBootstrapUrl: "https://property.jll.com/",
    cacheMode: "no-store",
    timeoutMs: 30_000,
    maxBytes: 2 * 1024 * 1024,
  };
}

/** All 17 cards are frozen before execution, including cards appended to the receipt graph later. */
export function jllBrowserCardRegistry(cohort: JllBrowserCohort): ReadonlyMap<string, RequestCard> {
  assertCohort(cohort);
  const enumeration = jllEnumerationCard(cohort);
  return allowlistedCards("jll", [enumeration, ...cohort.members.map(memberCard)]);
}

function enumerationCards(cohort: JllBrowserCohort): ReadonlyMap<string, RequestCard> {
  return allowlistedCards("jll", [jllEnumerationCard(cohort)]);
}

function runtime(
  cohort: JllBrowserCohort,
  binding: ReceiptBinding,
  options: JllBrowserRuntimeOptions,
): { producer: ReturnType<typeof createJllReceiptProducer>; transport: SourceBoundOneShotTransport } {
  options.coordinatorLock.assertHeld();
  const cards = jllBrowserCardRegistry(cohort);
  const direct = createLocalC10BrowserTransport("jll", binding, cards, options);
  const transport = new SourceBoundOneShotTransport("jll", binding, enumerationCards(cohort), options.store, direct);
  const plan: JllReceiptPlan = { ...cohort, enumerationCards: [] };
  return { producer: createJllReceiptProducer(plan), transport };
}

async function boundedMembers<T>(
  members: readonly JllReceiptMember[],
  concurrency: number,
  run: (member: JllReceiptMember) => Promise<T>,
): Promise<{ values: readonly T[]; observedMaxActive: number }> {
  if (!Number.isInteger(concurrency) || (concurrency !== 1 && concurrency !== 4 && concurrency !== 10)) {
    throw new C10ReceiptError("JLL browser execution has no reviewed concurrency");
  }
  const values = new Array<T>(members.length);
  let active = 0;
  let observedMaxActive = 0;
  let next = 0;
  const worker = async () => {
    while (true) {
      const index = next++;
      if (index >= members.length) return;
      active += 1;
      observedMaxActive = Math.max(observedMaxActive, active);
      try {
        values[index] = await run(members[index]!);
      } finally {
        active -= 1;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, members.length) }, worker));
  return { values: Object.freeze(values), observedMaxActive };
}

/** One known member, still bound to the immutable 16-member registry and arm token. */
export async function runJllBrowserFidelitySmoke(
  cohort: JllBrowserCohort,
  binding: ReceiptBinding,
  memberKey: string,
  options: JllBrowserRuntimeOptions,
): Promise<JllBrowserRun> {
  assertCohort(cohort);
  const member = cohort.members.find((candidate) => candidate.key === memberKey);
  if (!member) throw new C10ReceiptError("JLL browser smoke member is not in the immutable cohort");
  const execution = runtime(cohort, binding, options);
  const enumeration = await execution.producer.produceEnumerationReceipt({ transport: execution.transport });
  const receipt = await execution.producer.produceMemberReceipt({ transport: execution.transport }, member);
  return Object.freeze({ enumeration, members: Object.freeze([receipt]), scheduler: Object.freeze({ configuredConcurrency: 1, observedMaxActive: 1, scheduledMemberCount: 1 }), accounting: execution.transport.requestAccounting() });
}

/** All 16 members; only P0/P1 concurrency is admitted, and actual saturation must be observed. */
export async function runJllBrowserSaturationCalibration(
  cohort: JllBrowserCohort,
  binding: ReceiptBinding,
  concurrency: 4 | 10,
  options: JllBrowserRuntimeOptions,
): Promise<JllBrowserRun> {
  assertCohort(cohort);
  const execution = runtime(cohort, binding, options);
  const enumeration = await execution.producer.produceEnumerationReceipt({ transport: execution.transport });
  const result = await boundedMembers(cohort.members, concurrency, (member) =>
    execution.producer.produceMemberReceipt({ transport: execution.transport }, member),
  );
  if (result.observedMaxActive !== concurrency) {
    throw new C10ReceiptError("JLL browser saturation did not reach the reviewed concurrency");
  }
  return Object.freeze({
    enumeration,
    members: result.values,
    scheduler: Object.freeze({ configuredConcurrency: concurrency, observedMaxActive: result.observedMaxActive, scheduledMemberCount: cohort.members.length }),
    accounting: execution.transport.requestAccounting(),
  });
}
