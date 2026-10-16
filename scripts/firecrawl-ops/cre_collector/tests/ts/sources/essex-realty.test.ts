import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  createPerformanceRecorder,
  resetPerformanceRecorderForTests,
  setPerformanceRecorderForTests,
  withPerformanceSource,
} from "../../../lib/performance.js";
import {
  createEssexTransport,
  essexAssetUrl,
  mapEssexDetail,
  parseEssexArchive,
  parseEssexDetail,
  parseEssexSitemap,
  reconcileEssexInventory,
  srcEssexRealty,
} from "../../../sources/essex-realty.js";

test("Essex asset URLs preserve benign queries and reject unsafe or sensitive URLs", () => {
  assert.equal(
    essexAssetUrl("https://cdn.example.com/photo.jpg?ver=2&w=1200"),
    "https://cdn.example.com/photo.jpg?ver=2&w=1200"
  );
  for (const unsafe of [
    "http://cdn.example.com/photo.jpg",
    "https://user@cdn.example.com/photo.jpg",
    "https://cdn.example.com:8443/photo.jpg",
    "https://cdn.example.com/photo.jpg?session=secret",
    "https://cdn.example.com/photo.jpg?download_token=secret",
    "https://cdn.example.com/photo.jpg?X-Amz-Credential=secret",
    "https://cdn.example.com/photo.jpg?ver=2&ver=3",
    "https://cdn.example.com/photo.jpg?w=0",
    "https://cdn.example.com/photo.jpg#token=secret",
  ]) {
    assert.equal(essexAssetUrl(unsafe), null);
  }
});

function card(slug: string, status: string, units = 12): string {
  return `
    <a href="https://essexrealtygroup.com/properties/${slug}/" class="property-card">
      <div class="property-card__label label">${status}</div>
      <span class="property-info__address--address-1">${slug} Street</span>
      <span class="property-info__address--address-2">Chicago, IL 60601</span>
      <span class="property-info__units">${units}</span>
    </a>`;
}

function archive(rows: string[]): string {
  return `<a class="allposts">ALL [${rows.length}]</a>${rows.join("")}`;
}

function detail(id = 1090741, status = "UNDER CONTRACT", slug = "100-main"): string {
  return `
    <html><head>
      <link rel="canonical" href="https://essexrealtygroup.com/properties/${slug}/">
      <meta property="og:url" content="https://essexrealtygroup.com/properties/${slug}/">
    </head><body class="single-properties postid-${id}">
    <section class="property-detail-wrapper">
      <div class="property-info__label label">${status} - MULTI-FAMILY</div>
      <span class="property-info__address--address-1">100 Main Street</span>
      <span class="property-info__address--address-2">Chicago, IL 60601</span>
      <div class="property-info__label">PRICE</div><div class="property-info__info">$9,775,000</div>
      <div class="property-info__label">NUMBER OF UNITS</div><div class="property-info__info">115</div>
      <div class="property-photos-carousel__photo" style="background-image:url(https://cdn.example/one.jpg)"></div>
    </section>
    <section class="body-text">
      <div class="body-text__subhead">PROPERTY LISTED</div><p>Detailed description.</p>
      <div class="body-text__subhead">HIGHLIGHTS</div><ul><li>Durable cash flow</li></ul>
    </section>
    <section class="property-brokers">
      <div class="small-broker-card">
        <div class="small-broker-card__name"><a href="https://essexrealtygroup.com/team/jane-doe/">Jane Doe</a></div>
        <div class="small-broker-card__job-title">Director</div>
        <div class="small-broker-card__contact">773.555.0100</div>
        <div class="small-broker-card__contact"><a href="mailto:jane@example.com">Email</a></div>
      </div>
    </section>
    <script>var currentPropertyId = ${id};</script></body></html>`;
}

test("Essex archive parses exact current lifecycle inventory", () => {
  const parsed = parseEssexArchive(
    archive([
      ...Array.from({ length: 24 }, (_, index) => card(`on-${index}`, "ON MARKET")),
      ...Array.from(
        { length: 19 },
        (_, index) => card(`contract-${index}`, "UNDER CONTRACT")
      ),
    ]),
    "current"
  );
  assert.equal(parsed.length, 43);
  assert.equal(parsed.filter((row) => row.lifecycle === "on_market").length, 24);
  assert.equal(parsed.filter((row) => row.lifecycle === "under_contract").length, 19);
});

test("Essex archive rejects malformed totals, identities, statuses, and shells", () => {
  assert.throws(
    () => parseEssexArchive(`<a class="allposts">ALL [2]</a>${card("one", "ON MARKET")}`, "current"),
    /completeness failed/
  );
  assert.throws(
    () => parseEssexArchive(`${archive([card("one", "ON MARKET")])}<span>ALL [1]</span>`, "current"),
    /exactly one/
  );
  assert.throws(
    () => parseEssexArchive(archive([card("one", "MYSTERY")]), "current"),
    /unknown or contradictory/
  );
  assert.throws(
    () => parseEssexArchive(archive([card("one", "CLOSED")]), "current"),
    /contradictory/
  );
  assert.throws(
    () => parseEssexArchive(archive([card("one", "ON MARKET"), card("one", "ON MARKET")]), "current"),
    /duplicate URL/
  );
  assert.throws(
    () => parseEssexArchive("<html>verify you are human</html>", "current"),
    /error shell/
  );
});

test("Essex sitemap reconciliation proves 43 current + 713 closed = 756", () => {
  const current = [
    ...Array.from({ length: 24 }, (_, index) => ({
      url: `https://essexrealtygroup.com/properties/on-${index}/`,
      lifecycle: "on_market" as const,
      name: `On ${index}`,
      location: null,
      units: null,
    })),
    ...Array.from({ length: 19 }, (_, index) => ({
      url: `https://essexrealtygroup.com/properties/contract-${index}/`,
      lifecycle: "under_contract" as const,
      name: `Contract ${index}`,
      location: null,
      units: null,
    })),
  ];
  const closed = Array.from({ length: 713 }, (_, index) => ({
    url: `https://essexrealtygroup.com/properties/closed-${index}/`,
    lifecycle: "closed" as const,
    name: `Closed ${index}`,
    location: null,
    units: null,
  }));
  const sitemapUrls = [...current, ...closed].map((row) => row.url);
  const reconciled = reconcileEssexInventory(current, closed, sitemapUrls);
  assert.deepEqual(reconciled.lifecycleCounts, {
    on_market: 24,
    under_contract: 19,
    closed: 713,
  });
  assert.equal(reconciled.sitemapUrls.length, 756);
});

test("Essex sitemap and lifecycle reconciliation fail closed on drift", () => {
  const sitemap = parseEssexSitemap(`
    <urlset>
      <url><loc>https://essexrealtygroup.com/properties/</loc></url>
      <url><loc>https://essexrealtygroup.com/properties/one/</loc></url>
    </urlset>`);
  assert.deepEqual(sitemap, ["https://essexrealtygroup.com/properties/one/"]);
  const current = parseEssexArchive(archive([card("one", "ON MARKET")]), "current");
  assert.throws(
    () =>
      reconcileEssexInventory(
        current,
        [{
          url: current[0].url,
          lifecycle: "closed",
          name: "One",
          location: null,
          units: null,
        }],
        sitemap
      ),
    /lifecycle overlap/
  );
  assert.throws(
    () => reconcileEssexInventory(current, [], [
      ...sitemap,
      "https://essexrealtygroup.com/properties/two/",
    ]),
    /reconciliation failed/
  );
  assert.throws(
    () =>
      parseEssexSitemap(`
        <urlset>
          <url><loc>https://essexrealtygroup.com/properties/one/</loc></url>
          <url><loc>https://essexrealtygroup.com/properties/one/</loc></url>
        </urlset>`),
    /duplicate URL/
  );
});

test("Essex detail retains observed currentPropertyId and matching lifecycle", () => {
  const parsed = parseEssexDetail(
    detail(),
    "https://essexrealtygroup.com/properties/100-main/",
    "under_contract"
  );
  assert.equal(parsed.id, "properties/100-main");
  assert.equal(parsed.essexPropertyId, "1090741");
  assert.equal(parsed.salePriceUsd, 9775000);
  assert.equal(parsed.units, 115);
  assert.equal(parsed.statusBadge, "Under Contract");
  assert.equal(parsed.city, "Chicago");
  assert.equal(parsed.state, "IL");
  assert.equal(parsed.postalCode, "60601");
  assert.deepEqual(parsed.highlights, ["Durable cash flow"]);
  assert.equal(parsed.contactsDetailed[0].email, "jane@example.com");

  assert.throws(
    () =>
      parseEssexDetail(
        detail(1090741, "ON MARKET"),
        "https://essexrealtygroup.com/properties/100-main/",
        "under_contract"
      ),
    /does not match archive/
  );
  assert.throws(
    () =>
      parseEssexDetail(
        detail().replace("var currentPropertyId = 1090741;", ""),
        "https://essexrealtygroup.com/properties/100-main/",
        "under_contract"
      ),
    /exactly one currentPropertyId/
  );
  assert.throws(
    () =>
      parseEssexDetail(
        detail(1090741, "UNDER CONTRACT", "different-property"),
        "https://essexrealtygroup.com/properties/100-main/",
        "under_contract"
      ),
    /canonical identity does not match/
  );
  assert.throws(
    () =>
      parseEssexDetail(
        detail().replace(/<link rel="canonical"[^>]+>|<meta property="og:url"[^>]+>/g, ""),
        "https://essexrealtygroup.com/properties/100-main/",
        "under_contract"
      ),
    /canonical identity does not match/
  );
  assert.throws(
    () =>
      parseEssexDetail(
        `${detail()}<script>var currentPropertyId = 2;</script>`,
        "https://essexrealtygroup.com/properties/100-main/",
        "under_contract"
      ),
    /exactly one currentPropertyId/
  );
});

test("Essex detail output drops asset queries and fragments outside the presentation allowlist", () => {
  const html = detail()
    .replace(
      "https://cdn.example/one.jpg",
      "https://cdn.example/one.jpg?session=synthetic-secret#token=synthetic-secret"
    )
    .replace(
      "</section>\n    <section class=\"property-brokers\">",
      `<a href="https://cdn.example/offering.pdf?session=synthetic-secret#token=synthetic-secret">
        Offering Memorandum
      </a></section>
    <section class="property-brokers">`
    );
  const parsed = parseEssexDetail(
    html,
    "https://essexrealtygroup.com/properties/100-main/",
    "under_contract"
  );
  assert.deepEqual(parsed.photos, []);
  assert.deepEqual(parsed.documents, []);
  assert.equal(JSON.stringify(parsed).includes("synthetic-secret"), false);
});

test("Essex mapping carries live detail freshness and monitor preservation", () => {
  const parsed = parseEssexDetail(
    detail(),
    "https://essexrealtygroup.com/properties/100-main/",
    "under_contract"
  );
  const mapped = mapEssexDetail(
    parsed,
    "2026-07-30T18:00:00.000Z",
    "2026-07-30T18:01:00.000Z",
    "generation-2",
    true
  );
  assert.equal(mapped.inventoryObservedAt, "2026-07-30T18:00:00.000Z");
  assert.equal(mapped.detailObservedAt, "2026-07-30T18:01:00.000Z");
  assert.equal(mapped.freshnessProvenance.generationId, "generation-2");
  assert.equal(mapped.freshnessProvenance.detailScope, "detail_page");
  assert.equal(mapped.freshnessProvenance.cacheDisposition, "live");
  assert.equal(mapped.preserveChildCollections, true);
});

test("Essex transport enforces host, byte bound, and Crawl-delay serially", async () => {
  const starts: number[] = [];
  let clock = 1_000;
  const realNow = Date.now;
  Date.now = () => clock;
  try {
    const transport = createEssexTransport(
      async () => {
        starts.push(clock);
        return new Response("ok", { status: 200 });
      },
      async (ms) => {
        clock += ms;
      },
      10_000
    );
    await transport.getText("https://essexrealtygroup.com/properties/");
    await transport.getText("https://essexrealtygroup.com/properties/one/");
    assert.deepEqual(starts, [10_000, 20_000]);
    await assert.rejects(
      () => transport.getText("https://evil.example/properties/one/"),
      /rejected URL/
    );
    const oversized = createEssexTransport(
      async () =>
        new Response("ok", {
          status: 200,
          headers: { "content-length": "100" },
        }),
      async () => {},
      0
    );
    await assert.rejects(
      () => oversized.getText("https://essexrealtygroup.com/properties/", 10),
      /exceeds/
    );
    let cancelled = false;
    const streaming = createEssexTransport(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(new TextEncoder().encode("abc"));
              controller.enqueue(new TextEncoder().encode("def"));
            },
            cancel() {
              cancelled = true;
            },
          })
        ),
      async () => {},
      0
    );
    await assert.rejects(
      () => streaming.getText("https://essexrealtygroup.com/properties/", 4),
      /exceeds 4 bytes/
    );
    assert.equal(cancelled, true);
  } finally {
    Date.now = realNow;
  }
});

test("Essex transport records bounded direct outcomes and pacing without changing errors", async () => {
  const dir = mkdtempSync(join(tmpdir(), "essex-direct-performance-"));
  const path = join(dir, "performance.json");
  let monotonic = 0;
  let wall = 1_000;
  const realDateNow = Date.now;
  const timeoutError = new DOMException("The request was aborted", "AbortError");
  const recorder = createPerformanceRecorder({
    path,
    runId: "2026-09-13T120000Z-abcdef123456",
    commandId: "0123456789abcdef0123456789abcdef",
    monotonicMs: () => monotonic,
    nowIso: () => new Date(Date.UTC(2026, 8, 13, 12) + monotonic).toISOString(),
    processResources: () => ({ rssBytes: 100, cpuUserMicros: 10, cpuSystemMicros: 5 }),
  });
  assert.ok(recorder);
  const calls: string[] = [];
  const transport = createEssexTransport(
    async (input) => {
      const url = String(input);
      calls.push(url);
      monotonic += 5;
      if (url === "https://essexrealtygroup.com/properties/") {
        return new Response("archive", { status: 200 });
      }
      if (url === "https://essexrealtygroup.com/properties/?sale_deal_status_id=3") {
        return new Response("upstream", { status: 503 });
      }
      if (url === "https://essexrealtygroup.com/properties-sitemap.xml") {
        throw timeoutError;
      }
      return new Response("", { status: 200, headers: { "content-length": "20" } });
    },
    async (ms) => {
      wall += ms;
      monotonic += ms;
    },
    10
  );
  Date.now = () => wall;
  setPerformanceRecorderForTests(recorder);
  try {
    await withPerformanceSource("essex-realty", "sale", async () => {
      assert.equal(await transport.getText("https://essexrealtygroup.com/properties/"), "archive");
      await assert.rejects(
        () => transport.getText("https://essexrealtygroup.com/properties/?sale_deal_status_id=3"),
        /HTTP 503/
      );
      await assert.rejects(
        () => transport.getText("https://essexrealtygroup.com/properties-sitemap.xml"),
        (error) => error === timeoutError
      );
      await assert.rejects(
        () => transport.getText("https://essexrealtygroup.com/properties/detail/", 1),
        /exceeds 1 bytes/
      );
    });
    recorder.flush(true);
    const snapshot = JSON.parse(readFileSync(path, "utf8"));
    const rows = snapshot.metrics.direct_provider_requests.by_source_transaction_role;
    const row = (role: string) => rows.find((entry: any) => entry.role === role);
    assert.deepEqual(calls, [
      "https://essexrealtygroup.com/properties/",
      "https://essexrealtygroup.com/properties/?sale_deal_status_id=3",
      "https://essexrealtygroup.com/properties-sitemap.xml",
      "https://essexrealtygroup.com/properties/detail/",
    ]);
    assert.deepEqual(
      [row("current_archive").succeeded, row("current_archive").status_counts["200"]],
      [1, 1]
    );
    assert.deepEqual(
      [row("closed_archive").failed, row("closed_archive").status_counts["503"], row("closed_archive").error_categories.http_5xx],
      [1, 1, 1]
    );
    assert.equal(row("sitemap").error_categories.timeout, 1);
    assert.deepEqual(
      [row("detail").error_categories.response_too_large, row("detail").status_counts["200"]],
      [1, 1]
    );
    for (const role of ["closed_archive", "sitemap", "detail"]) {
      assert.equal(row(role).pacing_waits_completed, 1);
      assert.equal(row(role).pacing_wait_ms_scheduled, 10);
      assert.equal(row(role).pacing_wait_ms_elapsed, 10);
    }
    assert.equal(snapshot.metrics.requests.attempts_started, 0);
    assert.doesNotMatch(JSON.stringify(snapshot), /https?:\/\/|upstream/);
  } finally {
    Date.now = realDateNow;
    resetPerformanceRecorderForTests();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("Essex pending direct request is visible in the real snapshot before completion", async () => {
  const dir = mkdtempSync(join(tmpdir(), "essex-direct-active-"));
  const path = join(dir, "performance.json");
  let monotonic = 0;
  let releaseFetch: ((response: Response) => void) | undefined;
  let notifyFetchStarted: (() => void) | undefined;
  const fetchStarted = new Promise<void>((resolve) => { notifyFetchStarted = resolve; });
  const recorder = createPerformanceRecorder({
    path,
    runId: "2026-09-13T120000Z-abcdef123456",
    commandId: "0123456789abcdef0123456789abcdef",
    monotonicMs: () => monotonic,
    nowIso: () => new Date(Date.UTC(2026, 8, 13, 12) + monotonic).toISOString(),
    processResources: () => ({ rssBytes: 100, cpuUserMicros: 10, cpuSystemMicros: 5 }),
  });
  assert.ok(recorder);
  const transport = createEssexTransport(
    async () => {
      notifyFetchStarted?.();
      return new Promise<Response>((resolve) => { releaseFetch = resolve; });
    },
    async () => {},
    0
  );
  setPerformanceRecorderForTests(recorder);
  try {
    recorder.recordSourceStarted({ source: "essex-realty", transaction: "sale" });
    const pending = withPerformanceSource("essex-realty", "sale", () =>
      transport.getText("https://essexrealtygroup.com/properties/")
    );
    await fetchStarted;

    const inProgress = JSON.parse(readFileSync(path, "utf8"));
    const current = inProgress.metrics.direct_provider_requests.by_source_transaction_role
      .find((row: any) => row.role === "current_archive");
    assert.equal(inProgress.terminal, false);
    assert.equal(current.attempts_started, 1);
    assert.equal(current.attempts_completed, 0);
    assert.equal(current.active_locally_awaited, 1);
    assert.deepEqual(current.latency_histogram, {
      bounds_ms: [100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000, 60_000, 120_000],
      counts: Array(11).fill(0),
    });

    monotonic = 25_000;
    releaseFetch?.(new Response("archive", { status: 200 }));
    assert.equal(await pending, "archive");
    recorder.recordSourceCompleted(
      { source: "essex-realty", transaction: "sale" },
      "succeeded",
      0
    );
    recorder.flush(true);
    const terminal = JSON.parse(readFileSync(path, "utf8"));
    assert.equal(terminal.terminal, true);
    const completed = terminal.metrics.direct_provider_requests.by_source_transaction_role
      .find((row: any) => row.role === "current_archive");
    assert.equal(completed.attempts_completed, 1);
    assert.equal(completed.active_locally_awaited, 0);
    assert.equal(completed.status_counts["200"], 1);
  } finally {
    resetPerformanceRecorderForTests();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("Essex monitor/full adapter always resolves stable detail identity", async () => {
  const responses = new Map<string, string>([
    [
      "https://essexrealtygroup.com/properties/",
      archive([card("one", "UNDER CONTRACT")]),
    ],
    [
      "https://essexrealtygroup.com/properties/?sale_deal_status_id=3",
      archive([card("closed-one", "CLOSED")]),
    ],
    [
      "https://essexrealtygroup.com/properties-sitemap.xml",
      `<urlset>` +
        `<url><loc>https://essexrealtygroup.com/properties/one/</loc></url>` +
        `<url><loc>https://essexrealtygroup.com/properties/closed-one/</loc></url>` +
      `</urlset>`,
    ],
    [
      "https://essexrealtygroup.com/properties/one/",
      detail(1090741, "UNDER CONTRACT", "one"),
    ],
  ]);
  const transport = {
    async getText(url: string): Promise<string> {
      const response = responses.get(url);
      if (!response) throw new Error(`unexpected ${url}`);
      return response;
    },
  };
  const monitor = await srcEssexRealty("sale", 1, true, transport);
  const full = await srcEssexRealty("sale", 1, false, transport);
  assert.equal(monitor.totalAvailable, 1);
  assert.equal(monitor.listings[0].id, "properties/one");
  assert.equal(monitor.listings[0].essexPropertyId, "1090741");
  assert.equal(monitor.listings[0].preserveChildCollections, true);
  assert.equal(full.listings[0].id, "properties/one");
  assert.equal(full.listings[0].essexPropertyId, "1090741");
  assert.equal(full.listings[0].preserveChildCollections, undefined);
});

test("Essex keys preserve literal property paths without decoding or lowercasing", () => {
  for (const slug of ["Existing-Property", "New-Property", "Unit%2FBuilding"]) {
    const parsed = parseEssexDetail(
      detail(1090741, "ON MARKET", slug),
      `https://essexrealtygroup.com/properties/${slug}/`,
      "on_market"
    );
    assert.equal(parsed.id, `properties/${slug}`);
    assert.equal(parsed.essexPropertyId, "1090741");
  }
  for (const invalidId of [0, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(
      () => parseEssexDetail(
        detail(invalidId),
        "https://essexrealtygroup.com/properties/100-main/",
        "under_contract"
      ),
      /currentPropertyId is invalid/
    );
  }
  for (const suffix of ["", "?view=one#summary"]) {
    assert.equal(
      parseEssexDetail(
        detail(),
        `https://essexrealtygroup.com/properties/100-main${suffix}`,
        "under_contract"
      ).id,
      "properties/100-main"
    );
  }
  for (const path of ["/team/one/", "/properties/", "/properties/one/nested/"]) {
    assert.throws(
      () => parseEssexDetail(
        detail().replaceAll("/properties/100-main/", path),
        `https://essexrealtygroup.com${path}`,
        "under_contract"
      ),
      /requested URL is invalid/
    );
  }
});

test("Essex retains independent WordPress-page and canonical-path uniqueness", async () => {
  for (const duplicate of ["provider", "path"]) {
    const first = "https://essexrealtygroup.com/properties/one/";
    const second = duplicate === "provider"
      ? "https://essexrealtygroup.com/properties/two/"
      : `${first}?view=other`;
    const responses = new Map([
      ["https://essexrealtygroup.com/properties/", archive([
        card("one", "ON MARKET"),
        card("two", "ON MARKET").replace("https://essexrealtygroup.com/properties/two/", second),
      ])],
      ["https://essexrealtygroup.com/properties/?sale_deal_status_id=3", archive([card("closed-one", "CLOSED")])],
      ["https://essexrealtygroup.com/properties-sitemap.xml", `<urlset><url><loc>${first}</loc></url><url><loc>${second}</loc></url><url><loc>https://essexrealtygroup.com/properties/closed-one/</loc></url></urlset>`],
      [first, detail(1090741, "ON MARKET", "one")],
      [second, detail(1090742, "ON MARKET", duplicate === "provider" ? "two" : "one").replace("postid-1090742", duplicate === "provider" ? "postid-1090741" : "postid-1090742")],
    ]);
    await assert.rejects(
      () => srcEssexRealty("sale", Infinity, false, {
        async getText(url: string) {
          const response = responses.get(url);
          assert.notEqual(response, undefined, "unexpected offline request");
          return response!;
        },
      }),
      duplicate === "provider" ? /duplicate WordPress/ : /duplicate.*path/
    );
  }
});

test("Essex real distinct WordPress pages preserve shared raw variable without false duplicate", async () => {
  const slugs = ["1157-west-18th-street", "1423-w-cortez-st"];
  const urls = slugs.map(slug => "https://essexrealtygroup.com/properties/" + slug + "/");
  const bodies = [15709, 15875].map(post => readFileSync("tests/fixtures/essex-page-" + post + ".html", "utf8"));
  const responses = new Map([
    ["https://essexrealtygroup.com/properties/", archive([card(slugs[0], "UNDER CONTRACT"), card(slugs[1], "ON MARKET")])],
    ["https://essexrealtygroup.com/properties/?sale_deal_status_id=3", archive([card("closed-one", "CLOSED")])],
    ["https://essexrealtygroup.com/properties-sitemap.xml", "<urlset>" + [...urls, "https://essexrealtygroup.com/properties/closed-one/"].map(url => "<url><loc>" + url + "</loc></url>").join("") + "</urlset>"],
    ...urls.map((url, i) => [url, bodies[i]] as [string, string]),
  ]);
  const result = await srcEssexRealty("sale", Infinity, false, {async getText(url) {
    assert.ok(responses.has(url), "unexpected offline request");
    return responses.get(url)!;
  }});
  assert.deepEqual(result.listings.map(row => row.essexPropertyId), ["1090741", "1090741"]);
  assert.deepEqual(result.listings.map(row => row.essexWordpressPage), urls.map((canonicalUrl, i) => ({
    type: "wordpress_properties_post", id: String([15709, 15875][i]), canonicalUrl,
  })));
});

test("Essex requires one unambiguous positive WordPress property page identity", () => {
  const primary = readFileSync("tests/fixtures/essex-page-15709.html", "utf8");
  const url = "https://essexrealtygroup.com/properties/1157-west-18th-street/";
  for (const classes of [
    "single-properties", "single-properties postid-0", "single-properties postid-01",
    "single-properties postid-15709 postid-15875",
    "single-properties postid-15709 postid-15709",
    "single-post postid-15709", "single-properties single-post postid-15709",
    "single-properties postid-not-numeric",
    "single-properties postid-9007199254740992",
  ]) {
    assert.throws(() => parseEssexDetail(primary.replace(/<body class="[^"]*">/, '<body class="' + classes + '">'), url, "under_contract"), /WordPress/);
  }
  assert.throws(() => parseEssexDetail(primary.replace("</body>", '<body class="single-properties postid-15875"></body></body>'), url, "under_contract"), /WordPress/);
  assert.throws(() => parseEssexDetail(primary.replace("postid-15709", "postid-15709 postid-15875"), url, "under_contract"), /WordPress/);

});
