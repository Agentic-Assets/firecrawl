-- =============================================================================
-- 001_cre_brokerages.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Registry of commercial real estate brokerages that EQUIRE's ListingHunterAgent
-- scrapes for listings. Each row carries the Firecrawl scrape configuration
-- (proxy mode, render wait, pagination strategy) verified against the live site.
--
-- Naming convention: snake_case ops/EQUIRE layer, cre_ prefix, uuid PK, tstz.
-- The cre_ prefix is safe: only cre_business_plan_runs predates this schema.
-- =============================================================================

CREATE TABLE IF NOT EXISTS credeals.cre_brokerages (
    id            uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    name          text        NOT NULL,
    slug          text        UNIQUE NOT NULL,
    base_url      text,
    search_url    text,
    description   text,
    -- scrape_config keys: proxy, wait_for_ms, timeout_ms, pagination_strategy, notes
    scrape_config jsonb       DEFAULT '{}'::jsonb,
    active        boolean     DEFAULT true,
    created_at    timestamptz DEFAULT now(),
    updated_at    timestamptz DEFAULT now()
);

COMMENT ON TABLE  credeals.cre_brokerages              IS 'CRE brokerage registry with per-site Firecrawl scrape configuration for EQUIRE ListingHunterAgent.';
COMMENT ON COLUMN credeals.cre_brokerages.slug         IS 'Stable lowercase identifier used by the Python scraper config (cre_scrapers/config.py BROKERS dict).';
COMMENT ON COLUMN credeals.cre_brokerages.scrape_config IS 'jsonb: {proxy, wait_for_ms, timeout_ms, pagination_strategy, notes}. Verified against live site behavior.';
COMMENT ON COLUMN credeals.cre_brokerages.active       IS 'False disables the broker from scheduled scrape runs (e.g. access-gated or consistently failing sites).';

-- -----------------------------------------------------------------------------
-- Seed data: 10 national CRE brokerages.
-- proxy / wait_for_ms values come from live Firecrawl testing (2026-06-11).
-- CBRE is the reference implementation (proxy=stealth, wait_for=6000).
-- -----------------------------------------------------------------------------

INSERT INTO credeals.cre_brokerages (name, slug, base_url, search_url, description, scrape_config, active) VALUES

-- 1. CBRE -- reference implementation. Cloudflare Managed Challenge; stealth required.
('CBRE', 'cbre',
 'https://www.cbre.com',
 'https://www.cbre.com/properties/properties-for-sale/commercial-space',
 'World''s largest CRE brokerage. React SPA behind Cloudflare Managed Challenge. Reference scraper: scripts/firecrawl-ops/cbre_scrape.py. Property IDs format US-{TYPE}-{NUMBER}; address slug is SEO-only and ignored by routing.',
 '{"proxy": "stealth", "wait_for_ms": 6000, "timeout_ms": 60000, "pagination_strategy": "search_filter_combinations", "listing_url_pattern": "/properties/properties-for-{sale|lease}/commercial-space/details/{id}/{slug}", "external_id_pattern": "US-[A-Z]+-[0-9]+", "notes": "Reference impl. waitFor>=6000 required for SPA hydration after CF challenge. PDFs also behind CF; scrape detail page first to get URLs."}'::jsonb,
 true),

-- 2. JLL -- own Next.js front-end at property.jll.com. No Cloudflare. Browse pages thin; scrape category/search pages.
('JLL', 'jll',
 'https://property.jll.com',
 'https://property.jll.com/sale-office',
 'Jones Lang LaSalle. Own Next.js property site (property.jll.com), no Cloudflare. Homepage renders nav only; listing cards load client-side. Scrape category/search pages (/sale-office, /rent-industrial) for cards. Individual listing pages scrape cleanly.',
 '{"proxy": "stealth", "wait_for_ms": 5000, "timeout_ms": 60000, "pagination_strategy": "search_query_params", "listing_url_pattern": "/listings/{slug-address-market}", "search_url_pattern": "/search?tenureType={sale|rent}&propertyType={office|industrial|retail|land}", "notes": "Homepage thin; must scrape category/search pages. ~20 cards per results page, client-side paginated. Cursor/page param TBD."}'::jsonb,
 true),

-- 3. Cushman & Wakefield -- Coveo faceted search. Basic geo-redirect, no challenge. Use US URL directly.
('Cushman & Wakefield', 'cushman-wakefield',
 'https://www.cushmanwakefield.com',
 'https://www.cushmanwakefield.com/en/united-states/properties/invest/search',
 'Cushman & Wakefield. Coveo-based faceted search. Global URL geo-redirects to US (/en/united-states/...); no CF challenge. The invest/search page renders full listing cards including sale prices directly in HTML. Rich data (134K MD on search page).',
 '{"proxy": "stealth", "wait_for_ms": 6000, "timeout_ms": 60000, "pagination_strategy": "coveo_fragment_facets", "listing_url_pattern": "/en/united-states/properties/for-sale/{type}/{state}/{city}/{slug}/{slug}-s", "search_url_pattern": "/en/united-states/properties/invest/search?type={type}#f:TransactionType=[Sale]", "pagination_param": "#first=N", "notes": "Use US URL directly to skip geo-redirect. Trailing -s suffix on detail URLs is consistent. Coveo #first=N offset for paging."}'::jsonb,
 true),

-- 4. Colliers -- public site is POST-only for the current collector. Keep seeded for manual probes and future adapter work.
('Colliers', 'colliers',
 'https://www.colliers.com',
 'https://www.colliers.com/en/properties',
 'Colliers International. Legacy probes found stable usa{7-digit} listing ids in rendered pages, but the current production collector does not include Colliers because the usable inventory path is POST-only and no public GET endpoint has been verified. Seed remains active for targeted discovery and future adapter work.',
 '{"proxy": "stealth", "wait_for_ms": 5000, "timeout_ms": 60000, "pagination_strategy": "post_only_discovery_pending", "listing_url_pattern": "/en/properties/{name-slug}/{address-slug}/usa{7-digit-id}", "external_id_pattern": "usa[0-9]{7}", "search_url_pattern": "/en/properties#f:listingtype=[For%20Sale]&f:recenttransactions=[0]", "pagination_param": "#first=N", "notes": "Not collected by cre_collector as of 2026-06-11. Investigate the POST search request or sales.colliers.com before enabling production collection."}'::jsonb,
 true),

-- 5. Marcus & Millichap -- works via cre_collector (stealth + 120s timeout + 3x retry). Sale-only platform. Flaky but usable.
('Marcus & Millichap', 'marcus-millichap',
 'https://www.marcusmillichap.com',
 'https://www.marcusmillichap.com/properties',
 'Marcus & Millichap. Investment-sales platform (no public lease inventory). Verified 2026-06-11 via cre_collector/collect.ts: stealth proxy + timeout 120000 succeeds, but intermittent SCRAPE_TIMEOUTs occur and are absorbed by the collector''s 3-attempt retry. Stable numeric listing ids and canonical /properties/{id}/{slug} URLs.',
 '{"proxy": "stealth", "wait_for_ms": 10000, "timeout_ms": 120000, "pagination_strategy": "rendered_grid", "listing_url_pattern": "/properties/{listing-id}/{slug}", "notes": "Collected by cre_collector (source key marcus-millichap, sale-only). Flaky under stealth: expect 1-2 timeouts absorbed by retries per run."}'::jsonb,
 true),

-- 6. Avison Young -- Liferay SPA, hash routing. Listings hydrate client-side; not in initial HTML. Hard.
('Avison Young', 'avison-young',
 'https://www.avisonyoung.us',
 'https://www.avisonyoung.us/property-search',
 'Avison Young. Liferay CMS with hash-based SPA routing. No Cloudflare. Both landing and /property-search render nav/filters but NOT listing cards (hydrated client-side after hash filter resolves). Likely needs longer waitFor + scroll, or the underlying REST API endpoint.',
 '{"proxy": "stealth", "wait_for_ms": 9000, "timeout_ms": 90000, "pagination_strategy": "hash_spa_or_internal_api", "listing_url_pattern": "/web/{market}/properties/{listing-id}", "search_url_pattern": "/property-search#/?type={type}&view=sidebar&status=active&transaction=sale", "notes": "HARD: hash SPA, listings not in initial HTML. waitFor 8000-10000 to let hash filter hydrate. May require network interception of internal listing API."}'::jsonb,
 true),

-- 7. NAI Global -- CookieYes GDPR consent wall blocks listings. Need an actions click on Accept All.
('NAI Global', 'nai-global',
 'https://www.naiglobal.com',
 'https://www.naiglobal.com/north-american-listings/',
 'NAI Global (franchise network). No Cloudflare; blocker is a full-page CookieYes GDPR consent overlay. Listings render only after accepting. Listings may also distribute across nai{franchise}.com partner sites. Use Firecrawl actions to click Accept All, or set cookieyes-consent=accepted.',
 '{"proxy": "stealth", "wait_for_ms": 5000, "timeout_ms": 60000, "pagination_strategy": "unknown_until_consent_bypassed", "consent_wall": "cookieyes", "actions": [{"type": "click", "selector": "[data-cky-tag=accept-button], .cky-btn-accept"}], "cookie_header": "cookieyes-consent=yes", "notes": "MEDIUM: barrier is consent wall not CF. Add actions click on CookieYes accept button before content renders. Franchise listings may live on partner subdomains."}'::jsonb,
 true),

-- 8. Newmark -- works via public Algolia search API (credentials embedded in the /properties page). Sale + lease.
('Newmark', 'newmark',
 'https://www.nmrk.com',
 'https://www.nmrk.com/properties?saleOrLease=sale',
 'Newmark. The /properties page embeds public Algolia search credentials; the Algolia index serves full listing data without authentication (verified 2026-06-11 via cre_collector/collect.ts: 1,390 sale + 3,757 lease). Algolia''s ~1,000-hit retrieval cap is bypassed by splitting queries per state facet. The nim.nmrk.com portal gate only applies to the rendered site, not the search index.',
 '{"proxy": "auto", "wait_for_ms": 3000, "timeout_ms": 60000, "pagination_strategy": "algolia_state_facets", "facet_filter": "saleOrLease:Sale|Lease", "notes": "Collected by cre_collector (source key newmark). Algolia appId/searchKey/index are scraped from the page on each run; per-state facet split keeps every query under the 1k retrieval cap."}'::jsonb,
 true),

-- 9. SVN -- WordPress. No CF. Browse page is nav-only; listings behind a search form. Query-param discovery.
('SVN', 'svn',
 'https://svn.com',
 'https://svn.com/properties/?propertyTypes=3',
 'SVN (Sperry Van Ness). WordPress site, no Cloudflare. /properties/ renders category nav only; actual listings live behind a search form. Discovery via query params: ?propertyTypes={id}&searchText=&salePriceMin=&salePriceMax=. Smaller inventory than CBRE/JLL/Colliers.',
 '{"proxy": "stealth", "wait_for_ms": 5000, "timeout_ms": 60000, "pagination_strategy": "wordpress_query_params", "search_url_pattern": "/properties/?propertyTypes={type_id}&searchText={text}&salePriceMin=&salePriceMax=", "property_type_ids": {"industrial": 3, "multifamily": null, "office": null, "retail": null, "land": null}, "pagination_param": "&page=N", "notes": "MEDIUM: no cards on browse page; trigger search query by propertyTypes id. Standard WordPress paginated archive once URL pattern confirmed."}'::jsonb,
 true),

-- 10. Lee & Associates -- Buildout source currently blocked by HTML interstitials during full collection.
('Lee & Associates', 'lee-associates',
 'https://www.lee-associates.com',
 'https://www.lee-associates.com/properties/',
 'Lee & Associates. Office/industrial-focused national CRE brokerage. The current collector uses the Buildout inventory API discovered from the Lee property pages. Latest full run on 2026-06-11 aborted Lee at 12 of 333 pages because Buildout returned HTML interstitial pages instead of JSON; keep existing Lee rows untouched until a clean run succeeds.',
 '{"proxy": "stealth", "wait_for_ms": 6000, "timeout_ms": 60000, "pagination_strategy": "buildout_inventory_api", "listing_url_pattern": "/properties/{listing-id}", "notes": "Collected by cre_collector source key lee-associates, but blocked as of 2026-06-11 by repeated Buildout HTML interstitial responses. Use --no-mark-missing while this source is failing."}'::jsonb,
 true),

-- 11. Savills -- added 2026-06-11 for the multi-source collector (cre_collector/collect.ts).
('Savills', 'savills',
 'https://www.savills.us',
 'https://search.savills.com/us/en/list/property-for-sale/united-states-of-america',
 'Savills North America. Server-rendered paginated search at search.savills.com (/page/N). Sale base: property-for-sale/united-states-of-america; lease base: property-to-rent/united-states-of-america. Collected by cre_collector/collect.ts (source key savills); smaller US inventory (~100 sale listings).',
 '{"proxy": "auto", "wait_for_ms": 5000, "timeout_ms": 60000, "pagination_strategy": "path_page_n", "search_url_pattern": "https://search.savills.com/us/en/list/property-{for-sale|to-rent}/united-states-of-america/page/{n}", "notes": "Added for cre_collector multi-source run. HTML cards server-rendered; no Cloudflare observed."}'::jsonb,
 true),

-- 12. Matthews REIS -- added 2026-06-20 for the multi-source collector (cre_collector/collect.ts).
('Matthews', 'matthews',
 'https://www.matthews.com',
 'https://www.matthews.com/listings',
 'Matthews Real Estate Investment Services. Net-lease / investment-sales specialist. Next.js site whose /properties/{slug} DETAIL pages are fully server-rendered and curl-fetchable with no token or JS render. The full catalog (~3.5k listings) is enumerable from the public sitemap.xml; tenure is encoded in the slug (leasing-* = lease, else investment sale). Collected by cre_collector/collect.ts (source key matthews). Parser ported from the sibling display repo dynamically-display-cre-listing-data/lib/live/parsers/matthews.ts.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "sitemap_enumeration", "sitemap_url": "https://www.matthews.com/sitemap.xml", "listing_url_pattern": "/properties/{slug}", "tenure_from_slug": {"lease": "leasing-*", "sale": "*"}, "notes": "Server-rendered detail pages; DOM hooks #propertyTitle/#propertyAddress/#propertyPrice/.key-info-title/.key-info-value, brokers a#agentName. No narrative description (uses Highlights). Images on cms.matthews.com/wp-content/uploads. Broker email/phone exposed; no role/title."}'::jsonb,
 true),

-- 13. Franklin Street -- added 2026-06-20 for the multi-source collector (cre_collector/collect.ts).
('Franklin Street', 'franklin-street',
 'https://www.franklinst.com',
 'https://www.franklinst.com/properties/',
 'Franklin Street. Full-service southeastern US CRE firm. Site renders listings client-side via Buildout (buildout.com/api.js), but the Buildout plugin inventory API is public and collected the same way as SVN/Lee. SEPARATE for-sale and for-lease plugin feeds. Verified 2026-06-20: 227 sale + 195 lease via inventory.json. Collected by cre_collector/collect.ts (source key franklin-street) through srcBuildout, selecting the plugin token by transaction.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "buildout_inventory_api_dual", "buildout_plugin_key_sale": "a234450b432b2b2bebc1ace7e6f692e4489bde70", "buildout_plugin_key_lease": "2f82fcd26667c4b0126d0084938ffa265f05fa4a", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N", "listing_url_pattern": "/properties/for-{sale|lease}/?propertyId={id}", "notes": "Client-rendered Buildout; reuse srcBuildout, picking the sale vs lease token by transaction (each feed is single-tenure). Subject to Buildout throttling like Lee; abort-on-3%-failed-pages guard applies."}'::jsonb,
 true),

-- 14. Lyon Stahl -- added 2026-06-20 for the multi-source collector (cre_collector/collect.ts).
('Lyon Stahl', 'lyon-stahl',
 'https://www.lyonstahl.com',
 'https://www.lyonstahl.com/properties/',
 'Lyon Stahl Investment Real Estate. LA-area multifamily investment-sales specialist. Own WordPress site exposes a clean property sitemap (/properties-sitemapN.xml, ~2,000 detail URLs) and server-rendered detail pages with rich JSON-LD (Product offers.price, ApartmentComplex address/units/floorSize, Person broker nodes). Also listed on Crexi, but the own site is fully enumerable by plain GET. Collected by cre_collector/collect.ts (source key lyon-stahl, sale-only) through srcLyonStahl.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "sitemap_index_enumeration", "sitemap_url": "https://www.lyonstahl.com/sitemap.xml", "listing_url_pattern": "/properties/{slug}/", "notes": "Investment-sales only (no lease feed). JSON-LD @graph parse: Product.offers.price, ApartmentComplex {address, numberOfRooms=units, floorSize=sqft, additionalProperty}, Person nodes for brokers (name+title+headshot; no per-agent email/phone). Sold/off-market dropped via offers.availability. Sitemap includes sold comps; only active listings kept."}'::jsonb,
 true),

-- 15. Faris Lee Investments -- added 2026-06-20 for the multi-source collector (cre_collector/collect.ts).
('Faris Lee Investments', 'faris-lee',
 'https://www.farislee.com',
 'https://www.farislee.com/listings/',
 'Faris Lee Investments. Retail net-lease investment-sales specialist (Irvine, CA). Site lists via Buildout; the public plugin inventory API is collected the same way as SVN/Lee/Franklin Street. Verified 2026-06-20: 77 active sale listings via inventory.json. Investment-sales only. Collected by cre_collector/collect.ts (source key faris-lee) through srcBuildout.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "de89d4f043da3999d293e1adcfd541bf2530acca", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N", "notes": "Buildout inventory API; reuse srcBuildout. All sale (retail net lease). Subject to Buildout throttling; abort-on-3%-failed-pages guard applies."}'::jsonb,
 true),

-- 16. Fortis Net Lease -- added 2026-06-20 for the multi-source collector (cre_collector/collect.ts).
('Fortis Net Lease', 'fortis-net-lease',
 'https://www.fortisnetlease.com',
 'https://www.fortisnetlease.com/net-lease-properties/',
 'Fortis Net Lease. Single-tenant net-lease investment-sales firm (metro Detroit). Lists via Buildout; the public plugin inventory API is collected the same way as SVN/Lee/Franklin Street/Faris Lee. Verified 2026-06-20: 86 active listings via inventory.json. Investment-sales focus. Collected by cre_collector/collect.ts (source key fortis-net-lease) through srcBuildout.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "8c286e4a49fdc706359ab9c041e0db1465de1fcf", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N", "notes": "Buildout inventory API; reuse srcBuildout. STNL net lease. Subject to Buildout throttling; abort-on-3%-failed-pages guard applies."}'::jsonb,
 true),

-- 17-23. Buildout-backed regional firms -- added 2026-06-20 (cre_collector/collect.ts BUILDOUT_FIRMS).
-- All collected via the public Buildout plugin inventory API through srcBuildout; sale/lease
-- partitioned client-side by the inventory sale flag. Tokens read from each firm's site 2026-06-20.
('Unique Properties', 'unique-properties', 'https://www.uniqueprop.com', 'https://www.uniqueprop.com',
 'Unique Properties (Denver). Full-service regional CRE brokerage on Buildout. 351 active listings via inventory.json (sale + lease).',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "43994fa6c8bc167acf6e799d1ecd08173254b362", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('Kiser Group', 'kiser-group', 'https://www.kisergroup.com', 'https://www.kisergroup.com',
 'Kiser Group (Chicago). Multifamily investment-sales brokerage on Buildout. 79 active listings via inventory.json.',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "f9624a304f0b834544c60c666a56ca16fcf29a1f", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('Pinnacle Real Estate Advisors', 'pinnacle-rea', 'https://www.pinnaclerea.com', 'https://www.pinnaclerea.com',
 'Pinnacle Real Estate Advisors (Denver). Multifamily/commercial investment-sales on Buildout. 684 active listings via inventory.json.',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "53aeead9dc03d2337633a409497ff7976f68d56c", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('Cawley Chicago', 'cawley-chicago', 'https://www.cawleychicago.com', 'https://www.cawleychicago.com',
 'Cawley Chicago. Industrial/commercial brokerage on Buildout. 179 active listings via inventory.json.',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "408316c565e1efe74e56779fffe3baa3fdc1f3cf", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('Bradford Allen', 'bradford-allen', 'https://www.bradfordallen.com', 'https://www.bradfordallen.com',
 'Bradford Allen (Chicago, national). Office-focused CRE brokerage on Buildout. 80 active listings via inventory.json (lease-heavy).',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "f2c7e5eec6ebe7de1f4a0b261bd9a04d715ca1e1", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('Hudson Peters Commercial', 'hudson-peters', 'https://www.hudsonpeters.com', 'https://www.hudsonpeters.com',
 'Hudson Peters Commercial (Dallas-Fort Worth). CRE brokerage on Buildout. 39 active listings via inventory.json.',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "fb2068dac489e1dacd436ebe03523aed6df9fe2e", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('Gibson Commercial Real Estate', 'gibson-commercial', 'https://www.gibsoncre.com', 'https://www.gibsoncre.com',
 'Gibson Commercial Real Estate (Texas). CRE brokerage on Buildout. 16 active listings via inventory.json.',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "cf76c48a3374831d301742075017a4b5e88642bc", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('Leibsohn & Co', 'leibsohn', 'https://www.leibsohn.com', 'https://www.leibsohn.com',
 'Leibsohn & Co (Phoenix, AZ). CRE brokerage on Buildout. 75 active listings via inventory.json. Collected via srcBuildout (BUILDOUT_FIRMS map).',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "9be8516e186ae4deb9ee10eafda9478aca7ffe68", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),

-- 25-29. More Buildout-backed firms -- added 2026-06-20 (cre_collector/collect.ts BUILDOUT_FIRMS). NAI members + regional independents.
('NAI Hiffman', 'nai-hiffman', 'https://www.hiffman.com', 'https://www.hiffman.com',
 'NAI Hiffman (Chicago). Largest independent CRE firm in the Midwest; NAI network member. Buildout inventory API. 435 active listings via inventory.json.',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "783881343a019c17532413fa9b120e61d47c2ae3", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('NAI Martens', 'nai-martens', 'https://www.naimartens.com', 'https://www.naimartens.com',
 'NAI Martens (Wichita, KS). NAI network member. Buildout inventory API. 160 active listings via inventory.json.',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "6351fc3e892388a1a2dbf1bdc7f65fd1ac144231", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('Bull Realty', 'bull-realty', 'https://www.bullrealty.com', 'https://www.bullrealty.com',
 'Bull Realty (Atlanta). National CRE brokerage + advisory. Buildout inventory API. 552 active listings via inventory.json.',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "6e2064ba71e11d85d50740c87a9372ef9c961a46", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('TRI Commercial', 'tri-commercial', 'https://www.tricommercial.com', 'https://www.tricommercial.com',
 'TRI Commercial (San Francisco Bay Area). CORFAC affiliate. Buildout inventory API. 288 active listings via inventory.json.',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "4d24ff217c26907aaaa12bb0837e451e568a61e4", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),
('Berger Commercial Real Estate', 'berger-commercial', 'https://www.bergercommercial.com', 'https://www.bergercommercial.com',
 'Berger Commercial Real Estate (South Florida). CRE brokerage + management. Buildout inventory API. 109 active listings via inventory.json.',
 '{"proxy": "auto", "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "b1a0682147c41af0dc0ea1af91664ab8ea766aa9", "notes": "Buildout inventory API via srcBuildout."}'::jsonb, true),

-- Round 5 Buildout firms -- added 2026-06-20 (collect.ts BUILDOUT_FIRMS).
('NAI Bergman', 'nai-bergman', 'https://www.naibergman.com', 'https://www.naibergman.com',
 'NAI Bergman (Cincinnati). NAI network member. Buildout inventory API. 145 active listings.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "70e208db445d84be6d7c074ee0108373ccf755a8", "notes": "srcBuildout."}'::jsonb, true),
('NAI Isaac', 'nai-isaac', 'https://www.naiisaac.com', 'https://www.naiisaac.com',
 'NAI Isaac (Lexington, KY). NAI network member. Buildout inventory API. 161 active listings.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "9ad3babf4f98852f6ed9b0b9db30388bb7e07c5a", "notes": "srcBuildout."}'::jsonb, true),
('Trinity Partners', 'trinity-partners', 'https://www.trinity-partners.com', 'https://www.trinity-partners.com',
 'Trinity Partners (Charlotte; Carolinas/Southeast). Buildout inventory API. 543 active listings.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "1c2d2e5340b1956e6a900d94c4dd3b41b69c2af9", "notes": "srcBuildout."}'::jsonb, true),
('Metro Commercial', 'metro-commercial', 'https://www.metrocommercial.com', 'https://www.metrocommercial.com',
 'Metro Commercial (Philadelphia; retail). Buildout inventory API. 512 active listings.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "45a0bd5e3569b2b9d10a3bd88f93fda41ba238f6", "notes": "srcBuildout."}'::jsonb, true),
('33 Realty', '33-realty', 'https://33realty.com', 'https://33realty.com',
 '33 Realty (Chicago; multifamily/management). Buildout inventory API. 62 active listings.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "5bdefd87a602a896a48f635e07a6724215ed764e", "notes": "srcBuildout."}'::jsonb, true),

-- Round 6 Buildout firms -- added 2026-06-20 (collect.ts BUILDOUT_FIRMS).
('NAI Hallmark', 'nai-hallmark', 'https://www.naihallmark.com', 'https://www.naihallmark.com',
 'NAI Hallmark (Jacksonville, FL). NAI network member. Buildout inventory API. 120 active listings.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "f883dbd9ac44b7702c0c0bfd4722925868f23ecb", "notes": "srcBuildout."}'::jsonb, true),
('NAI Plotkin', 'nai-plotkin', 'https://www.naiplotkin.com', 'https://www.naiplotkin.com',
 'NAI Plotkin (Springfield, MA). NAI network member. Buildout inventory API. 30 active listings.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "f3a493d487cf05648f54bc6264231beb9f4cd176", "notes": "srcBuildout."}'::jsonb, true),

-- Found via discover_buildout.py (token-fingerprint automation) -- added 2026-06-20.
('Greysteel', 'greysteel', 'https://www.greysteel.com', 'https://www.greysteel.com',
 'Greysteel (Washington DC; private-capital multifamily/commercial investment sales). Previously assumed Crexi-locked, but the Buildout inventory API works. 185 active listings.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "a6dbbaba3cc0ba7d1fbc587e9f06c953cebed964", "notes": "srcBuildout. Found by discover_buildout.py."}'::jsonb, true),
('NAI TALCOR', 'nai-talcor', 'https://www.naitalcor.com', 'https://www.naitalcor.com',
 'NAI TALCOR (Tallahassee, FL). NAI network member. Buildout inventory API. 284 active listings.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "b9b19d2a3f66dfc3bb532e8c5db7399f4db33349", "notes": "srcBuildout."}'::jsonb, true),
('NAI Dominion', 'nai-dominion', 'https://www.naidominion.com', 'https://www.naidominion.com',
 'NAI Dominion (Roanoke/Portsmouth, VA). NAI network member. Buildout inventory API. 50 active listings.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "6a78703278580ac43114429ef6f4a0d484167434", "notes": "srcBuildout."}'::jsonb, true),

-- SRS Real Estate Partners -- added 2026-06-20. Cracked via Firecrawl-stealth render + JS reverse-engineering.
('SRS Real Estate Partners', 'srs', 'https://www.srsre.com', 'https://www.srsre.com/properties',
 'SRS Real Estate Partners. Largest US real estate firm exclusively dedicated to retail; ~2,122 listings. Site is Cloudflare-protected Next.js, but listings come from a PUBLIC Google Cloud Run backend (Salesforce-backed): POST srsre-next-...run.app/api/property-search. Collected by cre_collector/collect.ts (source key srs) via direct paginated API calls (not Firecrawl).',
 '{"pagination_strategy": "salesforce_cloudrun_api", "api": "https://srsre-next-412955565034.us-central1.run.app/api/property-search", "method": "POST", "page_size": 12, "notes": "srcSrs. body={query:{offset:12*page,pageSize:12,...filters},client_ip:\"\"}; resp={total,properties[apto_data=Salesforce SRS_Listings__c]}. Open API, no auth. Reverse-engineered from JS bundle 2026-06-20."}'::jsonb, true),

-- Hanley Investment Group -- added 2026-06-20. Embedded rethink_properties JSON.
('Hanley Investment Group', 'hanley', 'https://www.hanleyinvestment.com', 'https://hanleyinvestmentgroup.com/listings/',
 'Hanley Investment Group (Corona del Mar, CA). Retail net-lease investment-sales specialist on the Rethink (Salesforce) CRE platform. The /listings/ page server-embeds the whole catalog in a JS var (rethink_properties); collected by direct fetch + JSON parse (no Firecrawl/API). ~102 listings. Sale/lease from dealRecordType.',
 '{"pagination_strategy": "embedded_json_var", "source_url": "https://hanleyinvestmentgroup.com/listings/", "var": "rethink_properties", "notes": "srcHanley. Direct fetch (Cloudflare monitor-mode), parse rethink_properties[]; firecrawl stealth fallback. Salesfoce-backed; brokers expose only an id (no name). Prices often null (hidden)."}'::jsonb, true),

-- Kidder Mathews -- added 2026-06-20. Cracked via Firecrawl render + JS reverse-engineering.
('Kidder Mathews', 'kidder-mathews', 'https://www.kidder.com', 'https://www.kidder.com/properties/',
 'Kidder Mathews. Largest independent CRE firm on the US West Coast (>$10B/yr). The kidder.com/properties jQuery app loads from a PUBLIC backend: POST services.kidder.com/search/public/listing. ~3,108 listings. Collected by cre_collector/collect.ts (source key kidder-mathews) via direct paginated API calls (not Firecrawl).',
 '{"pagination_strategy": "km_backend_api", "api": "https://services.kidder.com/search/public/listing", "method": "POST", "body": "{startIndex,numResults:50,includeAggregations:false}", "page_size": 50, "notes": "srcKidder. resp={totalResultCount,results[listing_key,property_*,list_price,asking_rent_max,sf_avail,use_type,brokers,lat/lon,photos]}. Open API, no auth. Reverse-engineered from app.min.js 2026-06-20."}'::jsonb, true),

-- 30-31. LLM-extraction firms -- added 2026-06-20. Own property sitemap, heterogeneous DOM,
-- no consistent JSON-LD: enumerated from sitemap then Firecrawl `json` LLM extraction per page
-- (collect.ts srcSitemapExtract / SITEMAP_EXTRACT_FIRMS). Requires a local LLM profile.
('Interra Realty', 'interra-realty', 'https://www.interrarealty.com', 'https://www.interrarealty.com',
 'Interra Realty (Chicago). Multifamily investment-sales brokerage. ~865 listings via own sitemap; fields extracted by Firecrawl LLM json (deepseek-v4-flash budget profile) with sanitization.',
 '{"proxy": "auto", "pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.interrarealty.com/sitemap.xml", "listing_url_pattern": "/listing/{slug}", "notes": "srcSitemapExtract; per-page LLM extraction cached across tenure passes. Verified extraction quality on sample."}'::jsonb, true),
('DAUM Commercial', 'daum-commercial', 'https://www.daumcommercial.com', 'https://www.daumcommercial.com',
 'DAUM Commercial (West Coast industrial/office). ~1,764 listings via own sitemap; fields extracted by Firecrawl LLM json with sanitization.',
 '{"proxy": "auto", "pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.daumcommercial.com/sitemap.xml", "listing_url_pattern": "/property/{slug}", "notes": "srcSitemapExtract; per-page LLM extraction cached across tenure passes."}'::jsonb, true),

-- 32-40. More LLM-extraction firms -- added 2026-06-20 (collect.ts SITEMAP_EXTRACT_FIRMS). Own sitemap + Firecrawl json LLM extraction. NOT yet run (holds for LLM-run approval).
('Foundry Commercial', 'foundry-commercial', 'https://www.foundrycommercial.com', 'https://www.foundrycommercial.com',
 'Foundry Commercial (Southeast US, national). ~642 listings via own sitemap; thin JSON-LD so uses LLM extraction.',
 '{"pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.foundrycommercial.com/sitemap.xml", "listing_url_pattern": "/property/{slug}"}'::jsonb, true),
('Essex Realty Group', 'essex-realty', 'https://www.essexrealtygroup.com', 'https://www.essexrealtygroup.com',
 'Essex Realty Group (Chicago multifamily). ~745 listings via own sitemap; LLM extraction.',
 '{"pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.essexrealtygroup.com/sitemap_index.xml", "listing_url_pattern": "/properties/{slug}"}'::jsonb, true),
('Pyramid Brokerage Company', 'pyramid-brokerage', 'https://www.pyramidbrokerage.com', 'https://www.pyramidbrokerage.com',
 'Pyramid Brokerage Company (Upstate NY; Cushman affiliate). ~2,115 listings via own sitemap; LLM extraction.',
 '{"pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.pyramidbrokerage.com/sitemap.xml", "listing_url_pattern": "/listings/{slug}"}'::jsonb, true),
('SHOP Companies', 'shop-companies', 'https://www.shopcompanies.com', 'https://www.shopcompanies.com',
 'SHOP Companies (Texas retail). ~528 listings via own sitemap; LLM extraction.',
 '{"pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.shopcompanies.com/sitemap.xml", "listing_url_pattern": "/properties/{slug}"}'::jsonb, true),
('Velocity Retail Group', 'velocity-retail', 'https://www.velocityretail.com', 'https://www.velocityretail.com',
 'Velocity Retail Group (Phoenix retail). ~108 listings via own sitemap; LLM extraction.',
 '{"pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.velocityretail.com/sitemap_index.xml", "listing_url_pattern": "/property/{slug}"}'::jsonb, true),
('AQUILA Commercial', 'aquila-commercial', 'https://www.aquilacommercial.com', 'https://www.aquilacommercial.com',
 'AQUILA Commercial (Austin office/industrial). ~85 listings via own sitemap; LLM extraction.',
 '{"pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.aquilacommercial.com/sitemap.xml", "listing_url_pattern": "/property/{slug}"}'::jsonb, true),
('Finial Group', 'finial-group', 'https://www.finialgroup.com', 'https://www.finialgroup.com',
 'Finial Group (Houston). ~127 listings via own sitemap; LLM extraction.',
 '{"pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.finialgroup.com/sitemap.xml", "listing_url_pattern": "/properties/{slug}"}'::jsonb, true),
('Ackerman & Co', 'ackerman', 'https://www.ackermanco.com', 'https://www.ackermanco.com',
 'Ackerman & Co (Atlanta). ~125 listings via own sitemap; LLM extraction.',
 '{"pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.ackermanco.com/sitemap_index.xml", "listing_url_pattern": "/properties/{slug}"}'::jsonb, true),
('Maury L. Carter & Associates', 'maury-carter', 'https://www.maurycarter.com', 'https://www.maurycarter.com',
 'Maury L. Carter & Associates (Orlando; land/investment). ~120 listings via own sitemap; LLM extraction.',
 '{"pagination_strategy": "sitemap_llm_extract", "sitemap_url": "https://www.maurycarter.com/sitemap.xml", "listing_url_pattern": "/property/{slug}"}'::jsonb, true)

ON CONFLICT (slug) DO UPDATE SET
    name          = EXCLUDED.name,
    base_url      = EXCLUDED.base_url,
    search_url    = EXCLUDED.search_url,
    description   = EXCLUDED.description,
    scrape_config = EXCLUDED.scrape_config,
    active        = EXCLUDED.active,
    updated_at    = now();
