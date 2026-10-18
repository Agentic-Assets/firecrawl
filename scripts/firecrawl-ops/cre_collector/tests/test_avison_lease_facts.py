import copy

import pytest

from cre_ingest import to_row

URL = "https://www.avisonyoung.us/properties/test-lease"
OBSERVED = "2026-10-18T00:00:00Z"


def listing(rents=("$2.50/SF/Month",), statuses=None):
    observations = [
        dict(
            suite_text=f"Suite {i}",
            status_text=(statuses or ["Available"] * len(rents))[i],
            rent_text=rent,
            area_text="706 SF",
            lease_type_text="Full Service",
            fields=[],
            unparsed=[],
        )
        for i, rent in enumerate(rents)
    ]
    return dict(
        id="lease-test",
        sourceKey="avison-young",
        name="Test",
        url=URL,
        externalUrl=URL,
        country="US",
        transactionType="Lease",
        detailObservedAt=OBSERVED,
        detailScrape={"urls": [URL]},
        rawSharpLaunch={
            "id": "lease-test",
            "external_url": URL,
            "country": "US",
            "sale_currency": "USD",
        },
        leaseRateText="$2.50/SF/YR",
        leaseRateMin=2.5,
        extraFacts={
            "avison_lease_facts": dict(
                schema="avison-lease-facts-v1",
                external_id="lease-test",
                listing_url=URL,
                pages=[
                    dict(
                        source_url=URL,
                        observed_at=OBSERVED,
                        role="availability",
                        observations=observations,
                    )
                ],
            )
        },
    )


def stage(item):
    return to_row(item, {}, OBSERVED)


def test_explicit_monthly_rate_overrides_fabricated_legacy_annual():
    result = stage(listing())
    assert result["lease_rate_min"] == 30
    assert result["lease_rate_type"] == "full_service"


@pytest.mark.parametrize(
    "rent,expected",
    [
        ("$26.50/SF/Year", 26.5),
        ("$20/Month", None),
        ("$2,000/Month", None),
        ("$2.50", None),
        ("$2.50/SF", None),
        ("CAD 2.50/SF/Month", None),
    ],
)
def test_only_explicit_supported_area_period_currency_project(rent, expected):
    assert stage(listing((rent,)))["lease_rate_min"] == expected


def test_observed_subset_keeps_unknown_units_and_counts():
    result = stage(listing(("$2.50/SF/Month", "$2.50/SF/Month", None, None)))
    assert result["lease_rate_min"] == 30
    facts = result["extra_facts"]["avison_lease_facts"]
    assert facts["projection"]["coverage"] == "priced_available_subset"
    assert facts["projection"]["available_count"] == 4
    assert facts["projection"]["priced_available_count"] == 2
    assert facts["projection"]["unpriced_available_count"] == 2


def test_mixed_periods_keep_individual_rates_but_no_parent():
    result = stage(listing(("$2.50/SF/Month", "$26.50/SF/Year")))
    assert result["lease_rate_min"] is None
    assert sorted(
        x["annual_psf_min"]
        for x in result["extra_facts"]["avison_lease_facts"]["normalized_observations"]
    ) == [26.5, 30]


@pytest.mark.parametrize(
    "status", ["Draft", "Leased", "Available - November 1, 2026", None, "Unknown"]
)
def test_only_current_available_projects(status):
    assert stage(listing(statuses=[status]))["lease_rate_min"] is None


def test_no_new_rate_from_legacy_unproven_feed():
    item = listing()
    del item["extraFacts"]
    assert stage(item)["lease_rate_min"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("external_id", "foreign"),
        ("listing_url", "https://www.avisonyoung.us/properties/other"),
        ("schema", "future"),
    ],
)
def test_binding_mismatch_refuses(field, value):
    item = listing()
    item["extraFacts"]["avison_lease_facts"][field] = value
    with pytest.raises(ValueError):
        stage(item)


def test_raw_facts_are_not_mutated_by_stage():
    item = listing()
    saved = copy.deepcopy(item)
    result = stage(item)
    assert item == saved
    assert result["raw_data"]["extraFacts"] == saved["extraFacts"]


@pytest.mark.parametrize("other", ["$20/Month", "$20/ACRE/Year", "CAD 20/SF/Year"])
def test_explicit_other_basis_or_currency_blocks_parent_only(other):
    result = stage(listing(("$26.50/SF/Year", other)))
    assert result["lease_rate_min"] is None
    assert (
        result["extra_facts"]["avison_lease_facts"]["normalized_observations"][0][
            "annual_psf_min"
        ]
        == 26.5
    )


def test_conflicting_same_suite_is_one_ambiguous_unit():
    item = listing(("$26.50/SF/Year", "$20/SF/Month"))
    observations = item["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"]
    observations[1]["suite_text"] = observations[0]["suite_text"]
    result = stage(item)
    assert result["lease_rate_min"] is None
    projection = result["extra_facts"]["avison_lease_facts"]["projection"]
    assert (
        projection["available_count"],
        projection["ambiguous_available_count"],
        projection["priced_available_count"],
    ) == (1, 1, 0)


def test_identical_same_suite_not_double_counted():
    item = listing()
    page = item["extraFacts"]["avison_lease_facts"]["pages"][0]
    page["observations"].append(copy.deepcopy(page["observations"][0]))
    projection = stage(item)["extra_facts"]["avison_lease_facts"]["projection"]
    assert projection["available_count"] == projection["priced_available_count"] == 1


def test_overview_prose_not_a_rent_conflict():
    item = listing()
    facts = item["extraFacts"]["avison_lease_facts"]
    facts["pages"].append(
        dict(
            source_url=URL,
            observed_at=OBSERVED,
            role="overview",
            observations=[
                dict(
                    suite_text=None,
                    status_text=None,
                    rent_text=None,
                    area_text=None,
                    lease_type_text=None,
                    fields=[],
                    unparsed=["Attractive office near retail."],
                )
            ],
        )
    )
    assert stage(item)["lease_rate_min"] == 30


def test_explicit_overview_conflict_preserved_without_winner():
    item = listing()
    facts = item["extraFacts"]["avison_lease_facts"]
    observation = copy.deepcopy(facts["pages"][0]["observations"][0])
    observation["rent_text"] = "$2.00/SF/Month"
    facts["pages"].append(
        dict(
            source_url=URL,
            observed_at=OBSERVED,
            role="overview",
            observations=[observation],
        )
    )
    result = stage(item)
    assert result["lease_rate_min"] is None
    assert len(result["extra_facts"]["avison_lease_facts"]["pages"]) == 2


def test_total_monthly_known_type_is_independent_fact():
    item = listing(("$2,000/Month",))
    item["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][0][
        "lease_type_text"
    ] = "Modified Gross"
    result = stage(item)
    assert result["lease_rate_min"] is None
    assert result["lease_rate_type"] == "modified_gross"


def test_reversed_monetary_range_is_not_silently_reordered():
    assert stage(listing(("$20 - $10/SF/Year",)))["lease_rate_min"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("observed_at", "2026-10-17T00:00:00Z"),
        ("source_url", "https://www.avisonyoung.us/properties/foreign"),
        ("role", "unknown"),
    ],
)
def test_page_provenance_refuses(field, value):
    item = listing()
    item["extraFacts"]["avison_lease_facts"]["pages"][0][field] = value
    with pytest.raises(ValueError):
        stage(item)


def test_foreign_currency_context_not_overridden_by_dollar_symbol():
    item = listing()
    item["rawSharpLaunch"]["sale_currency"] = "CAD"
    assert stage(item)["lease_rate_min"] is None


def test_unknown_schema_never_borrows_another_source():
    item = listing()
    item["sourceKey"] = "cbre"
    assert stage(item)["lease_rate_min"] == 2.5


@pytest.mark.parametrize(
    "symbol,context,expected",
    [
        ("USD", None, 30),
        ("US$", "USD", 30),
        ("$", "USD", 30),
        ("$", None, None),
        ("CAD", "USD", None),
        ("AUD", "USD", None),
        ("EUR", "USD", None),
        ("£", "USD", None),
    ],
)
def test_currency_inference_is_honest_and_contextual(symbol, context, expected):
    item = listing((symbol + "2.50/SF/Month",))
    item["rawSharpLaunch"]["sale_currency"] = context
    result = stage(item)
    assert result["lease_rate_min"] == expected
    if expected:
        record = result["extra_facts"]["avison_lease_facts"]["normalized_observations"][
            0
        ]
        assert record["currency_evidence"] == (
            "inferred_us_source_context" if symbol == "$" else "explicit"
        )


def test_unknown_and_malformed_overview_rent_do_not_disappear():
    for text in ("unknown rent quotation", "$2,00/month", "20 per unit"):
        item = listing()
        page = copy.deepcopy(item["extraFacts"]["avison_lease_facts"]["pages"][0])
        page["role"] = "overview"
        page["observations"][0]["rent_text"] = text
        item["extraFacts"]["avison_lease_facts"]["pages"].append(page)
        assert stage(item)["lease_rate_min"] is None


def test_price_ambiguity_does_not_hide_recognized_different_period():
    item = listing(("$26.50/SF/Year", "$2.50/SF/Month unknown"))
    assert stage(item)["lease_rate_min"] is None


def test_shallow_projection_keeps_unrelated_heavy_objects_by_identity():
    from cre_avison_lease_facts import project_row

    original = stage(listing())
    projected = project_row(original)
    assert projected["raw_data"] is original["raw_data"]
    assert projected["images"] is original["images"]
    assert projected["extra_facts"] is not original["extra_facts"]


@pytest.mark.parametrize(
    "field,text",
    [
        ("rent_text", "Sale price $20"),
        ("status_text", "Available asking price $20"),
        ("suite_text", "Sale Price $20"),
    ],
)
def test_hidden_redaction_keeps_required_literal_null_not_laundered(field, text):
    item = listing()
    item["rawSharpLaunch"].update(hide_price=1, sale_price=20)
    observation = item["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][
        0
    ]
    observation[field] = text
    result = stage(item)
    actual = result["raw_data"]["extraFacts"]["avison_lease_facts"]["pages"][0][
        "observations"
    ][0]
    assert actual[field] is None
    assert result["lease_rate_min"] is None
    assert "$20" not in str(result["raw_data"])


def test_hidden_asking_amount_does_not_erase_a_literal_numeric_suite_identifier():
    item = listing()
    item["rawSharpLaunch"].update(hide_price=1, sale_price=20)
    observation = item["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][
        0
    ]
    observation["suite_text"] = "20"
    result = stage(item)
    actual = result["raw_data"]["extraFacts"]["avison_lease_facts"]["pages"][0][
        "observations"
    ][0]
    assert actual["suite_text"] == "20"
    assert result["lease_rate_min"] == 30


@pytest.mark.parametrize(
    "other", ["$2.50/SF/Week", "$20", "Unspecified monetary terms"]
)
def test_duplicate_labelled_rent_cannot_select_only_supported_claim(other):
    item = listing()
    observation = item["extraFacts"]["avison_lease_facts"]["pages"][0]["observations"][
        0
    ]
    observation["rent_text"] = None
    observation["fields"] = [
        {"label": "Rent", "value": "$2.50/SF/Month"},
        {"label": "Rental Rate", "value": other},
    ]
    assert stage(item)["lease_rate_min"] is None


@pytest.mark.parametrize(
    "external", [None, "  https://www.avisonyoung.us/properties/test-lease  "]
)
def test_feed_canonical_url_uses_existing_clean_external_or_native_fallback(external):
    item = listing()
    item["rawSharpLaunch"]["external_url"] = external
    item["rawSharpLaunch"]["url"] = URL
    assert stage(item)["lease_rate_min"] == 30
