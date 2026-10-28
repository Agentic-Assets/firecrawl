"""Synthetic offline plan only; never connects to any database."""
import json

import pytest

import cre_buildout_observation_prototype as prototype


def fixture(count=65):
    observation = {
        "schema": "buildout-inventory-attributes-v1",
        "attributes": [["literal", False], ["empty", None], ["duplicate", "first"], ["duplicate", "second"], ["notes", "a\nb"]],
        "omittedCount": 0, "omissions": {"malformed": 0, "sensitive": 0, "limit": 0},
        "provenance": {"observedAt": "valid", "generationId": "valid"},
        "observedAt": "2026-10-28T12:00:00Z", "generationId": "run-1",
    }
    return {"syntheticFixture": True, "runMeta": {"freshness": {"generationId": "run-1"}},
            "listings": [{"sourceKey": "faris-lee", "transactionMode": "sale", "id": str(i), "url": f"https://example.test/listing?propertyId={i}-sale",
                          "buildoutInventoryObservation": observation} for i in range(count)]}


def raw(data):
    return json.dumps(data).encode()


def test_sixty_five_independent_events_preserve_literals_and_do_not_guess_identity():
    records, summary = prototype.build_plan(raw(fixture()))
    assert len(records) == 65 and summary["observations"] == 65
    assert len({record[5] for record in records}) == 65
    assert json.loads(records[0][-1])["payload"]["attributes"] == fixture()["listings"][0]["buildoutInventoryObservation"]["attributes"]
    assert "'unresolved'" in prototype.sql_plan(records)
    assert "credeals." not in prototype.sql_plan(records)


def test_partial_and_distinct_generation_conflicting_values():
    data = fixture(2)
    data["listings"][0] = {"sourceKey": "faris-lee"}
    data["listings"][1]["buildoutInventoryObservation"]["omittedCount"] = 1
    data["listings"][1]["buildoutInventoryObservation"]["omissions"]["limit"] = 1
    records, summary = prototype.build_plan(raw(data))
    assert len(records) == 1 and summary["withoutObservation"] == 1
    assert summary["flaggedObservations"] == 1 and summary["status"] == "partial"


@pytest.mark.parametrize("mutation", ["nonsynthetic", "wrong_source", "secret", "bad_generation", "invalid_count"])
def test_fixture_boundary_rejects_unadmitted_payload(mutation):
    data = fixture(1)
    item = data["listings"][0]
    if mutation == "nonsynthetic": data["syntheticFixture"] = False
    elif mutation == "wrong_source": item["sourceKey"] = "jll"
    elif mutation == "secret": item["buildoutInventoryObservation"]["attributes"] = [["Authorization", "Bearer ABC"]]
    elif mutation == "bad_generation": item["buildoutInventoryObservation"]["generationId"] = "other"
    else: item["buildoutInventoryObservation"]["omittedCount"] = 1
    with pytest.raises(ValueError):
        prototype.build_plan(raw(data))


def test_unknown_native_identity_stays_unresolved():
    data = fixture(1)
    data["listings"][0]["id"] = {"unexpected": True}
    record = prototype.build_plan(raw(data))[0][0]
    assert record[6] is None
    assert "'unresolved'" in prototype.sql_plan([record])


def test_unsupported_url_and_missing_identity_are_explicit_partial():
    data = fixture(1)
    data["listings"][0]["url"] = "https://example.com/private?token=secret"
    data["listings"][0].pop("id")
    records, summary = prototype.build_plan(raw(data))
    assert summary["status"] == "partial"
    assert summary["sourceCoverage"] == "not_evaluated"
    assert records[0][6] is None and records[0][7] is None
    assert json.loads(records[0][-1])["sourceUrlState"] == "unsupported"


def test_forged_valid_time_rejected():
    data = fixture(1)
    data["listings"][0]["buildoutInventoryObservation"]["observedAt"] = "2026-W01-1T00:00:00Z"
    with pytest.raises(ValueError, match="invalid observation time"):
        prototype.build_plan(raw(data))


def test_empty_input_is_not_evidence_of_complete_source():
    data = fixture(0)
    _, summary = prototype.build_plan(raw(data))
    assert summary["status"] == "partial"
    assert summary["sourceAvailability"] == "not_evaluated"


def test_guard_delimiter_never_uses_an_adversarial_literal():
    data = fixture(1)
    data["listings"][0]["buildoutInventoryObservation"]["attributes"] = [["literal", "$$ $cre_observation_0$ O'Reilly \\ path"]]
    plan = prototype.sql_plan(prototype.build_plan(raw(data))[0])
    assert "SET LOCAL standard_conforming_strings = on;" in plan
    assert "DO $cre_observation_1$" in plan
    assert "O''Reilly" in plan


def test_plan_file_is_private_and_never_overwrites(tmp_path):
    target = tmp_path / "evidence.sql"
    prototype.write_plan(target, "synthetic")
    assert target.read_text() == "synthetic"
    assert target.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        prototype.write_plan(target, "replacement")
    assert target.read_text() == "synthetic"
    link = tmp_path / "link.sql"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        prototype.write_plan(link, "replacement")
    assert target.read_text() == "synthetic"


def test_input_read_is_bounded_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(prototype, "MAX_ARTIFACT_BYTES", 16)
    artifact = tmp_path / "oversized.json"
    artifact.write_bytes(b"x" * 100)
    bounded = prototype.read_fixture(artifact)
    assert len(bounded) == 17
    with pytest.raises(ValueError, match="exceeds byte limit"):
        prototype.build_plan(bounded)


def test_duplicate_object_key_rejected():
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        prototype.build_plan(b'{"syntheticFixture":true,"syntheticFixture":false}')
