"""Actual Avison producer/staging/merge boundaries; no DB or HTTP."""

import json
from pathlib import Path
import subprocess

import cre_backfill_raw_data as backfill
import cre_ingest as ingest
import cre_monitor as monitor
import pytest

from test_avison_lease_facts import OBSERVED, listing as observed_listing

COLLECTOR = Path(__file__).resolve().parents[1]
EXTRACT_DRIVER = r"""
import fs from 'node:fs';
import {extractAvisonLeaseFacts} from './sources/pure/avison-lease-facts.ts';
import {projectAvisonAskingPrice} from './sources/pure/avison-price-visibility.ts';
globalThis.fetch=async()=>{throw new Error('HTTP forbidden in closed integration')};
const items=JSON.parse(fs.readFileSync(0,'utf8'));
const output=items.map(({listing,rents,duplicateRent,malformedPair,overviewHtml})=>{
  const cards=rents.map((rent,index)=>`<div class="availability-card-v2"><div class="availability-card-v2-container">
    <div class="availability-card-status-tag">Available</div>
    <div class="availability-card-name"><h3>Unit ${index}</h3></div>
    <div class="availability-card-rent"><h3>${rent??''}</h3>${duplicateRent?`<h3>${duplicateRent}</h3>`:''}</div>
    <div class="availability-card-info-item"><span class="availability-card-info-item-title">Total Size</span><p class="availability-card-info-item-value">706 SF</p></div>
    <div class="availability-card-info-item"><span class="availability-card-info-item-title">Lease Type</span><p class="availability-card-info-item-value">Full Service</p></div>
  </div></div>`).join('');
  const facts=extractAvisonLeaseFacts({externalId:listing.id,listingUrl:listing.url,observedAt:listing.detailObservedAt,pages:[{sourceUrl:listing.url,rawHtml:cards+(overviewHtml??'')}]});
  if(malformedPair) facts.pages[0].observations[0].fields.push({label:'Other Amount',value:20});
  if(listing.rawSharpLaunch.hide_price===1) {
    facts.salePriceUsd=30;
    facts.askingAmount=30;
    facts.pricing={businessFunction:'Sell',amount:30};
  }
  return projectAvisonAskingPrice({...listing,extraFacts:{avison_lease_facts:facts}});
});
console.log(JSON.stringify(output));
"""


def legacy_listing(**changes):
    listing = {
        "sourceKey": "avison-young",
        "id": "lease-boundary",
        "url": "https://www.avisonyoung.us/properties/lease-boundary",
        "name": "Lease boundary fixture",
        "transactionMode": "lease",
        "transactionType": "Lease",
        "leaseRateText": "$2.50/SF/YR",
        "leaseRateMin": 2.5,
        "leaseRateMax": 2.5,
        "rawSharpLaunch": {
            "hide_price": 0,
            "availabilities_min_rent": 2.5,
            "availabilities_max_rent": 2.5,
        },
    }
    listing.update(changes)
    return listing


def test_legacy_synthesized_rate_is_not_new_avison_stage_authority():
    row = ingest.to_row(legacy_listing(), {}, "2026-10-18T07:00:00Z")
    assert row is not None
    assert row["lease_rate_min"] is None
    assert row["lease_rate_max"] is None
    assert row["raw_data"]["rawSharpLaunch"]["availabilities_min_rent"] == 2.5


def test_raw_backfill_cannot_restore_untyped_avison_feed_rent():
    derived, _extra = backfill.derive_avison_young(legacy_listing())
    assert derived.get("lease_rate_min") is None
    assert derived.get("lease_rate_max") is None


@pytest.mark.parametrize("reverse", [False, True])
def test_merge_rechecks_all_raw_passes_instead_of_first_rate_winning(reverse):
    monthly = observed_listing(("$2.50/SF/Month",))
    annual = observed_listing(("$26.50/SF/Year",))
    monthly["transactionMode"] = "sale"
    annual["transactionMode"] = "lease"
    monthly["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][0][
        "suite_text"
    ] = "Monthly unit"
    annual["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][0][
        "suite_text"
    ] = "Annual unit"
    rows = [ingest.to_row(item, {}, OBSERVED) for item in (monthly, annual)]
    assert [row["lease_rate_min"] for row in rows] == [30, 26.5]
    if reverse:
        rows.reverse()
    merged = ingest.merge_rows(*rows)
    assert merged["lease_rate_min"] is None
    assert merged["lease_rate_max"] is None
    assert "primary" in merged["raw_data"] and "secondary_pass" in merged["raw_data"]
    normalized = merged["extra_facts"]["avison_lease_facts"]["normalized_observations"]
    assert sorted(item["annual_psf_min"] for item in normalized) == [26.5, 30]
    assert (
        merged["extra_facts"]["avison_lease_facts"]["projection"]["available_count"]
        == 2
    )


def test_monitor_uses_stage_numbers_without_a_synthetic_lease_text(tmp_path):
    import json

    listing = observed_listing(("$26.50/SF/Year",))
    listing.pop("leaseRateText")
    listing.pop("leaseRateMin")
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        json.dumps({"runMeta": {"finishedAt": OBSERVED}, "listings": [listing]})
    )
    groups, *_ = monitor.load_artifact_groups([str(artifact)])
    final = monitor.finalize_group(next(iter(groups.values())))
    assert final["lease_rate_min"] == 26.5
    assert final["lease_rate_text"] is None


def test_hidden_sale_equal_to_rent_does_not_remove_independent_rent():
    listing = observed_listing(("$30/SF/Year",))
    listing["salePriceUsd"] = 30
    listing["salePriceText"] = "$30"
    listing["rawSharpLaunch"].update(hide_price=1, sale_price=30)
    row = ingest.to_row(listing, {}, OBSERVED)
    assert row["sale_price_usd"] is None
    assert row["lease_rate_min"] == 30
    assert (
        row["raw_data"]["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][
            0
        ]["rent_text"]
        == "$30/SF/Year"
    )


@pytest.mark.parametrize("changed", ["clock", "url"])
def test_actual_stager_refuses_foreign_observation_context(changed):
    listing = observed_listing()
    page = listing["extraFacts"]["avison_lease_facts"]["pages"][0]
    page["observed_at" if changed == "clock" else "source_url"] = (
        "2026-10-17T00:00:00Z"
        if changed == "clock"
        else "https://www.avisonyoung.us/properties/foreign"
    )
    with pytest.raises(ValueError):
        ingest.to_row(listing, {}, OBSERVED)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("different_suite", [False, True])
def test_merge_dedup_cannot_hide_conflicting_currency_context(reverse, different_suite):
    usd = observed_listing(("$2.50/SF/Month",))
    foreign = observed_listing(("$2.50/SF/Month",))
    foreign["rawSharpLaunch"]["sale_currency"] = "CAD"
    if different_suite:
        foreign["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][0][
            "suite_text"
        ] = "Foreign context unit"
    rows = [ingest.to_row(item, {}, OBSERVED) for item in (usd, foreign)]
    assert rows[0]["lease_rate_min"] == 30
    assert rows[1]["lease_rate_min"] is None
    if reverse:
        rows.reverse()
    merged = ingest.merge_rows(*rows)
    assert merged["lease_rate_min"] is None
    assert merged["lease_rate_max"] is None
    # Deduplication must not discard the distinct source context needed to
    # diagnose why otherwise identical literal observations cannot aggregate.
    assert {
        leaf["rawSharpLaunch"]["sale_currency"]
        for leaf in (
            merged["raw_data"]["primary"],
            merged["raw_data"]["secondary_pass"],
        )
    } == {"USD", "CAD"}


@pytest.mark.parametrize(
    "overview_rent,expected",
    [
        ("Contact broker", 30),
        ("$2,000/Month", None),
        ("Terms under review", None),
        ("$?/SF/Month", None),
    ],
)
def test_optional_overview_unknown_is_not_a_monetary_conflict(overview_rent, expected):
    import copy

    listing = observed_listing(("$2.50/SF/Month",))
    facts = listing["extraFacts"]["avison_lease_facts"]
    overview = copy.deepcopy(facts["pages"][0])
    overview["role"] = "overview"
    overview["observations"][0]["rent_text"] = overview_rent
    facts["pages"].append(overview)
    result = ingest.to_row(listing, {}, OBSERVED)
    assert result["lease_rate_min"] == expected
    assert (
        result["raw_data"]["extraFacts"]["avison_lease_facts"]["pages"][1][
            "observations"
        ][0]["rent_text"]
        == overview_rent
    )


def test_asking_label_value_pair_cannot_inherit_blanket_lease_exemption():
    listing = observed_listing(("$20/SF/Year",))
    listing["rawSharpLaunch"].update(hide_price=1, sale_price=20)
    observation = listing["extraFacts"]["avison_lease_facts"]["pages"][0][
        "observations"
    ][0]
    observation["fields"] = [
        {"label": "Sale Price", "value": "$20"},
        {"label": "Lease Rate", "value": "$20/SF/Year"},
        {"label": "Rental Rate", "value": "$20"},
    ]
    # Use the final source-owned staging pipeline, not a requirement to broaden
    # the generic asking projector for arbitrary label/value objects.
    projected = ingest.to_row(listing, {}, OBSERVED)
    fields = projected["raw_data"]["extraFacts"]["avison_lease_facts"]["pages"][0][
        "observations"
    ][0]["fields"]
    assert fields == [
        {"label": "Lease Rate", "value": "$20/SF/Year"},
        {"label": "Rental Rate", "value": "$20"},
    ]
    assert projected["sale_price_usd"] is None
    # Retained bare monetary text cannot supply the missing period/basis.
    assert projected["lease_rate_min"] is None


def test_redacted_required_literal_becomes_null_not_a_missing_schema_key():
    listing = observed_listing(("Asking price $20",))
    listing["rawSharpLaunch"].update(hide_price=1, sale_price=20)
    row = ingest.to_row(listing, {}, OBSERVED)
    observation = row["raw_data"]["extraFacts"]["avison_lease_facts"]["pages"][0][
        "observations"
    ][0]
    assert observation["rent_text"] is None
    assert row["lease_rate_min"] is None


def test_hidden_projection_cannot_silently_drop_a_malformed_pair():
    listing = observed_listing(("$20/SF/Year",))
    listing["rawSharpLaunch"].update(hide_price=1, sale_price=20)
    listing["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][0][
        "fields"
    ] = [{"label": "Other Amount", "value": 20}]
    with pytest.raises(ValueError):
        ingest.to_row(listing, {}, OBSERVED)


def test_conflicting_unit_claims_keep_individual_normalized_evidence():
    listing = observed_listing(("$2.50/SF/Month", "$26.50/SF/Year"))
    observations = listing["extraFacts"]["avison_lease_facts"]["pages"][0][
        "observations"
    ]
    for observation in observations:
        observation["suite_text"] = "Same observed unit"
    row = ingest.to_row(listing, {}, OBSERVED)
    assert row["lease_rate_min"] is None
    facts = row["extra_facts"]["avison_lease_facts"]
    assert sorted(
        item["annual_psf_min"] for item in facts["normalized_observations"]
    ) == [26.5, 30]
    assert [item["rent_text"] for item in facts["pages"][0]["observations"]] == [
        "$2.50/SF/Month",
        "$26.50/SF/Year",
    ]


def test_real_ts_raw_extractor_asking_projection_and_python_staging():
    cases = [
        (observed_listing(), ["$2.50/SF/Month", "$2.50/SF/Month", None, None]),
        (observed_listing(), ["$26.50/SF/Year"]),
        (observed_listing(), ["$2,000/Month"]),
    ]
    cases[0][0]["rawSharpLaunch"].update(hide_price=1, sale_price=30)
    result = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", EXTRACT_DRIVER],
        input=json.dumps(
            [{"listing": listing, "rents": rents} for listing, rents in cases]
        ),
        cwd=COLLECTOR,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    produced = json.loads(result.stdout)
    hidden_facts = produced[0]["extraFacts"]["avison_lease_facts"]
    assert "salePriceUsd" not in hidden_facts
    assert "askingAmount" not in hidden_facts
    assert "amount" not in hidden_facts.get("pricing", {})
    rows = [ingest.to_row(listing, {}, OBSERVED) for listing in produced]
    assert [row["lease_rate_min"] for row in rows] == [30, 26.5, None]
    projection = rows[0]["extra_facts"]["avison_lease_facts"]["projection"]
    assert [
        projection[key]
        for key in (
            "available_count",
            "priced_available_count",
            "unpriced_available_count",
            "ambiguous_available_count",
        )
    ] == [4, 2, 2, 0]
    assert [
        observation["rent_text"]
        for observation in produced[0]["extraFacts"]["avison_lease_facts"]["pages"][0][
            "observations"
        ]
    ] == ["$2.50/SF/Month", "$2.50/SF/Month", None, None]
    for listing in produced:
        assert "annual_psf_min" not in json.dumps(listing)


def test_actual_extractor_cannot_discard_a_second_conflicting_rent_heading():
    result = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", EXTRACT_DRIVER],
        input=json.dumps(
            [
                {
                    "listing": observed_listing(),
                    "rents": ["$2.50/SF/Month"],
                    "duplicateRent": "$26.50/SF/Year",
                }
            ]
        ),
        cwd=COLLECTOR,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    listing = json.loads(result.stdout)[0]
    observation = listing["extraFacts"]["avison_lease_facts"]["pages"][0][
        "observations"
    ][0]
    assert observation["rent_text"] is None
    assert any("$2.50/SF/Month" in text for text in observation["unparsed"])
    assert any("$26.50/SF/Year" in text for text in observation["unparsed"])
    staged = ingest.to_row(listing, {}, OBSERVED)
    assert staged["lease_rate_min"] is None
    projection = staged["extra_facts"]["avison_lease_facts"]["projection"]
    assert projection["ambiguous_available_count"] == 1
    assert projection["unpriced_available_count"] == 0


def test_ts_hidden_projection_does_not_launder_malformed_pair_before_staging():
    listing = observed_listing(("$20/SF/Year",))
    listing["rawSharpLaunch"].update(hide_price=1, sale_price=20)
    result = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", EXTRACT_DRIVER],
        input=json.dumps(
            [{"listing": listing, "rents": ["$20/SF/Year"], "malformedPair": True}]
        ),
        cwd=COLLECTOR,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    projected = json.loads(result.stdout)[0]
    with pytest.raises(ValueError):
        ingest.to_row(projected, {}, OBSERVED)


@pytest.mark.parametrize("second_claim", ["$3.00/SF/Month", "$20/SF/Week"])
def test_duplicate_overview_rent_claims_cannot_hide_a_conflict(second_claim):
    result = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", EXTRACT_DRIVER],
        input=json.dumps(
            [
                {
                    "listing": observed_listing(),
                    "rents": ["$2.50/SF/Month"],
                    "overviewHtml": '<div class="building__overview-text"><p>Rent: $2.50/SF/Month</p><p>Rent: '
                    + second_claim
                    + "</p></div>",
                }
            ]
        ),
        cwd=COLLECTOR,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    listing = json.loads(result.stdout)[0]
    assert ingest.to_row(listing, {}, OBSERVED)["lease_rate_min"] is None
    assert second_claim in json.dumps(listing["extraFacts"])


@pytest.mark.parametrize(
    "lease_type", ["Full Service (Annual)", "Full Service /SF/Year"]
)
def test_lease_type_prose_cannot_rewrite_the_explicit_rent_period(lease_type):
    listing = observed_listing(("$2.50/SF/Month",))
    observation = listing["extraFacts"]["avison_lease_facts"]["pages"][0][
        "observations"
    ][0]
    observation["lease_type_text"] = lease_type
    row = ingest.to_row(listing, {}, OBSERVED)
    # Rent owns its own period; ancillary type prose cannot change the amount.
    assert row["lease_rate_min"] == 30
    assert (
        row["raw_data"]["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][
            0
        ]["lease_type_text"]
        == lease_type
    )


def postgres_contract_sql():
    """Actual generated SQL in the existing owned disposable-PG harness."""
    import cre_avison_price_visibility as asking

    listing = observed_listing(("$20/SF/Year",))
    listing["rawSharpLaunch"].update(hide_price=1, sale_price=20)
    fields = [
        {"label": "Sale Price", "value": "$20"},
        {"label": "Other Amount", "value": "$20"},
        {"label": "Sale Price and Rent", "value": "$20"},
        {"label": "Rent", "value": "$20"},
        {"label": "Rental Rate", "value": "$20"},
        {"label": "Lease Rate", "value": "$20/SF/Year"},
        {"label": "Clear Height", "value": "20 feet"},
    ]
    listing["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][0][
        "fields"
    ] = fields
    definitions = asking.sql_policy([{"slug": "avison-young", "raw_data": listing}])[
        "definitions"
    ]
    payload = json.dumps(listing).replace("'", "''")
    expected = json.dumps(fields[3:]).replace("'", "''")
    redacted = json.loads(json.dumps(listing))
    redacted["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][0][
        "rent_text"
    ] = "Asking price $20"
    redacted_sql = json.dumps(redacted).replace("'", "''")
    malformed = json.loads(json.dumps(listing))
    malformed["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][0][
        "fields"
    ] = [{"label": "Other Amount", "value": 20}]
    malformed_sql = json.dumps(malformed).replace("'", "''")
    nested = json.loads(json.dumps(listing))
    namespace = nested["extraFacts"]["avison_lease_facts"]
    namespace.update(
        salePriceUsd=20,
        askingAmount=20,
        pricing={"businessFunction": "sell", "amount": 20},
    )
    nested_sql = json.dumps(nested).replace("'", "''")
    numeric_identity = json.loads(json.dumps(listing))
    identity_observation = numeric_identity["extraFacts"]["avison_lease_facts"][
        "pages"
    ][0]["observations"][0]
    identity_observation.update(suite_text="20", area_text="20")
    numeric_identity_sql = json.dumps(numeric_identity).replace("'", "''")
    identity_observation.update(suite_text="$20", area_text="Asking price $20")
    financial_identity_sql = json.dumps(numeric_identity).replace("'", "''")
    return (
        "BEGIN;\nSET LOCAL statement_timeout='15s';\n"
        + definitions
        + f"""
DO $contract$
DECLARE actual jsonb; fields jsonb;
BEGIN
 actual := pg_temp.avison_asking_raw('{payload}'::jsonb, ARRAY[20]::numeric[]);
 fields := actual #> '{{extraFacts,avison_lease_facts,pages,0,observations,0,fields}}';
 IF fields IS DISTINCT FROM '{expected}'::jsonb THEN
  RAISE EXCEPTION 'Lease namespace asking pair SQL projection mismatch';
 END IF;
 IF actual #>> '{{extraFacts,avison_lease_facts,pages,0,observations,0,rent_text}}' IS DISTINCT FROM '$20/SF/Year' THEN
  RAISE EXCEPTION 'Equal-valued independent rent lost';
 END IF;
 actual := pg_temp.avison_asking_raw('{redacted_sql}'::jsonb, ARRAY[20]::numeric[]);
 IF actual #> '{{extraFacts,avison_lease_facts,pages,0,observations,0,rent_text}}' IS DISTINCT FROM 'null'::jsonb THEN
  RAISE EXCEPTION 'Redacted nullable literal was lost or retained';
 END IF;
 actual := pg_temp.avison_asking_raw('{malformed_sql}'::jsonb, ARRAY[20]::numeric[]);
 IF actual #> '{{extraFacts,avison_lease_facts,pages,0,observations,0,fields,0,value}}' IS DISTINCT FROM '20'::jsonb THEN
  RAISE EXCEPTION 'Malformed pair was silently dropped before strict consumer refusal';
 END IF;
 actual := pg_temp.avison_asking_raw('{nested_sql}'::jsonb, ARRAY[20]::numeric[]);
 IF (actual #> '{{extraFacts,avison_lease_facts}}') ? 'salePriceUsd'
    OR (actual #> '{{extraFacts,avison_lease_facts}}') ? 'askingAmount'
    OR (actual #> '{{extraFacts,avison_lease_facts,pricing}}') ? 'amount' THEN
  RAISE EXCEPTION 'Nested asking authority bypassed generic policy';
 END IF;
 actual := pg_temp.avison_asking_raw('{numeric_identity_sql}'::jsonb, ARRAY[20]::numeric[]);
 IF actual #>> '{{extraFacts,avison_lease_facts,pages,0,observations,0,suite_text}}' IS DISTINCT FROM '20'
    OR actual #>> '{{extraFacts,avison_lease_facts,pages,0,observations,0,area_text}}' IS DISTINCT FROM '20' THEN
  RAISE EXCEPTION 'Independent numeric identity literal was lost';
 END IF;
 actual := pg_temp.avison_asking_raw('{financial_identity_sql}'::jsonb, ARRAY[20]::numeric[]);
 IF actual #> '{{extraFacts,avison_lease_facts,pages,0,observations,0,suite_text}}' IS DISTINCT FROM 'null'::jsonb
    OR actual #> '{{extraFacts,avison_lease_facts,pages,0,observations,0,area_text}}' IS DISTINCT FROM 'null'::jsonb THEN
  RAISE EXCEPTION 'Identity role exempted explicit financial text';
 END IF;
END $contract$;
SELECT 'lease-pair-contract-pass';
"""
        + "\\set ON_ERROR_STOP off\nDO $$ BEGIN RAISE EXCEPTION 'synthetic rollback sentinel'; END $$;\nROLLBACK;\n\\set ON_ERROR_STOP on\n"
    )
