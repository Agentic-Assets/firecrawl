// AGENTIC-5119: structure signals and size/money basis at capture.
process.argv = [process.argv[0]!, process.argv[1]!];
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as util from "../../../lib/util.js";
import { parseMoney, parseSizeText } from "../../../lib/parse.js";
import {
  avisonYoungBaseListing,
  avisonYoungPricePerAcre,
  avisonYoungSaleType,
} from "../../../sources/avison-young.js";
import { colliersAverageUnitSf, enrichColliersCard } from "../../../sources/colliers.js";
import { mapColliersMainCoveoListing } from "../../../sources/colliers-main.js";
import { jllStrandedStructured } from "../../../sources/jll.js";
import { pyramidSizeFacts } from "../../../sources/pyramid-brokerage.js";

test("money: $275M reads as 275 million through the one TS parser", () => {
  assert.equal(parseMoney("$275M"), 275_000_000);
  assert.equal(parseMoney("$275,000,000 UPB"), 275_000_000);
  assert.equal(parseMoney("Starting Bid: $1.2 million"), 1_200_000);
  // The former util copy is gone, so every adapter shares this parser.
  assert.equal("moneyToNumber" in util, false);
});

function colliersCard(): any {
  return {
    id: "147023",
    mapProjectId: "147023",
    url: "https://my.rcm1.com/handler/landing.aspx?pv=fixture",
    detailUrl: "https://my.rcm1.com/handler/landing.aspx?pv=fixture",
    detailPv: "fixture",
    name: "Indigo 19",
    transactionType: "Sale",
    assetType: null,
    status: null,
    city: "Virginia Beach",
    state: "VA",
    country: "US",
    salePriceUsd: null,
    salePriceText: null,
    sizeText: "931 sq ft",
    latitude: null,
    longitude: null,
    brokerIds: [],
    contactsDetailed: [],
    photos: [],
    colliersSalesTrackerCard: {},
  };
}

function colliersDetail(assetType: string, units: string | null): any {
  return {
    ProjectSummary: { ProjectId: 147023, ProjectName: "Indigo 19", AskingPrice: "$48,020,000", Address: {} },
    ProjectDetails: {
      AssetType: { Value: assetType },
      ProjectType: { Value: "Investment Sale" },
      ProjectFields: [
        { Name: "Size", Value: "931 sq ft" },
        ...(units ? [{ Name: "Units", Value: units }] : []),
      ],
    },
  };
}

async function enrichWith(t: any, detail: any): Promise<any> {
  const prior = globalThis.fetch;
  t.after(() => { globalThis.fetch = prior; });
  globalThis.fetch = (async () => new Response(JSON.stringify(detail))) as typeof fetch;
  return enrichColliersCard(colliersCard());
}

test("Colliers multifamily average unit size never reaches building size", async (t) => {
  const row = await enrichWith(t, colliersDetail("Multifamily - Mid-Rise", "196"));
  assert.equal(row.buildingSizeSqft, undefined);
  assert.equal(row.sizeText, undefined, "the ingest would re-parse sizeText into size_sf");
  assert.equal(row.extraFacts.avg_unit_sf, 931);
  assert.equal(row.extraFacts.project_type, "Investment Sale");
  assert.equal(row.colliersSalesTrackerDetail.sizeField, "931 sq ft");
  assert.equal(row.units, 196);
  assert.equal(row.salePriceUsd, 48_020_000);
});

test("Colliers office Size stays building size", async (t) => {
  const row = await enrichWith(t, colliersDetail("Office", null));
  assert.equal(row.buildingSizeSqft, 931);
  assert.equal(row.sizeText, "931 sq ft");
  assert.equal(row.extraFacts.avg_unit_sf, undefined);
});

test("colliersAverageUnitSf follows the validator rule and the unpriced fallback", () => {
  assert.equal(colliersAverageUnitSf("Multifamily", 931, 196, 48_020_000), 931);
  assert.equal(colliersAverageUnitSf("Multifamily", 2_400, 12, null), 2_400);
  assert.equal(colliersAverageUnitSf("Multifamily", 182_000, 196, 48_020_000), null);
  // No unit count: a sub-3,000 SF size priced above $1,000/SF is per unit.
  assert.equal(colliersAverageUnitSf("Multifamily - Garden", 872, null, 95_760_000), 872);
  assert.equal(colliersAverageUnitSf("Multifamily", 2_400, null, 600_000), null);
  assert.equal(colliersAverageUnitSf("Retail", 931, 196, 48_020_000), null);
});

test("Avison land unit price becomes price per acre only when it reproduces the acreage", () => {
  const land = {
    id: 20228, status: "active", transaction: ["sale"], type: ["land.residential"], sale_currency: "USD",
    sale_price: 1_125_540, sale_unit_price: 18_500, total_surface_unit: "sqft",
    description: "<p>60.84 +/- Acres available for sale in North Hall</p>",
  };
  assert.equal(avisonYoungPricePerAcre(land), 18_500);
  const listing = avisonYoungBaseListing(land, new Map());
  assert.equal(listing.pricePerAcre, 18_500);
  assert.equal(listing.salePricePerSf, undefined);
  assert.equal(listing.saleUnitPrice, 18_500);

  assert.equal(avisonYoungPricePerAcre({ ...land, description: "<p>For Sale</p>" }), null);
  assert.equal(avisonYoungPricePerAcre({ ...land, total_surface: 60.84, total_surface_unit: "ac", description: null }), 18_500);
  assert.equal(avisonYoungPricePerAcre({ ...land, type: ["office.medical"] }), null);
  assert.equal(avisonYoungPricePerAcre({ ...land, sale_unit_price: land.sale_price }), null);
  assert.equal(avisonYoungPricePerAcre({ ...land, sale_currency: "CAD" }), null);
});

test("Avison detail Sale type is captured verbatim", () => {
  const rawHtml = readFileSync(
    new URL("../../fixtures/raw_data/avison-young-official-display-role-50399.html", import.meta.url),
    "utf8"
  );
  assert.equal(avisonYoungSaleType([{ doc: { rawHtml } as any }]), "Investment");
  assert.equal(avisonYoungSaleType([{ doc: { rawHtml: "<ul><li><span>Zoning</span><span>RM 18</span></li></ul>" } as any }]), null);
});

test("Colliers-main Coveo sale and lease type IDs are carried verbatim", () => {
  const mapped = mapColliersMainCoveoListing(
    { id: "usa1171157", url: "https://www.colliers.com/en/properties/a/usa1171157", lastmod: "2026-10-01" },
    { raw: {
      propertyz32xtitle: "Free Standing Walgreens",
      propertyz32xfullz32xaddress: "2205 W 22nd St, Oak Brook, IL 60523, USA",
      forz32xsale: "1",
      forz32xlease: "1",
      salez32xtype: "E339916E-B7C0-45AD-8178-6D862EB519FB",
      leasez32xtype: "6ab7b7628c1241839f17f10a09bc3a19",
    } }
  );
  assert.deepEqual(mapped.extraFacts, {
    colliers_sale_type_id: "e339916eb7c045ad81786d862eb519fb",
    colliers_lease_type_id: "6ab7b7628c1241839f17f10a09bc3a19",
  });
  assert.equal(mapped.colliersMain.rawPricing.saleType, "E339916E-B7C0-45AD-8178-6D862EB519FB");
});

test("JLL tenure types ride extra_facts", () => {
  assert.deepEqual(jllStrandedStructured({ tenureTypes: ["sale", "sale", " rent "] }).extraFacts, {
    tenure_types: ["sale", "rent"],
  });
  assert.equal(jllStrandedStructured({ tenureTypes: [] }).extraFacts, undefined);
});

test("Pyramid sizes: building area only in size, available space apart, acreage to lot", () => {
  assert.deepEqual(parseSizeText("4,863 +/-SF"), { sizeSf: 4863, lotSf: null });
  assert.deepEqual(
    pyramidSizeFacts({ "Building Size": "99,620 +/-SF", "Largest Available": "14,000 +/-SF", "Smallest Available": "4,500 +/-SF" }),
    { sizeText: "99,620 +/-SF", buildingSizeSqft: 99_620, availableSf: 14_000 }
  );
  // Available space with no building size never becomes size_sf.
  assert.deepEqual(pyramidSizeFacts({ Available: "2,400 +/-SF" }), { availableSf: 2_400 });
  // Land: the offered acreage is the lot.
  assert.deepEqual(pyramidSizeFacts({ "Building Size": "0.00", "Largest Available": "2.00+/- Acres" }), {
    sizeText: "2.00+/- Acres",
  });
  assert.deepEqual(pyramidSizeFacts(undefined), {});
});
