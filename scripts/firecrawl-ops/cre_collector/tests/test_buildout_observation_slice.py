"""Offline source-scoped non-observation classification; no DB authority."""

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
