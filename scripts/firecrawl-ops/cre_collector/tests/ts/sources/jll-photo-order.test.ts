// AGENTIC-4164: real source enrichment with synthetic detail documents only.
process.argv = [process.argv[0]!, process.argv[1]!];

import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  enrichJllListing,
  jllDetailCachePath,
  writeJllDetailCache,
} from "../../../sources/jll.js";
import { firecrawl } from "../../../lib/scrape.js";
import type { ScrapedDoc } from "../../../types.js";

type PhotoCase = {
  native?: unknown;
  base?: Array<string | null | undefined>;
  harvested: string[];
  cache?: boolean;
};

function sourceFixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "jll-photo-order-"));
  const keys = [
    "JLL_DETAIL_CACHE_DIR",
    "JLL_DETAIL_CACHE_MIN_CACHED_AT",
    "CRE_REFRESH_GENERATION",
    "CRE_REFRESH_STARTED_AT",
    "CRE_REQUIRE_FRESH_DETAILS",
  ];
  const previous = keys.map((key) => [key, process.env[key]] as const);
  process.env.JLL_DETAIL_CACHE_DIR = directory;
  delete process.env.JLL_DETAIL_CACHE_MIN_CACHED_AT;
  delete process.env.CRE_REFRESH_GENERATION;
  delete process.env.CRE_REFRESH_STARTED_AT;
  delete process.env.CRE_REQUIRE_FRESH_DETAILS;
  t.after(() => {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(globalThis, "fetch", async () => {
    throw new Error("photo-order test attempted network dispatch");
  });
  let document: ScrapedDoc;
  let cacheOnly = false;
  t.mock.method(firecrawl, "scrape", async () => {
    if (cacheOnly) throw new Error("cached fixture attempted a rendered request");
    return document;
  });
  const url = "https://property.jll.com/listings/photo-order";
  return async ({ native = [], base, harvested, cache }: PhotoCase) => {
    cacheOnly = cache === true;
    document = {
      rawHtml: `<script id="__NEXT_DATA__">${JSON.stringify({
        props: {
          pageProps: {
            property: {
              id: "photo-order",
              pageUrl: "/listings/photo-order",
              images: native,
              hidePrice: false,
            },
            brokers: [],
          },
        },
      })}</script>`,
      markdown: "Synthetic property description.",
      links: [],
      images: harvested,
      attributes: [],
    };
    if (cache) writeJllDetailCache(url, document);
    else rmSync(jllDetailCachePath(url), { force: true });
    const listing = await enrichJllListing({
      id: "photo-order",
      url,
      transactionType: "Lease",
      photos: base,
    });
    assert.equal(listing.detailError, undefined);
    return listing;
  };
}

test("JLL preserves native gallery primary and order while sorting supplementary photos", async (t) => {
  const enrich = sourceFixture(t);
  const listing = await enrich({
    native: ["https://images.example/Z.jpg", "https://images.example/A.jpg"],
    base: ["https://images.example/old.jpg"],
    harvested: ["https://images.example/c.jpg", "https://images.example/b.jpg"],
  });
  assert.deepEqual(listing.photos, [
    "https://images.example/Z.jpg",
    "https://images.example/A.jpg",
    "https://images.example/b.jpg",
    "https://images.example/c.jpg",
  ]);
});

test("JLL converges shuffled rendered and cached supplementary photos", async (t) => {
  const enrich = sourceFixture(t);
  const native = ["https://images.example/primary.jpg"];
  const rendered = await enrich({
    native,
    harvested: ["https://images.example/z.jpg", "https://images.example/a.jpg"],
  });
  const cached = await enrich({
    native,
    harvested: ["https://images.example/a.jpg", "https://images.example/z.jpg"],
    cache: true,
  });
  const expected = [
    "https://images.example/primary.jpg",
    "https://images.example/a.jpg",
    "https://images.example/z.jpg",
  ];
  assert.deepEqual(rendered.photos, expected);
  assert.deepEqual(cached.photos, expected);
});

test("JLL preserves base gallery primary and order when the detail gallery is absent", async (t) => {
  const enrich = sourceFixture(t);
  const listing = await enrich({
    base: ["https://images.example/z.jpg", "https://images.example/b.jpg"],
    harvested: ["https://images.example/d.jpg", "https://images.example/a.jpg"],
  });
  assert.deepEqual(listing.photos, [
    "https://images.example/z.jpg",
    "https://images.example/b.jpg",
    "https://images.example/a.jpg",
    "https://images.example/d.jpg",
  ]);
});

test("JLL keeps existing relative URL resolution, filtering and duplicate identities", async (t) => {
  const enrich = sourceFixture(t);
  const listing = await enrich({
    native: [
      " https://images.example/z.jpg ",
      "https://images.example/z.jpg",
    ],
    harvested: [
      "/images/b.jpg",
      "https://images.example/z.jpg",
      "/images/a.jpg",
      "https://property.jll.com/images/b.jpg",
      "javascript:bad",
    ],
  });
  assert.deepEqual(listing.photos, [
    "https://images.example/z.jpg",
    "https://property.jll.com/images/a.jpg",
    "https://property.jll.com/images/b.jpg",
  ]);
});

test("JLL uses locale-independent URL ordering for the supplementary tail", async (t) => {
  const enrich = sourceFixture(t);
  const listing = await enrich({
    native: ["https://images.example/primary.jpg"],
    harvested: [
      "https://images.example/a.jpg",
      "https://images.example/Z.jpg",
      "https://images.example/A.jpg",
    ],
  });
  assert.deepEqual(listing.photos, [
    "https://images.example/primary.jpg",
    "https://images.example/A.jpg",
    "https://images.example/Z.jpg",
    "https://images.example/a.jpg",
  ]);
});

test("JLL retains legacy primary and order when no native or base gallery anchors it", async (t) => {
  const enrich = sourceFixture(t);
  const listing = await enrich({
    harvested: ["https://images.example/z.jpg", "https://images.example/a.jpg"],
  });
  assert.deepEqual(listing.photos, [
    "https://images.example/z.jpg",
    "https://images.example/a.jpg",
  ]);
});

test("JLL does not invent an anchor from blank base photos", async (t) => {
  const enrich = sourceFixture(t);
  const listing = await enrich({
    base: [null, " ", undefined],
    harvested: ["https://images.example/z.jpg", "https://images.example/a.jpg"],
  });
  assert.deepEqual(listing.photos, [
    "https://images.example/z.jpg",
    "https://images.example/a.jpg",
  ]);
});

test("JLL keeps an empty image collection empty", async (t) => {
  const enrich = sourceFixture(t);
  const listing = await enrich({ harvested: [] });
  assert.equal(listing.photos, undefined);
});
