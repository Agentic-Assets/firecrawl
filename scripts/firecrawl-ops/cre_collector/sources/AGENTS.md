READ ~/AGENTS.md FIRST (skip if missing).

# sources/ Module

## Most Critical Rule

**Every adapter exports `src*(tx, max, monitor): Promise<SourceResult>`** and maps broker-site data into the shared listing vocabulary in `types.ts`. Register the source key in `collect.ts` (`SOURCE_KEYS` + `runSource`); map it in `cre_ingest.py` `SOURCE_TO_BROKERAGE` and `../sql/001_cre_brokerages.sql`. **Never ingest a `--monitor` artifact through `cre_ingest.py`** (sparse rows wipe enriched prices, `raw_data`, and child tables).

For comprehensive public-field capture, supported normalization, retained
source metadata and evidence of missingness, follow the
[listing fidelity standard](../../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md#comprehensive-listing-fidelity).
An empty consumer field is not proof of source absence.

Transwestern's contact, document and photo extractors share the enrichment
call's listing-local Cheerio instance and must remain read-only. Preserve the
original document's links and harvest inputs; do not share a DOM across listings.

Transwestern building-field observations are scoped to the unique Details tab
at the witnessed property description hierarchy and its positional panel within
the accepted property's detail section. Explicitly concealed ancestors outside
the intentionally switchable panel reject promotion. These observations
retain source literals and ambiguity, and bind the detail URL, body hash and
observation clock. Computed visibility is not asserted: the Details panel may
be intentionally hidden until its tab is selected. Elevator zero is valid;
floor-size text keeps its source units and does not acquire inferred units.
The observations do not establish a new identity, feed admission or production
write authority. Existing feed-to-detail identity and freshness gates remain.
`observationCount` retains the number of observed target rows, including zero,
when outer collector pruning omits an empty `observations` array.

Transwestern Description uses the same unique property hierarchy and positional
tab association as Details. The selected tab may be intentionally hidden;
explicitly concealed ancestors reject it and concealed descendants are omitted.
When that widget is present but ambiguous or lacks Description, do not borrow
global overview or Markdown text, including when its identifying wrapper is
missing but its tab structure remains. Legacy layouts without the widget retain
their existing fallbacks and boilerplate filter. Description is source text
rendered as Markdown with links resolved against the accepted detail URL; it
does not establish computed visibility or completeness.

Photo order is part of fidelity: canonical ingest uses `photos[0]` as the
primary image. For Transwestern, prefer the canonical feed `PropertyImage`
only when it survives the shared image filter; otherwise consider only the
first `.photos-list a.chocolat-image[href]` from that property's detail DOM,
again requiring a retained URL. Move the witnessed image first while preserving
every other retained image's relative order. Record the selected URL and
`provider_feed` or `property_gallery` basis in
`transwesternPrimaryPhotoEvidence`. With no retained witness, leave order alone
and omit that evidence. A generic scraped image, avatar, filename or unrelated
gallery cannot establish a property-primary role. This source contract does
not grant another adapter the same selector or authorize persisted-image loss.

Transwestern's final photo-role filter lives in `pure/transwestern-images.ts`.
It excludes only the four observed owned template resources (`tw_gl.png`,
`connect-image.png`, `transwestern-mapmarker.png`, `mail-3.png`) at their exact
asset paths, and exact avatars from contacts with a parsed identity signal.
A retained feed or scoped property-gallery reference wins over either role.
Unknown URLs remain in their existing order; filenames or CDN folders alone do
not identify a portrait. Apply the filter after all harvest inputs, including
HTML-only candidates when `doc.images` is empty, so exclusions are recorded.
Successful details carry `transwesternPhotoRoleVersion: "v1"`; actual removals
also carry `transwesternExcludedPhotoEvidence` with schema
`transwestern-photo-role-v1` and per-URL role/basis entries. Keep avatars with
their contacts, and preserve documents, Markdown and other useful detail fields.
An empty photo array may be omitted by `prune`; neither missing primary evidence
nor an empty array proves that the publisher has no property imagery elsewhere.
These source observations do not waive persisted-child or primary-role checks.

Transwestern document/media role observations are additive location evidence
bound to the accepted detail URL, body digest and observation time. A unique
property attachment table or banner download and a unique footer provide
separate occurrence bases; hidden or ambiguous structures and harvested URLs
without a matching DOM occurrence remain unknown. A URL can have multiple
occurrences and roles. Existing link-channel URLs enter only when an exact source
download-affordance anchor matches them; this does not assert document type.
Counters make truncation and empty observations explicit.
These observations do not establish computed visibility, document content,
publisher permission or property identity, and do not filter child rows or
change document acquisition policy. Successful refreshes replace current
`raw_data`; this is not an observation history.

The Transwestern image-transition path is source-specific. It requires the exact
strict-fresh full-source artifact, generation/start, clean collector code SHA,
database target and ingest-attempt binding. In the same canonical ingest
transaction it snapshots prior physical image rows, preserves each protected
URL/multiplicity and offstage state, and permits removal only for exact
source-proved exclusions or the narrowly classified map-tile/template noise.
Primary choice uses the retained current feed/gallery witness when available;
otherwise it keeps the prior primary only if that exact URL remains staged,
and leaves primary unassigned when neither is supported. The post-`COMMIT`
receipt carries raw counts, conservation totals, primary-role totals and a full
image-state digest; normal raw-report comparison and full digest readback are
both required. This is not a Transwestern inventory/lifecycle waiver and does
not apply to another source. Code merge and isolated QA do not establish
runtime promotion or fresh full-source production acceptance; inspect the
current evidence on [AGENTIC-4576](https://linear.app/agenticassets/issue/AGENTIC-4576).

## Fix shared failure families

Before treating a failure as unique to one brokerage, identify its provider,
adapter and shared helpers, then search existing fixes, tests and the
[AGENTIC-2895 learning log](https://linear.app/agenticassets/issue/AGENTIC-2895).
Check other brokerages using the affected path. Fix genuinely shared behavior
once and add parameterized regressions for representative sources and variations,
rather than copying the fix into separate adapters. Examples include equivalent
owned-host forms, pagination, response shapes, retries and duplicate inventory
downloads; apply normalization only where the source contract permits it.

Shared code does not transfer another brokerage's identity, tenure, admission,
freshness or lifecycle authority. Retain each source's raw provenance and
source-specific checks. A sibling is not verified merely because it uses the
fixed helper: obtain its required fresh acceptance evidence. Follow the
[throughput workflow](../../../../docs/firecrawl-ops/references/cre-performance-telemetry.md#standing-parallel-work-and-throughput-instructions)
for parallel diagnosis and testing without repeated full-batch runs.

## Adapter Contract

- Return `{ company, sourceUrl, method, totalAvailable, listings, note?, truncated? }`.
- Listings: `id`, `name`, `transactionType`, address/geo, prices, `brokerIds` (`brokerRef()`), `brochures`, `photos`, `url`, `lastUpdated`; use `prune()` before emit.
- **`truncated?: boolean`**: set when this pass collected less than the feed's
  reported total (or hit a hard cap). `cre_monitor.py` treats `truncated` like
  `error` for disappearance gating. Adapters that set it include `newmark` (NIM
  output cap below reconciled US inventory), `cbre` and `cushman-wakefield` (collected <
  `min(max, reported total)`), `nai-global` (`--page-cap` clip with a full last
  page), and `colliers-main` (deferred or errored detail renders).
  The retained legacy Newmark Algolia adapter separately reports an unsplit hit cap.
- **`monitor=true`**: cheap enumeration only. Skip detail unless the feed is already complete.
- Folded sub-sources get ingest prefixes (`dealflow:`, `investor:`, `main:`), not adapter prefixes. `colliers-main` emits bare `usa#####`; ingest adds `main:`.

For provider or page variations, use the [brokerage completion playbook](../../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md)
for evidence and fail-closed rules. Keep the source-specific canonical
external-ID recipe, and use one source-owned comparison normalizer in mapping
and verification. Preserve raw source IDs and URLs in provenance; add only
primary-evidenced aliases or layout fallbacks with regression tests. Do not
infer a provider change from a parser gap.

Essex listing keys preserve the validated owned property's literal URL pathname
without outer slashes, decoding or lowercasing, matching the historical
properties/<slug> namespace. The observed currentPropertyId
remains raw essexPropertyId metadata: distinct real pages can share it, so it is
not a unique page identity. New essexWordpressPage provenance explicitly identifies
one positive postid-* on the canonical single-properties page. Version-2
receipts bind that typed page ID separately from the raw variable; old evidence
is not reinterpreted. Duplicate page IDs or output paths fail closed; a changed
path never authorizes a rekey. Canonical detail and lifecycle checks remain
required. Missing historical typed page IDs remain unknown; path/URL
contradictions still refuse. This rule does not admit first-seen writes or
authorize closed/missing lifecycle updates.
Current Essex inventory includes on-market and under-contract properties only;
the closed archive reconciles coverage but never inflates current counts. With
no baseline the raw gate reports `first_seen` before the default 100-row floor;
Essex's explicit source-bound current-inventory admission requires two matching
complete current/closed/sitemap reconciliations and every fresh current detail.
Closed cards bind coverage URLs/lifecycle only and never enter current output.
Only successful full, uncapped, non-monitor proof can supply the exact same
generation/start's empty lease receipt. Raw `first_seen` and baseline absence
remain; sub100 current cohorts may qualify without lowering any global floor or
waiving baseline-backed holds. Prewrite checks refuse contradictory literal
URL/path/known native-ID associations across all historical parents. These
implemented checks do not establish fresh production acceptance or authorize
closed/missing lifecycle changes.

Lyon-Stahl property sitemaps may include the exact owned listing archive
`https://lyonstahl.com/properties/`. Exclude only that URL from detail enumeration;
every other member still requires a valid owned detail URL, and empty or
archive-only shards fail. Sitemap membership does not establish active status,
historical identity compatibility or first-seen admission.

Lyon-Stahl may publish availability on a same-page `Product` Offer instead of
the `RealEstateListing`. When listing offers are absent, require one listing and
one Product bound to the exact requested canonical URL, Product `@id` equal to
that URL plus `#product`, and one typed Offer with that exact URL. Apply the
existing availability enum; missing, blank, unknown, ambiguous or conflicting
evidence stays held. A present listing Offer is never repaired with secondary
evidence. The retained primary fallback sample is `SoldOut`; active fallback
coverage is synthetic and does not prove fresh inventory or source admission.

Lyon detail parsing selects explicit same-property `For Sale` / `For Lease`
Offers for the requested transaction; Offer order and the other transaction's
legitimate status do not determine that result. Missing, foreign or ambiguous
requested Offers and unknown requested availability remain held. Supplied
non-string name/URL/business-function values cannot masquerade as absent
authority; no new JSON-LD transaction vocabulary is inferred. The existing
unlabelled single-Offer sale compatibility and Product identity/conflict rules
remain parser behavior, not current-market authority. Complete own Offer
objects survive source and collector pruning in `extraFacts.providerOffers`,
including literal false/null/empty values. A lease row never copies the page's
sale price into rent; unqualified Offer amounts/bases stay raw for future mapping.

The sale adapter reports detail-page acquisition failures separately from
parser identity/structure rejections. Either makes the pass truncated; the
aggregate note keeps their counts distinct. Held availability remains a
separate category, terminal detail pages remain excluded, and these diagnostic
categories do not change source admission. Lease acquisition remains
explicitly incomplete.

Lease **acquisition** is still unimplemented and explicitly incomplete; parsing
an observed lease Offer does not prove a complete lease inventory. Do not launch
a new historical sitemap crawl to manufacture that proof. Complete repeated
native/frontend reconciliation, provider pacing (the observed robots delay was
10 seconds), all current details/status/tenure and ordinary admission gates
remain [AGENTIC-4308](https://linear.app/agenticassets/issue/AGENTIC-4308) work.
Each source invocation shares one fixed 10-second request-start gate across
sitemaps, details, redirects and retries. It spaces starts without serializing
response bodies; the existing 60-second attempt timeout includes queued waits,
and an expired queued request never dispatches. This enforces the retained robots
rule, not a claim that it resolves HTTP 429 or complete source coverage.
The Lyon fetcher retries only recognized transport/timeouts and selected 429/5xx
responses without a `Retry-After` header, within its existing attempt bound.
It requires successful cancellation acknowledgement before retrying a discarded
HTTP body or following a redirect. Throwing, rejected or stalled cancellation
refuses another request; the former throw/rejection-as-success behavior was
corrected under AGENTIC-4999. Definitive HTTP, URL, content-type and size-limit
refusals remain fail-fast. Do not use retries to bypass a 403 or
managed challenge, or to imply inventory completeness.
The October 17 robots diagnostic stopped on an HTTP 403 managed challenge;
a separately bounded October 18 observation returned HTTP 200. Read the owning
issue's current decision and proof comments for the applicable access evidence.
Neither observation authorizes bypass or blind retries, and neither establishes
source, lifecycle or full51 acceptance.

Lyon's fresh REST `Closed` category can conflict with the same property's HTML
`InStock` Offer; keep that conflict held under
[AGENTIC-4308](https://linear.app/agenticassets/issue/AGENTIC-4308).
WordPress `publish` is publication state, not market status. A `closing_date`,
or its absence, is not a reliable market-status rule; standard REST metadata
does not resolve the conflict. Parser compatibility does not establish
current-inventory admission.

DAUM and Foundry output keys preserve the validated owned property's literal
pathname without outer slashes (`property/<slug>`); Lyon-Stahl preserves only
the final literal slug. Keep case and percent spelling unchanged. Positive
WordPress IDs remain separate `daumPostId`, `foundryPostId`, or `lyonStahlPostId`
provenance. Native IDs and output keys must each be unique within an emitted
source pass; changed paths never authorize aliases or rekeys. These recipes do
not establish current availability, change strict detail/lifecycle checks or
admit absent-baseline writes. Foundry's missing explicit status remains held.

Foundry emits one JSON stderr `foundry_held_observation` event (schema
`foundry-held-observation-v1`) per completed held parse, before later source
assertions. Its source-owned observation retains validated native ID/literal
key/requested URL, all parsed property notes in order (including an empty array),
unchanged nullable status/reason, requested transaction context and supplied
observation clocks only. It adds no accepted-row facts or freshness authority.
Existing checkpoint attempt logs retain stderr; standalone callers must capture
stderr. The in-progress collector change also carries these already parsed,
unqualified observations in `sources[].retainedObservations`. An all-held full
run is intended to write a normal-shaped artifact marked
`runMeta.evidenceOnly=true` and exit nonzero; mixed runs retain held context
alongside their original accepted counts. Evidence-only artifacts must be
refused by canonical ingest and monitor entrypoints. The private evidence bridge
is intended to settle the artifact before canonical gates. This behavior requires separate production acceptance and is not deployed.

The draft bounded native page context is omitted for recognized challenge
shells. Its shared table observer reports a failure with unknown omissions when
the detail clock is missing or the supplied HTML exceeds two million UTF-8
bytes; it does not retain the raw table cells in those cases. Other context has
explicit item and character limits. Existing `propertyNotes` remains outside
that budget and is not independently bounded; the legacy stderr event remains
unchanged. These are retention limits, not evidence of complete page coverage.

The draft shared `lib/source-page-context` helper owns that bounded extraction;
Foundry retains its source schema through a thin wrapper. Lyon-Stahl captures
held/rejected non-shell page context at its existing detail-fetch boundary.
Matthews captures accessible non-shell pages that fail parsing or identity
validation. If strict Matthews collection fails, its typed error also carries
the already completed parses after the shared mapper settles active workers.
Their accepted mapper, source counts, redirect/404 exclusions, status, tenure
and lifecycle gates remain authoritative for canonical output. Private captures
require no native ID or address and do not turn those failures into acceptance.
These integrations require separate production acceptance and are not deployed. Targeted enrichment paths are unchanged.

On a strict identity failure, the intended `RetainedSourceError` preserves the
original failure while carrying held context and otherwise accepted conflicting
parsed rows as unqualified observations in `sources[].retainedObservations`.
It does not rekey, merge or activate those rows. Evidence-only output is intended
to use an exclusive private mode-0600 file, refuse an existing destination, and
exit nonzero. The generic private source-evidence context bridge may retain the
observations under its existing admission rules.

The shared Python `cre_artifact_scope.require_canonical_artifact` guard is
intended to run in ingest main and its freshness guard, the monitor loader and
no-op reader, and gate counting before canonical database operations. This
marker guard is not authentication: an unmarked artifact still needs the usual
canonical gates. These changes remain unverified and not deployed. Typed access
to partial observations does not resolve identity or market status uncertainty.
They are parsed context, not raw-response archival, complete source coverage,
current-inventory proof or source admission. A process/host crash before log
flush is not covered by stderr retention.

DAUM transport preserves serial three-second physical-start spacing, two
attempts, a 30-second per-attempt timeout and the five-megabyte response cap.
Owned body reads use the shared reader's absolute monotonic deadline from that
attempt's start after pacing, including elapsed header time. Pending reads and
immediately ready zero-byte chunks cannot extend that body budget. DAUM retains
reader ownership and requires exact read-error settlement or acknowledged
cancellation within its separate one-second cleanup allowance before recovery.
This does not independently bound a header fetch that ignores its AbortSignal.
Only recognized transient failures and 429/500/502/503/504 without
`Retry-After` retry; POST replay is limited to generated empty inventory pages.
Discarded bodies must settle before a retry; uncertain cleanup poisons that
transport instance and prevents queued requests from starting.
Foundry retries only its bounded transient transport failures and HTTP 429/500/
502/503/504 responses without `Retry-After`. Definitive HTTP responses,
`Retry-After`, unsafe redirects, invalid content, size-limit failures, and
unexpected exceptions fail fast. A discarded response body must settle before
retrying or following a redirect; failed or stalled cancellation stops the
request chain. These retries do not change Foundry identity/status checks or
prove inventory completeness.

Foundry, Lyon and Matthews share the reader-only `lib/response-body.ts` byte
accumulator and absolute monotonic body deadline. Foundry and Lyon retain their
original total attempt budget across pacing, headers and redirects, declared
Content-Length preflight, and source-specific retry policy. Read-failure cleanup
requires either the exact caught read error on `reader.closed` or successful
cancellation acknowledgement within one bounded wait; ordinary closure alone
is insufficient. Matthews retains its separate 30-second body budget,
actual-stream-byte-only cap and best-effort cleanup. The byte bound is not a
process heap ceiling. This body mechanism does not independently settle a
Foundry/Lyon header implementation that ignores abort, or establish live source
acceptance.

DAUM's explicit absent-baseline additive admission requires an uncapped strict
sale+lease generation: serial selection remains singleton; the flat pipeline
may select DAUM with ordinary sources using its exact source-keyed admission.
The native singular `-AdmitCurrentInventorySource daum-commercial` forwards
this entry with `-Pipeline -Sources`; other sources cannot supply DAUM proof.
It requires two consecutive complete public POST inventory
passes with the same literal URL/tenure membership, followed by every fresh
canonical detail. Strict acquisition resolves inventory-tenure-unknown members
first in their original relative order, then the remaining members. Diagnostic
ordinals and the complete final cohort/output retain original inventory order;
in the default complete scope, unknown tenure still refuses and no partial
proof is emitted. Fully known
inventories retain their original request order. The source/aggregate verifier reconciles both transaction sets
and their unique union (at least 100); a property offered both ways counts once.
Known inventory tenure may resolve an absent detail tenure, but contradictions
or unresolved tenure refuse. Positive shortlink IDs remain separate provenance;
the pre-DML guard refuses conflicting historical URL/path/known-native claims,
including soft-deleted parents. Raw first_seen, baseline absence and additive
scope remain; this grants no missing/status/retirement authority or fresh
production acceptance.

Tenure refusals include the validated native post ID, ordinal and normalized
inventory/detail tenure with text-presence flags in the local collection log.
Use that bounded context to inspect the exact provider record; it neither
establishes that the website changed nor permits guessing missing tenure.
Strict fresh-detail parse and validation errors also retain the already validated
literal inventory URL. Raw page bodies and unparsed tenure text remain excluded;
transport, cancellation and rejection-sharing behavior is unchanged.
Within one collector process, the exact generation ID and start time share one
complete acquisition across sale and lease, including a rejected acquisition.
A second transaction cannot turn that failure into accepted proof or download
the same cohort again. A new generation/start or a fresh collector process
reacquires; the coordinator's existing bounded fresh-process retry remains.
Limited, monitor and non-fresh paths cannot borrow this strict cohort proof.
Local aggregate progress reports completed inventory-pass page/member counts,
the matched-inventory/detail transition, every 100 checked details and the final
acquisition count. Sale and lease share this progress with their cohort; held
observations count as checked, never as qualified listings or database writes.
Progress includes no listing identifiers or URLs and changes no pacing or proof.

The separate `--admit-daum-verified-subset` permission uses versioned
`daum-verified-subset-v1` proof, never the complete proof above. It still proves
two matching complete inventories and valid fresh details for every member;
only inventory-unknown AND detail-unknown tenure may become a typed hold.
The distinct `tenure-and-empty-title-v2` policy additionally holds an exactly
one-container empty visible title after canonical/native identity and tenure
conflict checks. It preserves resolved tenure and excludes that held ID from
both transaction sets. Empty-only typed observations bind to complete detail
receipts; V1/default behavior is unchanged. No title fallback or placeholder
listing is permitted. Policy joins scope, generation and start in cache identity.
Conflicts, malformed details, ownership or identity uncertainty fail the attempt.
All-member historical compatibility and emitted lifecycle preconditions run
before unchanged upserts. Held parents/children/index remain untouched; this
scope stays partial even with zero holds and grants no complete watermark,
baseline or certificate. Cache identity includes scope, generation and start.
Use the [DAUM subset contract](../../../../docs/firecrawl-ops/references/daum-verified-subset.md)
for exact counts, preservation and qualification requirements.
Subset V1/V2 proofs retain parsed cards in `heldInventoryObservations` and
parsed details in `heldDetailObservations` for exactly the held IDs in URL order.
Historical proofs may omit either audit field. These are not raw HTML or
fallback/admission sources and change no holds, listings, counts, SQL or coverage.
See the linked contract's held inventory and detail observations boundary.

## Transport & Scrape Primitives

| Transport | Sources |
|-----------|---------|
| Firecrawl `scrapeJson` / `scrapeDoc` / `scrapeRaw` | cbre, colliers-main, jll, jll-investor, cushman, transwestern, avison-young (ordinary detail path), newmark (legacy Algolia bootstrap only), buildout (fallback), savills (validated fallback only) |
| Direct public HTTP (no Firecrawl) | avison-young (strict checkpoint detail via pinned HTTPS), cbre-dealflow, colliers (RCM), newmark (active NIM search; legacy Algolia retained), marcus-millichap, nai-global, buildout (preferred path for registered sources), savills (server-rendered public list pages) |

Shared: `lib/scrape.ts` (3× scrape retry, `jsonAttempts`/`jsonBackoffMs` for interstitials), `lib/broker.ts`, `lib/html.ts`, `lib/util.ts` (`pmap`, `prune`).

Marcus direct POST and detail HTML requests share a fixed 30-second deadline
covering response headers and body. Timeouts abort the request and attempt to
cancel unread or late response bodies. A detail HTML deadline may trigger one
same-URL replacement only after the original fetch and body operation has
settled locally, including any known body-cancellation attempt. Responses
whose headers arrive after the deadline never permit a replacement, including
late HTTP denials. Settlement waits at most one additional second; an unsettled
operation keeps its error without a retry. The replacement keeps the same
30-second deadline, so detail recovery is bounded by 61 seconds. Local
settlement does not prove remote-server settlement. POSTs, HTTP refusals,
arbitrary transport errors, returned unusable/challenge pages and identity
errors do not trigger this retry. A body that times out before it can be
classified follows the bounded deadline policy above. Existing retry
telemetry counts the replacement with
zero deliberate backoff; cleanup wait is not reported as backoff.
Only successful enrichment supplies fresh provenance and enters the existing
same-generation, identity-bound cache. Persistent failures retain detailError
and still reject the strict whole-source artifact; never promote failed rows,
replay an old generation, or weaken full-detail evidence to recover a timeout.
The guarded owner's overall work deadline remains a separate bound.
Strict map inventory recognizes the native `Results: [...]` envelope as well
as the existing `Properties` array envelope. Neither shape supplies the source
total: the separate properties response must still provide a finite integer
`TotalCount` matching the unique map ActivityIds before detail expansion.
Only a valid strict total-versus-unique-ID mismatch permits one 3-second delayed
reread of both inventory POSTs. Discard the first pair; only the replacement
pair may supply details. Every raw map row must carry a nonempty string
ActivityId. Malformed input, denial, timeout or a persistent mismatch refuses;
there is no count tolerance or union of observations. Existing retry telemetry
records the actual wait.

Colliers SalesTracker (`colliers`, not `colliers-main`) may retry one failed
list/map GET pair on the same page after a 1-second delay. Only recognized native
timeouts or HTTP 500/502/503/504 without Retry-After qualify. Abort and settle the
sibling before deciding; any observed nonretryable failure wins. Keep the exact
engine key, start and page size, retain completed earlier pages, and record actual
wait telemetry. Schema, identity, count and parity failures remain fatal, as do
denials, explicit Retry-After and arbitrary transport errors. This bounded
recovery never accepts a partial inventory.

CBRE strict unlimited acquisition uses at most four cache-bypassed full
snapshots, still requiring two consecutive internally valid snapshots with the
same unique native-ID membership and total. Only aggregate cross-page duplicate
IDs or disagreement between assembled unique records and valid reported totals
permit another snapshot pass; failed passes clear the previous candidate. Page
shape/metadata, same-page duplicates, missing IDs, caps, sentinel, denial,
transport and unknown failures stop without another snapshot pass. Existing
HTTP/JSON helper retries are separate and unchanged. Keep page size 500 and
snapshot concurrency two; emit the later matching pass without combining data
from failed passes. The helper's default remains three and its explicit limit
is hard. A lone valid final pass is not enough. Source errors published in a
rejected artifact do not consume the pipeline's outer source-retry budget, so
recover known churn inside this acquisition boundary, never by admitting the
partial artifact. See [AGENTIC-4547](https://linear.app/agenticassets/issue/AGENTIC-4547).

Matthews published keyword/state filters are not proven exhaustive or disjoint
inventory partitions. A matched multi-state subject appeared in a keyword
query but was absent with the same keyword plus the IL filter. This observed
exclusion exposes a partition-completeness risk, not a proven provider defect
or cause of the broader count gap. Before relying on sharding,
test known multi-state, portfolio and hierarchical-type members and reconcile
the full native identity union; display state is not proven filter authority.
See [AGENTIC-4297](https://linear.app/agenticassets/issue/AGENTIC-4297).

Matthews retains each owned availability card's literal label/value pairs in
`extraFacts.matthewsLeasingSpaces`. Combined and individual spaces can overlap;
do not sum their areas, select a property-wide rate, or infer rate units/periods.
Only the unique owned property section's observed Spaces Available group supplies
these facts; nested sections are excluded before card or fallback-text extraction. Malformed or ambiguous cards retain their text in
`matthewsUnparsedLeasingSpaces` without claiming a structured association.
This field retention does not resolve inventory completeness or tenure authority.

Matthews failed HTTP attempts emit bounded `matthews/http` diagnostics containing
only request role, attempt/max, header/body/HTTP phase, numeric status when known,
elapsed milliseconds and the planned cooldown. Successful requests stay quiet;
URLs, headers, response bodies and exception text are never included in these
lines. The source-owned transport bounds actual decoded HTTP body bytes at
32 MiB, independently of Content-Length. Oversized bodies fail without returning
a truncated prefix and emit one terminal body diagnostic with zero cooldown.
The existing 30-second per-phase deadlines, six ordinary transient attempts,
provider pacing and their cooldowns remain unchanged. A final cooldown is labeled
before failure, not as a seventh retry: it may protect the next transaction or
outer acquisition. The byte ceiling does not prove complete source inventory.

Pyramid's WordPress inventory requests only `_embed=wp:term`: the mapper needs
those taxonomy labels for state, city, county and property type. Numeric term IDs
alone are insufficient. Keep 100-record pages, bounded response bodies and complete
ordered snapshot convergence; full mode still requires each selected detail page.
The [WordPress REST contract](https://developer.wordpress.org/rest-api/using-the-rest-api/global-parameters/#_embed)
supports selecting the term relation without broadly embedding other resources.

Pyramid canonical listing keys preserve the validated owned URL's literal pathname
without surrounding slashes. Do not decode or lowercase that path. Numeric
WordPress post IDs remain separate inventory/detail-shortlink evidence, and two
posts claiming the same canonical path fail closed. A changed path is a different
key, never an automatic rekey. Full detail mapping reads literal property-map
address text and scoped property photos; only the observed HTTPS image CDN is
allowed alongside owned images. Detail ownership stays on the Pyramid host.
No office-map/footer-logo fallback or coordinate inference is permitted.

Pyramid mapping and strict verification share exact detail transaction classification:
`Sale` maps to sale; `Lease` and `Sublease` map to lease, ignoring surrounding
whitespace and case while retaining the literal `detailFacts` value. Unknown or
ambiguous values and disagreement with inventory taxonomy fail with post ID and
bounded raw fact. Do not guess from descriptions or default unfamiliar labels
to sale; validate observed labels without loosening ownership or detail proof.

Pyramid strict first-seen proof requires full/uncapped sale+lease. A
successful immutable complete snapshot may be reused only within the exact
nonempty generation+start; monitor, capped and standalone calls do not populate
or consume it. Within that source process, sale/lease also share in-flight and
failed inventory outcomes, so a failed inventory is not walked again for the
other transaction. A new generation/start or source process may retry normally.
Partial inventory never becomes cached success. Inventory/detail worker counts
honor configured concurrency within their existing ceilings of two/four.
Receipts retain two consecutive equal full-payload passes and
each fresh detail's native ID, owned canonical URL, observation and body digest.
No missing detail or absent taxonomy label is covered by an inventory receipt.
When the fresh detail has property facts but genuinely no Sale/Lease field, only
that generation's complete inventory row may supply tenure: its published
`pbc-listings` identity/link and single record-owned embedded `sale-type` term
must agree literally (246/Sale/sale or 249/Lease/lease), bound to the second
converged page receipt. `detailTransactionFacts` records presence separately;
`transactionEvidence` identifies the HTML fact or bound WordPress term without
inventing a detail fact. Python source and aggregate admission independently
check this evidence. Present blank, unknown, duplicate/hidden or conflicting
property facts still fail. Global search filters, a numeric taxonomy ID alone,
content modification clocks and descriptions never establish tenure. This
adds no per-post inventory requests and leaves every fresh detail required.

## external_id Rules (collector id → ingest)

| Source | Stable `external_id` | Gotcha |
|--------|---------------------|--------|
| cbre | `Common.PrimaryKey` | Dual-aspect docs → `sale_or_lease` merge across passes |
| cbre-dealflow | `dealflow:` + detail `data.projectid` | Card `listingPv` ≠ projectid (~78%); monitor disabled |
| colliers (ST) | SLP `summary.ProjectId` | Index-paired card id ≠ ProjectId (~45%); **lease skipped**; monitor disabled |
| colliers-main | `main:` + `usa#####` | Monitor: sale pass only; full pass filters tx after enrich |
| jll | detail `property.id` (numeric) | URL slug ≠ id; monitor disabled |
| jll-investor | `investor:` + Salesforce `listing.id` | Stable U.S. search snapshots + exact detail ID/alias; no coords |
| cushman-wakefield | `url:v1:` + first 32 hex characters of normalized public-URL SHA-256 | Provider GUID is observational, not canonical identity; strict inventory preserves detail-only fields and children |
| transwestern | `PageUrl` slug | Invalid rows fail closed. The provider-published `-` slug requires fresh unique official-sitemap membership and an exact primary name/full-address match, including in monitor mode. |
| newmark | NIM canonical URL slug from `newmarkNimCanonicalIdentity` | Strict mode reconciles complete ascending 100-row pages before US filtering; provider ID and slug are separate observations |
| marcus-millichap | `DealId` from map tile | **Lease skipped**; list API caps ~100; map ActivityIds required |
| avison-young | SharpLaunch `row.id` | Strict refresh validates every selected property detail; only ordinary unlimited runs default to no detail enrichment |
| interra-realty | Numeric WordPress `ID` | Legacy `listing/<slug>` records require an explicitly reviewed identity transition; additive ingestion refuses exact-URL cross-identity collisions |
| nai-global | `infabode:{id}` (prefixed in adapter) | Bulk public `publicPosts` detail fields; full and monitor share the conservative `FOR_SALE_ON_MARKET` source-eligibility rule. Do not activate provider status. |
| Buildout sources | `propertyId` base from URL | Strip `-(sale|lease)`; dual rows merge to `sale_or_lease` |
| savills | `ExternalPropertyID` | Direct server-rendered `__NEXT_DATA__`; provider `NextUrl` only, so an invalid or incomplete page fails closed |

Cushman strict pagination accepts only numeric or nonempty numeric-string totals.
A later page with a valid zero total and an empty array receives one same-URL,
uncached read after 12 seconds; the original total, page size, identity and full
aggregate guards still apply. Other malformed/drifting/short responses remain
fatal. Each read retains the existing HTTP/JSON retry policy, so this adds at
most one semantic read for an eligible page, not a source success or lifecycle
waiver (AGENTIC-4520). The scheduled wait is recorded through the existing
`http_helper` retry telemetry; non-strict monitoring is unchanged.

Colliers-main treats only the non-OK browser sidecar's typed numeric
`bootstrapStatus: 403` error envelope as a terminal bootstrap denial. Its full
singleton collector exits 79 with no artifact, and the cohort wrapper carries
settled marker 83; source-bound terminal evidence prevents another automatic
whole-source attempt in the same generation. Monitor/mixed-CLI behavior stays
scoped to its existing paths. Untyped 502, bootstrap 503 and 429 retain their
bounded retries; no denial is source success or inventory removal (AGENTIC-4522).

Savills baseline/coverage comparisons use the registered U.S. commercial
sale+lease scope. See [AGENTIC-4364](https://linear.app/agenticassets/issue/AGENTIC-4364)
for the separate exact-preimage historical correction and ordinary acceptance;
provider/raw, gate-flat and merged unique counts are distinct domains.
Its full-source coverage proof belongs to exactly one Savills-contributing
artifact, even in a mixed-brokerage batch. Unrelated files must not invalidate
that proof; additional Savills source entries or listings in another file must
not borrow it. Keep all batch members and every other source's normal gate.

NAI physical facts come only from unambiguous exact `Year Built` and
`Property Type` two-cell rows in the selected public-post content, outside
nested tables and only without a detail error. Years are exact four-digit
numbers within the canonical ingest bounds. Physical types stay literal for
the existing ingest normalizer; unknown literals retain its existing `other`
behavior. Missing/conflicting labels supply no typed fact, while equivalent
repeats agree. Publication `contentType` is not `assetType`; it remains in the
unchanged raw post alongside the full content. Tags/prose are not inferred
physical facts. Feed fallback and bulk-detail monitor selection stay unchanged.

NAI Infabode post IDs identify publications; an admitted Infabode-to-property/offering
mapping remains unproved under [AGENTIC-4284](https://linear.app/agenticassets/issue/AGENTIC-4284).
The advertised NAI Global Buildout catalog can contain separate sale and lease
records sharing a physical address. Catalog record IDs are not established
physical-property keys or Infabode publication IDs. A first page and its
publisher-reported total do not prove complete inventory, supersession or a
crosswalk between these namespaces. AGENTIC-4284 owns the current findings and
next candidate-bound association proof; existing holds remain unchanged.
Discovery response refusals require acknowledged cancellation or the exact owned
read error as closure evidence, with a separate new one-second cleanup bound.
Unacknowledged cleanup stops script/fallback continuation and retains the rejected
discovery promise across later transactions. This does not change publicPosts.
The same `urlOriginal` can carry different content or images, so do not collapse
posts by URL. Preserve exact `propertyId` routing parameters on NAI Global
`/listings/?propertyId=...` links; a generic shell's canonical URL dropping the
query does not establish property identity.

Keep publication identity, a timestamped observation of that publication,
physical-property identity and sale/lease offering identity distinct. Even an
exact site-native property ID does not prove that two publications describe
the same offering or that the newer one supersedes the older economics and
assets. The current parent `raw_data` overwrite, shallow `extra_facts` merge
and child refresh are not a lossless publication-history store. Retain the
separate saved observations while the association is unresolved; do not append
conflicting publications to an ordinary ingest and claim history is preserved.
Require a provider-supported association/supersession contract and exact
historical compatibility proof before proposing a merge, rekey or retirement.

Avison detail identity supports feed-bound SharpLaunch street-as-name headings with a separate city/state location (full US state names and postal abbreviations), including location-suffixed titles. A registered `www.avisonyoung.us/properties/` wrapper can instead bind one unambiguous `RealEstateListing` name/address/URL to coherent visible name/location and page metadata. Map/script text alone is not street evidence for these layouts. Unsupported native pages may still use a validated official alternate through the existing property-detail child-preserving mode; explicit strict mode continues to require every selected URL.

Avison's bootstrap and entity headers/body now have an explicit 30-second
per-request elapsed budget. Settled bootstrap failure retains the documented
key fallback. The optional timeout argument on the key, entity and feed functions
sets this per-request budget for callers; the feed has no new overall budget.
Website inventory stays required and settled team failure stays
degraded. Both started feeds drain before failure or caching. Native detail has
one monotonic budget across DNS, redirects, headers and body, retaining the 5 MiB
limit and existing URL/IP/TLS/pinning rules. DNS expiry bounds caller return and
prevents late HTTP dispatch; it does not cancel the OS resolver. Native timeout
or response failure destroys owned request/response and requires close ACKs.
A separate new one-second cleanup bound is not measured provider latency.
Unconfirmed feed/native cleanup stops later Avison dispatch, fallback, retry and
alternate routes in this process. These bounds do not preempt synchronous parsing
or satisfy AGENTIC-4294 source acceptance.

Exact feed `name` and `headline` (`meta_title`) are known subject labels for
owned title/OG, complete description and structured-subject comparisons. A
marketing H1 is not itself subject authority. The stronger native named-location
witness needs an exact comma/pipe name-city-state tuple, complete known-ZIP
description, owned metadata and coherent heading. A registered official wrapper
may instead use its unique full structured address/known ZIP plus those metadata,
description and heading checks. A recognized monetary hero is display text only;
it cannot supply a native location witness.

A registered official named property can associate a labeled Offering Summary
Address with one matching hero only when the publisher's singleton remote ID,
complete-feed unique URL owner (including inactive rows), owned URLs and exact
non-address subject all agree. Feed and structured street fields must equal that
same name, with matching locality and present known ZIP; the feed street is not
rewritten. Extra physical/address/map or description-scope contradictions refuse
this witness. A nearby intersection in Property Highlights is location prose,
not the labeled offered address. Numeric/range/unit/intersection subjects receive
no authority from that named-property association. A range offering can use
only the separate, fully witnessed range-locator role below; neither family
rewrites the feed range or expands child replacement or source acceptance.
Existing legacy families remain intact.
Listing photos use the source predicate across discovered URLs, `doc.images` and feed fallback; generic harvested images cannot override it.
The retained property-gallery carousel role can override only the generic-header filename exclusion; unscoped `doc.images` and feed fallbacks cannot. Other source URL exclusions still apply.
Rendered image variants remain distinct URLs. Generic harvest can retain avatar/tracking candidates internally without granting property-photo authority.
Structured name-only agents can coexist with full cards; contact counts are not distinct people.
These existing limits remain on AGENTIC-4294; this witness does not establish clean-gallery/source acceptance.

An official offering H1 may instead bind agreeing complete structured and visible
street/city/state/known five-digit ZIP witnesses with owned subject metadata.
Prose is not identity authority. Within this new physical witness, explicit
description conflicts, bare contradictory address tuples, generic H1, missing
ZIP and price-only heroes remain refused.

A separate official-wrapper display witness may use one of three captured roles:
an exact feed subject with a locality hero (offering copy is optional), an exact
street H1 with a locality hero (with or without an offering suffix), or a
recognized offering/acreage H1 with a complete visible address. It also
requires a unique exact official URL among every current feed row (including
inactive rows), exact owned metadata and structured subject, a present matching
five-digit ZIP on every structured address, substantive page structure, and
agreement from every address-shaped subject or description claim. Repeated H1s
are allowed only when they are identical exact feed subjects; street/acreage
roles require one H1. Missing feed URL context disables only this new witness;
existing identity paths still decide under their own rules. Other captured-role
positives do not authorize street-range, unit, intersection, locality-punctuation,
wrong-number or incomplete-ZIP equivalence; only the separately bounded range
and intersection roles below authorize their respective evidenced layouts.

The exact registered range-offering role keeps the feed's ordered full street
range as the identity and accepts one of its two endpoint numbers only as the
display/map locator. It requires identical ordered endpoints and street in the
feed name and street, H1 and every typed address, plus the captured affirmative
“We are pleased to present the offering memorandum for the properties located
at [range]” clause in the native Building overview. A bare location mention or
an explicit statement that the properties are excluded from the offering refuses
the role. One
registered official URL, unique full-feed URL owner (including inactive rows),
singleton remote/RealEstateListing identity, exact canonical/OG/requested URLs,
matching city/state/known ZIP and hero, and a consistent endpoint in title, OG
title, structured name and every parsed POI are all required. Every POI must have
finite coordinates equal to that listing's typed structured geo; malformed or
contradictory map/address/range claims refuse. This role does not split the range,
infer parcels, or make endpoint containment a general street comparison rule.
Both endpoints are bounded roles; lower-endpoint tests are synthetic controls
unless separately supported by retained source evidence.

Minimized source-page fixtures must preserve the production selector/ancestor
structure, structured-data types, and every identity claim's shape and count.
Before using a minimized page, run the unchanged validator against both the
retained original and the fixture and confirm the same outcome; otherwise a
fixture can accidentally bypass the production identity branch. Remove unrelated
chrome only, and keep raw source values unchanged when comparison needs decoding.

Before retrying a source after a parser change, follow the
[full available corpus diagnosis](../../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md#diagnose-the-full-available-corpus-before-retrying).
Use the existing replay seams; do not discover each remaining layout through a
separate first-failure production attempt.

Avison's strict physical comparison may remove a terminal city/state/five-digit
ZIP tuple only when it agrees with the row's separately bound locality fields.
This comparison view never rewrites source or staged addresses and still rejects
units, ranges and intersections under the existing simple-street grammar. It
supplies no additional visible witness or marketing-hero acceptance role.

Separately, the registered official display decision can pair one complete
physical-address H1 with a finite property-family Space/Suites For Sale/Lease
hero. Only this additional pairing requires the exact official envelope, two
matching typed addresses, Place/category/Offer and feed geo/tenure consistency,
substantive Building overview, and agreement from every populated POI. Medical
Ready/Dental Ready modifiers require Office and exact `office.medical` category;
unknown words, negation, conflicting scope or tenure never become advisory.
Null/empty maps supply no location witness. Existing reciprocal roles retain
their own checks; raw addresses and supported detail freshness stay unchanged.

The official intersection and office-sublease roles remain bounded to the exact
registered-page identity envelope and complete-feed URL owner. Both require one
unique owned URL, remote identity, structured listing, known five-digit feed ZIP,
and agreement from the typed addresses and applicable city/state/geo witnesses.
The intersection role compares its two roads in order using the literal `&`
separator across the feed, visible heading, structured name, and every populated
map point; HTML entity decoding is comparison-only and raw values stay intact.
For the observed office sublease, use the same-page affirmative Building
overview expiry bullet with its marketing heading and coherent hero. A `null` or
empty-array map claim supplies no point; validate every populated claim and
refuse malformed or conflicting claims. Classification follows the registered
official Building family and does not require its incidental stats factbox; a
recognized official Building page must not fall through to generic JSON-LD
acceptance. Neither role infers missing identity, photo evidence, or source
acceptance.

A separate official scope witness may retain a wider description range only
when an exact known-subject H1 and two complete endpoint-address/known-ZIP
witnesses independently agree, with substantive highlights/availability. Every
unique present description must give the same ascending range, starting with
the exact feed house-number token and preserving every remaining street,
city/state/ZIP token. Raw intersection/unit markers cannot disappear through
normalization in this new scope witness. Address-shaped H2s must agree with the
endpoint. This is one-way description scope, not general range equivalence;
native conflicts and feed-range/page-endpoint disagreement retain their existing
refusals.

Keep every validated alternate's contacts/resources and the existing
child-preserving fallback. Identity acceptance does not change feed/page price
precedence: raw page wording and raw feed facts remain alongside the existing
typed feed prices; hidden-price projection remains mandatory. Outside the
complete official scope witness above, present metadata descriptions declaring
a `For sale/lease -` property/location tuple must agree with the exact feed
subject and full address; an otherwise matching visible family cannot override
an explicit contradiction. The prior exact-name visible family retains its
documented optional address-only SEO behavior; a newly used headline alias cannot
inherit contradictory address-only SEO. Absent or unstructured marketing
descriptions remain optional in that prior family.

Avison identity comparison recognizes the evidenced `Road`/`Rd`, `Avenue`/`Ave`
and `Boulevard`/`Blvd` spellings at word boundaries, plus a state followed by a
five-digit ZIP. When both page and
feed supply a ZIP, they must agree; an absent feed ZIP stays unknown. A single
combined hero can bind the exact property name, full street, city and state
when the registered official wrapper uses a descriptive marketing headline.
This still requires coherent visible identity, one structured listing and exact
page metadata/URL bindings. Literal IDs, URLs and raw addresses are unchanged;
the existing property-detail child-preserving mode remains unchanged.
Description tuples compare complete comma-containing names/streets before
separating field roles, and compare a known ZIP+4 exactly. This does not broaden
the stronger witness's five-digit ZIP eligibility. Source-local street comparison
may omit `Fwy`/`Freeway` only immediately after a numbered `I-` interstate;
route number, direction, quadrant, intersecting road and other tokens still match.
An endpoint does not equal a wider range outside that exact range-offering role,
nor does a ZIP or different-street conflict become optional merely because
another visible family agrees.
A native named-property hero may instead show `name | city, state` only when
the visible address heading independently binds the same full name, street,
city and state, with matching title/OG and owned page URLs. A complete visible
`street, city, state` hero can bind a legacy availability layout with matching
property-name metadata; it need not repeat city/state in a separate heading.
A native offering headline may use a bounded property-family `for sale/lease`
role (or `build-to-suit`), including bounded combinations of known property
families and sale/lease, only with one exact visible full-address hero and
coherent property-name/location title, OG and owned URL. A range property may
instead bind its exact visible feed name and city/state to unambiguous description
metadata containing the complete feed name, full street range, city/state and known
ZIP. When that metadata supplies the full-range witness, its complete identity
must agree; an endpoint alone cannot replace the full range. With independently
complete feed/visible street, property name, city/state and owned URL/title/OG,
optional SEO descriptions are advisory source text even if blank or different.
Keep the existing fallback description and `rawSharpLaunch.meta_description`
verbatim: those free-text values do not rewrite structured street/name and do not
prove postal truth or which provider text is current. A metadata-only match or an
arbitrary property/address headline cannot satisfy the witness. Numeric ranges
and units remain unchanged. If structured listing data is present,
require one coherent property name/URL and every supplied full address before
promoting its description, date or media; conflicting/duplicate records refuse.
These are comparison/layout rules, not address rewrites or identity aliases.
A native Building section may supply substantive structure without the older
property-info box: use the existing complete visible-address identity (exact
H1/name, full address hero, title/OG and owned URLs), or require the complete
description identity with known ZIP when it supplies the missing street.
Known displayed ZIP conflicts refuse; absent hero ZIP or unknown feed ZIP keeps
the existing complete-visible family's semantics. Both paths require
populated overview paragraphs/list items inside
`.page-content > section#building .building` plus populated feature fact children
or the existing availability title/value fields. This is only a structure
alternative in the complete native identity branch; all name, full-address,
known-ZIP, heading, metadata, structured-subject and owned-URL checks remain.
Empty/heading-only overviews and overview-only pages gain no acceptance from
this alternative. Populated native Building content without a complete identity
witness is diagnosed as an identity refusal, which need not mean the property is
wrong; empty or unrecognized templates retain the structure-refusal diagnosis.
It grants no current inventory, freshness or source acceptance.

A stronger native physical-identity family may retain a differing H1 as display
text only with BOTH an independently complete visible street/range/unit or
intersection, city/state hero AND the complete feed-name/street/city/state
metadata tuple with known matching five-digit feed ZIP. A hero ZIP, when
present, must agree; the ZIP need not be visibly printed. Exact title/OG-name,
owned requested/observed/OG and every present canonical URL, singular identity
containers, substantive structure and common H2/structured-subject vetoes remain.
A missing canonical link does not itself fail this family. Address-shaped H1s
must agree in full; unknown numeric/unit/intersection/location remnants refuse.
Only bounded evidenced size-qualified and property/space marketing conjunctions
may avoid that ambiguity veto. This is no claim that free-form names are aliases:
canonical feed names and original text stay unchanged. Missing descriptions or
unknown feed ZIP cannot gain this new authority. Other identity families and
validated official-alternate child harvesting retain their existing contracts.
Street comparison also recognizes `Highway/Hwy` and `Drive/Dr`; those rules
apply to a property name/title only when the feed names the property by its
street. The eight standard terminal street postdirections can compare with
their abbreviations; interior direction words, opposing pairs and unit values
are not rewritten. City comparison recognizes `Saint/St.` only in city fields and the
city portion of a title/location, never as `Street` or a property-name alias.
A named native property can use the same complete-description witness: exact
visible feed name and city/state, complete description name/street/city/state
and known five-digit ZIP (including names/streets containing commas only when
one exact field boundary matches), coherent title/OG/owned URLs and substantive property
sections. Missing or conflicting witness fields refuse. For complete native identities,
property highlights plus populated availability fields can establish structure
without the older property-info box. A redundant exact street-only heading
need not repeat independently bound city/state; contradictory identity headings
or structured subjects still refuse. Repeated coherent canonical URLs are one
unambiguous authority; foreign, malformed or differing path/query values refuse.
Do not replace a missing street with a matching city, map/script text or a
marketing headline. Retain wrong-number, ambiguous-heading, metadata, URL and
challenge refusals in regression tests. After repeated acceptance failures, keep a selected-identity-to-validated-route
map and classify unresolved cases before another manual full acceptance trial;
this is investigation guidance, not a per-run prerequisite. Preservation proves unchanged data, not
route coverage, and native-only replay does not prove official fallback or full
source acceptance. Keep dated counts in the owning issue, not as live thresholds.
For Avison, retain `on_market_at` and `off_market_at` as raw source facts; an
`off_market_at` value alone may be historical when `on_market_at` is later, so
do not infer eligibility or lifecycle from timestamp presence alone.
SharpLaunch `sale_unit_price` has no proven per-square-foot unit: retain it in
`rawSharpLaunch` and `saleUnitPrice`, without promoting it to `salePricePerSf`.
Canonical ingestion can derive per-square-foot price from known total and area.

Avison's public-feed producer uses `avison-public-facts-retention/v2`: retain
reported asking values, prose and original `hide_price` controls regardless of
their value. A flag is a source assertion, not proof of UI visibility or access.
Promote a numeric sale amount to USD only with an explicit source USD currency;
keep unknown-unit `sale_unit_price` untyped. Earlier `avison-asking-price-v1`
records retain their historical projection behavior. The optional
`avison_lease_facts` namespace and `cre_avison_lease_facts.py` continue to own
qualified lease observations; none of these paths changes lifecycle or access.

## Monitor Mode Matrix

| Behavior | Sources |
|----------|---------|
| Monitor ≈ full (same rows) | cbre, registered Buildout sources, savills, newmark (NIM) |
| Monitor = enum, skips detail | cushman, marcus, colliers-main, avison-young, transwestern |
| Monitor ≈ full (bulk public detail fields) | nai-global |
| Monitor returns `[]` | jll, jll-investor, cbre-dealflow, colliers (ST) - id only on detail page |

Monitor artifacts → `cre_monitor.py` only. Sources with `[]` stay on full-sweep cadence.

## File Map

| File | Pattern |
|------|---------|
| `cbre.ts` | Internal JSON API, stealth, `waitFor: 4000` |
| `cbre-dealflow.ts`, `colliers.ts` | RCM GET + SLP detail; direct `fetch`; detail concurrency `min(CONCURRENCY, 2)` |
| `cushman-wakefield.ts`, `transwestern.ts`, `avison-young.ts` | Public API/feed + per-URL detail enrich |
| `newmark.ts` | Active direct NIM search in both modes; Algolia/People implementation retained as `srcNewmarkAlgoliaLegacy` |
| `nai-global.ts` | Infabode GraphQL `publicPosts` bulk detail feed; identical conservative eligibility on full and monitor paths |
| `marcus-millichap.ts` | Map ActivityId tiles + detail HTML; JSONL detail cache |
| `jll.ts` | Search pages + `__NEXT_DATA__`; disk detail cache `out/cache/jll-detail/`; strict runs set `JLL_DETAIL_CACHE_MIN_CACHED_AT` |
| `jll-investor.ts` | Two matching complete U.S. search snapshots + fresh structured detail; sale only |
| `colliers-main.ts` | Strict checkpoint: exact-ID anonymous Coveo transport; confirmed bootstrap denial is terminal (see AGENTIC-4522 above). Sitemap/JSON-LD renderer and JSONL cache remain fallback code, not an alternate route after denial. |
| `buildout.ts` | Shared fail-closed inventory adapter |
| `buildout-registry.ts` | Governed single-token Buildout source definitions |
| `savills.ts` | Sale and lease: structured `__NEXT_DATA__`; provider `NextUrl` pagination; direct fetch with validated Firecrawl fallback |

For JLL Investor, only a full-string acreage value on the exact case-insensitive
detail attribute label `Land Area` may populate `lotSizeAcres`. Identical
duplicate numeric values may agree; conflicting or malformed values remain
raw. A unit-only placeholder contributes no value. If the retained search-row
`landArea` is present, it must be an explicit matching acreage value; a conflict
or unsupported unit keeps the normalized field unset. Preserve all original
attributes and search fields. The generic search `area` is not a building-size
source, including on `Land` asset types; do not map it to `buildingSizeSqft` or
`sizeText`. [AGENTIC-4625](https://linear.app/agenticassets/issue/AGENTIC-4625)
owns this bounded field lift.

JLL child harvesting uses a working DOM projection to exclude positively
identified other-property recommendation assets, map tile images, proven srcset
fragments, consent-widget images and native-contact broker-card portraits.
Exact native/base gallery-prefix or retained target-DOM references win when an
asset is shared. Complete comma-bearing srcset candidates remain eligible target
supplements. Unrecognized or ambiguous recommendation scopes retain legacy
harvesting with `jllDetail.harvestScope: "unknown"`; no new collection failure or
NEXT-only fallback is implied. Original raw/cache observations, Markdown and
useful location context remain intact. Scrape lengths/counts describe that
original document. Native/base primary and ordering follow4164, including legacy
order when no anchor exists. [AGENTIC-4342](https://linear.app/agenticassets/issue/AGENTIC-4342)
owns this source boundary; production child-retention guards and separate
performance/transport admission remain unchanged.

`jll-direct.ts` remains experimental and is not the ordinary `jll.ts` detail
route. Its HTML reconstruction uses the existing API native package lazily,
with Turndown/GFM, to rebuild Markdown, images, links, all five attribute
channels and HTML-derived metadata. Request context must come from the actual
response; never borrow rendered metadata or manufacture fresh observation
evidence. Missing native build output refuses conversion without a lossy
fallback; ordinary imports and collection do not require that build. See the
[experimental prerequisite](../SETUP.md#experimental-jll-html-reconstruction).
Conversion is awaited to settlement before abort/deadline checks and fresh
candidate evidence; synchronous native work cannot be preemptively cancelled.

Saved rendered-HTML experiments establish only the tested parser scope, not
native HTTP completeness or transport acceptance. Preserve exact final photo
order and primary, including empty native galleries; an image-set comparison
alone is insufficient. The native image extractor has no DOM-order guarantee.
When both native and base galleries are empty, the current source preserves its
supplied image order: identical conversions can therefore choose different
primaries. This remains an unresolved experimental transport-parity gate;
legacy input-order tests do not satisfy it. Do not sort or change ordinary
primary-photo behavior merely to make an equivalence test pass.
Retain per-build source, resolved Cargo lock, toolchain
and artifact hashes because upstream native dependency resolution is not fully
pinned. Do not copy production binaries or install a new runtime prerequisite
as a side effect of a parser test. Keep routing and admission unchanged until
[AGENTIC-2898](https://linear.app/agenticassets/issue/AGENTIC-2898) qualifies the
actual document mapper and transport.

Matched JLL detail retains strictly boolean `property.isSubleaseOnly` in
`jllDetail.isSubleaseOnly`, including `false` and price-withheld records. Absent,
null or malformed values supply no fact. This preserves the native sublease
flag: `false` means not exclusively sublease, not proof of direct lease. Do not
infer net/gross lease terms, numeric rent or a canonical lease subtype; native ID/URL and asking-price visibility checks remain unchanged.

For JLL native search diagnostics, use the current producer's Node `fetch`
transport and literal published `SearchResults` operation, variables and request
options. A different HTTP client's access-denied response alone does not prove
provider outage, native-record retirement or a detail-layout change. Retain
both observations and their exact request/response evidence; different client
outcomes do not isolate TLS, User-Agent or edge routing as the cause. A native
query that still advertises a detail URL does not establish that the detail
page currently validates; qualify that separately with the actual detail reader.

Strict fresh JLL collections make at most one source-local late pass after the
initial detail pass, only for the latest completed native `__NEXT_DATA__` 404
with exact requested response URLs and current generation/inventory evidence.
Eligibility is established after the existing render fallback: an older cached
404 never overrides a later timeout, abort, challenge or identity conflict.
If any first-pass error is ineligible, skip the entire late pass: those errors
would still refuse the artifact. The late pass reuses original inventory bases
and the same detail concurrency, cache reader and refresh fallback; successful
first-pass rows and their clocks are untouched. One late pass can make both
primary and fallback requests; it is not a one-HTTP-request limit. Local retry
start/outcome logs retain the initial completed-404 evidence without saving a
second body archive. Persistent failures remain `detailError` and still refuse the
complete artifact. No partial admission, retirement or cache-generation rewrite
is implied. [AGENTIC-4357](https://linear.app/agenticassets/issue/AGENTIC-4357)
owns this bounded recovery.

JLL Investor ingestion refuses new exact `source_url` conflict pairs before
committed writes, including non-Investor counterparts in the same brokerage.
The prospective comparison models staged URL moves and soft-deleted revival;
unchanged historical pairs remain visible without being repaired or waived.
A complete current feed does not authorize rekeying, merging or retiring older
identities that share a URL. [AGENTIC-4360](https://linear.app/agenticassets/issue/AGENTIC-4360)
owns the preventive guard and separately approved historical decision.

## Buildout (`buildout.ts`) - Shared Adapter

Provider coordinates are source-owned in `buildout-feeds/`, combining the
unchanged 25 registered tuples with `data/cre-buildout-core-feeds.json` for SVN,
Lee and Franklin. The shared registry has 28 source keys and 29 feed keys;
Franklin's sale/lease cache namespaces and plugin coordinates stay distinct.
Collection and private page capture use those same bindings. Source scope,
canonical inventory admission and source-specific transport options remain
separate. This consolidation requires separate production acceptance.

- **Diagnose the actual adapter before changing its transport.** From a clean,
  genuine isolated clone, replay the registered source's `buildoutInventoryUrl`
  and `directBuildoutJson` with its configured sort and Referer. Compare the
  exact query, User-Agent, Accept, Referer and redirect behavior: a different
  ad-hoc client returning HTML or repeated pages does not establish a current
  collector failure. Keep diagnostic requests bounded, with no automatic
  retries or fallback, and retain private raw bodies, observation times,
  request metadata and hashes. A successful page proves only that page;
  complete inventory still requires every declared page and unique-ID
  reconciliation. Reproduce the failure in the unchanged adapter and isolate
  the cause before adding URL variants, headers, cache changes or HTML parsing.
- Preserve an unambiguous native `# of Spaces` string as literal
  `extraFacts.number_of_spaces`. Equivalent whitespace-cleaned repeats agree;
  blank, malformed or conflicting values supply no fact. This availability text
  is not a building-unit or parking count. Other mappings and the native page
  cache remain unchanged.
- `buildoutFieldObservations` retains bounded source availability literals with
  ordinals and explicit omission/ambiguity markers. Numeric values are unverified
  candidates, never a canonical column update. Cap-rate values are omitted until
  source-specific visibility is established. This is current collector metadata;
  later raw-data updates can replace it, so it is not an observation history. The
  bounded text filter is not a confidentiality guarantee. A synthetic
  `priceWithheld` flag has no established Buildout provider contract in the
  available evidence; the existing sale-price mapping does not evaluate it.
- **No server-side sale/lease filter**; one inventory fetch per `pluginKey`, `buildoutCache` shared across sale+lease passes.
- Current-inventory receipts preserve literal owned HTTP(S) listing URLs, including
  one nonempty `propertyId` alongside empty template parameters and meaningful
  path-routed URLs using the existing native-ID fallback. Admission still requires
  complete pages, unique URL-to-stored-ID bindings and explained dual-mode merges.
  HTTP metadata proves neither redirects nor TLS. Detail resolution requires an
  unambiguous literal query `propertyId` and defaults to HTTPS listing input;
  Leibsohn alone admits its source-provided HTTP URL as local route input. The
  request remains the configured HTTPS Buildout iframe, and the original listing
  URL is retained. A route slug does not authenticate a native record ID.
  Buildout detail routes retain the validated literal `propertyId`, including
  any sale/lease suffix, as the publisher's script does. The original claim URL
  and existing unsuffixed canonical identity remain intact.
  Canonical suffix removal is case-sensitive, matching `cre_ingest.to_row`;
  uppercase and mixed-case suffixes remain part of the established identity.
  Empty, loading-only and tested image/document-only detail responses remain
  `rejected_unqualified_detail` observations; explicit known missing-listing
  markers retain `rejected_missing_detail_shell`. Neither completes the detail
  claim. This bounded check does not establish that other rendered text belongs
  to the claimed property. Footer or brand text can satisfy the content check;
  other Markdown and HTML forms are not universally classified. Markdown is
  parsed with an isolated Marked instance; parsing failure is unqualified.
- **Registered strict sources**: `requireCompletePages: true` (any page fail aborts); stable provider ordering and durable source-scoped page cache at `out/cache/buildout/{slug}/page-NNNN.json`.
- Cache policy is fail-fresh by default: ordinary collection reads live and
  writes every validated page (`read=false`, `write=true`). Explicit
  `BUILDOUT_USE_PAGE_CACHE`, cache-only, and cache-assembly modes read and
  write. `BUILDOUT_REFRESH_PAGE_CACHE=1` forces live reads and still writes.
- Lee assembly: all pages 0–332 present before `BUILDOUT_ASSEMBLE_FROM_CACHE=1`. Partial windows without cache-only → hard error.
- `buildoutFailureCache`: sale pass failure blocks lease pass retry in same process.
- Env: `BUILDOUT_CACHE_DIR`, `BUILDOUT_PAGE_START`/`END`, `BUILDOUT_CACHE_ONLY`, `BUILDOUT_ASSEMBLE_FROM_CACHE`, `BUILDOUT_PAGE_JITTER_MS`.
- Manual freshness run: `BUILDOUT_REFRESH_PAGE_CACHE=1` bypasses durable page-cache reads, fetches the current inventory once per source invocation, and overwrites each successfully fetched cache page. It keeps the in-process sale/lease share. It fails fast if combined with cache-only or cache-assembly recovery modes.
- The governed registry contains all 25 historical single-token public feeds
  recovered from commit `6245a7144`. `REGISTERED_BUILDOUT_SOURCE_KEYS` is the
  exact typed inventory; registry, ingest, checkpoint, SQL, and enrichment
  parity tests prevent partial admission. Offline proof does not replace a
  current live canary and guarded database readback for each source.
- Listing ownership centrally treats the same DNS host with one optional leading
  `www.` label as equivalent, in both producer/detail checks and Python admission
  (AGENTIC-4271, parent AGENTIC-1972). It does not admit other domains,
  subdomains, explicit ports, credentials, fragments or non-HTTPS URLs. Original
  listing URLs, property-ID identity and configured plugin/page/iframe coordinates
  remain intact; endpoint and source-proof binding stay exact.
- A different DNS domain requires a reviewed source-specific coordinate update,
  not a broader host alias. Bind the current official firm identity and its
  properties page to the exact registered Buildout token; retain primary
  evidence on AGENTIC-4271. An unavailable legacy host is not proof of a
  redirect or rebrand. Update only the verified registry page/host, preserving
  source keys, plugin tokens, original listing URLs and listing identities.
- Hudson's October 15, 2026 public feed and historical URLs use
  `hudsonpeters.com`; its tuple keeps that observed coordinate. Both old and
  corrected composed detail iframe probes returned 404. Host compatibility does
  not establish live detail availability.

## SRS, Kidder and Hanley native evidence

The shared `lib/source-evidence/native-json-feed.ts` publisher can retain
decoded source text before JSON validation, row qualification and pruning.
It is default off; evidence policy, custody and append remain in
[`source_evidence/`](../source_evidence/README.md). Unknown valid SRS/Kidder
shapes remain unqualified context. Hanley retains the bounded embedded array
literal and separately projects non-public rows before admission. On 2026-10-29
the owner directed retention and display of accessible SRS API-reported prices
regardless of `Hide_Sale_Price__c`; preserve the original control as context.
Do not infer publisher UI visibility, price currency or source authorization.
None of these
observations qualifies a canonical listing or certifies inventory coverage.

SRS and Kidder cache metadata uses private WeakMaps, including without native
capture. Preserve provider fields named `__creInventoryCacheDisposition` as
source data; they cannot assert collector cache provenance. Cache hits retain
the original observation clock and do not publish a fresh native capture.

## Newmark NIM inventory and evidence

`srcNewmark` serves full and monitor collection from the direct public NIM
search POST. It enumerates ascending 100-row pages for each transaction,
then filters US geography and caps output. Strict mode additionally reconciles
complete provider counts and identities; non-strict mode does not certify that
global reconciliation. `--max-items` does not bound inventory requests. The explicitly
named Algolia legacy implementation is separate from this current dispatch.

`CRE_NEWMARK_PUBLISHED_DETAIL=1` is an explicit experimental enrichment
route, not source permission or an authentication assertion. It resolves only
the witnessed RCM modern landing route from a bounded Newmark source-row
carrier, retains its exact embedded JSON literal and unqualified parsed
context, and compares name and property-local address before considering
substantive source fields. Unknown or mismatched pages remain observations and
do not complete a claim. The source clock is unverified. Source visibility and
credential projection must be reviewed before evidence admission. Normal
dispatch remains unchanged without the marker.

The optional native JSON feed profile captures decoded NIM response text before
page validation, geography, identity and output filtering. It binds sale/lease
request types separately, accepts only the fixed 100-row request and retains
all returned rows as observations. Unrecognized valid JSON remains unqualified
context. This is not canonical admission, source freshness, complete coverage,
display permission or evidence of a publisher's visibility policy. Existing
content checks and the 128-page/128-MiB attempt and 16-MiB decoded-page limits
remain; see [`source_evidence/README.md`](../source_evidence/README.md).

## Kidder Mathews complete inventory

Strict Kidder runs request one `startIndex=0, numResults=8000` response from
`https://services.kidder.com/search/public/listing`, matching the public
property-search frontend's bulk request. This avoids unsorted 50-row page
boundaries; it does not deduplicate provider rows. The independent provider
total, returned count and unique `listing_key` set must agree within the
8000-row ceiling. A provider cap, duplicate identity, malformed body or larger
inventory fails closed. The transport disables redirects and bounds the whole
request/body read to 30 seconds and decoded content to 32 MiB. Generation-bound
caching preserves the actual observation clock, and non-strict pagination is
unchanged. Provider evidence and the guarded live acceptance are tracked by
[AGENTIC-4269](https://linear.app/agenticassets/issue/AGENTIC-4269); transport
success alone does not prove source-gate or database acceptance.

## Key Env Vars (by source)

| Var | Source | Effect |
|-----|--------|--------|
| `CUSHMAN_QUERY` | cushman | Targeted API probe |
| `CUSHMAN_DETAIL_MODE` | cushman | `full` renders detail pages; `base` refreshes API inventory and explicitly preserves existing child collections |
| `COLLIERS_MAIN_*` | colliers-main | Detail concurrency, wait, challenge retries, `MAX_FETCHES_PER_RUN`, and run-scoped `DETAIL_CACHE_PATH` |
| `JLL_DETAIL_*` / `JLL_INVESTOR_*` | jll, jll-investor | Detail concurrency, wait, cache dir, minimum cache timestamp; legacy Investor sitemap-limit helper is unused by the current search collector |
| `SAVILLS_DIRECT_LIST_TIMEOUT_MS` | savills | Direct public list-page timeout (default 25s; bounded 5–60s; two attempts) |
| `SAVILLS_LIST_TIMEOUT_MS` | savills | Firecrawl fallback list-page timeout (default 30s; bounded 10–90s) |
| `NAI_GRAPHQL_TIMEOUT_MS` / `NAI_SOURCE_BATCH_SIZE` / `NAI_PAGE_SIZE` / `NAI_ENUMERATION_CONCURRENCY` | nai-global | Bound each `publicPosts` GraphQL body read (default 30s), split source-office filters (default 40), request up to 100 bulk-detail rows per page, and enumerate unlimited batches at a bounded fan-out of two. A timeout or page cap fails closed for monitor coverage. |
| `AVISON_YOUNG_DETAIL_LIMIT` | avison-young | **Required** for detail on unlimited full runs |
| `AVISON_YOUNG_DETAIL_CONCURRENCY` | avison-young | Detail parallelism |
| `AVISON_YOUNG_DIRECT_DETAIL_ATTEMPTS` / `AVISON_YOUNG_DIRECT_DETAIL_RETRY_MS` | avison-young | Strict direct-property-detail retry count (default 3, max 5) and linear backoff base (default 250 ms, max 5 s). Only the failed page is retried; terminal failures report bounded listing IDs and reasons. |
| `--page-cap` | jll, colliers*, nai | Caps rendered pages / feed offsets |
| `--concurrency` | all Firecrawl-heavy | `pmap` limit (1–6) |

## Detail Failures & Status

- Most detail-enrich sources return rows with `detailError` string; ingest skips child-row refresh when `detailError` in `raw_data`.
- A provider-confirmed absence of public detail is not a scrape error. Such rows
  use structured `detailUnavailable` plus `preserveChildCollections=true`;
  fresh card fields still upsert, while `scraped_at` and the top-level raw
  payload remain the last successful detail observation. The current sparse
  payload and time are stored as `latestInventoryObservation` and
  `inventoryObservedAt`. Existing detail children remain; current card contacts
  and images are applied additively, and new listings insert card children.
- CBRE Deal Flow includes public cards whose only link is a gated agreement or
  public brochure, plus cards with no link at all. They remain source-index
  inventory evidence and never become canonical comparable listings. Their
  provider-supplied agreement or brochure URL remains supporting evidence in
  `cre_source_index.url`. They use
  `gated_agreement`, `public_brochure_only`, or `card_not_linked`
  detail-unavailable reasons. Unlinked
  cards receive a deterministic provisional `card:` identity from their
  visible card fields and point to the public Deal Flow index. Because those
  fields are not a provider key, a later unlinked-to-linked transition is never
  auto-merged; reconcile it only with explicit evidence.
- A small number of CBRE/LightBox landing pages are valid public HTML but omit
  the normal embedded data object. They use `public_html_only`, prove that the
  public page was observed, and preserve last-good detail. The collector fails
  closed if this state exceeds the larger of five listings or 1% of a source
  pass, which guards against silently accepting a provider-wide layout change.
- Deliberately incomplete API/base rows carry `preserveChildCollections=true`;
  ingestion also excludes them from wholesale child replacement.
- A live ingest accepts only `runMeta.mode="full"` or `"enrich"`. Monitor
  artifacts can still be inspected with `--dry-run`, but cannot reach the
  database.
- Native terminal status: cbre-dealflow, colliers-main, cushman (`listingStatus`). NAI's provider status is used only for conservative source eligibility and is never activated. **Disappearance-only** (no status field): jll, jll-investor, newmark, marcus, savills, transwestern (monitor).

## Probe One Source

```bash
npx tsx collect.ts --source=<key> --transaction=both --max-items=6 --out=/tmp/probe.json
python3 cre_ingest.py --in /tmp/probe.json --dry-run
# Ordinary unlimited Avison enrichment: AVISON_YOUNG_DETAIL_LIMIT=50 ...
# Strict Avison refresh requires all selected details; this limit cannot cap it.
# NAI full inventory: --page-cap=400
# Lee cache assembly: BUILDOUT_ASSEMBLE_FROM_CACHE=1 (after pages 0-332 cached)
```

## References

- Parent: `../collect.ts`, `../AGENTS.md`
- Coverage counts: `../START_HERE.md`, `../BROKERAGE_STATUS_2026-06-12.md`
- Monitor layer: `../../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`
- Colliers-main handoff: `../HANDOFF_COLLIERS_MAIN_2026-06-13.md`, `run_colliers_main_full.sh`
- CBRE reference: `../../prometheus/CLAUDE.md`

## CBRE Deal Flow and Colliers source-reported detail facts

Public RCM contact details retain supplied fields independently of `Show*`
display flags. The source flags are preserved as typed state records in the
contact's `sourceDisplayControls`, and the containing detail retains its price
controls. Contact brokerage comes only from the source company field; absent
company does not create a broker-table reference. Credential-bearing and
nonpublic-host contact URLs remain excluded from mapped references.

CBRE's source `projectfields.value` and symbol are recorded in
`extraFacts.cbre_dealflow_reported_price`, including zero, null, missing and
empty-string states. Sale-price text is source-reported; USD numeric mapping
requires an explicit USD currency field and a compatible symbol. The staging
per-square-foot fallback respects that currency basis for these new source
facts; legacy records keep their existing behavior. This does not establish
publisher UI visibility, source authorization, or full detail completeness.
