"""
test_cre_enrich.py

Pure-transform / no-DB contracts for the Tier-B enrichment worker
(cre_enrich.py). Every assertion is on a builder string, the
select_done_and_retry partition, or an argv list; run() is exercised with
monkeypatched subprocess.run / load_db_url / psql so nothing connects to a
database, per tests/CLAUDE.md.

Covers the 15-case list in ENRICHMENT_WORKER_DESIGN_2026-06-15.md Section 8 and
out/enrich/IMPL_SPEC.md Section 4, plus the env-discovery delegation contract.
"""

import json
import os
import re

import pytest

import cre_enrich
from cre_enrich import (
    build_claim_sql,
    build_collect_argv,
    build_complete_sql,
    build_ingest_argv,
    build_release_sql,
    build_retry_increment_sql,
    select_done_and_retry,
    validate_enriched_artifact,
)

DB_URL_SENTINEL = "postgres://user:secret@db.example.com:5432/postgres"


def _noncomment_lines(sql):
    """SQL lines excluding psql meta (\\set) and blank lines, for safety greps."""
    out = []
    for line in sql.splitlines():
        s = line.strip()
        if not s or s.startswith("\\"):
            continue
        out.append(line)
    return out


# --- (1) CLAIM SQL shape ---------------------------------------------------


def test_claim_sql_core_shape_no_attempts_increment_at_claim():
    sql = build_claim_sql(200)
    assert (
        f"SELECT pg_advisory_xact_lock("
        f"{cre_enrich.QUEUE_MUTATION_ADVISORY_LOCK});"
    ) in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "attempts < 5" in sql
    assert "done_at IS NULL" in sql
    assert "ORDER BY priority, enqueued_at" in sql
    assert "LIMIT 200" in sql
    assert "RETURNING" in sql
    assert "q.url" in sql  # URL match decides completion; DELETE is id-keyed.
    # NO attempts increment at claim time (a stack-down run must not dead-letter
    # the whole batch).
    assert "attempts = attempts + 1" not in sql
    assert "attempts + 1" not in sql


# --- (2) CLAIM SQL reclaims stale claims -----------------------------------


def test_claim_sql_reclaims_stale_claims():
    sql = build_claim_sql(50)
    assert "claimed_at IS NULL OR claimed_at < now() - interval '1 hour'" in sql


# --- (3) CLAIM SQL pins + idempotent + no DB url ---------------------------


def test_claim_sql_pins_idempotent_and_never_contains_db_url():
    a = build_claim_sql(200)
    b = build_claim_sql(200)
    assert a == b  # idempotent across calls
    assert "SET LOCAL standard_conforming_strings = on" in a
    assert "\\set ON_ERROR_STOP on" in a
    assert DB_URL_SENTINEL not in a
    # batch is validated to int and inlined as a bare integer (no injection).
    assert build_claim_sql("75") == build_claim_sql(75)


def test_claim_sql_source_filter_is_exact_and_does_not_widen_default_drain():
    default_sql = build_claim_sql(200)
    targeted_sql = build_claim_sql(200, source="marcus-millichap")
    assert "AND source_key = 'marcus-millichap'" in targeted_sql
    assert "AND source_key = 'marcus-millichap'" not in default_sql
    # The filter belongs in the locked claim CTE, before any row can be marked
    # claimed or later have attempts incremented.
    assert targeted_sql.index("AND source_key = 'marcus-millichap'") < \
        targeted_sql.index("FOR UPDATE SKIP LOCKED")


def test_claim_sql_source_filter_quotes_and_rejects_blank_values():
    sql = build_claim_sql(1, source="broker'key")
    assert "AND source_key = 'broker''key'" in sql
    with pytest.raises(ValueError, match="non-empty queue source_key"):
        build_claim_sql(1, source="  ")


# --- (4) select_done_and_retry partitions by URL ---------------------------


def test_select_done_and_retry_marks_done_by_url_retries_rest():
    claimed = [
        {"id": "id-1", "url": "https://x/a", "attempts": 0},
        {"id": "id-2", "url": "https://x/b", "attempts": 0},
        {"id": "id-3", "url": "https://x/c", "attempts": 0},
    ]
    enriched = [{"url": "https://x/a"}, {"url": "https://x/c"}]
    done, retry, dead = select_done_and_retry(claimed, enriched)
    assert done == ["id-1", "id-3"]
    assert retry == ["id-2"]
    assert dead == []


# --- (5) URL match works across folded vs native external_id ---------------


def test_url_match_works_when_external_id_folded_but_artifact_native():
    # Queue row carries the folded id (main:usa1); the artifact carries the
    # native id (usa1). The url is identical in both, so URL-keying matches.
    claimed = [{"id": "id-1", "external_id": "main:usa1",
                "url": "https://colliers/usa1", "attempts": 0}]
    enriched = [{"id": "usa1", "url": "https://colliers/usa1"}]
    done, retry, dead = select_done_and_retry(claimed, enriched)
    assert done == ["id-1"]
    assert retry == [] and dead == []


# --- (6) build_complete_sql DELETEs done rows, sql_lit-quoted --------------


def test_build_complete_sql_deletes_done_rows_id_keyed_quoted():
    sql = build_complete_sql([
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ])
    assert "DELETE FROM credeals.cre_enrichment_queue" in sql
    assert "WHERE id IN (" in sql
    assert "'11111111-1111-1111-1111-111111111111'::uuid" in sql
    assert "WHERE url IN" not in sql
    # never a done_at = now() update; deletion is what lets re-enqueue happen.
    assert "done_at = now()" not in sql
    assert "SET LOCAL standard_conforming_strings = on" in sql


def test_build_complete_sql_empty_is_noop_no_malformed_in():
    sql = build_complete_sql(set())
    assert "IN ()" not in sql
    assert "DELETE FROM" not in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql


# --- (7) attempts==4 claimed-but-absent partitions into dead ---------------


def test_attempts_four_absent_row_partitions_into_dead_set():
    claimed = [{"id": "id-dead", "url": "https://x/gone", "attempts": 4}]
    enriched = []  # absent from the artifact
    done, retry, dead = select_done_and_retry(claimed, enriched)
    assert done == []
    assert retry == []
    assert dead == ["id-dead"]  # 4 + 1 == 5 -> leaves the attempts < 5 drain set


def test_attempts_three_absent_row_still_retries():
    claimed = [{"id": "id-retry", "url": "https://x/gone", "attempts": 3}]
    done, retry, dead = select_done_and_retry(claimed, [])
    assert retry == ["id-retry"] and dead == []


# --- (8) whole-run collect failure releases claims, no increment, no ingest -


def test_collect_failure_releases_claims_without_incrementing(monkeypatch, tmp_path):
    calls = _wire_run(monkeypatch, tmp_path,
                      claimed=[("id-1", "colliers-main", "main:usa1",
                                "https://x/a", "new", "0")],
                      collect_rc=1)
    rc = cre_enrich.run(_Args(env_file=None))
    assert rc == 1
    # release SQL ran; no ingest, no complete, no retry-increment.
    assert any("claimed_at = NULL" in s for s in calls["exec_sql"])
    assert all("attempts = attempts + 1" not in s for s in calls["exec_sql"])
    assert calls["ingest_called"] is False


def test_build_release_sql_does_not_increment_attempts():
    sql = build_release_sql(["11111111-1111-1111-1111-111111111111"], last_error="boom")
    assert "claimed_at = NULL" in sql
    assert "attempts" not in sql  # attempts is untouched on a whole-run failure
    assert "'11111111-1111-1111-1111-111111111111'::uuid" in sql
    assert "'boom'" in sql


# --- (9) empty / missing / invalid enriched.json skips ingest --------------


def test_missing_enriched_artifact_releases_and_skips_ingest(monkeypatch, tmp_path):
    calls = _wire_run(monkeypatch, tmp_path,
                      claimed=[("id-1", "colliers-main", "main:usa1",
                                "https://x/a", "new", "0")],
                      collect_rc=0, write_enriched=None)  # collect "succeeds" but writes nothing
    rc = cre_enrich.run(_Args(env_file=None))
    assert rc == 1
    assert calls["ingest_called"] is False
    assert any("claimed_at = NULL" in s for s in calls["exec_sql"])


def test_empty_listings_enriched_artifact_advances_retry_without_ingest(
    monkeypatch, tmp_path
):
    calls = _wire_run(monkeypatch, tmp_path,
                      claimed=[("id-1", "colliers-main", "main:usa1",
                                "https://x/a", "new", "0")],
                      collect_rc=0, write_enriched={"listings": []})
    rc = cre_enrich.run(_Args(env_file=None))
    assert rc == 0
    assert calls["ingest_called"] is False
    assert any(
        "attempts = attempts + 1" in sql for sql in calls["exec_sql"]
    )


def test_validate_enriched_artifact_rejects_wrong_mode_url_source_and_duplicate():
    claimed = [{"id": "q1", "url": "https://example.test/a", "source_key": "svn"}]
    invalid = [
        {"runMeta": {"mode": "full"}, "listings": [{"url": "https://example.test/a", "sourceKey": "svn"}]},
        {"runMeta": {"mode": "enrich"}, "listings": [{"url": "https://example.test/b", "sourceKey": "svn"}]},
        {"runMeta": {"mode": "enrich"}, "listings": [{"url": "https://example.test/a", "sourceKey": "lee-associates"}]},
        {
            "runMeta": {"mode": "enrich"},
            "listings": [
                {"url": "https://example.test/a", "sourceKey": "svn"},
                {"url": "https://example.test/a", "sourceKey": "svn"},
            ],
        },
    ]
    for artifact in invalid:
        with pytest.raises(ValueError):
            validate_enriched_artifact(claimed, artifact)


def test_provenance_failure_releases_claims_and_skips_ingest(monkeypatch, tmp_path):
    calls = _wire_run(
        monkeypatch,
        tmp_path,
        claimed=[("id-1", "svn", "a", "https://x/a", "new", "0")],
        write_enriched={
            "runMeta": {"mode": "enrich"},
            "listings": [{"url": "https://x/not-claimed", "sourceKey": "svn"}],
        },
    )
    assert cre_enrich.run(_Args(env_file=None)) == 1
    assert calls["ingest_called"] is False
    assert any("claimed_at = NULL" in sql for sql in calls["exec_sql"])


# --- (10) ingest argv is the safety guard ----------------------------------


def test_ingest_argv_is_exactly_in_path():
    argv = build_ingest_argv("/tmp/enriched.json")
    assert argv == ["--in", "/tmp/enriched.json"]
    for banned in ("--mark-missing", "--no-mark-missing", "--activate-status"):
        assert banned not in argv


def test_ingest_subprocess_never_carries_destructive_flags(monkeypatch, tmp_path):
    calls = _wire_run(monkeypatch, tmp_path,
                      claimed=[("id-1", "colliers-main", "main:usa1",
                                "https://x/a", "new", "0")],
                      collect_rc=0,
                      write_enriched={"listings": [{"url": "https://x/a"}]})
    rc = cre_enrich.run(_Args(env_file=None))
    assert rc == 0
    ingest_argv = calls["ingest_argv"]
    assert ingest_argv is not None
    joined = " ".join(ingest_argv)
    for banned in ("--mark-missing", "--no-mark-missing", "--activate-status"):
        assert banned not in joined
    assert "--in" in ingest_argv


# --- (11) collect argv targets --enrich-input + --out only -----------------


def test_collect_argv_targets_enrich_input_no_full_or_monitor_flags():
    argv = build_collect_argv("/tmp/claim.json", "/tmp/enriched.json")
    assert argv == ["npx", "tsx", "collect.ts",
                    "--enrich-input", "/tmp/claim.json",
                    "--out", "/tmp/enriched.json"]
    joined = " ".join(argv)
    for banned in ("--monitor", "--mark-missing", "--source", "--transaction"):
        assert banned not in joined


# --- (12) empty claim exits 0, runs no subprocess --------------------------


def test_empty_claim_exits_zero_no_subprocess(monkeypatch, tmp_path):
    calls = _wire_run(monkeypatch, tmp_path, claimed=[])
    rc = cre_enrich.run(_Args(env_file=None))
    assert rc == 0
    assert calls["collect_called"] is False
    assert calls["ingest_called"] is False
    assert calls["exec_sql"] == []  # no complete/release/increment


# --- (13) the DB url is never printed --------------------------------------


def test_db_url_never_printed(monkeypatch, tmp_path, capsys):
    _wire_run(monkeypatch, tmp_path,
              claimed=[("id-1", "colliers-main", "main:usa1",
                        "https://x/a", "new", "0")],
              collect_rc=0,
              write_enriched={"listings": [{"url": "https://x/a"}]})
    cre_enrich.run(_Args(env_file=None))
    captured = capsys.readouterr()
    assert DB_URL_SENTINEL not in captured.out
    assert DB_URL_SENTINEL not in captured.err
    # the env-file path IS allowed to appear (mirrors cre_monitor).
    assert "credentials:" in captured.err


# --- (14) env discovery reuses cre_ingest.load_db_url ----------------------


def test_env_discovery_delegates_to_cre_ingest_load_db_url():
    # cre_enrich imports load_db_url from cre_ingest (same object), so the
    # env-file precedence contract in test_env_discovery.py covers this worker.
    import cre_ingest
    assert cre_enrich.load_db_url is cre_ingest.load_db_url


# --- (15) dry-run builds claim SQL, never connects -------------------------


def test_dry_run_builds_claim_sql_and_does_not_connect(monkeypatch, capsys):
    def _boom(*a, **k):
        raise AssertionError("dry-run must not call load_db_url")

    monkeypatch.setattr(cre_enrich, "load_db_url", _boom)
    monkeypatch.setattr(cre_enrich, "_psql_query", _boom)
    rc = cre_enrich.run(_Args(env_file=None, dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "FOR UPDATE SKIP LOCKED" in out
    assert "RETURNING" in out


def test_dry_run_source_filter_builds_only_the_targeted_claim(monkeypatch, capsys):
    def _boom(*a, **k):
        raise AssertionError("dry-run must not connect")

    monkeypatch.setattr(cre_enrich, "load_db_url", _boom)
    rc = cre_enrich.run(_Args(dry_run=True, source="srs"))
    assert rc == 0
    assert "AND source_key = 'srs'" in capsys.readouterr().out


# --- retry-increment SQL shape (covers the +1 path) ------------------------


def test_build_retry_increment_sql_increments_and_releases():
    sql = build_retry_increment_sql(
        ["22222222-2222-2222-2222-222222222222"], last_error="absent")
    assert "attempts = attempts + 1" in sql
    assert "claimed_at = NULL" in sql
    assert "'22222222-2222-2222-2222-222222222222'::uuid" in sql
    assert "SET LOCAL standard_conforming_strings = on" in sql


def test_build_retry_increment_sql_empty_is_noop():
    sql = build_retry_increment_sql([])
    assert "attempts = attempts + 1" not in sql
    assert "IN ()" not in sql


# --- happy path drives complete (delete) + retry-increment -----------------


def test_happy_path_deletes_done_and_increments_absent(monkeypatch, tmp_path):
    calls = _wire_run(
        monkeypatch, tmp_path,
        claimed=[
            ("id-done", "colliers-main", "main:usa1", "https://x/a", "new", "0"),
            ("id-miss", "colliers-main", "main:usa2", "https://x/b", "new", "0"),
        ],
        collect_rc=0,
        write_enriched={"listings": [{"id": "usa1", "url": "https://x/a"}]},
    )
    rc = cre_enrich.run(_Args(env_file=None))
    assert rc == 0
    assert calls["ingest_called"] is True
    exec_sql = "\n".join(calls["exec_sql"])
    assert "DELETE FROM credeals.cre_enrichment_queue" in exec_sql
    assert "'id-done'::uuid" in exec_sql          # done row deleted by claimed id
    assert "WHERE url IN" not in exec_sql
    assert "attempts = attempts + 1" in exec_sql  # absent row incremented


# --- (C3) Bespoke enricher registry ----------------------------------------


def test_enricher_set_includes_all_current_detail_source_paths():
    """The bespoke enricher registry lives in lib/enrich.ts (TS). Assert its
    ENRICHERS map keys cover the current detail-source paths and that cbre is
    excluded (cbre is enumeration-only: the
    listings-api JSON already returns fully mapped rows, so there is no
    per-listing detail endpoint to enrich). The capture-everything build added
    the Buildout Tier-B enricher for svn + lee-associates (their detail iframe
    carries the media / tours / full gallery / OM docs the inventory bulk path
    cannot see). Marcus & Millichap, Avison Young, SRS, and Kidder Mathews use
    direct source paths rather than the generic JSON-LD fallback, which must
    never complete a claim without a source-backed payload.

    Source-text assertion only (no DB, no Node): mirrors the TS unit test
    tests/ts/lib/enrich.test.ts so a regression in either layer is caught here.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    enrich_ts = os.path.join(here, "lib", "enrich.ts")
    src = open(enrich_ts).read()
    # Grab the body of `export const ENRICHERS ... = { ... };`.
    m = re.search(
        r"export const ENRICHERS:[^=]*=\s*\{(.*?)\};", src, re.DOTALL
    )
    assert m, "ENRICHERS registry literal not found in lib/enrich.ts"
    body = m.group(1)
    # Keys are object-literal property names on the left of a colon: either
    # quoted string literals ("colliers-main") or bare identifiers (svn). Strip
    # // line comments first so commentary colons never read as keys.
    body_no_comments = re.sub(r"//[^\n]*", "", body)
    keys = set(re.findall(r'(?:"([a-z0-9-]+)"|\b([a-z][a-z0-9_-]*))\s*:', body_no_comments))
    keys = {q or b for (q, b) in keys}
    assert keys == {
        "colliers-main",
        "jll-investor",
        "svn",
        "lee-associates",
        "marcus-millichap",
        "avison-young",
        "srs",
        "kidder-mathews",
    }
    assert "cbre" not in keys
    # cbre's exclusion is intentional and documented in the same file.
    assert "cbre is intentionally ABSENT" in src
    assert "return row?.genericEnrich?.hadJsonLd ? row : null;" in src


# ---------------------------------------------------------------------------
# Test harness: monkeypatch the worker's DB + subprocess boundary.
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, env_file=None, batch=200, dry_run=False, source=None):
        self.env_file = env_file
        self.batch = batch
        self.dry_run = dry_run
        self.source = source


def _wire_run(monkeypatch, tmp_path, *, claimed, collect_rc=0,
              write_enriched="__unset__"):
    """Wire cre_enrich.run() against fakes so it never touches a real DB or shell.

    `claimed` is a list of RETURNING tuples (id, source_key, external_id, url,
    reason, attempts). `write_enriched`: dict written as enriched.json by the
    fake collect; None to write nothing; "__unset__" to write a default 1-listing
    artifact echoing each claimed url.
    """
    calls = {
        "exec_sql": [],
        "collect_called": False,
        "ingest_called": False,
        "ingest_argv": None,
        "collect_out_path": None,
    }

    monkeypatch.setattr(cre_enrich, "OUT_ENRICH_DIR", str(tmp_path))
    monkeypatch.setattr(cre_enrich, "load_db_url",
                        lambda env_file: (DB_URL_SENTINEL, "/fake/.env.local"))

    def _fake_query(db_url, sql):
        assert db_url == DB_URL_SENTINEL
        return list(claimed)

    def _fake_exec(db_url, sql):
        assert db_url == DB_URL_SENTINEL
        calls["exec_sql"].append(sql)

    monkeypatch.setattr(cre_enrich, "_psql_query", _fake_query)
    monkeypatch.setattr(cre_enrich, "_psql_exec", _fake_exec)

    claimed_urls = [t[3] for t in claimed]

    class _Proc:
        def __init__(self, rc):
            self.returncode = rc

    def _fake_subprocess_run(argv, **kwargs):
        if "collect.ts" in argv:
            calls["collect_called"] = True
            out_path = argv[argv.index("--out") + 1]
            calls["collect_out_path"] = out_path
            if write_enriched == "__unset__":
                payload = {
                    "runMeta": {"mode": "enrich"},
                    "listings": [
                        {"url": t[3], "sourceKey": t[1]}
                        for t in claimed
                    ],
                }
            else:
                payload = write_enriched
                if isinstance(payload, dict):
                    payload.setdefault("runMeta", {"mode": "enrich"})
                    source_by_url = {t[3]: t[1] for t in claimed}
                    for listing in payload.get("listings") or []:
                        if isinstance(listing, dict) and "sourceKey" not in listing:
                            listing["sourceKey"] = source_by_url.get(listing.get("url"))
            if payload is not None:
                with open(out_path, "w") as f:
                    json.dump(payload, f)
            return _Proc(collect_rc)
        # ingest invocation: python executable + cre_ingest.py
        if any(str(a).endswith("cre_ingest.py") for a in argv):
            calls["ingest_called"] = True
            calls["ingest_argv"] = list(argv)
            return _Proc(0)
        return _Proc(0)

    monkeypatch.setattr(cre_enrich.subprocess, "run", _fake_subprocess_run)
    return calls
