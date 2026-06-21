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
-- Seed data: 12 national CRE brokerage slugs (15 collector source keys fold in;
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
 true)

ON CONFLICT (slug) DO UPDATE SET
    name          = EXCLUDED.name,
    base_url      = EXCLUDED.base_url,
    search_url    = EXCLUDED.search_url,
    description   = EXCLUDED.description,
    scrape_config = EXCLUDED.scrape_config,
    active        = EXCLUDED.active,
    updated_at    = now();
