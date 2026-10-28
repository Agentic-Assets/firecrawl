Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Private source evidence

This is a separate unverified evidence store. It does not promote listing fields, establish publisher authenticity, determine listing status, store original binaries, or write OM facts. Artifacts and results may contain confidential material; keep their parent directories private. The CLI creates new output files exclusively with mode 0600 and prints only counts and the plan digest.

From the collector directory, build a plan without contacting a database:

```sh
python3 -m source_evidence.cli plan --artifact /private/artifact.json --source-key cbre --out /private/plan.json
```

For a Buildout observation artifact also provide `--observed-at 2026-10-28T12:00:00Z`; `--external-id` is an optional caller assertion for document evidence. Review the entire plan and source-retention policy. For approved private retention, prepare a private admission JSON with `schema_version` `cre-source-evidence-admission/v1`, `plan_sha256` matching the plan, `purpose` `private_unverified_evidence`, `data_scope` `public_listing_source`, `content_policy` `reviewed_for_private_retention`, and `source_keys` containing exactly the plan source key. JLL remains refused without its source-specific withheld-content adapter.

Existing single-source collector artifacts can be wrapped offline before planning:

```sh
python3 -m source_evidence.cli convert-collector --artifact /private/rejected.json --source-key svn --out /private/retained.json
python3 -m source_evidence.cli plan --artifact /private/retained.json --source-key svn --out /private/plan.json
```

The wrapper retains every emitted listing, including duplicate or incomplete records and supplied `raw_data`, plus all artifact metadata and errors. It refuses mixed or mismatched sources, and uses the artifact's run-finished clock, not a per-record observation time. Its digest binds canonical JSON, not the original file bytes. Collector records have already passed through source mapping and pruning; this does not recover discarded HTTP responses, prove completeness or establish source acceptance. Empty artifacts with explicit source entries are retained. Conversion and planning are local; private retention admission and database authorization remain separate.

Admission scans HTTP(S) URL tokens in text and object keys for known JLL hosts and credential-bearing query keys, including the reconstructed bounded document text across chunk boundaries; it is not a general secret detector. Conflicting nested declarations of protected source keys are refused. Avison collector evidence is checked against its source-owned asking-price visibility control and projection, including control witnesses in capture context. Withheld records or capture context that the projection would change are refused, including malformed or contradictory controls; use a source-owned projected artifact before requesting admission. Independent rent, cap rate and NOI remain eligible. Other Avison evidence shapes need an adapter before admission. Local planning alone does not establish that any historical Avison plan is safe to retain.

Once the separate database migration, roles, permissions and a specific write are authorized, bind an explicit environment file and target fingerprint:

```sh
python3 -m source_evidence.cli append --plan /private/plan.json --admission /private/admission.json --env-file /private/db.env --expected-db-target-sha256 FINGERPRINT --apply
```

The append is one transaction using `cre_source_evidence_writer` and the append function. The target fingerprint identifies the URL target, not the database's identity beyond that URL. Credentials stay out of command arguments. The function accepts exact retries and rejects divergent reuse. A timeout, connection failure or interrupted client can leave the commit outcome unknown. Reconcile against an authorized reader's database readback of the exact capture ID and item numbers; this CLI's text search is not a complete capture readback. When appropriate, replay the same unchanged plan and admission against the same target; do not alter rows or assume the failed client means rollback. The reviewed admission is an operator attestation, not cryptographic identity or publisher authentication.

Search requires the separate reader role and uses PostgreSQL `simple` full-text search on extracted text chunks only:

```sh
python3 -m source_evidence.cli search --query 'industrial warehouse' --mode phrase --limit 10 --out /private/results.json --env-file /private/db.env --expected-db-target-sha256 FINGERPRINT
```

Results are bounded to 25 chunks, not whole documents; a phrase crossing chunks will not match. Search does not prove completeness and content should be reviewed before disclosure. Queries may appear in the local process arguments. No command here applies DDL, downloads sources, modifies listing lifecycle, or changes scheduling.

Structured facts use PostgreSQL JSONB containment and a separate GIN index. For
example, an owner-only query file containing
`[{"kind":"document_outcome","value":{"extraction":{"status":"ocr_needed"}}}]`
finds retained OCR-needed outcomes without treating them as complete documents:

```sh
python3 -m source_evidence.cli search-facts --contains-file /private/query.json --out /private/matches.json --env-file /private/db.env --expected-db-target-sha256 FINGERPRINT
python3 -m source_evidence.cli get --capture-id CAPTURE_ID --item-no 0 --out /private/event.json --env-file /private/db.env --expected-db-target-sha256 FINGERPRINT
```

Fact search returns bounded match metadata. `get` returns one complete event,
including its ordered facts and optional text, into an exclusive private file.
Each text chunk records its ordinal, total count and original full-text digest;
verify all ordinals and the reconstructed UTF-8 hash before claiming complete
reconstruction. Chunk windows are not page citations. Original document bytes
and publisher identity are not independently verified by stored text hashes.
