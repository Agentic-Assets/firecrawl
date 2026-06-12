# Lee & Associates Scraper Notes

Production bulk collection uses the Buildout inventory path in
`../../../../cre_collector/collect.ts` under source key `lee-associates`.

## 2026-06-12 Deep Dive Notes

Status: blocked for production completion until a sustained full run proves
every Buildout page. The page failures appear transient rather than permanently
bad pages.

Evidence from bounded probes:

- Prior failure log:
  `scripts/firecrawl-ops/cre_collector/out/lee_latest_2026-06-12_004010.log`
  showed pages 286 through 297 returning non-JSON after 3 attempts.
- Fresh direct GET probe parsed 30 of 30 tested pages, including old failure
  windows 93 through 104 and 286 through 297.
- Fresh local Firecrawl serial probe parsed 24 of 24 tested pages.
- Fresh local Firecrawl window probe parsed pages 260 through 297 at
  concurrency 3, 38 of 38 pages.
- Edge page 332 parsed by direct GET and Firecrawl, with `total=9971`,
  `limit=30`, and `inventoryCount=11`.

Artifacts saved by the probe:

```text
/tmp/lee-buildout-probes-20260612/direct-pages-0-105-285-298.json
/tmp/lee-buildout-probes-20260612/firecrawl-serial-pages-93-104-286-297.json
/tmp/lee-buildout-probes-20260612/firecrawl-window-pages-260-297-concurrency3.json
/tmp/lee-buildout-probes-20260612/page-332-edge-check.json
```

Patch plan:

1. Add a Lee-safe Buildout paging mode that uses direct JSON GET with a browser
   user agent first, then falls back to local Firecrawl only if direct GET
   fails.
2. Add a resumable page cache under gitignored
   `out/cache/buildout/<pluginKey>/page-N.json`, with atomic writes only after
   successful JSON parse.
3. Fetch missing pages in bounded windows, for example 40 pages at concurrency
   2 or 3, with cooldowns between windows.
4. Validate complete page coverage from page 0 through
   `ceil(total / limit) - 1`; abort if any page remains missing.
5. Never cache interstitials or parse failures.
6. Add diagnostic env controls such as `BUILDOUT_PAGE_START` and
   `BUILDOUT_PAGE_END` for future probes.
7. Verify with typecheck, a bounded Lee window, a full Lee JSON artifact, and
   `cre_ingest.py --dry-run`. Do not live ingest Lee until the full artifact is
   clean.

## 2026-06-12 Codex Lee Buildout Probe

Status: bounded Lee probes are now safer, but Lee is not production-complete
until a full Lee-only run proves complete page coverage and passes dry-run
ingest. Do not live ingest Lee and do not use `--mark-missing` from these
bounded probes.

Commands run:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector

npx tsx collect.ts --source=lee-associates --transaction=both --max-items=8 --concurrency=1 --out=/tmp/lee_before_probe.json

npm run typecheck

npx tsx collect.ts --source=lee-associates --transaction=both --max-items=20 --concurrency=1 --out=/tmp/lee_after_probe.json

python3 cre_ingest.py --in /tmp/lee_after_probe.json --dry-run --keep-artifacts /tmp/lee_after_ingest_check
```

Before patch result:

- Wrote `/tmp/lee_before_probe.json`.
- Collected 16 listings, 8 sale and 8 lease, with 7 unique brokers.
- Buildout reported `total=9971`, `limit=30`, 333 pages.
- Firecrawl skipped pages 231, 232, 255, and 256 after three non-JSON
  attempts each, then cached 9,851 of 9,971 inventory rows. This was not safe
  enough for production because the artifact could be gappy while reporting
  source success.

Direct GET evidence:

- `/tmp/lee_direct_buildout_probe_20260612.json`: initial direct Node GET to
  pages 0, 231, 232, 255, 256, and edge page 332 returned `403 text/html`
  with a small HTML forbidden body, not JSON.
- `/tmp/lee_after_failure_direct_http_shape_20260612.json`: immediately after
  one failed collector pass, direct GET to pages 313, 314, and 332 returned
  `200 application/json`, keys `inventory,meta`, `total=9971`, `limit=30`;
  page 332 had `inventoryCount=11`.

Patch applied in `collect.ts` Buildout helper:

- Adds optional direct JSON fetch with browser headers and source referer.
- Falls back to Firecrawl when direct GET returns HTML or other non-JSON.
- Tracks failed page numbers rather than only a count.
- Adds a bounded recovery pass that can retry failed or skipped Lee pages after
  a cooldown.
- Requires complete Lee page coverage before caching or writing a successful
  Lee artifact.
- Leaves the Buildout behavior option-driven so the Lee recovery bound is not
  applied broadly to other brokers.

After patch verification:

- `npm run typecheck` passed.
- First `--max-items=20` after-probe failed closed, with no
  `/tmp/lee_after_probe.json` artifact, after page 313 pushed the failed set to
  29 of 333 pages. This verified that Lee no longer writes a gappy success
  artifact when page coverage is incomplete.
- Final `--max-items=20` after-probe wrote `/tmp/lee_after_probe.json`.
  It collected 40 listings, 20 sale and 20 lease, with 24 unique brokers.
  Pages 230, 231, and 232 failed through direct GET plus Firecrawl fallback,
  then recovered in the bounded recovery pass.
- Final inventory cache was complete: 9,971 items cached against source
  `total=9971`.
- Dry-run ingest staged 40 Lee rows, skipped 0 rows for missing URL, and wrote
  `/tmp/lee_after_ingest_check/ingest.sql`.

Recommendation:

- The Buildout transient failure can be addressed safely for Lee bounded probes
  with direct-first fetch, Firecrawl fallback, bounded recovery, and strict
  complete-page validation.
- A full Lee-only collector run is now a reasonable next test after the current
  Cushman workload is finished. Use `--concurrency=1`, do not live ingest from
  the first full Lee artifact, and do not use `--mark-missing` until a full
  clean Lee artifact plus `cre_ingest.py --dry-run` pass.
