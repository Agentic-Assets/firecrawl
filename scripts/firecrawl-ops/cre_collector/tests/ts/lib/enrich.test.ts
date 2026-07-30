// Isolate argv before lib/enrich.ts loads lib/config.ts (strict parseArgs runs
// at import time; an unknown flag from the test runner would throw otherwise).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import {
  ENRICHERS,
  REGISTERED_BUILDOUT_ENRICHERS,
  groupEnrichItems,
  resolveEnricher,
  runEnrichGroups,
  parseGenericJsonLd,
  type EnrichItem,
  type SourceEnricher,
} from "../../../lib/enrich.js";
import { REGISTERED_BUILDOUT_SOURCE_KEYS } from "../../../sources/buildout-registry.js";
import {
  parseColliersMainDetail,
  type ColliersMainEntry,
} from "../../../sources/colliers-main.js";
import type { ScrapedDoc } from "../../../types.js";

// ---------------------------------------------------------------------------
// Enricher registry: the sources with a proven targeted source path use that
// path. cbre stays absent because its enumeration response is already canonical.
// ---------------------------------------------------------------------------

test("ENRICHERS registry uses targeted source paths and excludes cbre", () => {
  assert.deepEqual(
    Object.keys(ENRICHERS).sort(),
    [
      "avison-young",
      "colliers-main",
      "jll-investor",
      "kidder-mathews",
      "lee-associates",
      "marcus-millichap",
      "srs",
      "svn",
      ...REGISTERED_BUILDOUT_SOURCE_KEYS,
    ].sort()
  );
  assert.equal(ENRICHERS.cbre, undefined); // enumeration-only; no detail endpoint
});

test("resolveEnricher returns bespoke for registered keys, generic otherwise", () => {
  assert.equal(resolveEnricher("colliers-main").label, "bespoke");
  assert.equal(resolveEnricher("jll-investor").label, "bespoke");
  // svn / lee-associates resolve to the bespoke Buildout Tier-B enricher.
  assert.equal(resolveEnricher("svn").label, "bespoke");
  assert.equal(resolveEnricher("lee-associates").label, "bespoke");
  assert.equal(resolveEnricher("marcus-millichap").label, "bespoke");
  assert.equal(resolveEnricher("avison-young").label, "bespoke");
  assert.equal(resolveEnricher("srs").label, "bespoke");
  assert.equal(resolveEnricher("kidder-mathews").label, "bespoke");
  for (const sourceKey of REGISTERED_BUILDOUT_SOURCE_KEYS) {
    assert.equal(resolveEnricher(sourceKey).label, "bespoke");
    assert.ok(REGISTERED_BUILDOUT_ENRICHERS[sourceKey]);
  }
  // cbre and any unregistered key fall through to the generic fallback.
  assert.equal(resolveEnricher("cbre").label, "generic");
  assert.equal(resolveEnricher("transwestern").label, "generic");
});

// ---------------------------------------------------------------------------
// --enrich-input grouping + artifact shape (runMeta.mode === "enrich" path):
// items group by sourceKey, and every emitted listing echoes its input url.
// A fake enricher keeps this no-network while exercising the real grouping +
// dispatch + echo-guard collect.ts --enrich-input runs.
// ---------------------------------------------------------------------------

function fakeEnricher(): SourceEnricher {
  // Echoes each item's url (the enricher contract) plus a marker field, and
  // intentionally emits one row WITHOUT a url to prove the echo-guard drops it.
  return {
    async enrich(items: EnrichItem[]) {
      const rows = items.map((it) => ({ id: it.externalId, url: it.url, fake: true }));
      rows.push({ id: "no-url", url: undefined as any, fake: true });
      return rows;
    },
  };
}

test("groupEnrichItems groups by sourceKey and drops items missing key or url", () => {
  const items: EnrichItem[] = [
    { sourceKey: "colliers-main", externalId: "main:usa1", url: "https://x/a" },
    { sourceKey: "colliers-main", externalId: "main:usa2", url: "https://x/b" },
    { sourceKey: "jll-investor", externalId: "investor:s1", url: "https://x/c" },
    { sourceKey: "colliers-main", externalId: "main:usa3", url: "" }, // no url -> dropped
    { sourceKey: "", externalId: "x", url: "https://x/d" }, // no key -> dropped
  ];
  const groups = groupEnrichItems(items);
  assert.deepEqual([...groups.keys()].sort(), ["colliers-main", "jll-investor"]);
  assert.equal(groups.get("colliers-main")!.length, 2); // the url-less item dropped
  assert.equal(groups.get("jll-investor")!.length, 1);
});

test("runEnrichGroups produces the enrich artifact shape; every listing echoes its input url", async () => {
  const items: EnrichItem[] = [
    { sourceKey: "src-a", externalId: "a1", url: "https://x/a", transaction: "sale" },
    { sourceKey: "src-a", externalId: "a2", url: "https://x/b", transaction: "lease" },
    { sourceKey: "src-b", externalId: "b1", url: "https://x/c" },
  ];
  const groups = groupEnrichItems(items);
  const resolve = () => ({ enricher: fakeEnricher(), label: "generic" });
  const { sources, listings } = await runEnrichGroups(
    groups,
    resolve,
    (key) => `Company ${key}`
  );

  // Assemble the artifact exactly as collect.ts enrichMain does, to assert the
  // documented shape (runMeta.mode === "enrich").
  const artifact = {
    runMeta: { mode: "enrich", enrichInput: "/tmp/claim.json" },
    sources,
    listings,
    totalListings: listings.length,
  };
  assert.equal(artifact.runMeta.mode, "enrich");

  // Echo invariant: every emitted listing carries a url, and that url is one of
  // the input urls (the url-less fake row was dropped by the echo-guard).
  const inputUrls = new Set(items.map((i) => i.url));
  assert.ok(listings.length > 0);
  for (const l of listings) {
    assert.ok(l.url, "every emitted listing must echo a url");
    assert.ok(inputUrls.has(l.url), `listing url ${l.url} must be one of the input urls`);
  }
  // Two source groups, the url-less row dropped from each, so 3 listings total.
  assert.equal(listings.length, 3);
  assert.deepEqual(sources.map((s) => s.sourceKey).sort(), ["src-a", "src-b"]);

  // transactionMode is tagged per input url (sale vs lease echoed through).
  const byUrl = new Map(listings.map((l) => [l.url, l]));
  assert.equal(byUrl.get("https://x/a").transactionMode, "sale");
  assert.equal(byUrl.get("https://x/b").transactionMode, "lease");
  assert.equal(byUrl.get("https://x/c").transactionMode, "sale"); // default
});

test("runEnrichGroups records a per-source error when an enricher throws (matches full-path)", async () => {
  const items: EnrichItem[] = [
    { sourceKey: "boom", externalId: "x1", url: "https://x/a" },
  ];
  const groups = groupEnrichItems(items);
  const throwing: SourceEnricher = {
    async enrich() {
      throw new Error("detail render exploded");
    },
  };
  const { sources, listings } = await runEnrichGroups(
    groups,
    () => ({ enricher: throwing, label: "generic" }),
    (k) => k
  );
  assert.equal(listings.length, 0);
  assert.equal(sources.length, 1);
  assert.match(sources[0].error, /detail render exploded/);
});

// ---------------------------------------------------------------------------
// Generic fallback extraction on a fixture: lift price/name from JSON-LD and
// always echo the input url; native id reconstructed from the folded external id.
// ---------------------------------------------------------------------------

test("parseGenericJsonLd extracts JSON-LD price/name and echoes the input url", () => {
  const html = `
    <html><head>
      <script type="application/ld+json">
        {"@type":"RealEstateListing","name":"123 Main St Office",
         "description":"Class A office",
         "geo":{"latitude":"32.7767","longitude":"-96.7970"},
         "offers":{"@type":"Offer","price":"4250000"}}
      </script>
    </head></html>`;
  const item: EnrichItem = {
    sourceKey: "transwestern",
    externalId: "tx-slug-123",
    url: "https://example.com/p/123",
  };
  const row = parseGenericJsonLd(item, html);
  assert.equal(row.url, "https://example.com/p/123"); // echoes the input url
  assert.equal(row.id, "tx-slug-123"); // no fold prefix to strip
  assert.equal(row.name, "123 Main St Office");
  assert.equal(row.salePriceUsd, 4250000); // numeric coercion of JSON-LD string
  assert.equal(row.latitude, 32.7767);
  assert.equal(row.longitude, -96.797);
  assert.equal(row.genericEnrich.hadJsonLd, true);
  assert.equal(row.genericEnrich.jsonLdType, "RealEstateListing");
});

test("parseGenericJsonLd strips a fold prefix to recover the native id", () => {
  const item: EnrichItem = {
    sourceKey: "colliers-main",
    externalId: "main:usa98765",
    url: "https://www.colliers.com/en/properties/usa98765",
  };
  // No JSON-LD on the page: still echoes url + native id. prune() drops the
  // genericEnrich block when both its fields are nullish/false (jsonLdType=null,
  // hadJsonLd=false), so it is absent here; the url + native id are the invariant.
  const row = parseGenericJsonLd(item, "<html><body>no structured data</body></html>");
  assert.equal(row.id, "usa98765"); // "main:" stripped
  assert.equal(row.url, "https://www.colliers.com/en/properties/usa98765");
  assert.equal(row.genericEnrich, undefined);
});

test("parseGenericJsonLd preserves the url for diagnostics without asserting it is safe to complete", () => {
  const item: EnrichItem = {
    sourceKey: "newmark",
    externalId: "slug-1",
    url: "https://nmrk.com/x",
  };
  const row = parseGenericJsonLd(item, "<html></html>");
  assert.equal(row.url, "https://nmrk.com/x");
  assert.equal(row.id, "slug-1");
});

// ---------------------------------------------------------------------------
// Colliers native-id reconstruction: the colliers-main enricher strips the
// "main:" fold prefix to rebuild the native usa##### id, builds a minimal
// ColliersMainEntry, and parseColliersMainDetail echoes both the native id and
// the input url. Verified on a saved JSON-LD detail fixture (no network).
// ---------------------------------------------------------------------------

function detailDoc(partial: Partial<ScrapedDoc>): ScrapedDoc {
  return { rawHtml: "", markdown: "", links: [], metadata: { statusCode: 200 }, ...partial };
}

test("colliers-main reconstructs the native id from a folded external id and echoes url", () => {
  const foldedExternalId = "main:usa12345";
  const url = "https://www.colliers.com/en/properties/usa12345";
  // Mirror the enricher's reconstruction: strip "main:" to rebuild the native id.
  const nativeId = foldedExternalId.replace(/^main:/, "");
  assert.equal(nativeId, "usa12345");
  const entry: ColliersMainEntry = { url, lastmod: null, id: nativeId };

  const html = `
    <html><body>
      <h1>500 Main St</h1>
      <script type="application/ld+json">
        {"@type":"RealEstateListing",
         "name":"Office For sale — 500 Main St, Dallas, TX 75201, USA | United States | Colliers"}
      </script>
    </body></html>`;
  const listing = parseColliersMainDetail(
    entry,
    detailDoc({ rawHtml: html, markdown: "Building Size: 25,000 SF\n$4,250,000 USD" })
  );

  // The artifact carries the NATIVE id (no "main:" prefix); ingest re-applies the
  // fold prefix. Double-prefixing here would dead-letter the row.
  assert.equal(listing.id, "usa12345");
  assert.ok(!String(listing.id).startsWith("main:"));
  // Completion is URL-keyed: the input url is echoed verbatim onto the row.
  assert.equal(listing.url, url);
  assert.equal(listing.transactionType, "Sale");
  assert.match(listing.city ?? "", /Dallas/);
});

test("colliers-main tombstones a not-found detail page but still echoes native id + url", () => {
  const entry: ColliersMainEntry = {
    url: "https://www.colliers.com/en/properties/usa00000",
    lastmod: "2026-06-01",
    id: "usa00000",
  };
  const listing = parseColliersMainDetail(
    entry,
    detailDoc({ metadata: { statusCode: 404, title: "Property Not Found" } })
  );
  assert.equal(listing.skip, "not_found");
  assert.equal(listing.id, "usa00000"); // native id preserved
  assert.equal(listing.url, entry.url); // url echoed for URL-keyed completion
});
