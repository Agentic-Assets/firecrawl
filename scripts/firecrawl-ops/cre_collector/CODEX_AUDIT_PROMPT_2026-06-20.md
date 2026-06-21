# Codex prompt — audit the CRE collector expansion + propose collection strategies

Paste the block below into Codex (run it from the repo root,
`/Users/css0069/Dropbox/firecrawl_aa`). It is a read-only audit + ideation task.

---

You are auditing a commercial-real-estate (CRE) **listing collector** and proposing
better ways to collect brokerage listing data. Work in this repo; **do not edit code,
do not run full collections, and do not write to any database.** This is a read +
analyze + advise task. Produce a written report.

## Context

`scripts/firecrawl-ops/cre_collector/` is the production pipeline that scrapes public
CRE listings from brokerage websites (via a self-hosted Firecrawl API at
`http://localhost:3002`) and upserts them into a Supabase `credeals` schema. It feeds
EQUIRE's deal-intelligence product. Architecture:

- `collect.ts` — multi-source collector. Each source is an async `srcXxx(tx, max)`
  returning `SourceResult { company, sourceUrl, method, totalAvailable, listings[] }`.
  Listings share one field vocabulary (see the comment above `type SourceResult`).
  Sources are registered in `SOURCE_KEYS` + `runSource`. Everything is fetched
  **GET-only through Firecrawl** (`scrapeRaw`/`scrapeJson`); `proxy:"stealth"` is used
  for Cloudflare sites.
- `cre_ingest.py` — maps the collector JSON to `credeals.cre_listings` (+ contacts/
  documents/images children) via psql. `SOURCE_TO_BROKERAGE` must match the slugs in
  the SQL seed. Dedup key is `(brokerage_id, external_id)`. `--mark-missing`
  soft-deletes rows a full clean run no longer sees (guarded).
- `../sql/001_cre_brokerages.sql` — the brokerage registry seed (one row per firm).

Read these first, in order:
1. `scripts/firecrawl-ops/cre_collector/CLAUDE.md` (source matrix, ingest semantics)
2. `scripts/firecrawl-ops/cre_collector/TOP30_EXPANSION_PLAN_2026-06-20.md`
   (the full plan, the working discovery method, per-firm findings, and what's blocked)
3. `scripts/firecrawl-ops/cre_collector/collect.ts`
4. `scripts/firecrawl-ops/cre_collector/cre_ingest.py`
5. `scripts/firecrawl-ops/sql/001_cre_brokerages.sql`

## What was just built (2026-06-20) — audit this specifically

13 new brokerages were onboarded this session and verified locally (small probes +
dry-run ingest), but **not yet ingested to production**. They use two methods:

- **Buildout inventory API** (reuse of `srcBuildout`): Franklin Street (dual sale/
  lease plugin tokens), Faris Lee, Fortis Net Lease, and 7 regional firms in the
  `BUILDOUT_FIRMS` map (Unique Properties, Kiser Group, Pinnacle REA, Cawley Chicago,
  Bradford Allen, Hudson Peters, Gibson Commercial, Leibsohn). Each is just a token +
  metadata; the inventory is paged via `buildout.com/plugins/{token}/inventory.json`.
- **Own sitemap → server-rendered detail page parse**: `srcMatthews` (public
  `sitemap.xml`, DOM hooks, tenure from slug) and `srcLyonStahl` (WordPress
  sitemap-index → JSON-LD `Product`/`ApartmentComplex`/`Person`). New shared helpers:
  `extractLdNodes`/`ldType`/`metaContent`, and a `title` field added to `brokerRef`.

## Audit scope — be adversarial and specific (cite file:line)

1. **Enumeration completeness ("collect ALL their listings"):** For each new source,
   does it actually get the *whole* catalog, or silently a subset? Check: Buildout
   pagination + the abort-on-3%-failed-pages guard (could it cache a partial run?);
   Franklin Street's two-token sale/lease split (does anything fall through?);
   Matthews slug-based sale/lease partition (mis-class risk?); Lyon Stahl sold-comp
   filtering via `offers.availability` (over/under-filtering?).
2. **Field-mapping fidelity & data quality:** cap rate stored as fraction `[0,1]`;
   price parsing ("Best offer"/"Subject to Market" → null, not 0); address splitting;
   photos stored as URLs only; the new broker `title` dedup-merge; `external_id`
   stability (is the Buildout `id` / Matthews slug / Lyon Stahl slug stable across
   runs so upserts dedup instead of duplicating?).
3. **Schema/ingest alignment:** every new `SOURCE_TO_BROKERAGE` key has a matching SQL
   seed slug AND the parser `company` name is consistent; the SQL is valid (the
   ON CONFLICT block, quoting, the 7-row Buildout block, the new Leibsohn row).
4. **Lifecycle / soft-delete safety:** would a partial/failed run of any new source,
   combined with `--mark-missing`, wrongly soft-delete live rows?
5. **Cross-firm duplication:** the same physical property is often listed by multiple
   brokers and on aggregators (Crexi/RCM). The dedup key is per-brokerage
   `(brokerage_id, external_id)`, so duplicates across brokerages are NOT merged. Is
   that the right call? What would entity-resolution across firms look like?
6. **Robustness:** Buildout token rotation/expiry, Firecrawl returning HTML
   interstitials instead of JSON, OrbStack crashing under high concurrency (a
   concurrency-5 full run took the stack down — full runs are pinned to ≤3), timeouts.
7. **Bugs / correctness defects** in the new TypeScript (regex edge cases, null
   handling, the JSON-LD `@graph` flattening, image-host filters).

## Ideas wanted — collecting brokerage listing data better

The plan doc documents the tiers we hit. Give concrete, evaluated recommendations:

1. **Heterogeneous sitemap-DOM firms** (Interra ~865, Essex ~745, Pyramid ~1,115,
   SHOP ~528, Millennium, Velocity, AQUILA, Finial, Fuller, Foundry ~796): they have
   clean own sitemaps but NO consistent JSON-LD — fields are in theme-specific DOM.
   Evaluate **Firecrawl LLM structured extraction** (`json`/`extract` format with a
   fixed CRE schema) over sitemap-enumerated detail URLs as a single approach across
   all of them: quality, cost, determinism, failure modes, vs. per-firm DOM parsers.
2. **JS-SPA firms that hide their API** (Kidder Mathews, Northmarq [RCM-gated],
   Stream Realty, Berkadia, Walker & Dunlop): how to discover the hidden listing API
   (Firecrawl JS render + network capture? bundle analysis?), or render-and-paginate;
   and which are genuinely not worth it.
3. **Platform-level unlocks:** is there a scalable way to discover *all* Buildout
   firms (a registry, a token pattern, a directory)? What about Catylist (Moody's),
   Brevitas, CREXi via an authorized/partner API, or RCM? One platform integration
   could onboard dozens of firms.
4. **Aggregator strategy:** Crexi/LoopNet/CommercialCafe/Catylist host most firms'
   listings. Crexi's API is Cloudflare-walled + bearer-auth (we treat it as blocked).
   Is there a legitimate/authorized path (partner API, data licensing) that beats
   per-firm scraping? Weigh build-vs-license.
5. **Data quality at scale:** cross-source entity resolution / dedup, geocoding,
   property-type normalization, freshness/staleness detection, and detecting when a
   firm's site/template changes and silently breaks a parser.
6. **Operational:** scheduling/incremental runs, monitoring per-source yields for
   drift, the concurrency/throughput ceiling, and respecting robots.txt / ToS / rate
   limits (we store URLs only, never media binaries — keep it that way).

## Output

A structured markdown report: (A) audit findings as a ranked list of issues
(severity, file:line, why it matters, suggested fix), and (B) a prioritized,
effort-tagged set of collection-strategy recommendations with the single highest-ROI
next move called out. Do not modify files; propose, don't implement.
