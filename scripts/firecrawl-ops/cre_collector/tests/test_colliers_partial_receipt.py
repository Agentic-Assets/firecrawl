import copy
import hashlib
import json

import pytest

from cre_colliers_partial import revalidate_source_receipt
from cre_partial_page_custody import settle


def fixture(tmp_path):
    run = tmp_path / "generation-1"
    (run / "colliers-partial").mkdir(parents=True, mode=0o700)
    (run / "rejected").mkdir()
    page_path = run / "colliers-partial" / "attempt-1.jsonl"
    page = [
        {"schema": "colliers-partial-pages/v1", "sourceKey": "colliers", "generation": run.name, "attempt": 1},
        {"kind": "fixture"},
    ]
    encoded = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in page).encode()
    page_path.write_bytes(encoded)
    page_path.chmod(0o600)
    receipt = {"state": "sealed", "coverage": "unverified", "outcome": "source_error", "sourceKey": "colliers",
               "generationId": run.name, "attempt": 1, "path": "colliers-partial/attempt-1.jsonl",
               "bytes": len(encoded), "observationRecords": 1, "sha256": hashlib.sha256(encoded).hexdigest()}
    main = run / "rejected" / "colliers-attempt-1.json"
    main.write_text(json.dumps({"sources": [{"sourceKey": "colliers", "error": "partial", "partialCapture": receipt}]}))
    main.chmod(0o600)
    attempt = {"number": 1, "started_at": "2026-11-04T15:00:00Z", "finished_at": "2026-11-04T16:00:00Z", "rc": 0,
               "rejected_artifact": "rejected/colliers-attempt-1.json", "rejected_artifact_sha256": hashlib.sha256(main.read_bytes()).hexdigest(),
               "colliers_partial": {"path": receipt["path"], "state": "pending"}}
    settle(run, attempt, main, "colliers")
    assert attempt["colliers_partial"]["state"] == "sealed_local"
    manifest = {"run_id": run.name, "started_at": "2026-11-04T14:00:00Z",
                "collector_git_sha": "a" * 40, "collector_git_dirty": False,
                "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "sources": {"colliers": {"attempts": [attempt]}}}
    return run, manifest


def test_revalidates_only_the_bound_settled_source_attempt(tmp_path):
    run, manifest = fixture(tmp_path)
    binding = revalidate_source_receipt(run, manifest, 1, "a" * 40, False)
    assert binding["attempt"] == 1 and binding["database_target_sha256"] == "b" * 64


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(collector_git_dirty=True),
    lambda m: m.update(started_at="2026-11-04T15:30:00Z"),
    lambda m: m["sources"]["colliers"]["attempts"][0].update(finished_at=None),
    lambda m: m["sources"]["colliers"]["attempts"].append({"number": 2}),
    lambda m: m["sources"]["colliers"]["attempts"][0].update(rejected_artifact="../elsewhere"),
    lambda m: m["sources"]["colliers"]["attempts"][0]["colliers_partial"].update(sha256="0" * 64),
])
def test_rejects_unsettled_or_changed_manifest_binding(tmp_path, mutate):
    run, manifest = fixture(tmp_path)
    changed = copy.deepcopy(manifest)
    mutate(changed)
    with pytest.raises(ValueError):
        revalidate_source_receipt(run, changed, 1, "a" * 40, False)


def test_rejects_changed_source_spool(tmp_path):
    run, manifest = fixture(tmp_path)
    (run / "colliers-partial" / "attempt-1.jsonl").write_text("changed\n")
    with pytest.raises(ValueError, match="receipt changed"):
        revalidate_source_receipt(run, manifest, 1, "a" * 40, False)


def test_rejects_symlinked_receipt_directory(tmp_path):
    run, manifest = fixture(tmp_path)
    source_dir = run / "colliers-partial"
    source_dir.rename(run / "actual-pages")
    source_dir.symlink_to(run / "actual-pages", target_is_directory=True)
    with pytest.raises(ValueError, match="directory invalid"):
        revalidate_source_receipt(run, manifest, 1, "a" * 40, False)
