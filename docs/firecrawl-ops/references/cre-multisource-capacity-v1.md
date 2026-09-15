# CRE multisource capacity experiment v1

`cre_capacity_multisource_v1.py` is an offline-only cohort-admission boundary.
It does not call a source adapter, make an HTTP request, change runtime limits,
write a database or cache, change status, schedule work, or configure a model
or OCR path. Its output is a review artifact, not authority to run the study.

The fixed source matrix is intentionally versioned rather than inferred from
the collector registry. Strict-detail plane: JLL, JLL Investor, Colliers ST,
Colliers Main, Marcus & Millichap, Avison Young, Savills, NAI Global,
Transwestern, Matthews, Foundry, and DAUM. Authoritative-inventory plane:
CBRE, CBRE Dealflow, Cushman & Wakefield, Newmark, SVN, Lee & Associates, SRS,
and Bull Realty. Colliers Main is always serial and exclusive. P0 is 2 CPU,
4 global pages, and one source worker; P1 is 6 CPU, 10 pages, and one worker;
P2 is the separately admitted two-worker test with JLL/JLL Investor,
CBRE/Dealflow, Buildout-sibling, and Colliers-family exclusions. The versioned
matrix verifies adapter host contracts, including `invest.jll.com`,
`www.cbredealflow.com`, and Colliers ST's `sales.colliers.com` plus
`my.rcm1.com`; this lane does not infer generic provider hosts.

Prevalidation samples eight receipts per source for calibration. A source joins
the core only with at least 16 current detail-eligible receipts, selecting 24
or its full eligible census when smaller. Selection is deterministic, seeded,
and round-robins the declared transaction class, property type, and page-weight
strata. It rehashes the private 0700-root, 0600-file enumeration receipt, raw
receipt, normalized artifact, field-locator artifact, and asset evidence for
every row. Each row is bound to its enumeration receipt, the source-contract
digest, and the cohort configuration digest. The review output exposes only safe
IDs, strata, hashes, and aggregates: never restricted paths, raw URLs, or
headers.

## JLL aggregate-enumeration receipt schema

JLL is the only source with a native population verifier in v1. Its private
aggregate must be exactly `jll_graphql_enumeration_aggregate_v1`, with
`observed_at`, `total`, `complete`, `truncated`, `provider_ids`, and a nonempty
`page_receipts` and `resolution_receipts` list. Every manifest is exactly a
private `path` plus its SHA-256. The aggregate is itself rehashed, and the row's
`enumeration_body_sha256` is the canonical digest of that sealed page-manifest
list, not an unverified synthetic response body.

Each referenced page must be exactly `jll_graphql_page_receipt_v1` and retain
the origin-bound `/api/graphql` request/final URL, HTTP 200 JSON transport,
fresh UTC observation time, positive timing, `SearchResults` operation, raw
GraphQL request body, request variables, and raw JSON response. The request
body must match the pinned current adapter query SHA-256, operation, and
variables. Variables must retain the public JLL `us`/`en` market and language,
one sale-or-rent tenure across the whole aggregate, every exact current adapter
property-type filter (`office`, `industrial`, `retail`, `land`, `medical`,
`multifamily`, `lab`, `coworking`, and `data-center`), `take: 50`, a zero-based
`skip` divisible by 50, and the pinned `dateModified desc` ordering. For every
property-type/tenure filter, page counts must agree, skips must be the complete
sequence without gaps or repeats, and each page must contain exactly its
expected number of unique cards. The aggregate observation timestamp must fall
inside the native page timestamp minimum/maximum bound. A former single
synthetic response cannot satisfy this schema.

Search-card `id` values are not cohort provider IDs. Every unique search ID and
canonical `pageUrl` must have one sealed `jll_detail_resolution_receipt_v1`.
That receipt rehashes a successfully fetched private detail-page artifact and
binds the search identity and URL to `__NEXT_DATA__.props.pageProps.property`.
Only that numeric detail `property.id` may appear in aggregate `provider_ids`
or the row wrapper. Search IDs, canonical URLs, and detail IDs must each form a
bijection: duplicate targets or detail IDs, changed detail URL, or an unresolved
card fail closed. `produce_jll_enumeration_artifacts` is the offline producer
for already captured private page/detail artifacts; it writes the aggregate and
resolution receipts but makes no network call.

All other sources remain screening-only until they receive reviewed native
enumeration verifiers. Their asserted wrapper total never populates a page band,
core target, ready state, or workload-weighted metric.

Readiness requires all twenty fixed sources and both plane floors (12
strict-detail and 8 authoritative-inventory); anything less is explicitly
`incomplete_screen`, not a partial experiment. Strict-detail and inventory
metrics remain separate. Equal-source is the primary estimand; the secondary
workload-weighted estimate uses fresh source population totals, precommitted
0.95 winsorization, and the same per-source qualified-throughput estimand, so
sample size cannot become a covert weight.

The only currently registered attrition classifier is JLL's exact
`__NEXT_DATA__` HTTP 404, a fresh provider identity/canonical target on the
expected host, no property object, and explicit not-found payload. Every other
source remains unsupported for attrition until its own classifier is added and
tested. A 404 or 410 alone never means attrition, and no experiment state may
infer a database inactive state. Challenge/throttle stops its provider family,
not unrelated sources. Primary measurement has zero retries.

The intended reviewed execution order is calibration, then three screening-only
epochs, then four paired epochs in B-C, C-B, C-B, B-C order using the immutable
cohort. A run must retain source-level latency, throughput, HTTP outcomes,
attrition, throttling/challenge/retry, fidelity, asset, and raw-receipt reports.
No generic multisource executor exists yet; a provider adapter is not admitted
to this experiment merely because it appears in the matrix.

```bash
python3 cre_capacity_multisource_v1.py \
  --receipts /restricted/multisource-v1-receipts.json
```
