"""Pyramid unit splitting preserves the existing additive gate boundaries."""

import json

import cre_checkpoint_refresh as refresh
import cre_gate as gate
from cre_ingest import SOURCE_KEYS_BY_SLUG
from test_cre_gate_compact_evidence import full_savills


def test_source_gate_inputs_decompose_around_singleton_pyramid(tmp_path):
    payloads = [full_savills()]
    for source in ("cbre", "pyramid-brokerage", "cbre-dealflow"):
        payloads.append({"sources": [{"sourceKey": source, "truncated": False}],
                         "listings": [{"sourceKey": source, "transactionMode": "sale",
                                       "url": f"https://example.invalid/{source}/1"}], "brokers": []})
    paths = []
    for index, payload in enumerate(payloads):
        path = tmp_path / f"{index}.json"
        path.write_text(json.dumps(payload))
        paths.append(path)
    combined_evidence = []
    counts, errors, observed, _, conversion_errors = gate.count_artifacts(paths, quiet=True, coverage_evidence=combined_evidence)
    policy = gate.load_gate_coverage_policy()
    pieces = []
    for path in paths:
        evidence = []
        per_counts, per_errors, per_observed, _, per_conversion_errors = gate.count_artifacts([path], quiet=True, coverage_evidence=evidence)
        pieces.append((per_counts, per_errors, per_observed, per_conversion_errors, evidence))
    assert observed == set().union(*(piece[2] for piece in pieces))
    assert counts == {key: value for piece in pieces for key, value in piece[0].items()}
    assert errors == {key: value for piece in pieces for key, value in piece[1].items()}
    assert conversion_errors == sum(piece[3] for piece in pieces)
    for source in observed:
        piece = next(piece for piece in pieces if source in piece[2])
        assert gate.effective_floor_for_source(source, gate.DEFAULT_FLOOR, combined_evidence, policy) == gate.effective_floor_for_source(source, gate.DEFAULT_FLOOR, piece[4], policy)
        count, error = counts.get(source, 0), errors.get(source)
        assert gate.verdict_for(count, bool(error), error, {"median": 1}, 0, 0.3) == gate.verdict_for(piece[0].get(source, 0), bool(piece[1].get(source)), piece[1].get(source), {"median": 1}, 0, 0.3)
    assert SOURCE_KEYS_BY_SLUG["pyramid-brokerage"] == {"pyramid-brokerage"}
    both = {source: {"mark_missing_safe": True} for source in ("cbre", "cbre-dealflow")}
    assert gate.rollup_brokerages(both)["cbre"]["mark_missing_safe"] is True
    assert all(not gate.rollup_brokerages({source: both[source]})["cbre"]["mark_missing_safe"] for source in both)
    argv = refresh.build_ingest_argv(tmp_path / "artifact.json", None)
    assert "--mark-missing" not in argv and "--activate-status" not in argv and "--update-baseline" not in argv
