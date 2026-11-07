import json

from cre_listing_history_sql import read_receipt


def write_receipt(path, outcomes, run_key="ingest:v1:test"):
    data = {"schema": "cre-listing-history-outcome/v1", "artifact_run_key": run_key, "outcomes": outcomes}
    path.write_text("ordinary log\nCRE_LISTING_HISTORY " + json.dumps(data) + "\n")


def test_receipt_reports_omissions_without_treating_no_prior_as_failure(tmp_path):
    path = tmp_path / "ingest.log"
    write_receipt(path, {"svn:preimage:no_prior": 1, "svn:observation:captured": 1})
    assert read_receipt(path, "ingest:v1:test")["state"] == "reported_no_omissions"
    write_receipt(path, {"svn:observation:credential_like_content": 1})
    assert read_receipt(path, "ingest:v1:test")["state"] == "reported_omissions"


def test_invalid_missing_or_unbound_receipts_are_unknown(tmp_path):
    path = tmp_path / "ingest.log"
    assert read_receipt(path, "ingest:v1:test")["state"] == "unknown"
    write_receipt(path, {"svn:postimage:captured": 1})
    assert read_receipt(path, "other")["state"] == "unknown"
    write_receipt(path, {})
    assert read_receipt(path, "ingest:v1:test")["state"] == "unknown"
    write_receipt(path, {"svn:postimage:invented": 1})
    assert read_receipt(path, "ingest:v1:test")["state"] == "unknown"
    path.write_text("CRE_LISTING_HISTORY {broken}\n")
    assert read_receipt(path, "ingest:v1:test")["state"] == "unknown"

def test_checkpoint_records_omissions_without_blocking_source(tmp_path, monkeypatch):
    import cre_checkpoint_refresh as refresh
    from cre_ingest import artifact_run_identity_from_digests

    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(run_dir, git_sha="abc", git_dirty=False,
                                    sources=("svn",), page_cap=400, concurrency=3)
    digest = "a" * 64
    manifest["sources"]["svn"]["artifact"] = {"sha256": digest}
    key = artifact_run_identity_from_digests([digest])[0]

    def fake_run(_argv, log, **_kwargs):
        log.parent.mkdir(parents=True, exist_ok=True)
        write_receipt(log, {"svn:observation:store_unavailable": 1}, key)
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    refresh.ingest_source(run_dir, manifest, "svn", run_dir / "source.json", None)
    checkpoint = manifest["sources"]["svn"]
    assert checkpoint["state"] == "ingested"
    assert checkpoint["ingest"]["listing_history"]["state"] == "reported_omissions"
    assert checkpoint["ingest"]["listing_history"]["outcomes"]["svn:observation:store_unavailable"] == 1