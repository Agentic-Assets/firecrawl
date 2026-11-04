"""Offline Newmark source-facts coexistence through production staging and SQL."""

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(HERE.parent), str(HERE.parent / "observation_store")]
from owned_observation_fixture_cluster import OwnedCluster, tool
from test_newmark_nim_public_facts import listing as nim_listing
from test_newmark_public_facts import artifact as rcm_artifact

import cre_ingest as ingest
from source_fields import newmark_nim_public_facts as nim
from source_fields import newmark_public_facts as rcm

STAMP = "2026-10-29T00:00:00Z"
PRIMARY = "cre_public_listing_facts"
SALE = nim.SLOTS["sale"]
LEASE = nim.SLOTS["lease"]


def typed_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            typed_equal(left[k], right[k]) for k in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            typed_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def synthetic_rows():
    artifact = rcm_artifact()
    source = artifact["sources"][0]["retainedObservations"][0]["sourceObservation"]
    source["literal"] = source["literal"][:-1] + ', "precise": 0.10000000000000000001}'
    source["parsed"]["precise"] = 0.1
    mapping = rcm.qualified(json.dumps(artifact).encode())
    assert len(mapping) == 1
    record = rcm.attach(artifact["listings"][0], mapping)
    record["preserveChildCollections"] = True
    detail = ingest.to_row(record, {}, STAMP)
    assert (
        detail
        and detail["extra_facts"][PRIMARY]["codec"]
        == "newmark-rcm-enrichment-withholding/v1"
    )
    precise = next(
        f for f in detail["extra_facts"][PRIMARY]["facts"] if f["label"] == "precise"
    )
    assert precise["value"] is None and precise["numericLiterals"] == {
        "": "0.10000000000000000001"
    }
    rows = {}
    for mode in ("sale", "lease"):
        value = nim_listing(mode)
        url = "https://www.nmrk.com/properties/property-0"
        value.update(
            id="property-0", url=url, canonicalUrl=url, preserveChildCollections=True
        )
        value["rawNewmarkNimRecord"].update(slug="property-0", externalWebsiteUrl=url)
        value["newmarkNimRawRetention"].update(listingId="property-0", listingUrl=url)
        rows[mode] = ingest.to_row(value, {}, STAMP, allow_newmark_nim=True)
        assert (
            rows[mode]
            and rows[mode]["extra_facts"][nim.SLOTS[mode]]["transactionMode"] == mode
        )
    return detail, rows["sale"], rows["lease"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute-disposable-fixture", action="store_true", required=True
    )
    parser.add_argument("--proof-dir", required=True, type=Path)
    parser.add_argument("--inventory-artifact", action="append", default=[], type=Path)
    args = parser.parse_args()
    proof = args.proof_dir
    proof.mkdir(mode=0o700, parents=False, exist_ok=False)
    if proof.stat().st_mode & 0o077:
        raise RuntimeError("proof directory is not owner-private")
    transcript = proof / "failure.log"
    aggregate = {
        "schema": "newmark-public-facts-postgres-proof/v1",
        "synthetic": {},
        "inventory": [],
    }
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    try:
        with OwnedCluster(initdb, pg_ctl, port=55491) as cluster:

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
                    fd = os.open(
                        transcript, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                    )
                    with os.fdopen(fd, "a", encoding="utf8") as out:
                        out.write(result.stderr)
                    raise AssertionError(
                        "disposable SQL failed; see private failure transcript"
                    )
                return result.stdout

            directory = ROOT.parent / "sql"
            schema = re.sub(
                r"^\\i (\S+)\s*$",
                lambda match: (directory / match[1]).read_text(),
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
            sql(
                "ALTER TABLE credeals.cre_listings ADD COLUMN available_space_count integer CHECK (available_space_count >= 0), ADD COLUMN elevator_count integer CHECK (elevator_count >= 0), ADD COLUMN typical_floor_size_text text;"
            )
            detail, sale, lease = synthetic_rows()
            expected = {
                PRIMARY: detail["extra_facts"][PRIMARY],
                SALE: sale["extra_facts"][SALE],
                LEASE: lease["extra_facts"][LEASE],
            }

            def script(rows):
                return ingest.build_sql(
                    rows, [], STAMP, set(), skip_post_commit_summary=True
                )

            def clear():
                sql("DELETE FROM credeals.cre_listings WHERE external_id='property-0';")

            def read():
                value = sql(
                    "SELECT json_build_object('id',l.id,'status',l.status,'deleted_at',l.deleted_at,'extra',l.extra_facts,'images',(SELECT count(*) FROM credeals.cre_listing_images i WHERE i.listing_id=l.id))::text FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE b.slug='newmark' AND l.external_id='property-0';"
                )
                parsed = json.loads(value)
                return parsed

            def protect():
                sql(
                    "INSERT INTO credeals.cre_listing_images(listing_id,url) SELECT l.id,'https://example.test/fixture.jpg' FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE b.slug='newmark' AND l.external_id='property-0';"
                )
                return read()

            def assert_all(before):
                after = read()
                assert (
                    after["id"] == before["id"] and after["status"] == before["status"]
                )
                assert (
                    after["deleted_at"] == before["deleted_at"] is None
                    and after["images"] == before["images"] == 1
                )
                assert all(
                    after["extra"].get(key) == value for key, value in expected.items()
                )
                return after

            for name, order in (
                ("sale_rcm_lease", (sale, detail, lease)),
                ("lease_rcm_sale", (lease, detail, sale)),
            ):
                clear()
                sql(script([order[0]]))
                before = protect()
                for row in order[1:]:
                    sql(script([row]))
                observed = assert_all(before)
                sql(script([order[-1]]))
                assert read()["extra"] == observed["extra"]
                aggregate["synthetic"][name] = "passed"

            clear()
            merged = ingest.merge_rows(copy.deepcopy(sale), copy.deepcopy(lease))
            merged = ingest.merge_rows(merged, copy.deepcopy(detail))
            sql(script([merged]))
            before = protect()
            assert_all(before)
            aggregate["synthetic"]["same_batch"] = "passed"

            # An unmarked enrich input cannot inject stale NIM slots or erase already stored ones.
            stale = copy.deepcopy(rcm_artifact()["listings"][0])
            stale["preserveChildCollections"] = True
            stale["extraFacts"] = {
                SALE: {"stale": True},
                LEASE: {"stale": True},
                "fixture_sibling": "safe",
            }
            clean = ingest.to_row(stale, {}, STAMP)
            assert (
                clean
                and SALE not in (clean["extra_facts"] or {})
                and LEASE not in (clean["extra_facts"] or {})
            )
            before = read()
            sql(script([clean]))
            after = assert_all(before)
            assert after["extra"]["fixture_sibling"] == "safe"
            # An explicitly partial new sale slot must not affect the independent lease/RCM slots.
            partial = copy.deepcopy(nim_listing("sale"))
            partial.update(
                id="property-0",
                url="https://www.nmrk.com/properties/property-0",
                preserveChildCollections=True,
            )
            partial["rawNewmarkNimRecord"].update(
                slug="property-0", externalWebsiteUrl=partial["url"], big="x" * 17000
            )
            partial["newmarkNimRawRetention"].update(
                listingId="property-0", listingUrl=partial["url"]
            )
            partial_row = ingest.to_row(partial, {}, STAMP, allow_newmark_nim=True)
            assert partial_row["extra_facts"][SALE]["captureCompleteness"] == "partial"
            sql(script([partial_row]))
            after = read()
            assert after["extra"][SALE]["captureCompleteness"] == "partial"
            assert (
                after["extra"][LEASE] == expected[LEASE]
                and after["extra"][PRIMARY] == expected[PRIMARY]
            )
            assert (
                after["id"] == before["id"]
                and after["status"] == before["status"]
                and after["deleted_at"] is None
                and after["images"] == 1
            )
            aggregate["synthetic"]["stale_and_partial"] = "passed"

            # Concurrent updates target independent slots on the same existing row.
            clear()
            sql(script([detail]))
            before = protect()
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(sql, (script([sale]), script([lease]))))
            assert_all(before)
            aggregate["synthetic"]["concurrent_variants"] = "passed"

            for path in args.inventory_artifact:
                with path.open("rb") as source:
                    payload = source.read(16 * 1024 * 1024 + 1)
                if len(payload) > 16 * 1024 * 1024:
                    raise ValueError("inventory artifact exceeds 16 MiB")
                digest = hashlib.sha256(payload).hexdigest()
                artifact = json.loads(payload)
                if (
                    not isinstance(artifact, dict)
                    or not isinstance(artifact.get("runMeta"), dict)
                    or artifact["runMeta"].get("mode") != "full"
                ):
                    raise ValueError("inventory artifact must be full mode")
                rows = []
                for listing in artifact.get("listings", []):
                    if listing.get("sourceKey") != "newmark":
                        continue
                    row = ingest.to_row(
                        listing,
                        {},
                        artifact.get("runMeta", {}).get("finishedAt"),
                        allow_newmark_nim=True,
                    )
                    if row and any(
                        key in (row.get("extra_facts") or {})
                        for key in nim.SLOTS.values()
                    ):
                        rows.append(row)
                merged = {}
                for row in rows:
                    key = (row["slug"], row["external_id"])
                    if key in merged:
                        ingest.validate_duplicate_identity_before_merge(
                            merged[key], row
                        )
                        merged[key] = ingest.merge_rows(merged[key], row)
                    else:
                        merged[key] = row
                if len(merged) < 51:
                    raise ValueError(
                        "inventory artifact must contain at least 51 eligible unique rows"
                    )
                sql(script(list(merged.values())))
                # Compare every projected slot using JSONB equality inside the disposable fixture.
                for row in merged.values():
                    expected_slots = {
                        key: row["extra_facts"][key]
                        for key in nim.SLOTS.values()
                        if key in row["extra_facts"]
                    }
                    identity = row["external_id"].replace("'", "''")
                    result = sql(
                        f"SELECT l.extra_facts::text FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE b.slug='newmark' AND l.external_id='{identity}';"
                    )
                    stored = json.loads(result)
                    if not all(
                        typed_equal(stored.get(key), value)
                        for key, value in expected_slots.items()
                    ):
                        raise AssertionError("inventory slot comparison failed")
                with path.open("rb") as source:
                    after = source.read(16 * 1024 * 1024 + 1)
                if after != payload or hashlib.sha256(after).hexdigest() != digest:
                    raise AssertionError("inventory artifact changed during replay")
                aggregate["inventory"].append(
                    {
                        "sha256": digest,
                        "input_rows": len(rows),
                        "unique_rows": len(merged),
                        "slots_verified": sum(
                            sum(
                                k in (row.get("extra_facts") or {})
                                for k in nim.SLOTS.values()
                            )
                            for row in merged.values()
                        ),
                    }
                )
        aggregate["cluster_cleanup"] = "confirmed"
        fd = os.open(
            proof / "aggregate.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(fd, "w", encoding="utf8") as out:
            json.dump(aggregate, out, sort_keys=True)
        print(json.dumps(aggregate, sort_keys=True))
    except BaseException:  # noqa: BLE001 - retain a private transcript even on interruption
        if not transcript.exists():
            fd = os.open(transcript, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf8") as out:
                out.write(traceback.format_exc())
        print(
            "fixture failed; details retained in private proof directory",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
