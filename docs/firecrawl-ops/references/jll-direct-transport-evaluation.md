Part of AGENTIC-2895; implementation and live equivalence tracked in AGENTIC-2898.

# JLL direct transport evaluation

The candidate `sources/jll-direct.ts` and observational
`sources/jll-direct-benchmark.ts` are not wired into `sources/jll.ts`. The
ordinary collector remains rendered-only. They create no C10 receipt, grant,
authority or production admission. Existing C10 request cards, sealed bundles
and authority pins remain unchanged.

The candidate was recovered from closed PR #43, exact head
`8cde359239fb60c65ec5ea5b9450c43ab056ac70`. Its historical throughput results
are hypotheses for current evaluation. The historical paired audit applied the
same Markdown converter to direct and rendered HTML, which did not establish
equality with actual Firecrawl Markdown. Preliminary C1/2/4/8 results also did
not all use the final pinned transport. They do not satisfy fresh acceptance.

Only exact HTTP 200 HTML responses are accepted. The transport permits HTTPS `property.jll.com/listings/<slug>` routes,
validates every redirect and all DNS answers, and pins the actual TLS socket
to a validated public address without connection pooling. One monotonic
process-wide pacer spaces starts by at least 150 ms, including redirects. A
15-second default total deadline bounds DNS, pacing and body acquisition;
synchronous conversion is checked after return and is not preemptively bounded;
body bytes are capped at 5 MiB. Caller cancellation aborts active requests,
removes pending starts and prevents late DNS completion from opening a socket.
HTML, actual derived Markdown, links, gallery URLs and all existing attribute
selectors are retained. No assets are downloaded.

`runJllTransportMatrix` accepts a single fixed cohort of up to 24 exact
provider IDs/canonical URLs, sample IDs, full source-index rows and an inventory
SHA-256, plus an explicitly supplied capture adapter. It snapshots and hashes
the entire cohort; callbacks cannot mutate the controls used by later cells.
The original inventory start/end and each row observation remain unchanged;
this measures cold details over frozen inventory, not new inventory per arm. It
compares client concurrency 1, 2, 4 and 8 on that identical cohort and
alternates transport order. Each cell has a total deadline. Failed, retried,
cached, wrong-identity or challenged captures stop the matrix before higher
concurrency. Late/uncertain backend work remains the foreground owner's
cleanup responsibility; an aborted promise is not a runtime-stop certificate.
Each returned capture is snapshotted before validation/storage; later callback
object reuse cannot rewrite evidence from an earlier arm.

`auditJllCapturedPair` allows the shared capacity coordinator to compare its
original rendered capture against a separately acquired direct capture without
another rendered request. Both records retain their original arm start/end,
generation and full cohort digest inside the same recorded calibration window.
Each observation must fall inside its own original arm, with the expected
transport method and matching projection/document/source-row binding. The
result retains those original clocks and returns fidelity diagnostics only;
it does not calculate throughput or restamp cached evidence as fresh. Actual
rendered/direct elapsed and resource measurements stay in their separate
coordinator records. The standalone matrix cannot consume a prior rendered
capture under a new arm start time.

Paired audits compare the complete native property and broker payloads, all
four real `harvestDetail` asset channels and ordered visible text from the
actual rendered Markdown. The conservative ordered comparison retains case,
punctuation and Markdown markers, normalizing only link destinations, HTML
markup and whitespace. Token omission counts are supplementary coverage
diagnostics; equal token counts alone do not establish Markdown equivalence.
They retain counts and content digests, rather than source
bodies or financial prose. There are initially no special exceptions for
consent chrome: any observed difference requires explicit examination before
acceptance.

The matrix also requires both actual source-enricher projections and exact
normalized fact equality. Missing projection evidence cannot complete a matrix.
`withOfflineJllProjection` uses the unchanged `enrichJllListing` parser with a
separate empty owner-only cache/current generation and fetch dispatch denied.
It must run in a dedicated child because the scoped dispatch guard is global.
A forced cache miss must prove that the dispatch guard intercepted transport,
not merely that the source returned an error. Each result is explicitly an
offline generation-cache projection. The original acquisition observation is
kept separately, including the candidate-only `jll_direct_candidate` method,
which is outside production freshness admission. The fact comparison resolves
broker references to records and compares every other field and array order,
including withholding controls and Markdown. Only acquisition clock,
generation, method and cache metadata are normalized; the original acquisition
and projection provenance are both reported. This does not admit a production
route or create another fresh observation.

The capture adapter receives an arm ID and actual start time, and must return
one client attempt with a live document observation made at/after that start.
The observation must use the expected transport method, a nonempty generation
consistent within the arm and distinct across arms, and cannot be over one
second in the future. The offline projection's original acquisition observation
and full input-document digest must match that actual captured document. Its
source-index row digest must match the original full cohort row, preserving
withholding and transaction controls passed to the parser.
These checks bind reported fresh-capture rates to the arm; immutable inventory
clocks remain separate and are never restamped.
The rendered request must explicitly request `maxAge: 0`, with the SDK retry
limit set to one, and retain that request evidence. Client attempts/retries
are separate from provider attempts/retries. The direct transport reports its
actual socket dispatch and redirect counts; internal rendered provider
attempts and origin cache warmth remain unknown unless independently measured.
Reading back the captured generation cache for an offline audit is not another
fresh request and must retain its real cache provenance.

A foreground evaluation owner must hold the canonical CRE lock, independent
CPU/memory guards, exact runtime ownership and cleanup protocol. It must record
the immutable cohort/inventory digest, current code SHA, requested and effective
client/browser/queue concurrency, all actual container CPU/RAM/PID/SHM caps,
host telemetry and concurrent host workloads. Client C8 does not imply eight
browser pages when the approved runtime has four. The harness does not infer
CPU or memory headroom from throughput.

No live run is performed by importing these modules. The shared rendered
worker being implemented under AGENTIC-2900 will provide the acquisition
adapter after the live JLL provider window is coordinated. No provider or
stack action should overlap the active JLL collection. Fresh successful paired
results are required before changing fallback, cache methods, ingest method
admission or C10 contracts.
