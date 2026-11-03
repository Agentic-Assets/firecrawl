"""Full generated ingestion in a disposable, network-none Postgres container."""

import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cre_ingest as ci

name = "coordinate-pair-" + uuid.uuid4().hex


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


created = False
try:
    subprocess.run(
        [
            "docker",
            "create",
            "--pull=never",
            "--name",
            name,
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
            "postgres:17",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    created = True
    subprocess.run(["docker", "start", name], check=True, stdout=subprocess.DEVNULL)
    for _ in range(30):
        if (
            subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"],
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
        flags=re.M,
    )
    schema += "\n" + "\n".join(
        [
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4306",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text(),
        ]
    )
    sql(schema)
    cases = [
        ("full", 40, -80, 41, None, 40, -80),
        ("partial", 40, None, None, -81, 40, None),
        ("empty", None, None, None, -82, None, -82),
        ("replace", 40, None, 41, -81, 41, -81),
        ("keepfull", 40, -80, None, None, 40, -80),
    ]
    ingest([row(key, a, b) for key, a, b, c, d, x, y in cases])
    ingest([row(key, c, d) for key, a, b, c, d, x, y in cases])
    results = json.loads(
        sql(
            "SELECT json_object_agg(external_id, json_build_array(lat,lng,raw_data->\u0027latitude\u0027,raw_data->\u0027longitude\u0027)) FROM credeals.cre_listings;"
        )
    )
    for key, a, b, c, d, x, y in cases:
        assert results[key] == [x, y, c, d], (key, results[key])
    print("full generated ingest PostgreSQL contract passed", len(cases), "cases")
finally:
    if created:
        subprocess.run(
            ["docker", "rm", "-f", name], check=True, stdout=subprocess.DEVNULL
        )
