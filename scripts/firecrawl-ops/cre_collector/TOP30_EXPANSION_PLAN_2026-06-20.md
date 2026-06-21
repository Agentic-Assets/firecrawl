# Top-30 CRE Source Expansion — Collector Plan & Research (2026-06-20)

Audience: whoever next works the `cre_collector` bulk pipeline. This is the
research + plan for taking the collector from its current 12 working brokerages
toward the **top ~30 US CRE firms by transaction volume**, plus a **verified
pilot (Matthews)** proving the onboarding path end-to-end.

Companion artifacts:
- Sibling display repo `dynamically-display-cre-listing-data` already has an
  approved **design** (`docs/superpowers/specs/2026-06-19-top-30-brokerages-design.md`),
  an **implementation plan** (`docs/superpowers/plans/2026-06-19-top-30-brokerages-foundation-and-pilot.md`),
  and a **detail-page probe survey** (`docs/probe-results/top-30-candidate-platform-survey-2026-06-19.md`).
  That work is the *display* side (live-parse one listing URL). This doc is the
  *collector* side (bulk-enumerate a firm's whole catalog into `credeals`).
- Current per-source matrix: `BROKERAGE_STATUS_2026-06-12.md`, `START_HERE.md`.

---

## TL;DR

1. **Two different problems.** The display app needs one *fetchable detail page*.
   The collector needs a way to *enumerate every listing a firm has* (sitemap /
   search API / inventory API). A curl-able detail page is necessary but **not
   sufficient** for the collector. This is the crux and it makes the collector
   expansion materially heavier than the display plan implied.
2. **You cannot literally scrape "the top 30 by volume."** Of the true top 30,
   roughly **5–6 are advisory / tenant-rep / capital-markets firms with no public
   listing catalog** (Eastdil, Cresa, Hughes Marino, Knight Frank-US, agency
   lenders). The achievable goal is **~24 scrapable firms in/near the top 30,
   backfilled with the next-most-significant scrapable firms** to honestly reach
   a "30 sources" count.
3. **12 of the top 30 are already covered** (CBRE, C&W, JLL, Colliers, Newmark,
   M&M, Avison Young, Lee, Savills, NAI, Transwestern*, SVN). *(Transwestern is
   seeded but `UNSUPPORTED`; Colliers likewise.)*
4. **Pilot done & verified locally: Matthews.** `srcMatthews` enumerates the full
   catalog from the public `sitemap.xml` (3,564 listings: 2,913 sale + 651 lease),
   fetches server-rendered detail pages through local Firecrawl, and parses clean
   rows that stage into `credeals`. See §6.
5. **Three high-leverage collector paths the display survey did not exploit**
   (§4): (a) the **Buildout inventory API** the collector *already has*
   (`srcBuildout`) absorbs *any* Buildout firm by token, even client-rendered
   ones the display app can't use; (b) a **Crexi adapter** unlocks a whole cluster
   of net-lease firms at once; (c) **public JSON APIs behind SPAs** are usually
   the cleanest bulk path and are unprobed.

---

## Progress log (2026-06-20)

Firm-by-firm onboarding (code + local verification; production ingest pending go-ahead):

| Firm | Method | Enumeration | Field quality | Status |
|------|--------|-------------|---------------|--------|
| **Matthews** | `sitemap.xml` → server-rendered detail | 3,564 (2,913 sale + 651 lease) | clean on 12-row probe; 100% address/photos/type | ✅ coded + verified locally |
| **Franklin Street** | dual Buildout inventory feeds (sale + lease tokens) | 422 (227 sale + 195 lease) | clean on 12-row probe both tenures | ✅ coded + verified locally |
| **Lyon Stahl** | own WordPress property sitemap → JSON-LD detail pages | 2,236 active sale (sale-only firm) | addresses/photos/names complete on 15-row probe; cap rates clamped to plausible; price disclosed ~⅓ (source reality) | ✅ coded + verified locally |
| Coldwell Banker Commercial | — | — | — | ↩︎ reclassified: site changed, no Buildout token; `/search` is a custom app with internal `/api/` + reCAPTCHA → **Tier B**, not the Buildout quick-win the display survey implied |

| **Faris Lee Investments** | Buildout plugin inventory API (single token) | 77 active sale (sale-only) | clean addresses/prices/types/brokers on probe | ✅ coded + verified locally |
| **Fortis Net Lease** | Buildout inventory API | 86 (sale) | clean | ✅ verified |
| **8 regional Buildout firms** (Unique Properties, Kiser Group, Pinnacle REA, Cawley Chicago, Bradford Allen, Hudson Peters, Gibson Commercial, Leibsohn) | Buildout inventory API via `BUILDOUT_FIRMS` map | 684/351/179/80/79/39/16/75 ≈ **1,503** | clean address/price/type/size on sampled probes | ✅ coded + verified locally |
| **5 more Buildout firms — round 4** (NAI Hiffman, NAI Martens, Bull Realty, TRI Commercial, Berger Commercial) | Buildout inventory API via `BUILDOUT_FIRMS` map | 435/160/552/288/109 ≈ **1,544** | clean on sampled probes (NAI members + regional independents) | ✅ coded + verified locally |

**Session total: 18 firms onboarded (~9,400 listings staged), collector now wires ~28
firms via collect.ts.** Sitemap backlog (own sitemap, need LLM-extraction/DOM parser)
has grown: DAUM Commercial (~1,764), Interra (~865), Essex (~745), SHOP (~528), Pyramid
(~1,115), Foundry (~796), Velocity (~108), Ackerman & Co (~125), Maury Carter (~120),
Finial (~127), AQUILA (~85) — ~6,400 more behind the LLM-extraction approach.

**Method that's working (per user direction "mimic the brokerages that work"):** a
threaded discovery sweep that, per firm, (1) greps `/`, `/properties`, `/listings`
for a Buildout `plugins/{40-hex}` token and tests it against
`buildout.com/plugins/{token}/inventory.json`, and (2) follows `sitemap.xml`/
`sitemap_index.xml` for individual `/property|listing/{slug}` URLs. **Buildout is a
rich vein** — regional investment-sales/multifamily firms (Denver, Chicago, Dallas,
TX) are heavily on it, and each onboards by adding one line to the `BUILDOUT_FIRMS`
map (zero new parser code). Sweep script pattern lives in the session transcript;
re-run with fresh candidate firms to keep adding.

**Sitemap backlog (own property sitemap, need a JSON-LD/DOM parser like Lyon Stahl —
next batch):** Interra Realty (~865, Chicago multifamily), Finial Group (~127),
Velocity Retail (~108, Phoenix retail), AQUILA Commercial (~85, Austin),
Foundry Commercial (~796, thinner LD). Vestian (~26) is occupier/tenant-rep — verify
the URLs are real listings before onboarding.

**Finding (2026-06-20): a single generic JSON-LD sitemap parser does NOT work for
this backlog.** Probed one detail page from each: only Lyon Stahl exposes rich
property JSON-LD (Product/ApartmentComplex with offers/address/sqft). Interra, Essex,
Pyramid, SHOP, Velocity, AQUILA, Finial, Fuller expose only WebPage/Organization/
BreadcrumbList LD (or none); Foundry/Millennium have thin RealEstateListing/Agent
nodes (name/description/image, no offers/address). So their listing fields live in
**heterogeneous WordPress DOM** (theme-specific) or load via JS. Options: (a) a
bespoke DOM parser per firm (low leverage), or (b) **Firecrawl LLM structured
extraction** (`json`/`extract` format with a fixed CRE schema) over the
sitemap-enumerated detail URLs — one extraction schema across all heterogeneous
themes. (b) is the promising unlock and is the kind of strategy the Codex audit
(below) should evaluate. Buildout remains the clean high-leverage path; keep mining it.

Also this session: added a shared `title` field to `brokerRef` (so JSON-LD `Person`
job titles carry through — already read by `cre_ingest.py`), and `ldType`/`metaContent`
helpers on top of the existing recursive `jsonLdObjects()` walker.

### Codex audit fixes applied (2026-06-20)

An external Codex audit reviewed the session's work. Verified each finding against
the code and applied the data-safety + correctness ones:
- **Completeness contract (Critical):** added `incomplete?: boolean` to `SourceResult`.
  Matthews/Lyon Stahl count detail-fetch failures (NOT intentional sold-comp skips);
  `srcBuildout` sets it when Buildout pages are skipped. `cre_ingest.py` now makes a
  brokerage **mark-missing INELIGIBLE** if any of its source passes is incomplete — so
  a flaky partial run can no longer soft-delete live rows.
- **Buildout response validation (High):** `buildoutInventory` throws (and caches the
  failure) if a response lacks `inventory[]` or has a non-numeric `meta.total`,
  instead of caching an empty feed from an interstitial.
- **Buildout dual-mode dedup (High):** the `?propertyId=` suffix-strip that merges a
  sale+lease pair to one `sale_or_lease` row now applies to ALL Buildout firms
  (URL-pattern keyed), not just `svn`/`lee-associates`. Verified: Franklin Street rows
  stage with `external_id` = propertyId base.
- **Recursive JSON-LD (Medium):** Lyon Stahl now uses the recursive `jsonLdObjects()`
  walker (handles nested `@graph`); removed the shallow `extractLdNodes`.
- **moneyToNumber (Medium):** handles `$1.2M`/`$950K`/`$1.2 million` suffixes.
- **Concurrency (Medium):** hard ceiling lowered 6→4 (5 crashed OrbStack); default 3.
- **Docs (Low):** corrected "7 regional Buildout firms" → 8 (incl. Leibsohn).

Reasoned-deferred (quality enhancements, not safety): richer per-source telemetry
(attempted/parsed/failed counts) beyond the incomplete flag; Lyon Stahl sold-comp
detection via multiple signals (current `offers.availability` check is a reasonable
first pass; multi-signal risks over-filtering); Matthews tenure cross-check against
detail-page labels (slug partition is reliable in practice). The Franklin dual-token
sale-filter edge case is moot now that #4 merges dual-mode pairs.

### Codex audit v2 fixes applied (2026-06-20)

Second audit (after the Buildout fleet + LLM-extraction source). Applied:
- **Capped-run mark-missing guard (Critical):** `cre_ingest.py` now DISABLES
  `--mark-missing` entirely when any input run was `--max-items`-capped (a sample is
  not a full catalog). Closes the hole where a clean-but-low-yield run could
  inactivate a larger live population. Verified the guard fires.
- **Buildout per-page validation (High):** every inventory page (not just page 0) now
  requires `Array.isArray(inventory)`; a shapeless interstitial on a later page counts
  as a failed page (→ `incomplete`) instead of a silent empty.
- **LLM lease-rate / tenure hardening (High):** `sanitizeExtracted` now requires a real
  currency/rate signal ($ or PSF / /SF / /mo / /yr / NNN) before keeping a lease rate
  OR inferring lease tenure — a hallucinated "1 acre"/"2025" can't flip a listing to
  lease. Unknown tenure infers only from validated evidence, else defaults to sale.
- **Sitemap undercount (High):** `enumerateSitemap` now follows ALL same-host
  sub-sitemaps (property/listing-named first), not just name-matched ones, and logs
  when a sub-sitemap cap is exceeded — so listings in a generically-named child sitemap
  aren't silently missed (which would look "complete").

Codex confirmed as already-correct: `sale_or_lease` rows MERGE not duplicate
(URL-slug `external_id` + `(brokerage_id, external_id)` upsert); the `doc.json` SDK
response path; the generalized Buildout `?propertyId=` dedup; and that a `collect.ts`
run cannot delete the legacy Colliers/Transwestern rows.

Reasoned-deferred from v2: full-recursive JSON-LD walker (`jsonLdObjects` is shared by
srcJll/srcNewmark/srcLyonStahl — making it descend arbitrary nested properties risks
changing their behavior; current @graph+array recursion covers every live use; revisit
only when a nested-JSON-LD firm is added); low-field-fill → incomplete (risks false
positives on genuinely-sparse firms); preserving raw LLM JSON + schema/prompt hashes
(useful provenance, deferred to the incremental-cache build). **Highest-ROI next build
(Codex-confirmed): persistent incremental LLM extraction cache keyed by
sourceKey+url+sitemap `<lastmod>`+schema/prompt/model hash — turns repeat ~6,400-page
runs into near-zero LLM spend.**

### Tier-B (high-volume SPA) finding (2026-06-20) — these firms gate/JS-hide their catalogs

Probed the highest-volume gap firms for a clean GET catalog. None has one:
- **Kidder Mathews** (`kidder.com`) — WordPress with NO property post type; `/properties`
  is a 26KB JS shell that fetches an API not discoverable from static HTML. Hidden API.
- **Northmarq** (`northmarq.com`) — Drupal; server-renders ~105 cards but each
  `data-listing-url` points to **RCM deal rooms** (`my.rcm1.com`, qualified-investor
  gated). Public data is card-level only (name + type) — too thin to be "clean."
- **Stream Realty** (`streamrealty.com`) — its `properties-sitemap.xml` lists only 16
  **market hub** pages (`/properties/dallas/`); individual listings load via JS per market.
- **Voit** (`voitco.com`) — sitemap is press releases; `/property-search` is JS.
- **Berkadia, Walker & Dunlop, Sands Investment Group, Kidder** — custom JS SPAs
  (confirmed by the display survey + own-site probes); no sitemap / hidden API.

**Takeaway:** the clean public-catalog firms are **net-lease / regional shops with
own sitemaps or Buildout** (Matthews, Franklin Street, Lyon Stahl, Faris Lee — all
done), NOT the high-volume national capital-markets firms (whose investment-sales
inventory is gated to qualified investors or hidden behind JS search apps). Reaching
those needs either per-firm hidden-API reverse-engineering (capture the XHR via a
real browser / Firecrawl render — heavier, fragile) or accepting rendered-grid
**partial** sources (first page only, fails the "all listings" bar). Decision needed
before investing there.

**Documented clean-ish backlog (own sitemap, but thinner structured data → needs a
bespoke DOM parser, moderate quality):**
- **Foundry Commercial** (`foundrycommercial.com`) — ✓ sitemap with ~796 individual
  `/property/{slug}` URLs + `RealEstateListing` JSON-LD, but the LD carries only
  name/description/image (rich free-text specs, no structured address/price/offer);
  tenure/type available via `property_status`/`property_type` sub-sitemaps. Buildable
  as a Matthews-style DOM parser; expect strong name/description/images/type but
  weaker address/price. Good next "moderate" firm if approved.

**Lessons captured this session:**
- **Buildout firms can split sale/lease across two separate plugin tokens**
  (Franklin Street does). `srcBuildout`'s sale-boolean filter alone is not enough —
  select the plugin token *by transaction* or you silently drop a whole tenure.
  Always probe both `/properties/for-sale/` and `/properties/for-lease/` for
  distinct tokens.
- **Do not run a full collection (thousands of fetches) at concurrency ≥5 against
  the local stack — it OOM-crashed OrbStack.** Full production runs stay at
  concurrency ≤3 (the proven daily pattern). During firm-by-firm dev, validate
  with small probes (`--max-items=6`) + direct API counts; reserve the full run
  for the batched production landing.
- Per-firm verification recipe that meets the quality bar without a full run:
  (1) confirm the enumeration source returns the full `total`; (2) `--max-items=6`
  both tenures for field quality; (3) `cre_ingest.py --dry-run` to confirm mapping.

### Crexi platform finding (2026-06-20) — "easy leverage" premise DISPROVEN

Probed Crexi as a reusable platform adapter (the Tier-C plan). It is **triple-walled
against a scripted GET collector**:
- `www.crexi.com/` and `sitemap.xml` return **403** to non-browser clients
  (Cloudflare bot management) — can't even read the SPA bundle or enumerate.
- The search API `POST https://api.crexi.com/assets` is **POST + bearer auth**
  (`401 Unauthorized`); `api.crexi.com/health` is 200 so the API is up, but no
  trivial anonymous-token endpoint was found.
This is the **same POST/auth wall** that makes Colliers and Transwestern
`UNSUPPORTED`, and the collector fetches GET-only through Firecrawl. A clean Crexi
adapter would require a NEW authenticated direct-POST client + Cloudflare-stealth +
anonymous-token acquisition/rotation — fragile, and **ToS-gray (auth-gated API)**.
**Recommendation: do not treat Crexi as a platform unlock.** Onboard Crexi-cluster
firms via their OWN sites where one exists; mark the rest Crexi-locked.

Net-lease cluster reality (own-site probe):
- **Lyon Stahl** — ✅ own property sitemap (`/properties-sitemapN.xml`, ~2,000
  `/properties/{slug}/` URLs); detail pages are server-rendered (JSON-LD, og:title
  address, `$` price tokens, units/sqft), **zero Crexi refs**. Onboardable via the
  Matthews sitemap pattern. (Smaller firm, ~$3B; LA multifamily.)
- **Greysteel** — ✗ WordPress sitemap has only the `/properties/` landing page;
  actual listings are Crexi-embedded. Crexi-locked.
- **The Boulder Group** — ✗ homepage embeds a Crexi widget; no own catalog / sitemap
  404. Crexi-locked.

Implication: the net-lease cluster is **not** a single-adapter win. It's per-firm
own-site work (where a catalog exists) plus a set of Crexi-locked firms that need an
architecture decision (build the authenticated Crexi client, or leave them out).

---

### Firecrawl stealth-render on the "hard/can't" firms (2026-06-20)

Tested local Firecrawl `proxy:"stealth"` + `waitFor` against the firms previously
classed Akamai-blocked / unscrapable. **Result: the anti-bot is NOT the barrier —
all returned HTTP 200 when rendered through Firecrawl.** The real barrier is
ENUMERATION: these firms serve listings from paginated internal / Salesforce-backed
JS APIs, not sitemaps. So each is now *reachable* but needs its specific API
reverse-engineered + paginated (firm-by-firm, but newly possible).

| Firm | Rendered | Listings reachable? | Path / blocker |
|------|:--------:|---------------------|----------------|
| **SRS Real Estate Partners** | 200, Next.js | **2,122 — ✅ ONBOARDED** (`srcSrs`) | **DONE.** Cracked the API: `POST https://srsre-next-412955565034.us-central1.run.app/api/property-search` body `{query:{offset:12*page,pageSize:12,...UI_FILTERS},client_ip:""}` → `{total, properties:[{apto_data:<Salesforce SRS_Listings__c>, location, square_feet_data, permalink}]}`. Open Cloud Run backend (NOT Cloudflare-gated) — collector calls it DIRECTLY (global fetch, not Firecrawl), paginated. Verified: 10/10 address/type/photos/geo, prices hidden on most (`Contact Us`). |
| **Hanley Investment** | 200 | **102 — ✅ ONBOARDED** (`srcHanley`) | **DONE.** Even simpler than SRS: the `/listings/` page is directly fetchable (Cloudflare monitor-mode) and server-embeds the WHOLE catalog in `var rethink_properties = [...]` (Rethink/Salesforce platform). Direct fetch + JSON parse, no Firecrawl/API. All sale (Seller_Rep). Verified 6/6 address/type/SF/photos/geo. |
| **Stream Realty** | 200 | **NO catalog** (investigated 2026-06-20) | ✗ Not viable. WordPress with NO property post type; `/properties/{market}/` are market-overview pages (stats/team/services), not listings. Only AJAX actions are `load_post` (generic, empty) + `load_member` (team carousel). Sitemap index (19 sub-sitemaps) has ZERO individual property URLs; `/availabilities//find-space//lease//listings/` all 404. "Find space" UX surfaces availabilities via broker contact, not a public feed. **No listings to enumerate.** |
| **Kidder Mathews** | 200, jQuery app | **3,108 — ✅ ONBOARDED** (`srcKidder`) | **DONE.** Listings from an open backend, reverse-engineered from `app.min.js`: `POST https://services.kidder.com/search/public/listing` body `{startIndex, numResults, includeAggregations:false}` (the `SearchRequest` shape — `numResults` is the page size) → `{totalResultCount, results:[listing_key, property_*, list_price, asking_rent_max, sf_avail, use_type, brokers(by NAME), lat/lon, photos]}`. Open, no auth. Direct paginated fetch. Verified 16/16 address/type/photos/geo/brokers. |
| **Voit** | 403 on listings | **ToS dead-end (2026-06-20)** | ✗ Not viable, and not for a technical reason. Voit's catalog is on `looplink.voitco.com` = **CoStar/LoopNet "LoopLink"** (403 to bots; LoopNet ToS explicitly bars scraping/data extraction). Unlike SRS/Hanley/Kidder (the firms' OWN open backends), this is LoopNet's platform. **Do NOT bypass** — the legitimate path is a licensed CoStar/LoopNet feed. Same applies to any LoopLink-hosted firm. |
| **Northmarq / Berkadia / Walker & Dunlop** | 200 | card-level ONLY | listings are **RCM deal-room links** (`my.rcm1.com`), qualified-investor gated — real data inaccessible |
| **Eastdil / Cresa / Hughes Marino** | n/a | none | no public catalog (advisory / tenant-rep) — confirmed unscrapable |

**Takeaway:** Firecrawl converts most "can't" firms to "reachable, needs API
enumeration." SRS (2,122, major retail) is the best concrete next target — a new
source type: stealth render → reverse-engineer the Salesforce search API → paginate.
The genuinely impossible remain the no-catalog advisory/tenant-rep firms and the
RCM-gated capital-markets deal rooms.

## 1. Collector vs. display: why the bulk problem is harder

| | Display repo | **Collector (this repo)** |
|---|---|---|
| Unit of work | one listing URL | a firm's entire catalog |
| Minimum requirement | a fetchable detail page | an **enumerable index**: sitemap, search/listing API, or inventory API |
| "Matthews works" means | its `/properties/{slug}` page parses | its `sitemap.xml` lists all 3,564 properties **and** each parses |
| Reuse lever | shared parse fn (`parseBuildout`) | shared **fetch/enumeration** adapter (`srcBuildout`, a future `srcCrexi`) |

The display survey (2026-06-19) concluded "every new firm needs Firecrawl /
bespoke-API / Crexi / Akamai-bypass." That is true for *detail-page rendering*.
For the **collector**, the relevant question is different and partly more
favorable: **does the firm expose a bulk feed we can page through?** Buildout and
Crexi firms do (via their platform APIs) even when their detail pages are
client-rendered.

---

## 2. True top-30 by volume + coverage map

Synthesized 2026-06-20 from CPE 2026 "Top CRE Brokerage Firms" (FY2025, pub.
2026-05-28), MSCI RCA / Green Street office league tables (FY2024), Multi-Housing
News 2026, Walker & Dunlop FY2025 SEC filing, Lipsey 2025/26 (brand tiebreaker
only), and the SharpLaunch aggregator. **Caveat:** no single public source gives a
clean volume-ordered top-30; CPE is self-reported and blends growth/market share,
RCA is subscriber-gated, and several majors (JLL, Savills, Avison Young) did not
submit to CPE. Treat ranks as indicative. Sources listed in the Appendix.

Legend: ✅ covered · ➕ gap-scrapable · ⛔ gap-no-catalog

| # | Firm | Domain | Volume signal | Status |
|--:|------|--------|---------------|--------|
| 1 | CBRE | cbre.com | $191.2B sales + $213.1B leasing FY25 | ✅ |
| 2 | Cushman & Wakefield | cushmanwakefield.com | $59.8B sales + $88.3B leasing; CPE #1 | ✅ |
| 3 | JLL | jll.com | RCA office #3 ($5.57B); did not submit to CPE | ✅ |
| 4 | Colliers | colliers.com | $69.5B sales + $51.9B leasing; CPE #3 | ✅ (seeded, `UNSUPPORTED` POST-only) |
| 5 | Newmark | nmrk.com | $70.4B sales + $48.5B leasing; CPE #4 | ✅ |
| 6 | Eastdil Secured | eastdilsecured.com | #1 office sales $8.08B (28% share) | ⛔ advisory/capital-markets |
| 7 | Marcus & Millichap | marcusmillichap.com | #1 multifamily $26.7B; ~$43.6B total | ✅ |
| 8 | Walker & Dunlop | walkerdunlop.com | $54.8B total FY25 (SEC) | ➕ partial (mostly capital-markets) |
| 9 | Avison Young | avisonyoung.com | ~$15.7B sales | ✅ |
| 10 | Berkadia | berkadia.com | major multifamily IS + lending | ➕ (qualified-investor gating likely) |
| 11 | Northmarq | northmarq.com | ~$37B; MHN #2 multifamily | ➕ |
| 12 | Lee & Associates | lee-associates.com | largest broker-owned; Lipsey #5 | ✅ (Buildout; blocked on full run) |
| 13 | Savills (NA) | savills.us | occupier/tenant-rep; Lipsey #11 | ✅ |
| 14 | NAI Global | naiglobal.com | $3B sales + $17.5B leasing; CPE #5 | ✅ |
| 15 | Transwestern | transwestern.com | ~$3.76B sales; Lipsey #8 | ✅ (seeded, `UNSUPPORTED` POST-only) |
| 16 | Kidder Mathews | kidder.com | >$10B/yr; largest indep. West Coast | ➕ |
| 17 | Stream Realty | streamrealty.com | CPE top-20; industrial-heavy | ➕ |
| 18 | SVN International | svn.com | ~$21.1B; franchise | ✅ (Buildout) |
| 19 | TCN Worldwide | tcnworldwide.com | CPE top-10 by leasing; network | ➕ (Akamai + multi-host) |
| 20 | Matthews REIS | matthews.com | net-lease/multifamily IS specialist | ➕ **→ PILOT (done, §6)** |
| 21 | Greysteel | greysteel.com | ~$10B private-capital IS | ➕ (Crexi) |
| 22 | Voit | voitco.com | CPE top-20; SoCal industrial | ➕ |
| 23 | Coldwell Banker Commercial | cbcworldwide.com | 40-country franchise | ➕ (Buildout, client-rendered) |
| 24 | Lyon Stahl | lyonstahl.com | ~$3.15B LA multifamily | ➕ |
| 25 | Cresa | cresa.com | largest occupier-only advisory | ⛔ tenant-rep only |
| 26 | CORFAC International | corfac.com | independent-firm network | ➕ (network, probe) |
| 27 | Hughes Marino | hughesmarino.com | West-Coast tenant-rep | ⛔ tenant-rep only |
| 28 | Institutional Property Advisors | ipausa.com | M&M division | ✅ (folded into Marcus & Millichap; do not double-count) |
| 29 | Agency-lending peers (Greystone, etc.) | — | debt-side, appear in volume tables | ⛔ no listing catalog |
| 30 | Knight Frank (US) | knightfrank.com | US presence via Cresa alliance | ⛔ non-US catalog |

**Covered: 12** (incl. 2 seeded-but-blocked). **Scrapable gaps (➕): ~11–13.**
**No-catalog (⛔): ~6.**

Additional scrapable net-lease firms named in the display design (not all in the
strict top-30 but high-inventory, good backfill to honestly reach 30): **SRS Real
Estate Partners** (Akamai), **Hanley Investment Group** (Akamai), **Sands
Investment Group / SIG** (custom JS), **The Boulder Group** (Crexi), **Franklin
Street** (Buildout client-rendered), **Foundry Commercial**, **Lincoln Property
Company**.

---

## 3. The honest "30" problem

A literal "top-30-by-volume" target is self-defeating for a *listing* collector:
the firms that rank highest on investment-sales dollars (Eastdil) or that are
pure occupier advisors (Cresa, Hughes Marino) **do not publish a browsable
catalog** — their deals are gated to qualified investors or are tenant-rep
engagements with no inventory to list.

**Recommended target definition:** "the scrapable firms within the top ~30 by
volume, plus the next-most-significant scrapable firms, until the collector has
30 working sources." Mark the ⛔ firms as *intentionally out of scope, no public
catalog* in `BROKERAGE_STATUS` so the "30" count stays honest (this mirrors how
the display design dropped IPA as a double-count).

---

## 4. Collector enumeration feasibility — by method tier

Ordered easiest→hardest for the *collector* specifically.

- **Tier A — public sitemap / server-rendered, curl-able (cheapest).**
  Enumerate from `sitemap.xml` (or a server-rendered index), fetch detail pages,
  DOM-parse. **Matthews is the proven example (§6).** The display survey suggests
  Matthews may be the *only* new firm in this tier, but sitemaps are cheap to
  check and were **not** part of that survey — probe `/sitemap.xml` for every gap
  firm first; it's the single fastest win when present.

- **Tier B — public JSON API behind an SPA (cleanest when it exists).**
  Many SPA firms (Kidder Mathews, Stream Realty, Northmarq, Berkadia, Walker &
  Dunlop, SIG, Voit) fetch listings from a JSON endpoint visible in DevTools →
  Network. If it's a GET with no auth, it's the best bulk path (this is how
  `srcCbre` and `srcNewmark` already work). **Unprobed — highest-value research
  next step.** Watch for POST-only endpoints (Colliers/Transwestern are already
  blocked for exactly this reason).

- **Tier C — Crexi platform adapter (highest leverage, one build → many firms).**
  Net-lease/private-capital firms cluster on Crexi (Greysteel, The Boulder Group,
  likely Lyon Stahl/Hanley and many regionals). A single `srcCrexi` enumeration
  adapter onboards all of them. Crexi has a structured backend; detail pages are
  JS-rendered and it has anti-bot, so route through Firecrawl stealth. Build once,
  reuse widely — the collector analogue of the display app's `parseBuildout`.

- **Tier D — Buildout inventory API by token (reuse what we already have).**
  `srcBuildout(company, token, baseUrl, tx, max)` already powers SVN and Lee. It
  hits Buildout's **inventory API**, which works **regardless of whether the
  firm's site renders Buildout server-side or client-side.** So client-rendered
  Buildout firms the display survey rejected (**Franklin Street, Coldwell Banker
  Commercial**) are onboardable here with *zero new code* — just extract their
  Buildout plugin token (40-hex, in page source / `plugins/{token}`) and add a
  `runSource` case. Caveat: Buildout throttles sustained paging (see the Lee
  block); keep the abort threshold and consider a resumable/slower mode.

- **Tier E — Akamai-blocked (SRS, Hanley, TCN).**
  curl 403s on Akamai TLS fingerprinting. The collector already defeats Cloudflare
  for CBRE via Firecrawl `proxy:"stealth"`. Probe whether stealth also clears
  Akamai; if yes these become Tier A/B/C. TCN additionally aggregates across
  member sites (multi-host).

- **Tier F — no public catalog (skip, document why).**
  Eastdil, Cresa, Hughes Marino, Knight Frank-US, agency lenders, and IPA
  (double-count of M&M).

---

## 5. Per-firm gap matrix

"Method" = recommended collector enumeration path. "Confidence" reflects whether
it's verified (Matthews), inferred from the display detail-page survey, or
needs a live bulk-feed probe.

| Firm | Recommended collector method | Tier | Effort | Confidence | Notes |
|------|------------------------------|:----:|:------:|------------|-------|
| **Matthews** | sitemap.xml → detail pages | A | done | **verified** | Pilot, §6 |
| Franklin Street | Buildout inventory API (token) | D | low | inferred (display: client-rendered Buildout) | reuse `srcBuildout`; get token |
| Coldwell Banker Commercial | Buildout inventory API (token) | D | low | inferred ("Powered by Buildout") | reuse `srcBuildout`; get token |
| Greysteel | Crexi adapter | C | med (1st Crexi) | inferred (display: Crexi) | builds the reusable `srcCrexi` |
| The Boulder Group | Crexi adapter | C | low (after Crexi) | inferred (display: Crexi) | net-lease |
| Kidder Mathews | JSON API probe → else Firecrawl render | B | med | needs probe | display: JS SPA, no SSR links |
| Stream Realty | JSON API probe | B | med | needs probe | "find space" UX; confirm stable URLs |
| Northmarq | JSON API probe → Firecrawl render | B | med | needs probe | display: custom JS SPA |
| Berkadia | JSON API probe | B | med | needs probe | investor gating likely |
| Walker & Dunlop | JSON API probe | B | med-high | needs probe | mostly capital-markets; thin catalog |
| Sands Investment Group | JSON API probe → Crexi | B/C | med | needs probe | net-lease; check Crexi presence |
| Voit | sitemap → JSON API probe | A/B | med | needs probe | SoCal industrial |
| Lyon Stahl | Crexi adapter | C | low (after Crexi) | needs probe | LA multifamily |
| CORFAC | per-member probe | E/B | high | needs probe | network, multi-host |
| SRS Real Estate Partners | Akamai bypass via stealth → then ? | E | high | inferred (display: 403 Akamai) | major retail |
| Hanley Investment Group | Akamai bypass → Crexi | E/C | high | inferred (display: 403 Akamai) | retail net-lease |
| TCN Worldwide | Akamai bypass + multi-host | E | high | inferred (display: 403 Akamai) | member network |
| Eastdil / Cresa / Hughes Marino / Knight Frank-US / agency lenders / IPA | — | F | skip | verified-by-nature | no public catalog / double-count |

---

## 6. Pilot: Matthews — implemented & verified (this session)

**What was built (in this repo):**
- `collect.ts`: `srcMatthews(tx, max)` + helpers (`matthewsImages`,
  `matthewsBrokers`, `parseMatthewsAddress`, `parseMatthewsDetail`). Detail-page
  DOM hooks ported from the display repo's `lib/live/parsers/matthews.ts`.
- `collect.ts`: registered `"matthews"` in `SOURCE_KEYS` and `runSource`.
- `cre_ingest.py`: added `"matthews": ("matthews", "")` to `SOURCE_TO_BROKERAGE`.
- `sql/001_cre_brokerages.sql`: added the `matthews` brokerage seed row
  (`pagination_strategy: "sitemap_enumeration"`).

**Enumeration design:** fetch `https://www.matthews.com/sitemap.xml` (a flat
8,108-URL urlset), regex out the `/properties/{slug}` detail URLs (3,564), and
partition by slug prefix — `leasing-*` → lease, everything else → sale (mirrors
the Buildout client-side sale/lease partition). No token, no JS render.

**Verified end-to-end (2026-06-20, local stack up):**
```bash
# typecheck
npm run typecheck                       # exit 0

# live probe (sale + lease, 6 each) through local Firecrawl
FIRECRAWL_API_URL=http://localhost:3002 FIRECRAWL_API_KEY=local-self-hosted \
  npx tsx collect.ts --source=matthews --transaction=both --max-items=6 \
  --concurrency=3 --out=/tmp/matthews_probe.json
#   matthews/sale: 6 listings (source total: 2913)
#   matthews/lease: 6 listings (source total: 651)
#   done: 12 listings, 23 unique brokers

# dry-run ingest (no DB connection)
python3 cre_ingest.py --in /tmp/matthews_probe.json --dry-run \
  --keep-artifacts /tmp/matthews_ingest
#   staged listings: 12 (matthews: 12) → /tmp/matthews_ingest/ingest.sql
```

**Field quality across the 12 probe rows:** name/street/city/state/zip/photos/
assetType **12/12**; sale price **6/6 sale**; lease rate **6/6 lease**; cap rate &
building SF populate on net-lease deals; Offering Memorandum PDF captured;
brokers carry email + phone + avatar (11/12 — one self-storage card used a
non-standard broker block). Highlights are joined into `description` (Matthews has
no narrative description section). Full original payload preserved in `raw_data`.

**NOT done (needs explicit go-ahead — writes to production EQUIRE Supabase):**
1. Apply the `matthews` brokerage seed to `fhqycqubkkrdgzswccwd`
   (`sql/001_cre_brokerages.sql` is idempotent; re-run it or apply just the new row).
2. Full collection: `--source=matthews --transaction=both --max-items=0`
   (~3,564 detail fetches; estimate runtime before scheduling).
3. Live additive ingest: `python3 cre_ingest.py --in <full>.json`
   (NOT `--mark-missing`; Lee is still blocked, keep ingest additive).

---

## 7. Recommended sequencing

1. **Land Matthews** (apply seed → full run → live ingest). Proves the collector
   onboarding path against a real firm and adds a clean net-lease feed.
2. **Tier D — Buildout reuse (Franklin Street, Coldwell Banker Commercial).**
   Near-zero code: extract token, add `runSource` cases, seed rows. Validate the
   Buildout throttle behavior with the existing abort threshold.
3. **Tier B probe sweep.** Live-probe JSON endpoints for Kidder Mathews, Stream
   Realty, Northmarq, Berkadia, Walker & Dunlop, SIG, Voit. Implement the clean
   GET-API ones (`srcCbre`/`srcNewmark` pattern). This is the biggest
   information-gain step and should be done as a batch.
4. **Tier C — build `srcCrexi`** once, then onboard Greysteel, The Boulder Group,
   Lyon Stahl, and any Tier-B firms that turned out to be Crexi-backed.
5. **Tier E — Akamai probe** (SRS, Hanley, TCN) via Firecrawl stealth; promote to
   whatever tier they resolve to.
6. **Document ⛔ firms** as out-of-scope-no-catalog to keep the "30" honest.

Stop-and-decide gates: any firm whose only path is a **POST** endpoint behind a
consent wall (cf. Colliers/Transwestern) stays `UNSUPPORTED` until a safe GET or
an authorized integration exists. Do not weaken that line.

---

## 8. Per-firm collector onboarding runbook

Extends the "Adding a source" steps in `CLAUDE.md`:

1. **Probe enumeration first (not the detail page).** In order: `/sitemap.xml`
   (Tier A) → DevTools Network for a JSON listing/search GET (Tier B) → is it
   Buildout? grab the `plugins/{40-hex}` token (Tier D) → is it Crexi? (Tier C) →
   else Firecrawl-rendered index or Akamai bypass (Tier E). Record findings; if
   only a POST endpoint exists, mark `UNSUPPORTED` and stop.
2. **Implement the adapter.** Reuse `srcBuildout` (token) or the future `srcCrexi`
   where possible; otherwise write `srcNewFirm(tx, max): Promise<SourceResult>`
   returning the shared listing vocabulary (see the comment above `SourceResult`
   in `collect.ts`). Partition sale/lease the way the source allows (server filter,
   slug prefix, or `sale` boolean).
3. **Register** in `SOURCE_KEYS` + `runSource`.
4. **Map** the key in `cre_ingest.py` `SOURCE_TO_BROKERAGE` and add a seed row in
   `sql/001_cre_brokerages.sql` (apply it to Supabase).
5. **Probe + verify:** `--source=<key> --transaction=both --max-items=6`, then
   `cre_ingest.py --dry-run --keep-artifacts` and eyeball the staged row.
6. **Update** `BROKERAGE_STATUS` and this matrix; full run + additive ingest.

---

## 9. Open decisions / risks

- **Production writes.** Steps that touch `fhqycqubkkrdgzswccwd` (seed apply, live
  ingest) need a human go-ahead and the EQUIRE `.env.local` Postgres URL. Keep
  `--mark-missing` OFF while Lee is blocked, and never on subset runs.
- **Runtime/scale.** The 2026-06-12 all-source run was ~27 min for ~35k rows.
  Matthews adds ~3,564 detail fetches; a full Crexi/Buildout fan-out across many
  new firms will grow wall-time materially — budget it and keep concurrency ≤ 6.
- **Buildout throttling** (Tier D) is the known failure mode (Lee). New Buildout
  firms inherit it; the abort-on-3%-failed-pages guard must stay.
- **Anti-bot drift.** Crexi/Akamai paths depend on Firecrawl stealth continuing to
  work; treat them as monitored, fail-soft sources.
- **Ranking is approximate.** §2 is a best-effort synthesis from gated sources;
  re-pull CPE/RCA before treating any specific rank as authoritative.

---

## Appendix — ranking sources

- CPE 2026 "Top CRE Brokerage Firms" (FY2025, 2026-05-28):
  https://www.commercialsearch.com/news/top-commercial-real-estate-brokerage-firms/
- CPE 2024 (FY2023, 2024-05-29):
  https://www.commercialsearch.com/news/2024-top-cre-brokerage-firms/
- Real Estate Alert / Green Street office league table (FY2024, 2025-01-28):
  https://www.eastdilsecured.com/2025/01/28/real-estate-alert-office-sales-claw-back-eastdil-wins-crown/
- MSCI Real Capital Analytics (subscriber): https://app.rcanalytics.com/
- Multi-Housing News 2026 Top Multifamily Brokerage Firms:
  https://www.multihousingnews.com/top-multifamily-brokerage-firms/
- Lipsey 2025 brand survey (PDF):
  https://lipseyco.com/wp-content/uploads/2025/02/2025-Top-25-Results-021325-v1.pdf
- Walker & Dunlop FY2025 results (SEC, Feb 2026):
  https://www.walkerdunlop.com/news/walker-dunlop-reports-fourth-quarter-2025-financial-results
- SharpLaunch Top 20 CRE Brokerages (FY2023 aggregator):
  https://www.sharplaunch.com/blog/top-commercial-real-estate-brokerages
- Display-side detail-page probe survey (platform classifications):
  `dynamically-display-cre-listing-data/docs/probe-results/top-30-candidate-platform-survey-2026-06-19.md`
