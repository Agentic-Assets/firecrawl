"""The actual NAI mapper's inventory-only result currently fails strict admission.

This regression exercises the producer mapping and coordinator validator,
without provider IO. It documents the boundary; it does not claim that the
current top-level bulk-feed producer invokes the mapper with missing detail.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cre_checkpoint_refresh as refresh
from tests.test_cre_checkpoint_refresh import strict_artifact
from tests.test_nai_post_partial import post


def test_real_source_mapper_inventory_only_is_rejected_before_partial_gate(tmp_path):
    collector = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc)
    generation = "nai-inventory-boundary"
    generation_started = (stamp - timedelta(seconds=30)).isoformat()
    observed = (stamp - timedelta(seconds=10)).isoformat()
    script = """
const [input, generation, observed] = process.argv.slice(1);
process.argv = [process.argv[0], 'nai-fixture'];
const { naiListingFromFeed } = await import('./sources/nai-global.ts');
const row = JSON.parse(input);
const mapped = naiListingFromFeed(row, 'sale', null, 'synthetic unavailable detail', true,
  { generationId: generation, observedAt: observed,
    method: 'infabode_public_posts_graphql', cacheDisposition: 'live' });
console.log(JSON.stringify({ ...mapped, sourceKey: 'nai-global', transactionMode: 'sale' }));
"""
    producer = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", script,
         json.dumps(post(1)["feedRow"]), generation, observed],
        cwd=collector, check=True, capture_output=True, text=True, timeout=30,
    )
    inventory = json.loads(producer.stdout)
    assert inventory["freshnessProvenance"]["detailScope"] == "inventory_only"
    assert inventory.get("detailObservedAt") is None
    data = strict_artifact("nai-global", detail_scope="source_native_public_record",
                           generation=generation, generation_started_at=generation_started)
    data["runMeta"].update(startedAt=(stamp - timedelta(seconds=20)).isoformat(), finishedAt=stamp.isoformat())
    data["listings"][0] = inventory
    data["listings"][1].update(inventoryObservedAt=observed, detailObservedAt=observed)
    data["sources"][0]["freshness"].update(detailErrors=1, detailObserved=0, childPreservationRows=1)
    path = tmp_path / "nai-global.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(refresh.ArtifactValidationError, match="detailErrors must be zero"):
        refresh._validate_source_artifact_local(
            path, "nai-global", (stamp - timedelta(seconds=25)).isoformat(),
            require_strict_freshness=True, expected_generation_id=generation,
            expected_generation_started_at=generation_started,
        )
