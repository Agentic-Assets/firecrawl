import assert from "node:assert/strict";
import test from "node:test";
import {
  INTERRA_INVENTORY_URL,
  boundedText,
  fetchInterraInventory,
  mapInterraListing,
  parseInterraInventory,
  srcInterraRealty,
} from "../../../sources/interra-realty.js";

function row(id: number, status: string, overrides: Record<string, any> = {}): any {
  return {
    ID: id,
    post_title: `Property ${id}`,
    post_content: "<p>Investment description.</p>",
    post_modified_gmt: "2026-07-30 14:53:09",
    permalink: `https://interrarealty.com/listing/property-${id}/`,
    thumbnail: `https://interrarealty.com/wp-content/uploads/property-${id}.jpg`,
    terms: [
      {
        taxonomy: "interra_listing_property_type",
        name: "Multifamily",
      },
    ],
    meta: {
      listing_status: status,
      listing_address: "1701 W CHICAGO AVE",
      listing_city: "CHICAGO, IL 60622",
      key_details: "3",
      key_details_0_name: "Sale Price",
      key_details_0_value: "$2,350,000",
      key_details_1_name: "Submarket",
      key_details_1_value: "West Town",
      key_details_2_name: "Number of Units",
      key_details_2_value: "Four (4)",
      listing_highlights: "<ul><li>One</li><li>Two</li></ul>",
    },
    ...overrides,
  };
}

test("Interra complete inventory reconciles all lifecycle buckets", () => {
  const posts = [
    ...Array.from({ length: 29 }, (_, index) => row(index + 1, "available")),
    ...Array.from(
      { length: 24 },
      (_, index) => row(index + 30, "under_contract")
    ),
    ...Array.from({ length: 856 }, (_, index) => row(index + 54, "closed")),
  ];
  const parsed = parseInterraInventory({
    success: true,
    posts,
    has_next_page: false,
  });
  assert.equal(parsed.total, 909);
  assert.deepEqual(parsed.lifecycleCounts, {
    available: 29,
    under_contract: 24,
    closed: 856,
  });
});

test("Interra inventory rejects malformed totals, status, identity, and partial pages", () => {
  assert.throws(
    () => parseInterraInventory({ success: true, posts: [row(1, "available")] }),
    /has_next_page=false/
  );
  assert.throws(
    () =>
      parseInterraInventory({
        success: true,
        posts: [row(1, "available")],
        has_next_page: true,
      }),
    /has_next_page=false/
  );
  assert.throws(
    () =>
      parseInterraInventory({
        success: true,
        posts: [row(1, "mystery")],
        has_next_page: false,
      }),
    /unknown lifecycle/
  );
  assert.throws(
    () =>
      parseInterraInventory({
        success: true,
        posts: [row(1, "available"), row(1, "closed")],
        has_next_page: false,
      }),
    /duplicate ID/
  );
  assert.throws(
    () =>
      parseInterraInventory({
        success: true,
        posts: [row(0, "available")],
        has_next_page: false,
      }),
    /positive integer ID/
  );
  assert.throws(
    () =>
      parseInterraInventory({
        success: true,
        posts: [row(1, "available", { permalink: "https://evil.example/listing/1" })],
        has_next_page: false,
      }),
    /invalid permalink/
  );
});

test("Interra mapping uses stable WP identity and strict freshness provenance", () => {
  const listing = mapInterraListing(
    row(54438, "under_contract"),
    "2026-07-30T18:00:00.000Z",
    "generation-1"
  );
  assert.equal(listing.id, "54438");
  assert.equal(listing.salePriceUsd, 2350000);
  assert.equal(listing.units, 4);
  assert.equal(listing.city, "CHICAGO");
  assert.equal(listing.state, "IL");
  assert.equal(listing.postalCode, "60622");
  assert.equal(listing.statusBadge, "Under Contract");
  assert.equal(listing.inventoryObservedAt, "2026-07-30T18:00:00.000Z");
  assert.equal(listing.detailObservedAt, "2026-07-30T18:00:00.000Z");
  assert.equal(listing.freshnessProvenance.generationId, "generation-1");
  assert.equal(
    listing.freshnessProvenance.detailScope,
    "authoritative_inventory_feed"
  );
  assert.equal(listing.freshnessProvenance.cacheDisposition, "live");
  assert.equal(listing.preserveChildCollections, true);
  assert.equal(listing.rawInterra, undefined);
});

test("Interra mapping rejects malformed key detail counts and duplicate keys", () => {
  assert.throws(
    () =>
      mapInterraListing(
        row(1, "available", { meta: { listing_status: "available", key_details: "bad" } }),
        "2026-07-30T18:00:00.000Z"
      ),
    /malformed key_details/
  );
  const duplicate = row(1, "available");
  duplicate.meta.key_details = "2";
  duplicate.meta.key_details_1_name = "Sale Price";
  assert.throws(
    () => mapInterraListing(duplicate, "2026-07-30T18:00:00.000Z"),
    /duplicate key detail/
  );
});

test("Interra direct transport rejects malformed JSON and oversized responses", async () => {
  await assert.rejects(
    () =>
      fetchInterraInventory(async () =>
        new Response("{", { status: 200 })
      ),
    /malformed JSON/
  );
  await assert.rejects(
    () =>
      fetchInterraInventory(async () =>
        new Response("{}", {
          status: 200,
          headers: { "content-length": String(40 * 1024 * 1024) },
        })
      ),
    /exceeds/
  );
});

test("Interra streaming byte bound cancels before buffering the full response", async () => {
  let cancelled = false;
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("abc"));
        controller.enqueue(new TextEncoder().encode("def"));
      },
      cancel() {
        cancelled = true;
      },
    })
  );
  await assert.rejects(
    () => boundedText(INTERRA_INVENTORY_URL, async () => response, 1_000, 4),
    /exceeds 4 bytes/
  );
  assert.equal(cancelled, true);
});

test("Interra monitor/full adapter keeps stable identities and reports finite truncation", async () => {
  const fetchImpl = async () =>
    Response.json({
      success: true,
      posts: [row(1, "available"), row(2, "under_contract"), row(3, "closed")],
      has_next_page: false,
    });
  const monitor = await srcInterraRealty("sale", 1, true, fetchImpl);
  const full = await srcInterraRealty("sale", 2, false, fetchImpl);
  assert.equal(monitor.totalAvailable, 2);
  assert.equal(monitor.listings[0].id, "1");
  assert.equal(monitor.truncated, true);
  assert.deepEqual(full.listings.map((listing) => listing.id), ["1", "2"]);
  assert.equal(full.truncated, false);
  assert.equal(full.listings[0].preserveChildCollections, true);
});
