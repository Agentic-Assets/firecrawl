import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_franklin_site_projection import artifact

from cre_franklin_site_partial import derive


def mixed():
    data = artifact()
    data["runMeta"].update(transactions=["sale", "lease"], maxItemsPerSource=None,
                           freshness={"generationId": "run-1", "generationStartedAt": "2026-11-04T11:59:00Z",
                                      "requireFreshDetails": True})
    peer = {"sourceKey": "franklin-street", "transactionMode": "lease", "id": "buildout-1",
            "url": "https://buildout.example/1", "name": "private Buildout peer"}
    data["sources"].append({"sourceKey": "franklin-street", "transaction": "lease", "supported": True,
                            "truncated": False, "listingsCollected": 1})
    data["listings"] = [peer]
    data["brokers"] = [{"name": "private broker"}]
    data["totalListings"] = 1
    raw = json.dumps(data).encode()
    binding = {"artifact_sha256": hashlib.sha256(raw).hexdigest(), "generation_id": "run-1",
               "generation_started_at": "2026-11-04T11:59:00Z",
               "started_at": "2026-11-04T12:00:00Z", "finished_at": "2026-11-04T12:00:02Z"}
    return raw, binding


def test_mixed_partial_accounts_pending_peers_without_claiming_they_were_saved(tmp_path):
    raw, binding = mixed()
    result = derive(raw, binding, {"PATH": os.environ["PATH"]})
    meta = result["runMeta"]["franklinSitePartial"]
    assert meta["buildoutPending"] == 1
    assert meta["coverage"] == "partial_unknown"
    assert meta["identityUncertainty"]["siteToBuildout"] == "unresolved"
    assert len(result["listings"]) == 1
    assert result["listings"][0]["id"].startswith("franklin-site:sale:")
    assert result["sources"][1]["error"] == "buildout_peers_pending"
    assert result["brokers"] == []
    assert json.loads(raw)["listings"][0]["name"] == "private Buildout peer"
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(result))
    cli = Path(__file__).resolve().parent.parent / "cre_ingest.py"
    response = subprocess.run([sys.executable, str(cli), "--in", str(path), "--dry-run"],
                              check=False, capture_output=True, text=True)
    assert response.returncode != 0
    assert "refusing Franklin site partial without guarded checkpoint admission" in response.stderr


def test_wrong_artifact_binding_cannot_be_projected():
    raw, binding = mixed()
    binding["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        derive(raw, binding, {"PATH": os.environ["PATH"]})