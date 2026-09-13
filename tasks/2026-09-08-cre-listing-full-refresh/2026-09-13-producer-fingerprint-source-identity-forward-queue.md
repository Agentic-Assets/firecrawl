# Forward queue after producer fingerprint source identity repair

This is a bounded follow-up list, not publication authorization.

## Required release sequence

- Review and merge Firecrawl PR #45 through the protected repository gate.
- Review GetCREdata PR #34 independently. Apply its forward migration only
  after named shared-schema DDL approval and private rollback evidence exist.
- From a clean immutable merged Firecrawl SHA, perform a new complete
  51-source observation. Do not reconstruct v2 freshness from legacy manifests,
  partial source clocks, or the current listing snapshot.
- Build and validate the exact inventory generation, publish a receipt only
  after all 51 sources pass, then run the separately approved GetCREdata refresh
  and post-publication readback.

## Private preimage before source 1

The checkpoint series collects and commits one source before starting the next;
it is serial partial-commit work, not an all-or-nothing 51-source transaction.
Take and checksum the complete private preimage before source 1. Do not start
from a hand-written source list or guessed brokerage mapping. At the exact clean
release SHA, generate the scope from the policy and the same resolver used by
ingest:

```bash
python3 - <<'PY'
import json
import sys
from pathlib import Path

root = Path.cwd()
collector = root / "scripts/firecrawl-ops/cre_collector"
sys.path.insert(0, str(collector))
import cre_ingest

policy = json.loads((collector / "data/cre-source-policy.json").read_text())
source_keys = list(policy)
assert len(source_keys) == 51
assert set(source_keys) == set(cre_ingest.SOURCE_TO_BROKERAGE)
print(json.dumps({
    "sourceKeys": source_keys,
    "brokerageSlugs": sorted({
        slug for slug, _ in cre_ingest.SOURCE_TO_BROKERAGE.values()
    }),
    "foldedSourcePrefixes": cre_ingest.FOLDED_SOURCE_PREFIXES,
    "inventoryOnly": cre_ingest.INVENTORY_ONLY_SOURCE_DEFINITIONS,
    "canonicalSourceKeySql": cre_ingest.source_key_sql("l", "b").strip(),
    "lifecycleLockSql": cre_ingest.lifecycle_transaction_lock_sql(),
}, sort_keys=True, indent=2))
PY
```

Save that output with the release SHA and its SHA-256. QA's backup query must
load `sourceKeys` and `brokerageSlugs` as data, then build these scopes inside
one repeatable-read, read-only transaction:

```sql
-- Bind the two JSON arrays produced above with psql variables. Substitute the
-- generated canonicalSourceKeySql verbatim for the marked expression.
with release_keys as (
  select pg_catalog.jsonb_array_elements_text(:'source_keys_json'::jsonb)
           as source_key
), release_slugs as (
  select pg_catalog.jsonb_array_elements_text(:'brokerage_slugs_json'::jsonb)
           as slug
), release_brokerages as (
  select b.*
  from credeals.cre_brokerages b
  join release_slugs s using (slug)
), release_listings as (
  select l.*
  from credeals.cre_listings l
  join credeals.cre_brokerages b on b.id = l.brokerage_id
  where b.id in (select id from release_brokerages)
     or (<canonicalSourceKeySql>) in (select source_key from release_keys)
), release_source_index as (
  select si.*
  from credeals.cre_source_index si
  where si.brokerage_id in (select id from release_brokerages)
     or si.source_key in (select source_key from release_keys)
)
select
  (select count(*) from release_brokerages) as brokerages,
  (select count(*) from release_listings) as listings,
  (select count(*) from release_source_index) as source_index;
```

The broad mapped-brokerage predicate is intentional. It captures folded CBRE
Dealflow, JLL Investor, and Colliers Main identities, the inventory-only CBRE
Dealflow and Colliers card/watermark namespaces, and null, stale, or legacy
`source_key` rows under a mapped brokerage. Use the same temporary listing-ID
set to capture full rows from contacts, documents, images, and any installed
media/links tables. Capture full listing and source-index rows, table schemas,
PK/FK/index definitions, counts, and stable digests. Record the pre-run
`cre_scrape_jobs` boundary and the existing event/price-history IDs for the
scoped listings; those three journals are retained, not blanket-restored.

The query is a scope contract, not a backup implementation. QA must verify all
51 source keys, every mapped slug, all three folded prefixes, both
`INVENTORY_ONLY_SOURCE_DEFINITIONS`, and both OR branches return covered rows
before admitting the backup. Optional tables must be discovered with
`to_regclass`; absence is evidence, not an instruction to create them.

## Bounded recovery after a partial series

- Preserve every per-source artifact, manifest, generation ID, deterministic
  `artifact_run_key`, job UUID, and success/failure boundary. Restore the prior
  local `producer-source-health.json` only after database recovery succeeds;
  retain the interrupted series directory.
- Generate rollback SQL from the private preimage and an immediate postimage.
  There is no proven general full-refresh rollback utility. Source-specific
  repair programs such as the Newmark, JLL, and Cushman tools do not authorize
  or implement a 51-source rollback.
- Acquire the existing transaction-wide lifecycle advisory lock from
  `lifecycle_transaction_lock_sql()` and lock rows in ingest order:
  `cre_source_index` before `cre_listings`. Do not introduce a second lock key.
- For a pre-existing row, restore it only if its current row and children still
  match the recorded postimage from the exact source job. Any later timestamp,
  generation, job linkage, or digest drift is concurrent work and makes that
  identity fail closed for manual review.
- Delete a newly inserted listing/source-index identity only when it was absent
  from the preimage and remains bound to the exact artifact generation/source
  postimage. Delete or restore child rows only through that admitted listing-ID
  set. Never bulk-replace a table or all rows for a brokerage.
- Keep `cre_scrape_jobs`, `cre_listing_events`, and
  `cre_listing_price_history` as audit journals by default. Do not erase a
  failed or rolled-back attempt merely to make the run appear absent.
- Recompute counts and digests inside the rollback transaction and again after
  commit. Stop on any mismatch. A failure at source N leaves earlier source
  commits real until each is individually admitted for rollback; later sources
  that never started require no database rollback.
