# CRE Collector TODO

Forward-looking workstream ideas for the EQUIRE listing intelligence pipeline.
This file captures product, automation, and research directions that build on
the observe-only monitor layer, enrichment queue, and the column-coverage baseline
in `CRE_LISTINGS_COLUMN_COVERAGE_2026-06-15.md`.

**Canonical shipped/planned infra:** `START_HERE.md`, `ENRICHMENT_WORKER_DESIGN_2026-06-15.md`,
`FRESHNESS_HISTORY_REVIEW_2026-06-15.md`, `docs/firecrawl-ops/references/cre-intelligence-system-design.md`.

**Status key:** `[idea]` not scoped | `[design]` spec started | `[blocked]` external gate | `[ready]` prerequisites met

---

## 1. Disappearance-triggered automations

When monitor or mark-missing detects that a listing vanished (URL 404, feed drop,
soft-delete, or `disappeared` event in `cre_listing_events`), run downstream
workflows instead of treating it as a silent board shrink.

### 1.1 Event contract (foundation)

- [ ] `[ready]` Define a single internal `listing_lifecycle` event shape sourced from:
  - `cre_listing_events.event_type = 'disappeared'` (observe-only monitor path)
  - mark-missing soft-delete + paired `disappeared` event (ingest path, shipped in code)
  - optional URL health check (HEAD/GET via Firecrawl) when `source_url` still exists but feed id is gone
- [ ] `[design]` Add `cre_listing_workflow_queue` (or reuse `cre_enrichment_queue` with `job_type`) for post-disappearance jobs: `verify_url`, `broker_outreach`, `deep_research`, `archive_snapshot`
- [ ] `[idea]` Priority rules: high-value listings first (sale price present, cap rate, large SF, recent price_change, mandate-relevant markets)

### 1.2 Verify-before-act

- [ ] `[idea]` On disappearance, re-scrape the listing URL once (stealth) and classify:
  - hard 404 / redirect off-site
  - page live but status terminal (sold, leased, withdrawn)
  - page live but listing id changed (re-list detection via `canonical_key`)
  - transient / bot block (do not email broker yet)
- [ ] `[idea]` Write outcomes back to `cre_listing_events` as `status_change` or `relisted` without mutating board status until ingest gate approves

### 1.3 Automations to wire after verification

- [ ] `[idea]` **Archive capture:** snapshot final HTML/markdown, hero screenshot, and child document URLs into durable storage before child rows are pruned (extends 009 archive tables)
- [ ] `[idea]` **Notify internal team:** Slack or email digest of disappearances by brokerage, city, and estimated deal size
- [ ] `[idea]` **Enqueue enrichment backfill** on ambiguous cases (page up, fields blank) before broker outreach
- [ ] `[idea]` **Consumer hook:** EQUIRE "recently removed" feed from `v_cre_recent_changes` + disappearance metadata

**Dependencies:** monitor tier running reliably; consumer board-gate deploy for any status-facing UI; draft-first email policy (`~/AGENTS.md`).

---

## 2. Automated broker outreach (disappearance and data gaps)

Turn "listing gone" and "field missing" signals into structured broker conversations
that recover price, status, and OM-level detail humans would otherwise chase manually.

### 2.1 Outreach triggers

- [ ] `[idea]` Disappearance with prior `sale_price_usd` or `cap_rate` populated (likely real deal, not junk row)
- [ ] `[idea]` Disappearance within N days of a `price_change` event (deal may have traded)
- [ ] `[idea]` Persistent high-value row with POA pricing (`sale_price_usd` null but description mentions auction, portfolio, etc.)
- [ ] `[idea]` Column-coverage gap on otherwise complete listing (e.g. geo + size + contacts present, no price after enrichment pass)

### 2.2 Email workflow (draft-first)

- [ ] `[blocked]` Integrate Resend (or existing AA mail stack) with **draft-only** default; never auto-send without explicit Cayman approval per release gate
- [ ] `[idea]` Template family:
  - "Listing no longer on your site" (cite `source_url`, `external_id`, last seen date)
  - "Status check" (under contract vs sold vs withdrawn)
  - "Pricing request" (ask price, cap rate, NOI, guidance)
  - "Document request" (OM / brochure if disappeared before download)
- [ ] `[idea]` Personalize To: from `cre_listing_contacts` (primary broker first); fallback to brokerage generic inbox from `cre_brokerages` metadata
- [ ] `[idea]` Log every outreach attempt in new table `cre_listing_outreach` (listing_id, contact_id, template, draft_id, sent_at, response_received_at, parsed_fields jsonb)

### 2.3 Response ingestion

- [ ] `[idea]` Inbound mailbox webhook → parse broker reply → map to `sale_price_usd`, `status`, `transaction_date`, free-text notes
- [ ] `[idea]` Human review queue before writing parsed values to `cre_listings` (avoid trusting unverified email text as ground truth)
- [ ] `[idea]` Link recovered facts to `cre_listing_price_history` and `cre_listing_events` with provenance `source='broker_email'`

**Dependencies:** contact coverage (97.5% today); legal/compliance review for automated broker email; Attio or CRM sync optional later.

---

## 3. Deep research on disappeared or changed listings

When the feed goes quiet, run multi-source investigation to infer what happened
without waiting for a broker reply.

### 3.1 Property-level research pack

- [ ] `[idea]` Given address + geo + prior broker, run a research workflow:
  - county recorder / assessor search (where public)
  - news search (trade press, local business journal)
  - CoStar / LoopNet / public web mentions (respect access matrix; no blocked-source scraping in production path)
  - prior OM PDF parse if document URL was captured
- [ ] `[idea]` Store structured output in `cre_listing_research_runs` (listing_id, run_at, hypothesis, confidence, evidence_urls[], summary markdown)

### 3.2 Hypothesis taxonomy

- [ ] `[idea]` Classify each disappearance into explainable buckets:
  - traded (sale/lease comp likely)
  - withdrawn / expired
  - relisted under new id or brokerage
  - data error / duplicate merge
  - unknown (broker outreach candidate)

### 3.3 Tooling

- [ ] `[idea]` Firecrawl `/search` + `/scrape` research agent with strict budget cap per listing
- [ ] `[idea]` Optional Corbis pass for academic comps or published transaction studies when property type and market match
- [ ] `[idea]` Batch mode: weekly "disappearance dossier" PDF or Notion export for analyst review

**Dependencies:** enrichment worker for detail refresh; document archive on high-value rows; local Firecrawl stack healthy. For count benchmarks vs LoopNet / CoStar, see section 6 (do not scrape blocked sources in the daily production path).

---

## 4. Listing quality and research score

Give every listing a reproducible score from heterogeneous inputs so EQUIRE can
rank deals, prioritize enrichment, and feed ML or paper-ready datasets.

### 4.1 Score inputs (multi-modal)

| Input | Source today | Notes |
|-------|----------------|-------|
| Column completeness | `CRE_LISTINGS_COLUMN_COVERAGE_2026-06-15.md` | 16-field core score per row |
| Structured fields | `cre_listings` columns + `raw_data` | Price, size, type, status |
| Child richness | contacts, documents, images counts | Already strong for images/contacts |
| Listing page screenshot | not stored | Capture at collect or enrich time |
| Document text | URL only | Parse OM/brochure on demand |
| Image signals | URLs only | Optional CV: property type verification, vacancy cues |
| Freshness | `updated_date`, `scraped_at`, price history | From 009 history tables when applied |
| Monitor stability | `cre_source_index`, event ledger | Churn, re-list, flip frequency |
| Broker reliability | response rate from outreach log | Future |

### 4.2 Score design tasks

- [ ] `[design]` Define `listing_research_score` (0–100) with explicit sub-scores:
  - **data_completeness** (column + child table coverage)
  - **source_trust** (brokerage prior, monitor error rate)
  - **evidence_depth** (documents parsed, screenshot present, description length)
  - **freshness** (recency of scrape and source update)
  - **deal_signal** (price, cap rate, size, market tier heuristics)
- [ ] `[idea]` Persist scores in `cre_listings.research_score` + `research_score_detail jsonb` or side table with versioned score schema
- [ ] `[idea]` Nightly recompute job; bump score when enrichment worker fills gaps
- [ ] `[idea]` EQUIRE UI: sort/filter by score; badge "thin data" vs "investment-ready"

### 4.3 Screenshot pipeline

- [ ] `[idea]` At enrich or monitor-change time, request Playwright screenshot of `source_url` (or canonical listing page)
- [ ] `[idea]` Store in Supabase Storage or external blob; hash for dedup; link from listing row
- [ ] `[idea]` Use screenshot as optional VLM input for score + property-type QA (compare to structured `property_type`)

**Dependencies:** Tier-B enrichment worker; storage policy for screenshots; cost cap per listing.

---

## 5. Academic research directions

Several paper-grade questions fall out of this infrastructure once disappearance
events, price history, and outreach outcomes exist at scale.

### 5.1 Candidate papers

1. **Listing duration and delisting outcomes in commercial brokerage markets**
   - Data: time from first seen → disappearance; price_change sequences; status labels
   - Question: what predicts fast removal vs long-on-market stale inventory?

2. **Broker platform data quality and selective disclosure**
   - Data: column coverage by brokerage (`cre_listings_column_coverage` CSV); POA rates; geo without address (NAI-style feeds)
   - Question: how does public-feed completeness vary across platforms and property types?

3. **Price revision dynamics before transaction**
   - Data: `cre_listing_price_history` + monitor `price_change` events
   - Question: do CRE ask prices mean-revert, spiral, or stick until delist?

4. **Automated broker outreach as missing-data recovery**
   - Data: outreach log + response parse rate + field recovery rate
   - Question: can lightweight email agents recover economically meaningful fields cheaper than full re-scrape?

5. **Multi-modal listing quality scores and downstream outcomes**
   - Data: research score components vs eventual disappearance class (traded vs withdrawn)
   - Question: which cheap signals (screenshot, doc count, text length) predict real deals?

### 5.2 Research enablers (engineering)

- [ ] `[idea]` Anonymized research export view (no broker emails in public artifact; aggregate brokerage slugs only)
- [ ] `[idea]` Reproducible panel: `listing_id`, `first_seen`, `last_seen`, `price_path[]`, `disappearance_class`, `research_score_v1`
- [ ] `[idea]` Document methodology appendix tying score inputs to collector provenance (`scraped_at` vs `updated_date` semantics)

---

## 6. Suggested implementation order

| Phase | Focus | Unblocks |
|------:|-------|----------|
| **A** | Enrichment worker drain (`cre_enrich.py`, launchd enrich tier) | Column fill, detail refresh |
| **B** | Apply 009 history migration + price history in prod | Duration and price-path papers |
| **C** | Disappearance verify job (URL re-scrape classifier) | Safe automations |
| **D** | `cre_listing_workflow_queue` + internal Slack digest | Operator visibility |
| **E** | Listing research score v1 (no screenshot) | EQUIRE ranking |
| **F** | Draft-first broker outreach + response log | Missing-data recovery |
| **G** | Screenshot + deep research agent | Multi-modal score, paper 5 |
| **H** | Research export panel | Academic outputs |

---

## 7. Open questions

- Should disappearance automations run on **monitor-only** events, **mark-missing** events, or both (different false-positive rates)?
- Minimum evidence bar before emailing a broker (avoid spam on monitor enumeration noise)?
- Where should screenshots and parsed OMs live (Supabase Storage vs external blob vs URL-only forever)?
- Which score sub-components are product-facing in EQUIRE vs internal-only QA?

---

## 8. Related artifacts

| File | Role |
|------|------|
| `CRE_LISTINGS_COLUMN_COVERAGE_2026-06-15.md` | Column fill baseline |
| `reports/cre_listings_coverage_summary_2026-06-15.json` | Machine-readable coverage |
| `FRESHNESS_HISTORY_REVIEW_2026-06-15.md` | History, disappearance events, archives |
| `ENRICHMENT_WORKER_DESIGN_2026-06-15.md` | Queue drain worker plan |
| `docs/firecrawl-ops/references/cre-monitor-subsystem.md` | Monitor safety rails |
| `docs/firecrawl-ops/references/cre-equire-consumer-api.md` | EQUIRE read contract |
