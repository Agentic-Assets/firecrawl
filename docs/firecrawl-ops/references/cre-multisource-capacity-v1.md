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
P2 is the separately admitted two-worker test with provider-family exclusions.

Prevalidation samples eight receipts per source for calibration. A source joins
the core only with at least 16 current detail-eligible receipts, selecting 24
or its full eligible census when smaller. It retains per-source private raw
receipt path/hash, final URL, HTTP result, content type, redacted headers,
timing, normalized/parser/config hashes, structured and asset fidelity, and
classification. Each receipt's enumeration hash is independently bound to its
source, provider ID, and canonical target, and the selected core emits a cohort
hash over those identity and receipt bindings. The core reports equal-source
weighted primary results and workload-weighted secondary results so a large
source cannot mask others.

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
