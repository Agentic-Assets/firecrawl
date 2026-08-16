# Adversarial skeptic report: Linear packets and execution plan

**Date:** 2026-08-14
**Role:** independent skeptic pass over `F-LIN-01` through `F-LIN-10`
**Authority:** read-only roadmap, source, governance, and authenticated
read-only Linear inspection only. No Linear, runtime, model, OCR, CRE, or plan
change was made.

## Verdicts

### F-LIN-01 — REFUTED

The roadmap's `2277 -> 2260 -> 2278 -> 2279 -> 2280` sequence is an
implementation recommendation, not a set of causal prerequisites. The forward
queue calls it an “interface-ladder order” at
`2026-08-14-local-agent-adoption-roadmap-forward-queue.md:21-25`; none of the
five packet definitions of done makes the preceding packet a prerequisite
(`local-agent-adoption-plan.md:251-318`).

Live Linear confirms no added chain relations, while AGENTIC-2260 is already
blocked by the separate CLI-safety packet AGENTIC-2254. Canonical governance
uses the `delegate` field for dispatch and reserves `HOLD:No-Agents` as the
enforceable stop signal (`reference/linear.md:338-356`), not priority as a
topological ordering. Adding the proposed blockers would falsely prevent
independent doctor, documentation, or handoff work. The Medium priority on
AGENTIC-2260 is a triage choice, not proof that its scoped helper work may not
start when its actual AGENTIC-2254 dependency is satisfied.

### F-LIN-02 — CONFIRMED

The plan and live AGENTIC-2278 require that untested `@latest` never be
silently promoted (`local-agent-adoption-plan.md:280-292`; AGENTIC-2278,
Definition of done). Current normal invocation still defaults to
`firecrawl-cli@latest` at `scripts/firecrawl-ops/firecrawl_cli.sh:5-7` and
`firecrawl-mcp@latest` at `scripts/firecrawl-ops/firecrawl_mcp.sh:4-7`.
Environment overrides are optional, so no recorded doctor result controls the
ordinary wrapper default.

**Minimal correction:** AGENTIC-2278 should select and record one exact tested
default per wrapper, make the wrapper tests reject an `@latest` default, and
reserve an explicit caller override for a doctor-mediated upgrade test.

### F-LIN-03 — CONFIRMED

The protection is procedural only. Every helper subcommand accepts
`--model-profile` at `scripts/firecrawl-ops/firecrawl_request.py:405-419`,
then `main()` invokes mutation before the requested command at `:833-837`.
`apply_model_profile()` immediately runs the profile writer and Compose
recreate at `:143-161`; the CLI wrapper has the equivalent unguarded path at
`scripts/firecrawl-ops/firecrawl_cli.sh:64-106`. The profile script writes the
environment before any queue or active-work inspection at
`scripts/firecrawl-ops/set_model_profile.sh:88-94`.

This contradicts AGENTIC-2280's claim that checks prove profile actions cannot
occur automatically during active work and the plan's exclusive-window rule
(`local-agent-adoption-plan.md:162-177`). An explicit flag is sufficient to
disrupt shared work today.

**Minimal correction:** AGENTIC-2280 must require a guarded, operator-only
profile-mutation path. Before any `.env` write or Compose action, it must make
bounded queue and active-work checks, fail closed for active or unknown state,
and require recorded authorization. Tests must prove both failure cases leave
the environment byte-identical and do not invoke Compose.

### F-LIN-04 — CONFIRMED

Phase 0 requires a “compact versioned JSON document” and smoke-evidence
freshness (`local-agent-adoption-plan.md:103-121`), while AGENTIC-2277 only
requires ready/degraded/unavailable/stale fixtures. Neither source defines a
schema, timestamp authority, stale-age threshold, or missing-timestamp result.
The current packet therefore permits incompatible implementations to label the
same evidence differently.

**Minimal correction:** add a versioned schema and status table to
AGENTIC-2277: `schema_version`, `observed_at`, evidence digest/source,
per-check `observed|static|unknown` provenance, and
`ready|degraded|unavailable|stale|unknown`. Define a caller-visible safe
max-age and require missing or invalid evidence time to be `unknown`.

### F-LIN-05 — CONFIRMED

The current helper implements only crawl polling, with the global
`completed|failed|cancelled` terminal set at
`scripts/firecrawl-ops/firecrawl_request.py:30-31` and a hard-wired
`/v2/crawl/:id` poll at `:639-724`. AGENTIC-2260 expands waiting to batch
scrape and extract but specifies only shared mocked terminal cases.

The APIs are not identical by assertion: batch status deliberately reuses the
crawl controller (`apps/api/src/routes/v2.ts:331-335`), whereas extract reports
`processing|completed|failed` through a separate controller
(`apps/api/src/controllers/v2/extract-status.ts:56-70,100-120`). The capability
matrix also records batch and extract status paths as only partly covered
(`docs/firecrawl-ops/references/local-capability-matrix.md:13-16,32-34`).

**Minimal correction:** add a three-route contract table to AGENTIC-2260 with
endpoint, ID field, terminal/failure states, and body-free terminal projection.
Use route-specific fixtures and make unknown statuses fail closed.

### F-LIN-06 — CONFIRMED

AGENTIC-2281 requires both `artifact checksum/path` and a test proving that no
raw local path is retained. The Phase 1 manifest likewise says
`artifact path/checksum` (`local-agent-adoption-plan.md:141-145`), while the
forward queue excludes raw paths
(`2026-08-14-local-agent-adoption-roadmap-forward-queue.md:35-37`). None of
these locations defines a non-sensitive path representation. The resulting
contract is ambiguous exactly where the receipt must be reproducible.

**Minimal correction:** replace `path` with a defined `artifact_ref`: checksum,
opaque identifier, and an optional controlled task-local relative name. Reject
absolute paths and parent traversal, and state that any local resolution map is
not persisted with the receipt or shared manifest.

### F-LIN-07 — CONFIRMED

The missing-gate portion is refuted: live AGENTIC-2282 explicitly says “Do not
start implementation” until a separately reviewed merged fallback and named
consumer exist, and calls current work conditional design. AGENTIC-188 likewise
limits its reader to a named consumer and a public or licensed canary.

The finding is nevertheless confirmed on its independent stale-parent claim.
Live AGENTIC-2253 still describes AGENTIC-195 as a “crawl swarm,” while live
AGENTIC-195 now opens by replacing the autonomous scheduled swarm with a
validation-only materializer and prohibits `POST /v2/crawl`. That stale parent
summary can be read as authorization for the superseded design despite the
roadmap's explicit prohibition (`local-agent-adoption-plan.md:191-193,
363-374`).

**Minimal correction:** update only AGENTIC-2253's related-issue sentence to
label its swarm wording historical and link AGENTIC-195's validation-only
contract. Preserve the original 2026-08-13 evaluation evidence; do not add
invented PR SHA or consumer data to conditional packets before they exist.

### F-LIN-08 — REFUTED

AGENTIC-188 is a reader-contract and full-PDF benchmark packet, not the capped
`/v2/parse` pilot in Phase 1. Its live definition of done requires consuming a
prior benchmark result without launching OCR or changing profiles; it does not
declare AGENTIC-2262 as a prerequisite. The target source already forwards
`maxPages` to the PDF fallback at
`apps/api/src/scraper/scrapeURL/engines/pdf/index.ts:673-684` and applies the
cap in `pdfParse.ts:7-19`. The benchmark also records `num_pages` and page
artifact metrics at `scripts/firecrawl-ops/pdf_ocr_benchmark.py:167-180`.

AGENTIC-2262 remaining Backlog needs ordinary tracker reconciliation on its
own evidence, but it does not establish that AGENTIC-188's reader would accept
a conflicting capped artifact. Adding the proposed dependency would conflate
distinct parse and reader scopes.

### F-LIN-09 — CONFIRMED

The plan and live AGENTIC-195 require validation of caller caps but define no
numeric policy (`local-agent-adoption-plan.md:363-374`). The actual
`params-preview` request accepts only URL and prompt, then returns
model-generated options without a local cap at
`apps/api/src/controllers/v2/crawl-params-preview.ts:9-14,80-113`. The general
crawl schema defaults `limit` to 10,000 and has no displayed upper bound at
`apps/api/src/controllers/v2/types.ts:1128-1173`.

The future materializer can therefore preserve an explicit but unsafe candidate
without violating its written contract.

**Minimal correction:** define static maxima for page/limit, depth,
concurrency, timeout, artifact size, and permitted public host classes. Validate
both caller input and generated preview against the same policy, reject
private/link-local/loopback/metadata targets, and retain a unit proof that the
materializer makes only the preview request.

### F-LIN-10 — REFUTED

The claimed circularity treats founder-decision labels as a kill switch.
Canonical governance identifies `Needs Cayman` as the decision record and
`HOLD:No-Agents` as the enforceable stop signal
(`reference/linear.md:327-340`); it separately states that a `Needs Cayman`
item parks only its dependent action
(`policies/automation-system.md:136-140`). A founder gate can therefore
authorize threat-model analysis without authorizing a sandbox build, matching
AGENTIC-2283's objective to decide before building
(`local-agent-adoption-plan.md:376-388`).

Ingress discovery is also sufficiently bounded for a decision packet: the plan
permits read-only Compose, host-firewall, and reachability evidence before a
change proposal (`local-agent-adoption-plan.md:390-401`), and live
AGENTIC-2284 names bounded probes while prohibiting tunnels, public endpoints,
remote runners, and firewall mutation. No additional split gate is needed to
avoid the claimed deadlock.

## Verified summary and next decision

Live read-only Linear access succeeded on 2026-08-14. All reviewed packets are
Backlog and unassigned; their labels, priorities, parentage, and relations were
rechecked rather than accepted from the finder. The confirmed set is
**F-LIN-02, F-LIN-03, F-LIN-04, F-LIN-05, F-LIN-06, F-LIN-07 (stale-parent
portion only), and F-LIN-09**. The refuted set is **F-LIN-01, F-LIN-08, and
F-LIN-10**.

**Next decision:** approve one narrow, documentation-and-Linear-only tracking
pass for the seven confirmed corrections. Keep F-LIN-03's runtime guard as a
separate implementation packet with its own tests and operator approval; do
not bundle it with issue-description or roadmap edits.
