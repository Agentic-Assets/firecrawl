Part of [AGENTIC-5255](https://linear.app/agenticassets/issue/AGENTIC-5255).

# Listing history draft

This draft captures three representations inside the existing canonical ingest
transaction: the previous parent and its contact, document, image, media and
link rows; the staged collector observation; and the resulting database state.
The staging representation is collector output, not a claim about original
provider wire bytes or a complete source inventory. `captured_at` is the
snapshot time; source and collector clocks remain in their original payloads.
For an explicit complete-inventory retirement, the preimage and resulting state
are captured around the existing lifecycle transition. No disappearance is
inferred from an incomplete collection by this change.

The stable artifact run key, brokerage, external ID, phase and SHA-256 of the
JSON payload identify an exact replay. Each different payload remains separate;
physical child IDs and database clocks may cause a later physical state to be
distinct even when the displayed values look similar. Existing observations
that were overwritten before capture cannot be reconstructed. Inventory-only
source-index changes and specialized write paths that bypass `build_sql` are
not covered. This is not a document-byte archive or OM extraction store.

The private `credeals.cre_listing_snapshots` table is independent of the
separately gated `cre_source_evidence` writer; this draft does not activate that
writer. Migration 022 is excluded from the generic migration runner and has a
separate owner approval guard. The table enables RLS, revokes public and
application roles, and grants the existing service role select/insert only.
These statements do not establish production effective privileges or isolation:
review schema defaults, role inheritance, RLS, and actual service callers before
any separately authorized application. No production DDL follows from this PR.

A conservative in-database admission scan refuses a whole snapshot when it
finds credential-like keys, known signed URL parameters, URL userinfo, unknown
percent-encoded URL queries, or escaped Unicode it cannot inspect. The receipt
reports the refusal without logging the payload; normal canonical updates can
continue. This scanner is not the existing source-evidence admission workflow,
does not prove the absence of all secrets or private material, and needs
independent privacy review before any activation. False positives are possible.
No source-specific price-hiding rule or new source access authority is added.

There is no automatic deletion, time-to-live, fixed per-record cap or fixed
history cap. The table grows with new physical snapshots, and backup capacity,
restore procedure, monitoring and storage ownership must be qualified before
production use. An absent table, detected sensitive content, unavailable child
schema, or a caught insertion failure is counted by brokerage and phase in the
post-commit `CRE_LISTING_HISTORY` receipt and checkpoint ingest manifest. A
missing, malformed or unbound receipt is `unknown`; it does not stop eligible
sources. These counts are not a per-record retry queue or proof of a backup.
Existing upstream artifact limits, PostgreSQL value limits, memory and statement
timeouts still apply. A database-wide outage, cancellation, connection loss or
full shared volume can abort canonical work; an in-transaction exception handler
cannot guarantee isolation from those failures.

Before use, independently review source-content handling, snapshot access and
backup/restore, realistic size and throughput, omission reporting, migration and
rollback. Run the isolated PostgreSQL proof from a standalone no-hardlinks clone:
`python tests/run_listing_history_postgres.py`. The proof uses synthetic data
and a disposable network-none container, not production credentials or data.