"""SVN uses the existing private attempt custody without write authority."""

from cre_checkpoint_refresh import fresh_source_env
from cre_partial_page_custody import prepare, reconcile


def test_svn_attempt_capture_is_scoped_and_does_not_leak_to_peers(tmp_path):
    state = prepare(tmp_path, 1, "svn")
    assert state == {"path": "svn-partial/attempt-1.jsonl", "state": "pending"}
    env, _ = fresh_source_env("svn", tmp_path, base={}, attempt_number=1)
    assert env["CRE_SVN_PARTIAL_PATH"] == str(tmp_path / state["path"])
    assert env["CRE_SVN_PARTIAL_ATTEMPT"] == "1"
    peer, _ = fresh_source_env("newmark", tmp_path, base=env, attempt_number=1)
    assert "CRE_SVN_PARTIAL_PATH" not in peer
    assert "CRE_SVN_PARTIAL_ATTEMPT" not in peer


def test_interrupted_svn_attempt_keeps_bytes_without_claiming_custody(tmp_path):
    state = prepare(tmp_path, 1, "svn")
    path = tmp_path / state["path"]
    path.write_bytes(b'{"incomplete":')
    attempt = {"number": 1, "finished_at": None, "rc": None, "svn_partial": state}
    reconcile(tmp_path, {"sources": {"svn": {"attempts": [attempt]}}}, "svn")
    assert path.read_bytes() == b'{"incomplete":'
    assert state["state"] == "unverified"
