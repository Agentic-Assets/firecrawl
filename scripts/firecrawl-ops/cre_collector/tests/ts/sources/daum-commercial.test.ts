import test from "node:test";
import assert from "node:assert/strict";
import {
  DAUM_PAGE_SIZE,
  DAUM_MAX_RESPONSE_BYTES,
  DaumInventoryRow,
  DaumPoliteTransport,
  assembleDaumSnapshot,
  assertDaumSnapshot,
  assertDaumUniqueProviderIds,
  canonicalDaumPropertyUrl,
  daumPageUrl,
  daumSnapshotIdentitySignature,
  daumTenure,
  daumBelongsToTx,
  fetchDaumSnapshot,
  mapDaumListing,
  parseDaumDetail,
  parseDaumSearchPage,
  resolveDaumTenure,
} from "../../../sources/daum-commercial.js";
import { resetBrokerStateForTests } from "../../../lib/broker.js";

function searchHtml(options: {
  total?: number;
  tenure?: string;
  url?: string;
  malformedData?: boolean;
} = {}): string {
  const total = options.total ?? 1;
  const tenure = options.tenure ?? "Sale- Investment";
  const url = options.url ?? "https://daumcommercial.com/property/main-street/";
  const data = JSON.stringify([
    {
      title: "100 Main Street",
      address: "100 Main Street, Phoenix, AZ, 85001",
      single_post_link: url,
      lat: "33.4484",
      lng: "-112.0740",
      image: "https://daumcommercial.com/wp-content/uploads/main.jpg",
      totalsqft: "12,500",
      "property-type": "Industrial",
      "link-to": url,
    },
  ]);
  return `
    <html><body>
      <div class="results-list-controls"><div class="results">${total} Results Showing</div></div>
      <div class="results-list wrap-row"><div class="item">
        <a class="photo" href="${url}">
          <div data-bg-image="url('https://daumcommercial.com/wp-content/uploads/thumb.jpg')"></div>
        </a>
        <div class="content">
          <p class="listing-type">${tenure}</p>
          <a class="red-text" href="${url}">100 Main Street</a>
          <p class="info-location">Phoenix <span>|</span> AZ <span>|</span> 85001</p>
        </div>
        <div class="content">
          <p>12,500 SF</p>
          <div class="sale-price">$ 3,250,000</div>
          <p>2 Spaces</p>
          <p>Industrial</p>
        </div>
        <div class="wrap-links-prop">
          <a class="red-text" href="${url}">Jane Doe</a>
          <a class="red-text" href="${url}">John Doe</a>
        </div>
      </div></div>
      <script>${options.malformedData ? "var propertySearchData = [;" : `var propertySearchData = ${data};`}</script>
    </body></html>
  `;
}

const detailHtml = `
  <html>
    <head>
      <link rel="canonical" href="https://daumcommercial.com/property/main-street/">
      <link rel="shortlink" href="https://daumcommercial.com/?p=50388">
    </head>
    <body>
      <section id="agent-contact"><div class="item">
        <a class="photo-link" data-bg-image="url('https://daumcommercial.com/agent.jpg')"></a>
        <a class="name" href="https://daumcommercial.com/employee/jane-doe/">Jane Doe</a>
        <div class="content"><p>100 Office Way</p><p>Phoenix | AZ | 85001</p></div>
        <div class="content">
          <p>License: AZ 123456</p>
          <p><a class="email" href="mailto:jane@example.com">jane@example.com</a></p>
          <p><a class="phone" href="tel:(602) 555-0100">P (602) 555-0100</a></p>
        </div>
      </div></section>
      <div class="main-section">
        <div class="title-property">100 Main Street</div>
        <ul class="state-city"><li>Phoenix</li><li>AZ</li><li>85001</li></ul>
        <div class="title-about">Sale- Investment</div>
        <div class="info-wrap">
          <div class="info-units">$ 3,250,000</div>
          <div class="info-units">12,500 SF</div>
          <a href="https://daumcommercial.com/wp-content/uploads/brochure.pdf">property brochure</a>
        </div>
        <div class="property-summary"><ul>
          <li><span class="title">Property Type:</span><span class="info">Industrial</span></li>
          <li><span class="title">Total Space Available:</span><span class="info">12,500 SF</span></li>
          <li><span class="title">Smallest Space:</span><span class="info">2,500 SF</span></li>
          <li><span class="title">Largest Space:</span><span class="info">10,000 SF</span></li>
          <li><span class="title">Zoning:</span><span class="info">M-1</span></li>
        </ul></div>
        <div class="units-info"><div class="left-side"><div class="content"><ul>
          <li>Dock-high loading</li><li>Fenced yard</li>
        </ul></div></div></div>
        <div data-bg-image="url('https://daumcommercial.com/wp-content/uploads/detail.jpg')"></div>
      </div>
    </body>
  </html>
`;

function inventory(overrides: Partial<DaumInventoryRow> = {}): DaumInventoryRow {
  return {
    url: "https://daumcommercial.com/property/main-street/",
    title: "100 Main Street",
    tenure: "sale",
    tenureText: "Sale- Investment",
    city: "Phoenix",
    state: "AZ",
    postalCode: "85001",
    sizeText: "12,500 SF",
    spaceCount: 2,
    assetType: "Industrial",
    salePriceText: "$ 3,250,000",
    leaseRateText: null,
    brokerNames: ["Jane Doe", "John Doe"],
    imageUrl: "https://daumcommercial.com/wp-content/uploads/main.jpg",
    latitude: 33.4484,
    longitude: -112.074,
    ...overrides,
  };
}

test("DAUM page URLs are clean path pagination with no query strings", () => {
  assert.equal(daumPageUrl(1), "https://daumcommercial.com/property-search/");
  assert.equal(daumPageUrl(7), "https://daumcommercial.com/property-search/page/7/");
  assert.equal(new URL(daumPageUrl(7)).search, "");
});

test("DAUM canonical property URLs reject query, fragments, foreign hosts, and malformed paths", () => {
  assert.equal(
    canonicalDaumPropertyUrl("https://daumcommercial.com/property/main-street/"),
    "https://daumcommercial.com/property/main-street/"
  );
  assert.equal(canonicalDaumPropertyUrl("https://daumcommercial.com/property/main-street/?x=1"), null);
  assert.equal(canonicalDaumPropertyUrl("https://example.com/property/main-street/"), null);
  assert.equal(canonicalDaumPropertyUrl("https://daumcommercial.com/property-search/"), null);
  assert.equal(canonicalDaumPropertyUrl("http://daumcommercial.com/property/main-street/"), null);
  assert.equal(canonicalDaumPropertyUrl("https://user@daumcommercial.com/property/main-street/"), null);
  assert.equal(canonicalDaumPropertyUrl("https://daumcommercial.com:8443/property/main-street/"), null);
});

test("DAUM tenure parser retains blank values as unknown and rejects new provider labels", () => {
  assert.equal(daumTenure("Lease"), "lease");
  assert.equal(daumTenure("Sublease"), "lease");
  assert.equal(daumTenure("Lease or Sale"), "sale_or_lease");
  assert.equal(daumTenure("Sale- User"), "sale");
  assert.equal(daumTenure(""), "unknown");
  assert.throws(() => daumTenure("Auction"), /unknown transaction type/);
});

test("DAUM search parser deterministically extracts tenure, price, space, brokers, image, and coordinates", () => {
  const page = parseDaumSearchPage(searchHtml());
  assert.equal(page.reportedTotal, 1);
  assert.equal(page.reportedPages, 1);
  assert.equal(page.rows.length, 1);
  assert.equal(page.rows[0].tenure, "sale");
  assert.equal(page.rows[0].salePriceText, "$ 3,250,000");
  assert.equal(page.rows[0].sizeText, "12,500 SF");
  assert.equal(page.rows[0].spaceCount, 2);
  assert.deepEqual(page.rows[0].brokerNames, ["Jane Doe", "John Doe"]);
  assert.equal(page.rows[0].imageUrl, "https://daumcommercial.com/wp-content/uploads/main.jpg");
  assert.equal(page.rows[0].longitude, -112.074);
});

test("DAUM search parser retains a blank-tenure card as unknown", () => {
  const page = parseDaumSearchPage(searchHtml({ tenure: "" }));
  assert.equal(page.rows[0].tenure, "unknown");
  assert.equal(page.rows[0].tenureText, null);
});

test("DAUM search parser recovers a blank card title from matched embedded data", () => {
  const html = searchHtml().replace(">100 Main Street</a>", "></a>");
  const page = parseDaumSearchPage(html);
  assert.equal(page.rows[0].title, "100 Main Street");
});

test("DAUM search parser rejects malformed data, duplicate embedded URLs, and card/map mismatch", () => {
  assert.throws(() => parseDaumSearchPage(searchHtml({ malformedData: true })), /malformed/);
  const duplicateData = searchHtml().replace(
    /var propertySearchData = (\[[\s\S]*?\]);/,
    (_, json) => `var propertySearchData = ${JSON.stringify([...JSON.parse(json), ...JSON.parse(json)])};`
  );
  assert.throws(() => parseDaumSearchPage(duplicateData), /duplicate URL/);
  const mismatch = searchHtml().replace(
    /"single_post_link":"[^"]+"/,
    '"single_post_link":"https://daumcommercial.com/property/other/"'
  ).replace(
    /"link-to":"[^"]+"/,
    '"link-to":"https://daumcommercial.com/property/other/"'
  );
  assert.throws(() => parseDaumSearchPage(mismatch), /absent from propertySearchData/);
});

test("DAUM snapshot rejects partial pages and cross-page duplicate identities", () => {
  const base = inventory();
  assert.throws(
    () =>
      assertDaumSnapshot([
        { reportedTotal: DAUM_PAGE_SIZE + 1, reportedPages: 2, rows: [base] },
      ]),
    /pages/
  );
  const fullFirst = Array.from({ length: DAUM_PAGE_SIZE }, (_, index) =>
    inventory({
      url: `https://daumcommercial.com/property/item-${index}/`,
      title: `Item ${index}`,
    })
  );
  assert.throws(
    () =>
      assertDaumSnapshot([
        { reportedTotal: DAUM_PAGE_SIZE + 1, reportedPages: 2, rows: fullFirst },
        {
          reportedTotal: DAUM_PAGE_SIZE + 1,
          reportedPages: 2,
          rows: [fullFirst[0]],
        },
      ]),
    /duplicate URL/
  );
});

test("DAUM cache-skewed totals retain a complete union but block lifecycle authority", () => {
  const first = Array.from({ length: DAUM_PAGE_SIZE }, (_, index) =>
    inventory({
      url: `https://daumcommercial.com/property/item-${index}/`,
      title: `Item ${index}`,
    })
  );
  const final = [
    inventory({
      url: "https://daumcommercial.com/property/final-item/",
      title: "Final Item",
    }),
  ];
  const snapshot = assembleDaumSnapshot([
    { reportedTotal: DAUM_PAGE_SIZE, reportedPages: 2, rows: first },
    {
      reportedTotal: DAUM_PAGE_SIZE + 1,
      reportedPages: 2,
      rows: final,
    },
  ]);
  assert.equal(snapshot.total, DAUM_PAGE_SIZE + 1);
  assert.equal(snapshot.rows.length, DAUM_PAGE_SIZE + 1);
  assert.equal(snapshot.lifecycleExact, false);
  assert.deepEqual(snapshot.reportedTotals, [DAUM_PAGE_SIZE, DAUM_PAGE_SIZE + 1]);
});

test("DAUM detail parser extracts stable shortlink ID and deterministic detail fields", () => {
  const detail = parseDaumDetail(
    detailHtml,
    "https://daumcommercial.com/property/main-street/"
  );
  assert.equal(detail.postId, 50388);
  assert.equal(detail.tenure, "sale");
  assert.equal(detail.salePriceText, "$ 3,250,000");
  assert.equal(detail.sizeText, "12,500 SF");
  assert.equal(detail.facts.Zoning, "M-1");
  assert.deepEqual(detail.highlights, ["Dock-high loading", "Fenced yard"]);
  assert.equal(detail.contacts[0].name, "Jane Doe");
  assert.equal(detail.contacts[0].license, "AZ 123456");
  assert.equal(detail.brochures[0].url, "https://daumcommercial.com/wp-content/uploads/brochure.pdf");
});

test("DAUM detail parser rejects missing shells, missing IDs, and canonical mismatches", () => {
  assert.throws(() => parseDaumDetail("<html>Page not found</html>"), /shell/);
  assert.throws(
    () => parseDaumDetail(detailHtml.replace(/<link rel="shortlink"[^>]+>/, "")),
    /shortlink ID/
  );
  for (const shortlink of [
    "https://example.com/?p=50388",
    "http://daumcommercial.com/?p=50388",
    "https://user@daumcommercial.com/?p=50388",
    "https://daumcommercial.com:8443/?p=50388",
    "https://daumcommercial.com/property/?p=50388",
    "https://daumcommercial.com/?p=50388&p=50388",
    "https://daumcommercial.com/?p=50388&preview=true",
    "https://daumcommercial.com/?p=0",
  ]) {
    assert.throws(
      () =>
        parseDaumDetail(
          detailHtml.replace(
            "https://daumcommercial.com/?p=50388",
            shortlink
          )
        ),
      /shortlink ID/
    );
  }
  assert.throws(
    () => parseDaumDetail(detailHtml, "https://daumcommercial.com/property/other/"),
    /identity mismatch/
  );
});

test("DAUM mapper rejects unresolved tenure so ingest cannot fall back to sale", () => {
  resetBrokerStateForTests();
  const detail = parseDaumDetail(
    detailHtml.replace("Sale- Investment", ""),
    "https://daumcommercial.com/property/main-street/"
  );
  assert.throws(
    () =>
      mapDaumListing(
        inventory({ tenure: "unknown", tenureText: null }),
        detail,
        "sale",
        "2026-07-30T18:00:00.000Z",
        "2026-07-30T18:01:00.000Z"
      ),
    /unresolved transaction tenure/
  );
  assert.equal(daumBelongsToTx("unknown", "sale"), false);
  assert.equal(daumBelongsToTx("unknown", "lease"), false);
});

test("DAUM mapper emits a recognized detail-resolved tenure with live freshness", () => {
  resetBrokerStateForTests();
  const detail = parseDaumDetail(
    detailHtml,
    "https://daumcommercial.com/property/main-street/"
  );
  const mapped = mapDaumListing(
    inventory({ tenure: "unknown", tenureText: null }),
    detail,
    "sale",
    "2026-07-30T18:00:00.000Z",
    "2026-07-30T18:01:00.000Z"
  );
  assert.equal(mapped.id, "50388");
  assert.equal(mapped.transactionType, "Sale");
  assert.equal(mapped.salePriceUsd, 3_250_000);
  assert.equal(mapped.freshnessProvenance.detailScope, "detail_page");
  assert.equal(mapped.freshnessProvenance.cacheDisposition, "live");
});

test("DAUM tenure resolution holds unknown and rejects inventory/detail disagreement", () => {
  assert.equal(resolveDaumTenure("unknown", "unknown"), "unknown");
  assert.equal(resolveDaumTenure("unknown", "lease"), "lease");
  assert.equal(resolveDaumTenure("sale", "unknown"), "sale");
  assert.throws(() => resolveDaumTenure("sale", "lease"), /disagrees/);
});

test("DAUM polite transport refuses query URLs before network access", async () => {
  let called = false;
  const transport = new DaumPoliteTransport(async () => {
    called = true;
    return new Response("<html></html>");
  }, 0);
  await assert.rejects(
    transport.get("https://daumcommercial.com/property-search/?page=2"),
    /forbids query URL/
  );
  assert.equal(called, false);
  await assert.rejects(
    transport.get("http://daumcommercial.com/property/main-street/"),
    /unsafe URL/
  );
  await assert.rejects(
    transport.get("https://user@daumcommercial.com/property/main-street/"),
    /unsafe URL/
  );
  await assert.rejects(
    transport.get("https://daumcommercial.com:8443/property/main-street/"),
    /unsafe URL/
  );
  assert.equal(called, false);
});

test("DAUM polite transport uses an empty POST to bypass path-page caches", async () => {
  const calls: Array<{ input: string; init?: RequestInit }> = [];
  const transport = new DaumPoliteTransport(async (input, init) => {
    calls.push({ input: String(input), init });
    return new Response("<html></html>");
  }, 0);
  await transport.post("https://daumcommercial.com/property-search/page/2/");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.redirect, "manual");
  assert.equal(calls[0].init?.body, "");
  assert.equal(
    new Headers(calls[0].init?.headers).get("Content-Type"),
    "application/x-www-form-urlencoded"
  );
});

test("DAUM polite transport rejects redirects and oversized bodies", async () => {
  const redirected = new DaumPoliteTransport(
    async () =>
      new Response("", {
        status: 302,
        headers: { Location: "https://example.com/" },
      }),
    0
  );
  await assert.rejects(
    redirected.get("https://daumcommercial.com/property/main-street/"),
    /HTTP 302/
  );

  const oversized = new DaumPoliteTransport(
    async () => new Response("x".repeat(32)),
    0,
    30_000,
    16
  );
  await assert.rejects(
    oversized.get("https://daumcommercial.com/property/main-street/"),
    /exceeds 16 bytes/
  );
  assert.ok(DAUM_MAX_RESPONSE_BYTES > 283_608);
});

test("DAUM identity convergence signature is ordered and field-change agnostic", () => {
  const one = inventory();
  const two = inventory({
    url: "https://daumcommercial.com/property/second/",
    title: "Second",
  });
  assert.equal(
    daumSnapshotIdentitySignature([one, two]),
    `${one.url}\t${one.tenure}\n${two.url}\t${two.tenure}`
  );
  assert.notEqual(
    daumSnapshotIdentitySignature([one, two]),
    daumSnapshotIdentitySignature([two, one])
  );
  assert.equal(
    daumSnapshotIdentitySignature([one]),
    daumSnapshotIdentitySignature([{ ...one, salePriceText: "$ 4,000,000" }])
  );
  assert.notEqual(
    daumSnapshotIdentitySignature([one]),
    daumSnapshotIdentitySignature([{ ...one, tenure: "lease" }])
  );
});

test("DAUM emitted provider IDs must be unique", () => {
  assert.doesNotThrow(() => assertDaumUniqueProviderIds([{ id: "1" }, { id: "2" }]));
  assert.throws(
    () => assertDaumUniqueProviderIds([{ id: "1" }, { id: "1" }]),
    /duplicate provider ID 1/
  );
});

test("DAUM full-pass convergence requires two identical consecutive identity snapshots", async () => {
  const firstUrl = "https://daumcommercial.com/property/first/";
  const finalUrl = "https://daumcommercial.com/property/final/";
  const pages = [
    searchHtml({ url: firstUrl }),
    searchHtml({ url: finalUrl }),
    searchHtml({ url: finalUrl }),
  ];
  let calls = 0;
  const snapshot = await fetchDaumSnapshot(
    {
      post: async () => pages[calls++],
    } as any,
    3
  );
  assert.equal(calls, 3);
  assert.equal(snapshot.total, 1);
  assert.equal(snapshot.lifecycleExact, true);
  assert.equal(snapshot.rows[0].url, finalUrl);
});

test("DAUM convergence requires stable normalized tenure for each identity", async () => {
  const pages = [
    searchHtml({ tenure: "Sale- Investment" }),
    searchHtml({ tenure: "Lease" }),
    searchHtml({ tenure: "Lease" }),
  ];
  let calls = 0;
  const snapshot = await fetchDaumSnapshot(
    { post: async () => pages[calls++] } as any,
    3
  );
  assert.equal(calls, 3);
  assert.equal(snapshot.rows[0].tenure, "lease");

  let divergentCalls = 0;
  await assert.rejects(
    () =>
      fetchDaumSnapshot(
        {
          post: async () =>
            [
              searchHtml({ tenure: "Sale- Investment" }),
              searchHtml({ tenure: "Lease" }),
            ][divergentCalls++],
        } as any,
        2
      ),
    /did not converge/
  );
});

test("DAUM full-pass convergence fails closed after its bounded pass budget", async () => {
  const urls = [
    "https://daumcommercial.com/property/first/",
    "https://daumcommercial.com/property/second/",
    "https://daumcommercial.com/property/third/",
  ];
  let calls = 0;
  await assert.rejects(
    fetchDaumSnapshot(
      {
        post: async () => searchHtml({ url: urls[calls++] }),
      } as any,
      3
    ),
    /did not converge/
  );
  assert.equal(calls, 3);
  await assert.rejects(
    fetchDaumSnapshot({ post: async () => searchHtml() } as any, 1),
    /integer from 2 to 5/
  );
});
