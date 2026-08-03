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

-- RLS posture (defense in depth): collector-owned base table. RLS is ENABLED
-- with NO public row policy. The service-role / direct-postgres connection the
-- collector and ingestor use bypasses RLS, so this never blocks ingest; it only
-- ensures no rows leak if the table is ever reached via the Data API. Do NOT add
-- anon/authenticated policies (see cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md;
-- the 007 monitor tables already follow this pattern). ENABLE is a safe no-op if
-- already enabled, so 000_run_all.sql stays idempotent.
ALTER TABLE credeals.cre_brokerages ENABLE ROW LEVEL SECURITY;

-- -----------------------------------------------------------------------------
-- Seed data: 48 CRE brokerage slugs (51 collector source keys fold in;
-- cbre-dealflow -> cbre, jll-investor -> jll, colliers-main -> colliers).
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

-- 4. Colliers -- legacy SalesTracker plus public-site production adapters fold
-- into this brokerage slug.
('Colliers', 'colliers',
 'https://www.colliers.com',
 'https://www.colliers.com/en/properties',
 'Colliers International. Current production collectors include the SalesTracker investment-sale subset and the public colliers-main adapter; both fold into the colliers brokerage slug.',
 '{"proxy": "stealth", "wait_for_ms": 5000, "timeout_ms": 60000, "pagination_strategy": "salestracker_plus_public_site", "listing_url_pattern": "/en/properties/{name-slug}/{address-slug}/usa{7-digit-id}", "external_id_pattern": "usa[0-9]{7}", "search_url_pattern": "/en/properties#f:listingtype=[For%20Sale]&f:recenttransactions=[0]", "pagination_param": "#first=N", "notes": "Production adapters: colliers and colliers-main; both map to this brokerage slug."}'::jsonb,
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

-- 12. Transwestern -- public GET feed plus detail pages, added 2026-06-12.
('Transwestern', 'transwestern',
 'https://transwestern.com',
 'https://transwestern.com/properties',
 'Transwestern. Public property search exposes a repeatable GET feed at /properties?call=ajax with DealsType buckets for Sale, Lease, Sublease, and Sale or Lease. Detail pages expose broker profile links, vCards, flyer/PDF URLs, gallery image URLs, property facts, and availability tables.',
 '{"proxy": "auto", "wait_for_ms": 1500, "timeout_ms": 60000, "pagination_strategy": "public_ajax_get_deal_buckets", "search_url_pattern": "/properties?call=ajax&DealsType={Sale|Lease|Sublease|Sale%20or%20Lease}", "listing_url_pattern": "/property/{PageUrl}", "notes": "Collected by cre_collector source key transwestern. Use GET, not the browser POST body. Skip feed rows whose PageUrl is empty or ''-''."}'::jsonb,
 true),

-- 13. Matthews -- public sitemap plus server-rendered detail pages, added from stace-june20 recovery.
('Matthews', 'matthews',
 'https://www.matthews.com',
 'https://www.matthews.com/listings',
 'Matthews Real Estate Investment Services. Public sitemap.xml exposes /properties/{slug} detail URLs. Detail pages are server-rendered and can be collected with throttled plain fetch; lease listings are identified by leasing-* slugs.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "sitemap_plain_fetch", "search_url_pattern": "/sitemap.xml", "listing_url_pattern": "/properties/{slug}", "notes": "Collected by cre_collector source key matthews. Use throttled direct HTTP fetch, not Firecrawl renders, to avoid provider rate limits."}'::jsonb,
 true),

-- 14. Franklin Street -- Buildout-backed dual-token property inventory, added from stace-june20 recovery.
('Franklin Street', 'franklin-street',
 'https://www.franklinst.com',
 'https://www.franklinst.com/properties/',
 'Franklin Street. Public property inventory is powered by two Buildout plugin feeds, one for sale and one for lease. The collector uses direct Buildout JSON with complete-page requirements and per-tenure cache slugs.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "buildout_dual_plugin_inventory_api", "search_url_pattern": "/properties/", "listing_url_pattern": "/properties/?propertyId={id}", "notes": "Collected by cre_collector source key franklin-street. Sale token and lease token are selected per transaction pass."}'::jsonb,
 true),

-- 15. SRS Real Estate Partners -- direct public Cloud Run search API.
('SRS Real Estate Partners', 'srs',
 'https://www.srsre.com',
 'https://www.srsre.com/properties',
 'SRS Real Estate Partners. Cloudflare-protected site, but listing data is exposed by a public Salesforce-backed Cloud Run search API used by the site bundle.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "salesforce_cloudrun_api", "api": "https://srsre-next-412955565034.us-central1.run.app/api/property-search", "method": "POST", "page_size": 12, "notes": "Collected by source key srs via direct paginated API calls."}'::jsonb,
 true),

-- 16. Hanley Investment Group -- embedded Rethink JSON.
('Hanley Investment Group', 'hanley',
 'https://hanleyinvestmentgroup.com',
 'https://hanleyinvestmentgroup.com/listings/',
 'Hanley Investment Group. Retail net-lease investment-sales specialist on the Rethink Salesforce CRE platform. The listings page embeds the public catalog in a rethink_properties JavaScript array.',
 '{"proxy": "stealth", "wait_for_ms": 3000, "timeout_ms": 60000, "pagination_strategy": "embedded_json_var", "source_url": "https://hanleyinvestmentgroup.com/listings/", "var": "rethink_properties", "notes": "Collected by source key hanley via direct fetch with Firecrawl raw fallback."}'::jsonb,
 true),

-- 17. Kidder Mathews -- direct public backend API.
('Kidder Mathews', 'kidder-mathews',
 'https://www.kidder.com',
 'https://www.kidder.com/properties/',
 'Kidder Mathews. The property search application loads listing data from a public Kidder backend search API at services.kidder.com.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "kidder_backend_api", "api": "https://services.kidder.com/search/public/listing", "method": "POST", "page_size": 50, "notes": "Collected by source key kidder-mathews via direct paginated API calls."}'::jsonb,
 true),

-- 18. Faris Lee Investments -- recovered public Buildout inventory definition.
('Faris Lee Investments', 'faris-lee',
 'https://www.farislee.com',
 'https://www.farislee.com/listings/',
 'Faris Lee Investments. Retail and net-lease investment-sales inventory backed by a public Buildout plugin feed.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "de89d4f043da3999d293e1adcfd541bf2530acca", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N", "notes": "Recovered historical public-feed definition. Uses the governed shared Buildout adapter with stable ordering, complete-page validation, and source-scoped cache recovery."}'::jsonb,
 true),

-- 19. Fortis Net Lease -- recovered public Buildout inventory definition.
('Fortis Net Lease', 'fortis-net-lease',
 'https://www.fortisnetlease.com',
 'https://www.fortisnetlease.com/net-lease-properties/',
 'Fortis Net Lease. Net-lease investment-sales inventory backed by a public Buildout plugin feed.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "8c286e4a49fdc706359ab9c041e0db1465de1fcf", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N", "notes": "Recovered historical public-feed definition. Uses the governed shared Buildout adapter with stable ordering, complete-page validation, and source-scoped cache recovery."}'::jsonb,
 true),

-- 20. Unique Properties -- recovered public Buildout inventory definition.
('Unique Properties', 'unique-properties',
 'https://www.uniqueprop.com',
 'https://www.uniqueprop.com/',
 'Unique Properties. Commercial property inventory backed by a public Buildout plugin feed.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "43994fa6c8bc167acf6e799d1ecd08173254b362", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N", "notes": "Recovered historical public-feed definition. Uses the governed shared Buildout adapter with stable ordering, complete-page validation, and source-scoped cache recovery."}'::jsonb,
 true),

-- 21. Kiser Group -- recovered public Buildout inventory definition.
('Kiser Group', 'kiser-group',
 'https://www.kisergroup.com',
 'https://www.kisergroup.com/',
 'Kiser Group. Multifamily investment-sales inventory backed by a public Buildout plugin feed.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "f9624a304f0b834544c60c666a56ca16fcf29a1f", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N", "notes": "Recovered historical public-feed definition. Uses the governed shared Buildout adapter with stable ordering, complete-page validation, and source-scoped cache recovery."}'::jsonb,
 true),

-- 22. Pinnacle Real Estate Advisors -- recovered public Buildout inventory definition.
('Pinnacle Real Estate Advisors', 'pinnacle-rea',
 'https://www.pinnaclerea.com',
 'https://www.pinnaclerea.com/',
 'Pinnacle Real Estate Advisors. Commercial property inventory backed by a public Buildout plugin feed.',
 '{"proxy": "auto", "wait_for_ms": 0, "timeout_ms": 60000, "pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "53aeead9dc03d2337633a409497ff7976f68d56c", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N", "notes": "Recovered historical public-feed definition. Uses the governed shared Buildout adapter with stable ordering, complete-page validation, and source-scoped cache recovery."}'::jsonb,
 true),

-- 23-42. Remaining historical public Buildout feeds recovered from 6245a7144.
('Cawley Chicago', 'cawley-chicago',
 'https://www.cawleychicago.com', 'https://www.cawleychicago.com/',
 'Cawley Chicago. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "408316c565e1efe74e56779fffe3baa3fdc1f3cf", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('Bradford Allen', 'bradford-allen',
 'https://www.bradfordallen.com', 'https://www.bradfordallen.com/',
 'Bradford Allen. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "f2c7e5eec6ebe7de1f4a0b261bd9a04d715ca1e1", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('Hudson Peters Commercial', 'hudson-peters',
 'https://www.hudsonpeters.com', 'https://www.hudsonpeters.com/',
 'Hudson Peters Commercial. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "fb2068dac489e1dacd436ebe03523aed6df9fe2e", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('Gibson Commercial Real Estate', 'gibson-commercial',
 'https://www.gibsoncre.com', 'https://www.gibsoncre.com/',
 'Gibson Commercial Real Estate. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "cf76c48a3374831d301742075017a4b5e88642bc", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('Leibsohn & Co', 'leibsohn',
 'https://www.leibsohn.com', 'https://www.leibsohn.com/',
 'Leibsohn & Co. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "9be8516e186ae4deb9ee10eafda9478aca7ffe68", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('NAI Hiffman', 'nai-hiffman',
 'https://www.hiffman.com', 'https://www.hiffman.com/',
 'NAI Hiffman. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "783881343a019c17532413fa9b120e61d47c2ae3", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('NAI Martens', 'nai-martens',
 'https://www.naimartens.com', 'https://www.naimartens.com/',
 'NAI Martens. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "6351fc3e892388a1a2dbf1bdc7f65fd1ac144231", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('Bull Realty', 'bull-realty',
 'https://www.bullrealty.com', 'https://www.bullrealty.com/',
 'Bull Realty. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "6e2064ba71e11d85d50740c87a9372ef9c961a46", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('TRI Commercial', 'tri-commercial',
 'https://www.tricommercial.com', 'https://www.tricommercial.com/',
 'TRI Commercial. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "4d24ff217c26907aaaa12bb0837e451e568a61e4", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('Berger Commercial Real Estate', 'berger-commercial',
 'https://www.bergercommercial.com', 'https://www.bergercommercial.com/',
 'Berger Commercial Real Estate. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "b1a0682147c41af0dc0ea1af91664ab8ea766aa9", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('NAI Bergman', 'nai-bergman',
 'https://www.naibergman.com', 'https://www.naibergman.com/',
 'NAI Bergman. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "70e208db445d84be6d7c074ee0108373ccf755a8", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('NAI Isaac', 'nai-isaac',
 'https://www.naiisaac.com', 'https://www.naiisaac.com/',
 'NAI Isaac. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "9ad3babf4f98852f6ed9b0b9db30388bb7e07c5a", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('Trinity Partners', 'trinity-partners',
 'https://www.trinity-partners.com', 'https://www.trinity-partners.com/',
 'Trinity Partners. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "1c2d2e5340b1956e6a900d94c4dd3b41b69c2af9", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('Metro Commercial', 'metro-commercial',
 'https://www.metrocommercial.com', 'https://www.metrocommercial.com/',
 'Metro Commercial. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "45a0bd5e3569b2b9d10a3bd88f93fda41ba238f6", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('33 Realty', '33-realty',
 'https://33realty.com', 'https://33realty.com/',
 '33 Realty. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "5bdefd87a602a896a48f635e07a6724215ed764e", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('NAI Hallmark', 'nai-hallmark',
 'https://www.naihallmark.com', 'https://www.naihallmark.com/',
 'NAI Hallmark. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "f883dbd9ac44b7702c0c0bfd4722925868f23ecb", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('NAI Plotkin', 'nai-plotkin',
 'https://www.naiplotkin.com', 'https://www.naiplotkin.com/',
 'NAI Plotkin. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "f3a493d487cf05648f54bc6264231beb9f4cd176", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('Greysteel', 'greysteel',
 'https://www.greysteel.com', 'https://www.greysteel.com/',
 'Greysteel. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "a6dbbaba3cc0ba7d1fbc587e9f06c953cebed964", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('NAI TALCOR', 'nai-talcor',
 'https://www.naitalcor.com', 'https://www.naitalcor.com/',
 'NAI TALCOR. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "b9b19d2a3f66dfc3bb532e8c5db7399f4db33349", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),
('NAI Dominion', 'nai-dominion',
 'https://www.naidominion.com', 'https://www.naidominion.com/',
 'NAI Dominion. Public Buildout-backed commercial property inventory.',
 '{"pagination_strategy": "buildout_inventory_api", "buildout_plugin_key": "6a78703278580ac43114429ef6f4a0d484167434", "inventory_url": "https://buildout.com/plugins/{key}/inventory.json?page=N"}'::jsonb, true),

-- 43-48. Dedicated restored adapters with current source-specific validation.
('Interra Realty', 'interra-realty',
 'https://www.interrarealty.com', 'https://interrarealty.com/listings/',
 'Interra Realty. Sale inventory from the public filtered-loop endpoint with exact lifecycle reconciliation.',
 '{"pagination_strategy": "interra_filtered_loop", "notes": "Dedicated adapter; current, under-contract, and closed counts reconcile before strict admission."}'::jsonb, true),
('Essex Realty Group', 'essex-realty',
 'https://www.essexrealtygroup.com', 'https://www.essexrealtygroup.com/properties/',
 'Essex Realty Group. Current and archived sale inventory with validated property-detail pages.',
 '{"pagination_strategy": "essex_current_archive_html", "notes": "Dedicated adapter; sitemap identity and lifecycle scopes reconcile before detail admission."}'::jsonb, true),
('Pyramid Brokerage Company', 'pyramid-brokerage',
 'https://www.pyramidbrokerage.com', 'https://www.pyramidbrokerage.com/listings/',
 'Pyramid Brokerage Company. Public WordPress inventory with direct property-detail validation.',
 '{"pagination_strategy": "wordpress_rest", "notes": "Dedicated adapter; stable post identity and full detail validation."}'::jsonb, true),
('DAUM Commercial Real Estate Services', 'daum-commercial',
 'https://daumcommercial.com', 'https://daumcommercial.com/properties/',
 'DAUM Commercial Real Estate Services. Public WordPress search inventory with direct property-detail validation.',
 '{"pagination_strategy": "wordpress_ajax", "notes": "Dedicated adapter; exact tenure and shortlink post identity validation."}'::jsonb, true),
('Foundry Commercial', 'foundry-commercial',
 'https://www.foundrycommercial.com', 'https://www.foundrycommercial.com/properties/',
 'Foundry Commercial. Property sitemap inventory with validated public detail pages.',
 '{"pagination_strategy": "property_sitemap", "notes": "Dedicated adapter; sitemap and detail identities fail closed."}'::jsonb, true),
('Lyon Stahl', 'lyon-stahl',
 'https://lyonstahl.com', 'https://lyonstahl.com/properties/',
 'Lyon Stahl. Property sitemap inventory with validated public sale detail pages.',
 '{"pagination_strategy": "property_sitemap", "notes": "Dedicated adapter; sitemap, shortlink identity, and availability fail closed."}'::jsonb, true)

ON CONFLICT (slug) DO UPDATE SET
    name          = EXCLUDED.name,
    base_url      = EXCLUDED.base_url,
    search_url    = EXCLUDED.search_url,
    description   = EXCLUDED.description,
    scrape_config = EXCLUDED.scrape_config,
    active        = EXCLUDED.active,
    updated_at    = now();
