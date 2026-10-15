Parent: [AGENTIC-4266](https://linear.app/agenticassets/issue/AGENTIC-4266). Repair: [AGENTIC-4268](https://linear.app/agenticassets/issue/AGENTIC-4268).

# Interra legacy identity transition

The historical generic collector used `listing/<slug>` as an Interra external
ID. The authoritative WordPress feed supplies a numeric `ID`. Ordinary ingestion
upserts by brokerage and external ID, so a fresh complete feed can still insert
a second record for an existing property's exact URL. Provider completeness
and database identity compatibility are separate checks.

The failed Windows trial is retained on AGENTIC-4266. Its fresh provider feed
had unique IDs and URLs; final database quality caught overlaps with historical
slug records after ingestion. A successful write or exact-generation readback
does not override that failed quality result. Do not retry that generation,
rewrite its manifest, omit overlapping listings or replace native IDs with URL
slugs to force acceptance.

`cre_ingest.py` now refuses existing and same-batch Interra cross-identity URL
collisions inside the existing lifecycle transaction, before listing upsert.
The refusal changes no identity, lifecycle state or other source's behavior.
Exact URL equality identifies a conflict; it does not authorize an automatic
merge. Old slug IDs missing from a numeric-ID artifact do not prove that their
properties disappeared.

## Bounded repair procedure

`cre_repair_interra_identity.py` is a one-time, exact-plan tool for the reviewed
22-pair transition. It preserves established listing UUIDs and their contacts,
adopts the exact provider IDs, transfers the specified image rows and retains
the newly inserted duplicate parents as explicitly superseded history. Native
source-index rows remain unchanged. The other historical listings and OM facts
are outside its mutation scope. Preserved contacts remain historical; the
repair does not claim they were fetched again.

The default invocation only writes a private SQL preview and preimage:

```bash
python3 cre_repair_interra_identity.py \
  --plan /private/path/approved-plan.json \
  --artifact /exact/path/sources/interra-realty.json \
  --out-dir /private/path/new-preview-directory
```

Apply additionally requires the founder's approval of the exact plan hash, a
separate owned private operator contract, explicit confirmation, the approved
database target and a clean reviewed deployed SHA. Never manufacture an approval
contract from an implementation plan. Root owns production execution and records
the actual human answer on AGENTIC-4268.

The tool rechecks fresh artifact bytes, exact mappings, complete parent/child
preimages, source-index state and the foreign-key catalog. It holds the canonical
filesystem lease and database lifecycle lock, locks affected rows and applies
one serializable transaction. Refusals and injected failures are tested for
transactional rollback; the private preimage is retained. There is no automatic
post-commit undo command. A reversal after commit needs a separately reviewed
state comparison, especially after a later refresh changes those rows.

If the database outcome is uncertain, the recovery fence stays armed. Inspect
the private attempt, SQL, logs, deterministic audit job and actual database state;
do not automatically retry. A completed repair is followed by a new guarded
Interra refresh, full quality check and exact readback before scheduling.

For future restored adapters, audit the historical ID namespace before the first
live ingest. Fresh inventory proof alone does not establish a safe identity
transition. Keep broader lifecycle remediation and missing-baseline admission
separate from this exact repair.
