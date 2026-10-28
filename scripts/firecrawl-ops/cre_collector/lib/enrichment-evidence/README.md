Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Enrichment evidence

The optional enrichment evidence channel preserves each claim separately,
including repeated URLs and conflicting transaction assertions. The client
capture clock is observed after `scrapeDoc` returns; provider freshness, cache
state, listing identity and visibility remain unverified. Scrape failures carry
only bounded failure metadata, with no invented document or provider clock.
The page-context `detailObservedAt` and table timestamps, when present, carry
that same collector receive clock, never a provider modification timestamp or
evidence of a fresh fetch. `pageContextClockOrigin` records this explicitly.

Buildout detail enrichment uses the same returned iframe scrape for its existing
listing mapping and a separate bounded observation. The claim URL remains a
claim witness; `requestedPageUrl` and page-context source URL identify the
derived iframe request and do not assert canonical identity. The observation
labels whether the existing detail guard accepted the response or rejected a
missing-detail shell; a rejected response never produces a canonical row or
proves listing absence. Existing shell and detail URL guards remain in force.
Any returned provider detail observation is
separate from the collector receive clock. Credential-bearing material and
source-specific withholding remain subject to existing private admission
checks; this local channel does not itself authorize database retention.

The shared page-context helper retains bounded source-native scripts, metadata,
links, images and table observations. HTML above 2,000,000 UTF-8 bytes is not
parsed for context. Each supplied JSON-serializable `ScrapedDoc` field is kept
exactly as its JSON representation up to 256 KiB, with explicit omission
reasons for larger or non-JSON fields. Nonfinite numbers, negative zero,
undefined values, functions, symbols, sparse arrays, nonplain objects and
other values JSON would transform or omit are not silently converted. A single observation is capped
at 2,500,000 bytes; if larger, its context and fields are omitted explicitly.
Groups cap observations at 10,000 and 8 MiB, retaining the bounded prefix and
marking `evidenceIncomplete` with an `evidenceError` on the source result.
This limit does not discard independently mapped listing rows. Generic JSON-LD
mapping and URL completion behavior remain separate. This is collector-supplied
content, not raw HTTP bytes or proof of complete extraction or publisher access.
Existing content and credential admission remains mandatory.

Local verification and private artifacts do not establish deployment or
production database retention; see PR #363 for the current verification scope.

Targeted enrichment artifacts remain private local custody. The enrichment
worker does not automatically append this observation channel to the evidence
database. Observations alone never complete a canonical queue claim; absent
canonical output keeps the worker's existing retry and dead-letter accounting.

## Existing storage planning path

For an existing single-source enrichment artifact, the ordinary private evidence
CLI can already convert and plan its listing rows and retained observations:

```sh
python3 -m source_evidence.cli convert-collector --artifact /private/enriched-svn.json --source-key svn --out /private/svn-observation.json
python3 -m source_evidence.cli plan --artifact /private/svn-observation.json --source-key svn --out /private/svn-plan.json
```

Run from the collector directory.
These commands are offline planning, not a database append. Source, credential,
withholding and size refusals still apply; a successful conversion is not an
admission receipt. Mixed-source artifacts intentionally refuse this path. A
source-filtered queue run can produce a single-source artifact; never relabel or
drop other source groups to make a mixed artifact pass.

The opt-in parent lane in `source_evidence/guarded_evidence.py` supplies
foreground settlement, policy binding and durable exact-receipt append/replay.
The worker itself does not append. Read the source-evidence guide before
changing those boundaries; do not add an untracked append to `cre_enrich.py`,
a second queue or a background writer. AGENTIC-5040 still gates production
credentials and activation.
