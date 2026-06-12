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

Latest complete artifact reviewed:
`cre_collector/out/svn_full_cache_2026-06-12_assembled.json`.

- Assembled on 2026-06-12 from durable Buildout cache pages 0 through 184.
- Raw SVN source rows: 5,526 total, 2,989 sale-bucket rows and 2,537
  lease-bucket rows.
- Staged rows after ingestor canonicalization: 5,287 unique listings.
- Live active Supabase rows after source-scoped reconciliation: 5,287 total,
  2,660 sale, 2,192 lease, and 435 sale_or_lease.
- Artifact coverage: 5,526 listing URLs, 5,526 listings with broker/contact
  refs, 636 unique SVN brokers, 4,071 listings with PDF/document URLs, and
  5,526 listings with image URLs.
- Live child rows after ingest: 5,235 image URL rows, 3,899 document URL rows,
  and 5,287 contact rows.
- Quality proof: 0 missing URLs, 0 missing titles, 0 missing raw data, 0
  duplicate external IDs, 0 bad asset URLs, and 0 orphan contacts, documents,
  or images. One active SVN row is missing state.

The earlier live probes below are retained as failure-mode evidence. They led
to the durable Buildout cache/window strategy now used for SVN.

Live probe on 2026-06-12 before cache-window recovery:

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

Current recommendation: use durable cache-window fills and assemble from cache
only after all pages are present. Do not use `--mark-missing` from an SVN run
that reports Buildout page failures or an incomplete cache. The 2026-06-12
assembled cache artifact passed dry-run ingest, live ingest, and source-scoped
Supabase validation.
