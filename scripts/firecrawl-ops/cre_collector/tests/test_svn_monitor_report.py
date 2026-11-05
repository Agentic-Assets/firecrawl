"""Monitor reporting reflects the transaction's deferred SVN rows."""
import json
import subprocess
import sys

import pytest

import cre_monitor as monitor
from test_monitor import BID, RUN, _gfin, _idx


@pytest.mark.parametrize("case", ["success", "missing_receipt", "replay", "report_failure"])
def test_apply_summary_excludes_deferred_svn_events(tmp_path, monkeypatch, case):
    old = _gfin("deferred", source_key="svn", status="active", sale_price_usd=100)
    new = _gfin("deferred", source_key="svn", status="active", sale_price_usd=200)
    key = (BID, "deferred")
    listing_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaab"
    prior = _idx(old, observed_status="active")
    prior.update(observation_present=True, presence_generation=0)
    output = (tmp_path / "missing" / "monitor-summary.json" if case == "report_failure"
              else tmp_path / "monitor-summary.json")
    monkeypatch.setattr(monitor, "load_artifact_groups", lambda _: ({"one":new},
                        "2026-11-04T00:00:00Z", {"svn":1}, 0, set()))
    monkeypatch.setattr(monitor, "finalize_group", lambda g: g)
    monkeypatch.setattr(monitor, "artifact_run_identity", lambda _: ("synthetic", RUN))
    monkeypatch.setattr(monitor, "load_db_url", lambda _: ("postgresql://fixture/test", "fixture"))
    monkeypatch.setattr(monitor, "assert_expected_database_target", lambda *_: None)
    monkeypatch.setattr(monitor, "load_prior_state", lambda *_: (
        {"svn":BID}, {key:prior}, {key:{"id":listing_id,"status":"active","deleted":False}}, {}))
    monkeypatch.setattr(monitor, "find_psql", lambda: "psql")
    monkeypatch.setattr(monitor, "psql_connection_args", lambda _: [])
    monkeypatch.setattr(monitor, "psql_connection_env", lambda _: {})
    receipt = ("CRE_MONITOR_SVN_REPLAY_V1" if case == "replay" else
               "" if case == "missing_receipt" else
               "CRE_MONITOR_SVN_SUPPRESSED_V1 "+json.dumps([[BID,"deferred"]]))
    monkeypatch.setattr(monitor.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, stdout=receipt+"\n"))
    monkeypatch.setattr(sys, "argv", ["cre_monitor.py","--in","fixture.json","--apply",
                                    "--expected-db-target-sha256","a"*64,"--out",str(output),"--quiet"])
    if case == "replay":
        assert monitor.main() == 22
        assert not output.exists()
        return
    if case != "success":
        message = ("outcome or report is unproven" if case == "missing_receipt" else
                   "committed but its report could not be saved")
        with pytest.raises(SystemExit, match=message):
            monitor.main()
        assert not output.exists()
        return
    assert monitor.main() == 20
    result = json.loads(output.read_text())
    assert result["status"] == "partial"
    assert result["svn_deferred_count"] == 1
    assert result["events"] == []
    assert result["summary"]["events_by_type"] == {}
    assert result["enqueue"] == {"new":0, "changed":0}
    assert result["enqueue_counts_are_candidates"] is True
