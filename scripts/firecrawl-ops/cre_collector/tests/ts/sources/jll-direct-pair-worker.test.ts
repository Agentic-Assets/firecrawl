import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import {
  mkdtempSync,
  mkdirSync,
  rmSync,
  writeFileSync,
  readFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { jllDirectHtmlDoc } from "../../../sources/jll-direct.js";
import { jllCohortSha256 } from "../../../sources/jll-direct-benchmark.js";
import { withOfflineJllProjection } from "../../../sources/jll-direct-projection.js";
import {
  executeJllPairWorker,
  renderedPairListingSha256,
  type JllPairRequest,
} from "../../../sources/jll-direct-pair-worker.js";

// Synthetic cell captures exercise actual source parsing. They do not claim a
// network acquisition; the production worker requires coordinator-owned evidence.
async function fixture(run: (f: any) => Promise<void>) {
  const root = mkdtempSync(join(tmpdir(), "jll-pair-worker-"));
  const keys = [
    "JLL_DETAIL_CACHE_DIR",
    "CRE_REFRESH_GENERATION",
    "CRE_REQUIRE_FRESH_DETAILS",
    "CRE_REFRESH_STARTED_AT",
  ];
  const before = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  const start = new Date(Date.now() - 2000).toISOString();
  const members = [1, 2].map((id) => ({
    sampleId: `s${id}`,
    providerId: String(id),
    canonicalUrl: `https://property.jll.com/listings/property-${id}`,
    baseRow: {
      id: String(id),
      url: `https://property.jll.com/listings/property-${id}`,
      transactionType: "sale",
      hidePrice: false,
      inventoryObservedAt: start,
    },
  }));
  const cohort = {
    source: "jll" as const,
    inventorySha256: "a".repeat(64),
    inventoryStartedAt: start,
    inventoryFinishedAt: start,
    members,
  };
  const cohortSha256 = jllCohortSha256(cohort);
  const docs = members.map((member) => {
    const doc = jllDirectHtmlDoc(
      `<html><body><h1>Property ${member.providerId}</h1><p>Useful description.</p><script id="__NEXT_DATA__">${JSON.stringify(
        {
          props: {
            pageProps: {
              property: {
                id: member.providerId,
                pageUrl: new URL(member.canonicalUrl).pathname,
                title: `Property ${member.providerId}`,
                description: "Useful description.",
                hidePrice: false,
              },
              brokers: [],
            },
          },
        },
      )}</script></body></html>`,
      member.canonicalUrl,
    );
    doc.detailObservation = {
      observedAt: new Date(Date.now() - 1000).toISOString(),
      generationId: "rendered-gen",
      method: "jll_detail",
      cacheDisposition: "live",
    };
    return doc;
  });
  const renderedDir = join(root, "rendered");
  // Coordinator retains request/log beside a dedicated empty result directory.
  writeFileSync(join(root, "input.json"), "{}", { mode: 0o600 });
  writeFileSync(join(root, "command.log"), "private child log", {
    mode: 0o600,
  });
  const output = join(root, "output");
  mkdirSync(output, { mode: 0o700 });
  const cache = join(root, "fixture-cache");
  mkdirSync(cache, { mode: 0o700 });
  Object.assign(process.env, {
    JLL_DETAIL_CACHE_DIR: cache,
    CRE_REFRESH_GENERATION: "rendered-gen",
    CRE_REFRESH_STARTED_AT: start,
    CRE_REQUIRE_FRESH_DETAILS: "1",
  });
  const records = new Map<string, any>();
  const client = {
    dispatches: 1,
    statuses: [200],
    failedDispatches: 0,
    redirected: false,
    maxAgeZero: true,
    responseUrlMatches: true,
    bodyIdentity: "jll_native_property",
  };
  const cell = {
    source: "jll",
    cohortSha256,
    generationId: "rendered-gen",
    startedAt: start,
    finishedAt: new Date().toISOString(),
    requestedConcurrency: 2,
    elapsedMs: 1000,
    freshRowsPerMinute: 120,
    rows: members.map((member) => ({
      sampleId: member.sampleId,
      client,
      fidelitySha256: "pending",
    })),
  };
  try {
    await withOfflineJllProjection(async (project) => {
      for (let i = 0; i < members.length; i++) {
        const member = members[i]!,
          doc = docs[i]!;
        const projected = await project(member, doc);
        const listing = projected.listing;
        listing.freshnessProvenance.cacheDisposition = "live";
        cell.rows[i]!.fidelitySha256 = renderedPairListingSha256({
          listing,
          brokers: projected.brokers,
        });
        records.set(
          join(
            renderedDir,
            `capture-${createHash("sha256").update(member.sampleId).digest("hex")}.json`,
          ),
          {
            member,
            listing,
            brokers: projected.brokers,
            client,
            doc: {
              ...doc,
              detailObservedAt: doc.detailObservation!.observedAt,
              generationId: "rendered-gen",
            },
          },
        );
      }
    });
    const input = {
      cohort,
      cohortSha256,
      inventoryArtifact: "/private/inventory.json",
      maxInventoryAgeHours: 1,
      generationId: "rendered-gen",
      concurrency: 2,
    };
    records.set(join(renderedDir, "input.json"), input);
    records.set(join(renderedDir, "cell.json"), cell);
    const request: JllPairRequest = {
      schemaVersion: 1,
      kind: "cre_jll_captured_pair_request",
      renderedCellDirectory: renderedDir,
      inventoryArtifact: input.inventoryArtifact,
      cohortSha256,
      requestedConcurrency: 2,
      directGenerationId: "direct-gen",
      maxInventoryAgeHours: 1,
    };
    Object.assign(process.env, {
      CRE_REFRESH_GENERATION: "direct-gen",
      JLL_DETAIL_CACHE_DIR: "/unused-private-cache",
    });
    const writes = new Map<string, any>();
    let verifies = 0,
      dispatches = 0;
    const io = {
      readWorkerInput: (path: string) => structuredClone(records.get(path)),
      writeWorkerOutput: (path: string, value: unknown) => {
        writes.set(path, structuredClone(value));
        return JSON.stringify(value).length;
      },
      verifyInventory: async () => {
        verifies++;
      },
    };
    const acquire = async (url: string) => {
      dispatches++;
      const index = members.findIndex((member) => member.canonicalUrl === url);
      const doc = structuredClone(docs[index]!);
      doc.detailObservation = {
        observedAt: new Date().toISOString(),
        generationId: "direct-gen",
        method: "jll_direct_candidate",
        cacheDisposition: "live",
      };
      doc.metadata = { ...doc.metadata, providerAttempts: 1 };
      return doc;
    };
    await run({
      root,
      output,
      records,
      request,
      input,
      cell,
      docs,
      members,
      writes,
      io,
      acquire,
      counts: () => ({ dispatches, verifies }),
    });
  } finally {
    for (const key of keys)
      if (before[key] === undefined) delete process.env[key];
      else process.env[key] = before[key];
    rmSync(root, { recursive: true, force: true });
  }
}

test("runnable paired cell uses original rendered arm, fresh direct acquisition and real offline parser", async () =>
  fixture(async (f) => {
    const result = await executeJllPairWorker(
      f.request,
      f.output,
      new AbortController().signal,
      f.io,
      f.acquire,
    );
    assert.equal(result.status, "paired_equivalent");
    assert.deepEqual(f.counts(), { dispatches: 2, verifies: 1 });
    assert.equal(result.renderedArm.startedAt, f.cell.startedAt);
    assert.equal(result.renderedArm.elapsedMs, 1000);
    assert.equal(result.directArm.clientAttempts, 2);
    assert.ok(result.directArm.peakActiveAcquisitions > 0);
    assert.ok(
      result.directArm.peakActiveAcquisitions <= f.request.requestedConcurrency,
    );
    assert.equal("peakActiveRequests" in result.directArm, false);
    assert.match(
      result.directArm.concurrencyBasis,
      /DNS, pacing.*HTTP\/socket inflight concurrency unmeasured/,
    );
    assert.ok(
      result.directArm.acceptedNormalizedElapsedMs >
        result.directArm.acquisitionElapsedMs,
    );
    assert.ok(
      result.directArm.acceptedNormalizedRowsPerMinute <
        result.directArm.acquisitionFreshPagesPerMinute,
    );
    assert.ok(
      Date.parse(result.directArm.normalizedFinishedAt) >=
        Date.parse(result.directArm.finishedAt),
    );
    assert.ok(
      result.pairs.every(
        (p: any) =>
          p.equivalent &&
          p.sourceProjection.projectionProvenance.rendered.cacheDisposition ===
            "live" &&
          p.sourceProjection.projectionProvenance.direct.cacheDisposition ===
            "generation_cache",
      ),
    );
    assert.equal(
      JSON.stringify(f.writes.get(join(f.output, "pair.json"))).includes(
        "Useful description",
      ),
      false,
    );
  }));

for (const fault of [
  "stale",
  "changed-member",
  "retried",
  "wrong-generation",
  "changed-input",
  "inventory-failed",
  "future-cell",
  "duplicate-row",
  "changed-listing",
  "changed-live-method",
  "missing-capture",
  "digest-mismatch",
  "provider-identity",
]) {
  test(`rejects ${fault} rendered evidence before any direct dispatch`, async () =>
    fixture(async (f) => {
      const captured = [...f.records.values()].find((v: any) => v.member);
      if (fault === "missing-capture")
        f.records.delete(
          [...f.records.keys()].find((key: string) => key.includes("capture-")),
        );
      if (fault === "digest-mismatch")
        f.cell.rows[0].fidelitySha256 = "0".repeat(64);
      if (fault === "provider-identity")
        captured.doc.rawHtml = captured.doc.rawHtml.replace(
          '"id":"1"',
          '"id":"999"',
        );
      if (fault === "changed-listing") captured.listing.name = "changed";
      if (fault === "changed-live-method")
        captured.listing.freshnessProvenance.method = "jll_direct_candidate";
      if (fault === "stale")
        captured.doc.detailObservedAt = captured.listing.detailObservedAt =
          new Date(Date.parse(f.cell.startedAt) - 1).toISOString();
      if (fault === "changed-member") captured.member.baseRow.hidePrice = true;
      if (fault === "retried") captured.client.dispatches = 2;
      if (fault === "wrong-generation") captured.doc.generationId = "other";
      if (fault === "changed-input")
        f.input.cohort.members[0].baseRow.hidePrice = true;
      if (fault === "inventory-failed")
        f.io.verifyInventory = async () => {
          throw new Error("secret diagnostic");
        };
      if (fault === "future-cell")
        f.cell.finishedAt = new Date(Date.now() + 60000).toISOString();
      if (fault === "duplicate-row") f.cell.rows[1] = f.cell.rows[0];
      await assert.rejects(
        executeJllPairWorker(
          f.request,
          f.output,
          new AbortController().signal,
          f.io,
          f.acquire,
        ),
      );
      assert.equal(f.counts().dispatches, 0);
    }));
}

test("direct content loss cannot emit paired success", async () =>
  fixture(async (f) => {
    await assert.rejects(
      executeJllPairWorker(
        f.request,
        f.output,
        new AbortController().signal,
        f.io,
        async (url, options) => ({
          ...(await f.acquire(url, options)),
          markdown: "Property",
        }),
      ),
      /pair_document_fidelity_regression/,
    );
    assert.equal(f.writes.has(join(f.output, "pair.json")), false);
  }));

test("cancellation before acquisition does not dispatch", async () =>
  fixture(async (f) => {
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(
      executeJllPairWorker(
        f.request,
        f.output,
        controller.signal,
        f.io,
        f.acquire,
      ),
    );
    assert.equal(f.counts().dispatches, 0);
  }));

test("failed direct sibling is aborted and settled before return", async () =>
  fixture(async (f) => {
    let settled = false;
    await assert.rejects(
      executeJllPairWorker(
        f.request,
        f.output,
        new AbortController().signal,
        f.io,
        async (url, options) => {
          if (url.endsWith("1")) throw new Error("private provider body");
          await new Promise<void>((resolve) => {
            if (options.aborted) resolve();
            else
              options.addEventListener("abort", () => resolve(), {
                once: true,
              });
          });
          settled = true;
          throw new Error("aborted");
        },
      ),
      /direct_acquisition_failed/,
    );
    assert.equal(settled, true);
  }));

for (const fault of ["stale", "future", "wrong-method", "wrong-generation"]) {
  test(`direct ${fault} observation fails paired audit`, async () =>
    fixture(async (f) => {
      await assert.rejects(
        executeJllPairWorker(
          f.request,
          f.output,
          new AbortController().signal,
          f.io,
          async (url) => {
            const doc = await f.acquire(url);
            if (fault === "stale")
              doc.detailObservation.observedAt = f.cell.startedAt;
            if (fault === "future")
              doc.detailObservation.observedAt = new Date(
                Date.now() + 60000,
              ).toISOString();
            if (fault === "wrong-method")
              doc.detailObservation.method = "jll_detail";
            if (fault === "wrong-generation")
              doc.detailObservation.generationId = "other";
            return doc;
          },
        ),
      );
      assert.equal(f.writes.has(join(f.output, "pair.json")), false);
    }));
}

test("concrete CLI uses private bounded I/O and emits sanitized failed result", () => {
  const root = mkdtempSync(join(tmpdir(), "jll-pair-cli-"));
  try {
    const input = join(root, "request.json"),
      output = join(root, "output");
    mkdirSync(output, { mode: 0o700 });
    writeFileSync(
      input,
      JSON.stringify({
        schemaVersion: 99,
        kind: "invalid",
        secret: "private-body-marker",
      }),
      { mode: 0o600 },
    );
    const child = spawnSync(
      process.execPath,
      ["--import", "tsx", "sources/jll-direct-pair-worker.ts"],
      {
        cwd: resolve("."),
        env: {
          ...process.env,
          CRE_JLL_PAIR_INPUT: input,
          CRE_JLL_PAIR_OUTPUT: output,
        },
        timeout: 10000,
        encoding: "utf8",
      },
    );
    assert.equal(child.status, 1);
    assert.equal(child.stdout.includes("private-body-marker"), false);
    assert.equal(child.stderr.includes("private-body-marker"), false);
    const result = JSON.parse(readFileSync(join(output, "pair.json"), "utf8"));
    assert.equal(result.status, "failed");
    assert.equal(result.failure, "pair_request");
    assert.equal(JSON.stringify(result).includes("private-body-marker"), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("first detectable document regression prevents subsequent C1 dispatch", async () =>
  fixture(async (f) => {
    f.request.requestedConcurrency =
      f.input.concurrency =
      f.cell.requestedConcurrency =
        1;
    await assert.rejects(
      executeJllPairWorker(
        f.request,
        f.output,
        new AbortController().signal,
        f.io,
        async (url) => ({
          ...(await f.acquire(url)),
          markdown: "lost useful details",
        }),
      ),
      /pair_document_fidelity_regression/,
    );
    assert.equal(f.counts().dispatches, 1);
    assert.equal(f.writes.has(join(f.output, "pair.json")), false);
  }));
