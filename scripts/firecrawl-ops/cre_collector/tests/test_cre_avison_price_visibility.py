import pytest
import cre_ingest as ingest


def listing(control=1, **changes):
    value = {
        "sourceKey": "avison-young",
        "id": "test-price",
        "url": "https://www.avisonyoung.us/properties/test-price",
        "name": "Test property",
        "transactionMode": "sale",
        "transactionType": "Sale",
        "buildingSizeSqft": 1000,
        "salePriceUsd": 2000000,
        "salePriceText": "$2,000,000",
        "leaseRateMin": 20,
        "leaseRateMax": 25,
        "leaseRateType": "SF/YR",
        "noi": 50,
        "capRatePct": 5,
        "grm": 9,
        "rawSharpLaunch": {"hide_price": control, "sale_price": 2000000},
        "description": "Great frontage. Asking price $2,000,000. Rent $20/SF/YR.",
        "detailObservedAt": "2026-10-16T22:00:00Z",
    }
    value.update(changes)
    return value


def test_hidden_asking_cannot_enter_canonical_stage():
    row = ingest.to_row(listing(), {}, None)
    assert row["sale_price_usd"] is None
    assert row["sale_price_per_sf"] is None
    assert row["lease_rate_min"] == 20
    assert row["noi"] == 50
    assert row["grm"] == 9
    assert row["description"] == "Great frontage. Rent $20/SF/YR."
    assert row["raw_data"]["rawSharpLaunch"] == {"hide_price": 1}


@pytest.mark.parametrize("control", [True, False, None, "1", 2])
def test_malformed_raw_control_refuses_before_merge(control):
    with pytest.raises(ValueError, match="Avison"):
        ingest.to_row(listing(control), {}, None)


@pytest.mark.parametrize("reverse", [False, True])
def test_hidden_pass_wins_both_merge_orders_without_erasing_independent_facts(reverse):
    rows = [ingest.to_row(listing(0), {}, None), ingest.to_row(listing(1), {}, None)]
    if reverse:
        rows.reverse()
    row = ingest.merge_rows(*rows)
    assert row["sale_price_usd"] is None
    assert row["sale_price_per_sf"] is None
    assert row["lease_rate_min"] == 20
    assert row["grm"] == 9
    assert "2000000" not in str(row["raw_data"])


def test_other_source_marker_does_not_grant_asking_clear_authority():
    value = listing(
        sourceKey="svn",
        avisonPriceVisibility={
            "schema": "avison-asking-price-v1",
            "control": "withheld",
        },
    )
    row = ingest.to_row(value, {}, None)
    assert row["sale_price_usd"] == 2000000


def test_mismatched_current_marker_refuses():
    with pytest.raises(ValueError, match="Avison"):
        ingest.to_row(
            listing(
                avisonPriceVisibility={
                    "schema": "avison-asking-price-v1",
                    "control": "visible",
                }
            ),
            {},
            None,
        )


def test_unknown_price_unit_input_refuses_instead_of_inventing_sale_authority():
    with pytest.raises(ValueError, match="ambiguous"):
        ingest.to_row(listing(pricePerUnit=10, pricePerAcre=30), {}, None)


def test_independent_staged_economics_are_not_cleared():
    import cre_avison_price_visibility as visibility

    row = ingest.to_row(listing(), {}, None)
    row.update(price_per_unit=10, price_per_acre=30, grm=9)
    projected = visibility.project_row(row)
    assert [projected[k] for k in ("price_per_unit", "price_per_acre", "grm")] == [
        10,
        30,
        9,
    ]


def _vectors():
    import json
    from pathlib import Path

    return json.loads(
        (Path(__file__).parent / "fixtures/avison-asking-price-v1.json").read_text()
    )["vectors"]


@pytest.mark.parametrize("vector", _vectors())
def test_frozen_source_truth_vectors(vector):
    import cre_avison_price_visibility as visibility

    value, expected, role = vector["input"], vector["expected"], vector["role"]

    def apply():
        if role == "control":
            return visibility.asking_control(
                {} if value == {"missing": True} else {"hide_price": value}
            )
        if role in {"raw", "identity"}:
            return visibility.project_value(value, [20])
        return visibility.sanitize_text(value, [20], role)

    if expected in ("REFUSE", "REFUSE_AMBIGUOUS_FINANCIAL_INPUT"):
        with pytest.raises(ValueError):
            apply()
    else:
        assert apply() == expected


def postgres_vector_sql():
    import json
    import cre_avison_price_visibility as visibility

    statements = [
        "BEGIN;",
        visibility.sql_policy([ingest.to_row(listing(), {}, None)])["definitions"],
    ]
    extra = [
        {
            "role": "raw",
            "input": {"askingBrochureUrl": "https://example.invalid/20.pdf"},
            "expected": {"askingBrochureUrl": "https://example.invalid/20.pdf"},
        },
        {
            "role": "raw",
            "input": {
                "askingDetails": {
                    "amount": 20,
                    "newMetadata": {"energy": {"value": 20, "unit": "kWh"}},
                }
            },
            "expected": {
                "askingDetails": {
                    "newMetadata": {"energy": {"value": 20, "unit": "kWh"}}
                }
            },
        },
        {"role": "narrative", "input": "Asking price $20/SF/YR", "expected": ""},
        {
            "role": "narrative",
            "input": "Offered at $20/SF/YR",
            "expected": "Offered at $20/SF/YR",
        },
        {
            "role": "raw",
            "input": {"grossRevenue": "$20. Asking price $20.", "revpar": "$20"},
            "expected": {"grossRevenue": "$20.", "revpar": "$20"},
        },
        {
            "role": "narrative",
            "input": "Roof replaced 2020. 20 SF. 20%. 20 years.",
            "expected": "Roof replaced 2020. 20 SF. 20%. 20 years.",
        },
        {"role": "raw", "input": {"pricing": {"amount": 20}}, "expected": "REFUSE"},
    ]
    extra.append(
        {
            "role": "raw",
            "input": {
                "noi": {"amount": 20, "currency": "USD", "note": "Asking price $20"},
                "lease": {"amount": 20, "basis": "SF/YR", "note": "Asking price $20"},
            },
            "expected": {
                "noi": {"amount": 20, "currency": "USD"},
                "lease": {"amount": 20, "basis": "SF/YR"},
            },
        }
    )
    extra += [
        {
            "role": "narrative",
            "input": "Retail frontage. " + amount + " available. Zoned retail.",
            "expected": "Retail frontage. Zoned retail.",
        }
        for amount in ("30 dollars", "USD 30", "30 USD", "¥30", "30 CAD")
    ]
    extra += [
        {
            "role": "narrative",
            "input": "Rent USD 30/SF/YR. NOI 30 dollars. 30 million SF.",
            "expected": "Rent USD 30/SF/YR. NOI 30 dollars. 30 million SF.",
        },
        {
            "role": "raw",
            "input": {
                "newLabel": "  phase 20  ",
                "energy": {"value": 20, "unit": "kWh"},
            },
            "expected": {
                "newLabel": "  phase 20  ",
                "energy": {"value": 20, "unit": "kWh"},
            },
        },
    ]
    extra += [
        {
            "role": "narrative",
            "input": "Excellent frontage. " + text + " available. Zoned retail.",
            "amounts": [amount],
            "expected": "Excellent frontage. Zoned retail.",
        }
        for amount, text in (
            (100000, "100,000.00"),
            (100000.5, "100,000.50"),
            (100000.5, "100000.50"),
            (100000, "100.00K"),
            (100000, "0.100M"),
        )
    ]
    extra += [
        {
            "role": "narrative",
            "input": "30 million SF. 20K SF.",
            "amounts": [30000000, 20000],
            "expected": "30 million SF. 20K SF.",
        },
        {
            "role": "narrative",
            "input": "1100000 available. 1,100,000.00 available.",
            "amounts": [100000],
            "expected": "1100000 available. 1,100,000.00 available.",
        },
    ]
    extra.append(
        {
            "role": "narrative",
            "input": "20 parking spaces. Asking price 20.",
            "expected": "20 parking spaces.",
        }
    )
    extra += _independent_fidelity_vectors()
    for index, vector in enumerate(_vectors() + extra):
        value, expected, role = vector["input"], vector["expected"], vector["role"]
        encoded = ingest.sql_lit(json.dumps(value)) + "::jsonb"
        amounts = (
            "ARRAY["
            + ",".join(str(x) for x in vector.get("amounts", [20]))
            + "]::numeric[]"
        )
        if role == "control":
            feed = {} if value == {"missing": True} else {"hide_price": value}
            raw = {"sourceKey": "avison-young", "rawSharpLaunch": feed}
            expr = (
                "to_jsonb(pg_temp.avison_asking_control("
                + ingest.sql_lit(json.dumps(raw))
                + "::jsonb))"
            )
        elif role in {"raw", "identity"}:
            expr = f"pg_temp.avison_asking_raw({encoded},{amounts})"
        else:
            expr = (
                "to_jsonb(pg_temp.avison_asking_text("
                + ingest.sql_lit(value)
                + ","
                + amounts
                + ","
                + ingest.sql_lit(role)
                + "))"
            )
        if expected in ("REFUSE", "REFUSE_AMBIGUOUS_FINANCIAL_INPUT"):
            statements.append(f"""DO $test$ BEGIN
              BEGIN PERFORM {expr}; RAISE EXCEPTION 'missing expected refusal';
              EXCEPTION WHEN OTHERS THEN IF SQLERRM NOT LIKE 'Avison%' THEN RAISE; END IF; END;
            END $test$;""")
        else:
            wanted = ingest.sql_lit(json.dumps(expected)) + "::jsonb"
            statements.append(
                f"DO $test$ BEGIN IF {expr} IS DISTINCT FROM {wanted} THEN RAISE EXCEPTION 'vector {index} mismatch: %', {expr}; END IF; END $test$;"
            )
    statements.append(
        "ROLLBACK;\\echo 'Avison 32 shared + 47 adversarial PostgreSQL parity passed'"
    )
    return "\n".join(statements)


def postgres_contract_sql():
    """Actual migrations and generated ingest; no hand-written substitute upsert."""
    import re
    from pathlib import Path

    directory = Path(ingest.__file__).parent.parent / "sql"
    schema = re.sub(
        r"^\\i (\S+)\s*$",
        lambda m: (directory / m[1]).read_text(),
        (directory / "000_run_all.sql").read_text(),
        flags=re.M,
    )
    schema += "\n" + "\n".join(
        [
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4330",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text(),
        ]
    )

    def make(key, control=0, **changes):
        item = listing(
            control,
            id=key,
            url="https://www.avisonyoung.us/properties/" + key,
            salePriceUsd=100000,
            rawSharpLaunch={"hide_price": control, "sale_price": 100000},
            description="Great frontage. Asking price $100,000. Rent $20/SF/YR.",
            markdown="Roof replaced 2020. 100000. Rent $20/SF/YR.",
            documents=[
                {
                    "url": "https://example.invalid/100000.pdf",
                    "title": "Brochure 100000; Rent $20/SF/YR",
                    "docType": "brochure",
                }
            ],
            media=[
                {
                    "url": "https://example.invalid/video",
                    "title": "Tour 100000; Rent $20/SF/YR",
                    "mediaType": "video",
                }
            ],
            brokerIds=[0],
            **({"preserveChildCollections": True} | changes),
        )
        return ingest.to_row(
            item,
            {
                0: {
                    "name": "Broker",
                    "email": "broker@example.invalid",
                    "title": "Broker 100000; Rent $20/SF/YR",
                    "license": "100000",
                }
            },
            "2026-10-16T20:00:00Z",
        )

    def generated(rows, when):
        summaries = [
            {
                "slug": slug,
                "discovered": sum(r["slug"] == slug for r in rows),
                "saved": sum(r["slug"] == slug for r in rows),
                "errors": 0,
                "notes": "synthetic",
            }
            for slug in sorted({r["slug"] for r in rows})
        ]
        return ingest.build_sql(rows, summaries, when, set())

    def check(condition, name):
        return (
            "DO $test$ BEGIN IF NOT ("
            + condition
            + ") THEN RAISE EXCEPTION 'Avison generated contract: "
            + name
            + "'; END IF; END $test$;"
        )

    seed = [make("old-raw"), make("old-column"), make("recovery")]
    foreign = make("foreign", sourceKey="svn")
    seed.append(foreign)
    statements = [
        postgres_vector_sql(),
        schema,
        generated(seed, "2026-10-16T20:00:00Z"),
    ]
    # Deliberately remove old asking aliases, leaving only the old canonical amount.
    statements += [
        """
UPDATE credeals.cre_listings SET
 raw_data=jsonb_build_object('sourceKey','avison-young','rawSharpLaunch',jsonb_build_object('hide_price',0),
 'description','Safe old facts. 100000. Rent $20/SF/YR.', 'noi',jsonb_build_object('amount',20,'note','Annual $20. Asking price $100,000.'), 'bodySha256',repeat('a',64),'detailObservedAt','2026-10-16T20:00:00Z'),
 markdown='Roof replaced 2020. 100,000.00 available. Rent $20/SF/YR.'
WHERE external_id='old-column';
CREATE TEMP TABLE protected_foreign AS SELECT to_jsonb(t) data FROM credeals.cre_listings t WHERE external_id='foreign';
CREATE TEMP TABLE old_state AS SELECT id, external_id, scraped_at, status, deleted_at FROM credeals.cre_listings;
CREATE TEMP TABLE old_index AS SELECT id,soft_deleted,observation_present,presence_generation,presence_changed_at FROM credeals.cre_source_index;
CREATE TEMP TABLE old_children AS SELECT 'contacts' kind,id,listing_id FROM credeals.cre_listing_contacts UNION ALL SELECT 'documents',id,listing_id FROM credeals.cre_listing_documents UNION ALL SELECT 'media',id,listing_id FROM credeals.cre_listing_media;
CREATE TEMP TABLE old_events AS SELECT * FROM credeals.cre_listing_events;
CREATE TEMP TABLE old_om AS SELECT * FROM credeals.cre_listing_om_facts;
CREATE TEMP TABLE old_baseline AS SELECT * FROM credeals.cre_source_baseline;
"""
    ]
    hidden = []
    for key in ("old-raw", "old-column", "recovery", "new-hidden"):
        row = make(key, 1)
        # Current data is valid but child-preserving/degraded: original SQL selects old raw/Markdown.
        row["raw_data"].update(
            detailError="synthetic detail unavailable",
            preserveChildCollections=True,
            preserveExistingMarkdown=True,
        )
        row["raw_data"]["rawSharpLaunch"] = {"hide_price": 1, "sale_price": 200000}
        row["raw_data"]["avisonPriceVisibility"] = {
            "schema": "avison-asking-price-v1",
            "control": "withheld",
        }
        hidden.append(row)
    statements.append(generated(hidden, "2026-10-16T21:00:00Z"))
    statements += [
        check(
            "NOT EXISTS(SELECT 1 FROM credeals.cre_listings WHERE external_id IN ('old-raw','old-column','recovery','new-hidden') AND (sale_price_usd IS NOT NULL OR sale_price_per_sf IS NOT NULL))",
            "two normalized columns cleared",
        ),
        check(
            "NOT EXISTS(SELECT 1 FROM credeals.cre_listings WHERE external_id IN ('old-raw','old-column','recovery') AND (markdown LIKE '%100000%' OR coalesce(raw_data->>'description','') LIKE '%100000%' OR jsonb_path_exists(raw_data,'$.**.sale_price')))",
            "retained old canonical amount removed",
        ),
        check(
            "(SELECT markdown FROM credeals.cre_listings WHERE external_id='old-column') LIKE '%Roof replaced 2020%'",
            "safe old prose retained",
        ),
        check(
            "(SELECT raw_data->>'bodySha256' FROM credeals.cre_listings WHERE external_id='old-column')=repeat('a',64)",
            "old body provenance retained",
        ),
        check(
            "NOT EXISTS(SELECT 1 FROM credeals.cre_listings t JOIN old_state o USING(id) WHERE t.external_id IN ('old-raw','old-column','recovery') AND (t.scraped_at IS DISTINCT FROM o.scraped_at OR t.status IS DISTINCT FROM o.status OR t.deleted_at IS DISTINCT FROM o.deleted_at))",
            "degraded clocks and lifecycle unchanged",
        ),
        check(
            "NOT EXISTS(SELECT 1 FROM credeals.cre_listings WHERE external_id IN ('old-raw','old-column','recovery') AND (lease_rate_min IS DISTINCT FROM 20 OR noi IS DISTINCT FROM 50 OR grm IS DISTINCT FROM 9))",
            "independent economics retained",
        ),
        check(
            "(SELECT to_jsonb(t) FROM credeals.cre_listings t WHERE external_id='foreign')=(SELECT data FROM protected_foreign)",
            "other source unchanged",
        ),
    ]
    for table in ("contacts", "documents", "media"):
        statements.append(
            check(
                f"NOT EXISTS(SELECT 1 FROM credeals.cre_listing_{table} c JOIN credeals.cre_listings t ON t.id=c.listing_id WHERE t.external_id IN ('old-raw','old-column','recovery') AND (c.title LIKE '%100000%' OR c.title NOT LIKE '%Rent $20/SF/YR%'))",
                table + " retained title cleaned and rent preserved",
            )
        )
    statements += [
        check(
            "(SELECT url FROM credeals.cre_listing_documents c JOIN credeals.cre_listings t ON t.id=c.listing_id WHERE t.external_id='old-column')='https://example.invalid/100000.pdf'",
            "resource URL retained",
        ),
        check(
            "(SELECT license FROM credeals.cre_listing_contacts c JOIN credeals.cre_listings t ON t.id=c.listing_id WHERE t.external_id='old-column')='100000'",
            "license retained",
        ),
        check(
            "(SELECT count(*) FROM credeals.cre_listing_events)=(SELECT count(*) FROM old_events)",
            "no fabricated lifecycle event",
        ),
        check(
            "NOT EXISTS((SELECT * FROM old_om EXCEPT ALL SELECT * FROM credeals.cre_listing_om_facts) UNION ALL (SELECT * FROM credeals.cre_listing_om_facts EXCEPT ALL SELECT * FROM old_om))",
            "OM unchanged",
        ),
        check(
            "NOT EXISTS((SELECT * FROM old_baseline EXCEPT ALL SELECT * FROM credeals.cre_source_baseline) UNION ALL (SELECT * FROM credeals.cre_source_baseline EXCEPT ALL SELECT * FROM old_baseline))",
            "baseline unchanged",
        ),
    ]
    statements.append(
        check(
            "NOT EXISTS(SELECT 1 FROM old_children o WHERE NOT EXISTS(SELECT 1 FROM (SELECT 'contacts' kind,id,listing_id FROM credeals.cre_listing_contacts UNION ALL SELECT 'documents',id,listing_id FROM credeals.cre_listing_documents UNION ALL SELECT 'media',id,listing_id FROM credeals.cre_listing_media) c WHERE c.kind=o.kind AND c.id=o.id AND c.listing_id=o.listing_id))",
            "retained child identity and membership",
        )
    )
    statements.append(
        check(
            "NOT EXISTS(SELECT 1 FROM credeals.cre_listing_price_history h JOIN credeals.cre_listings t ON t.id=h.listing_id WHERE t.external_id IN ('old-raw','old-column','recovery') AND h.sale_price_usd IS NOT NULL)",
            "history sees only final sanitized asking",
        )
    )
    statements.append(
        check(
            "NOT EXISTS(SELECT 1 FROM old_index o JOIN credeals.cre_source_index t USING(id) WHERE t.soft_deleted IS DISTINCT FROM o.soft_deleted OR t.observation_present IS DISTINCT FROM o.observation_present OR t.presence_generation IS DISTINCT FROM o.presence_generation OR t.presence_changed_at IS DISTINCT FROM o.presence_changed_at)",
            "source-index lifecycle preserved",
        )
    )
    statements.append(
        check(
            "(SELECT raw_data#>>'{noi,note}' FROM credeals.cre_listings WHERE external_id='old-column')='Annual $20.'",
            "retained independent financial prose kept while asking removed",
        )
    )
    # A later independently visible observation follows ordinary upsert restoration.
    recovery = make("recovery", 0, preserveChildCollections=False)
    recovery["sale_price_usd"] = 300000
    recovery["sale_price_per_sf"] = 300
    statements += [
        generated([recovery], "2026-10-16T22:00:00Z"),
        check(
            "(SELECT sale_price_usd=300000 AND sale_price_per_sf=300 FROM credeals.cre_listings WHERE external_id='recovery')",
            "visible recovery",
        ),
        "\\echo 'Avison actual generated SQL retention, children, recovery and protected scopes passed'",
    ]
    rollback_row = make("must-rollback", 1)
    rollback_sql = generated([rollback_row], "2026-10-16T23:00:00Z")
    before_commit = rollback_sql.rfind("COMMIT;")
    rollback_sql = (
        rollback_sql[:before_commit]
        + "\\set ON_ERROR_STOP off\nDO $$ BEGIN RAISE EXCEPTION 'synthetic rollback sentinel'; END $$;\nROLLBACK;\n\\set ON_ERROR_STOP on\n"
        + rollback_sql[before_commit + len("COMMIT;") :]
    )
    statements += [
        "\\set ON_ERROR_STOP off",
        rollback_sql,
        "\\set ON_ERROR_STOP on",
        check(
            "NOT EXISTS(SELECT 1 FROM credeals.cre_listings WHERE external_id='must-rollback')",
            "failed transaction rolls back inserted hidden row",
        ),
    ]
    return "\n".join(statements)


@pytest.mark.parametrize(
    "value", [None, [], {"hidePrice": 1}, {"hide_price": 1, "hidePrice": 1}]
)
def test_control_alias_or_nonobject_refuses(value):
    import cre_avison_price_visibility as visibility

    with pytest.raises(ValueError, match="Avison"):
        visibility.asking_control(value)


def test_source_provenance_is_not_borrowed_by_nested_direct_pass():
    import cre_avison_price_visibility as visibility

    raw = listing(1)
    raw["secondary_pass"] = {"sourceKey": "svn", "rawSharpLaunch": {"hide_price": 1}}
    with pytest.raises(ValueError, match="Avison"):
        visibility.effective_control(raw)


def test_retained_current_observation_is_idempotent_and_has_no_hints():
    import cre_avison_price_visibility as visibility

    raw = listing(0)
    raw["latestInventoryObservation"] = listing(1)
    row = ingest.to_row(raw, {}, None)
    assert visibility.project_row(row) == row
    assert row["raw_data"]["avisonPriceVisibility"]["control"] == "withheld"


def test_resource_identity_and_harmless_quantity_survive_asking_context():
    import cre_avison_price_visibility as visibility

    raw = {
        "askingBrochureUrl": "https://example.invalid/20.pdf",
        "askingDetails": {
            "amount": 20,
            "newMetadata": {"energy": {"value": 20, "unit": "kWh"}},
        },
    }
    assert visibility.project_value(raw, [20]) == {
        "askingBrochureUrl": "https://example.invalid/20.pdf",
        "askingDetails": {"newMetadata": {"energy": {"value": 20, "unit": "kWh"}}},
    }


def test_explicit_asking_label_wins_over_lease_basis_but_offered_at_does_not():
    import cre_avison_price_visibility as visibility

    assert visibility.sanitize_text("Asking price $20/SF/YR", [20]) == ""
    assert (
        visibility.sanitize_text("Offered at $20/SF/YR", [20]) == "Offered at $20/SF/YR"
    )


def test_metadata_asking_marker_cannot_smuggle_extra_public_values():
    import cre_avison_price_visibility as visibility

    with pytest.raises(ValueError, match="Avison"):
        visibility.project_value(
            {
                "avisonPriceVisibility": {
                    "schema": "avison-asking-price-v1",
                    "control": "withheld",
                    "amount": 20,
                }
            },
            [20],
        )


def test_independent_structured_role_survives_conflicting_asking_note():
    import cre_avison_price_visibility as visibility

    value = {
        "noi": {"amount": 20, "currency": "USD", "note": "Asking price $20"},
        "lease": {"amount": 20, "basis": "SF/YR", "note": "Asking price $20"},
    }
    assert visibility.project_value(value, [20]) == {
        "noi": {"amount": 20, "currency": "USD"},
        "lease": {"amount": 20, "basis": "SF/YR"},
    }


def test_default_sql_policy_is_empty_without_hidden_actual_avison_rows():
    import cre_avison_price_visibility as visibility

    for rows in (
        [],
        [ingest.to_row(listing(0), {}, None)],
        [ingest.to_row(listing(sourceKey="svn"), {}, None)],
    ):
        assert visibility.sql_policy(rows) == {
            "definitions": "",
            "prior_select": "",
            "post_sql": "",
        }


def test_malformed_second_direct_pass_is_checked_even_after_hidden_first():
    raw = listing(1)
    raw["secondary_pass"] = listing("1")
    with pytest.raises(ValueError, match="Avison"):
        ingest.to_row(raw, {}, None)


@pytest.mark.parametrize(
    "amount,text",
    [
        (100000, "100,000.00"),
        (100000.5, "100,000.50"),
        (100000.5, "100000.50"),
        (100000, "100.00K"),
        (100000, "0.100M"),
    ],
)
def test_known_asking_equivalent_decimal_variants_are_suppressed(amount, text):
    import cre_avison_price_visibility as visibility

    assert (
        visibility.sanitize_text(
            "Excellent frontage. " + text + " available. Zoned retail.", [amount]
        )
        == "Excellent frontage. Zoned retail."
    )


@pytest.mark.parametrize("text", ["30 dollars", "USD 30", "30 USD", "¥30", "30 CAD"])
def test_unqualified_currency_clause_is_omitted(text):
    import cre_avison_price_visibility as visibility

    assert (
        visibility.sanitize_text(
            "Retail frontage. " + text + " available. Zoned retail.", [20]
        )
        == "Retail frontage. Zoned retail."
    )


def test_independent_currency_and_scaled_quantity_stay_public():
    import cre_avison_price_visibility as visibility

    text = "Rent USD 30/SF/YR. NOI 30 dollars. 30 million SF."
    assert visibility.sanitize_text(text, [20]) == text


def test_harmless_new_unknown_text_is_retained_literally():
    import cre_avison_price_visibility as visibility

    raw = {"newLabel": "  phase 20  ", "energy": {"value": 20, "unit": "kWh"}}
    assert visibility.project_value(raw, [20]) == raw


def test_scaled_measurements_preserved_when_equal_to_known_asking():
    import cre_avison_price_visibility as visibility

    text = "30 million SF. 20K SF."
    assert visibility.sanitize_text(text, [30000000, 20000]) == text


def test_known_amount_cannot_match_suffix_of_larger_grouped_number():
    import cre_avison_price_visibility as visibility

    text = "1100000 available. 1,100,000.00 available."
    assert visibility.sanitize_text(text, [100000]) == text


def test_parking_quantity_preserved_but_bare_asking_number_is_not():
    import cre_avison_price_visibility as visibility

    assert (
        visibility.sanitize_text("20 parking spaces. Asking price 20.", [20])
        == "20 parking spaces."
    )
    assert visibility.sanitize_text("20 available. Asking price 20.", [20]) == ""


def test_incomplete_merge_wrapper_without_direct_source_authority_refuses():
    import cre_avison_price_visibility as visibility

    with pytest.raises(ValueError, match="Avison"):
        visibility.effective_control({"primary": listing(1)})


def _independent_fidelity_vectors():
    pairs = [
        (
            {"amount": 20, "currency": "USD", "note": "Annual $20"},
            {"amount": 20, "currency": "USD", "note": "Annual $20"},
        ),
        (
            {"value": 20, "description": "Annual $20"},
            {"value": 20, "description": "Annual $20"},
        ),
        ({"amount": 20, "note": "Asking price $20"}, {"amount": 20}),
        (
            {
                "amount": 20,
                "note": "Annual $20. Asking price $20.",
                "unit": "USD",
                "url": "https://example.test/asking/20.pdf",
                "id": "20",
                "address": "20 Main St",
            },
            {
                "amount": 20,
                "note": "Annual $20.",
                "unit": "USD",
                "url": "https://example.test/asking/20.pdf",
                "id": "20",
                "address": "20 Main St",
            },
        ),
        (
            {"value": 20, "unit": "kWh", "note": "Metered energy"},
            {"value": 20, "unit": "kWh", "note": "Metered energy"},
        ),
        (
            {"amount": 20, "description": "Rent $20/SF/YR. Asking price $20."},
            {"amount": 20, "description": "Rent $20/SF/YR."},
        ),
    ]
    return [
        {"role": "raw", "input": {key: before}, "expected": {key: after}}
        for key in ("noi", "rent", "grossRevenue", "revpar")
        for before, after in pairs
    ]


@pytest.mark.parametrize("vector", _independent_fidelity_vectors())
def test_nested_independent_economics_preserve_prose_without_retaining_asking(vector):
    import cre_avison_price_visibility as visibility

    assert visibility.project_value(vector["input"], [20]) == vector["expected"]
