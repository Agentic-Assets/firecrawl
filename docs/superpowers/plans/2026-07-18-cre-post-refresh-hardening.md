# CRE Post-Refresh Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make future CRE refreshes auditable and safe by aligning the monitor contract with full collection, producing a repeatable live refresh report, and replacing stale operational guidance with the verified July 18 state.

**Architecture:** Keep collection additive and status-neutral. A small read-only report command will query the existing `credeals` tables through the same `psql` and environment-discovery path as validation. NAI receives an end-to-end mocked source-contract test so full and monitor inventories cannot silently diverge again; documentation then points operators to the report and the approval-gated scheduler path.

**Tech Stack:** TypeScript/tsx collector, Python 3 standard library, PostgreSQL via `psql`, pytest, Node built-in test runner, Markdown.

## Global Constraints

- Work on a `fix/*` or `docs/*` branch; do not push or merge `main` without the required approval.
- Keep the normal ingest additive: no `--mark-missing` and no `--activate-status` in any new automated path.
- `GetCREdata` remains the sole production OM-extraction writer; do not add an OM parse call to Firecrawl.
- Do not restore, install, or load launchd jobs in this work. Scheduler activation remains a separate Gate 5, founder-approved action.
- Do not log or persist database URLs, tokens, cookies, or local environment values.
- Treat a monitor artifact as unsafe for `cre_ingest.py`; monitor artifacts go only to `cre_monitor.py`.
- Preserve the existing known collation warning as an advisory validation finding unless a separately approved database remediation is performed.

---

## File Structure

- Modify: `scripts/firecrawl-ops/cre_collector/sources/nai-global.ts` — expose a testable NAI collection seam and preserve the one eligibility rule for full and monitor passes.
- Modify: `scripts/firecrawl-ops/cre_collector/tests/ts/sources/nai-global.test.ts` — prove full and monitor output exactly the same eligible NAI identities from mocked public GraphQL pages.
- Create: `scripts/firecrawl-ops/cre_collector/cre_refresh_report.py` — read-only, date-bounded database report for inventory, source-index, events, and enrichment queue health.
- Create: `scripts/firecrawl-ops/cre_collector/tests/test_cre_refresh_report.py` — pure and subprocess-mocked tests for the report; no live database.
- Modify: `scripts/firecrawl-ops/cre_collector/START_HERE.md` — concise current July 18 banner, report command, and clear historical-document boundary.
- Modify: `scripts/firecrawl-ops/cre_collector/CLAUDE.md` — make the report the source for quoting current counts and link the completed refresh evidence.
- Modify: `docs/firecrawl-ops/references/cre-monitor-subsystem.md` — correct NAI’s monitor/full contract and remove stale “launchd is running” claims.
- Modify: `scripts/firecrawl-ops/cre_collector/launchd/README.md` — retain the templates and gate, but state the actual disabled scheduler status and post-merge activation prerequisites.
- Modify: `tasks/2026-07-18-cre-listing-refresh/refresh-summary.md` — link the new report and convert the remaining queue into an explicit source-specific follow-up table.

## Task 1: Lock NAI full/monitor inventory parity with an end-to-end source test

**Files:**

- Modify: `scripts/firecrawl-ops/cre_collector/sources/nai-global.ts:73-110,408-512`
- Modify: `scripts/firecrawl-ops/cre_collector/tests/ts/sources/nai-global.test.ts`

**Interfaces:**

- Consumes: `srcNaiGlobal(tx: Tx, max: number, monitor: boolean): Promise<SourceResult>` and `naiIsSourceEligible(row, tx)`.
- Produces: `srcNaiGlobalWithSourceIds(tx: Tx, max: number, monitor: boolean, sourceIds: number[]): Promise<SourceResult>` for tests; the public `srcNaiGlobal` continues to call it with `NAI_SOURCE_IDS`.
- Invariant: for identical source pages, full and monitor output the same ordered `id` set and both report `truncated === false` when the mock ends with an empty page.

- [ ] **Step 1: Add the failing parity test**

Add this test and import `srcNaiGlobalWithSourceIds` and `naiFeedPageCache`:

```ts
test("NAI monitor and full paths emit the same source-eligible identities", async (t) => {
  const originalFetch = globalThis.fetch;
  naiFeedPageCache.clear();
  globalThis.fetch = (async (_url, init) => {
    const body = JSON.parse(String(init?.body));
    const offset = body.variables.offset;
    const rows = offset === 0
      ? [
          { id: 101, contentType: { id: 4, name: "Sale" }, listingStatus: "FOR_SALE_ON_MARKET", title: "Sale" },
          { id: 102, contentType: { id: 4, name: "Sale" }, listingStatus: "OFF_MARKET", title: "Old sale" },
          { id: 103, contentType: { id: 10, name: "Lease" }, listingStatus: "FOR_SALE_ON_MARKET", title: "Lease" },
        ]
      : [];
    return { ok: true, status: 200, text: async () => JSON.stringify({ data: { publicPosts: rows } }) } as Response;
  }) as typeof fetch;
  t.after(() => { globalThis.fetch = originalFetch; naiFeedPageCache.clear(); });

  const full = await srcNaiGlobalWithSourceIds("sale", Infinity, false, [9001]);
  naiFeedPageCache.clear();
  const monitor = await srcNaiGlobalWithSourceIds("sale", Infinity, true, [9001]);

  assert.deepEqual(full.listings.map((row: any) => row.id), ["infabode:101"]);
  assert.deepEqual(monitor.listings.map((row: any) => row.id), ["infabode:101"]);
  assert.equal(full.truncated, false);
  assert.equal(monitor.truncated, false);
});
```

- [ ] **Step 2: Run the test to verify the missing seam fails**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
node --import tsx --test tests/ts/sources/nai-global.test.ts
```

Expected: the new test fails to import `srcNaiGlobalWithSourceIds`.

- [ ] **Step 3: Add the minimal test seam without changing production behavior**

Replace the current public entry point with this wrapper plus implementation signature:

```ts
export async function srcNaiGlobal(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  return srcNaiGlobalWithSourceIds(tx, max, monitor, NAI_SOURCE_IDS);
}

export async function srcNaiGlobalWithSourceIds(
  tx: Tx,
  max: number,
  monitor: boolean,
  sourceIds: number[]
): Promise<SourceResult> {
  const sourceBatches = naiSourceIdBatches(sourceIds);
  // Move the existing srcNaiGlobal body here unchanged after this declaration.
  // Every fetch must continue to receive the local sourceBatches value.
}
```

Keep the existing source-eligibility predicate inside `collectBatch` exactly:

```ts
const matching = page.filter((row: any) => naiIsSourceEligible(row, tx));
```

Do not change `naiListingStatus`, do not emit `statusBadge`, and do not modify
the default `srcNaiGlobal` behavior.

- [ ] **Step 4: Run focused verification**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
node --import tsx --test tests/ts/sources/nai-global.test.ts
```

Expected: typecheck exits 0 and every NAI source test passes, including the new parity test.

- [ ] **Step 5: Commit the independently reviewable contract guard**

```bash
git add scripts/firecrawl-ops/cre_collector/sources/nai-global.ts \
  scripts/firecrawl-ops/cre_collector/tests/ts/sources/nai-global.test.ts
git commit -m "test(cre): lock NAI monitor inventory parity"
```

## Task 2: Fail closed on targeted-enrichment artifact provenance

**Files:**

- Modify: `scripts/firecrawl-ops/cre_collector/cre_enrich.py:398-500`
- Modify: `scripts/firecrawl-ops/cre_collector/tests/test_cre_enrich.py`
- Modify: `scripts/firecrawl-ops/cre_collector/tests/ts/lib/enrich.test.ts`

**Interfaces:**

- Consumes: claimed queue rows (`url`, `source_key`) and the enrich artifact (`runMeta.mode`, `listings`).
- Produces: `validate_enriched_artifact(claimed_rows, artifact) -> list[dict]`.
- Invariant: mode is `enrich`; every output URL was claimed; every output `sourceKey` matches the claimed row for that URL.

- [ ] **Step 1: Write the failing artifact-provenance tests**

Add this test block to `tests/test_cre_enrich.py`:

```python
def test_validate_enriched_artifact_rejects_wrong_mode_or_url_or_source():
    claimed = [{"id": "q1", "url": "https://example.test/a", "source_key": "svn"}]
    invalid = [
        {"runMeta": {"mode": "full"}, "listings": [{"url": "https://example.test/a", "sourceKey": "svn"}]},
        {"runMeta": {"mode": "enrich"}, "listings": [{"url": "https://example.test/b", "sourceKey": "svn"}]},
        {"runMeta": {"mode": "enrich"}, "listings": [{"url": "https://example.test/a", "sourceKey": "lee-associates"}]},
    ]
    for artifact in invalid:
        with pytest.raises(ValueError):
            cre_enrich.validate_enriched_artifact(claimed, artifact)
```

Add a `run()` orchestration test that supplies an artifact with URL `b`, asserts
the ingest subprocess is not called, and asserts the claimed id `q1` is released.

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest -q tests/test_cre_enrich.py -k "validate_enriched_artifact"
```

Expected: failure because the helper does not exist.

- [ ] **Step 3: Implement the pre-ingest provenance guard**

Add this helper above `run()` in `cre_enrich.py`:

```python
def validate_enriched_artifact(claimed_rows, artifact):
    if not isinstance(artifact, dict) or (artifact.get("runMeta") or {}).get("mode") != "enrich":
        raise ValueError("enriched artifact must declare runMeta.mode == 'enrich'")
    listings = artifact.get("listings")
    if not isinstance(listings, list) or not listings:
        raise ValueError("enriched artifact must contain a nonempty listings array")
    claimed_by_url = {row["url"]: row for row in claimed_rows if row.get("url")}
    safe = []
    for listing in listings:
        url = listing.get("url") if isinstance(listing, dict) else None
        claimed = claimed_by_url.get(url)
        if claimed is None:
            raise ValueError(f"enriched artifact URL was not claimed: {url!r}")
        if listing.get("sourceKey") != claimed.get("source_key"):
            raise ValueError(f"enriched artifact source key mismatch: {url!r}")
        safe.append(listing)
    return safe
```

Call it after artifact JSON parsing and before `run_ingest`. On `ValueError`, run the existing release-claims path for every claimed id, retain the artifact under `out/enrich/`, and exit nonzero. Pass only the returned `safe` list to `select_done_and_retry`.

- [ ] **Step 4: Lock the TypeScript enrichment boundary**

Add this assertion to the current `runEnrichGroups` test in `tests/ts/lib/enrich.test.ts`:

```ts
const inputUrls = new Set(input.map((row) => row.url));
assert.ok(result.listings.every((listing: any) => inputUrls.has(listing.url)));
```

- [ ] **Step 5: Run focused verification**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest -q tests/test_cre_enrich.py tests/test_cre_enrich_psql.py
node --import tsx --test tests/ts/lib/enrich.test.ts
```

Expected: wrong-mode, unclaimed-URL, and wrong-source artifacts cannot reach ingest.

- [ ] **Step 6: Commit the safety fix**

```bash
git add scripts/firecrawl-ops/cre_collector/cre_enrich.py \
  scripts/firecrawl-ops/cre_collector/tests/test_cre_enrich.py \
  scripts/firecrawl-ops/cre_collector/tests/ts/lib/enrich.test.ts
git commit -m "fix(cre): verify targeted enrichment provenance"
```

## Task 3: Add a read-only refresh-report command

**Files:**

- Create: `scripts/firecrawl-ops/cre_collector/cre_refresh_report.py`
- Create: `scripts/firecrawl-ops/cre_collector/tests/test_cre_refresh_report.py`
- Modify: `scripts/firecrawl-ops/cre_collector/cre_validate.py`
- Modify: `scripts/firecrawl-ops/cre_collector/tests/test_cre_validate.py`

**Interfaces:**

- Consumes: `cre_ingest.load_db_url(env_file)`, `cre_ingest.find_psql()`, an ISO-8601 `--since` timestamp, and read-only `psql` queries.
- Produces: JSON with keys `since`, `inventory`, `events_by_type`, `source_index`, and `queue_by_source`; Markdown is a rendering of that same object. Validation exposes `integrity_ok` separately from `operational_readiness_ok`.
- CLI: `python3 cre_refresh_report.py --since 2026-07-18T13:00:00Z --env-file /path/to/equire.env --format markdown --out /tmp/cre-refresh.md`.

- [ ] **Step 1: Write failing pure-function tests**

Create `tests/test_cre_refresh_report.py` with these contract tests:

```python
import cre_refresh_report as report

def test_validate_since_normalizes_z_suffix():
    assert report.validate_since("2026-07-18T13:00:00Z") == "2026-07-18T13:00:00+00:00"

def test_validate_since_rejects_naive_timestamp():
    try:
        report.validate_since("2026-07-18T13:00:00")
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive timestamp must fail")

def test_render_markdown_includes_inventory_and_queue():
    data = {
        "since": "2026-07-18T13:00:00+00:00",
        "inventory": {"active_total": 10, "created_since": 2, "refreshed_since": 7, "refreshed_pct": 70.0},
        "events_by_type": {"new": 3},
        "source_index": {"seen_since": 8},
        "queue_by_source": [{"source_key": "svn", "pending": 4}],
    }
    text = report.render_markdown(data)
    assert "## Inventory" in text
    assert "| active_total | 10 |" in text
    assert "| svn | 4 |" in text

def test_build_queries_keeps_since_parameter_out_of_sql_literal():
    queries = report.build_queries()
    assert ":since" in queries["inventory"]
    assert "2026-" not in "\n".join(queries.values())
```

- [ ] **Step 2: Run the new test file and confirm it fails**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest -q tests/test_cre_refresh_report.py
```

Expected: import failure because `cre_refresh_report.py` does not yet exist.

- [ ] **Step 3: Implement the report with fixed read-only queries**

Create `cre_refresh_report.py`. Use this query map, replacing `:since` only
after `validate_since` returns a timezone-aware ISO string and quoting it with
`cre_ingest.sql_lit`:

```python
def build_queries():
    return {
        "inventory": """
SELECT count(*) AS active_total,
       count(*) FILTER (WHERE created_at >= :since) AS created_since,
       count(*) FILTER (WHERE scraped_at >= :since) AS refreshed_since,
       round(100.0 * count(*) FILTER (WHERE scraped_at >= :since) /
             nullif(count(*), 0), 2) AS refreshed_pct
FROM credeals.cre_listings WHERE deleted_at IS NULL;
""",
        "events_by_type": """
SELECT event_type, count(*) AS count
FROM credeals.cre_listing_events
WHERE detected_at >= :since
GROUP BY event_type ORDER BY event_type;
""",
        "source_index": """
SELECT count(*) AS seen_since
FROM credeals.cre_source_index WHERE last_seen >= :since;
""",
        "queue_by_source": """
SELECT source_key, count(*) AS pending
FROM credeals.cre_enrichment_queue
WHERE done_at IS NULL GROUP BY source_key ORDER BY pending DESC, source_key;
""",
    }
```

Implement the command around the existing validation conventions:

```python
def run_query(psql, db_url, sql):
    completed = subprocess.run(
        [psql, db_url, "-X", "-A", "-F", "\t", "-P", "pager=off", "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )
    return parse_tsv(completed.stdout)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--out")
    args = parser.parse_args()
    since = validate_since(args.since)
    db_url = load_db_url(args.env_file)
    data = collect_report(find_psql(), db_url, since)
    text = json.dumps(data, indent=2) if args.format == "json" else render_markdown(data)
    if args.out:
        Path(args.out).write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
    else:
        print(text)
```

`collect_report` must use `sql_lit(since)` to replace only the `:since`
placeholder, call `run_query` once per fixed query, and convert the single-row
inventory and source-index outputs to dictionaries. Do not print `db_url`.

- [ ] **Step 4: Add subprocess-mocked CLI coverage**

Add this test to ensure the command remains read-only and renders a file:

```python
def test_main_writes_markdown_without_printing_database_url(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(report, "load_db_url", lambda _: "postgresql://secret")
    monkeypatch.setattr(report, "find_psql", lambda: "psql")
    monkeypatch.setattr(report, "run_query", lambda *_: [{"active_total": "1"}])
    out = tmp_path / "report.md"
    monkeypatch.setattr(sys, "argv", ["cre_refresh_report.py", "--since", "2026-07-18T13:00:00Z", "--out", str(out)])
    report.main()
    assert out.exists()
    assert "postgresql://secret" not in capsys.readouterr().out
```

Adjust the `run_query` fake by call order if necessary so each fixed query gets
the expected row shape.

- [ ] **Step 5: Run report tests and a live read-only proof**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest -q tests/test_cre_refresh_report.py tests/test_cre_validate.py
python3 cre_refresh_report.py \
  --since 2026-07-18T13:00:00Z \
  --env-file ~/.config/cre/equire.env \
  --format markdown \
  --out /tmp/cre-refresh-report.md
```

Expected: tests pass; the report has inventory, events, source-index, and queue
sections; the command makes no writes and shows no credential.

- [ ] **Step 6: Add operational-readiness validation without weakening integrity validation**

Add queue-health and source-freshness queries to `cre_validate.py`. Preserve the existing `ok` behavior as `integrity_ok`, then emit:

```python
result["integrity_ok"] = result.pop("ok")
result["operational_readiness_ok"] = (
    result["integrity_ok"]
    and result["queue_health"]["pending"] == 0
    and result["queue_health"]["dead"] == 0
    and result["source_freshness"]["stale"] == 0
)
result["ok"] = result["integrity_ok"]
```

Use a `--fresh-within-hours` argument with default `24`; stale means a source-index row is older than that threshold. Pending queue rows and duplicate source URLs must be reported as operational blockers or warnings, not silently converted into referential-integrity failures.

Add these tests to `tests/test_cre_validate.py`:

```python
def test_readiness_is_false_for_queue_backlog_but_integrity_stays_true():
    result = cre_validate.combine_readiness(True, {"pending": 3, "dead": 0}, {"stale": 0})
    assert result == {"integrity_ok": True, "operational_readiness_ok": False, "ok": True}

def test_readiness_is_true_only_when_integrity_queue_and_freshness_are_clean():
    result = cre_validate.combine_readiness(True, {"pending": 0, "dead": 0}, {"stale": 0})
    assert result["integrity_ok"] is True
    assert result["operational_readiness_ok"] is True
```

Run:

```bash
python3 -m pytest -q tests/test_cre_validate.py
```

Expected: `ok` remains the structural integrity result for compatibility, while callers can no longer mistake a valid schema for an operationally ready pipeline.

- [ ] **Step 7: Commit the reporting and readiness surfaces**

```bash
git add scripts/firecrawl-ops/cre_collector/cre_refresh_report.py \
  scripts/firecrawl-ops/cre_collector/tests/test_cre_refresh_report.py \
  scripts/firecrawl-ops/cre_collector/cre_validate.py \
  scripts/firecrawl-ops/cre_collector/tests/test_cre_validate.py
git commit -m "feat(cre): report refresh and readiness health"
```

## Task 4: Reconcile the operator documentation with the verified July 18 state

**Files:**

- Modify: `scripts/firecrawl-ops/cre_collector/START_HERE.md:1-31,99-240,242-289`
- Modify: `scripts/firecrawl-ops/cre_collector/CLAUDE.md:1-55`
- Modify: `docs/firecrawl-ops/references/cre-monitor-subsystem.md:1-36,79-172`
- Modify: `scripts/firecrawl-ops/cre_collector/launchd/README.md:1-43,47-105`
- Modify: `tasks/2026-07-18-cre-listing-refresh/refresh-summary.md`

**Interfaces:**

- Consumes: the July 18 refresh evidence, `cre_refresh_report.py`, the operator runbook, and the source contract in `sources/CLAUDE.md`.
- Produces: one current-state entry point, explicit historical sections, and commands that future operators can execute without treating dated counts or launchd state as live truth.

- [ ] **Step 1: Add a dated current-state banner at the top of `START_HERE.md`**

Insert immediately after the existing ownership/handoff banners:

```markdown
> **Verified refresh snapshot, 2026-07-18:** all 20 supported adapters ran.
> Active inventory was 114,487; 75,992 active rows were fully re-observed;
> 97,587 source records were enumerated; and the comparable-source event rate
> was 5.71%. These are an evidence snapshot, not a promise that the count is
> current now. Recreate it with `cre_refresh_report.py --since <run-start>`.
>
> **Scheduler state:** no CRE launchd tier is authorized to load in this work.
> The next scheduler action requires the operator runbook's Gate 5 approval;
> monitor, enrich, and weekly are the only candidates. Daily remains retired.
```

Change the old “Latest Source Matrix” heading to `Historical Source Matrix
(2026-07-05)` and add this immediately beneath it:

```markdown
Do not update this historical table by hand. Use `cre_refresh_report.py` for
current aggregate counts and save a dated, task-local evidence artifact for a
per-source refresh snapshot.
```

- [ ] **Step 2: Correct the monitor subsystem’s stale NAI and scheduler statements**

Replace gotcha 6 with:

```markdown
6. **NAI full and monitor use the same source-eligibility rule.** Both retain
   only `FOR_SALE_ON_MARKET` rows from the bulk public `publicPosts` feed. The
   provider label is an eligibility filter, not a terminal EQUIRE status, and
   monitor never activates it. A regression test must keep the eligible identity
   sets identical. `colliers-main` remains detail-sparse and may differ in its
   transaction classification; its monitor artifact still goes only to
   `cre_monitor.py`.
```

Replace the assertions that launchd tiers are loaded/running with:

```markdown
The templates and dispatchers are implemented, but the scheduler is disabled
pending the operator runbook's Gate 5 approval. Verify the actual host state
with `bash scripts/firecrawl-ops/cre_collector/cre_status.sh`; never infer it
from this document or from historical run markers.
```

- [ ] **Step 3: Add the report command to the two operational guides**

Add this exact command under the existing preflight commands in `START_HERE.md`
and `CLAUDE.md`:

```bash
python3 cre_refresh_report.py \
  --since 2026-07-18T13:00:00Z \
  --env-file ~/.config/cre/equire.env \
  --format markdown \
  --out /tmp/cre-refresh-report.md
```

State that the timestamp must be the start of the specific refresh being
reported, not a copied historical timestamp.

- [ ] **Step 4: Make the queue follow-up concrete and safe**

In `refresh-summary.md`, replace the generic remaining-queue sentence with a
table containing the verified current source counts: Lee 1,189; SVN 882;
Cushman 237; CBRE 192; Newmark 156; Franklin Street 12; Hanley 4. Add the
following command and safety explanation:

```bash
python3 cre_enrich.py --source lee-associates --batch 50 \
  --env-file ~/.config/cre/equire.env
```

Explain that each source requires an isolated dry run/readback first; the
unfiltered queue worker is not re-enabled merely because this backlog exists;
and absence of a safe detail payload means retry/dead-letter, never a thin
URL-only overwrite.

Correct every claim that weekly "recovers dead-lettered queue rows." The
current weekly dispatcher runs a full additive collection only; its normal
`cre_enrich.py` claim predicate accepts `attempts < 5`, so it cannot recover a
dead-letter row. Replace those claims with: "weekly may refresh the underlying
listing through a full source pass; dead-letter recovery requires a separate,
source-scoped, reviewed recovery command." Do not add an automatic retry reset
in this documentation task.

- [ ] **Step 5: Verify documentation links and stale claims**

Run:

```bash
rg -n "monitor emits supersets for `nai-global`|launchd tiers are loaded|Both launchd tiers now execute|13,779-record|12,517" \
  scripts/firecrawl-ops/cre_collector \
  docs/firecrawl-ops/references/cre-monitor-subsystem.md \
  tasks/2026-07-18-cre-listing-refresh
git diff --check
```

Expected: no active operator guide claims that NAI monitor is a superset or
that launchd is currently loaded; historical archival references remain only
when explicitly labelled historical.

- [ ] **Step 6: Commit the documentation reconciliation**

```bash
git add scripts/firecrawl-ops/cre_collector/START_HERE.md \
  scripts/firecrawl-ops/cre_collector/CLAUDE.md \
  docs/firecrawl-ops/references/cre-monitor-subsystem.md \
  scripts/firecrawl-ops/cre_collector/launchd/README.md \
  tasks/2026-07-18-cre-listing-refresh/refresh-summary.md
git commit -m "docs(cre): reconcile post-refresh operations guidance"
```

## Task 5: Conduct the source-specific enrichment admission pass

**Files:**

- Modify: `tasks/2026-07-18-cre-listing-refresh/refresh-summary.md` only if a source passes or fails its evidence gate.
- Create: `tasks/2026-07-18-cre-listing-refresh/enrichment-admission-<source>.md` for each reviewed source.

**Interfaces:**

- Consumes: `cre_enrich.py --source <source_key> --batch <N>`, the queue report from Task 3, and `cre_validate.py`.
- Produces: a source-specific decision of `drain`, `retry`, or `hold`; no global queue action.

- [ ] **Step 1: Start with the largest queue using a dry-run claim**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 cre_enrich.py --source lee-associates --batch 50 --dry-run \
  --env-file ~/.config/cre/equire.env
```

Expected: claim SQL contains `source_key = 'lee-associates'`, contains no other
source key, and contains neither `--mark-missing` nor `--activate-status`.

- [ ] **Step 2: Record an admission note before any source write**

Create `tasks/2026-07-18-cre-listing-refresh/enrichment-admission-lee-associates.md`
with this exact decision template, filled from the dry run and a read-only
sample of the 50 URLs:

```markdown
# Lee Associates enrichment admission - 2026-07-18

## Evidence

- Queue rows before batch:
- Batch size: 50
- Detail payload fields present: canonical URL / price / size / contacts / documents
- Unsafe payloads or provider failures:

## Decision

`drain` | `retry` | `hold`

## Safety boundary

The worker may only use `cre_ingest.py --in <artifact>`; it must not activate
status, mark missing, invoke OM parsing, or replace a detailed row with a
URL-only record.
```

- [ ] **Step 3: Execute only if the admission decision is `drain`**

Run:

```bash
python3 cre_enrich.py --source lee-associates --batch 50 \
  --env-file ~/.config/cre/equire.env
python3 cre_validate.py --env-file ~/.config/cre/equire.env \
  --format json --out /tmp/cre-validate-after-lee.json
```

Expected: enrichment exits 0, validation JSON reports `"ok": true`, queue
completion is URL-matched, and the report does not show a status or deletion
mutation.

- [ ] **Step 4: Repeat the same bounded gate in descending queue order**

Apply Steps 1-3 independently to `svn`, `cushman-wakefield`, `cbre`,
`newmark`, `franklin-street`, and `hanley`. Use batches of 50 until two
consecutive source-specific batches validate cleanly; only then increase that
source to 200. Do not carry a `drain` decision from one source to another.

- [ ] **Step 5: Commit only durable, non-secret admission records**

```bash
git add tasks/2026-07-18-cre-listing-refresh/enrichment-admission-*.md \
  tasks/2026-07-18-cre-listing-refresh/refresh-summary.md
git commit -m "docs(cre): record enrichment admission decisions"
```

## Task 6: Review, hand off, and only then consider scheduler activation

**Files:**

- Modify: `tasks/2026-07-18-cre-listing-refresh/final-evidence-comment.md`
- Modify: `tasks/2026-07-18-cre-listing-refresh/post-nai-bulk-evidence-comment.md`
- Modify: the existing AGENTIC-1229 issue through a comment after verification.

**Interfaces:**

- Consumes: branch SHA, PR #23 review result, the Task 3 report, exact test output, and any Task 5 admission notes.
- Produces: a reviewer-ready handoff with explicit remaining founder gates.

- [ ] **Step 1: Run the final local verification set**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npm run test:unit
python3 -m pytest -q tests/test_cre_refresh_report.py tests/test_cre_enrich.py \
  tests/test_cre_enrich_psql.py tests/test_monitor.py tests/test_monitor_events.py \
  tests/test_monitor_old_value.py tests/test_cre_monitor_gaps.py tests/test_cre_validate.py
git diff --check
```

Expected: every command exits 0. If the repository-wide hook still fails on the
unrelated missing `apps/api/node_modules/typescript5/package.json`, record it as
a separate environment blocker and do not call it passing CI.

- [ ] **Step 2: Update evidence and Linear without changing routing or state**

Put branch, full SHA, exact commands/results, report artifact path, remaining
queue by source, and scheduler status into the two task evidence files. Then
post the same concise content through:

```bash
linear issue comment add AGENTIC-1229 \
  --body-file tasks/2026-07-18-cre-listing-refresh/final-evidence-comment.md
```

Do not self-assign, alter labels, or mark the issue Done.

- [ ] **Step 3: Obtain review and merge approval before any merge**

Keep PR #23 draft until the batch is coherent and review-ready. Run the
required adversarial review before requesting merge. Merge only after Cayman
explicitly says to merge this branch to main.

- [ ] **Step 4: Treat scheduler activation as a separate decision**

After a merged SHA is verified, run `bash cre_status.sh --expected-sha <merged-sha>`
and attach the output to the operator record. Do not run
`install_launchd.sh --load ...` unless Gate 5 has the named Cayman approval for
the coordinator, credentials, observation window, rollback owner, and only the
monitor/enrich/weekly tier set.

## Self-Review

**Spec coverage:** The plan covers the requested finalization and refinement work: exact current documentation, a regression guard for the NAI flaw found in the refresh, repeatable live metrics, safe handling of the remaining enrichment backlog, PR/Linear handoff, and the separate scheduler approval boundary.

**Completeness scan:** Every source-specific queue action uses an exact command and a recorded admission decision; the plan intentionally contains no deferred implementation markers.

**Type consistency:** `srcNaiGlobalWithSourceIds` takes the existing `Tx`, `number`, and `boolean` parameters plus `number[]`; the public `srcNaiGlobal` signature remains unchanged. `cre_refresh_report.py` consumes only existing `cre_ingest` helpers and emits the documented report object.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-18-cre-post-refresh-hardening.md`. Two execution options:

1. **Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
