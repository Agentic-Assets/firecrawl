// Draft regressions. Do not execute while the operator's checks hold is active.
process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { srcMatthews } from "../../../sources/matthews.js";
import { RetainedSourceError } from "../../../lib/source-observations.js";

const host = "https://www.matthews.com";
const detail = (index: number) => `${host}/properties/partial-${index}`;
const page = (index: number, canonical = detail(index)) => `<html><head>
  <link rel="canonical" href="${canonical}"><script type="application/ld+json">{"unknown":null}</script>
  </head><body><section><h1 id="propertyTitle">Partial ${index}</h1>
  <div id="propertyPrice">Contact broker</div><div class="prose">Literal ${index}</div>
  <div class="key-info-title">Unknown units</div><div class="key-info-value">0</div>
  <table><tr><th>Rate</th><td></td></tr><tr><th>Rate</th><td>Original units</td></tr></table>
  <a href="/brochure.pdf">Brochure</a><img src="/photo.jpg" alt="Literal image"></section></body></html>`;

function provider(t: TestContext, bodies: Array<string | number>, strict: boolean) {
  const oldFetch = globalThis.fetch, oldTimer = globalThis.setTimeout;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  process.env.CRE_REQUIRE_FRESH_DETAILS = strict ? "1" : "0";
  const requests: string[] = [];
  globalThis.fetch = async (input) => {
    const url = String(input);
    requests.push(url);
    if (url === `${host}/sitemap.xml`) return new Response(`<urlset>${bodies.map((_, i) =>
      `<url><loc>${detail(i)}</loc></url>`).join("")}</urlset>`);
    const index = bodies.findIndex((_, i) => detail(i) === url);
    assert.ok(index >= 0, `unexpected provider request ${url}`);
    const body = bodies[index];
    return typeof body === "number" ? new Response(null, { status: body, headers: { location: "/listings" } }) : new Response(body);
  };
  // Only provider pacing is accelerated; bounded request deadlines stay intact.
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) =>
    oldTimer(callback, typeof delay === "number" && (delay < 29_900 || delay > 30_000) ? 0 : delay, ...args)) as typeof setTimeout;
  t.after(() => {
    globalThis.fetch = oldFetch; globalThis.setTimeout = oldTimer;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  });
  return requests;
}

test("78 accessible Matthews identity failures retain ordered context without canonical rows", async (t) => {
  const bodies = Array.from({ length: 78 }, (_, index) => page(index, `${host}/properties/conflict-${index}`));
  const requests = provider(t, bodies, false);
  await assert.rejects(srcMatthews("sale", Infinity, false), (error: unknown) => {
    assert.ok(error instanceof RetainedSourceError);
    assert.match(error.message, /none parsed/);
    assert.equal(error.retainedObservations.length, 78);
    error.retainedObservations.forEach((value, index) => {
      const observation = value as any;
      assert.equal(observation.requestedUrl, detail(index));
      assert.equal(observation.httpStatus, 200);
      assert.equal(observation.identity, "unverified");
      assert.equal(observation.pageContext.scripts[0].text, '{"unknown":null}');
      assert.equal(observation.pageContext.links[0].href, "/brochure.pdf");
      assert.equal(observation.pageContext.tables.status, "observed");
    });
    return true;
  });
  assert.equal(requests.length, 79);
});

test("strict failure retains settled Matthews page observations and keeps the failure", async (t) => {
  provider(t, [page(0), page(1, `${host}/properties/conflict`), page(2)], true);
  await assert.rejects(srcMatthews("sale", Infinity, false), (error: unknown) => {
    assert.ok(error instanceof RetainedSourceError);
    assert.match(error.message, /failed identity or detail validation/);
    const observations = error.retainedObservations as any[];
    assert.ok(observations.some((value) => value.requestedUrl === detail(1)));
    assert.ok(observations.some((value) => value.schema === "matthews-completed-parse-observation/v1"));
    return true;
  });
});

test("Matthews mixed results keep accepted counts and omit challenge-shell context", async (t) => {
  const shell = `<html><head><link rel="canonical" href="${host}/properties/conflict-shell"></head><body><h1>Access Denied</h1></body></html>`;
  provider(t, [page(0), page(1, `${host}/properties/conflict`), shell], false);
  const result = await srcMatthews("sale", Infinity, false);
  assert.equal(result.listings.length, 1);
  assert.equal(result.truncated, true);
  assert.equal(result.retainedObservations?.length, 1);
  assert.equal((result.retainedObservations![0] as any).requestedUrl, detail(1));
});

for (const tx of ["sale", "lease"] as const) for (const redirectIndex of [0, 2]) {
  test(`strict Matthews ${tx} holds 307 at ${redirectIndex} and preserves peer evidence`, async (t) => {
    // The lease selector is exercised with lease-shaped sitemap URLs.
    const oldFetch = globalThis.fetch, oldTimer = globalThis.setTimeout;
    const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const urls = [0, 1, 2].map(i => `${host}/properties/${tx === "lease" ? "leasing-" : ""}peer-${i}`);
    const requested: string[] = [];
    globalThis.fetch = async input => {
      const url = String(input); requested.push(url);
      if (url === `${host}/sitemap.xml`) return new Response(`<urlset>${urls.map(u => `<url><loc>${u}</loc></url>`).join("")}</urlset>`);
      const i = urls.indexOf(url); assert.ok(i >= 0);
      if (i === redirectIndex) return new Response(null, { status: 307, headers: { location: "/listings" } });
      return new Response(page(i, urls[i]).replace(`Partial ${i}`, `Peer ${i}`));
    };
    globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) =>
      oldTimer(callback, typeof delay === "number" && (delay < 29_900 || delay > 30_000) ? 0 : delay, ...args)) as typeof setTimeout;
    t.after(() => { globalThis.fetch = oldFetch; globalThis.setTimeout = oldTimer;
      if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS; else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict; });
    await assert.rejects(srcMatthews(tx, Infinity, false), (error: unknown) => {
      assert.ok(error instanceof RetainedSourceError);
      assert.match(error.message, /partial.*temporary redirect/);
      const observations = error.retainedObservations as any[];
      const held = observations.find(x => x.outcome === "held_temporary_redirect");
      assert.equal(held.requestedUrl, urls[redirectIndex]); assert.equal(held.httpStatus, 307);
      assert.equal(held.coverage, "unverified"); assert.equal(held.location, undefined);
      assert.ok(observations.some(x => x.schema === "matthews-completed-parse-observation/v1"));
      return true;
    });
    assert.ok(requested.includes(urls[2]));
    assert.ok(!requested.includes(`${host}/listings`));
  });
}

for (const status of [401, 403, 418, 429]) {
  test(`strict Matthews HTTP ${status} still stops without continuing peers`, async t => {
    const requests = provider(t, [status, status, page(2)], true);
    await assert.rejects(srcMatthews("sale", Infinity, false), status === 403 || status === 429 ? /throttled after retries/ : new RegExp(`Matthews HTTP ${status}`));
    assert.ok(!requests.includes(detail(2)));
  });
}

test("strict Matthews continues past more than 50 redirects to retain a valid peer", async t => {
  const requests = provider(t, [...Array.from({ length: 60 }, () => 307), page(60)], true);
  await assert.rejects(srcMatthews("sale", Infinity, false), (error: unknown) => {
    assert.ok(error instanceof RetainedSourceError);
    assert.match(error.message, /partial: 60 temporary redirect/);
    const observations = error.retainedObservations as any[];
    assert.equal(observations.filter(x => x.outcome === "held_temporary_redirect").length, 60);
    assert.ok(observations.some(x => x.schema === "matthews-completed-parse-observation/v1"));
    return true;
  });
  assert.ok(requests.includes(detail(60)));
});
