"""Retain collector output without treating it as native HTTP or canonical data."""
import copy
import json

import pytest
from source_evidence import cli, collector, events


def artifact(source, count=60):
    rows = [{"sourceKey": source, "externalId": None, "raw_data": {"unknown": [None, 0, ""]},
             "extraFacts": {"offers": [{"price": 2}, {"price": None}]}, "name": f"card-{i}"}
            for i in range(count)]
    if count > 1:
        rows[1] = copy.deepcopy(rows[0])
    return {"runMeta": {"startedAt": "2026-10-28T09:00:00Z", "finishedAt": "2026-10-28T10:00:00Z",
                        "mode": "full"}, "sources": [{"sourceKey": source, "transaction": "sale",
                        "truncated": True, "error": "detail failed", "totalAvailableOnSource": 90}],
            "listings": rows, "brokers": [{"unknown": None}], "totalListings": count}


@pytest.mark.parametrize("source", ["svn", "fortis"])
def test_partial_records_and_context_roundtrip(source):
    original = artifact(source, 78)
    if source == "fortis":
        for row in original["listings"]:
            row["raw_data"] = {"provider": {"attributes": [{"name": "space", "value": None},
                                                        {"name": "space", "value": "3"}]}}
    wrapped = collector.convert(original, source_key=source)
    value = events.plan(wrapped, source_key=source)
    events.validate_plan(value)
    assert [r["facts"][0]["value"] for r in value["events"][1:]] == original["listings"]
    assert value["events"][0]["facts"][0]["value"]["context"] == {k: v for k, v in original.items() if k != "listings"}
    assert value["events"][0]["provenance"]["observed_at_origin"] == "collector_run_finished_at"
    assert value == events.plan(wrapped, source_key=source)


def test_empty_and_mismatched_sources():
    wrapped = collector.convert(artifact("svn", 0), source_key="svn")
    assert len(events.plan(wrapped, source_key="svn")["events"]) == 1
    for mutation in (lambda d: d["sources"].append({"sourceKey": "other"}),
                     lambda d: d["listings"][0].update(sourceKey="other"),
                     lambda d: d["sources"].clear()):
        bad = artifact("svn")
        mutation(bad)
        with pytest.raises(ValueError):
            collector.convert(bad, source_key="svn")
    with pytest.raises(ValueError):
        events.plan(wrapped, source_key="other")


def test_rehashed_mutation_rejected_and_limits_enforced():
    wrapped = collector.convert(artifact("svn"), source_key="svn")
    value = events.plan(wrapped, source_key="svn")
    value["events"][1]["facts"][0]["value"]["name"] = "changed"
    value["plan_sha256"] = events.sha256(events.encoded({k: v for k, v in value.items() if k != "plan_sha256"}))
    with pytest.raises(ValueError):
        events.validate_plan(value)
    with pytest.raises(ValueError):
        collector.convert(artifact("svn", 10001), source_key="svn")
    big = artifact("svn", 1)
    big["listings"][0]["large"] = "x" * events.MAX_ARTIFACT_BYTES
    with pytest.raises(ValueError):
        collector.convert(big, source_key="svn")


def test_admission_still_refuses_sensitive_sources_and_urls():
    def receipt(value):
        return {"schema_version": "cre-source-evidence-admission/v1", "plan_sha256": value["plan_sha256"],
                "purpose": "private_unverified_evidence", "data_scope": "public_listing_source",
                "content_policy": "reviewed_for_private_retention", "source_keys": [value["source_key"]]}
    for source, url in [("jll", "https://example.org"), ("svn", "https://jll.com/a"),
                        ("svn", "https://example.org/a?token=secret")]:
        original = artifact(source, 1)
        original["listings"][0]["url"] = url
        value = events.plan(collector.convert(original, source_key=source), source_key=source)
        with pytest.raises(ValueError):
            events.validate_admission(value, receipt(value))


def test_offline_cli_converts_and_plans_without_database(tmp_path, monkeypatch):
    source = tmp_path / "input.json"
    wrapped = tmp_path / "retained.json"
    output = tmp_path / "plan.json"
    source.write_text(json.dumps(artifact("svn", 60)))
    monkeypatch.setattr(cli, "run_psql", lambda *args: pytest.fail("database contacted"))
    cli.main(["convert-collector", "--artifact", str(source), "--source-key", "svn", "--out", str(wrapped)])
    cli.main(["plan", "--artifact", str(wrapped), "--source-key", "svn", "--out", str(output)])
    value = json.loads(output.read_text())
    events.validate_plan(value)
    assert len(value["events"]) == 61
    assert wrapped.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        cli.main(["convert-collector", "--artifact", str(source), "--source-key", "svn", "--out", str(wrapped)])


def test_avison_visibility_rejects_unsafe_and_keeps_independent_facts():
    def receipt(value):
        return {"schema_version": "cre-source-evidence-admission/v1", "plan_sha256": value["plan_sha256"],
                "purpose": "private_unverified_evidence", "data_scope": "public_listing_source",
                "content_policy": "reviewed_for_private_retention", "source_keys": ["avison-young"]}

    def check(record):
        data = artifact("avison-young", 0)
        data["listings"] = [record]
        value = events.plan(collector.convert(data, source_key="avison-young"), source_key="avison-young")
        events.validate_admission(value, receipt(value))

    safe = {"sourceKey": "avison-young", "rawSharpLaunch": {"hide_price": 1},
            "leaseRateMin": 18, "capRate": 0.06, "noi": 50000, "salePriceUsd": None}
    check(safe)
    for mutation in ({"salePriceUsd": 2000000}, {"description": "Asking price $2 million"},
                     {"rawSharpLaunch": {"hide_price": "1"}},
                     {"rawSharpLaunch": {"hide_price": 1}, "avisonPriceVisibility":
                         {"schema": "avison-asking-price-v1", "control": "visible"}}):
        with pytest.raises(ValueError):
            check({**safe, **mutation})
    nested = {"sourceKey": "avison-young", "raw_data": {"sourceKey": "avison-young",
              "rawSharpLaunch": {"hide_price": 1}}, "leaseRateMin": 18}
    check(nested)
    with pytest.raises(ValueError):
        check({**nested, "salePriceUsd": 2000000})
    with pytest.raises(ValueError):
        check({**nested, "rawSharpLaunch": {"hide_price": 0}})
    with pytest.raises(ValueError):
        check({**nested, "raw_data": {"sourceKey": "avison-young", "rawSharpLaunch": {"hide_price": None}}})


@pytest.mark.parametrize("count", [0, 1])
def test_avison_context_witness_is_enforced_even_without_withheld_records(count):
    data = artifact("avison-young", count)
    if count:
        data["listings"] = [{"sourceKey": "avison-young", "rawSharpLaunch": {"hide_price": 0}}]
    data["contextExtra"] = {"rawSharpLaunch": {"hide_price": 1}, "sale_price": 2000000}
    def check():
        value = events.plan(collector.convert(data, source_key="avison-young"), source_key="avison-young")
        receipt = {"schema_version": "cre-source-evidence-admission/v1", "plan_sha256": value["plan_sha256"],
                   "purpose": "private_unverified_evidence", "data_scope": "public_listing_source",
                   "content_policy": "reviewed_for_private_retention", "source_keys": ["avison-young"]}
        events.validate_admission(value, receipt)
    with pytest.raises(ValueError):
        check()
    data["contextExtra"]["rawSharpLaunch"]["hide_price"] = 0
    check()
    data["contextExtra"]["rawSharpLaunch"] = {"HidePrice": 1}
    with pytest.raises(ValueError):
        check()
    data["contextExtra"]["rawSharpLaunch"] = {"hide_price": 1}
    data["contextExtra"]["avisonPriceVisibility"] = {"schema": "avison-asking-price-v1", "control": "visible"}
    with pytest.raises(ValueError):
        check()


def test_nested_foreign_source_declaration_in_collector_record_is_refused():
    data = artifact("svn", 1)
    data["listings"][0]["raw_data"] = {"Source_Key": "Avison_Young", "rawSharpLaunch": {"hide_price": 1}}
    value = events.plan(collector.convert(data, source_key="svn"), source_key="svn")
    receipt = {"schema_version": "cre-source-evidence-admission/v1", "plan_sha256": value["plan_sha256"],
               "purpose": "private_unverified_evidence", "data_scope": "public_listing_source",
               "content_policy": "reviewed_for_private_retention", "source_keys": ["svn"]}
    with pytest.raises(ValueError, match="conflicting protected source"):
        events.validate_admission(value, receipt)


@pytest.mark.parametrize("hidden", [False, True])
def test_split_observations_do_not_bypass_avison_context_visibility(hidden):
    data = artifact("avison-young", 0)
    data["sources"][0]["retainedObservations"] = [
        {"rawSharpLaunch": {"hide_price": int(hidden)}, "sale_price": 2000000,
         "leaseRateMin": 18, "unknown": [None, False, ""]} for _ in range(60)]
    wrapped = collector.convert(data, source_key="avison-young")
    assert wrapped["schema"] == collector.SCHEMA_V2
    value = events.plan(wrapped, source_key="avison-young")
    if hidden:
        with pytest.raises(ValueError, match="context requires source-owned price projection"):
            events.validate_content_policy(value)
    else:
        events.validate_content_policy(value)
