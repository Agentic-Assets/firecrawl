process.argv = [process.argv[0]!, process.argv[1]!];
import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  assertJllDirectUrl, fetchJllDirectDoc, isJllPublicAddress, jllDirectHtmlDoc,
  JLL_DIRECT_MAX_BYTES, JLL_DIRECT_START_INTERVAL_MS, requestJllPinned, type JllPinnedResponse,
} from "../../../sources/jll-direct.js";
import { enrichJllListing, readJllDetailCache } from "../../../sources/jll.js";
import { firecrawl } from "../../../lib/scrape.js";

const url = "https://property.jll.com/listings/example-property";
const property = {
  id: "123", pageUrl: "/listings/example-property", title: "Example Property",
  images: ["https://images.example/property.jpg"],
  brochures: ["https://docs.example/brochure.pdf"],
  floorPlans: { files: ["https://docs.example/plan.pdf"], images: [] },
  videos: ["https://www.youtube.com/watch?v=abc12345678"],
  descriptionSections: [{ title: "Overview", content: "Full property description" }],
};
const html = `<html><body><header>Public property navigation</header><main>
<h1>Example Property</h1><p>Full property description</p>
<a href="/listings/example-property/flyer.pdf">Download flyer</a>
<img src="https://images.example/property.jpg">
<iframe src="https://my.matterport.com/show/?m=demo123"></iframe>
<video><source src="https://videos.example/tour.mp4"></video>
<div data-video-url="https://videos.example/other.mp4"></div>
<table><tr><th>Area</th><th>Rent</th></tr><tr><td>10,000 SF</td><td>$20</td></tr></table>
</main><script id="__NEXT_DATA__">${JSON.stringify({ props: { pageProps: {
  property, brokers: [{ name: "Example Broker", email: "broker@jll.com", telephone: "555-0100" }],
} } })}</script></body></html>`;
const response = (body = html): JllPinnedResponse => ({ status: 200, location: null, contentType: "text/html; charset=utf-8", body });
const resolvePublic = async () => ["93.184.216.34"];

test("JLL direct URL and DNS admission rejects alternate targets and non-public ranges", () => {
  assert.equal(assertJllDirectUrl(url).hostname, "property.jll.com");
  for (const target of ["http://property.jll.com/listings/a", "https://property.jll.com:8443/listings/a",
    "https://user:password@property.jll.com/listings/a", "https://property.jll.com.evil.com/listings/a",
    "https://property.jll.com/api/graphql", "https://127.0.0.1/listings/a"]) {
    assert.throws(() => assertJllDirectUrl(target), /approved/);
  }
  for (const ip of ["127.0.0.1", "10.1.1.1", "169.254.169.254", "100.64.0.1", "192.0.2.1",
    "::1", "::ffff:127.0.0.1", "fc00::1", "fe80::1", "2001:db8::1", "2002:7f00:1::", "invalid"]) {
    assert.equal(isJllPublicAddress(ip), false, ip);
  }
  assert.ok(isJllPublicAddress("93.184.216.34"));
  assert.ok(isJllPublicAddress("2606:4700:4700::1111"));
});

test("JLL direct captures full Markdown, HTML, links, images, and matching attribute selectors", async () => {
  const doc = await fetchJllDirectDoc(url, 1000, {
    resolveHost: resolvePublic,
    requestPinned: async (target, address, signal) => {
      assert.equal(target.toString(), url);
      assert.equal(address, "93.184.216.34");
      assert.equal(signal.aborted, false);
      return response();
    },
  });
  assert.equal(doc.rawHtml, html);
  assert.match(doc.markdown, /Public property navigation/);
  assert.match(doc.markdown, /Full property description/);
  assert.match(doc.markdown, /<table>/);
  assert.match(doc.markdown, /10,000 SF/);
  assert.match(doc.markdown, /https:\/\/property.jll.com\/listings\/example-property\/flyer.pdf/);
  assert.ok(doc.images?.includes("https://images.example/property.jpg"));
  assert.equal(doc.attributes?.length, 5);
  assert.ok(doc.attributes?.some((block) => block.values.includes("https://videos.example/tour.mp4")));
  assert.equal(doc.metadata?.transport, "direct_http");
});

test("JLL direct rejects empty/mixed private DNS before opening sockets", async () => {
  for (const addresses of [[], ["127.0.0.1"], ["93.184.216.34", "10.0.0.1"]]) {
    await assert.rejects(fetchJllDirectDoc(url, 1000, {
      resolveHost: async () => addresses,
      requestPinned: async () => { assert.fail("must not open socket"); },
    }), /non-public/);
  }
});

test("JLL direct validates every redirect target and its newly resolved DNS", async () => {
  let calls = 0;
  let resolutions = 0;
  await assert.rejects(fetchJllDirectDoc(url, 1000, {
    resolveHost: async () => ++resolutions === 1 ? ["93.184.216.34"] : ["127.0.0.1"],
    requestPinned: async () => { calls++; return { ...response(""), status: 302, location: "/listings/redirected" }; },
  }), /non-public/);
  assert.equal(calls, 1);
  for (const location of ["http://property.jll.com/listings/a", "https://evil.example/listings/a", "/api/graphql"]) {
    calls = 0;
    await assert.rejects(fetchJllDirectDoc(url, 1000, {
      resolveHost: resolvePublic,
      requestPinned: async () => { calls++; return { ...response(""), status: 302, location }; },
    }), /approved/);
    assert.equal(calls, 1);
  }
  calls = 0;
  await assert.rejects(fetchJllDirectDoc(url, 1000, {
    resolveHost: resolvePublic,
    requestPinned: async () => { calls++; return { ...response(""), status: 302, location: "/listings/loop" }; },
  }), /redirect.*limit/);
  assert.equal(calls, 4);
  calls = 0;
  const redirected = await fetchJllDirectDoc(url, 1000, {
    resolveHost: resolvePublic,
    requestPinned: async () => ++calls === 1
      ? { ...response(""), status: 308, location: "/listings/example-property/" }
      : response(),
  });
  assert.equal(redirected.metadata?.sourceURL, `${url}/`);
  assert.equal(calls, 2);
});

test("JLL direct bounds DNS, the complete request, content type, status, and response size", async () => {
  await assert.rejects(fetchJllDirectDoc(url, 10, { resolveHost: async () => new Promise(() => {}) }), /deadline/);
  let requestSignal: AbortSignal | undefined;
  await assert.rejects(fetchJllDirectDoc(url, 250, {
    resolveHost: resolvePublic,
    requestPinned: async (_, __, signal) => { requestSignal = signal; return new Promise(() => {}); },
  }), /deadline/);
  assert.equal(requestSignal?.aborted, true);
  for (const bad of [
    { ...response(), contentType: "application/json" },
    { ...response(), status: 503 }, response(" "),
    response("x".repeat(JLL_DIRECT_MAX_BYTES + 1)),
  ]) {
    await assert.rejects(fetchJllDirectDoc(url, 1000, {
      resolveHost: resolvePublic, requestPinned: async () => bad,
    }));
  }
});

test("JLL process-wide direct pacing applies to concurrent callers and removes expired waiters", async () => {
  const starts: number[] = [];
  const dependencies = {
    resolveHost: resolvePublic,
    requestPinned: async () => { starts.push(Date.now()); return response(); },
  };
  await Promise.all(Array.from({ length: 4 }, () => fetchJllDirectDoc(url, 2000, dependencies)));
  assert.equal(starts.length, 4);
  for (let i = 1; i < starts.length; i++) {
    assert.ok(starts[i] - starts[i - 1] >= JLL_DIRECT_START_INTERVAL_MS);
  }
  let expiredStarts = 0;
  await assert.rejects(fetchJllDirectDoc(url, 10, {
    resolveHost: resolvePublic,
    requestPinned: async () => { expiredStarts++; return response(); },
  }), /deadline|abort/i);
  // A later waiter progresses and proves the cancelled task never dispatches.
  await fetchJllDirectDoc(url, 1000, dependencies);
  assert.equal(expiredStarts, 0);
  assert.equal(starts.length, 5);
});

test("JLL deadline during DNS prevents late resolution from opening a socket", async () => {
  let release!: (addresses: string[]) => void;
  let sockets = 0;
  const pending = fetchJllDirectDoc(url, 10, {
    resolveHost: async () => new Promise<string[]>((resolve) => { release = resolve; }),
    requestPinned: async () => { sockets++; return response(); },
  });
  await assert.rejects(pending, /deadline/);
  release(["93.184.216.34"]);
  await new Promise(resolve => setTimeout(resolve, JLL_DIRECT_START_INTERVAL_MS + 10));
  assert.equal(sockets, 0);
});

test("JLL pinned request preserves TLS host, pins lookup, and rejects oversized and aborted streams", async () => {
  for (const mode of ["success", "length", "stream", "abort", "error"] as const) {
    let destroyed = false;
    const fakeRequest = ((_url: URL, options: any, callback: (res: any) => void) => {
      assert.equal(_url.hostname, "property.jll.com");
      assert.equal(options.agent, false);
      assert.equal(options.headers["cache-control"], "no-cache");
      options.lookup("property.jll.com", { all: true }, (_: any, records: any) => {
        assert.deepEqual(records, [{ address: "93.184.216.34", family: 4 }]);
      });
      const req: any = new EventEmitter();
      req.destroy = () => { destroyed = true; };
      req.end = () => queueMicrotask(() => {
        const res: any = new EventEmitter();
        res.headers = { "content-type": "text/html", ...(mode === "length" ? { "content-length": JLL_DIRECT_MAX_BYTES + 1 } : {}) };
        res.statusCode = 200;
        res.destroy = () => { destroyed = true; };
        callback(res);
        if (mode === "length") return;
        if (mode === "abort") { res.emit("aborted"); return; }
        if (mode === "error") { res.emit("error", new Error("broken stream")); return; }
        res.emit("data", mode === "stream" ? Buffer.alloc(JLL_DIRECT_MAX_BYTES + 1) : Buffer.from(html));
        res.emit("end");
      });
      return req;
    }) as any;
    const pending = requestJllPinned(new URL(url), "93.184.216.34", new AbortController().signal, fakeRequest);
    if (mode === "success") assert.equal((await pending).body, html);
    else await assert.rejects(pending);
    if (mode === "length" || mode === "stream") assert.equal(destroyed, true);
  }
});

test("JLL opt-in preserves enrichment identity/assets and current-generation provenance; fallback remains browser", async () => {
  const cacheDir = mkdtempSync(join(tmpdir(), "jll-direct-test-"));
  const savedEnv = { ...process.env };
  const originalScrape = firecrawl.scrape;
  let browserCalls = 0;
  (firecrawl as any).scrape = async () => {
    browserCalls++;
    return { ...jllDirectHtmlDoc(html, url), metadata: { transport: "firecrawl" } };
  };
  try {
    process.env.JLL_DETAIL_CACHE_DIR = cacheDir;
    process.env.CRE_REFRESH_GENERATION = "current-generation";
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    delete process.env.JLL_DETAIL_TRANSPORT;
    const base = { id: "123", url, inventoryObservedAt: "2026-09-09T00:00:00Z" };
    await enrichJllListing(base, { directFetch: async () => { assert.fail("default must be browser"); } });
    assert.equal(browserCalls, 1);
    for (const [index, direct] of [
      async () => jllDirectHtmlDoc(html, url),
      async () => { throw new Error("timeout"); },
      async () => jllDirectHtmlDoc("<p>Shell</p>", url),
      async () => jllDirectHtmlDoc(html.replace('"id":"123"', '"id":"456"'), url),
      async () => jllDirectHtmlDoc(html.replace('"pageUrl":"/listings/example-property"', '"pageUrl":"/listings/wrong"'), url),
    ].entries()) {
      process.env.JLL_DETAIL_TRANSPORT = "direct";
      process.env.CRE_REFRESH_GENERATION = `generation-${index}`;
      const before: number = browserCalls;
      const row = await enrichJllListing(base, { directFetch: direct });
      assert.equal(row.detailError, undefined);
      assert.equal(browserCalls - before, index === 0 ? 0 : 1);
      assert.equal(row.id, "123");
      assert.equal(row.canonicalUrl, url);
      assert.equal(row.contactsDetailed[0].email, "broker@jll.com");
      assert.ok(row.documents.some((doc: any) => doc.url === "https://docs.example/plan.pdf" && doc.docType === "floor_plan"));
      assert.ok(row.brochures.some((doc: any) => doc.url === "https://docs.example/brochure.pdf"));
      assert.ok(row.media.some((media: any) => /matterport/.test(media.url)));
      assert.ok(row.photos.includes("https://images.example/property.jpg"));
      assert.ok(row.links.length);
      assert.equal(row.freshnessProvenance.generationId, `generation-${index}`);
      assert.equal(row.freshnessProvenance.cacheDisposition, "live");
      assert.ok(Number.isFinite(Date.parse(row.detailObservedAt)));
      assert.equal(readJllDetailCache(url)?.detailObservation?.generationId, `generation-${index}`);
      if (index === 0) {
        assert.equal(row.preserveExistingMarkdown, true);
        assert.equal(row.freshnessProvenance.method, "jll_direct_detail");
        delete process.env.JLL_DETAIL_TRANSPORT;
        assert.equal(readJllDetailCache(url), null, "default browser must reject direct cache");
      } else {
        assert.equal(row.preserveExistingMarkdown, undefined);
        assert.equal(row.freshnessProvenance.method, "jll_detail");
      }
    }
  } finally {
    process.env = savedEnv;
    firecrawl.scrape = originalScrape;
    rmSync(cacheDir, { recursive: true, force: true });
  }
});
