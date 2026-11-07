import test from "node:test";
import assert from "node:assert/strict";
import { firecrawl, withRequestDeadline } from "../../../lib/scrape.js";
import { enrichAvisonYoungListing } from "../../../sources/avison-young.js";
import { AvisonYoungUnsettledError } from "../../../sources/avison-young-transport.js";

test("the real Firecrawl client deadline fences later Avison requests", async () => {
  const original = firecrawl.scrape;
  const fresh = process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS;
  const transport = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
  let calls = 0;
  try {
    process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
    delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    firecrawl.scrape = async () => {
      calls++;
      return withRequestDeadline(() => new Promise<never>(() => {}), 1);
    };
    const row = { id: "1", sharpLaunchUrl: "https://example.sharplaunch.com/", externalUrl: "https://www.avisonyoung.us/properties/example" };
    await assert.rejects(enrichAvisonYoungListing(row, false), AvisonYoungUnsettledError);
    await assert.rejects(enrichAvisonYoungListing(row, false), AvisonYoungUnsettledError);
    assert.equal(calls, 1);
  } finally {
    firecrawl.scrape = original;
    if (fresh === undefined) delete process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS; else process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = fresh;
    if (transport === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT; else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = transport;
  }
});
