import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parseLyonStahlDetail, srcLyonStahl } from "../../../sources/lyon-stahl.js";

const fixture = readFileSync(new URL("../../fixtures/lyon-stahl-product-offer.html", import.meta.url), "utf8");
const url = "https://lyonstahl.com/properties/11863-nebraska-ave-los-angeles-ca-90025/";
function mutate(fn: (nodes: any[]) => void): string {
  return fixture.replace(/(<script type="application\/ld\+json">)([\s\S]*?)(<\/script>)/, (_all, start, body, end) => {
    const data = JSON.parse(body);
    fn(data["@graph"]);
    return start + JSON.stringify(data) + end;
  });
}
function parse(html = fixture) {
  return parseLyonStahlDetail(html, url, "sale", { strict: true });
}

test("Lyon recognizes the primary exact-page Product SoldOut offer", () => {
  const result = parse();
  assert.equal(result.kind, "terminal");
  if (result.kind === "terminal") assert.equal(result.availability, "soldout");
});

test("Lyon Product evidence refuses wrong or ambiguous identity and availability", () => {
  const mutations: Array<(nodes: any[]) => void> = [
    n => { n[1].offers.url = "https://example.com/properties/wrong/"; },
    n => { n[1]["@id"] = url + "nested/#product"; },
    n => { n[1]["@id"] = "https://example.com/#product"; },
    n => { n[1].offers.availability = ""; },
    n => { n[1].offers.availability = "NewUnknownStatus"; },
    n => { delete n[1].offers.availability; },
    n => { n.push(structuredClone(n[1])); },
    n => { n[1].offers = [n[1].offers, { ...n[1].offers, availability: "InStock" }]; },
    n => { n[0].offers = { availability: "InStock" }; },
    n => { n[0].offers = null; },
    n => { n[0].offers = {}; },
    n => { n[0].offers = { availability: "" }; },
    n => { n[0].offers = [{ availability: "SoldOut" }, { availability: "InStock" }]; },
    n => { n.push({ ...n[0] }); },
    n => { n[0].url = url.replace("11863", "21863"); },
  ];
  for (const mutation of mutations) {
    const result = parse(mutate(mutation));
    assert.notEqual(result.kind, "accepted");
    assert.notEqual(result.kind, "terminal");
  }
});

test("Lyon preserves an existing listing Offer when exact-page Product evidence agrees", () => {
  assert.equal(parse(mutate(n => { n[0].offers = { availability: "SoldOut" }; })).kind, "terminal");
  const current = parse(mutate(n => {
    n[0].offers = { availability: "InStock" };
    n[1].offers.availability = "InStock";
  }));
  assert.equal(current.kind, "accepted");
  if (current.kind === "accepted") {
    assert.equal(current.listing.id, "11863-nebraska-ave-los-angeles-ca-90025");
    assert.equal(current.listing.canonicalUrl, url);
  }
});

test("Lyon applies the existing availability enum to a synthetic same-page current Product Offer", () => {
  // Primary fallback evidence is SoldOut; this mutation tests the same property binding
  // with a previously supported active enum, not a newly observed active property.
  const current = parse(mutate(n => { n[1].offers.availability = "https://schema.org/InStock"; }));
  assert.equal(current.kind, "accepted");
});

test("Lyon Product fallback retains identity, challenge and unlabeled lease refusal checks", () => {
  assert.equal(parse(fixture.replace('rel="canonical" href="' + url, 'rel="canonical" href="https://example.com/')).kind, "rejected");
  assert.equal(parse(fixture.replace(/rel="shortlink"[^>]+/, 'rel="shortlink" href="https://example.com/?p=1"')).kind, "rejected");
  assert.equal(parse(fixture.replace("<h1>11863 Nebraska Ave</h1>", "<h1>Access denied</h1>")).kind, "rejected");
  assert.equal(parseLyonStahlDetail(fixture, url, "lease", { strict: true }).kind, "held");
  assert.equal(parse(mutate(n => { n.splice(1); })).kind, "held");
});

test("Lyon source excludes exact terminal page and keeps unknown pages truncated", async (t) => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 100_000 });
  t.mock.method(performance, "now", () => Date.now());
  const collect = async () => {
    let settled = false;
    const result = srcLyonStahl("sale", Infinity, false);
    void result.then(() => { settled = true; }, () => { settled = true; });
    for (let tick = 0; !settled && tick < 400; tick++) {
      await new Promise<void>((resolve) => setImmediate(resolve));
      if (!settled) t.mock.timers.tick(250);
    }
    assert.ok(settled, "fixture source must settle in bounded fake time");
    return result;
  };
  const previous = globalThis.fetch;
  try {
    globalThis.fetch = (async (input: any) => {
      const requested = String(input);
      const body = requested.endsWith("/sitemap.xml")
        ? '<sitemapindex><sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap></sitemapindex>'
        : requested.endsWith("properties-sitemap1.xml")
        ? `<urlset><url><loc>${url}</loc></url></urlset>` : fixture;
      return new Response(body, { headers: { "content-type": requested.endsWith(".xml") ? "text/xml" : "text/html" } });
    }) as typeof fetch;
    const terminal = await collect();
    assert.equal(terminal.listings.length, 0);
    assert.equal(terminal.truncated, false);
    assert.match(terminal.note ?? "", /1 terminal/);
    const fake = globalThis.fetch;
    globalThis.fetch = (async (input: any, init: any) => String(input) === url
      ? new Response(mutate(n => { delete n[1].offers.availability; }), { headers: { "content-type": "text/html" } })
      : fake(input, init)) as typeof fetch;
    const unknown = await collect();
    assert.equal(unknown.truncated, true);
    assert.match(unknown.note ?? "", /1 detail page.*held/);
  } finally {
    globalThis.fetch = previous;
  }
});
