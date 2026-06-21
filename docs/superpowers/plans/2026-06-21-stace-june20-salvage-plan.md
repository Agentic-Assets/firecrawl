# Stace June20 Salvage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover the useful work from `origin/stace-june20` without regressing the much newer CRE collector architecture on `origin/main`.

**Architecture:** Do not merge `origin/stace-june20` wholesale. Treat it as a research branch and port only validated adapters, tools, and safety improvements into current `main` modules. Every data-writing path must be gated by dry-run artifacts, source completeness checks, and Supabase read-only validation before any production mutation.

**Tech Stack:** TypeScript collector modules under `scripts/firecrawl-ops/cre_collector`, Python ingest and validation tools, Supabase Postgres and REST Data API, self-hosted Firecrawl at `http://localhost:3002`, Node test runner, pytest.

---

## Executive Decision

`origin/stace-june20` contains useful work, but it is stale as a branch. It is 5 commits ahead of its old base and 74 commits behind `origin/main`. A simulated merge conflicts in:

- `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
- `scripts/firecrawl-ops/cre_collector/collect.ts`
- `scripts/firecrawl-ops/cre_collector/cre_ingest.py`
- `scripts/firecrawl-ops/sql/001_cre_brokerages.sql`

Current `main` has modularized the collector into `sources/`, `lib/`, `types.ts`, enrichment workers, monitor mode, geodata, status gates, and a large test suite. The old branch kept most new source logic inside a monolithic `collect.ts`. The intelligent path is selective porting.

## What Is Useful On `origin/stace-june20`

### Tier 1: Port First

- Matthews source adapter, especially the later throttled plain-fetch fix from commit `3f036b73e`.
- Buildout firm discovery and onboarding pattern, including `discover_buildout.py`.
- Franklin Street dual-token Buildout handling.
- SRS, Hanley, and Kidder direct API adapters, after live probe verification.
- `TOP30_EXPANSION_PLAN_2026-06-20.md` as a research artifact, copied or rewritten into current docs.

### Tier 2: Port After Tier 1 Works

- Lyon Stahl sitemap and JSON-LD source adapter.
- Additional Buildout firms from the `BUILDOUT_FIRMS` map.
- SQL seed rows for validated new brokerages.
- Ingest mapping for validated new source keys.

### Tier 3: Keep As Research Until Approved

- Generic sitemap plus LLM extraction source for Interra, DAUM, Foundry, Essex, Pyramid, SHOP, Velocity, AQUILA, Finial, Ackerman, and Maury Carter.
- `cre_ingest_rest.py`, because it uses clean-slate DELETE plus INSERT per brokerage.
- Gemini no-thinking middleware in `apps/api/src/lib/generic-ai.ts`, because it is useful only if the current local extraction route still uses Gemini through an OpenAI-compatible gateway.

### Do Not Port

- The branch's old monolithic `collect.ts` structure.
- Voit or any LoopLink or CoStar-backed scraper path.
- Full database write behavior from `cre_ingest_rest.py` without current-main safety gates.

## File Map

### Files To Create

- `scripts/firecrawl-ops/cre_collector/sources/matthews.ts`
  - Owns Matthews sitemap enumeration, throttled plain fetch, DOM parsing, and unit-testable parsing helpers.
- `scripts/firecrawl-ops/cre_collector/sources/franklin-street.ts`
  - Wraps `srcBuildout` with sale and lease plugin tokens.
- `scripts/firecrawl-ops/cre_collector/sources/srs.ts`
  - Owns the SRS Cloud Run API client and mapper.
- `scripts/firecrawl-ops/cre_collector/sources/hanley.ts`
  - Owns Hanley direct HTML fetch and `rethink_properties` parsing.
- `scripts/firecrawl-ops/cre_collector/sources/kidder-mathews.ts`
  - Owns Kidder public backend API client and mapper.
- `scripts/firecrawl-ops/cre_collector/discover_buildout.py`
  - Recovered from the branch, adjusted to current modular source locations.
- `scripts/firecrawl-ops/cre_collector/docs/stace-june20-recovery.md`
  - Durable research summary extracted from `TOP30_EXPANSION_PLAN_2026-06-20.md`.
- `scripts/firecrawl-ops/cre_collector/tests/ts/sources/matthews.test.ts`
- `scripts/firecrawl-ops/cre_collector/tests/ts/sources/franklin-street.test.ts`
- `scripts/firecrawl-ops/cre_collector/tests/ts/sources/srs.test.ts`
- `scripts/firecrawl-ops/cre_collector/tests/ts/sources/hanley.test.ts`
- `scripts/firecrawl-ops/cre_collector/tests/ts/sources/kidder-mathews.test.ts`

### Files To Modify

- `scripts/firecrawl-ops/cre_collector/types.ts`
  - Add source keys only after the corresponding source module has passing unit tests.
- `scripts/firecrawl-ops/cre_collector/collect.ts`
  - Add dispatch cases for validated new modules.
- `scripts/firecrawl-ops/cre_collector/cre_ingest.py`
  - Add `SOURCE_TO_BROKERAGE` mappings for validated source keys.
- `scripts/firecrawl-ops/sql/001_cre_brokerages.sql`
  - Add seed rows for validated new brokerages.
- `scripts/firecrawl-ops/cre_collector/sources/CLAUDE.md`
  - Add short notes for new source families.
- `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
  - Link to the recovery doc and current source module list.
- `scripts/firecrawl-ops/cre_collector/package.json`
  - No script change expected. Use existing `npm run test`.

### Files To Leave Alone Unless Re-Verified

- `apps/api/src/lib/generic-ai.ts`
  - Only add the Gemini no-thinking middleware after confirming the current model route needs it.
- `scripts/firecrawl-ops/cre_collector/cre_ingest_rest.py`
  - Do not introduce this as a production write path until a separate safety plan is complete.

## Supabase Safety Notes

The Supabase documentation confirms three points relevant to this recovery:

- Data API access must be protected with RLS and least-privilege grants: <https://supabase.com/docs/guides/database/secure-data>
- Service role keys bypass RLS and must never be exposed to frontend code: <https://supabase.com/docs/guides/database/postgres/row-level-security>
- DELETE and INSERT through REST are privileged operations and must be treated as backend-only administrative workflows.

For this repo, the project reference in existing docs is `fhqycqubkkrdgzswccwd`. Use the Supabase MCP only for read-only verification until a task explicitly authorizes writes.

## Task 1: Preserve Branch Evidence

**Files:**
- Create: `scripts/firecrawl-ops/cre_collector/docs/stace-june20-recovery.md`
- Read: `origin/stace-june20:scripts/firecrawl-ops/cre_collector/TOP30_EXPANSION_PLAN_2026-06-20.md`
- Read: `origin/stace-june20:scripts/firecrawl-ops/cre_collector/CODEX_AUDIT_PROMPT_2026-06-20.md`
- Read: `origin/stace-june20:scripts/firecrawl-ops/cre_collector/CODEX_AUDIT_PROMPT_2026-06-20-v2.md`

- [ ] **Step 1: Extract the branch-only research artifacts**

Run:

```bash
git show origin/stace-june20:scripts/firecrawl-ops/cre_collector/TOP30_EXPANSION_PLAN_2026-06-20.md > /tmp/stace-top30.md
git show origin/stace-june20:scripts/firecrawl-ops/cre_collector/CODEX_AUDIT_PROMPT_2026-06-20.md > /tmp/stace-audit-v1.md
git show origin/stace-june20:scripts/firecrawl-ops/cre_collector/CODEX_AUDIT_PROMPT_2026-06-20-v2.md > /tmp/stace-audit-v2.md
```

Expected: three files exist in `/tmp` and are non-empty.

- [ ] **Step 2: Create a current-main recovery doc**

Create `scripts/firecrawl-ops/cre_collector/docs/stace-june20-recovery.md` with these sections:

```markdown
# Stace June20 Recovery Notes

## Verdict

Use `origin/stace-june20` as source research and adapter source code only. Do not merge it wholesale.

## Confirmed Useful Adapters

- Matthews: sitemap enumeration plus throttled plain fetch.
- Franklin Street: dual Buildout plugin tokens.
- SRS: open Cloud Run search API.
- Hanley: embedded `rethink_properties` JSON.
- Kidder Mathews: open public listing API.

## Research To Preserve

- Buildout firm token list and discovery workflow.
- Top-30 feasibility notes.
- Voit LoopLink and CoStar dead-end warning.
- Generic sitemap plus LLM extraction design.

## Write Risk

`cre_ingest_rest.py` can delete and replace active brokerage rows. It must not be used until current-main mark-missing, source completeness, and dry-run gates are ported into it.
```

- [ ] **Step 3: Verify the doc contains no stale merge instructions**

Run:

```bash
rg -n "merge origin/stace-june20|git merge|force|--replace" scripts/firecrawl-ops/cre_collector/docs/stace-june20-recovery.md
```

Expected: no `git merge` or force-merge recommendation. `--replace` may appear only in a warning.

- [ ] **Step 4: Commit evidence preservation**

Run:

```bash
git add scripts/firecrawl-ops/cre_collector/docs/stace-june20-recovery.md
git commit -m "docs(cre): preserve stace june20 recovery notes"
```

Expected: one docs-only commit.

## Task 2: Port Matthews As The Pilot Source

**Files:**
- Create: `scripts/firecrawl-ops/cre_collector/sources/matthews.ts`
- Create: `scripts/firecrawl-ops/cre_collector/tests/ts/sources/matthews.test.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/types.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/collect.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/cre_ingest.py`
- Modify: `scripts/firecrawl-ops/sql/001_cre_brokerages.sql`

- [ ] **Step 1: Write parser tests first**

Create `scripts/firecrawl-ops/cre_collector/tests/ts/sources/matthews.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import { parseMatthewsDetail, matthewsTenureFromUrl } from "../../../sources/matthews.js";

test("matthewsTenureFromUrl classifies leasing slugs as lease", () => {
  assert.equal(matthewsTenureFromUrl("https://www.matthews.com/properties/leasing-abc"), "lease");
  assert.equal(matthewsTenureFromUrl("https://www.matthews.com/properties/panera-bread"), "sale");
});

test("parseMatthewsDetail extracts core fields from server-rendered HTML", () => {
  const html = `
    <html>
      <head><meta property="og:image" content="https://cms.matthews.com/wp-content/uploads/photo.jpg"></head>
      <body>
        <h1 id="propertyTitle">Panera Bread</h1>
        <div id="propertyAddress">123 Main St, Tulsa, OK 74103</div>
        <div id="propertyPrice">$3,000,000</div>
        <div class="key-info-title">Cap Rate</div><div class="key-info-value">6.40%</div>
        <div class="key-info-title">Property Type</div><div class="key-info-value">Retail</div>
        <a id="agentName" href="/agents/jane">Jane Broker</a>
      </body>
    </html>`;
  const row = parseMatthewsDetail(html, "https://www.matthews.com/properties/panera-bread", "sale");
  assert.equal(row?.id, "panera-bread");
  assert.equal(row?.name, "Panera Bread");
  assert.equal(row?.transactionType, "Sale");
  assert.equal(row?.salePriceUsd, 3000000);
  assert.equal(row?.capRatePct, 6.4);
  assert.equal(row?.assetType, "Retail");
  assert.equal(row?.state, "OK");
});
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run test:unit -- tests/ts/sources/matthews.test.ts
```

Expected: fail because `sources/matthews.ts` does not exist.

- [ ] **Step 3: Implement the module from the branch, adapted to current helpers**

Create `scripts/firecrawl-ops/cre_collector/sources/matthews.ts` by porting the branch logic around `matthewsFetch`, `parseMatthewsDetail`, and `srcMatthews`. Use current helpers from:

- `lib/broker.ts` for `brokerRef`
- `lib/html.ts` for HTML and metadata helpers when available
- `lib/util.ts` for `clean`, `moneyToNumber`, `pmap`, and `prune` style helpers
- `types.ts` for `SourceResult` and `Tx`

The module must export:

```ts
export function matthewsTenureFromUrl(url: string): "sale" | "lease";
export function parseMatthewsDetail(html: string, url: string, tx: Tx): any | null;
export async function srcMatthews(tx: Tx, max: number, monitor: boolean): Promise<SourceResult>;
```

Implementation requirements:

- Use plain `fetch`, not Firecrawl renders, for sitemap and detail pages.
- Apply a global rate gate near 30 to 35 requests per minute.
- Back off on `429` and `403`.
- Mark the result `truncated: true` if any fetched detail page fails without throwing.
- In monitor mode, still fetch enough detail pages to emit stable ids, because Matthews detail URLs are the enumeration source.

- [ ] **Step 4: Wire the source key**

Modify `scripts/firecrawl-ops/cre_collector/types.ts`:

```ts
  "matthews",
```

Add it after `"transwestern"` so legacy keys keep their order.

Modify `scripts/firecrawl-ops/cre_collector/collect.ts`:

```ts
import { srcMatthews } from "./sources/matthews.js";
```

Add dispatch:

```ts
    case "matthews":
      return srcMatthews(tx, max, monitor);
```

- [ ] **Step 5: Add ingest mapping**

Modify `scripts/firecrawl-ops/cre_collector/cre_ingest.py` in `SOURCE_TO_BROKERAGE`:

```python
    "matthews": ("matthews", ""),
```

- [ ] **Step 6: Add SQL seed row**

Modify `scripts/firecrawl-ops/sql/001_cre_brokerages.sql` with a Matthews row using slug `matthews`, base URL `https://www.matthews.com`, search URL `https://www.matthews.com/listings`, and `pagination_strategy` equal to `sitemap_enumeration_plain_fetch`.

- [ ] **Step 7: Run tests**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run test:unit -- tests/ts/sources/matthews.test.ts
npm run typecheck
python3 -m pytest tests/test_enum_key_invariant.py tests/test_cre_ingest_builders.py -q
```

Expected: all pass.

- [ ] **Step 8: Run a bounded live probe**

Run only after the local Firecrawl stack status is known:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run collect -- --source=matthews --transaction=both --max-items=4 --out=out/probes/matthews-smoke.json
python3 cre_ingest.py --in out/probes/matthews-smoke.json --dry-run
```

Expected: JSON artifact with sale and lease rows; dry-run stages rows without write.

- [ ] **Step 9: Commit Matthews**

Run:

```bash
git add scripts/firecrawl-ops/cre_collector/sources/matthews.ts \
  scripts/firecrawl-ops/cre_collector/tests/ts/sources/matthews.test.ts \
  scripts/firecrawl-ops/cre_collector/types.ts \
  scripts/firecrawl-ops/cre_collector/collect.ts \
  scripts/firecrawl-ops/cre_collector/cre_ingest.py \
  scripts/firecrawl-ops/sql/001_cre_brokerages.sql
git commit -m "feat(cre): port matthews collector source"
```

## Task 3: Port Buildout Discovery And Franklin Street

**Files:**
- Create: `scripts/firecrawl-ops/cre_collector/discover_buildout.py`
- Create: `scripts/firecrawl-ops/cre_collector/sources/franklin-street.ts`
- Create: `scripts/firecrawl-ops/cre_collector/tests/ts/sources/franklin-street.test.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/types.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/collect.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/cre_ingest.py`
- Modify: `scripts/firecrawl-ops/sql/001_cre_brokerages.sql`

- [ ] **Step 1: Recover `discover_buildout.py`**

Run:

```bash
git show origin/stace-june20:scripts/firecrawl-ops/cre_collector/discover_buildout.py > scripts/firecrawl-ops/cre_collector/discover_buildout.py
chmod +x scripts/firecrawl-ops/cre_collector/discover_buildout.py
```

Then edit `already_onboarded()` so it reads:

```python
src = ""
for path in [HERE / "types.ts", *(HERE / "sources").glob("*.ts")]:
    if path.exists():
        src += "\n" + path.read_text()
```

Expected: the tool deduplicates against current modular source files, not just monolithic `collect.ts`.

- [ ] **Step 2: Add a smoke test for the discovery script**

Create `scripts/firecrawl-ops/cre_collector/tests/test_discover_buildout.py`:

```python
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_discover_buildout_requires_domains():
    proc = subprocess.run(
        ["python3", str(ROOT / "discover_buildout.py")],
        text=True,
        capture_output=True,
    )
    assert proc.returncode != 0
    assert "no domains given" in proc.stderr or "no domains given" in proc.stdout
```

- [ ] **Step 3: Run the script test**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest tests/test_discover_buildout.py -q
```

Expected: pass.

- [ ] **Step 4: Add Franklin Street wrapper**

Create `scripts/firecrawl-ops/cre_collector/sources/franklin-street.ts`:

```ts
import { srcBuildout } from "./buildout.js";
import { SourceResult, Tx } from "../types.js";

const FRANKLIN_SALE_TOKEN = "a234450b432b2b2bebc1ace7e6f692e4489bde70";
const FRANKLIN_LEASE_TOKEN = "2f82fcd26667c4b0126d0084938ffa265f05fa4a";

export function franklinStreetToken(tx: Tx): string {
  return tx === "lease" ? FRANKLIN_LEASE_TOKEN : FRANKLIN_SALE_TOKEN;
}

export async function srcFranklinStreet(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  return srcBuildout(
    "Franklin Street",
    franklinStreetToken(tx),
    "https://www.franklinst.com/properties/",
    tx,
    max,
    monitor,
    {
      preferDirectJson: true,
      directReferer: "https://www.franklinst.com/properties/",
      pageConcurrency: 1,
      requireCompletePages: true,
      cacheSlug: `franklin-street-${tx}`,
      usePageCache: true,
      recoveryPasses: 1,
      recoveryCooldownMs: 15000,
      maxRecoveryPages: 20,
    }
  );
}
```

- [ ] **Step 5: Test token selection**

Create `scripts/firecrawl-ops/cre_collector/tests/ts/sources/franklin-street.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import { franklinStreetToken } from "../../../sources/franklin-street.js";

test("Franklin Street uses separate sale and lease Buildout tokens", () => {
  assert.equal(franklinStreetToken("sale"), "a234450b432b2b2bebc1ace7e6f692e4489bde70");
  assert.equal(franklinStreetToken("lease"), "2f82fcd26667c4b0126d0084938ffa265f05fa4a");
});
```

- [ ] **Step 6: Wire source key and ingest**

Add `"franklin-street"` to `SOURCE_KEYS`, import `srcFranklinStreet`, dispatch it in `collect.ts`, and add:

```python
    "franklin-street": ("franklin-street", ""),
```

to `cre_ingest.py`.

- [ ] **Step 7: Add SQL seed row**

Add Franklin Street with `pagination_strategy` equal to `buildout_inventory_api_dual` and both plugin keys in `scrape_config`.

- [ ] **Step 8: Verify**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run test:unit -- tests/ts/sources/franklin-street.test.ts tests/ts/sources/buildout.test.ts
npm run typecheck
python3 -m pytest tests/test_discover_buildout.py tests/test_enum_key_invariant.py -q
```

Expected: all pass.

- [ ] **Step 9: Commit Buildout discovery and Franklin Street**

Run:

```bash
git add scripts/firecrawl-ops/cre_collector/discover_buildout.py \
  scripts/firecrawl-ops/cre_collector/sources/franklin-street.ts \
  scripts/firecrawl-ops/cre_collector/tests/test_discover_buildout.py \
  scripts/firecrawl-ops/cre_collector/tests/ts/sources/franklin-street.test.ts \
  scripts/firecrawl-ops/cre_collector/types.ts \
  scripts/firecrawl-ops/cre_collector/collect.ts \
  scripts/firecrawl-ops/cre_collector/cre_ingest.py \
  scripts/firecrawl-ops/sql/001_cre_brokerages.sql
git commit -m "feat(cre): port franklin street buildout source"
```

## Task 4: Port Direct API Sources One At A Time

**Files:**
- Create: `scripts/firecrawl-ops/cre_collector/sources/srs.ts`
- Create: `scripts/firecrawl-ops/cre_collector/sources/hanley.ts`
- Create: `scripts/firecrawl-ops/cre_collector/sources/kidder-mathews.ts`
- Create corresponding tests under `scripts/firecrawl-ops/cre_collector/tests/ts/sources/`
- Modify source keys, dispatcher, ingest mapping, and SQL seeds only after each source passes tests.

- [ ] **Step 1: Probe endpoints read-only before porting code**

Run:

```bash
node -e 'fetch("https://srsre-next-412955565034.us-central1.run.app/api/property-search",{method:"POST",headers:{"content-type":"application/json","origin":"https://www.srsre.com"},body:JSON.stringify({query:{offset:0,pageSize:1},client_ip:""})}).then(r=>r.text().then(t=>console.log(r.status,t.slice(0,300))))'
node -e 'fetch("https://hanleyinvestmentgroup.com/listings/",{headers:{"user-agent":"Mozilla/5.0"}}).then(r=>r.text().then(t=>console.log(r.status,t.includes("rethink_properties"))))'
node -e 'fetch("https://services.kidder.com/search/public/listing",{method:"POST",headers:{"content-type":"application/json;charset=UTF-8","origin":"https://www.kidder.com"},body:JSON.stringify({startIndex:0,numResults:1,includeAggregations:false})}).then(r=>r.text().then(t=>console.log(r.status,t.slice(0,300))))'
```

Expected:

- SRS returns HTTP 200 with a JSON object containing `properties`.
- Hanley returns HTTP 200 and `true`.
- Kidder returns HTTP 200 with a JSON object containing `results`.

- [ ] **Step 2: Port only one source per commit**

For each source, follow the Matthews pattern:

1. Add pure mapper tests from a small fixture.
2. Add source module.
3. Add source key.
4. Add dispatcher.
5. Add ingest mapping.
6. Add SQL seed row.
7. Run unit tests and dry-run ingest.
8. Commit.

- [ ] **Step 3: SRS minimum tests**

Create a fixture test asserting:

```ts
assert.equal(mapSrsFixture(fixture, "sale").company, undefined);
assert.equal(mapSrsFixture(fixture, "sale").transactionType, "Sale");
assert.equal(mapSrsFixture(fixture, "sale").country, "US");
assert.ok(mapSrsFixture(fixture, "sale").url?.startsWith("https://www.srsre.com"));
```

Expected: mapping produces stable URL, address, geo, type, and price or price-null behavior without network.

- [ ] **Step 4: Hanley minimum tests**

Create a test for `extractRethinkProperties(html)` using:

```html
<script>var rethink_properties = [{"id":"abc","visibility":"Public","name":"Test Listing","dealRecordType":"Seller_Rep"}];</script>
```

Expected: one parsed public sale listing.

- [ ] **Step 5: Kidder minimum tests**

Create a mapper test with one fake result containing `listing_key`, `property_address`, `city`, `state_code`, `list_price`, `use_type`, `latitude`, `longitude`, and `brokers`.

Expected: source row has stable `id`, `url`, sale price, address, and broker refs.

- [ ] **Step 6: Run verification after each source**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run test:unit -- tests/ts/sources/<source>.test.ts
npm run typecheck
python3 -m pytest tests/test_enum_key_invariant.py tests/test_cre_ingest_builders.py -q
npm run collect -- --source=<source> --transaction=both --max-items=3 --out=out/probes/<source>-smoke.json
python3 cre_ingest.py --in out/probes/<source>-smoke.json --dry-run
```

Expected: each source produces a bounded artifact and dry-run stages rows.

## Task 5: Supabase Read-Only Validation Before Any Write

**Files:**
- No code file changes in this task.
- Use Supabase MCP read-only SQL against project `fhqycqubkkrdgzswccwd`.

- [ ] **Step 1: Confirm current active source coverage**

Run through Supabase MCP `execute_sql` with this read-only query:

```sql
select b.slug, count(*) as active_listings
from credeals.cre_brokerages b
left join credeals.cre_listings l
  on l.brokerage_id = b.id
 and l.deleted_at is null
group by b.slug
order by active_listings desc, b.slug;
```

Expected: returns current active counts. Save only aggregate counts in notes, not secrets or client data.

- [ ] **Step 2: Check whether branch-claimed batch landed**

Run:

```sql
select slug, id, active
from credeals.cre_brokerages
where slug in (
  'matthews',
  'franklin-street',
  'lyon-stahl',
  'srs',
  'hanley',
  'kidder-mathews',
  'faris-lee',
  'fortis-net-lease'
)
order by slug;
```

Expected: confirms whether the branch's claim of 29 new firms in production is current fact.

- [ ] **Step 3: Run advisors before schema changes**

Run Supabase MCP advisors:

- security advisors for `fhqycqubkkrdgzswccwd`
- performance advisors for `fhqycqubkkrdgzswccwd`

Expected: capture only findings relevant to `credeals` tables or new REST ingestion.

- [ ] **Step 4: Do not write yet**

No `DELETE`, `INSERT`, `UPDATE`, `apply_migration`, or REST write call is allowed in this task.

## Task 6: Decide Fate Of `cre_ingest_rest.py`

**Files:**
- Do not create this file in `main` until this task passes.
- Potential create: `scripts/firecrawl-ops/cre_collector/cre_ingest_rest.py`
- Potential create: `scripts/firecrawl-ops/cre_collector/tests/test_cre_ingest_rest.py`

- [ ] **Step 1: Classify the need**

Use direct Postgres first:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 cre_validate.py --help
python3 cre_ingest.py --help
```

Expected: if direct Postgres works for dry-run and non-writing validation, REST ingest is not urgent.

- [ ] **Step 2: If REST is needed, make it safer before adding it**

Required behavior before adding `cre_ingest_rest.py`:

- Default mode is dry-run only.
- `--go` requires `--confirm-slug=<slug>` when exactly one brokerage is in scope.
- Multi-brokerage writes require `--confirm-batch-file=<path>` containing the exact sorted slug list.
- Existing nonempty brokerage replacement requires `--replace` and a pre-delete count printout.
- Script refuses to run if any input run has `runMeta.maxItemsPerSource` set.
- Script refuses to run if any input source has `error`, `truncated`, or `incomplete`.
- Script prints before and after counts.

- [ ] **Step 3: Write tests for refusal behavior**

Create tests that build a tiny fake collector artifact and assert:

```python
assert "dry" in run_without_go.stdout.lower()
assert run_with_capped_input.returncode != 0
assert "max-items" in run_with_capped_input.stderr.lower()
assert run_with_truncated_source.returncode != 0
assert "truncated" in run_with_truncated_source.stderr.lower()
```

- [ ] **Step 4: Only then port the script**

Recover from branch:

```bash
git show origin/stace-june20:scripts/firecrawl-ops/cre_collector/cre_ingest_rest.py > scripts/firecrawl-ops/cre_collector/cre_ingest_rest.py
chmod +x scripts/firecrawl-ops/cre_collector/cre_ingest_rest.py
```

Then apply the safety requirements above before any commit.

## Task 7: Evaluate Gemini No-Thinking Middleware Separately

**Files:**
- Potential modify: `apps/api/src/lib/generic-ai.ts`
- Potential test: existing API tests or a small unit test around request-body middleware if local patterns support it.

- [ ] **Step 1: Confirm current model path**

Run:

```bash
rg -n "MODEL_NAME|OPENAI_BASE_URL|gemini|providerOptions|generic-ai" apps/api scripts/firecrawl-ops .env.example apps/api/.env.example
```

Expected: clear evidence whether Gemini through the OpenAI-compatible provider is still used for local extraction.

- [ ] **Step 2: If still used, extract the middleware into a named function**

Instead of pasting anonymous fetch middleware inline, implement:

```ts
export async function geminiNoThinkingFetch(input: any, init: any): Promise<Response> {
  if (init && typeof init.body === "string") {
    try {
      const body = JSON.parse(init.body);
      if (typeof body.model === "string" && body.model.includes("gemini")) {
        body.providerOptions = {
          ...(body.providerOptions || {}),
          google: {
            ...(body.providerOptions?.google || {}),
            thinkingConfig: { thinkingBudget: 0 },
          },
        };
        init = { ...init, body: JSON.stringify(body) };
      }
    } catch {
      return fetch(input, init);
    }
  }
  return fetch(input, init);
}
```

Then pass `fetch: geminiNoThinkingFetch as any` to `createOpenAI`.

- [ ] **Step 3: Test body rewrite without network**

Add a unit test if the API test harness supports this layer. The test should call a pure helper that rewrites the JSON body and assert:

```ts
assert.equal(rewritten.providerOptions.google.thinkingConfig.thinkingBudget, 0);
```

- [ ] **Step 4: Commit separately**

Run:

```bash
git add apps/api/src/lib/generic-ai.ts
git commit -m "feat(api): disable gemini thinking for gateway extraction"
```

## Task 8: Port Additional Buildout Firms In Batches

**Files:**
- Modify or create source registry files depending on the pattern chosen in Task 3.
- Modify `types.ts`, `collect.ts`, `cre_ingest.py`, and SQL seeds.

- [ ] **Step 1: Pick a registry pattern**

If there are more than five Buildout-only firms, create:

```ts
// scripts/firecrawl-ops/cre_collector/sources/buildout-registry.ts
export const BUILDOUT_FIRMS = {
  "unique-properties": {
    company: "Unique Properties",
    token: "43994fa6c8bc167acf6e799d1ecd08173254b362",
    page: "https://www.uniqueprop.com/",
  },
} as const;
```

Then dispatch any registry key to `srcBuildout`.

- [ ] **Step 2: Batch size rule**

Port no more than five Buildout firms per commit. Each commit must include:

- source key additions
- ingest mappings
- SQL seed rows
- one test that asserts every registry key has an ingest mapping

- [ ] **Step 3: First batch recommendation**

Start with firms the branch claims were sampled cleanly:

- `faris-lee`
- `fortis-net-lease`
- `unique-properties`
- `kiser-group`
- `pinnacle-rea`

- [ ] **Step 4: Verification command**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npm run test:unit -- tests/ts/sources/buildout.test.ts
python3 -m pytest tests/test_enum_key_invariant.py tests/test_cre_ingest_builders.py -q
```

Then run one bounded live probe per new source:

```bash
npm run collect -- --source=<source> --transaction=both --max-items=2 --out=out/probes/<source>-smoke.json
python3 cre_ingest.py --in out/probes/<source>-smoke.json --dry-run
```

Expected: no source writes to Supabase, and every dry-run maps to a seeded brokerage slug.

## Task 9: Full Gate Before Any Branch Publication

**Files:**
- No new files.

- [ ] **Step 1: Run collector TypeScript checks**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run test
```

Expected: typecheck and unit tests pass.

- [ ] **Step 2: Run focused Python tests**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest tests/test_enum_key_invariant.py \
  tests/test_cre_ingest_builders.py \
  tests/test_ingest_mark_missing.py \
  tests/test_folded_coverage_count_aware.py \
  tests/test_transaction_type_no_narrow.py \
  -q
```

Expected: pass.

- [ ] **Step 3: Run lint-free diff check**

Run:

```bash
git diff --check
```

Expected: no whitespace errors.

- [ ] **Step 4: Summarize source status**

Create or update a short status section in `scripts/firecrawl-ops/cre_collector/docs/stace-june20-recovery.md` with:

- source key
- ported or deferred
- test command run
- dry-run artifact path
- Supabase write status, always `not written` unless a later approved write task changes it

- [ ] **Step 5: Commit final docs status**

Run:

```bash
git add scripts/firecrawl-ops/cre_collector/docs/stace-june20-recovery.md
git commit -m "docs(cre): record stace june20 port status"
```

## Open Decisions For Cayman

1. Whether to prioritize more inventory coverage or richer per-listing enrichment first.
2. Whether REST ingestion should exist at all, or whether direct Postgres credentials should be repaired instead.
3. Whether generic LLM extraction is acceptable for production, given cost, latency, and field-quality risk.
4. Whether to treat branch-claimed production landing of 10,062 listings as fact only after live Supabase verification.

## Recommended Execution Order

1. Task 1, preserve evidence.
2. Task 2, Matthews pilot.
3. Task 3, Buildout discovery plus Franklin Street.
4. Task 5, Supabase read-only validation.
5. Task 4, direct API sources, one at a time.
6. Task 8, additional Buildout firms, five per commit.
7. Task 7, Gemini middleware only if still relevant.
8. Task 6, REST ingest only if direct Postgres remains blocked.
9. Task 9, full gate and status doc.

## Self-Review

Spec coverage:

- Branch usefulness is captured in tiers.
- Merge strategy is explicit: no wholesale merge, selective porting.
- Supabase risk is included before any write path.
- Current `main` architecture is preserved.
- Verification commands are included for each implementation phase.

Placeholder scan:

- No task depends on an unspecified future design.
- Every source port has exact files, expected commands, and pass criteria.
- Deferred items have explicit approval or validation gates.

Type consistency:

- New source modules export `src<Name>(tx, max, monitor): Promise<SourceResult>`, matching current `collect.ts` dispatch shape.
- Source keys must be added in `types.ts`, `collect.ts`, `cre_ingest.py`, and SQL seeds in the same task.
- Buildout wrappers call the current `srcBuildout(company, token, page, tx, max, monitor, opts)` signature.
