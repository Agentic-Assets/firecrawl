"""Static contracts for the guarded legacy OM-facts key alignment.

The disposable PostgreSQL runner exercises the live DDL behavior.  These pure
checks keep the runner, migration, and fresh-schema source of truth from
quietly drifting between opt-in contract runs.
"""

import ast
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import cre_ingest as ci


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
    assert "RAISE EXCEPTION" in migration
    assert "migration 015 requires CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1" in migration
    assert "REFUSED: migration 015 requires" in migration


def test_015_is_a_no_op_once_the_getcredata_run_lineage_column_exists():
    migration = MIGRATION_015.read_text()

    assert "SUPERSEDED (AGENTIC-5136/5137)" in migration
    assert "column_name = 'extraction_run_id'" in migration
    assert "WHEN :'om_facts_lineage_migrated'::boolean THEN false" in migration
    assert "\\elif :om_facts_lineage_migrated" in migration


def test_disposable_postgres_contract_covers_refusal_and_approved_idempotence():
    contract = POSTGRES_CONTRACT.read_text()

    assert "expected fresh schema to use the canonical five-column key" in contract
    assert "legacy alignment unexpectedly ran without approval" in contract
    assert "CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1" in contract
    assert contract.count(' < "$ALIGN_MIGRATION"') >= 3


def test_013_creates_the_retired_index_and_comment_only_before_run_lineage():
    migration = MIGRATION_013.read_text()
    guard_start = migration.index("DO $$\nBEGIN\n  IF NOT EXISTS (")
    guard_end = migration.index("END $$;", guard_start)
    guarded = migration[guard_start:guard_end]
    outside = migration[:guard_start] + migration[guard_end:]

    assert "attname = 'extraction_run_id'" in guarded
    assert "CREATE UNIQUE INDEX IF NOT EXISTS cre_listing_om_facts_uq" in guarded
    assert "COMMENT ON TABLE credeals.cre_listing_om_facts IS" in guarded
    # A rerun after GetCREdata drops the five-column index must recreate nothing.
    assert "CREATE UNIQUE INDEX" not in outside
    assert "COMMENT ON TABLE credeals.cre_listing_om_facts IS" not in outside


def _runner_archive_generator():
    contract = POSTGRES_CONTRACT.read_text()
    match = re.search(
        r"cat > \"\$WORK_DIR/archive_stage.py\" <<'PY'\n(.*?)\nPY\n", contract, re.S
    )
    assert match, "runner no longer embeds its archive-stage generator"
    return match.group(1)


def test_runner_does_not_slice_literals_the_generator_no_longer_emits():
    # The runner once sliced the removed OM upsert out of build_sql by literal and
    # raised ValueError. Every literal it searches for must still be generated.
    contract = POSTGRES_CONTRACT.read_text()
    generated = {
        guard: ci.build_sql(
            [], [], datetime(2026, 7, 11, tzinfo=timezone.utc).isoformat(), {"svn"},
            history_guard=guard,
        )
        for guard in (True, False)
    }
    literal = r"(\"(?:[^\"\\]|\\.)*\")"
    needles = [
        ast.literal_eval(m.group(1))
        for pattern in (r"generated\.index\(\s*" + literal, r"marker = " + literal)
        for m in re.finditer(pattern, contract)
    ]
    assert needles, "runner no longer extracts anything from build_sql"
    for needle in needles:
        assert needle in generated[True], needle
    assert "om_upsert" not in contract
    assert "ON CONFLICT (" not in contract


def test_runner_archive_generator_runs_for_both_lineage_stages(tmp_path):
    script = tmp_path / "archive_stage.py"
    script.write_text(_runner_archive_generator())
    collector_dir = Path(__file__).resolve().parents[1]

    outputs = {}
    for stage in ("legacy", "lineage"):
        done = subprocess.run(
            [sys.executable, str(script), str(collector_dir), stage],
            cwd=tmp_path, capture_output=True, text=True, timeout=120,
        )
        assert done.returncode == 0, done.stderr
        outputs[stage] = done.stdout

    for stdout in outputs.values():
        assert "-- (013) Archive final OM facts of retired listings." in stdout
        assert "EXECUTE" in stdout
    assert "ADD COLUMN extraction_run_id uuid" in outputs["lineage"]
    assert "ADD COLUMN extraction_run_id" not in outputs["legacy"]


def test_runner_executes_the_015_no_op_and_013_rerun_on_a_lineage_schema():
    contract = POSTGRES_CONTRACT.read_text()
    tail = contract[contract.index("assert_lineage_schema_untouched() {") :]

    assert "retired five-column index was recreated" in tail
    assert "table comment was rewritten" in tail
    assert 'psql_contract < "$MIGRATION"' in tail
    assert tail.count('-v CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1 < "$ALIGN_MIGRATION"') >= 2
