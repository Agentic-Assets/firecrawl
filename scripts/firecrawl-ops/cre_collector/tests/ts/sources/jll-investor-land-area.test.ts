import test from "node:test";
import assert from "node:assert/strict";
import { parseJllInvestorStructuredDetail } from "../../../sources/jll-investor.js";

const listingId = "006P500000vz5tCIAQ";
const alias = "retail/cvs-pharmacy-at-caesar-s-virginia-danville-va";

function parseLandArea(
  values: unknown[],
  searchRow: Record<string, unknown> = {},
  label = " Land Area "
) {
  const attributes = values.map((value) => ({ label, value }));
  const base = {
    id: listingId,
    url: `https://invest.jll.com/us/en/listings/${alias}`,
    assetType: "Retail",
    sizeText: "90 units",
    jllInvestorSearchRow: { area: "11,945 sf", ...searchRow },
  };
  const payload = {
    pageProps: {
      initialState: {
        pdp: {
          listing: {
            id: listingId,
            alias,
            name: "Synthetic retail listing",
            country: null,
            fullLocation: "Danville, VA, US, Americas",
            customAttributes: attributes,
          },
        },
      },
    },
  };
  return { attributes, parsed: parseJllInvestorStructuredDetail(base, payload, alias) };
}

test("JLL Investor lifts only an exact Land Area acreage into lotSizeAcres", () => {
  const { attributes, parsed } = parseLandArea(["3.71 Acres"]);

  assert.equal(parsed.lotSizeAcres, 3.71);
  assert.equal(parsed.buildingSizeSqft, undefined);
  assert.equal(parsed.sizeText, "90 units");
  assert.deepEqual(parsed.jllInvestorDetail.customAttributes, attributes);
});

test("JLL Investor accepts equivalent duplicate acreage and ignores a unit-only placeholder", () => {
  assert.equal(parseLandArea(["28.03 Acres", "28.030 ac"] ).parsed.lotSizeAcres, 28.03);
  assert.equal(parseLandArea(["Acres", "3.71 Acres"]).parsed.lotSizeAcres, 3.71);
  assert.equal(parseLandArea(["Acres"]).parsed.lotSizeAcres, undefined);
});

test("JLL Investor refuses conflicting, malformed, nonpositive, ranged, or composite acreage", () => {
  const invalidSets: unknown[][] = [
    ["1.2 Acres", "1.3 Acres"],
    ["3.71"],
    ["1–2 Acres"],
    ["-1 Acres"],
    ["0 acres"],
    ["±89.92 AC"],
    ["20 AC | 871,200 SF"],
    ["3.71 Acres", "unknown"],
    ["1,2 Acres"],
  ];

  for (const values of invalidSets) {
    const parsed = parseLandArea(values).parsed;
    assert.equal(parsed.lotSizeAcres, undefined, JSON.stringify(values));
    assert.equal(parsed.buildingSizeSqft, undefined, JSON.stringify(values));
  }
});

test("JLL Investor requires the exact Land Area label", () => {
  for (const label of ["Area", "Acres", "Land Size", "Land Area Estimate"]) {
    assert.equal(parseLandArea(["3.71 Acres"], {}, label).parsed.lotSizeAcres, undefined, label);
  }
});

test("JLL Investor refuses a detail acreage that conflicts with explicit search landArea", () => {
  const conflict = parseLandArea(["12.63 acres"], { landArea: "23.11 acres" }).parsed;
  assert.equal(conflict.lotSizeAcres, undefined);

  const agreeing = parseLandArea(["0.74 ac."], { landArea: "0.74 acres" }).parsed;
  assert.equal(agreeing.lotSizeAcres, 0.74);

  const unsupported = parseLandArea(["3.71 Acres"], { landArea: "161,608 sf" }).parsed;
  assert.equal(unsupported.lotSizeAcres, undefined);
});
