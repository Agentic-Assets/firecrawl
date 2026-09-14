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
every row. The enumeration receipt records its UTC observation time, total
population, completion/non-truncation proof, provider IDs, and body digest;
each row is bound to that receipt, the source-contract digest, and the cohort
configuration digest. The review output exposes only safe IDs, strata, hashes,
and aggregates: never restricted paths, raw URLs, or headers.

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
