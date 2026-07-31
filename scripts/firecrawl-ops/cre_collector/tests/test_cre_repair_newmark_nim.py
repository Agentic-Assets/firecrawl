import hashlib
import json
from unittest.mock import patch

import cre_repair_newmark_nim as repair
import pytest


def artifact_listing(
    *,
    provider_id,
    slug,
    external_url,
    unit,
    size,
    mode="sale",
    price="$1,000,000",
):
    canonical_id = external_url.rsplit("/", 1)[-1] if external_url else slug
    old_url = (
        f"https://www.nmrk.com/properties/{slug}"
        if external_url and external_url.startswith("https://nmrk.com/")
        else external_url or f"https://www.nmrk.com/properties/{slug}"
    )
    return {
        "id": slug,
        "url": old_url,
        "source_url": old_url,
        "transactionMode": mode,
        "rawNewmarkNimRecord": {
            "id": provider_id,
            "slug": slug,
            "externalWebsiteUrl": external_url,
            "priceSummary": price,
            "properties": [
                {
                    "countryCode": "US",
                    "stateAbbreviation": "OK",
                    "zip": "74103",
                    "unitOfMeasurement": unit,
                    "size": size,
                }
            ],
        },
        "_canonical_id_for_test": canonical_id,
    }


def test_canonical_identity_preserves_case_and_limits_fallback_hosts():
    assert repair.canonical_identity(
        {
            "slug": "internal-lower",
            "externalWebsiteUrl":
                "https://www.nmrk.com/properties/Canonical-Case-123?campaign=x",
        }
    ) == (
        "Canonical-Case-123",
        "https://www.nmrk.com/properties/Canonical-Case-123",
    )
    assert repair.canonical_identity(
        {
            "slug": "fallback-slug",
            "externalWebsiteUrl": "https://my.rcm1.com/handler/modern.aspx?pv=1",
        }
    ) == (
        "fallback-slug",
        "https://www.nmrk.com/properties/fallback-slug",
    )
    with pytest.raises(ValueError, match="unsupported"):
        repair.canonical_identity(
            {
                "slug": "unsafe-host",
                "externalWebsiteUrl":
                    "https://example.com/properties/unsafe-host",
            }
        )
    with pytest.raises(ValueError, match="safe HTTPS"):
        repair.canonical_identity(
            {
                "slug": "insecure",
                "externalWebsiteUrl":
                    "http://www.nmrk.com/properties/insecure",
            }
        )


@pytest.mark.parametrize(
    ("unit", "size", "expected"),
    [
        ("Sq. Ft.", 25_000, ("Sq. Ft.", 25_000.0, None, None)),
        ("Sq. Meters", 19_941.26, ("Sq. Meters", 19_941.26, None, None)),
        ("Units", 152, ("Units", None, None, 152)),
        ("Acres", 1.5, ("Acres", None, 65_340.0, None)),
        (
            "Hectares",
            2,
            ("Hectares", None, pytest.approx(215_278.2119272), None),
        ),
    ],
)
def test_normalized_measurement_routes_dimensions(unit, size, expected):
    actual = repair.normalized_measurement(
        {"unitOfMeasurement": unit, "size": size}
    )
    assert actual == expected


def test_load_plan_accepts_provider_ids_reused_across_sale_and_lease(tmp_path):
    rows = [
        artifact_listing(
            provider_id="same-provider",
            slug="sale-old",
            external_url="https://www.nmrk.com/properties/Sale-Canonical",
            unit="Sq. Ft.",
            size=1_000,
            mode="sale",
            price="$20,000,000",
        ),
        artifact_listing(
            provider_id="same-provider",
            slug="lease-stable",
            external_url="https://www.nmrk.com/properties/lease-stable",
            unit="Units",
            size=10,
            mode="lease",
        ),
    ]
    for row in rows:
        row.pop("_canonical_id_for_test")
    payload = {
        "runMeta": {
            "freshness": {"generationId": repair.EXPECTED_GENERATION}
        },
        "listings": rows,
    }
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(payload))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected_units = {
        "Sq. Ft.": 1,
        "Units": 1,
        "Acres": 0,
        "Hectares": 0,
        "Sq. Meters": 0,
    }
    with (
        patch.object(repair, "EXPECTED_ARTIFACT_SHA256", digest),
        patch.object(repair, "EXPECTED_LISTINGS", 2),
        patch.object(repair, "EXPECTED_IDENTITIES", 1),
        patch.object(repair, "EXPECTED_REJECTED_PRICES", 1),
        patch.object(repair, "EXPECTED_UNITS", expected_units),
    ):
        plan = repair.load_plan(path)
    assert [row.provider_id for row in plan] == [
        "same-provider",
        "same-provider",
    ]
    assert plan[0].old_id == "sale-old"
    assert plan[0].canonical_id == "Sale-Canonical"
    assert plan[0].rejected_price is True


def test_apply_sql_moves_all_aliases_before_final_identity_assignment():
    # Use distinct old IDs while retaining a desired-ID overlap.
    rows = [
        repair.PlanRow(
            provider_id="1",
            old_id="a-old",
            old_url="https://www.nmrk.com/properties/b-old",
            canonical_id="b-old",
            canonical_url="https://www.nmrk.com/properties/b-old",
            transaction_mode="sale",
            unit="Units",
            size_sf=None,
            lot_size_sf=None,
            units=12,
            rejected_price=False,
        ),
        repair.PlanRow(
            provider_id="2",
            old_id="b-old",
            old_url="https://www.nmrk.com/properties/b-canonical",
            canonical_id="b-canonical",
            canonical_url="https://www.nmrk.com/properties/b-canonical",
            transaction_mode="sale",
            unit="Acres",
            size_sf=None,
            lot_size_sf=43_560,
            units=None,
            rejected_price=False,
        ),
    ]
    sql = repair.build_apply_sql(rows, valid_preimage())
    move = sql.index("SET external_id=a.temp_id")
    final = sql.index("WHEN a.survivor_id IS NULL THEN a.canonical_id")
    assert move < final
    assert "nim-superseded:" in sql
    assert "old_pairs <> 0" in sql


def test_apply_sql_contains_required_safety_and_data_quality_boundaries():
    row = repair.PlanRow(
        provider_id="1",
        old_id="old",
        old_url="https://www.nmrk.com/properties/old",
        canonical_id="canonical",
        canonical_url="https://www.nmrk.com/properties/canonical",
        transaction_mode="sale",
        unit="Sq. Meters",
        size_sf=19_941.26,
        lot_size_sf=None,
        units=None,
        rejected_price=True,
    )
    sql = repair.build_apply_sql([row], valid_preimage())
    for required in (
        "pg_advisory_xact_lock",
        "cre_listing_om_facts",
        "claimed queue rows",
        "unreviewed cre_listings FK surface",
        "cre_listing_contacts",
        "cre_listing_documents",
        "cre_listing_images",
        "cre_listing_media",
        "cre_listing_links",
        "cre_source_index",
        "cre_enrichment_queue",
        "WHEN p.rejected_price THEN NULL",
        "WHEN p.unit='Units' AND p.units IS NOT NULL THEN NULL",
        "WHEN p.unit IN ('Acres','Hectares') AND p.lot_size_sf IS NOT NULL",
        "active duplicate URL groups",
        "postcondition dimension mismatches",
        "latestInventoryObservation",
        "postcondition current-generation canonical rows",
    ):
        assert required in sql
    assert "UPDATE credeals.cre_listing_events" not in sql
    assert "UPDATE credeals.cre_listing_price_history" not in sql
    assert "UPDATE credeals.cre_scrape_log" not in sql
    assert "'postimageSha256', (SELECT sha256 FROM _nim_postimage_state)" in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_apply_is_bound_to_exact_reviewed_state_before_first_mutation():
    row = repair.PlanRow(
        provider_id="1",
        old_id="old",
        old_url="https://www.nmrk.com/properties/old",
        canonical_id="canonical",
        canonical_url="https://www.nmrk.com/properties/canonical",
        transaction_mode="sale",
        unit="Sq. Ft.",
        size_sf=10_000,
        lot_size_sf=None,
        units=None,
        rejected_price=False,
    )
    sql = repair.build_apply_sql([row], valid_preimage())
    guard = sql.index("reviewed Newmark preimage state drifted")
    first_mutation = min(
        sql.index("UPDATE credeals.cre_source_index"),
        sql.index("UPDATE credeals.cre_enrichment_queue"),
        sql.index("UPDATE credeals.cre_listings"),
    )
    assert "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'" in sql
    assert "digest(convert_to((payload)::text,'UTF8'),'sha256')" in sql
    assert "reviewed Newmark preimage payload digest is invalid" in sql
    assert guard < first_mutation


def test_preimage_contains_every_mutated_surface_and_history_counts():
    row = repair.PlanRow(
        provider_id="1",
        old_id="old",
        old_url="https://www.nmrk.com/properties/old",
        canonical_id="canonical",
        canonical_url="https://www.nmrk.com/properties/canonical",
        transaction_mode="sale",
        unit="Sq. Ft.",
        size_sf=10_000,
        lot_size_sf=None,
        units=None,
        rejected_price=False,
    )
    sql = repair.build_preimage_sql([row])
    for key in (
        "'capturedAt'",
        "'reviewedStateSha256'",
        "'identityMap'",
        "'identityListings'",
        "'dqColumns'",
        "'contacts'",
        "'documents'",
        "'images'",
        "'media'",
        "'links'",
        "'sourceIndex'",
        "'queue'",
        "'retainedHistoryCounts'",
    ):
        assert key in sql
    assert "digest(convert_to((payload)::text,'UTF8'),'sha256')" in sql
    assert "ORDER BY q.brokerage_id,q.external_id,q.reason,q.id" in sql
    assert sql.rstrip().endswith("ROLLBACK;")


def test_private_preimage_is_atomic_and_owner_only(tmp_path):
    path = tmp_path / "preimage.json"
    repair.atomic_private_json(path, {"ok": True})
    assert json.loads(path.read_text()) == {"ok": True}
    assert path.stat().st_mode & 0o777 == 0o600


def valid_preimage():
    return {
        "schemaVersion": repair.PREIMAGE_SCHEMA_VERSION,
        "capturedAt": "2026-07-30T04:30:00+00:00",
        "generation": repair.EXPECTED_GENERATION,
        "artifactSha256": repair.EXPECTED_ARTIFACT_SHA256,
        "databaseTargetSha256": repair.EXPECTED_DB_TARGET_SHA256,
        "reviewedStateSha256": "a" * 64,
        "identityMap": [
            {
                "alias_id": f"alias-{i}",
                "survivor_id": f"survivor-{i}" if i < 27 else None,
                "alias_generation": repair.EXPECTED_GENERATION,
                "alias_inventory_observed_at":
                    repair.EXPECTED_OBSERVED_MIN,
            }
            for i in range(repair.EXPECTED_IDENTITIES)
        ],
        "identityListings": [{"id": str(i)} for i in range(62)],
        "dqColumns": [{"id": str(i)} for i in range(repair.EXPECTED_LISTINGS)],
        "contacts": [],
        "documents": [],
        "images": [],
        "media": [],
        "links": [],
        "sourceIndex": [],
        "queue": [],
    }


def test_rollback_sql_restores_promoted_survivor_fields():
    row = repair.PlanRow(
        provider_id="1",
        old_id="old",
        old_url="https://www.nmrk.com/properties/old",
        canonical_id="canonical",
        canonical_url="https://www.nmrk.com/properties/canonical",
        transaction_mode="sale",
        unit="Sq. Ft.",
        size_sf=10_000,
        lot_size_sf=None,
        units=None,
        rejected_price=False,
    )
    sql = repair.build_rollback_sql([row], valid_preimage(), "b" * 64)
    for restored in (
        "transaction_type=p.transaction_type",
        "property_type=p.property_type",
        "sale_price_usd=p.sale_price_usd",
        "scraped_at=p.scraped_at",
        "source_lastmod=p.source_lastmod",
        "canonical_key=p.canonical_key",
        "updated_at=clock_timestamp()",
    ):
        assert restored in sql
    assert "'mode','rollback_applied'" in sql
    assert "SET external_id='nim-rollback:' || md5(l.id::text)" in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_rollback_refuses_exact_postimage_drift_before_first_mutation():
    row = repair.PlanRow(
        provider_id="1",
        old_id="old",
        old_url="https://www.nmrk.com/properties/old",
        canonical_id="canonical",
        canonical_url="https://www.nmrk.com/properties/canonical",
        transaction_mode="sale",
        unit="Sq. Ft.",
        size_sf=10_000,
        lot_size_sf=None,
        units=None,
        rejected_price=False,
    )
    sql = repair.build_rollback_sql([row], valid_preimage(), "b" * 64)
    guard = sql.index("rollback refused: Newmark postimage SHA-256 drifted")
    first_mutation = min(
        sql.index("DELETE FROM credeals.cre_listing_contacts"),
        sql.index("UPDATE credeals.cre_listings"),
    )
    assert "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'" in sql
    assert "_nim_live_postimage_state" in sql
    assert "rollback preimage reviewed-state SHA-256 is invalid" in sql
    assert guard < first_mutation


def test_rollback_roundtrip_forces_outer_rollback_and_reuses_no_stage_tables():
    row = repair.PlanRow(
        provider_id="1",
        old_id="old",
        old_url="https://www.nmrk.com/properties/old",
        canonical_id="canonical",
        canonical_url="https://www.nmrk.com/properties/canonical",
        transaction_mode="sale",
        unit="Sq. Ft.",
        size_sf=10_000,
        lot_size_sf=None,
        units=None,
        rejected_price=False,
    )
    sql = repair.build_rollback_roundtrip_sql([row], valid_preimage())
    assert sql.count("BEGIN ISOLATION LEVEL SERIALIZABLE;") == 1
    assert "DROP TABLE _nim_aliases;" in sql
    assert "DROP TABLE _nim_live_reviewed_state;" in sql
    assert "(SELECT sha256 FROM _nim_postimage_state)" in sql
    assert "'mode','rollback_applied'" in sql
    assert sql.rstrip().endswith("ROLLBACK;")


def test_preimage_validation_requires_reviewed_state_digest():
    payload = valid_preimage()
    payload.pop("reviewedStateSha256")
    with pytest.raises(ValueError, match="reviewed-state SHA-256"):
        repair.validate_preimage(payload)
    payload["reviewedStateSha256"] = "A" * 64
    with pytest.raises(ValueError, match="reviewed-state SHA-256"):
        repair.validate_preimage(payload)


def test_private_preimage_loader_binds_exact_file_digest(tmp_path):
    path = tmp_path / "preimage.json"
    path.write_text(json.dumps(valid_preimage()))
    path.chmod(0o600)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    payload, actual = repair.load_private_preimage(path, expected)
    assert payload == valid_preimage()
    assert actual == expected
    with pytest.raises(ValueError, match="does not match"):
        repair.load_private_preimage(path, "0" * 64)


def test_private_preimage_loader_rejects_broad_permissions(tmp_path):
    path = tmp_path / "preimage.json"
    path.write_text(json.dumps(valid_preimage()))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="group- or world-accessible"):
        repair.load_private_preimage(
            path,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize(
    "digest_args",
    [
        ["--expected-preimage-sha256", "a" * 64],
        ["--expected-postimage-sha256", "b" * 64],
        [],
    ],
)
def test_persistent_rollback_requires_both_reviewed_digests(digest_args):
    argv = [
        "cre_repair_newmark_nim.py",
        "--env-file",
        "unused.env",
        "--rollback-preimage",
        "/tmp/preimage.json",
        *digest_args,
    ]
    with (
        patch.object(repair.sys, "argv", argv),
        pytest.raises(SystemExit) as exc_info,
    ):
        repair.main()
    assert exc_info.value.code == 2


def test_cli_refuses_an_alternate_cre_lock_path(tmp_path):
    argv = [
        "cre_repair_newmark_nim.py",
        "--env-file",
        "unused.env",
        "--lock-dir",
        str(tmp_path / ".cre.lock"),
    ]
    with (
        patch.object(repair.sys, "argv", argv),
        pytest.raises(SystemExit) as exc_info,
    ):
        repair.main()
    assert exc_info.value.code == 2


def test_repair_uses_the_checkpoint_runners_shared_directory_lock(tmp_path):
    lock_dir = tmp_path / ".cre.lock"
    with repair.shared_cre_lock(lock_dir):
        assert lock_dir.is_dir()
        assert int((lock_dir / "pid").read_text().split()[0]) > 0
    assert not lock_dir.exists()


def test_repair_safely_migrates_its_empty_legacy_file_lock(tmp_path):
    lock_dir = tmp_path / ".cre.lock"
    lock_dir.write_text("")
    with repair.shared_cre_lock(lock_dir):
        assert lock_dir.is_dir()
        assert (lock_dir / "pid").is_file()
    assert not lock_dir.exists()


def test_repair_refuses_to_migrate_an_actively_held_legacy_lock(tmp_path):
    lock_dir = tmp_path / ".cre.lock"
    lock_dir.write_text("")
    with (
        patch.object(repair.fcntl, "flock", side_effect=BlockingIOError),
        pytest.raises(RuntimeError, match="actively held"),
    ):
        with repair.shared_cre_lock(lock_dir):
            pass
    assert lock_dir.is_file()
