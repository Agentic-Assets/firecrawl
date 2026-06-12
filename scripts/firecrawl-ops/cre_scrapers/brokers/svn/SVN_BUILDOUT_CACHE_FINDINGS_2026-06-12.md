# SVN Buildout Cache Findings - 2026-06-12

Scope: SVN only, source key `svn`.

## Finding

SVN can use the Lee-style durable Buildout page cache and window controls.
The Buildout feed accepted a small direct public JSON cache fill for pages 0
through 2, and cache-only mode correctly refused to write a partial listing
artifact.

The collector now opts SVN into the same durable cache path as Lee:

- `cacheSlug: "svn"`
- `usePageCache: true`
- `maxRecoveryPages: 60`

No Supabase ingest was run. No PDFs or image binaries were downloaded. The
probe only touched public Buildout inventory JSON.

## Commands Run

From `scripts/firecrawl-ops/cre_collector`:

```bash
BUILDOUT_CACHE_ONLY=1 BUILDOUT_PAGE_START=0 BUILDOUT_PAGE_END=2 \
  BUILDOUT_PAGE_JITTER_MS=250,750 FIRECRAWL_API_URL=http://localhost:3002 \
  npx tsx collect.ts --source=svn --transaction=sale --max-items=0 \
  --concurrency=1 --out=/tmp/svn_cache_window_probe_should_not_write.json \
  2>&1 | tee /tmp/svn_cache_window_probe_2026-06-12.log
```

Result:

- Direct Buildout JSON was available.
- Cache-only window reported `0 selected page(s) missing`.
- No listing artifact was written, as expected.
- Cached pages: `out/cache/buildout/svn/page-0000.json` through
  `page-0002.json`.
- Page metadata: `total=5526`, `limit=30`, `rows=30` for each cached page.

Assemble-from-cache fail-closed check:

```bash
BUILDOUT_ASSEMBLE_FROM_CACHE=1 FIRECRAWL_API_URL=http://localhost:3002 \
  npx tsx collect.ts --source=svn --transaction=sale --max-items=6 \
  --concurrency=1 --out=/tmp/svn_partial_cache_assemble_should_fail.json \
  2>&1 | tee /tmp/svn_partial_cache_assemble_2026-06-12.log
```

Result:

- The collector refused to assemble from an incomplete cache.
- Missing cache pages began at page 3.
- No listing artifact was written.

Post-patch SVN cache window check:

```bash
BUILDOUT_CACHE_ONLY=1 BUILDOUT_PAGE_START=3 BUILDOUT_PAGE_END=4 \
  BUILDOUT_PAGE_JITTER_MS=250,750 FIRECRAWL_API_URL=http://localhost:3002 \
  npx tsx collect.ts --source=svn --transaction=sale --max-items=0 \
  --concurrency=1 --out=/tmp/svn_cache_window_3_4_should_not_write.json
```

Result:

- Cache-only window reported `0 selected page(s) missing`.
- Command exited nonzero by design because no partial listing artifact is
  allowed.
- SVN cache now has pages `page-0000.json` through `page-0004.json`.
- Each cached page reports `total=5526`, `limit=30`, and `rows=30`.

Validation:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
```

Result:

- Local Firecrawl healthcheck passed.
- `npm run typecheck` passed.

## Next Safe SVN Run Pattern

Fill the cache in small windows first:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
BUILDOUT_CACHE_ONLY=1 BUILDOUT_PAGE_START=0 BUILDOUT_PAGE_END=24 \
  BUILDOUT_PAGE_JITTER_MS=250,1000 FIRECRAWL_API_URL=http://localhost:3002 \
  npx tsx collect.ts --source=svn --transaction=sale --max-items=0 \
  --concurrency=1 --out=/tmp/svn_cache_window_should_not_write.json
```

Repeat with non-overlapping windows until pages 0 through 184 are present,
then assemble only from cache:

```bash
BUILDOUT_ASSEMBLE_FROM_CACHE=1 FIRECRAWL_API_URL=http://localhost:3002 \
  npx tsx collect.ts --source=svn --transaction=both --max-items=0 \
  --concurrency=1 --out=out/svn_full_cache_2026-06-12_assembled.json
```

Only consider live ingest or source-scoped reconciliation after a clean
assembled artifact and dry-run ingest pass.
