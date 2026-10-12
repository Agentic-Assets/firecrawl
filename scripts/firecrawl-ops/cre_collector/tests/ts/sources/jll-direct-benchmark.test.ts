import assert from "node:assert/strict";
import test from "node:test";
import {
  type JllBenchmarkMember,
  type JllBenchmarkTransport,
  type JllArmContext,
  jllDocumentSha256,
  auditJllPairedDocs,
  runJllTransportMatrix,
} from "../../../sources/jll-direct-benchmark.js";
import { jllDirectHtmlDoc } from "../../../sources/jll-direct.js";
const members = Array.from({ length: 8 }, (_, index) => ({
  sampleId: `sample-${index + 1}`,
  baseRow: {
    id: String(index + 1),
    url: `https://property.jll.com/listings/property-${index + 1}`,
    hidePrice: false,
    inventoryObservedAt: "2026-10-12T10:00:01.000Z",
  },
  providerId: String(index + 1),
  canonicalUrl: `https://property.jll.com/listings/property-${index + 1}`,
}));
const cohort = (selected = members) => ({
  source: "jll" as const,
  inventorySha256: "a".repeat(64),
  inventoryStartedAt: "2026-10-12T10:00:00.000Z",
  inventoryFinishedAt: "2026-10-12T10:00:02.000Z",
  members: selected,
});
function doc(member: JllBenchmarkMember = members[0]!) {
  const property = {
    id: member.providerId,
    pageUrl: new URL(member.canonicalUrl).pathname,
    title: "Office Property",
    images: ["https://assets.example/photo.jpg"],
    brochures: ["https://assets.example/brochure.pdf"],
    description: "Full property details",
  };
  return jllDirectHtmlDoc(
    `<html><body><h1>Office Property</h1><p>Full property details</p><a href="https://assets.example/brochure.pdf">Brochure</a><a href="https://broker.example/profile">Broker profile</a><img src="https://assets.example/photo.jpg"><iframe src="https://my.matterport.com/show/?m=abcdefg"></iframe><script id="__NEXT_DATA__">${JSON.stringify({ props: { pageProps: { property, brokers: [{ name: "Official Broker", email: "broker@jll.com" }] } } })}</script></body></html>`,
    member.canonicalUrl,
  );
}
// Synthetic projection fixtures test orchestration; real parser coverage lives
// in jll-direct-projection.test.ts and fresh provider proof is separate.
function projection(member: JllBenchmarkMember = members[0]!) {
  return {
    kind: "offline_generation_cache_projection" as const,
    listing: {
      id: member.providerId,
      url: member.canonicalUrl,
      name: "Office Property",
      freshnessProvenance: {
        method: "jll_detail",
        cacheDisposition: "generation_cache",
        generationId: "fixture",
        detailScope: "detail_page",
      },
    },
    brokers: [],
    acquisition: {
      method: "jll_detail",
      observedAt: new Date().toISOString(),
      generationId: "fixture",
      cacheDisposition: "live" as const,
    },
  };
}
function captureFixture(
  transport: JllBenchmarkTransport,
  member: JllBenchmarkMember,
  context: JllArmContext,
  overrides: Partial<ReturnType<typeof doc>> = {},
) {
  const captured = { ...doc(member), ...overrides };
  const observation = {
    method: transport === "direct" ? "jll_direct_candidate" : "jll_detail",
    observedAt: new Date().toISOString(),
    generationId: context.armId,
    cacheDisposition: "live" as const,
  };
  captured.detailObservation = observation;
  return {
    doc: captured,
    projection: {
      ...projection(member),
      acquisition: observation,
      documentSha256: jllDocumentSha256(captured),
    },
    attempts: 1,
    cacheDisposition: "live" as const,
  };
}
test("paired audit uses real document Markdown and all structured/harvested channels", () => {
  const captured = doc();
  const audit = auditJllPairedDocs(
    members[0]!,
    captured,
    structuredClone(captured),
  );
  assert.equal(audit.equivalent, true);
  assert.equal(audit.counts.direct.documents, 1);
  assert.equal(audit.counts.direct.media, 1);
  assert.equal(audit.counts.direct.links, 1);
  assert.equal(audit.counts.direct.images, 1);
  const direct = { ...captured, markdown: "Office Property" };
  const loss = auditJllPairedDocs(members[0]!, direct, captured);
  assert.equal(loss.equivalent, false);
  assert.ok(loss.omissions.markdownTokens > 0);
  assert.equal(
    JSON.stringify(loss).includes("Full property details"),
    false,
    "body/Markdown text is not persisted in audit",
  );
});
test("ordered Markdown audit rejects reordered negation despite identical token coverage", () => {
  const captured = doc();
  const rendered = {
    ...captured,
    markdown: "Office is not available. Retail is available.",
  };
  for (const markdown of [
    "Office is available. Retail is not available.",
    "Retail is available. Office is not available.",
  ]) {
    const audit = auditJllPairedDocs(
      members[0]!,
      { ...captured, markdown },
      rendered,
    );
    assert.equal(audit.omissions.markdownTokens, 0);
    assert.equal(audit.markdownOrderedVisibleEqual, false);
    assert.equal(audit.equivalent, false);
  }
  const audit = auditJllPairedDocs(
    members[0]!,
    {
      ...captured,
      markdown: "Office   is not available.\nRetail is available.",
    },
    rendered,
  );
  assert.equal(audit.markdownOrderedVisibleEqual, true);
  assert.equal(audit.equivalent, true);
});
test("paired audit rejects wrong response/primary identity and changed native broker/property payloads", () => {
  const captured = doc();
  for (const direct of [
    { ...captured, metadata: { ...captured.metadata, statusCode: 403 } },
    {
      ...captured,
      metadata: { ...captured.metadata, sourceURL: members[1]!.canonicalUrl },
    },
    { ...captured, rawHtml: captured.rawHtml.replace('"id":"1"', '"id":"2"') },
    { ...captured, rawHtml: "<h1>Challenge</h1>" },
  ])
    assert.throws(
      () => auditJllPairedDocs(members[0]!, direct, captured),
      /identity/,
    );
  for (const replacement of ["Official Broker", "Full property details"]) {
    const changed = {
      ...captured,
      rawHtml: captured.rawHtml.replaceAll(replacement, "Different"),
    };
    assert.equal(
      auditJllPairedDocs(members[0]!, changed, captured).nativePayloadEqual,
      false,
    );
  }
});
test("paired audit detects rendered-only document/media/link/gallery channels without exceptions for chrome", () => {
  const captured = doc();
  const direct = jllDirectHtmlDoc(
    captured.rawHtml
      .replace(/<a[^>]*>[\s\S]*?<\/a>/g, "")
      .replace(/<img[^>]*>/g, "")
      .replace(/<iframe[^>]*><\/iframe>/g, ""),
    members[0]!.canonicalUrl,
  );
  const audit = auditJllPairedDocs(members[0]!, direct, captured);
  for (const key of ["documents", "media", "links", "images"] as const)
    assert.equal(audit.omissions[key], 1, key);
  assert.equal(audit.equivalent, false);
});
test("matrix captures the same cold cohort at C1/2/4/8 and measures actual client overlap", async () => {
  let active = 0,
    peak = 0,
    calls = 0;
  const peaks: number[] = [];
  const result = await runJllTransportMatrix({
    cohort: cohort(),
    signal: new AbortController().signal,
    capture: async (transport, member, _signal, context) => {
      active++;
      peak = Math.max(peak, active);
      calls++;
      await new Promise((resolve) => setImmediate(resolve));
      active--;
      if (calls % members.length === 0 && active === 0) {
        peaks.push(peak);
        peak = 0;
      }
      return captureFixture(transport, member, context);
    },
  });
  assert.equal(result.complete, true);
  assert.equal(result.kind, "jll_transport_observation_not_admission");
  assert.equal(calls, members.length * 8);
  assert.ok(
    result.cells.every((cell) =>
      cell.measurements.every(
        (row) => row.providerAttempts === null && row.providerRetries === null,
      ),
    ),
    "synthetic/unmeasured provider counts remain unknown",
  );
  assert.deepEqual(peaks, [1, 1, 2, 2, 4, 4, 8, 8]);
  assert.deepEqual(
    result.cells.map((cell) => cell.clientConcurrency),
    [1, 2, 4, 8],
  );
});
test("cohort hash covers original inventory controls and callbacks cannot mutate later cells", async () => {
  const fixed = cohort();
  let captures = 0;
  const result = await runJllTransportMatrix({
    cohort: fixed,
    signal: new AbortController().signal,
    capture: async (transport, member, _signal, context) => {
      assert.equal(member.baseRow.hidePrice, false);
      assert.equal(
        member.baseRow.inventoryObservedAt,
        "2026-10-12T10:00:01.000Z",
      );
      member.baseRow.hidePrice = true;
      captures++;
      return captureFixture(transport, member, context);
    },
  });
  assert.equal(result.complete, true);
  assert.equal(captures, 64);
  assert.match(result.cohortSha256, /^[a-f0-9]{64}$/);
  assert.equal(fixed.members[0]!.baseRow.hidePrice, false);
  const changed = structuredClone(fixed);
  changed.members[0]!.baseRow.hidePrice = true;
  const other = await runJllTransportMatrix({
    cohort: changed,
    signal: new AbortController().signal,
    capture: async (transport, member, _signal, context) =>
      captureFixture(transport, member, context),
  });
  assert.notEqual(other.cohortSha256, result.cohortSha256);
});
test("inventory clocks and row observation binding reject before transport", async () => {
  for (const change of [
    { inventoryStartedAt: "invalid" },
    { inventoryFinishedAt: "2026-10-11T00:00:00.000Z" },
    { inventoryStartedAt: "2026-10-12T10:00:01.500Z" },
  ]) {
    await assert.rejects(
      runJllTransportMatrix({
        cohort: { ...cohort(), ...change },
        signal: new AbortController().signal,
        capture: async () => {
          assert.fail("invalid inventory dispatched");
        },
      }),
    );
  }
});
test("missing real-enricher projection cannot complete a document-only matrix", async () => {
  const result = await runJllTransportMatrix({
    cohort: cohort(),
    signal: new AbortController().signal,
    capture: async (transport, member, _signal, context) => {
      const { projection: _projection, ...row } = captureFixture(
        transport,
        member,
        context,
      );
      return row;
    },
  });
  assert.equal(result.complete, false);
  assert.equal(result.stoppedAtConcurrency, 1);
  assert.equal(
    result.cells[0]!.audits.every(
      (audit) => audit.sourceProjection === null && !audit.equivalent,
    ),
    true,
  );
});
test("normalized source fact difference stops before higher concurrency", async () => {
  const result = await runJllTransportMatrix({
    cohort: cohort(),
    signal: new AbortController().signal,
    capture: async (transport, member, _signal, context) => {
      const row = captureFixture(transport, member, context);
      if (transport === "direct") row.projection.listing.name = "Different";
      return row;
    },
  });
  assert.equal(result.complete, false);
  assert.equal(result.stoppedAtConcurrency, 1);
  assert.equal(
    result.cells[0]!.audits.every(
      (audit) => !audit.sourceProjection!.normalizedFactsEqual,
    ),
    true,
  );
});
test("fidelity regression stops before higher concurrency", async () => {
  let calls = 0;
  const result = await runJllTransportMatrix({
    cohort: cohort(members.slice(0, 2)),
    signal: new AbortController().signal,
    capture: async (transport, member, _signal, context) => {
      calls++;
      return captureFixture(
        transport,
        member,
        context,
        transport === "direct" ? { markdown: "Office" } : {},
      );
    },
  });
  assert.equal(result.complete, false);
  assert.equal(result.failure, "fidelity_regression");
  assert.equal(result.stoppedAtConcurrency, 1);
  assert.equal(calls, 4);
});
test("retried or cached observations cannot be called cold and terminate the matrix", async () => {
  for (const change of [
    { attempts: 2 },
    { cacheDisposition: "generation_cache" },
  ]) {
    let calls = 0;
    const result = await runJllTransportMatrix({
      cohort: cohort(),
      signal: new AbortController().signal,
      capture: async (transport, member, _signal, context) => {
        calls++;
        return {
          doc: doc(member),
          attempts: 1,
          cacheDisposition: "live",
          ...change,
        } as never;
      },
    });
    assert.equal(result.complete, false);
    const cell = result.cells[0]!;
    assert.equal("partial" in cell ? cell.partial?.completed : undefined, 0);
    assert.equal(calls, 1);
  }
});
test("arm acquisition rejects stale/future/method/generation and swapped projection evidence", async () => {
  for (const fault of [
    "stale",
    "future",
    "method",
    "cache",
    "empty_generation",
    "mixed_generation",
    "reused_generation",
    "projection_observation",
    "projection_body",
  ] as const) {
    let calls = 0;
    const result = await runJllTransportMatrix({
      cohort: cohort(),
      signal: new AbortController().signal,
      capture: async (transport, member, _signal, context) => {
        calls++;
        const row = captureFixture(transport, member, context);
        const observation = row.doc.detailObservation!;
        if (fault === "stale")
          observation.observedAt = new Date(
            Date.parse(context.startedAt) - 1,
          ).toISOString();
        if (fault === "future")
          observation.observedAt = new Date(Date.now() + 60000).toISOString();
        if (fault === "method")
          observation.method =
            transport === "direct" ? "jll_detail" : "jll_direct_candidate";
        if (fault === "cache")
          observation.cacheDisposition = "generation_cache";
        if (fault === "empty_generation") observation.generationId = "";
        if (fault === "mixed_generation")
          observation.generationId = member.sampleId;
        if (fault === "reused_generation") observation.generationId = "reused";
        // Bind valid metadata changes to projection too, so each specific guard
        // (not the projection mismatch guard) must reject the invalid acquisition.
        row.projection.acquisition = {
          ...observation,
          generationId: observation.generationId!,
          cacheDisposition: "live",
        };
        row.projection.documentSha256 = jllDocumentSha256(row.doc);
        if (fault === "projection_observation")
          row.projection.acquisition.observedAt = "2000-01-01T00:00:00.000Z";
        if (fault === "projection_body")
          row.projection.documentSha256 = "0".repeat(64);
        return row;
      },
    });
    assert.equal(result.complete, false, fault);
    assert.equal(result.stoppedAtConcurrency, 1, fault);
    assert.equal(result.failure, "capture_failed_or_cancelled", fault);
    assert.ok(calls <= members.length + 1, fault);
  }
});
test("cell timeout aborts outstanding transport and prevents subsequent requests", async () => {
  let calls = 0;
  let signal!: AbortSignal;
  const result = await runJllTransportMatrix({
    cohort: cohort(),
    signal: new AbortController().signal,
    cellTimeoutMs: 1000,
    capture: async (_, __, childSignal) => {
      calls++;
      signal = childSignal;
      return new Promise(() => {});
    },
  });
  assert.equal(result.complete, false);
  assert.equal(result.stoppedAtConcurrency, 1);
  assert.equal(signal.aborted, true);
  assert.equal(calls, 1);
});
test("invalid or duplicate cohort rejects before any transport", async () => {
  for (const invalid of [
    [],
    [members[0]!, members[0]!],
    [
      {
        ...members[0]!,
        canonicalUrl: members[0]!.canonicalUrl + "?token=secret",
      },
    ],
  ]) {
    await assert.rejects(
      runJllTransportMatrix({
        cohort: cohort(invalid),
        signal: new AbortController().signal,
        capture: async () => {
          assert.fail("invalid dispatch");
        },
      }),
    );
  }
});
