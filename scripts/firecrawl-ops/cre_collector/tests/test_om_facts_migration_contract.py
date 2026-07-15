"""Static contracts for the guarded legacy OM-facts key alignment.

The disposable PostgreSQL runner exercises the live DDL behavior.  These pure
checks keep the runner, migration, and fresh-schema source of truth from
quietly drifting between opt-in contract runs.
"""

from pathlib import Path


SQL_DIR = Path(__file__).resolve().parents[2] / "sql"
RUNNER = SQL_DIR / "000_run_all.sql"
MIGRATION_013 = SQL_DIR / "013_cre_listing_om_facts.sql"
MIGRATION_015 = SQL_DIR / "015_align_om_facts_conflict_key.sql"
POSTGRES_CONTRACT = Path(__file__).with_name("run_om_facts_postgres_contract.sh")


def test_fresh_runner_uses_013_five_column_key_without_015():
    runner = RUNNER.read_text()
    included_files = [
        line.strip()
        for line in runner.splitlines()
        if line.lstrip().startswith("\\i ")
    ]

    assert "\\i 013_cre_listing_om_facts.sql" in included_files
    assert "\\i 015_align_om_facts_conflict_key.sql" not in included_files
    assert (
        "(listing_id, fact_group, fact_key, source_doc_url, parser_version) "
        "NULLS NOT DISTINCT"
    ) in MIGRATION_013.read_text()


def test_legacy_alignment_refuses_without_explicit_psql_approval():
    migration = MIGRATION_015.read_text()

    assert "CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT" in migration
    assert "\\if :CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT" in migration
    assert "\\quit 3" in migration
    assert "REFUSED: migration 015 requires" in migration


def test_disposable_postgres_contract_covers_refusal_and_approved_idempotence():
    contract = POSTGRES_CONTRACT.read_text()

    assert "expected fresh schema to use the canonical five-column key" in contract
    assert "legacy alignment unexpectedly ran without approval" in contract
    assert "CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1" in contract
    assert contract.count(' < "$ALIGN_MIGRATION"') >= 3
