"""Exercise the production ingest builder in an owned socket-only PG17 cluster."""

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(HERE.parent), str(HERE.parent / "observation_store")]
import cre_ingest as ingest
from owned_observation_fixture_cluster import OwnedCluster, tool
from source_fields.buildout import qualified_counts
from source_fields.transwestern import qualified_building_fields
from test_buildout_current_inventory import setup
from test_transwestern_source_fields import artifact as building_artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute-disposable-fixture", action="store_true", required=True
    )
    parser.add_argument("--real-artifact", action="append", type=Path, default=[])
    parser.add_argument(
        "--real-building-artifact", action="append", type=Path, default=[]
    )
    args = parser.parse_args()
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    with OwnedCluster(initdb, pg_ctl, port=55463) as cluster:

        def sql(statement):
            result = subprocess.run(
                [
                    psql,
                    "-X",
                    "-qAt",
                    "-h",
                    str(cluster.root),
                    "-p",
                    str(cluster.port),
                    "-d",
                    "postgres",
                    "-v",
                    "ON_ERROR_STOP=1",
                ],
                input=statement,
                capture_output=True,
                text=True,
                timeout=120,
                env=cluster.env,
                check=False,
            )
            if result.returncode:
                raise AssertionError(result.stderr)
            return result.stdout

        directory = ROOT.parent / "sql"
        schema = re.sub(
            r"^\\i (\S+)\s*$",
            lambda m: (directory / m[1]).read_text(),
            (directory / "000_run_all.sql").read_text(),
            flags=re.MULTILINE,
        )
        schema += "\n" + "\n".join(
            [
                "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
                "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
                "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-1",
                "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
                (directory / "016_cre_listing_lifecycle.sql").read_text(),
            ]
        )
        sql(schema)
        # Match the existing production column, with no production migration.
        sql(
            "ALTER TABLE credeals.cre_listings ADD COLUMN available_space_count integer CHECK (available_space_count >= 0), ADD COLUMN elevator_count integer CHECK (elevator_count >= 0), ADD COLUMN typical_floor_size_text text;"
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, _, artifact, *_ = setup(Path(tmp))
            template = next(
                row for row in artifact["listings"] if row["transactionMode"] == "lease"
            )
            proof = artifact["sources"][0]["currentInventoryProof"]
            item_template = next(
                row
                for page in proof["pages"]
                for row in page["rows"]
                if not row["sale"] and not row["closed"]
            )
            listings, items = [], []
            for i in range(60):
                native = str(1000 + i)
                url = (
                    template["url"].split("propertyId=")[0]
                    + "propertyId="
                    + native
                    + "-lease"
                )
                listing = copy.deepcopy(template)
                listing.update(
                    id=native,
                    url=url,
                    canonicalUrl=url,
                    name=f"Synthetic {i}",
                    extraFacts={"fixture": "protected"},
                )
                listings.append(listing)
                item = copy.deepcopy(item_template)
                item.update(id=native, showLink=url)
                items.append(item)
            artifact["listings"] = listings
            artifact["totalListings"] = len(listings)
            for entry in artifact["sources"]:
                entry["listingsCollected"] = (
                    60 if entry["transaction"] == "lease" else 0
                )
                entry["totalAvailableOnSource"] = 60
                entry["currentInventoryProof"]["total"] = 60
                entry["currentInventoryProof"]["limit"] = 60
                page = copy.deepcopy(proof["pages"][0])
                page.update(index=0, total=60, limit=60, rows=items)
                entry["currentInventoryProof"]["pages"] = [page]

            def generated(value, *, timestamp=None, records=None, mutate=None):
                data = copy.deepcopy(artifact)
                if timestamp:
                    for entry in data["sources"]:
                        entry["currentInventoryProof"]["pages"][0]["observedAt"] = (
                            timestamp
                        )
                    for listing in data["listings"]:
                        listing["inventoryObservedAt"] = timestamp
                if records is not None:
                    data["listings"] = records
                for listing in data["listings"]:
                    listing["buildoutFieldObservations"] = {
                        "schema": "buildout-field-observations-v1",
                        "malformedAttributeCount": 0,
                        "availableSpaceCount": {
                            "state": "observed",
                            "literals": [
                                {"sourceOrdinal": 0, "literal": f"{value} Spaces"}
                            ],
                            "omittedCount": 0,
                            "value": value,
                        },
                    }
                if mutate:
                    mutate(data)
                raw = json.dumps(data, sort_keys=True).encode()
                ingest.validate_strict_artifact_freshness(
                    data, require_strict_freshness=True
                )
                candidates = qualified_counts(data, hashlib.sha256(raw).hexdigest())
                assert sum(
                    "available_space_count" in decision["values"]
                    for decision in candidates.values()
                ) == (57 if mutate else 60)
                rows = [
                    ingest.to_row(
                        listing, {}, data["runMeta"]["finishedAt"], candidates
                    )
                    for listing in data["listings"]
                ]
                assert all(rows) and len({row["external_id"] for row in rows}) == 60
                text = ingest.build_sql(
                    rows,
                    [],
                    data["runMeta"]["finishedAt"],
                    set(),
                    skip_post_commit_summary=True,
                )
                assert "postgresql://" not in text
                return text

            first = artifact["sources"][0]["currentInventoryProof"]["pages"][0][
                "observedAt"
            ]
            sql(generated(0))

            def counts():
                return json.loads(
                    sql(
                        "SELECT json_agg(json_build_array(external_id,available_space_count,extra_facts) ORDER BY external_id) FROM credeals.cre_listings;"
                    )
                )

            actual = counts()
            assert len(actual) == 60 and all(
                row[1] == 0 and row[2]["fixture"] == "protected" for row in actual
            ), actual[:1]
            sql(generated(4))
            assert all(row[1] == 0 for row in counts()), (
                "equal timestamp conflict changed count"
            )
            newer = (datetime.fromisoformat(first) + timedelta(seconds=1)).isoformat()
            sql(generated(4, timestamp=newer))
            assert all(row[1] == 4 for row in counts())
            newest = (datetime.fromisoformat(first) + timedelta(seconds=2)).isoformat()
            sql(generated(0, timestamp=newest))
            assert all(row[1] == 0 for row in counts())
            sql(generated(4))
            assert all(row[1] == 0 for row in counts()), "older timestamp changed count"
            sql("""UPDATE credeals.cre_listings SET available_space_count=7, extra_facts=extra_facts - '_source_field_provenance' WHERE external_id='1000';
UPDATE credeals.cre_listings SET extra_facts=jsonb_set(extra_facts,'{_source_field_provenance,available_space_count,source_key}','"other-source"') WHERE external_id='1001';
INSERT INTO credeals.cre_listing_images(listing_id,url) SELECT id,'https://example.test/protected.jpg' FROM credeals.cre_listings WHERE external_id='1002';""")
            protected_before = sql(
                "SELECT json_agg(json_build_array(l.external_id,l.title,l.status,l.deleted_at,i.url) ORDER BY l.external_id)::text FROM credeals.cre_listings l LEFT JOIN credeals.cre_listing_images i ON i.listing_id=l.id;"
            )

            def hostile(data):
                for listing in data["listings"]:
                    listing["extraFacts"]["_source_field_provenance"] = {
                        "available_space_count": {"source_key": "attacker"}
                    }
                data["listings"][2]["buildoutFieldObservations"]["availableSpaceCount"][
                    "state"
                ] = "missing"
                data["listings"][3]["buildoutFieldObservations"]["availableSpaceCount"][
                    "literals"
                ][0]["literal"] = "invalid"
                data["listings"][4]["buildoutFieldObservations"]["availableSpaceCount"][
                    "literals"
                ].append({"sourceOrdinal": 1, "literal": "6 Spaces"})

            last = (datetime.fromisoformat(first) + timedelta(seconds=3)).isoformat()
            sql(generated(6, timestamp=last, mutate=hostile))
            result = {row[0]: row for row in counts()}
            assert result["1000"][1] == 7
            assert result["1001"][1] == 0
            assert all(result[str(i)][1] == 0 for i in (1002, 1003, 1004))
            assert all(result[str(i)][1] == 6 for i in range(1005, 1060))
            assert (
                result["1001"][2]["_source_field_provenance"]["available_space_count"][
                    "source_key"
                ]
                == "other-source"
            )
            assert all(row[2]["fixture"] == "protected" for row in result.values())
            assert (
                sql(
                    "SELECT json_agg(json_build_array(l.external_id,l.title,l.status,l.deleted_at,i.url) ORDER BY l.external_id)::text FROM credeals.cre_listings l LEFT JOIN credeals.cre_listing_images i ON i.listing_id=l.id;"
                )
                == protected_before
            )
            print(
                "PG17 full ingest: 60 distinct rows, NULL -> 0 -> 4 -> 0, equal/older, unowned, cross-source, malformed, reserved-key and child preservation checks passed"
            )
        # Property fields exercise the ordinary qualified producer -> stage -> SQL path.
        data = building_artifact()
        base = data["listings"][0]
        data["listings"] = []
        for i in range(8):
            row = copy.deepcopy(base)
            identity = f"synthetic-building-{i}"
            url = f"https://transwestern.com/property/{identity}"
            row.update(
                id=identity,
                url=url,
                name=f"Building {i}",
                extraFacts={"fixture": "protected"},
                photos=["https://example.test/building-protected.jpg"]
                if i == 3
                else [],
            )
            row["rawTranswesternFeed"]["PageUrl"] = identity
            row["transwesternBuildingFacts"]["sourceUrl"] = url
            data["listings"].append(row)
        first = data["listings"][0]["detailObservedAt"]

        def building_statement(elevator, floor, delta, mutate=None):
            candidate_data = copy.deepcopy(data)
            timestamp = (
                (
                    datetime.fromisoformat(first.replace("Z", "+00:00"))
                    + timedelta(seconds=delta)
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
            for listing in candidate_data["listings"]:
                facts = listing["transwesternBuildingFacts"]
                listing["detailObservedAt"] = facts["observedAt"] = timestamp
                facts["elevatorCount"] = elevator
                facts["typicalFloorSizeText"] = floor
                facts["observations"][0]["literal"] = str(elevator)
                facts["observations"][1]["literal"] = floor
            if mutate:
                mutate(candidate_data)
            raw = json.dumps(candidate_data, sort_keys=True).encode()
            ingest.validate_strict_artifact_freshness(
                candidate_data, require_strict_freshness=True
            )
            qualified = qualified_building_fields(
                candidate_data, hashlib.sha256(raw).hexdigest()
            )
            rows = [
                ingest.to_row(
                    listing,
                    {},
                    candidate_data["runMeta"]["finishedAt"],
                    building_candidates=qualified,
                )
                for listing in candidate_data["listings"]
            ]
            assert len(rows) == 8 and all(rows)
            statement = ingest.build_sql(
                rows,
                [],
                candidate_data["runMeta"]["finishedAt"],
                set(),
                skip_post_commit_summary=True,
            )
            assert "postgresql://" not in statement
            return statement

        def building_values():
            value = sql(
                "SELECT json_agg(json_build_array(external_id,elevator_count,typical_floor_size_text,extra_facts) ORDER BY external_id) FROM credeals.cre_listings WHERE external_id LIKE 'synthetic-building-%';"
            )
            return {item[0]: item[1:] for item in json.loads(value)}

        sql(building_statement(0, "34,567", 0))
        assert all(row[:2] == [0, "34,567"] for row in building_values().values())
        sql(building_statement(3, "34,567 SF", 0))
        assert all(row[:2] == [0, "34,567"] for row in building_values().values())
        sql(building_statement(3, "34,567 SF", 1))
        assert all(row[:2] == [3, "34,567 SF"] for row in building_values().values())
        sql(building_statement(0, "34,567", 2))
        assert all(row[:2] == [0, "34,567"] for row in building_values().values())
        sql(building_statement(3, "34,567 SF", 1))
        assert all(row[:2] == [0, "34,567"] for row in building_values().values())
        sql("""UPDATE credeals.cre_listings SET elevator_count=7, extra_facts=extra_facts #- '{_source_field_provenance,elevator_count}' WHERE external_id='synthetic-building-0';
UPDATE credeals.cre_listings SET extra_facts=jsonb_set(extra_facts,'{_source_field_provenance,elevator_count,source_key}','"other"') WHERE external_id='synthetic-building-1';
UPDATE credeals.cre_listings SET extra_facts=jsonb_set(extra_facts,'{_source_field_provenance,typical_floor_size_text,schema_version}','"unknown"') WHERE external_id='synthetic-building-2';
""")
        previous_fields = building_values()
        protected = sql(
            "SELECT json_agg(json_build_array(l.external_id,l.title,l.status,l.deleted_at,i.url) ORDER BY l.external_id)::text FROM credeals.cre_listings l LEFT JOIN credeals.cre_listing_images i ON i.listing_id=l.id WHERE l.external_id LIKE 'synthetic-building-%';"
        )

        def hostile_building(value):
            for listing in value["listings"]:
                listing["extraFacts"]["_source_field_provenance"] = {
                    "elevator_count": {"source_key": "attacker"}
                }
            facts = value["listings"][3]["transwesternBuildingFacts"]
            facts["observations"].append(copy.deepcopy(facts["observations"][0]))
            facts = value["listings"][4]["transwesternBuildingFacts"]
            facts["observations"][1]["eligibility"] = "ineligible"
            facts = value["listings"][7]["transwesternBuildingFacts"]
            facts["state"] = "malformed_or_concealed_row"
            facts["observations"][0]["eligibility"] = "ineligible"

        sql(building_statement(4, "12,000 SF", 3, hostile_building))
        actual = building_values()
        assert actual["synthetic-building-0"][:2] == [7, "12,000 SF"]
        assert actual["synthetic-building-1"][:2] == [0, "12,000 SF"]
        assert actual["synthetic-building-2"][:2] == [4, "34,567"]
        assert actual["synthetic-building-3"][:2] == [0, "12,000 SF"]
        assert actual["synthetic-building-4"][:2] == [4, "34,567"]
        assert all(
            actual[f"synthetic-building-{i}"][:2] == [4, "12,000 SF"] for i in (5, 6)
        )
        assert actual["synthetic-building-7"][:2] == [0, "12,000 SF"]
        for index, field in (
            (1, "elevator_count"),
            (2, "typical_floor_size_text"),
            (3, "elevator_count"),
            (4, "typical_floor_size_text"),
            (7, "elevator_count"),
        ):
            key = f"synthetic-building-{index}"
            assert (
                actual[key][2]["_source_field_provenance"][field]
                == previous_fields[key][2]["_source_field_provenance"][field]
            )
        assert all(row[2]["fixture"] == "protected" for row in actual.values())
        assert (
            actual["synthetic-building-1"][2]["_source_field_provenance"][
                "elevator_count"
            ]["source_key"]
            == "other"
        )
        assert (
            actual["synthetic-building-2"][2]["_source_field_provenance"][
                "typical_floor_size_text"
            ]["schema_version"]
            == "unknown"
        )
        assert (
            sql(
                "SELECT json_agg(json_build_array(l.external_id,l.title,l.status,l.deleted_at,i.url) ORDER BY l.external_id)::text FROM credeals.cre_listings l LEFT JOIN credeals.cre_listing_images i ON i.listing_id=l.id WHERE l.external_id LIKE 'synthetic-building-%';"
            )
            == protected
        )
        print(
            "PG17 building fields: transitions, literal units, clocks, per-field guards, sibling metadata, injection and child preservation passed"
        )
        for index, reverse in ((5, False), (6, True)):
            pair_data = copy.deepcopy(data)
            good = copy.deepcopy(pair_data["listings"][index])
            good["detailObservedAt"] = (
                (
                    datetime.fromisoformat(first.replace("Z", "+00:00"))
                    + timedelta(seconds=4)
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
            facts = good["transwesternBuildingFacts"]
            facts["observedAt"] = good["detailObservedAt"]
            facts["elevatorCount"] = 9
            facts["typicalFloorSizeText"] = "25,000 SF"
            facts["observations"][0]["literal"] = "9"
            facts["observations"][1]["literal"] = "25,000 SF"
            uncertain = copy.deepcopy(good)
            uncertain["transactionMode"] = "lease"
            uncertain["transactionType"] = "Lease"
            uncertain_facts = uncertain["transwesternBuildingFacts"]
            uncertain_facts["state"] = "ambiguous_fields"
            uncertain_facts["observations"].append(
                {
                    "field": "elevators",
                    "label": "Elevators",
                    "literal": "11",
                    "eligibility": "eligible",
                }
            )
            pair_data["listings"] = [good, uncertain]
            raw = json.dumps(pair_data, sort_keys=True).encode()
            ingest.validate_strict_artifact_freshness(
                pair_data, require_strict_freshness=True
            )
            qualified = qualified_building_fields(
                pair_data, hashlib.sha256(raw).hexdigest()
            )
            a, b = [
                ingest.to_row(
                    item,
                    {},
                    pair_data["runMeta"]["finishedAt"],
                    building_candidates=qualified,
                )
                for item in pair_data["listings"]
            ]
            if reverse:
                a, b = b, a
            ingest.validate_duplicate_identity_before_merge(a, b)
            merged = ingest.merge_rows(a, b)
            assert merged["_source_building_conflicts"] == ["elevator_count"]
            assert (
                merged["source_field_building"]["typical_floor_size_text"]["value"]
                == "25,000 SF"
            )
            sql(
                ingest.build_sql(
                    [merged],
                    [],
                    pair_data["runMeta"]["finishedAt"],
                    set(),
                    skip_post_commit_summary=True,
                )
            )
            row = building_values()[f"synthetic-building-{index}"]
            assert row[:2] == [4, "25,000 SF"]
            result = json.loads(
                sql(
                    "SELECT raw_data::text FROM credeals.cre_listings WHERE external_id="
                    + ingest.sql_lit(f"synthetic-building-{index}")
                    + ";"
                )
            )
            literals = sorted(
                [
                    len(result[key]["transwesternBuildingFacts"]["observations"])
                    for key in ("primary", "secondary_pass")
                ]
            )
            assert literals == [2, 3], literals
        print(
            "PG17 dual-pass ambiguity holds in either order while sibling field and both raw passes survive"
        )

        for path in args.real_artifact:
            raw = path.read_bytes()
            data = json.loads(raw)
            digest = hashlib.sha256(raw).hexdigest()
            ingest.validate_strict_artifact_freshness(
                data, require_strict_freshness=True
            )
            candidates = qualified_counts(data, digest)
            assert candidates, (str(path), digest)
            merged = {}
            brokers = {i: b for i, b in enumerate(data.get("brokers", []))}
            for listing in data["listings"]:
                row = ingest.to_row(
                    listing, brokers, data["runMeta"]["finishedAt"], candidates
                )
                if row is None:
                    continue
                key = (row["slug"], row["external_id"])
                if key in merged:
                    ingest.validate_duplicate_identity_before_merge(merged[key], row)
                    merged[key] = ingest.merge_rows(merged[key], row)
                else:
                    merged[key] = row
            expected = {
                key: row["source_field_count"]
                for key, row in merged.items()
                if row.get("source_field_count") is not None
            }
            assert len(expected) == sum(
                "available_space_count" in decision["values"]
                for decision in candidates.values()
            )
            statement = ingest.build_sql(
                list(merged.values()),
                [],
                data["runMeta"]["finishedAt"],
                set(),
                skip_post_commit_summary=True,
            )
            assert "postgresql://" not in statement
            sql(statement)

            slug = next(iter(expected))[0]

            def real_values(slug=slug):
                value = sql(
                    "SELECT COALESCE(json_agg(json_build_array(b.slug,l.external_id,l.available_space_count,l.extra_facts->'_source_field_provenance'->'available_space_count') ORDER BY b.slug,l.external_id),'[]')::text FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE b.slug="
                    + ingest.sql_lit(slug)
                    + ";"
                )
                return {(a, b): (c, d) for a, b, c, d in json.loads(value)}

            stored = real_values()
            assert all(
                stored[key] == (candidate["value"], candidate)
                for key, candidate in expected.items()
            )
            sql(statement)
            assert real_values() == stored, "exact replay drifted"
            absent_rows = [
                dict(row, source_field_count=None) for row in merged.values()
            ]
            sql(
                ingest.build_sql(
                    absent_rows,
                    [],
                    data["runMeta"]["finishedAt"],
                    set(),
                    skip_post_commit_summary=True,
                )
            )
            assert real_values() == stored, "missing field cleared or changed ownership"
            print(
                f"Real artifact replay: {data['sources'][0]['sourceKey']}, sha256={digest}, promoted={len(expected)}, exact replay and missing preserved"
            )
        for path in args.real_building_artifact:
            original = path.read_bytes()
            source_digest = hashlib.sha256(original).hexdigest()
            data = json.loads(original)
            assert data["runMeta"]["mode"] == "synthetic_offline_replay"
            assert data["runMeta"]["evidenceOnly"] is True
            assert not qualified_building_fields(data, source_digest)
            try:
                ingest.validate_strict_artifact_freshness(
                    data, require_strict_freshness=True
                )
            except ValueError as exc:
                assert "private source observations" in str(exc)
            else:
                raise AssertionError(
                    "original evidence-only wrapper passed canonical guard"
                )
            # Test-only adaptation: preserve the disclosure and capture clocks; the
            # strict validator itself requires a second ephemeral copy with the
            # evidence-only markers removed. Neither copy is an ingest artifact.
            data["runMeta"]["mode"] = "full"
            raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            digest = hashlib.sha256(raw).hexdigest()
            validation_copy = copy.deepcopy(data)
            validation_copy["runMeta"].pop("evidenceOnly")
            validation_copy["runMeta"].pop("evidencePurpose")
            ingest.validate_strict_artifact_freshness(
                validation_copy, require_strict_freshness=True
            )
            candidates = qualified_building_fields(data, digest)
            assert len(data["listings"]) > 50 and candidates
            merged = {}
            brokers = {i: b for i, b in enumerate(data.get("brokers", []))}
            for listing in data["listings"]:
                row = ingest.to_row(
                    listing,
                    brokers,
                    data["runMeta"]["finishedAt"],
                    building_candidates=candidates,
                )
                if row is None:
                    continue
                key = (row["slug"], row["external_id"])
                if key in merged:
                    ingest.validate_duplicate_identity_before_merge(merged[key], row)
                    merged[key] = ingest.merge_rows(merged[key], row)
                else:
                    merged[key] = row
            expected = {
                key: row["source_field_building"]
                for key, row in merged.items()
                if row.get("source_field_building")
            }
            assert expected
            statement = ingest.build_sql(
                list(merged.values()),
                [],
                data["runMeta"]["finishedAt"],
                set(),
                skip_post_commit_summary=True,
            )
            assert "postgresql://" not in statement
            sql(statement)

            def captured_values():
                value = sql(
                    "SELECT COALESCE(json_agg(json_build_array(b.slug,l.external_id,l.elevator_count,l.typical_floor_size_text,l.extra_facts->'_source_field_provenance') ORDER BY b.slug,l.external_id),'[]')::text FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE b.slug='transwestern' AND l.external_id NOT LIKE 'synthetic-building-%';"
                )
                return {(a, b): (c, d, e) for a, b, c, d, e in json.loads(value)}

            stored = captured_values()
            for key, fields in expected.items():
                elevator, floor, provenance = stored[key]
                for name, candidate in fields.items():
                    assert (
                        elevator if name == "elevator_count" else floor
                    ) == candidate["value"]
                    assert provenance[name] == candidate
            sql(statement)
            assert captured_values() == stored, "building exact replay drifted"
            absent = [dict(row, source_field_building=None) for row in merged.values()]
            sql(
                ingest.build_sql(
                    absent,
                    [],
                    data["runMeta"]["finishedAt"],
                    set(),
                    skip_post_commit_summary=True,
                )
            )
            assert captured_values() == stored, (
                "missing building field changed ownership"
            )
            totals = {
                field: sum(field in values for values in expected.values())
                for field in ("elevator_count", "typical_floor_size_text")
            }
            assert totals == {"elevator_count": 16, "typical_floor_size_text": 37}, (
                totals
            )
            assert (
                sum(
                    fields.get("elevator_count", {}).get("value") == 0
                    for fields in expected.values()
                )
                == 12
            )
            print(
                f"Real HTML synthetic-wrapper replay: original_sha256={source_digest}, adapted_sha256={digest}, evidenceOnly=true, records={len(data['listings'])}, fields={totals}, exact replay and missing preserved"
            )


if __name__ == "__main__":
    main()
