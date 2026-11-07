import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { firecrawl } from "../../../lib/scrape.js";

const fixtures = new URL("../../fixtures/raw_data/", import.meta.url);
const first = JSON.parse(readFileSync(new URL("avison-young-official-display-role-18194.json", fixtures), "utf8")).feed_row;
const second = JSON.parse(readFileSync(new URL("avison-young-official-display-role-50399.json", fixtures), "utf8")).feed_row;
const firstHtml = readFileSync(new URL("avison-young-official-display-role-18194.html", fixtures), "utf8");
const secondHtml = readFileSync(new URL("avison-young-official-display-role-50399.html", fixtures), "utf8");
const native = "https://alternate-failure.sharplaunch.com";
const secret = "0123456789abcdef0123456789abcdef";

async function fixture(mode: "valid" | "wrong-subject" | "wrong-address" | "secret") {
  const keys = ["CRE_REQUIRE_FRESH_DETAILS", "CRE_REQUIRE_FRESH_PROPERTY_DETAILS", "CRE_REFRESH_GENERATION", "AVISON_YOUNG_DETAIL_TRANSPORT", "AVISON_YOUNG_DETAIL_CONCURRENCY"];
  const previous = new Map(keys.map(key => [key, process.env[key]]));
  const originalFetch = globalThis.fetch;
  const originalScrape = firecrawl.scrape;
  const outcomes: any[] = [], peers: any[] = [];
  try {
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "alternate-test";
    process.env.AVISON_YOUNG_DETAIL_CONCURRENCY = "1";
    const held = { ...first, url: native, order_id: 1, transaction: ["sale"], status: "active" };
    const peer = { ...second, order_id: 2, transaction: ["sale"], status: "active" };
    globalThis.fetch = async input => {
      const url = new URL(String(input));
      if (url.hostname === "www.avisonyoung.us") return new Response(`SharpLaunch.PSE.create('${secret}')`);
      if (!url.hostname.endsWith("sharplaunch.com")) throw new Error("unexpected network");
      return new Response(JSON.stringify({ items: url.searchParams.get("entity") === "website" ? [held, peer] : [] }),
        { status: 200, headers: { "content-type": "application/json" } });
    };
    firecrawl.scrape = async (url: string) => {
      let html = url === native ? "<html><body>unrelated page</body></html>" : url === first.external_url ? firstHtml : secondHtml;
      if (url === first.external_url && mode === "wrong-subject") html = html.replaceAll("4235 W Ann Lurie Place", "Wrong subject");
      if (url === first.external_url && mode === "wrong-address") html = html.replaceAll("4235 West Ann Lurie Place", "9999 Wrong Street");
      if (url === first.external_url && mode === "secret") html += `<p>${secret}</p>`;
      return { rawHtml: html, markdown: "", links: [], metadata: { sourceURL: url, url, statusCode: 200 } };
    };
    const source = await import("../../../sources/avison-young.js");
    await assert.rejects(() => source.srcAvisonYoung("sale", 100, false,
      listing => peers.push(listing), result => outcomes.push(...result)), source.AvisonYoungPartialDetailError);
    return { outcomes, peers };
  } finally {
    globalThis.fetch = originalFetch; firecrawl.scrape = originalScrape;
    for (const [key, value] of previous) if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
}

test("held record retains validated official page and independently validated peer", async () => {
  const { outcomes, peers } = await fixture("valid");
  const held = outcomes.find(item => item.id === "18194");
  assert.equal(held.state, "failed");
  assert.equal(held.validatedOfficialDetail.qualification, "page_identity_validated_record_held");
  assert.equal(held.validatedOfficialDetail.pages[0].url, first.external_url);
  assert.equal(held.validatedOfficialDetail.pages[0].doc.rawHtml, firstHtml);
  assert.ok(Date.parse(held.validatedOfficialDetail.pages[0].observedAt) <= Date.parse(held.validatedOfficialDetail.retainedAt));
  assert.ok(peers.some(item => item.id === "50399"));
});
for (const mode of ["wrong-subject", "wrong-address"] as const) test(`${mode} official page remains held without validated-page evidence`, async () => {
  const { outcomes, peers } = await fixture(mode);
  assert.equal(outcomes.find(item => item.id === "18194").validatedOfficialDetail, undefined);
  assert.ok(peers.some(item => item.id === "50399"));
});
test("secret-bearing official evidence is refused without suppressing peers", async () => {
  const { outcomes, peers } = await fixture("secret");
  assert.deepEqual(outcomes.find(item => item.id === "18194").validatedOfficialDetail, { state: "retention_failed" });
  assert.ok(!JSON.stringify(outcomes).includes(secret));
  assert.ok(peers.some(item => item.id === "50399"));
});
