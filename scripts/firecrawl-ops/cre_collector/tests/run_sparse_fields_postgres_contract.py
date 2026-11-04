"""Full generated ingestion in a disposable, network-none Postgres container."""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cre_ingest as ci

owner = uuid.uuid4().hex
name = "sparse-fields-" + owner
label = "cre-sparse-fixture-owner"


def sql(query):
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            name,
            "psql",
            "-X",
            "-qAt",
            "-U",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        check=False,
        input=query,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


def row(key, lat, lng):
    return ci.to_row(
        {
            "sourceKey": "cbre",
            "id": key,
            "url": "https://example.test/" + key,
            "latitude": lat,
            "longitude": lng,
        },
        {},
        "2026-11-03T00:00:00Z",
    )


def ingest(rows):
    sql(
        ci.build_sql(
            rows,
            [
                {
                    "slug": "cbre",
                    "discovered": len(rows),
                    "saved": len(rows),
                    "errors": 0,
                    "notes": "synthetic",
                }
            ],
            "2026-11-03T00:00:00Z",
            set(),
        )
    )


container_id = None
try:
    if os.geteuid() == 0:
        raise RuntimeError("requires a nonroot isolated checkout")
    image = subprocess.run(["docker", "image", "inspect", "postgres:17", "--format", "{{.Id}}"],
                           check=True, capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise RuntimeError("invalid fixture image ID")
    container_id = subprocess.run(
        [
            "docker",
            "create",
            "--pull=never",
            "--name",
            name,
            "--label", label + "=" + owner,
            "--network",
            "none",
            "--memory",
            "512m",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/var/lib/postgresql/data",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            image,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert re.fullmatch(r"[a-f0-9]{64}", container_id)
    info = json.loads(subprocess.run(["docker", "inspect", name], check=True, capture_output=True, text=True).stdout)[0]
    assert info["Id"] == container_id and info["Image"] == image
    assert info["Config"]["Labels"][label] == owner
    assert info["HostConfig"]["NetworkMode"] == "none" and not info["Mounts"] and not info["NetworkSettings"]["Ports"]
    subprocess.run(["docker", "start", name], check=True, stdout=subprocess.DEVNULL)
    for _ in range(30):
        if (
            subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            break
        time.sleep(1)
    else:
        raise RuntimeError("Postgres not ready")
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
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5261",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text(),
        ]
    )
    sql(schema)

    keys = ["missing", "invalid", "valid", "bounds"]
    ingest([row(key, None, None) for key in keys])
    sql("UPDATE credeals.cre_listings SET size_sf=1000000001, sale_price_per_sf=15000, lease_rate_min=600, lease_rate_max=700;")
    incoming = [row(key, None, None) for key in keys]
    incoming[1].update(size_sf=1000000001, sale_price_per_sf=10001, lease_rate_min=501, lease_rate_max=-1)
    incoming[2].update(size_sf=1000, sale_price_per_sf=200, lease_rate_min=20, lease_rate_max=30)
    incoming[3].update(size_sf=1000000000, sale_price_per_sf=10000, lease_rate_min=500, lease_rate_max=500)
    ingest(incoming)
    result = json.loads(sql("SELECT jsonb_object_agg(external_id, jsonb_build_array(size_sf,sale_price_per_sf,lease_rate_min,lease_rate_max)) FROM credeals.cre_listings;"))
    assert result["missing"] == [1000000001, 15000, 600, 700], result
    assert result["invalid"] == [1000000001, 15000, 600, 700], result
    assert result["valid"] == [1000, 200, 20, 30], result
    assert result["bounds"] == [1000000000, 10000, 500, 500], result
    # A source-proven JLL price-withholding event intentionally clears prices.
    def source_row(source, key, **fields):
        return ci.to_row({"sourceKey": source, "id": key, "url": "https://example.test/" + key,
                          **fields}, {}, "2026-11-04T00:00:00Z")

    def source_ingest(slug, rows, **options):
        sql(ci.build_sql(rows, [{"slug": slug, "discovered": len(rows), "saved": len(rows),
                                "errors": 0, "notes": "synthetic"}],
                         "2026-11-04T00:00:00Z", set(), **options))

    source_ingest("jll", [source_row("jll", "jll-hidden")])
    sql("UPDATE credeals.cre_listings SET sale_price_per_sf=15000, lease_rate_min=600, lease_rate_max=700 WHERE external_id='jll-hidden';")
    source_ingest("jll", [source_row("jll", "jll-hidden", hidePrice=True)])
    jll = json.loads(sql("SELECT jsonb_build_array(sale_price_per_sf,lease_rate_min,lease_rate_max) FROM credeals.cre_listings WHERE external_id='jll-hidden';"))
    assert jll == [None, None, None], jll

    # Colliers Main first-party transition remains explicitly authoritative.
    photo = "https://example.test/property.jpg"
    source_ingest("colliers", [source_row("colliers-main", "colliers-transition", photos=[photo])])
    sql("UPDATE credeals.cre_listings SET size_sf=1000000001, sale_price_per_sf=15000, lease_rate_min=600, lease_rate_max=700 WHERE external_id='main:colliers-transition';")
    replacement = source_row("colliers-main", "colliers-transition", photos=[photo],
                             freshnessProvenance={"detailScope":"first_party_detail_api"})
    source_ingest("colliers", [replacement], colliers_first_party_transition=True)
    changed = json.loads(sql("SELECT jsonb_build_array(size_sf,sale_price_per_sf,lease_rate_min,lease_rate_max) FROM credeals.cre_listings WHERE external_id='main:colliers-transition';"))
    assert changed == [None, None, None, None], changed
    print("generated ingest sparse-field contract passed: missing, invalid, valid, boundary, JLL and Colliers controls")

finally:
    if container_id:
        info = json.loads(subprocess.run(["docker", "inspect", name], check=True, capture_output=True, text=True).stdout)[0]
        assert info["Id"] == container_id and info["Config"]["Labels"][label] == owner
        subprocess.run(
            ["docker", "rm", "-f", container_id], check=True, stdout=subprocess.DEVNULL
        )
