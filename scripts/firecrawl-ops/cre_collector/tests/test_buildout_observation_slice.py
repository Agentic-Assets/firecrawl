"""Offline source-scoped non-observation classification; no DB authority."""

import json

import pytest

from source_fields.buildout import classify_prior_inventory_presence, validate_inventory
from test_buildout_current_inventory import SOURCE, buildout, setup


def prior(external_id, *native_ids, source=SOURCE, present=True):
    return {
        "external_id": external_id,
        "native_ids": list(native_ids),
        "source_key": source,
        "observation_present": present,
    }


def test_classify_validated_full_feed_including_closed_and_other_tenure(tmp_path):
    _, _, artifact, manifest, _ = setup(tmp_path)
    validated = validate_inventory(
        artifact, SOURCE, buildout.definitions()[SOURCE], manifest["run_id"],
        manifest["started_at"],
    )
    unseen = prior("gone", "99")
    closed = prior("closed", "40")
    lease = prior("lease", "30")
    dual = prior("dual", "10", "20")
    result = classify_prior_inventory_presence(validated, [unseen, closed, lease, dual], SOURCE)
    assert result == {"not_observed": [unseen], "still_observed": [closed, lease, dual], "held": []}


def test_ambiguous_missing_or_wrong_source_prior_bindings_are_held(tmp_path):
    _, _, artifact, manifest, _ = setup(tmp_path)
    validated = validate_inventory(
        artifact, SOURCE, buildout.definitions()[SOURCE], manifest["run_id"],
        manifest["started_at"],
    )
    rows = [prior("a", "99"), prior("b", "99"), prior("c"),
            prior("d", "98", source="other"), prior("e", "97", present=False),
            prior("same", "95"), prior("same", "96"), prior("unsafe", "9007199254740992")]
    result = classify_prior_inventory_presence(validated, rows, SOURCE)
    assert result == {"not_observed": [], "still_observed": [], "held": rows}


def test_invalid_feed_identity_is_not_interpreted_as_absence():
    with pytest.raises(ValueError, match="inventory identity"):
        classify_prior_inventory_presence({"pages": [{"rows": [{"id": None}]}]}, [], SOURCE)


def test_incomplete_page_proof_is_refused_before_classification(tmp_path):
    _, _, artifact, manifest, _ = setup(tmp_path)
    for entry in artifact["sources"]:
        entry["currentInventoryProof"]["pages"].pop()
    with pytest.raises(ValueError, match="evidence is incomplete or drifted"):
        validate_inventory(
            artifact, SOURCE, buildout.definitions()[SOURCE], manifest["run_id"],
            manifest["started_at"],
        )


def test_scope_binds_proof_target_code_generation_and_all_native_ids(tmp_path):
    from source_fields.buildout import prepare_inventory_observation_scope
    from test_buildout_current_inventory import CODE, TARGET

    _, path, _, manifest, _ = setup(tmp_path)
    raw = path.read_bytes()
    scope = prepare_inventory_observation_scope(
        raw, manifest, SOURCE, current_code_sha=CODE, current_git_dirty=False, current_target_sha256=TARGET
    )
    assert scope["native_ids"] == ["10", "20", "30", "40"]
    assert scope["generation_id"] == manifest["run_id"]
    assert scope["observed_at"] == json.loads(raw)["runMeta"]["finishedAt"]
    assert scope["database_target_sha256"] == TARGET
    assert scope["artifact_sha256"] == manifest["sources"][SOURCE]["artifact"]["sha256"]
    with pytest.raises(ValueError):
        prepare_inventory_observation_scope(
            raw + b" ", manifest, SOURCE, current_code_sha=CODE, current_git_dirty=False, current_target_sha256=TARGET
        )
    with pytest.raises(ValueError):
        prepare_inventory_observation_scope(
            raw, manifest, SOURCE, current_code_sha=CODE, current_git_dirty=True, current_target_sha256=TARGET
        )
    with pytest.raises(ValueError):
        prepare_inventory_observation_scope(
            raw, manifest, SOURCE, current_code_sha=CODE, current_git_dirty=False, current_target_sha256="f" * 64
        )
    manifest["config"]["transactions"] = ["sale"]
    with pytest.raises(ValueError):
        prepare_inventory_observation_scope(
            raw, manifest, SOURCE, current_code_sha=CODE, current_git_dirty=False, current_target_sha256=TARGET
        )


def test_prior_binding_requires_matching_source_identity_url_and_preimage():
    from source_fields.buildout import qualify_prior_inventory_binding

    listing = {"id": "listing", "brokerage_id": "broker", "external_id": "listing-key",
               "source_url": "https://example.test/listing", "raw_data": {
                   "sourceKey": SOURCE, "id": "99", "url": "https://example.test/listing"}}
    index = {"brokerage_id": "broker", "external_id": "listing-key", "source_key": SOURCE,
             "url": listing["source_url"], "observation_present": True,
             "presence_generation": 4, "last_enumerated_at": "2026-11-06T00:00:00Z"}
    bound = qualify_prior_inventory_binding(SOURCE, listing, index)
    assert bound["native_ids"] == ["99"] and bound["presence_generation"] == 4
    assert qualify_prior_inventory_binding(SOURCE, listing, {**index, "url": "https://example.test/other"}) is None
    assert qualify_prior_inventory_binding(SOURCE, listing, {**index, "source_key": "other"}) is None
    assert qualify_prior_inventory_binding(SOURCE, listing, {**index, "observation_present": False}) is None
    assert qualify_prior_inventory_binding(SOURCE, {**listing, "raw_data": {**listing["raw_data"], "id": None}}, index) is None
    assert qualify_prior_inventory_binding(SOURCE, {**listing, "raw_data": {**listing["raw_data"], "sourceKey": "other"}}, index) is None
