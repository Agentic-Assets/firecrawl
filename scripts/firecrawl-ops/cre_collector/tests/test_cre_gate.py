"""
test_cre_gate.py

Validator for the coverage-and-anomaly gate (cre_gate.py, design sections 8/9).
Covers the safety-critical behaviors:

  - verdict precedence: no-baseline -> first_seen wins over everything (so a
    dry run with an empty baseline is all first_seen, even for an errored
    source); then error / floor / drop -> hold; else ok.
  - mark_missing_safe is true ONLY for 'ok'.
  - rolling median resists a single downward spike and still tracks a real rise.
  - baseline update only seeds first_seen sources that are clean and non-empty,
    updates ok sources, and never writes hold sources.
  - per-source counts come from to_row acceptance on FLAT listings (dual-mode
    sale+lease rows count twice, matching cre_ingest per_source_counts), and a
    source pass carrying an 'error' marks the source as failed.
  - sub-sources are gated as their own source_key and folded into the parent
    only for the brokerage rollup, which is safe only if every member is ok.
"""

import json
import os
import tempfile

import pytest

import cre_gate as g
from cre_ingest import SOURCE_TO_BROKERAGE

FLOOR = 100
DROP = 0.30


def test_read_baseline_rejects_target_drift_before_psql(monkeypatch):
    monkeypatch.setattr(
        g,
        "load_db_url",
        lambda _env_file: (
            "postgresql://user:secret@db.example.test/cre",
            "/fake/.env",
        ),
    )
    monkeypatch.setattr(
        g,
        "find_psql",
        lambda: pytest.fail("target drift must fail before psql discovery"),
    )

    with pytest.raises(SystemExit, match="does not match"):
        g.read_baseline(None, True, "0" * 64)


# ---------------------------------------------------------------------------
# verdict_for
# ---------------------------------------------------------------------------


class TestVerdict:
    def test_no_baseline_is_first_seen_even_with_error(self):
        # No baseline row -> first_seen wins, so dry-run is uniformly first_seen.
        verdict, reason, safe, median = g.verdict_for(
            5000, True, "boom", None, FLOOR, DROP
        )
        assert verdict == "first_seen"
        assert safe is False
        assert median is None
        assert "errored" in reason

    def test_error_with_baseline_holds(self):
        verdict, _, safe, _ = g.verdict_for(
            5000, True, "boom", {"median": 5000, "last": 5000}, FLOOR, DROP
        )
        assert verdict == "hold"
        assert safe is False

    def test_below_floor_holds(self):
        verdict, reason, safe, _ = g.verdict_for(
            40, False, None, {"median": 5000, "last": 5000}, FLOOR, DROP
        )
        assert verdict == "hold"
        assert "floor" in reason
        assert safe is False

    def test_drop_below_band_holds(self):
        # 5000 * (1 - 0.30) = 3500; 3400 < 3500 -> hold.
        verdict, reason, safe, _ = g.verdict_for(
            3400, False, None, {"median": 5000, "last": 5000}, FLOOR, DROP
        )
        assert verdict == "hold"
        assert "median" in reason
        assert safe is False

    def test_just_above_band_is_ok(self):
        verdict, _, safe, _ = g.verdict_for(
            3600, False, None, {"median": 5000, "last": 5000}, FLOOR, DROP
        )
        assert verdict == "ok"
        assert safe is True

    def test_healthy_is_ok(self):
        verdict, _, safe, _ = g.verdict_for(
            5200, False, None, {"median": 5000, "last": 5000}, FLOOR, DROP
        )
        assert verdict == "ok"
        assert safe is True

    def test_baseline_row_with_null_median_uses_floor_only(self):
        # A seeded-but-medianless row can still gate on the floor.
        verdict, _, safe, median = g.verdict_for(
            5000, False, None, {"median": None, "last": None}, FLOOR, DROP
        )
        assert verdict == "ok"
        assert safe is True
        assert median is None


def _savills_full_scope_artifact(*, entries=None, listings=None, run_meta=None):
    """Small but complete Savills fixture for the source-floor exception."""
    return {
        "runMeta": run_meta
        or {
            "transactions": ["sale", "lease"],
            "mode": "full",
            "maxItemsPerSource": None,
        },
        "sources": entries
        if entries is not None
        else [
            {
                "sourceKey": "savills",
                "transaction": "sale",
                "supported": True,
                "truncated": False,
                "listingsCollected": 1,
                "totalAvailableOnSource": 1,
                "sourceUrl": "https://search.savills.com/sale",
                "method": "direct",
            },
            {
                "sourceKey": "savills",
                "transaction": "lease",
                "supported": True,
                "truncated": False,
                "listingsCollected": 1,
                "totalAvailableOnSource": 1,
                "sourceUrl": "https://search.savills.com/lease",
                "method": "direct",
            },
        ],
        "listings": listings
        if listings is not None
        else [
            {
                "sourceKey": "savills",
                "transactionMode": "sale",
                "id": "sale-1",
                "url": "https://search.savills.com/com/en/property-detail/sale-1",
            },
            {
                "sourceKey": "savills",
                "transactionMode": "lease",
                "id": "lease-1",
                "url": "https://search.savills.com/com/en/property-detail/lease-1",
            },
        ],
        "brokers": [],
    }


class TestSavillsFullScopeCoverageFloor:
    def test_complete_small_sale_and_lease_artifact_receives_its_declared_floor(self):
        policy = g.load_gate_coverage_policy()
        floor, policy_reason, failure = g.effective_floor_for_source(
            "savills", g.DEFAULT_FLOOR, [_savills_full_scope_artifact()], policy
        )

        assert floor == 1
        assert policy_reason == "savills full sale+lease coverage policy"
        assert failure is None
        verdict, reason, safe, _ = g.verdict_for(
            2, False, None, {"median": 2, "last": 2}, floor, DROP
        )
        assert (verdict, reason, safe) == ("ok", None, True)

    @pytest.mark.parametrize(
        ("artifact", "expected_reason"),
        [
            (
                _savills_full_scope_artifact(
                    entries=[
                        {
                            "sourceKey": "savills",
                            "transaction": "sale",
                            "supported": True,
                            "truncated": False,
                            "listingsCollected": 2,
                            "totalAvailableOnSource": 2,
                            "sourceUrl": "https://search.savills.com/sale",
                            "method": "direct",
                        }
                    ]
                ),
                "one Savills entry for sale and one for lease",
            ),
            (
                _savills_full_scope_artifact(
                    entries=[
                        {
                            **entry,
                            "truncated": True,
                        }
                        if entry["transaction"] == "lease"
                        else entry
                        for entry in _savills_full_scope_artifact()["sources"]
                    ]
                ),
                "lease is not explicitly untruncated",
            ),
            (
                _savills_full_scope_artifact(
                    entries=[
                        {
                            **entry,
                            "error": "timeout",
                        }
                        if entry["transaction"] == "sale"
                        else entry
                        for entry in _savills_full_scope_artifact()["sources"]
                    ]
                ),
                "sale carries a source error",
            ),
            (
                _savills_full_scope_artifact(
                    run_meta={
                        "transactions": ["lease", "sale"],
                        "mode": "full",
                        "maxItemsPerSource": None,
                    }
                ),
                "exactly sale+lease transactions",
            ),
            (
                _savills_full_scope_artifact(
                    entries=[
                        {
                            **entry,
                            "totalAvailableOnSource": 2,
                        }
                        if entry["transaction"] == "sale"
                        else entry
                        for entry in _savills_full_scope_artifact()["sources"]
                    ]
                ),
                "sale collected count does not reconcile to provider total",
            ),
            (
                _savills_full_scope_artifact(
                    listings=[
                        _savills_full_scope_artifact()["listings"][0],
                        {
                            **_savills_full_scope_artifact()["listings"][0],
                            "id": "sale-2",
                        },
                        _savills_full_scope_artifact()["listings"][1],
                    ]
                ),
                "sale collected count does not reconcile to artifact listings",
            ),
            (
                _savills_full_scope_artifact(
                    listings=[],
                    entries=[
                        {
                            **entry,
                            "listingsCollected": 0,
                            "totalAvailableOnSource": 0,
                        }
                        for entry in _savills_full_scope_artifact()["sources"]
                    ],
                ),
                None,
            ),
        ],
    )
    def test_partial_or_unsafe_artifacts_cannot_use_the_small_source_exception(
        self, artifact, expected_reason
    ):
        policy = g.load_gate_coverage_policy()
        floor, policy_reason, failure = g.effective_floor_for_source(
            "savills", g.DEFAULT_FLOOR, [artifact], policy
        )

        if expected_reason is None:
            # A zero complete source still uses the reviewed floor, but 0 is
            # below it and therefore never a raw-gate success.
            assert (floor, policy_reason, failure) == (
                1,
                "savills full sale+lease coverage policy",
                None,
            )
            verdict, _, safe, _ = g.verdict_for(
                0, False, None, {"median": 0, "last": 0}, floor, DROP
            )
            assert verdict == "hold" and safe is False
            return

        assert floor == g.DEFAULT_FLOOR
        assert policy_reason is None
        assert expected_reason in failure
        verdict, reason, safe, _ = g.verdict_for(
            200, True, failure, {"median": 200, "last": 200}, floor, DROP
        )
        assert verdict == "hold" and safe is False
        assert expected_reason in reason

    def test_multiple_artifacts_cannot_claim_the_single_artifact_exception(self):
        policy = g.load_gate_coverage_policy()
        floor, policy_reason, failure = g.effective_floor_for_source(
            "savills",
            g.DEFAULT_FLOOR,
            [_savills_full_scope_artifact(), _savills_full_scope_artifact()],
            policy,
        )

        assert (floor, policy_reason) == (g.DEFAULT_FLOOR, None)
        assert "exactly one full Savills artifact" in failure

    def test_policy_loader_rejects_lowered_global_floor(self, tmp_path):
        policy = g.load_gate_coverage_policy()
        policy["default_floor"] = 1
        path = tmp_path / "unsafe-coverage-policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")

        with pytest.raises(g.GateCoveragePolicyError, match="default_floor"):
            g.load_gate_coverage_policy(path)

    def test_policy_loader_rejects_non_list_transaction_scope(self, tmp_path):
        policy = g.load_gate_coverage_policy()
        policy["source_overrides"]["savills"]["required_transactions"] = "sale,lease"
        path = tmp_path / "unsafe-coverage-policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")

        with pytest.raises(g.GateCoveragePolicyError, match="sale\\+lease"):
            g.load_gate_coverage_policy(path)

    def test_failed_coverage_is_retained_for_first_seen_baseline_selection(self):
        coverage_error = "Savills full-coverage policy failed: mode must be full"
        errors = {
            "savills": g.effective_source_error(None, coverage_error),
        }
        updates = g.select_baseline_updates(
            {"savills": {"verdict": "first_seen", "current_active": 2}},
            errors,
            {},
        )

        assert errors["savills"] == coverage_error
        assert updates == []


# ---------------------------------------------------------------------------
# rolling_median
# ---------------------------------------------------------------------------


class TestRollingMedian:
    def test_seed_from_nothing_is_current(self):
        assert g.rolling_median(None, None, 5000) == 5000

    def test_single_spike_is_resisted(self):
        # A one-run collapse to 50 must not move a 5000 baseline.
        assert g.rolling_median(5000, 5000, 50) == 5000

    def test_genuine_rise_tracks(self):
        assert g.rolling_median(5000, 5200, 5400) == 5200


# ---------------------------------------------------------------------------
# select_baseline_updates
# ---------------------------------------------------------------------------


class TestBaselineUpdateSelection:
    def test_only_ok_and_clean_first_seen_are_written(self):
        per_source = {
            "svn": {"verdict": "ok", "current_active": 5200},
            "nai-global": {"verdict": "first_seen", "current_active": 30},
            "lee-associates": {"verdict": "first_seen", "current_active": 0},
            "colliers": {"verdict": "first_seen", "current_active": 0},
            "cbre": {"verdict": "hold", "current_active": 10},
        }
        source_error = {
            "svn": None,
            "nai-global": None,
            "lee-associates": "boom",
            "colliers": None,
            "cbre": None,
        }
        baseline = {"svn": {"median": 5000, "last": 5100}}
        ups = {u["source_key"]: u for u in g.select_baseline_updates(per_source, source_error, baseline)}
        assert set(ups) == {"svn", "nai-global"}
        # svn: rolling median of (5000, 5100, 5200) -> 5100
        assert ups["svn"]["new_median"] == 5100
        # nai-global: first-seen seed -> current
        assert ups["nai-global"]["new_median"] == 30
        # hold and errored/empty first_seen are never written.
        assert "cbre" not in ups
        assert "lee-associates" not in ups
        assert "colliers" not in ups


# ---------------------------------------------------------------------------
# count_artifacts (flat-count, error flagging, dual-mode double-count)
# ---------------------------------------------------------------------------


def _write_artifact(payload):
    fd, path = tempfile.mkstemp(prefix="cre_gate_test_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    return path


class TestCountArtifacts:
    def test_flat_count_dual_mode_and_error(self):
        payload = {
            "runMeta": {"finishedAt": "2026-06-13T00:00:00.000Z"},
            "brokers": [],
            "sources": [
                {"sourceKey": "svn", "transaction": "sale", "error": None},
                {"sourceKey": "svn", "transaction": "lease", "error": None},
                {"sourceKey": "lee-associates", "transaction": "sale",
                 "error": "Error: Lee & Associates: aborting this source"},
            ],
            "listings": [
                # svn dual-mode: same property in both passes -> counts twice.
                {"sourceKey": "svn", "transactionMode": "sale",
                 "url": "https://www.svn.com/listings/?propertyId=42-sale"},
                {"sourceKey": "svn", "transactionMode": "lease",
                 "url": "https://www.svn.com/listings/?propertyId=42-lease"},
                # A second distinct svn property.
                {"sourceKey": "svn", "transactionMode": "sale",
                 "url": "https://www.svn.com/listings/?propertyId=43-sale"},
                # A listing with no url is rejected by to_row (source_url NOT NULL).
                {"sourceKey": "svn", "transactionMode": "sale"},
            ],
        }
        path = _write_artifact(payload)
        try:
            current, errors, observed, scraped_at, torow_errors = g.count_artifacts([path], quiet=True)
        finally:
            os.unlink(path)

        # 3 of 4 svn listings have a url -> accepted; the urlless one is rejected.
        assert current["svn"] == 3
        # lee-associates appears only in sources (errored, 0 listings).
        assert errors["lee-associates"]
        assert current.get("lee-associates", 0) == 0
        assert observed == {"svn", "lee-associates"}
        assert torow_errors == 0
        assert scraped_at == "2026-06-13T00:00:00.000Z"


# ---------------------------------------------------------------------------
# End-to-end dry-run invariant (the contract the runbook self-check exercises)
# ---------------------------------------------------------------------------


class TestDryRunInvariant:
    def test_empty_baseline_makes_every_source_first_seen(self):
        # Mirrors --dry-run: empty baseline -> every observed source first_seen,
        # mark_missing_safe false, brokerage rollup unsafe.
        observed = ["cbre", "cbre-dealflow", "svn", "lee-associates"]
        current = {"cbre": 20000, "cbre-dealflow": 1800, "svn": 5000, "lee-associates": 0}
        errs = {"lee-associates": "boom"}
        baseline = {}  # dry run

        per_source = {}
        for sk in observed:
            verdict, reason, safe, median = g.verdict_for(
                current.get(sk, 0), bool(errs.get(sk)), errs.get(sk),
                baseline.get(sk), FLOOR, DROP,
            )
            per_source[sk] = {"verdict": verdict, "mark_missing_safe": safe}

        assert all(v["verdict"] == "first_seen" for v in per_source.values())
        assert all(v["mark_missing_safe"] is False for v in per_source.values())

    def test_subsource_keyed_independently_and_folds_into_parent_rollup(self):
        # cbre-dealflow is its own gate key but folds into the cbre brokerage.
        assert SOURCE_TO_BROKERAGE["cbre-dealflow"][0] == "cbre"
        assert SOURCE_TO_BROKERAGE["cbre"][0] == "cbre"
        assert g._slug_for("cbre-dealflow") == "cbre"

        # Brokerage rollup: safe only if EVERY member source_key is ok.
        per_source = {
            "cbre": {"mark_missing_safe": True},
            "cbre-dealflow": {"mark_missing_safe": False},
        }
        rollup = {}
        for sk in per_source:
            slug = g._slug_for(sk)
            pb = rollup.setdefault(slug, {"mark_missing_safe": True, "source_keys": []})
            pb["source_keys"].append(sk)
            if not per_source[sk]["mark_missing_safe"]:
                pb["mark_missing_safe"] = False
        assert rollup["cbre"]["mark_missing_safe"] is False
        assert sorted(rollup["cbre"]["source_keys"]) == ["cbre", "cbre-dealflow"]
