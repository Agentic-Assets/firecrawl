"""
test_om_parse.py

Pure-transform / no-DB / no-network contracts for the OM/PDF underwriting parse
tier (om_parse.py). Per tests/CLAUDE.md: import and call the real functions; feed
SAVED /v2/parse-shaped fixtures (tests/fixtures/parse/cbre_om.json,
jll_om.json) through the PURE extractor; never hit a live DB / Firecrawl. The
network boundary (parse_pdf_to_text) is monkeypatched where run() is exercised.

This is retained diagnostic extraction code, so the tests pin: NOI / cap_rate /
occupancy / units / year_built extraction; unit_mix / rent_roll row shaping;
that EVERY emitted scalar carries provenance (source_doc_url, parser_version,
confidence in [0,1]); and that a non-underwriting PDF yields ZERO scalars (no
fabrication). Separate regression cases prove its artifacts cannot reach the
Firecrawl ingestion path.
"""

import json
import os

import pytest

import om_parse
from om_parse import (
    CONFIDENCE_FLOOR,
    PARSER_VERSION,
    build_candidate_sql,
    build_docs_sql,
    build_enriched_listing,
    extract_om_facts,
    listing_scalars_from_facts,
)

_FIX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "parse")


def _load_md(name):
    with open(os.path.join(_FIX_DIR, name)) as f:
        data = json.load(f)
    payload = data["data"]
    return payload["markdown"], payload["metadata"]["sourceURL"]


def _by_key(facts, fact_key, group="scalar"):
    for f in facts:
        if f["factKey"] == fact_key and f["factGroup"] == group:
            return f
    return None


# --- CBRE multifamily OM: full scalar set + unit_mix + rent_roll -----------


def test_cbre_om_extracts_all_scalar_underwriting_fields():
    md, url = _load_md("cbre_om.json")
    facts = extract_om_facts(md, url)
    noi = _by_key(facts, "noi")
    cap = _by_key(facts, "cap_rate")
    occ = _by_key(facts, "occupancy_rate")
    units = _by_key(facts, "units")
    yb = _by_key(facts, "year_built")
    assert noi and noi["factValueNum"] == 1485000.0
    assert cap and cap["factValueNum"] == pytest.approx(0.0606)
    assert occ and occ["factValueNum"] == pytest.approx(0.945)
    assert units and units["factValueNum"] == 168
    assert yb and yb["factValueNum"] == 1998


def test_cbre_om_unit_mix_rows_shaped_with_count_type_rent():
    md, url = _load_md("cbre_om.json")
    facts = extract_om_facts(md, url)
    mix = [f for f in facts if f["factGroup"] == "unit_mix"]
    assert len(mix) == 3
    by_count = {f["unitCount"]: f for f in mix}
    assert set(by_count) == {48, 84, 36}
    # unit_type label is carried in factValueText; the count in unit_count.
    assert by_count[48]["factKey"] == "unit_type"
    assert "1br" in by_count[48]["factValueText"]
    assert by_count[84]["factValueText"].startswith("2br")
    # every unit_mix row still carries provenance.
    for f in mix:
        assert f["sourceDocUrl"] == url
        assert f["parserVersion"] == PARSER_VERSION
        assert 0 <= f["confidence"] <= 1


def test_cbre_om_rent_roll_rows_shaped_with_tenant_and_rent():
    md, url = _load_md("cbre_om.json")
    facts = extract_om_facts(md, url)
    roll = [f for f in facts if f["factGroup"] == "rent_roll"]
    assert len(roll) == 3
    rents = sorted(f["factValueNum"] for f in roll)
    assert rents == [1250.0, 1675.0, 2150.0]
    for f in roll:
        assert f["factKey"] == "tenant"
        assert f["factValueText"]  # non-empty label
        assert f["sourceDocUrl"] == url
        assert f["parserVersion"] == PARSER_VERSION


# --- JLL office OM: scalars present, NO units (office) ---------------------


def test_jll_om_extracts_office_scalars_without_units():
    md, url = _load_md("jll_om.json")
    facts = extract_om_facts(md, url)
    assert _by_key(facts, "noi")["factValueNum"] == 6240000.0
    assert _by_key(facts, "cap_rate")["factValueNum"] == pytest.approx(0.0725)
    assert _by_key(facts, "occupancy_rate")["factValueNum"] == pytest.approx(0.882)
    assert _by_key(facts, "year_built")["factValueNum"] == 2007
    # An office OM with no unit count must NOT invent a units scalar.
    assert _by_key(facts, "units") is None


# --- provenance is mandatory on EVERY emitted fact -------------------------


@pytest.mark.parametrize("fixture", ["cbre_om.json", "jll_om.json"])
def test_every_fact_carries_full_provenance(fixture):
    md, url = _load_md(fixture)
    facts = extract_om_facts(md, url)
    assert facts, "fixture should produce at least one fact"
    for f in facts:
        # the three required provenance fields (cre_ingest.om_facts_rows drops a
        # row missing any of them, so they must always be present).
        assert f["sourceDocUrl"] == url and f["sourceDocUrl"]
        assert f["parserVersion"] == PARSER_VERSION
        assert f["confidence"] is not None
        assert 0.0 <= f["confidence"] <= 1.0
        # fact_key is always present and non-empty.
        assert f["factKey"]
        # fact_group is one of the three allowed classes.
        assert f["factGroup"] in ("scalar", "unit_mix", "rent_roll")


# --- confidence floor: < 0.6 writes om_facts ONLY, not the column ----------


def test_confidence_floor_keeps_low_confidence_scalar_out_of_columns():
    # A bare "NOI $850,000" abbreviation with no full "Net Operating Income"
    # label is label-weak: the heuristic assigns < 0.6, so it must NOT become a
    # cre_listings column, but it MUST still appear as an om_facts provenance row.
    text = "Property highlights. NOI $850,000 in place. Great location."
    url = "https://x/y.pdf"
    facts = extract_om_facts(text, url)
    noi = _by_key(facts, "noi")
    assert noi is not None  # captured as an audit row
    assert noi["confidence"] < CONFIDENCE_FLOOR
    # listing_scalars_from_facts omits the sub-floor scalar from the column writes.
    scalars = listing_scalars_from_facts(facts)
    assert "noi" not in scalars


def test_high_confidence_scalar_writes_both_column_and_om_fact():
    md, url = _load_md("cbre_om.json")
    facts = extract_om_facts(md, url)
    scalars = listing_scalars_from_facts(facts)
    # The full-label NOI clears the floor, so it writes the column (camelCase key)
    # AND remains in the om_facts list (audit trail).
    assert scalars["noi"] == 1485000.0
    assert scalars["capRatePct"] == pytest.approx(0.0606)
    assert scalars["occupancyRate"] == pytest.approx(0.945)
    assert scalars["units"] == 168
    assert scalars["yearBuilt"] == 1998
    assert _by_key(facts, "noi") is not None  # still in the provenance set


def test_listing_scalars_only_emit_clearing_facts_camelcase_keys():
    md, url = _load_md("jll_om.json")
    facts = extract_om_facts(md, url)
    scalars = listing_scalars_from_facts(facts)
    # keys are the cre_ingest camelCase listing vocabulary, never snake_case.
    assert set(scalars).issubset({"noi", "capRatePct", "occupancyRate", "units", "yearBuilt"})
    assert "cap_rate" not in scalars and "occupancy_rate" not in scalars
    # office OM: no units column write.
    assert "units" not in scalars


# --- non-underwriting PDF -> ZERO scalars (no fabrication) ------------------


def test_non_underwriting_pdf_yields_zero_facts():
    text = (
        "# Property Flyer\n\nWelcome to a beautiful retail storefront in the heart "
        "of downtown. Walk-in traffic. Call our broker for a tour today. "
        "Signage available. Ample street parking nearby."
    )
    facts = extract_om_facts(text, "https://x/flyer.pdf")
    assert facts == []


def test_empty_or_nonstring_text_yields_zero_facts():
    assert extract_om_facts("", "https://x/y.pdf") == []
    assert extract_om_facts("   ", "https://x/y.pdf") == []
    assert extract_om_facts(None, "https://x/y.pdf") == []


def test_missing_source_doc_url_yields_zero_facts():
    md, _ = _load_md("cbre_om.json")
    # Provenance requires a source_doc_url; an extractor with no url emits nothing
    # (never an un-attributable fact).
    assert extract_om_facts(md, None) == []
    assert extract_om_facts(md, "") == []


def test_build_enriched_listing_none_when_no_facts():
    cand = {"sourceKey": "cbre", "externalId": "abc", "url": "https://x/a"}
    assert build_enriched_listing(cand, []) is None


# --- enriched listing shape: scalars + omFacts + addressing keys -----------


def test_build_enriched_listing_carries_scalars_and_full_omfacts():
    md, url = _load_md("cbre_om.json")
    facts = extract_om_facts(md, url)
    cand = {"sourceKey": "cbre", "externalId": "maple-court-1",
            "url": "https://cbre.example/p/maple-court", "transactionMode": "sale"}
    listing = build_enriched_listing(cand, facts)
    # addressing keys cre_ingest needs to find the row.
    assert listing["sourceKey"] == "cbre"
    assert listing["externalId"] == "maple-court-1"
    assert listing["url"] == "https://cbre.example/p/maple-court"
    # high-confidence scalar columns present.
    assert listing["noi"] == 1485000.0
    assert listing["units"] == 168
    # the FULL omFacts audit list (scalars + line items) rides along.
    assert listing["omFacts"] == facts
    assert any(f["factGroup"] == "unit_mix" for f in listing["omFacts"])
    assert any(f["factGroup"] == "rent_roll" for f in listing["omFacts"])


def test_legacy_omfacts_serializer_preserves_provenance_for_regression_only():
    # This validates the retired serializer's pure provenance shape. It does not
    # imply a production writer path: to_row() and build_sql() discard omFacts.
    import cre_ingest
    md, url = _load_md("cbre_om.json")
    facts = extract_om_facts(md, url)
    staged = cre_ingest.om_facts_rows(facts)
    assert len(staged) == len(facts)
    for s in staged:
        assert s["factKey"]
        assert s["sourceDocUrl"] == url
        assert s["parserVersion"] == PARSER_VERSION
        assert s["factGroup"] in ("scalar", "unit_mix", "rent_roll")


# --- retired artifact boundary ------------------------------------------------


def test_retired_enriched_listing_is_rejected_before_staging_scalars():
    import cre_ingest

    md, url = _load_md("cbre_om.json")
    listing = build_enriched_listing(
        {
            "sourceKey": "cbre",
            "externalId": "maple-court-1",
            "url": "https://cbre.example/p/maple-court",
        },
        extract_om_facts(md, url),
    )

    assert listing["noi"] == 1485000.0  # The retired artifact still has diagnostics.
    assert cre_ingest.to_row(listing, {}, "2026-07-15T00:00:00+00:00") is None


# --- candidate selection SQL shape + safety --------------------------------


def test_build_candidate_sql_selects_listings_missing_underwriting_with_pdf_doc():
    sql = build_candidate_sql(["cbre", "jll"], limit=50,
                              brokerage_slugs=["cbre", "jll"])
    assert "FROM credeals.cre_listings l" in sql
    assert "JOIN credeals.cre_brokerages b" in sql
    assert "cre_listing_documents d" in sql
    # only un-underwritten listings (at least one scalar NULL).
    assert "l.noi IS NULL" in sql and "l.cap_rate IS NULL" in sql
    assert "l.year_built IS NULL" in sql
    # never selects soft-deleted rows.
    assert "l.deleted_at IS NULL" in sql
    # GUC pins + bare-int limit (no injection).
    assert "SET standard_conforming_strings = on;" in sql
    assert "\\set ON_ERROR_STOP on" in sql
    assert "LIMIT 50;" in sql
    # doc_types are sql_lit-quoted, not f-string'd.
    assert "'om'" in sql and "'brochure'" in sql


def test_build_candidate_sql_limit_validated_to_int():
    # a string limit is coerced to int and inlined bare (no quoting / injection).
    assert build_candidate_sql(["cbre"], limit="25", brokerage_slugs=["cbre"]) == \
        build_candidate_sql(["cbre"], limit=25, brokerage_slugs=["cbre"])
    assert "LIMIT 25;" in build_candidate_sql(["cbre"], limit=25, brokerage_slugs=["cbre"])


def test_build_candidate_sql_slug_quote_doubling():
    # a slug carrying an apostrophe is quote-doubled under standard_conforming_strings.
    sql = build_candidate_sql(["x"], limit=1, brokerage_slugs=["o'brien"])
    assert "'o''brien'" in sql


def test_build_docs_sql_empty_ids_is_noop_no_malformed_in():
    sql = build_docs_sql([], ["cbre"])
    assert "IN ()" not in sql
    assert "SELECT NULL WHERE false;" in sql


def test_build_docs_sql_quotes_ids_and_filters_doc_types():
    sql = build_docs_sql(["a1", "b2"], ["cbre", "jll"])
    assert "'a1'" in sql and "'b2'" in sql
    assert "d.doc_type IN (" in sql
    assert "'om'" in sql
    assert "l.deleted_at IS NULL" in sql


# --- the DB url is never printed by run() (smoke) --------------------------


DB_URL_SENTINEL = "postgres://user:secret@db.example.com:5432/postgres"


class _Args:
    def __init__(self, **kw):
        self.sources = kw.get("sources", "cbre,jll")
        self.limit = kw.get("limit", 100)
        self.api_url = kw.get("api_url", "http://localhost:3002")
        self.env_file = kw.get("env_file", None)
        self.apply = kw.get("apply", False)
        self.dry_run = kw.get("dry_run", True)
        self.show_sql = kw.get("show_sql", False)


def test_show_sql_dry_run_prints_candidate_sql_without_connecting(monkeypatch, capsys):
    def _boom(*a, **k):
        raise AssertionError("--show-sql dry-run must not connect")

    monkeypatch.setattr(om_parse, "load_db_url", _boom)
    monkeypatch.setattr(om_parse, "_psql_query", _boom)
    rc = om_parse.run(_Args(dry_run=True, show_sql=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "FROM credeals.cre_listings l" in out
    assert "LIMIT" in out


def test_run_dry_run_writes_artifact_does_not_ingest(monkeypatch, tmp_path, capsys):
    # Wire a fake DB + fake parse so run() produces an artifact but never ingests
    # (default dry-run). Asserts the DB url is never printed and no ingest fires.
    md, url = _load_md("cbre_om.json")
    monkeypatch.setattr(om_parse, "OUT_OM_DIR", str(tmp_path))
    monkeypatch.setattr(om_parse, "load_db_url",
                        lambda env_file: (DB_URL_SENTINEL, "/fake/.env.local"))

    calls = {"ingest_called": False}

    def _fake_query(db_url, sql):
        assert db_url == DB_URL_SENTINEL
        if "cre_listing_documents d ON" in sql:  # docs query
            return [("cbre", "maple-1", url, "om")]
        return [("maple-1", "https://cbre.example/p/maple", "cbre")]  # candidate query

    monkeypatch.setattr(om_parse, "_psql_query", _fake_query)
    monkeypatch.setattr(om_parse, "parse_pdf_to_text",
                        lambda doc_url, api_url=None: md)

    def _fake_run(argv, **kw):
        if any(str(a).endswith("cre_ingest.py") for a in argv):
            calls["ingest_called"] = True

        class _P:
            returncode = 0
        return _P()

    monkeypatch.setattr(om_parse.subprocess, "run", _fake_run)

    rc = om_parse.run(_Args(apply=False, dry_run=True))
    assert rc == 0
    assert calls["ingest_called"] is False  # dry-run must NOT ingest
    captured = capsys.readouterr()
    assert DB_URL_SENTINEL not in captured.out
    assert DB_URL_SENTINEL not in captured.err
    assert "credentials:" in captured.err
    # The artifact is explicitly quarantined from cre_ingest.
    written = list(tmp_path.glob("om_*.json"))
    assert len(written) == 1
    artifact = json.loads(written[0].read_text())
    assert artifact["artifactKind"] == om_parse.RETIRED_OM_PARSE_ARTIFACT_KIND
    assert artifact["listings"][0]["noi"] == 1485000.0
    assert artifact["listings"][0]["omFacts"]
    assert "cannot be ingested" in captured.err


def test_run_apply_fails_before_any_database_or_ingest_work(monkeypatch, capsys):
    def _boom(*_args, **_kwargs):
        raise AssertionError("retired --apply must not reach the database")

    monkeypatch.setattr(om_parse, "load_db_url", _boom)
    monkeypatch.setattr(om_parse, "_psql_query", _boom)
    rc = om_parse.run(_Args(apply=True, dry_run=False))
    assert rc == om_parse.RETIRED_WRITER_EXIT_CODE
    assert "sole production OM extraction writer" in capsys.readouterr().err


def test_run_zero_candidates_is_noop(monkeypatch, capsys):
    monkeypatch.setattr(om_parse, "load_db_url",
                        lambda env_file: (DB_URL_SENTINEL, "/fake/.env.local"))
    monkeypatch.setattr(om_parse, "_psql_query", lambda db_url, sql: [])
    rc = om_parse.run(_Args())
    assert rc == 0
    assert "0 candidates" in capsys.readouterr().err
