# SVN Scraper Notes

Production bulk collection uses SVN's Buildout plugin inventory feed in `cre_collector/collect.ts`.

## Buildout Feed

- Endpoint pattern: `https://buildout.com/plugins/<plugin-key>/inventory.json?page=<n>`
- SVN plugin key: `b933480474026c41d248b77156c84aef37dcac68`
- Collector source key: `svn`
- Public listing page recorded by the collector: `https://svn.com/properties/`
- The feed returns `meta.total`, `meta.limit`, and `inventory`.
- Rows are partitioned client-side: `sale=true` for sale, otherwise lease availability.

## Verification Evidence

Latest complete artifact reviewed: `cre_collector/out/full_latest_2026-06-11_230423.json`.

- Finished at `2026-06-12T04:31:24.562Z`.
- SVN source rows: 5,521 total, 2,988 sale and 2,533 lease.
- Detail coverage in that artifact: 5,521 listing URLs, 5,521 listings with broker refs, 637 unique SVN brokers, 4,058 listings with PDF brochure URLs, 5,521 listings with image URLs, 5,481 listings with lat/lon, 2,958 sale rows with price text, and 2,533 lease rows with lease-rate text.
- Dual-mode coverage: 646 rows surfaced as `Sale/Lease`.

Live probe on 2026-06-12:

```bash
FIRECRAWL_API_URL=http://localhost:3002 npx tsx collect.ts \
  --source=svn --transaction=both --max-items=6 --page-cap=10 --concurrency=2 \
  --out=out/svn_probe_2026-06-12_040223.json
```

Result: no JSON artifact was written. Log: `cre_collector/out/svn_probe_2026-06-12_040223.log`.
Buildout returned non-JSON 403-style HTML on repeated inventory pages; the collector failed closed at 7 failed pages out of 185 and reused the cached failure for the lease pass.

Follow-up sequential probe:

```bash
FIRECRAWL_API_URL=http://localhost:3002 npx tsx collect.ts \
  --source=svn --transaction=both --max-items=6 --page-cap=10 --concurrency=1 \
  --out=out/svn_probe_seq_2026-06-12_040714.json
```

Result: no JSON artifact was written. Log: `cre_collector/out/svn_probe_seq_2026-06-12_040714.log`.
Sequential paging still failed closed at 6 failed pages out of 185.

## Rate Limits

Buildout feeds can serve HTML interstitials under sustained paging. The production collector retries individual pages and aborts the source if too many pages fail.

Current recommendation: treat SVN as mapping-complete from the latest full artifact, but partial for live refresh verification until a fresh both-transaction probe writes a valid artifact again. Do not use `--mark-missing` from an SVN run that reports Buildout page failures.
