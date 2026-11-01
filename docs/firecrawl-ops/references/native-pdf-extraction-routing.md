Part of [AGENTIC-5134](https://linear.app/agenticassets/issue/AGENTIC-5134), [AGENTIC-5140](https://linear.app/agenticassets/issue/AGENTIC-5140) and [AGENTIC-5141](https://linear.app/agenticassets/issue/AGENTIC-5141).

# Native PDF extraction routing

Cayman's decision on November 1, 2026 selects this policy for public CRE listing
documents:

| Stage | Default | Purpose |
|---|---|---|
| Independent text retention | Pinned AnyDoc parser | Retain extracted text, source-content hash and parser provenance separately from model output |
| Native PDF structured extraction | `meta/muse-spark-1.3-contributor`, provider `meta`, requested `high` | Extract typed listing facts from the original PDF |
| Uncertainty or conflict review | `anthropic/claude-haiku-5.5`, provider `anthropic`, requested `high` | Review unclear or conflicting facts in a bounded additional pass |

Provider pins prevent silent cross-provider routing. Requested High records the
API setting; it is not proof of equal native computation across models. Meta
Contributor's provider-training terms are accepted for the authorized public CRE
document scope. Private deal files and gated deal rooms remain outside that scope.

Retain the primary and review observations, source evidence, qualifiers and
unresolved disagreement. A second model's answer does not silently erase the
first. Missing facts remain missing, and model agreement is not independent proof
of source truth. Preserve historical parser/model runs rather than rewriting
their results under the new model identity. Retain AnyDoc output independently
even when structured extraction fails; model-generated transcription is not the
text archive.

## Implementation owner

The executable extraction path belongs to
[GetCREdata `documents/`](https://github.com/Agentic-Assets/GetCREdata/tree/main/documents),
with model and budget configuration in
[`config.py`](https://github.com/Agentic-Assets/GetCREdata/blob/main/config.py).
Its existing pilot/backfill entrypoints use the shared extractor and writer.
GetCREdata remains the sole OM-facts writer. Firecrawl captures source listings
and document links and preserves its ownership boundary.

Implementation and integration proof are in
[GetCREdata #65](https://github.com/Agentic-Assets/GetCREdata/pull/65) and
[#66](https://github.com/Agentic-Assets/GetCREdata/pull/66). Both provider routes
passed an owned synthetic native-PDF test at requested High, with all four
expected facts correct and matching provider cost receipts. Meta uses a closed
strict JSON schema. Haiku's full nullable schema exceeds its provider's union
limit, so its request uses JSON-object mode with the schema in the trusted prompt
and local Pydantic validation, including an exact single outer-fence recovery.
That validation does not establish strict compliance of the raw Haiku response.
The [body-free live proof](https://github.com/Agentic-Assets/GetCREdata/blob/main/documents/eval/native_pdf_routing_smoke.json)
preserves the successful calls and rejected compatibility diagnostics separately.

The global self-hosted Firecrawl text-model profile is a separate surface, owned
by the [model-routing procedure](model-routing.md). This document selects the
native-PDF listing extractor's routing; it does not prescribe a global scraper
model transition or a new Firecrawl OM writer.

## Evidence and acceptance

The [High comparison](document-input-benchmark/2026-11-01-native-pdf-30/README.md)
and [XHigh follow-up](document-input-benchmark/2026-11-01-native-pdf-xhigh-30/README.md)
support the choice. Meta High was the cheapest and fastest of these three in the
30-document convenience sample, with a scored omission and an additional verified
building-area error. Haiku remains a review/reference model. The benchmark's
full-transcription prompt is distinct from GetCREdata's typed facts-only request;
the actual route needs its own request and integration verification.

W5 tracks typed extraction, routing and its evaluation. W6 tracks durable worker
leases, status transitions, spend controls, storage lifecycle and writer proof.
Local AnyDoc artifacts do not establish cloud/database retention; the worker's
durable storage lifecycle must be verified in its deployment environment.
W3 shared DDL, W7 scheduled deployment and W11 backlog execution retain their
existing separate gates in the
[canonical plan](https://linear.app/agenticassets/document/listing-brochure-extraction-plan-agentic-5134-be9ffcf20b84).
Record source merge, live request proof, database retention and scheduled runtime
acceptance separately.
