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
