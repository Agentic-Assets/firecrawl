# lib/ Module

## Most Critical Rule

**Shared collector primitives only.** Broker fetch, parse, and `external_id` logic stays in `../sources/`; add to `lib/` only when two or more adapters need the same helper. Types and adapter contracts: `../types.ts`.

## Module Map

| File | Owns |
|------|------|
| `response-body.ts` | Reader-only capped UTF-8 accumulation and absolute monotonic body deadlines for DAUM, Foundry, Lyon and Matthews; bounded cancellation acknowledgement and exact-read-error settlement witnesses. Callers own reader acquisition/release, controllers, HTTP/header policy and retry decisions. |
| `config.ts` | CLI `parseArgs` (strict, import-time); `API_URL`, `flags`, `PAGE_CAP`, `CONCURRENCY`, `OUT_PATH` |
| `scrape.ts` | Firecrawl singleton; `scrapeRaw` / `scrapeDoc` / `scrapeJson` (up to 3× retry, no retry after a client deadline with unknown remote settlement); `parseJsonBody` for HTML-wrapped JSON. `scrapeDoc` requests `markdown`/`links`/`images`/`rawHtml` + an `attributes` selector block (video/iframe/anchor) for the harvester |
| `scrape_transport.ts` | Fork-owned v2 scrape HTTP transport. An AbortSignal stays outside JSON and aborts HTTP/body/backoff waits; bounded 502 retry and SDK error/data shapes are retained. The pinned SDK exposes no public scrape signal. |
| `performance.ts` | Optional bounded run/request aggregates, separate Firecrawl and direct-provider counts, source context and atomic event-driven snapshots. Measurement failures cannot change acquisition. Coverage and interpretation live in the [performance guide](../../../../docs/firecrawl-ops/references/cre-performance-telemetry.md). |
| `harvest.ts` | Pure `harvestDetail(doc, ctx)` -> `{media, links, documents, images}`. Classifies video/tour providers, documents, links, gallery images from a detail scrape; identity-dedups media (provider+id, vimeo hash preserved); feeds `cre_listing_media`/`cre_listing_links`. No argv/network import |
| `detail-tables/` | Bounded ordered native HTML table observations for shared Buildout detail enrichment, plus shape validation before preserving caller-owned snapshots across pruning. Read its scoped `AGENTS.md` and README; no normalized economics, publication or computed-visibility claim. |
| `source-page-context/` | Bounded source-native page context and a limited challenge-shell detector shared by Foundry, Lyon and Matthews. Caller owns fetch, clocks, source qualification and private-retention admission; read scoped `AGENTS.md` and README. Draft integrations remain unverified under the operator's test/service hold. |
| `parse.ts` | Shared CRE text parsers (price/sqft/cap-rate/address); Python-mirrored by `cre_parse.py`, verified identical via golden vectors (`tests/ts/lib/parse.test.ts`) |
| `geo.ts` | Pure ZIP/geo normalizers (`zip5`, `geoKey`); the full ZIP->county+CBSA crosswalk lookup lives in Python `cre_geo.py` (backfill + ingest run there) |
| `enrich.ts` | Tier-B enrichment helpers: batch-claim SQL, URL echoing for completion matching, dead-letter filtering for `cre_enrich.py` |
| `enrichment-evidence/` | Bounded private generic-enrichment observations, separate from canonical rows and queue completion. Source results mark incomplete evidence without discarding qualified rows; read scoped `AGENTS.md` and README. Targeted artifacts are not automatically saved to the evidence database. |
| `broker.ts` | Run-wide `brokers[]` + `brokerRef()` dedupe (`email\|name\|company`); merged by `collect.ts` |
| `html.ts` | JSON-LD, sitemap XML, entity decode, `stripHtmlText`, `dedupeStrings` |
| `util.ts` | `clean`, `num`, `prune`, `pmap`, `moneyToNumber`, `isPerSfPriceText`, `boundedInt` |

## Naming & Boundaries

- ESM: `.js` import suffix. **`prune()`** every listing before emit. **`pmap(items, CONCURRENCY, fn)`** for detail batches.
- **Not here:** source keys, monitor matrix, ingest mapping, listing vocabulary, default `proxy` (callers pass `stealth` for CF).
- **`scrapeJson`:** use `jsonAttempts` / `jsonBackoffMs` only for Buildout interstitials; keep default 3× scrape retry elsewhere.

## Integration & Gotchas

- Hanley enrichment reuses the source's complete validated public inventory and
  mapper, selects exact claimed native IDs and canonical URLs, and preserves
  existing child collections. Ordinary enrichment records the actual feed
  observation; it does not certify a full-source strict generation. Worker and
  direct-ingest admission refuse generic/thin Hanley enrichment payloads.

- `pmap()` yields to the event loop after each completed item so immediately
  resolved cache work cannot starve timers or network I/O. It keeps ordered
  results and the configured concurrency, stops dispatch after the first
  rejection, and drains all started workers (including a pending yield) before
  rejecting. This is scheduling fairness, not a provider pacing or timeout rule.

- One `broker.ts` table per `collect.ts` run across all sources.
- When filtering basemap noise, `harvest.ts` should reject only the observed
  modern Google Maps resource on `mapsresources-pa.googleapis.com` at
  `/v1/tiles`; do not exclude the host wholesale. Preserve genuine gallery
  imagery and other useful media. Parser fixtures do not authorize deployment
  or cleanup: filtering may affect persisted-child retention, so require
  source-backed evidence for each changed image role or excluded URL and the
  canonical artifact-to-ingest-to-database readback. A valid source may have no
  identifiable property photo; do not invent a primary image or infer source
  absence from missing role evidence.
- Direct-provider metrics must bind source, transaction and operation role;
  never combine adapters that share a role name. Complete an attempt after its
  bounded body read or before retry backoff. Keep existing acquisition/error
  behavior and historical coverage labels; telemetry never proves freshness or
  listing completeness. Use the existing recorder rather than a second logger.
- Importing `config.ts` parses argv; tests needing scrape/util alone should avoid it.
- `num()` drops zero; `parseJsonBody` handles Chrome JSON viewer and bad quotes.
- Use a scoped module folder when a shared primitive needs its own contract,
  preservation rules and focused tests. Keep source routing in `sources/` and
  read only the module guidance relevant to the change.

## References

- `../sources/CLAUDE.md` (adapters, monitor, `external_id`)
- `../CLAUDE.md` (orchestration, ingest, daily ops)
- `../../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`

## Own observation timestamps

Use `observationNow()` from `observation-clock.ts` for NEW collector-owned observation and run-envelope timestamps. One process UTC anchor plus monotonic elapsed time keeps these stamps ordered through small wall-clock adjustments. Do not replace provider modification dates, explicit passed timestamps, or cached historical observations; keep `Date.now()` pacing, TTL and duration behavior unchanged.

Compare same-process observation chronology against `observationNow()` start/end
barriers, not raw wall-clock samples. Independent wall-clock future-time checks
remain unchanged.

The shared clock refuses invalid/regressing monotonic samples or wall drift beyond the existing five-minute outer bound with a bounded numeric error. Small drift emits no diagnostic. This is not cross-process synchronization: existing generation/attempt chronology and stricter source future-time checks still apply, and may refuse earlier. Never reanchor, clamp to generation start or restamp old evidence to make validation pass.
