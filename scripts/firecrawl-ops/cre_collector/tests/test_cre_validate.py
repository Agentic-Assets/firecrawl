"""test_cre_validate.py: unit tests for cre_validate.py.

Coverage targets:
  - parse_tsv
  - normalize_warning
  - markdown_table
  - render_markdown
  - run_query (monkeypatched subprocess.run)
  - run_queries single-snapshot batching (monkeypatched subprocess.run)
  - main (monkeypatched load_db_url + find_psql + run_queries)

No network, no live DB, no psql connection.  All subprocess calls are
intercepted via monkeypatch.
"""

import json
import sys
from pathlib import Path

import pytest

# conftest.py already puts cre_collector/ on sys.path.
import cre_validate
from cre_validate import (
    QUERIES,
    SOURCE_KEY_SQL,
    markdown_table,
    normalize_warning,
    parse_query_batch,
    parse_tsv,
    render_markdown,
    run_query,
    run_queries,
)


def test_child_quality_queries_cover_media_and_links():
    assert "'media'" in QUERIES["child_counts"]
    assert "'links'" in QUERIES["child_counts"]
    assert "media_bad_url" in QUERIES["bad_child_urls"]
    assert "link_bad_url" in QUERIES["bad_child_urls"]
    assert "SELECT 'media'" in QUERIES["orphans"]
    assert "SELECT 'links'" in QUERIES["orphans"]


# ---------------------------------------------------------------------------
# parse_tsv
# ---------------------------------------------------------------------------


def test_parse_tsv_empty_string():
    assert parse_tsv("") == []


def test_parse_tsv_whitespace_only():
    assert parse_tsv("   \n   \n") == []


def test_parse_tsv_header_only():
    # One line = just headers, no data rows -> empty list
    assert parse_tsv("metric\tvalue\n") == []


def test_parse_tsv_two_rows():
    tsv = "metric\tvalue\nfoo\t42\nbar\t99\n"
    rows = parse_tsv(tsv)
    assert rows == [{"metric": "foo", "value": "42"}, {"metric": "bar", "value": "99"}]


def test_parse_tsv_row_keys_match_header():
    tsv = "a\tb\tc\n1\t2\t3\n"
    rows = parse_tsv(tsv)
    assert len(rows) == 1
    assert set(rows[0].keys()) == {"a", "b", "c"}


def test_parse_tsv_ragged_row_truncates():
    """A row with fewer values than headers is silently truncated (zip stops short)."""
    tsv = "a\tb\tc\n1\t2\n"   # only two values for three-column header
    rows = parse_tsv(tsv)
    assert len(rows) == 1
    assert rows[0] == {"a": "1", "b": "2"}   # 'c' key absent (truncated)
    assert "c" not in rows[0]


def test_parse_tsv_extra_values_are_truncated():
    """A row with MORE values than headers: extra values are dropped (zip behavior)."""
    tsv = "a\tb\n1\t2\t3\t4\n"
    rows = parse_tsv(tsv)
    assert rows == [{"a": "1", "b": "2"}]


def test_parse_tsv_single_column():
    tsv = "metric\nfoo\nbar\n"
    rows = parse_tsv(tsv)
    assert rows == [{"metric": "foo"}, {"metric": "bar"}]


def test_parse_tsv_blank_lines_skipped():
    """Blank lines in the middle are filtered before splitting."""
    tsv = "a\tb\n\n1\t2\n\n3\t4\n"
    rows = parse_tsv(tsv)
    assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


# ---------------------------------------------------------------------------
# normalize_warning
# ---------------------------------------------------------------------------


def test_normalize_warning_none_returns_none():
    assert normalize_warning(None) is None


def test_normalize_warning_empty_string_returns_none():
    assert normalize_warning("") is None


def test_normalize_warning_collation_mismatch():
    raw = "WARNING:  database 16384: collation version mismatch\nDETAIL: blah"
    result = normalize_warning(raw)
    assert result == (
        "database collation version mismatch warning "
        "(known project-level warning; validation queries still completed)"
    )


def test_normalize_warning_generic_collapses_to_one_line():
    raw = "  some error  \n  extra line  \n"
    result = normalize_warning(raw)
    assert "\n" not in result
    assert "some error" in result
    assert "extra line" in result


def test_normalize_warning_single_line_strips():
    result = normalize_warning("  psql: error: connection refused  ")
    assert result == "psql: error: connection refused"


def test_normalize_warning_does_not_contain_collation_keyword_passes_through():
    raw = "WARNING: some other thing happened"
    result = normalize_warning(raw)
    assert result is not None
    assert "some other thing" in result


# ---------------------------------------------------------------------------
# markdown_table
# ---------------------------------------------------------------------------


def test_markdown_table_empty_rows():
    assert markdown_table([]) == "_No rows._\n"


def test_markdown_table_single_row_structure():
    rows = [{"metric": "foo", "value": "42"}]
    out = markdown_table(rows)
    lines = out.strip().splitlines()
    # header line
    assert "metric" in lines[0]
    assert "value" in lines[0]
    # separator line
    assert "---" in lines[1]
    # data line
    assert "foo" in lines[2]
    assert "42" in lines[2]


def test_markdown_table_separator_has_correct_column_count():
    rows = [{"a": "1", "b": "2", "c": "3"}]
    out = markdown_table(rows)
    sep_line = out.strip().splitlines()[1]
    assert sep_line.count("---") == 3


def test_markdown_table_none_value_renders_as_empty_string():
    rows = [{"metric": "x", "value": None}]
    out = markdown_table(rows)
    # None should render as empty string, not the word "None"
    assert "None" not in out
    lines = out.strip().splitlines()
    data_line = lines[2]
    assert "x" in data_line


def test_markdown_table_multiple_rows():
    rows = [{"k": "a"}, {"k": "b"}, {"k": "c"}]
    out = markdown_table(rows)
    assert out.count("| a |") == 1
    assert out.count("| b |") == 1
    assert out.count("| c |") == 1


def test_markdown_table_ends_with_newline():
    rows = [{"x": "1"}]
    assert markdown_table(rows).endswith("\n")


def test_markdown_table_pipe_delimited():
    rows = [{"col1": "v1", "col2": "v2"}]
    lines = markdown_table(rows).strip().splitlines()
    for line in lines:
        assert line.startswith("|") or "---" in line


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


def _minimal_report(psql_warnings=None, query_rows=None):
    """Build a minimal valid report dict."""
    if query_rows is None:
        query_rows = {k: [] for k in QUERIES}
    return {
        "generated_at": "2026-06-15T00:00:00+00:00",
        "env_file": "/fake/.env.local",
        "queries": query_rows,
        "psql_warnings": psql_warnings if psql_warnings is not None else [],
    }


def test_render_markdown_contains_generated_at():
    report = _minimal_report()
    md = render_markdown(report)
    assert "2026-06-15T00:00:00+00:00" in md


def test_source_counts_separates_inventory_and_detail_observation():
    sql = QUERIES["source_counts"]
    assert "inventoryObservedAt" in sql
    assert "latest_inventory_observed_at" in sql
    assert "latest_inventory_batch_active" in sql
    assert "detail_unavailable" in sql


def test_source_key_inference_covers_preserved_and_merged_payloads():
    assert "latestInventoryObservation,sourceKey" in SOURCE_KEY_SQL
    assert "latestInventoryObservation,primary,sourceKey" in SOURCE_KEY_SQL
    assert "latestInventoryObservation,secondary_pass,sourceKey" in SOURCE_KEY_SQL
    assert "primary,sourceKey" in SOURCE_KEY_SQL
    assert "secondary_pass,sourceKey" in SOURCE_KEY_SQL
    assert SOURCE_KEY_SQL.index("external_id LIKE 'investor:%'") < SOURCE_KEY_SQL.index(
        "latestInventoryObservation,sourceKey"
    )
    assert SOURCE_KEY_SQL.index(
        "latestInventoryObservation,sourceKey"
    ) < SOURCE_KEY_SQL.index("l.raw_data->>'sourceKey'")


def test_persisted_freshness_provenance_reads_every_supported_raw_shape():
    fixtures = {
        "flat": {
            "inventoryObservedAt": "2026-07-31T01:00:00Z",
            "detailObservedAt": "2026-07-31T01:01:00Z",
            "freshnessProvenance": {
                "generationId": "flat-generation",
                "detailScope": "source_native_public_record",
                "cacheDisposition": "live",
            },
        },
        "primary": {
            "primary": {
                "inventoryObservedAt": "2026-07-31T02:00:00Z",
                "detailObservedAt": "2026-07-31T02:01:00Z",
                "freshnessProvenance": {
                    "generationId": "primary-generation",
                    "detailScope": "detail_page",
                    "cacheDisposition": "generation_cache",
                },
            }
        },
        "secondary-pass": {
            "secondary_pass": {
                "inventoryObservedAt": "2026-07-31T03:00:00Z",
                "detailObservedAt": "2026-07-31T03:01:00Z",
                "freshnessProvenance": {
                    "generationId": "secondary-generation",
                    "detailScope": "detail_page",
                    "cacheDisposition": "source_revision_cache",
                },
            }
        },
        "latest-inventory-observation": {
            "latestInventoryObservation": {
                "inventoryObservedAt": "2026-07-31T04:00:00Z",
                "detailObservedAt": "2026-07-31T04:01:00Z",
                "freshnessProvenance": {
                    "generationId": "latest-generation",
                    "detailScope": "authoritative_inventory_feed",
                    "cacheDisposition": "live",
                },
            }
        },
    }

    for name, raw_data in fixtures.items():
        provenance = cre_validate.persisted_freshness_provenance(raw_data)
        assert provenance["generation_id"] == f"{name.split('-')[0]}-generation"
        assert provenance["inventory_observed_at"].endswith("00:00Z")
        assert provenance["detail_observed_at"].endswith("01:00Z")
        assert provenance["detail_scope"]
        assert provenance["cache_disposition"]


def test_persisted_freshness_provenance_never_uses_scraped_at_as_detail_proof():
    assert cre_validate.persisted_freshness_provenance({}) == {
        "inventory_observed_at": None,
        "detail_observed_at": None,
        "generation_id": None,
        "detail_scope": None,
        "cache_disposition": None,
    }


def test_freshness_generations_groups_readback_by_persisted_generation():
    sql = QUERIES["freshness_generations"]
    assert "l.raw_data #> '{latestInventoryObservation}'" in sql
    assert "l.raw_data #> '{latestInventoryObservation,primary}'" in sql
    assert "l.raw_data #> '{latestInventoryObservation,secondary_pass}'" in sql
    assert "l.raw_data #> '{primary}'" in sql
    assert "l.raw_data #> '{secondary_pass}'" in sql
    assert "jsonb_to_record(" in sql
    assert 'provenance."generationId"' in sql
    assert 'provenance."detailScope"' in sql
    assert 'provenance."cacheDisposition"' in sql
    assert 'observed."detailObservedAt"' in sql
    assert "source_policy (source_key, evidence_class, detail_claim)" in sql
    assert "missing_persisted_detail_proof" in sql
    assert 'observed."inventoryObservedAt"' in sql
    assert "GROUP BY" in sql
    assert "earliest_inventory_observed_at" in sql
    assert "earliest_detail_observed_at" in sql
    assert "l.scraped_at" not in sql
    assert "detail_scraped_at" not in sql
    assert "latest_inventory_batch_active" not in sql


def test_freshness_generations_materializes_once_and_has_no_presentation_sort():
    sql = QUERIES["freshness_generations"]
    assert "raw AS MATERIALIZED" in sql
    assert "OFFSET 0" in sql
    assert "ORDER BY active.source_key" not in sql
    assert sql.rstrip().endswith("active.generation_id;")
    assert "AS detail_scopes" in sql
    assert "AS cache_dispositions" in sql


def test_inventory_only_index_reports_each_declarative_source_namespace():
    sql = QUERIES["inventory_only_index"]
    assert "credeals.cre_source_index" in sql
    assert "dealflow:card:%" in sql
    assert "salestracker:card:%" in sql
    assert "definitions.external_id_like" in sql
    assert "definitions.watermark_external_id" in sql
    assert "cbre-dealflow" in sql
    assert "colliers" in sql
    assert "coalesce(summary.active, 0)" in sql
    assert "latest_batch_active" in sql
    assert "latest_enumerated_at" in sql
    assert "dealflow:scope:inventory-only-watermark" in sql
    assert "salestracker:scope:inventory-only-watermark" in sql
    assert "scope_watermark_at" in sql


def test_primary_child_conflicts_checks_contacts_and_images():
    sql = QUERIES["primary_child_conflicts"]
    assert "cre_listing_contacts" in sql
    assert "cre_listing_images" in sql
    assert "HAVING count(*) > 1" in sql


def test_quality_by_source_exposes_absolute_url_and_economic_defect_counts():
    sql = QUERIES["quality_by_source"]
    for field in (
        "missing_canonical_url",
        "bad_canonical_url",
        "sale_price_flags",
        "lease_rate_min_flags",
        "lease_rate_max_flags",
    ):
        assert field in sql
    assert "canonical_url IS NULL OR btrim(canonical_url) = ''" in sql
    assert "sale_price_usd > 20000000000" in sql


def test_render_markdown_credentials_not_in_output():
    """The env_file path appears but the URL value must not."""
    report = _minimal_report()
    report["env_file"] = "/some/path/.env.local"
    md = render_markdown(report)
    assert "/some/path/.env.local" in md
    assert "Values were not printed" in md


def test_render_markdown_no_psql_warnings_section_when_empty():
    report = _minimal_report(psql_warnings=[])
    md = render_markdown(report)
    assert "## psql Warnings" not in md


def test_render_markdown_psql_warnings_section_present():
    report = _minimal_report(psql_warnings=["database collation version mismatch warning"])
    md = render_markdown(report)
    assert "## psql Warnings" in md
    assert "collation version mismatch" in md


def test_render_markdown_all_query_labels_present():
    """Every key in QUERIES must produce a heading in the rendered output."""
    labels = {
        "totals": "Totals",
        "source_counts": "Source Counts",
        "freshness_generations": "Freshness Generations",
        "inventory_only_index": "Inventory-Only Source Index",
        "quality_by_source": "Quality By Source",
        "duplicates": "Duplicate Checks",
        "child_counts": "Child Counts",
        "bad_child_urls": "Bad Child URLs",
        "primary_child_conflicts": "Primary Child Conflicts",
        "orphans": "Child Orphans",
        "search_smoke": "Search Smoke",
    }
    report = _minimal_report()
    md = render_markdown(report)
    for label in labels.values():
        assert label in md, f"expected heading '{label}' in rendered markdown"


def test_render_markdown_no_rows_placeholder_for_empty_queries():
    report = _minimal_report()
    md = render_markdown(report)
    assert "_No rows._" in md


def test_render_markdown_data_rows_in_output():
    query_rows = {k: [] for k in QUERIES}
    query_rows["totals"] = [{"metric": "cre_listings_active", "value": "87328"}]
    report = _minimal_report(query_rows=query_rows)
    md = render_markdown(report)
    assert "87328" in md
    assert "cre_listings_active" in md


# ---------------------------------------------------------------------------
# run_query  (monkeypatched subprocess.run)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_query_wraps_sql_in_repeatable_read_only_transaction(monkeypatch):
    """The SQL passed to psql must use a stable read-only snapshot."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["kwargs"] = kwargs
        captured["script_path"] = Path(argv[argv.index("-f") + 1])
        captured["script"] = captured["script_path"].read_text()
        return _FakeProc(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre_validate.subprocess, "run", fake_run)
    run_query("psql", "postgres://SENTINEL", "SELECT 1;")
    script = captured["script"]
    assert script.startswith(
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
    )
    assert script.strip().endswith("ROLLBACK;")
    assert "SELECT 1;" in script
    assert "input" not in captured["kwargs"]
    assert "stdin" not in captured["kwargs"]
    assert not captured["script_path"].exists()


def test_run_query_sql_content_inside_wrapper(monkeypatch):
    """The original SQL appears between BEGIN READ ONLY and ROLLBACK."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["script"] = Path(argv[argv.index("-f") + 1]).read_text()
        return _FakeProc(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre_validate.subprocess, "run", fake_run)
    run_query("psql", "postgres://SENTINEL", "SELECT count(*) FROM foo;")
    script = captured["script"]
    begin_pos = script.index(
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"
    )
    rollback_pos = script.index("ROLLBACK;")
    sql_pos = script.index("SELECT count(*) FROM foo;")
    assert begin_pos < sql_pos < rollback_pos


def test_run_query_argv_contains_format_flags(monkeypatch):
    """psql argv must include -F, tab separator, -A, and -v ON_ERROR_STOP=1."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre_validate.subprocess, "run", fake_run)
    run_query("psql", "postgres://SENTINEL", "SELECT 1;")
    argv = captured["argv"]
    assert "-F" in argv
    assert "\t" in argv
    assert "-A" in argv
    assert "-f" in argv
    assert "-v" in argv
    assert "ON_ERROR_STOP=1" in argv


def test_run_query_keeps_database_url_out_of_process_argv(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return _FakeProc(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre_validate.subprocess, "run", fake_run)
    run_query("psql", "postgres://user:secret@db.example.test/cre", "SELECT 1;")
    assert all("secret" not in arg for arg in captured["argv"])
    assert captured["env"]["PGHOST"] == "db.example.test"
    assert captured["env"]["PGDATABASE"] == "cre"
    assert captured["env"]["PGUSER"] == "user"
    assert captured["env"]["PGPASSWORD"] == "secret"


def test_run_query_keeps_uri_only_options_in_credential_free_dbname(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return _FakeProc(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre_validate.subprocess, "run", fake_run)
    run_query(
        "psql",
        (
            "postgres://user:secret@db.example.test/cre"
            "?sslmode=require&keepalives=1&fallback_application_name=a+b"
        ),
        "SELECT 1;",
    )
    dbname = captured["argv"][captured["argv"].index("--dbname") + 1]
    assert "secret" not in dbname
    assert "user" not in dbname
    assert "keepalives=1" in dbname
    assert "fallback_application_name=a%2Bb" in dbname
    assert captured["env"]["PGPASSWORD"] == "secret"
    assert captured["env"]["PGSSLMODE"] == "require"


def test_run_query_uses_text_and_capture_output(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeProc(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre_validate.subprocess, "run", fake_run)
    run_query("psql", "postgres://SENTINEL", "SELECT 1;")
    assert captured["kwargs"].get("text") is True
    assert captured["kwargs"].get("capture_output") is True


def test_run_query_returns_parsed_rows_and_stderr(monkeypatch):
    tsv = "metric\tvalue\ncre_listings_active\t87328\n"

    def fake_run(argv, **kwargs):
        return _FakeProc(returncode=0, stdout=tsv, stderr="some warning")

    monkeypatch.setattr(cre_validate.subprocess, "run", fake_run)
    rows, stderr = run_query("psql", "postgres://SENTINEL", "SELECT 1;")
    assert rows == [{"metric": "cre_listings_active", "value": "87328"}]
    assert stderr == "some warning"


def test_run_query_nonzero_returncode_raises_system_exit(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["script_path"] = Path(argv[argv.index("-f") + 1])
        return _FakeProc(returncode=1, stdout="", stderr="fatal error")

    monkeypatch.setattr(cre_validate.subprocess, "run", fake_run)
    with pytest.raises(SystemExit):
        run_query("psql", "postgres://SENTINEL", "SELECT 1;")
    assert not captured["script_path"].exists()


def test_run_query_db_url_passed_only_in_child_environment(monkeypatch):
    """The DB URL reaches libpq without appearing in the process argument list."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return _FakeProc(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre_validate.subprocess, "run", fake_run)
    run_query("psql", "postgres://user:SENTINEL_URL@db.test/cre", "SELECT 1;")
    assert all("SENTINEL_URL" not in arg for arg in captured["argv"])
    assert captured["env"]["PGPASSWORD"] == "SENTINEL_URL"


def test_parse_query_batch_splits_marked_result_sets():
    output = (
        "__CRE_VALIDATION_QUERY__:first\n"
        "metric\tvalue\n"
        "a\t1\n"
        "__CRE_VALIDATION_QUERY__:second\n"
        "name\tcount\n"
        "b\t2\n"
    )

    parsed = parse_query_batch(output, ("first", "second"))

    assert parsed == {
        "first": [{"metric": "a", "value": "1"}],
        "second": [{"name": "b", "count": "2"}],
    }


def test_run_queries_uses_one_repeatable_read_snapshot(monkeypatch):
    captured = {}
    output = (
        "__CRE_VALIDATION_QUERY__:first\n"
        "metric\tvalue\n"
        "a\t1\n"
        "__CRE_VALIDATION_QUERY__:second\n"
        "metric\tvalue\n"
        "b\t2\n"
    )

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        captured["script_path"] = Path(argv[argv.index("-f") + 1])
        captured["script"] = captured["script_path"].read_text()
        return _FakeProc(returncode=0, stdout=output, stderr="warning")

    monkeypatch.setattr(cre_validate.subprocess, "run", fake_run)
    rows, stderr = run_queries(
        "psql",
        "postgres://SENTINEL",
        {"first": "SELECT 1;", "second": "SELECT 2;"},
    )

    assert rows["first"] == [{"metric": "a", "value": "1"}]
    assert rows["second"] == [{"metric": "b", "value": "2"}]
    assert stderr == "warning"
    assert captured["script"].count("BEGIN TRANSACTION") == 1
    assert (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"
        in captured["script"]
    )
    assert captured["script"].strip().endswith("ROLLBACK;")
    assert "input" not in captured["kwargs"]
    assert "stdin" not in captured["kwargs"]
    assert not captured["script_path"].exists()
    assert "postgres://SENTINEL" not in captured["argv"]
    assert captured["kwargs"]["env"]["PGHOST"] == "sentinel"


# ---------------------------------------------------------------------------
# main  (monkeypatched load_db_url + find_psql + run_queries)
# ---------------------------------------------------------------------------

_DUMMY_ROWS = [{"metric": "cre_listings_active", "value": "99"}]


def _patch_main(monkeypatch):
    """Patch all I/O in main() so nothing connects."""
    monkeypatch.setattr(cre_validate, "load_db_url", lambda env_file: ("postgres://SENTINEL", "/fake/.env.local"))
    monkeypatch.setattr(cre_validate, "find_psql", lambda: "psql")
    monkeypatch.setattr(
        cre_validate,
        "run_queries",
        lambda psql, url, queries: (
            {name: _DUMMY_ROWS for name in queries},
            "",
        ),
    )


def test_main_markdown_format_does_not_print_sentinel(monkeypatch, capsys):
    _patch_main(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "markdown"])
    cre_validate.main()
    out = capsys.readouterr().out
    assert "SENTINEL" not in out


def test_main_json_format_does_not_print_sentinel(monkeypatch, capsys):
    _patch_main(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "json"])
    cre_validate.main()
    out = capsys.readouterr().out
    assert "SENTINEL" not in out


def test_main_rejects_target_drift_before_psql_discovery(monkeypatch):
    monkeypatch.setattr(
        cre_validate,
        "load_db_url",
        lambda _env_file: (
            "postgresql://user:secret@db.example.test/cre",
            "/fake/.env.local",
        ),
    )
    monkeypatch.setattr(
        cre_validate,
        "find_psql",
        lambda: pytest.fail("target drift must fail before psql discovery"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_validate.py",
            "--format",
            "json",
            "--expected-db-target-sha256",
            "0" * 64,
        ],
    )

    with pytest.raises(SystemExit, match="does not match"):
        cre_validate.main()


def test_main_json_format_produces_valid_json(monkeypatch, capsys):
    _patch_main(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "json"])
    cre_validate.main()
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "queries" in parsed
    assert "generated_at" in parsed


def test_main_json_env_file_path_in_output_not_url(monkeypatch, capsys):
    _patch_main(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "json"])
    cre_validate.main()
    out = capsys.readouterr().out
    parsed = json.loads(out)
    # env_file path is included
    assert parsed["env_file"] == "/fake/.env.local"
    # The URL itself must NOT appear in the JSON output
    assert "SENTINEL" not in out


def test_main_markdown_contains_query_headings(monkeypatch, capsys):
    _patch_main(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "markdown"])
    cre_validate.main()
    out = capsys.readouterr().out
    assert "## Totals" in out
    assert "## Source Counts" in out


def test_main_out_flag_writes_file(monkeypatch, tmp_path, capsys):
    _patch_main(monkeypatch)
    out_file = tmp_path / "report.md"
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "markdown", "--out", str(out_file)])
    cre_validate.main()
    assert out_file.exists()
    content = out_file.read_text()
    assert "## Totals" in content
    # Sentinel must not be in written file either
    assert "SENTINEL" not in content


def test_main_out_flag_json_writes_file(monkeypatch, tmp_path):
    _patch_main(monkeypatch)
    out_file = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "json", "--out", str(out_file)])
    cre_validate.main()
    assert out_file.exists()
    parsed = json.loads(out_file.read_text())
    assert "queries" in parsed


def test_main_out_creates_parent_dirs(monkeypatch, tmp_path):
    _patch_main(monkeypatch)
    out_file = tmp_path / "nested" / "deep" / "report.md"
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "markdown", "--out", str(out_file)])
    cre_validate.main()
    assert out_file.exists()


def test_main_collects_warnings(monkeypatch, capsys):
    """Warnings from run_query are deduped and included in the report."""
    monkeypatch.setattr(cre_validate, "load_db_url", lambda env_file: ("postgres://SENTINEL", "/fake/.env.local"))
    monkeypatch.setattr(cre_validate, "find_psql", lambda: "psql")
    monkeypatch.setattr(
        cre_validate,
        "run_queries",
        lambda psql, url, queries: (
            {name: _DUMMY_ROWS for name in queries},
            "collation version mismatch WARNING",
        ),
    )
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "markdown"])
    cre_validate.main()
    out = capsys.readouterr().out
    assert "## psql Warnings" in out


def test_main_warnings_deduped(monkeypatch, capsys):
    """The same warning is only added once even though multiple queries run."""
    monkeypatch.setattr(cre_validate, "load_db_url", lambda env_file: ("postgres://SENTINEL", "/fake/.env.local"))
    monkeypatch.setattr(cre_validate, "find_psql", lambda: "psql")
    monkeypatch.setattr(
        cre_validate,
        "run_queries",
        lambda psql, url, queries: (
            {name: _DUMMY_ROWS for name in queries},
            "collation version mismatch alert",
        ),
    )
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "json"])
    cre_validate.main()
    out = capsys.readouterr().out
    parsed = json.loads(out)
    # All queries emit the same (normalized) warning but it appears only once.
    warnings = parsed["psql_warnings"]
    assert len(warnings) == len(set(warnings))
