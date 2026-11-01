Part of [AGENTIC-5134](https://linear.app/agenticassets/issue/AGENTIC-5134) and [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Three-model xhigh PDF trial

**Requested xhigh was more expensive and slower for all three models in this
matched cohort. It did not produce a consistent extraction-quality gain.**
Keep High as the baseline candidate for a larger representative trial. Luna
XHigh improved whole-response formatting, but it still omitted an additional
brochure association. Meta XHigh retained a previously observed numeral error
and introduced additional structured omissions. Haiku XHigh exhausted the
output allowance on two documents. These observations do not change production.

## Conditions and scope

The same 30 frozen public PDFs, 189 pages, original extraction prompt and whole
PDF byte uploads were used. The three routes were Muse Spark 1.3 Contributor
through `meta`, Claude Haiku 5.5 through `anthropic`, and GPT-6 Luna through
`openai`. All requested `xhigh`, JSON-object output and a 65,000-token output
allowance, with four workers and no paid retries. Three image-only synthetic
PDF canaries independently verified PDF visibility, two numeric associations,
the marker and matching non-BYOK provider receipts before the cohort ran.

The comparator is the immutable [High study](../2026-11-01-native-pdf-30/README.md),
merged in [PR #401](https://github.com/Agentic-Assets/firecrawl/pull/401) at
`5e10f3d400f0ccbb21a9eb5b7b626646cf2f5af9`. Its documents, prompt, output cap
and providers match. Its primary lane used four workers; its later Meta
supplement used two. Runs occurred sequentially, with one call per cell and
uncontrolled provider/cache conditions. This is a matched descriptive comparison,
not randomized evidence that effort caused the differences.

All 90 XHigh calls settled with HTTP 200 and 90 matching, explicit non-BYOK cost
receipts. Cohort charges were **$0.900649468**; the three canaries added
**$0.000972650**, for **$0.901622118** total. Read-only generation lookups are
separate from inference. There are no unknown charges and no paid retries.

## Cost, speed and format

Costs cover all 30 attempts, including failures. Latency is client API time,
excluding download, preparation, local dispatch waiting and receipt lookup;
Gateway/provider queueing within the HTTP request is included. Medians include
HTTP-200 format failures. Exact nearest-rank and interpolated p90 values are
saved in the measurements and reusable comparison output.

| Model | Requested effort | Cost, 30 PDFs | Median API time | Strict usable /30 | Usable after one fence /30 |
|---|---|---:|---:|---:|---:|
| Meta Contributor | High | $0.08275 | 19.76 s | 30 | 30 |
| Meta Contributor | XHigh | $0.09950 | 35.61 s | 30 | 30 |
| Haiku 5.5 | High | $0.28523 | 26.77 s | 9 | 28 |
| Haiku 5.5 | XHigh | $0.59096 | 59.87 s | 21 | 28 |
| GPT-6 Luna | High | $0.17538 | 31.39 s | 29 | 29 |
| GPT-6 Luna | XHigh | $0.21019 | 54.06 s | 30 | 30 |

Strict/recovered usable requires HTTP 200, a normal stop, no truncation and all
required member shapes. XHigh Haiku needed seven exact outer-fence recoveries;
its other two outputs ended `length` and remain unusable. High Haiku instead had
19 recovered outputs and two wrong-shape JSON objects. Equal usable counts do
not mean the same documents failed or the same failure mode occurred.

All costs are actual billed charges. Reported XHigh reasoning tokens totalled
160,696 for Meta, 532,315 for Haiku and 128,252 for Luna, with all 30 records
available per model. Meta reported 1,584 cached input tokens; Luna reported
590,470 cache-creation input tokens. Those are provider usage fields, not a
separate fee, cold-cache normalization or proof of a native compute budget.

## Selected facts and tables

Two independent agent lanes reviewed all frozen references against source
pages. This is agent review, not human certification. The original gold has
98 fact targets/model. One ambiguous source target, `core-10`, is excluded
consistently from both views and every model, leaving 97 scorable targets.
The same 16 table rows/model were reviewed with source headers and context.
These selected probes are not exhaustive precision or recall.

| Model / effort | Clean fact matches /97 | Other scorable fact outcomes | Clean table rows /16 |
|---|---:|---|---:|
| Meta / High | 96 | 1 missing | 16 |
| Meta / XHigh | 94 | 1 correct with conflicting extra value, 2 missing | 16 |
| Haiku / High | 97 | None in this reference | 16 |
| Haiku / XHigh | 91 | 6 unscorable because of truncation | 13 |
| Luna / High | 97 | None in this reference | 16 |
| Luna / XHigh | 97 | None in this reference | 16 |

The table is the explicitly identified content sensitivity; strict decisions
remain separately saved. A source-correct value in an appropriately associated
table can count; transcription-only presence does not count as a structured
fact. A readable field in wrong-shape JSON does not make its document format
usable. Truncated outputs are not mined for partial successes.

Meta's two XHigh structured omissions are the Form 1065 attachment instruction
in `external-19`, repeated from High, and the publication number in `external-21`,
which High retained. In `core-15`, correct size fields coexist with another
structured rendering whose numeric punctuation conflicts with the source.
That outcome remains `correct_with_conflict`, not a clean pass.

An entire document counts as a format-and-selected-reference pass only when all
its frozen probes have been reviewed, all non-ambiguous probes are clean and its
response is usable. High/XHigh counts are Meta 29/27, Haiku 28/28 and Luna 29/30.
Their cost per such pass is respectively $0.002853/$0.003685,
$0.010187/$0.021106 and $0.006047/$0.007006. These are selected-reference task
metrics and do not include the separate extra challenges below.

### Separate known-error challenges

Two extra brochure challenges were specified before XHigh outputs were reviewed,
then reviewed across all three models and both conditions (12 judgments).
They remain outside the frozen 97-fact denominator.

| Challenge | Haiku High → XHigh | Meta High → XHigh | Luna High → XHigh |
|---|---|---|---|
| `core-05` office/warehouse association | Correct → correct | Correct → correct | Reversed association → missing association |
| `core-06` building area versus suite area | Correct → correct | Wrong building numeral → same wrong numeral | Correct → correct |

Haiku's High `core-05` and XHigh `core-06` judgments require the separately saved
fence sensitivity. Luna's 97/97 selected facts do not cancel its extra challenge
failure. A value omitted after previously being wrong is not a corrected extraction.

### Long documents and locators

On the full 84-page `external-21` control, Meta took 103.83 s/$0.024039298,
Luna 51.85 s/$0.080905600 and Haiku 250.92 s/$0.282638000. Haiku reached the
65,000-token allowance and ended `length`; its control facts and rows are
unscorable. Its 18-page `public-15` output also ended `length`. This establishes
two failed responses under these settings, not a model page-limit rule.

The otherwise usable Meta/Luna control transcriptions had only 0.1345/0.0242
token overlap with the separately retained Poppler reference. Their structured
facts/tables can contain other source text, so these are transcription-field
diagnostics, not full semantic recall. A normal stop does not prove complete text.
Keep AnyDoc text independently rather than relying on LLM transcription as the
archive.

Meta again used printed folios for `public-15` and the IRS table (physical PDF
page 5 labeled printed page 1). XHigh Luna labeled that table physical page 5,
where its High output used printed page 1. The original prompt did not specify
which convention to use. Save these as locator-convention flags, not fabricated
source values or a general citation-accuracy result.

## Requested effort and reusable implementation

All routes accepted the XHigh canary. Actual native effort mapping remains
unverified. Meta's own documentation conflicts about whether XHigh differs
from High for this family; the numeric strengths for the separate Muse Spark
Open model cannot be transferred to Contributor. The
[dated primary-source notes](reasoning-research.md) preserve the sources and
uncertainty. Do not infer extra native computation from acceptance alone.

The paid study used its frozen private dispatcher and existing committed
transport. While it ran, the methods were generalized into the committed
[reusable system](../../../../../scripts/firecrawl-ops/benchmarks/document_input/README.md):
configurable grids, image-only qualification and generator, source/code sealing,
finite budget reservations, no automatic paid retries, read-only adoption,
strict/fence/field sensitivities, full reference-roster checks and paired
comparisons. The new CLI was tested with simulated transport; its offline
adoption independently reconstructed and verified all 150 High and 90 XHigh
results. The paid cohort was not rerun through the new CLI.

The [reusable grid](reusable-grid.json) preserves this trial's route/settings
selection without private paths. Future availability and provider terms require
fresh verification. Contributor approval applies to this exact public corpus.
No production model profile, document writer, collector, schema or scheduler was
changed. [W5](https://linear.app/agenticassets/issue/AGENTIC-5140) and
[W6](https://linear.app/agenticassets/issue/AGENTIC-5141) remain the consumer-side
implementation homes; this study does not establish their runtime acceptance.

## Durable artifacts and reproduction

- [Frozen protocol](protocol.json), [prompt](prompt.txt), [qualification receipts metadata](qualification.json) and [provenance](provenance.json).
- [90-cell measurements](measurements.json), [342 source-review decisions](semantic-review.json), [12 additional challenge judgments](challenges.json) and [reusable comparison output](comparison.json).
- Common-format verified reports: [High primary](verified-high-primary.json), [High Meta](verified-high-meta.json), [XHigh](verified-xhigh.json). The first includes the two earlier Gemini comparators for baseline audit only; they were not rerun at XHigh.
- The prior [public corpus index](../2026-11-01/corpus.json) retains safe public URLs and PDF hashes. Exact reproduction requires separately retained original bytes and the frozen reviewed gold. Source bodies, contacts, credentials and full responses are not committed.

The comparison can be regenerated offline with committed code:

```sh
python3 scripts/firecrawl-ops/benchmarks/document_input/compare.py \
  --report docs/firecrawl-ops/references/document-input-benchmark/2026-11-01-native-pdf-xhigh-30/verified-high-primary.json \
  --report docs/firecrawl-ops/references/document-input-benchmark/2026-11-01-native-pdf-xhigh-30/verified-high-meta.json \
  --report docs/firecrawl-ops/references/document-input-benchmark/2026-11-01-native-pdf-xhigh-30/verified-xhigh.json \
  --review docs/firecrawl-ops/references/document-input-benchmark/2026-11-01-native-pdf-30/semantic-review.json \
  --review docs/firecrawl-ops/references/document-input-benchmark/2026-11-01-native-pdf-xhigh-30/semantic-review.json \
  --gold /private/corpus/reviewed_associations.json \
  --out /private/new-comparison.json
```

The original High folder is unchanged. The new study preserves failures,
conflicts and unknowns under their measured definitions instead of replacing
historical results.
