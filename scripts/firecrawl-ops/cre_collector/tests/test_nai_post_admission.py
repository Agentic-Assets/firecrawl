import copy
import hashlib
import json

import pytest

from cre_nai_post_admission import candidate
from tests.test_nai_post_partial import post


def fixture(tmp_path):
    root = tmp_path / "gen-1"
    source = root / "sources"
    source.mkdir(parents=True)
    path = source / "nai-global.json"
    data = {"runMeta": {"transactions": ["sale", "lease"], "mode": "full",
                        "startedAt": "2026-11-05T09:59:00Z", "finishedAt": "2026-11-05T10:00:02Z",
                        "freshness": {"generationId": "gen-1"}}, "listings": [post()]}
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = "a" * 40
    manifest = {"run_id": "gen-1", "started_at": "2026-11-05T09:58:00Z", "collector_git_sha": sha,
                "collector_git_dirty": False, "config": {"sources": ["nai-global"], "additive": True,
                "mark_missing": False, "status_activation": False, "transactions": ["sale", "lease"],
                "max_items": 0}, "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "sources": {"nai-global": {"artifact": {"path": "sources/nai-global.json",
                "attempt_started_at": "2026-11-05T09:58:30Z", "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw), "freshness_generation_id": "gen-1"}, "attempts": [{"rc": 0,
                "error": None, "started_at": "2026-11-05T09:58:30Z", "finished_at": "2026-11-05T10:00:03Z"}]}}}
    return path, manifest, sha


def test_bound_artifact_partition(tmp_path):
    path, manifest, sha = fixture(tmp_path)
    binding, partition = candidate(path, manifest, code_sha=sha, dirty=False)
    assert binding["source_count"] == 1 and binding["staged_count"] == 1
    assert partition["staged"][0]["external_id"] == "infabode:1"


def test_clock_unqualified_post_is_held_while_peer_proceeds(tmp_path):
    path, manifest, sha = fixture(tmp_path)
    data = json.loads(path.read_bytes())
    bad = post(2)
    bad["inventoryObservedAt"] = "2026-11-05T09:00:00Z"
    data["listings"].append(bad)
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    manifest["sources"]["nai-global"]["artifact"].update(
        sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    binding, partition = candidate(path, manifest, code_sha=sha, dirty=False)
    assert binding["source_count"] == 2 and binding["staged_count"] == 1
    assert partition["held"] == [{"index": 1, "reason": "unqualified_source_post"}]


@pytest.mark.parametrize("change", [
    lambda m: m["sources"]["nai-global"]["attempts"][-1].update(rc=1),
    lambda m: m["sources"]["nai-global"]["attempts"][-1].update(finished_at=None),
    lambda m: m["sources"]["nai-global"]["artifact"].update(sha256="0" * 64),
    lambda m: m["config"].update(mark_missing=True),
    lambda m: m["preflight"]["database_target"].update(value="bad"),
    lambda m: m.update(run_id="other"),
])
def test_unbound_source_refused(tmp_path, change):
    path, manifest, sha = fixture(tmp_path)
    altered = copy.deepcopy(manifest)
    change(altered)
    with pytest.raises((ValueError, KeyError)):
        candidate(path, altered, code_sha=sha, dirty=False)
