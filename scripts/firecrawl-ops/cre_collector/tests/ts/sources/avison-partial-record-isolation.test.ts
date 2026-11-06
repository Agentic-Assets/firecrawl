import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { firecrawl } from "../../../lib/scrape.js";
import { SdkError } from "@mendable/firecrawl-js";
import { AvisonYoungDeadlineError, AvisonYoungUnsettledError } from "../../../sources/avison-young-transport.js";

const fixtures = new URL("../../fixtures/raw_data/", import.meta.url);
const row = JSON.parse(readFileSync(new URL("avison-young-official-display-role-18194.json", fixtures), "utf8")).feed_row;
const html = readFileSync(new URL("avison-young-official-display-role-18194.html", fixtures), "utf8");
const badUrl = "https://www.avisonyoung.us/properties/unrelated-sale";
const bad = { ...row, id: 17000, order_id: 17000, url: badUrl, external_url: badUrl };

async function runCase(denied: boolean, ordinaryFailure?: "500" | "sdk403" | "missing-url" | "encoded-key" | "bad-row-json") {
  const keys = ["CRE_REQUIRE_FRESH_DETAILS", "CRE_REQUIRE_FRESH_PROPERTY_DETAILS", "CRE_REFRESH_GENERATION",
    "AVISON_YOUNG_DETAIL_TRANSPORT", "AVISON_YOUNG_DETAIL_CONCURRENCY"];
  const previous = new Map(keys.map(key => [key, process.env[key]]));
  const originalFetch = globalThis.fetch;
  const originalScrape = firecrawl.scrape;
  const originalParse = JSON.parse;
  const observed: any[] = [];
  let outcomes: any[] = [];
  const calls: string[] = [];
  try {
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "partial-test";
    process.env.AVISON_YOUNG_DETAIL_CONCURRENCY = "1";
    if (ordinaryFailure === "bad-row-json") {
      JSON.parse = ((text: string, reviver?: (this: any, key: string, value: any) => any) => {
        const value = originalParse(text, reviver);
        if (Array.isArray(value?.items)) {
          const held = value.items.find((item: any) => item.id === 17000);
          if (held) held.unserializable = BigInt(1);
        }
        return value;
      }) as typeof JSON.parse;
    }
    globalThis.fetch = async input => {
      const url = new URL(String(input));
      if (url.hostname === "www.avisonyoung.us") return new Response("SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')");
      return new Response(JSON.stringify({ items: url.searchParams.get("entity") === "website"
          ? [["missing-url", "encoded-key", "bad-row-json"].includes(ordinaryFailure ?? "")
            ? { ...bad, url: null, external_url: null,
                description: ordinaryFailure === "encoded-key"
                  ? "record " + String.fromCharCode(92) + "u0030" + "123456789abcdef0123456789abcdef"
                  : "record 0123456789abcdef0123456789abcdef extra" }
            : bad, row] : [] }),
        { status: 200, headers: { "content-type": "application/json" } });
    };
    firecrawl.scrape = async (url: string) => {
      calls.push(url);
      if (url === badUrl && ordinaryFailure === "sdk403") throw new SdkError("Forbidden", 403, undefined, undefined);
      return { rawHtml: html, markdown: "", links: [],
        metadata: { sourceURL: url, url, statusCode: url === badUrl ? (denied ? 403 : ordinaryFailure === "500" ? 500 : 200) : 200 } };
    };
    const source = await import(`../../../sources/avison-young.js?isolation=${denied}`);
    let failure: unknown;
    try {
      await source.srcAvisonYoung("sale", 100, false,
        listing => observed.push(listing), result => { outcomes = result; });
    } catch (error) { failure = error; }
    return { source, failure, observed, outcomes, calls };
  } finally {
    globalThis.fetch = originalFetch;
    firecrawl.scrape = originalScrape;
    JSON.parse = originalParse;
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
}

test("identity mismatch holds the row and retains a later validated peer", async () => {
  const { source, failure, observed, outcomes } = await runCase(false);
  assert.ok(failure instanceof source.AvisonYoungPartialDetailError);
  assert.deepEqual((failure as InstanceType<typeof source.AvisonYoungPartialDetailError>).heldIds, ["17000"]);
  assert.deepEqual(observed.map(row => row.id), ["18194"]);
  assert.deepEqual(outcomes.map(row => [row.id, row.state]), [["17000", "failed"], ["18194", "validated"]]);
});

test("a URL-less source row is held while a later identified peer is retained", async () => {
  const { source, failure, observed, outcomes, calls } = await runCase(false, "missing-url");
  assert.ok(failure instanceof source.AvisonYoungPartialDetailError);
  assert.deepEqual((failure as InstanceType<typeof source.AvisonYoungPartialDetailError>).heldIds, ["17000"]);
  assert.deepEqual(observed.map(item => item.id), ["18194"]);
  assert.deepEqual(outcomes.map(item => [item.id, item.state]), [["17000", "failed"], ["18194", "validated"]]);
  assert.equal(outcomes[0].reason, "missing_public_detail_urls");
  const evidence = outcomes[0].feedObservation;
  assert.equal(evidence.sourceKey, "avison-young");
  assert.equal(evidence.qualification, "unverified");
  assert.equal(evidence.credentialRedacted, true);
  assert.ok(!evidence.sourceRowJson.includes("0123456789abcdef0123456789abcdef"));
  const retained = JSON.parse(evidence.sourceRowJson);
  assert.equal(retained.id, 17000);
  assert.equal(retained.url, null);
  assert.equal(retained.external_url, null);
  assert.equal(retained.description, "record [REDACTED_API_KEY] extra");
  assert.ok(!calls.includes(badUrl));
});

for (const reason of ["encoded-key", "bad-row-json"] as const) {
  test(`URL-less ${reason} evidence refusal retains a later peer`, async () => {
    const { source, failure, observed, outcomes, calls } = await runCase(false, reason);
    assert.ok(failure instanceof source.AvisonYoungPartialDetailError);
    assert.deepEqual((failure as InstanceType<typeof source.AvisonYoungPartialDetailError>).heldIds, ["17000"]);
    assert.deepEqual(observed.map(item => item.id), ["18194"]);
    assert.equal(outcomes[0].reason, "missing_public_detail_urls");
    assert.deepEqual(outcomes[0].feedObservation, { state: "retention_failed" });
    assert.equal(outcomes[1].state, "validated");
    assert.ok(!JSON.stringify(outcomes).includes("0123456789abcdef0123456789abcdef"));
    assert.ok(!JSON.stringify(outcomes).includes("u0030"));
    assert.ok(!calls.includes(badUrl));
  });
}

test("access denial stops before requesting a later peer", async () => {
  const { source, failure, observed, calls } = await runCase(true);
  assert.ok(failure instanceof source.AvisonYoungAccessDeniedError);
  assert.deepEqual(observed, []);
  assert.deepEqual(calls, [badUrl]);
});

test("a thrown Firecrawl SDK 403 stops before requesting a later peer", async () => {
  const { source, failure, observed, calls } = await runCase(false, "sdk403");
  assert.ok(failure instanceof source.AvisonYoungAccessDeniedError);
  assert.deepEqual(observed, []);
  assert.deepEqual(calls, [badUrl]);
});

for (const failure of ["500"] as const) {
  test(`settled ${failure} holds the row while a later peer is retained`, async () => {
    const result = await runCase(false, failure);
    assert.ok(result.failure instanceof result.source.AvisonYoungPartialDetailError);
    assert.deepEqual(result.observed.map(row => row.id), ["18194"]);
    assert.deepEqual(result.outcomes.map(row => [row.id, row.state]), [["17000", "failed"], ["18194", "validated"]]);
  });
}

test("a valid first URL cannot conceal a 500 on the second", async () => {
  const previousFresh = process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS;
  const previousTransport = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
  const originalScrape = firecrawl.scrape;
  try {
    process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
    delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    const source = await import("../../../sources/avison-young.js?mixed-url-errors");
    const base = source.avisonYoungBaseListing(row, new Map());
    base.sharpLaunchUrl = row.external_url;
    base.externalUrl = "https://www.avisonyoung.us/properties/second-sale";
    const calls: string[] = [];
    firecrawl.scrape = async (url: string) => {
      calls.push(url);
      return { rawHtml: html, markdown: "", links: [], metadata: {
        sourceURL: url, url, statusCode: url === base.externalUrl ? 500 : 200,
      } };
    };
    await assert.rejects(
      source.enrichAvisonYoungListing(base, false, undefined, { matchingExternalUrlRowIds: ["18194"] }),
      /HTTP 500/,
    );
    assert.deepEqual(calls, [base.sharpLaunchUrl, base.externalUrl]);
  } finally {
    firecrawl.scrape = originalScrape;
    if (previousFresh === undefined) delete process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = previousFresh;
    if (previousTransport === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previousTransport;
  }
});

test("settled direct deadline retains its actual type after retries", async () => {
  const source = await import("../../../sources/avison-young.js?settled-deadline");
  await assert.rejects(source.fetchAvisonYoungDirectDocWithRetry(
    "https://example.sharplaunch.com/", async () => { throw new AvisonYoungDeadlineError(); }, 2, 0,
  ), AvisonYoungDeadlineError);
});

test("drained workers select unsettled then denial regardless of completion order", async () => {
  const source = await import("../../../sources/avison-young.js?priority-test");
  for (const [first, second] of [
    [new Error("ordinary"), new source.AvisonYoungAccessDeniedError("denied")],
    [new source.AvisonYoungAccessDeniedError("denied"), new Error("ordinary")],
    [new source.AvisonYoungAccessDeniedError("denied"), new AvisonYoungUnsettledError("deadline")],
    [new AvisonYoungUnsettledError("deadline"), new source.AvisonYoungAccessDeniedError("denied")],
  ]) {
    const expected = first instanceof AvisonYoungUnsettledError || second instanceof AvisonYoungUnsettledError
      ? AvisonYoungUnsettledError : source.AvisonYoungAccessDeniedError;
    await assert.rejects(source.mapAvisonYoungStrictDetails([first, second], 2, async (error, index) => {
      if (index === 1) await new Promise(resolve => setTimeout(resolve, 10));
      throw error;
    }), expected);
  }
});
