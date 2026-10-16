"""Actual TypeScript detail producers through strict artifact and Python staging."""

import json
import os
import subprocess
from pathlib import Path

import cre_ingest as ingest


def test_legacy_producer_keys_survive_artifact_and_python_staging():
    script = r"""
import {observationNow} from './lib/observation-clock.ts';
import {legacyIdentityListings} from './tests/fixtures/legacy-path-identity.ts';
const startedAt=observationNow();
process.env.CRE_REQUIRE_FRESH_DETAILS='1';
process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS='1';
process.env.CRE_REFRESH_GENERATION='legacy-path-offline';
process.env.CRE_REFRESH_STARTED_AT=startedAt;
const listings=legacyIdentityListings();
console.log(JSON.stringify({runMeta:{mode:'full',startedAt,finishedAt:observationNow(),transactions:['sale','lease'],freshness:{generationId:'legacy-path-offline',generationStartedAt:startedAt,requireFreshDetails:true,requireFreshPropertyDetails:true}},listings,totalListings:listings.length}));
"""
    child = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        env={
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG")
            if key in os.environ
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    artifact = json.loads(child.stdout)
    ingest.validate_strict_artifact_freshness(artifact)
    rows = [
        ingest.to_row(row, {}, artifact["runMeta"]["finishedAt"])
        for row in artifact["listings"]
    ]
    assert [row["external_id"] for row in rows] == [
        "property/MiXeD%2fCase",
        "property/MiXeD%2fCase",
        "MiXeD%2fCase",
    ]
    assert [row["slug"] for row in rows] == [
        "daum-commercial",
        "foundry-commercial",
        "lyon-stahl",
    ]
    assert [
        row["raw_data"][field]
        for row, field in zip(
            rows, ("daumPostId", "foundryPostId", "lyonStahlPostId"), strict=True
        )
    ] == ["101", "101", "101"]
