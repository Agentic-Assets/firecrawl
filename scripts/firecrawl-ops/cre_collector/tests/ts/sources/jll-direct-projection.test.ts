import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { jllDirectHtmlDoc } from "../../../sources/jll-direct.js";
import {
  auditOfflineJllProjections,
  withOfflineJllProjection,
} from "../../../sources/jll-direct-projection.js";
const member = {
  sampleId: "sample-1",
  providerId: "123",
  canonicalUrl: "https://property.jll.com/listings/office-property",
  baseRow: {
    id: "123",
    url: "https://property.jll.com/listings/office-property",
    transactionType: "sale",
    hidePrice: false,
    inventoryObservedAt: "2026-10-12T10:00:00.000Z",
    jllSearchResult: { hidePrice: false },
  },
};
function doc(hidePrice = false) {
  const captured = jllDirectHtmlDoc(
    `<html><body><h1>Office Property</h1><p>Office description</p><script id="__NEXT_DATA__">${JSON.stringify(
      {
        props: {
          pageProps: {
            property: {
              id: "123",
              pageUrl: "/listings/office-property",
              title: "Office Property",
              address: "1 Main Street",
              city: "Miami",
              state: "FL",
              hidePrice,
              salePrice: { amount: 1250000, currency: "USD" },
              description: "Office description",
            },
            brokers: [
              {
                firstName: "Real",
                lastName: "Broker",
                email: "broker@jll.com",
                phone: "1234567890",
              },
            ],
          },
        },
      },
    )}</script></body></html>`,
    member.canonicalUrl,
  );
  captured.detailObservation = {
    observedAt: new Date().toISOString(),
    generationId: "offline-audit-generation",
    method: "jll_direct_candidate",
    cacheDisposition: "live",
  };
  return captured;
}
async function isolated(run: () => Promise<void>) {
  const root = mkdtempSync(join(tmpdir(), "jll-offline-projection-"));
  const keys = [
    "JLL_DETAIL_CACHE_DIR",
    "CRE_REFRESH_GENERATION",
    "CRE_REQUIRE_FRESH_DETAILS",
    "CRE_REFRESH_STARTED_AT",
    "CRE_SCRAPE_MAX_ATTEMPTS",
    "JLL_DETAIL_CACHE_MIN_CACHED_AT",
  ];
  const before = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  Object.assign(process.env, {
    JLL_DETAIL_CACHE_DIR: root,
    CRE_REFRESH_GENERATION: "offline-audit-generation",
    CRE_REQUIRE_FRESH_DETAILS: "1",
    CRE_REFRESH_STARTED_AT: new Date(Date.now() - 1000).toISOString(),
    CRE_SCRAPE_MAX_ATTEMPTS: "1",
  });
  try {
    await run();
  } finally {
    for (const key of keys) {
      if (before[key] === undefined) delete process.env[key];
      else process.env[key] = before[key];
    }
    rmSync(root, { recursive: true, force: true });
  }
}
test("offline projection uses real source enrichment, resolves brokers, and never relabels replay as live", async () =>
  isolated(async () => {
    const originalFetch = globalThis.fetch;
    const directDoc = doc();
    await withOfflineJllProjection(async (project) => {
      const direct = await project(member, directDoc);
      const renderedDoc = structuredClone(directDoc);
      renderedDoc.detailObservation!.method = "jll_detail";
      const rendered = await project(member, renderedDoc);
      assert.equal(direct.kind, "offline_generation_cache_projection");
      assert.equal(direct.listing.name, "Office Property");
      assert.equal(direct.listing.city, "Miami");
      assert.equal(direct.listing.markdown, directDoc.markdown);
      assert.equal(
        direct.listing.freshnessProvenance.cacheDisposition,
        "generation_cache",
      );
      assert.deepEqual(direct.acquisition, directDoc.detailObservation);
      assert.equal(
        auditOfflineJllProjections(direct, rendered).normalizedFactsEqual,
        true,
      );
      const differentMethod = structuredClone(rendered);
      differentMethod.listing.freshnessProvenance.method =
        "jll_direct_candidate";
      assert.equal(
        auditOfflineJllProjections(direct, differentMethod)
          .normalizedFactsEqual,
        true,
      );
      assert.equal(
        auditOfflineJllProjections(direct, differentMethod).projectionProvenance
          .rendered.method,
        "jll_direct_candidate",
      );
      const reorderedRegistry = structuredClone(rendered);
      const oldId = reorderedRegistry.listing.brokerIds[0];
      reorderedRegistry.brokers = [null, reorderedRegistry.brokers[oldId]];
      reorderedRegistry.listing.brokerIds = [1];
      assert.equal(
        auditOfflineJllProjections(direct, reorderedRegistry)
          .normalizedFactsEqual,
        true,
      );
      for (const field of ["name", "markdown", "transactionType"]) {
        const changed = structuredClone(rendered);
        changed.listing[field] = "Different";
        assert.equal(
          auditOfflineJllProjections(direct, changed).normalizedFactsEqual,
          false,
          field,
        );
      }
      const corrupt = structuredClone(rendered);
      corrupt.listing.brokerIds = [999999];
      assert.throws(
        () => auditOfflineJllProjections(direct, corrupt),
        /broker_reference/,
      );
    });
    assert.equal(globalThis.fetch, originalFetch);
  }));
test("offline parser preserves native and source-index withholding controls", async () =>
  isolated(async () => {
    await withOfflineJllProjection(async (project) => {
      for (const sourceWithheld of [false, true]) {
        const base = structuredClone(member);
        base.baseRow.hidePrice = sourceWithheld;
        base.baseRow.jllSearchResult.hidePrice = sourceWithheld;
        const result = await project(base, doc(!sourceWithheld));
        assert.equal(result.listing.jllDetail.pricing.visibility, "withheld");
        assert.equal(result.listing.salePrice, undefined);
        assert.equal(result.listing.markdown, undefined);
        assert.equal(result.listing.description, undefined);
      }
    });
  }));
test("offline missing cache/fallback cannot dispatch, and restores fetch after failure", async () =>
  isolated(async () => {
    const originalFetch = globalThis.fetch;
    let actualDispatches = 0;
    globalThis.fetch = async () => {
      actualDispatches++;
      throw new Error("must never call real transport");
    };
    try {
      await withOfflineJllProjection(async (project) => {
        const stale = doc();
        process.env.JLL_DETAIL_CACHE_MIN_CACHED_AT = new Date(
          Date.now() + 60000,
        ).toISOString();
        await assert.rejects(
          project(member, stale),
          /offline_projection_dispatch_blocked/,
        );
        await assert.rejects(
          project({ ...member, providerId: "999" }, doc()),
          /input_binding/,
        );
        const unobserved = doc();
        delete unobserved.detailObservation;
        await assert.rejects(project(member, unobserved), /input_binding/);
      });
      assert.equal(actualDispatches, 0);
      assert.notEqual(globalThis.fetch, originalFetch);
    } finally {
      globalThis.fetch = originalFetch;
    }
  }));
test("offline projection refuses overlapping writes for the same source identity", async () =>
  isolated(async () => {
    await withOfflineJllProjection(async (project) => {
      const first = project(member, doc());
      await assert.rejects(project(member, doc()), /concurrent_same_url/);
      await first;
    });
  }));
test("offline projection rejects shared or reused cache before replacing fetch", async () =>
  isolated(async () => {
    const originalFetch = globalThis.fetch;
    await withOfflineJllProjection(async (project) => {
      await project(member, doc());
    });
    await assert.rejects(
      withOfflineJllProjection(async () => {}),
      /cache_not_empty_owned_directory/,
    );
    assert.equal(globalThis.fetch, originalFetch);
  }));
