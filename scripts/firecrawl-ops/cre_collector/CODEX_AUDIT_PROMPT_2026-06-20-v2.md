# Codex prompt v2 — audit the expanded CRE collector (Buildout fleet + LLM extraction)

Run from repo root (`/Users/css0069/Dropbox/firecrawl_aa`). Read-only audit + ideas.
Supersedes `CODEX_AUDIT_PROMPT_2026-06-20.md` (v1); a v1 audit already ran and its
fixes were applied — re-verify they hold.

---

You are auditing a commercial-real-estate listing collector and proposing improvements.
Work in this repo; **do not edit code, do not run full collections (they spend LLM
credits + hours), do not write to any database.** Read, analyze, advise. Produce a
written report.

## Context

`scripts/firecrawl-ops/cre_collector/` scrapes public CRE listings from brokerage
sites via a self-hosted Firecrawl API (`http://localhost:3002`) and upserts to a
Supabase `credeals` schema (live table: ~87k active listings across 12 legacy
brokerages). This session grew `collect.ts` from 14 to **48 source keys / 34 new
firms**, none yet in production (verified locally only).

Read first: `cre_collector/CLAUDE.md`; `cre_collector/TOP30_EXPANSION_PLAN_2026-06-20.md`;
`collect.ts`; `cre_ingest.py`; `sql/001_cre_brokerages.sql`. The v1 audit + applied
fixes are summarized in the plan doc's "Codex audit fixes applied" section.

## What was built this session — audit specifically

1. **Buildout fleet (21 firms).** Most onboard by one entry in the `BUILDOUT_FIRMS`
   map (`collect.ts`), resolved in `runSource`'s default branch → existing
   `srcBuildout` → `buildoutInventory` (paginated `buildout.com/plugins/{token}/
   inventory.json`). Franklin Street uses TWO tokens (separate sale/lease feeds).
   Plugin tokens were read from each firm's site.
2. **Generic LLM-extraction source (new).** For own-sitemap firms with heterogeneous
   DOM / no usable JSON-LD: `srcSitemapExtract` + `SITEMAP_EXTRACT_FIRMS` map (11
   firms). Per firm: `enumerateSitemap` (follow sitemap-index, match `detailPathRe`)
   → `scrapeExtract` (Firecrawl `json` format with `CRE_EXTRACT_SCHEMA` +
   `CRE_EXTRACT_PROMPT`, local LLM) per page → `sanitizeExtracted` (guardrails) →
   `extractCache` keyed per firm so the sale+lease passes share one set of paid LLM
   calls. Only `interra-realty` + `daum-commercial` have been run (small samples);
   the other 9 are config-wired, never executed.
3. **Local model config.** The local Firecrawl had NO LLM profile; I set
   `OPENAI_API_KEY` (OpenRouter key from a sibling .env) + `set_model_profile.sh
   budget` (deepseek-v4-flash).
4. **v1 audit fixes** (verify these are correct AND complete): `SourceResult.incomplete`
   flag + `cre_ingest.py` mark-missing gate; `buildoutInventory` shape validation;
   generalized Buildout `?propertyId=` dual-mode dedup in `cre_ingest.py`;
   `jsonLdObjects` recursive walker for Lyon Stahl; `moneyToNumber` K/M/B; concurrency
   ceiling 6→4.

## Audit scope — adversarial, cite file:line

**LLM extraction (highest scrutiny — it's new and unrun at scale):**
- `extractCache` + tenure filtering: a `sale_or_lease` listing is emitted in BOTH
  passes — does its `external_id` (URL-slug) make `cre_ingest` merge them to one row,
  or duplicate? Trace it.
- `max` is repurposed as a per-firm FETCH cap shared across passes — is that correct
  for `--max-items` probes AND `--max-items=0` full runs? Any off-by-one / double-fetch?
- `sanitizeExtracted` guardrails: too aggressive (dropping real data) or too loose
  (keeping hallucinations)? e.g. `buildingSizeSqft >= 100`, lease-rate `[1-9]` test,
  cap `0<x<=20`, `salePriceUsd` only when sale + `>=1000`, `_tt` inference when the
  model returns `unknown`.
- `scrapeExtract` assumes the response shape `doc.json ?? doc.data?.json`. Verify
  against the installed `@mendable/firecrawl-js` and the local API's actual `json`
  format response. Retry/empty handling.
- `enumerateSitemap`: host match strips only `www.`; sub-sitemap following is capped
  at 30 and filtered to `/propert|listing/i` names — could it MISS a firm's listing
  sub-sitemap (silent undercount that then looks "complete")? `detailPathRe` per firm
  in `SITEMAP_EXTRACT_FIRMS` — are any wrong (would enumerate 0)?
- Cost/hallucination risk at full scale (~6,400 pages); the `incomplete` flag only
  trips on extraction failures, not on low field-fill — is that the right safety line?

**Buildout fleet:**
- 21 firms share one `srcBuildout`. Confirm the v1 dedup-generalization covers ALL of
  them (URL-pattern keyed in `cre_ingest`), incl. Franklin's dual tokens and any
  `also_for_sale_or_lease` items. Token rotation/expiry exposure; `failedPages` →
  `incomplete`. `external_id` stability across re-runs (raw inventory id vs propertyId
  base).

**Cross-cutting:**
- With 48 sources, the same physical property appears under multiple firms /
  aggregators (Crexi/RCM). Dedup is per-brokerage `(brokerage_id, external_id)`. Is
  cross-firm duplication a real problem for the consuming app, and what would entity
  resolution look like?
- Production-landing safety: live table is ~87k across 12 legacy brokerages (incl.
  17k Colliers from a legacy Python path NOT in collect.ts, and 2k Transwestern with
  NO producing code). `--mark-missing` is scoped to in-run slugs (cre_ingest.py:571) —
  confirm a `collect.ts` run can't delete the legacy Colliers/Transwestern rows, and
  flag the real exposure (firms the collector produces at lower volume than the DB
  holds, e.g. JLL 4.7k-yield vs 11.7k-in-DB).
- `cre_ingest.py` shells out to `psql` with `POSTGRES_URL` from `CRE_EQUIRE/.env.local`
  — that URL currently FAILS auth. The landing is blocked on this.

## Ideas wanted — improve collection (evaluated, effort-tagged)

1. **Incremental LLM extraction via sitemap `<lastmod>`** — only (re)extract pages
   whose `lastmod` changed since the last run; cache extractions keyed by url+lastmod.
   This could turn a ~6,400-page full run into near-zero ongoing cost. Evaluate
   feasibility + where to persist the cache (the collector is currently stateless).
2. **Hybrid extraction** — deterministic `og:title`/JSON-LD/meta for address + price
   where present, LLM only for the rest. Would it raise field-fill (Interra address
   was inconsistent) and cut cost? Or keep pure-LLM for simplicity?
3. **Extraction quality/cost knobs** — prompt/schema improvements; model choice
   (flash vs pro vs gpt-5.4-mini — pro was only marginally better); `/v2/extract`
   batch vs per-page scrape `json`; concurrency vs the OrbStack crash ceiling.
4. **Buildout discovery automation** — a way to find more Buildout firms/tokens at
   scale (directory crawl, token pattern) vs the current manual per-metro sweeps.
5. **Entity resolution / dedup across 48 sources** — normalized-address + geo + parcel
   keying, confidence + provenance, WITHOUT collapsing per-broker listings.
6. **Operational** — incremental/scheduled runs, per-source yield + field-fill drift
   monitoring (detect a firm template change that silently breaks extraction), the
   DB-landing credential issue, and whether to port the legacy Colliers `ColliersScraper`
   into a `srcColliers` so it runs in the unified pipeline. Keep media as URLs only;
   respect robots.txt / ToS / rate limits.

## Output
(A) Audit findings — ranked by severity, each with file:line, why it matters, suggested
fix. (B) Prioritized, effort-tagged improvement recommendations with the single
highest-ROI next move called out (I suspect it's #1, incremental extraction — confirm
or refute). Propose, do not implement.
