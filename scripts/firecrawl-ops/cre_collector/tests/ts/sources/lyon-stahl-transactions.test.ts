import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import * as cheerio from "cheerio";

let networkCalls = 0;
globalThis.fetch = async () => {
  networkCalls++;
  throw new Error("Lyon transaction tests prohibit network requests");
};
const { parseLyonStahlDetail, preserveLyonStahlOffers, srcLyonStahl } = await import("../../../sources/lyon-stahl.js");
const html = readFileSync(new URL("../../fixtures/lyon-stahl-dual-offer.html", import.meta.url), "utf8");
const url = "https://lyonstahl.com/properties/314-n-wilmington-blvd-915-w-c-st-wilmington-ca-90744/";

function page(change: (listing: any) => void): string {
  const $ = cheerio.load(html);
  const script = $("script[type='application/ld+json']");
  const graph = JSON.parse(script.html()!);
  const listing = graph["@graph"].find((value: any) => value["@type"] === "RealEstateListing");
  change(listing);
  script.text(JSON.stringify(graph));
  return $.html();
}

function parse(document: string, tx: "sale" | "lease") {
  return parseLyonStahlDetail(document, url, tx, { strict: true });
}

function accepted(document: string, tx: "sale" | "lease") {
  const result = parse(document, tx);
  assert.equal(result.kind, "accepted", result.kind === "accepted" ? "" : result.reason);
  assert.equal(result.kind, "accepted");
  return result.listing;
}

const { prune } = await import("../../../lib/util.js");
const collectSource = readFileSync(new URL("../../../collect.ts", import.meta.url), "utf8");
const collectBody = collectSource.match(/for \(const l of res.listings\) \{([\s\S]*?)\n        \}/)?.[1];
assert.ok(collectBody, "actual ordinary collector emission loop must be found");
const emit = new Function("key", "res", "tx", "prune", "preserveJllSubleaseFact", "preserveLyonStahlOffers",
  `const listings = []; for (const l of res.listings) {${collectBody}} return listings[0];`);

test("observed dual Offers support sale and lease independently without sale-price leakage", () => {
  const sale = accepted(html, "sale");
  const lease = accepted(html, "lease");
  assert.equal(sale.transactionType, "Sale");
  assert.equal(lease.transactionType, "Lease");
  assert.ok(sale.salePriceUsd > 0);
  assert.equal(lease.salePriceUsd, undefined);
  assert.equal(lease.salePriceText, undefined);
  assert.equal(lease.leaseRateUsdPerSfYr, undefined);
  assert.equal(lease.id, sale.id);
  assert.equal(lease.lyonStahlPostId, "40528");
  assert.deepEqual(lease.photos, sale.photos);
  assert.deepEqual(lease.brokerIds, sale.brokerIds);
  assert.equal(lease.street, sale.street);
  assert.equal(lease.description, sale.description);
  assert.equal(lease.extraFacts.providerOffers.length, 2);
  assert.match(lease.extraFacts.providerOffers[1].description, /available for lease/i);
});

test("transaction selection is independent of Offer order", () => {
  const reversed = page((listing) => listing.offers.reverse());
  for (const tx of ["sale", "lease"] as const) {
    const reordered = accepted(reversed, tx);
    const original = accepted(html, tx);
    assert.deepEqual(reordered.extraFacts.providerOffers, original.extraFacts.providerOffers.toReversed());
    assert.deepEqual({ ...reordered, extraFacts: undefined }, { ...original, extraFacts: undefined });
  }
});

for (const tx of ["sale", "lease"] as const) {
  test(`${tx}: explicit opposite-only Offer does not gain authority from incidental prose or price`, () => {
    const opposite = tx === "sale" ? "For Lease" : "For Sale";
    const result = parse(page((listing) => listing.offers = listing.offers.filter((offer: any) => offer.name === opposite)), tx);
    assert.equal(result.kind, "held");
  });

  for (const availability of [undefined, "", "https://schema.org/Unknown"] as const) {
    test(`${tx}: missing or unknown requested availability ${String(availability)} stays held`, () => {
      const result = parse(page((listing) => {
        const offer = listing.offers.find((value: any) => value.name === (tx === "sale" ? "For Sale" : "For Lease"));
        offer.availability = availability;
      }), tx);
      assert.equal(result.kind, "held");
    });
  }

  test(`${tx}: same-transaction conflicting Offers remain held`, () => {
    const result = parse(page((listing) => {
      const offer = listing.offers.find((value: any) => value.name === (tx === "sale" ? "For Sale" : "For Lease"));
      listing.offers.push({ ...offer, availability: "https://schema.org/SoldOut" });
    }), tx);
    assert.equal(result.kind, "held");
  });

  test(`${tx}: foreign requested Offer cannot supply transaction authority`, () => {
    const result = parse(page((listing) => {
      const offer = listing.offers.find((value: any) => value.name === (tx === "sale" ? "For Sale" : "For Lease"));
      offer.url = "https://example.invalid/another-property";
    }), tx);
    assert.equal(result.kind, "held");
  });
}

test("recognized opposite transaction can have a different legitimate status", () => {
  const saleCurrent = page((listing) => listing.offers[1].availability = "https://schema.org/SoldOut");
  assert.equal(parse(saleCurrent, "sale").kind, "accepted");
  assert.equal(parse(saleCurrent, "lease").kind, "terminal");
  const leaseCurrent = page((listing) => listing.offers[0].availability = "https://schema.org/SoldOut");
  assert.equal(parse(leaseCurrent, "lease").kind, "accepted");
  assert.equal(parse(leaseCurrent, "sale").kind, "terminal");
});

test("complete own Offer objects retain unknown useful metadata without inventing rent", () => {
  const document = page((listing) => {
    listing.offers[1].eligibleRegion = { "@type": "Place", name: "Observed offering area" };
    listing.offers[1].price = "12.34";
    listing.offers[1].priceSpecification = { description: "Provider quotation; basis unspecified" };
  });
  const lease = accepted(document, "lease");
  assert.deepEqual(lease.extraFacts.providerOffers[1].eligibleRegion, { "@type": "Place", name: "Observed offering area" });
  assert.equal(lease.extraFacts.providerOffers[1].price, "12.34");
  assert.equal(lease.salePriceUsd, undefined);
  assert.equal(lease.leaseRateUsdPerSfYr, undefined);
});

test("actual producer to canonical staging and merge preserves both transactions and own Offer facts", () => {
  const sale = accepted(html, "sale");
  const lease = accepted(html, "lease");
  const result = spawnSync("python3", ["-c", [
    "import json,sys,cre_ingest as ci",
    "items=json.load(sys.stdin)",
    "rows=[ci.to_row(dict(item,sourceKey='lyon-stahl',transactionMode=mode),{},'2026-10-17T09:30:00Z') for item,mode in zip(items,['sale','lease'])]",
    "assert rows[0]['transaction_type']=='sale' and rows[1]['transaction_type']=='lease'",
    "assert rows[1]['sale_price_usd'] is None and rows[1]['lease_rate_min'] is None",
    "merged=ci.merge_rows(*rows)",
    "assert merged['transaction_type']=='sale_or_lease'",
    "assert rows[0]['external_id']==rows[1]['external_id']",
    "assert 'available for lease' in json.dumps(merged,default=list).lower()",
    "print(json.dumps({'transaction_type':merged['transaction_type'],'external_id':merged['external_id']}))",
  ].join("; ")], {
    cwd: process.cwd(), input: JSON.stringify([
      emit("lyon-stahl", { company: "Lyon Stahl", listings: [sale] }, "sale", prune, (v: any) => v, preserveLyonStahlOffers),
      emit("lyon-stahl", { company: "Lyon Stahl", listings: [lease] }, "lease", prune, (v: any) => v, preserveLyonStahlOffers),
    ]), encoding: "utf8", timeout: 10_000,
    env: { ...process.env, PYTHONPATH: "." },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).transaction_type, "sale_or_lease");
});

test("lease acquisition explicitly remains incomplete and makes zero requests", async () => {
  const result = await srcLyonStahl("lease", Infinity, false);
  assert.equal(result.totalAvailable, null);
  assert.equal(result.truncated, true);
  assert.deepEqual(result.listings, []);
  assert.match(result.note!, /complete.*acquisition.*not implemented/i);
  assert.equal(networkCalls, 0);
});


test("source and actual outer collector prune preserve complete own Offer JSON literals", () => {
  const document = page((listing) => {
    listing.offers[1].eligibleRegion = { name: "Native area", enabled: false, missing: null, label: "", count: 0 };
  });
  const listing = accepted(document, "lease");
  const emitted = emit("lyon-stahl", { company: "Lyon Stahl", listings: [listing] }, "lease",
    prune, (value: any) => value, preserveLyonStahlOffers);
  const golden = { name: "Native area", enabled: false, missing: null, label: "", count: 0 };
  assert.deepEqual(JSON.parse(JSON.stringify(emitted)).extraFacts.providerOffers[1].eligibleRegion, golden);
  assert.deepEqual(emitted.extraFacts.providerOffers, listing.extraFacts.providerOffers);
});

for (const name of ["Unclassified Offering", ""]) {
  test(`lease does not infer transaction from unknown Offer label ${name}`, () => {
    assert.equal(parse(page((listing) => listing.offers = [{ ...listing.offers[1], name }]), "lease").kind, "held");
  });
}

test("unverified nonempty business function cannot silently contradict requested Offer name", () => {
  assert.equal(parse(page((listing) => listing.offers[0].businessFunction = "https://example.invalid/unknown-function"), "sale").kind, "held");
});

test("foreign opposite Offer never becomes retained own-property provenance", () => {
  const sale = accepted(page((listing) => listing.offers[1].url = "https://example.invalid/recommendation"), "sale");
  assert.equal(sale.extraFacts.providerOffers.length, 1);
  assert.equal(sale.extraFacts.providerOffers[0].name, "For Sale");
});

function legacy(change: (offer: any) => void): string {
  return page((listing) => {
    const offer = { ...listing.offers[0] };
    delete offer.name;
    change(offer);
    listing.offers = [offer];
  });
}

for (const businessFunction of ["https://schema.org/LeaseOut", "https://example.invalid/unknown"]) {
  test(`unlabelled Offer cannot bypass unverified business function ${businessFunction}`, () => {
    assert.equal(parse(legacy((offer) => offer.businessFunction = businessFunction), "sale").kind, "held");
  });
}

test("provided foreign unlabelled Offer URL is held while missing legacy fields remain compatible", () => {
  assert.equal(parse(legacy((offer) => offer.url = "https://example.invalid/other"), "sale").kind, "held");
  assert.equal(parse(legacy((offer) => { delete offer.url; delete offer["@type"]; }), "sale").kind, "accepted");
});

test("provided wrong Offer type and ambiguous unlabelled groups do not grant sale", () => {
  assert.equal(parse(legacy((offer) => offer["@type"] = "Demand"), "sale").kind, "held");
  assert.equal(parse(page((listing) => { delete listing.offers[0].name; listing.offers = [listing.offers[0], { ...listing.offers[0] }]; }), "sale").kind, "held");
});

for (const [field, value] of [
  ["businessFunction", { "@id": "https://schema.org/LeaseOut" }],
  ["businessFunction", ["https://schema.org/LeaseOut"]],
  ["name", { "@value": "For Lease" }],
  ["url", 123],
  ["url", { "@id": "https://example.invalid/foreign" }],
] as const) {
  test(`supplied non-string ${field} cannot become absent in legacy sale fallback`, () => {
    assert.equal(parse(legacy((offer) => offer[field] = value), "sale").kind, "held");
  });
}

test("named requested Offer also refuses object-valued business function", () => {
  assert.equal(parse(page((listing) => listing.offers[1].businessFunction = { "@id": "https://schema.org/LeaseOut" }), "lease").kind, "held");
});

for (const field of ["name", "url", "businessFunction"] as const) {
  for (const value of [{ unexpected: "authority" }, ["authority"], 123, false]) {
    test(`${field}: supplied ${Array.isArray(value) ? "array" : typeof value} authority is held`, () => {
      assert.equal(parse(legacy((offer) => offer[field] = value), "sale").kind, "held");
    });
  }
}
